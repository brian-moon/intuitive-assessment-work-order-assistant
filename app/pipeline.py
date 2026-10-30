"""The work-order pipeline: an explicit LangGraph state machine, one node per phase.

    guard ─refused→ END
    load_asset → data_quality → detect_anomalies → alert
    agent_draft (the current tier's model + read tools, bounded steps)
    reconcile (category rules: grounded evidence, parts, isolation, data age, priority) → confidence
    route     (model ladder, routing.py): accept ─→ approval
                                          escalate → agent_draft with the next tier (local Qwen → Claude Haiku 5.5)
                                          human ─→ approval with the best draft, or END when no tier drafted
    approval  (human in the loop: interrupt, checkpointed in PostgreSQL, survives restarts)
    generate  (numbered work-order document)  ─rejected→ END
    export    (mock CMMS) ─timeout→ approval (planner retries)
    complete

Each phase emits events ``{phase, status, label, detail, data}``. They are streamed to the browser (SSE), stored in
``run_events`` as the run's execution history, and each phase is an OpenTelemetry span.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from itertools import pairwise
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from opentelemetry import trace
from psycopg.types.json import Jsonb

from . import anomaly, workorder
from .cmms import CMMSTimeout, MockCMMS
from .db import Database
from .llm import LLMUnavailable, Tier, Usage
from .models import WorkOrderDraft
from .notify import Notifier
from .observability import trace_id_hex
from .reconcile import expected_priority, has_blocking, reconcile
from .routing import Policy, decide
from .safety import check_request
from .tools import Toolbox
from .trustplane import GovernanceSink

DATA_TOOLS = {"get_telemetry", "get_asset", "get_maintenance_history"}   # an error here is a data outage

log = logging.getLogger("app.pipeline")

LABELS = {
    "guard": "Safety check of the request", "load_asset": "Load asset and category rules",
    "data_quality": "Check data quality", "detect_anomalies": "Detect anomalies",
    "alert": "Alert on critical findings", "agent_draft": "Agent drafts the work order",
    "reconcile": "Reconcile the draft with the source data", "route": "Route by confidence",
    "approval": "Planner approval",
    "generate": "Generate the work order", "export": "Export to CMMS", "complete": "Done",
}


class RunState(TypedDict, total=False):
    run_id: str
    asset_id: str
    request: str
    started_by: str
    status: str
    reason: str
    findings: list[dict]
    worst: str
    messages: list[dict]
    draft: dict
    issues: list[dict]
    decision: dict
    work_order: str
    usage: dict
    tier: int
    outcome: str          # draft | model_failure | data_outage (the current attempt)
    attempts: list[dict]
    next: str             # escalate | approval | end
    route: str
    confidence: float


class Pipeline:
    def __init__(self, db: Database, tiers: list[Tier], toolbox: Toolbox, notifier: Notifier, cmms: MockCMMS, settings,
                 checkpointer, tracer, policy: Policy, gov: GovernanceSink):
        self.db, self.tiers, self.toolbox, self.notifier, self.cmms = db, tiers, toolbox, notifier, cmms
        self.settings, self.tracer, self.policy, self.gov = settings, tracer, policy, gov
        self._run_ctx: dict[str, Any] = {}   # run_id -> OTel context of the run span (parent of every phase span)
        self.graph = self._build(checkpointer)

    # --- events ---------------------------------------------------------------------------------------------------
    def _emit(self, state: RunState, phase: str, status: str, detail: str = "", **data: Any) -> None:
        ev = {"phase": phase, "label": LABELS.get(phase, phase), "status": status, "detail": detail,
              "t": round(time.time(), 3), **({"data": data} if data else {})}
        with self.db.pool.connection() as c:
            seq = c.execute("SELECT coalesce(max(seq), 0) + 1 AS s FROM run_events WHERE run_id = %s",
                            (state["run_id"],)).fetchone()["s"]
            c.execute("INSERT INTO run_events (run_id, seq, event) VALUES (%s,%s,%s)", (state["run_id"], seq, Jsonb(ev)))
        log.info("run=%s %s %s %s", state["run_id"], phase, status, detail)
        get_stream_writer()(ev)

    def _span(self, phase: str, state: RunState):
        """A phase span, parented explicitly on the run span: the stream is iterated by the web server's worker
        threads, so the run span cannot simply be 'current' across phases."""
        return self.tracer.start_as_current_span(f"phase.{phase}", context=self._run_ctx.get(state["run_id"]),
                                                 attributes={"run.id": state["run_id"], "run.phase": phase})

    def _trace_id(self, s: RunState) -> str | None:
        r = self.db.row("SELECT trace_id FROM runs WHERE run_id = %s", s["run_id"])
        return r["trace_id"] if r else None

    # --- phases ---------------------------------------------------------------------------------------------------
    def guard(self, s: RunState) -> dict:
        with self._span("guard", s):
            reason = check_request(s["request"])
            if reason:
                self.gov.emit("guardrail.trigger", self._trace_id(s), {
                    "gov.layer": 1, "gov.kind": "intent", "gov.action": "rejected", "gov.rule": "unsafe_request",
                    "gov.reason": reason, "run.id": s["run_id"], "asset.id": s["asset_id"]})
                self._emit(s, "guard", "failed", f"Request refused: it {reason}. No model was called.")
                return {"status": "refused", "reason": f"Request refused: it {reason}."}
            self._emit(s, "guard", "done", "No unsafe instruction found.")
            return {}

    def load_asset(self, s: RunState) -> dict:
        with self._span("load_asset", s):
            a = self.db.asset(s["asset_id"])
            cat = self.db.category(a.category)
            rules = {m: f"warning {r.warning:g} / critical {r.critical:g} {r.unit}" + (" (low)" if r.direction == "low" else "")
                     for m, r in cat.metrics.items()}
            self._emit(s, "load_asset", "done", f"{a.name}, {cat.label.lower()}, {a.criticality} criticality.",
                       asset=a.model_dump(), thresholds=rules, max_data_age_h=cat.max_data_age_h)
            return {}

    def data_quality(self, s: RunState) -> dict:
        with self._span("data_quality", s):
            stale = self.db.is_stale(s["asset_id"], self.settings.now)
            rejected = self.db.rejections_for(s["asset_id"])
            last = self.db.last_reading_at(s["asset_id"])
            notes = []
            if stale:
                notes.append(f"stale: last reading {last:%Y-%m-%d %H:%M} UTC")
            if rejected:
                notes.append(f"{rejected} reading(s) rejected as malformed")
            self._emit(s, "data_quality", "warning" if notes else "done", "; ".join(notes) or "Data is fresh and complete.",
                       stale=stale, rejected=rejected)
            return {}

    def detect_anomalies(self, s: RunState) -> dict:
        with self._span("detect_anomalies", s):
            findings = anomaly.classify_asset(self.db, s["asset_id"], self.settings.now)
            worst = anomaly.worst(findings)
            bad = [f for f in findings if f["severity"] != "normal"]
            detail = ", ".join(f"{f['metric']} {f['value']:g} {f['unit']} {f['severity']}" for f in bad) or "No anomaly."
            self._emit(s, "detect_anomalies", {"critical": "critical", "warning": "warning"}.get(worst, "done"), detail,
                       findings=[{**f, "ts": f["ts"].isoformat()} for f in findings])
            return {"findings": [{**f, "ts": f["ts"].isoformat()} for f in bad], "worst": worst}

    def alert(self, s: RunState) -> dict:
        with self._span("alert", s):
            if s.get("worst") != "critical":
                self._emit(s, "alert", "skipped", "No critical finding.")
                return {}
            findings = [f for f in anomaly.classify_asset(self.db, s["asset_id"], self.settings.now) if f["severity"] != "normal"]
            raised = self.notifier.alert(findings)
            emails = ", ".join(f"{r['to']} {r['status']}" for r in raised if r["channel"] == "email") or "no recipients"
            detail = f"Dashboard alert raised; e-mail: {emails}." if raised else "Already alerted for these readings."
            self._emit(s, "alert", "done", detail, raised=raised)
            return {}

    def agent_draft(self, s: RunState) -> dict:
        idx = s.get("tier", 0)
        tier = self.tiers[idx]
        llm = tier.llm
        with self._span("agent_draft", s) as span:
            span.set_attribute("route.tier", tier.name)
            usage = Usage()
            messages = [{"role": "user", "content": f"Asset: {s['asset_id']}\nPlanner request: {s['request']}"}]
            self._emit(s, "agent_draft", "running",
                       f"{tier.name} model {llm.model}, at most {self.settings.max_agent_steps} steps.", tier=tier.name)

            def failed(outcome: str, reason: str, **extra) -> dict:
                self._emit(s, "agent_draft", "failed", reason, tier=tier.name)
                return {"outcome": outcome, "reason": reason, "draft": None, "issues": [],
                        "usage": self._usage(s, tier.name, usage), **extra}

            data_errors = 0
            for step in range(self.settings.max_agent_steps):
                t0 = time.time()
                try:
                    with self.tracer.start_as_current_span("gen_ai.chat") as g:
                        g.set_attribute("gen_ai.system", getattr(llm, "provider", "unknown"))
                        g.set_attribute("gen_ai.request.model", llm.model)
                        g.set_attribute("route.tier", tier.name)
                        resp = llm.create(messages)
                        for k, v in (("gen_ai.usage.input_tokens", "input_tokens"),
                                     ("gen_ai.usage.output_tokens", "output_tokens")):
                            g.set_attribute(k, int(resp.usage.get(v, 0) or 0))
                        g.set_attribute("gen_ai.response.finish_reasons", [resp.stop_reason])
                except LLMUnavailable as e:
                    return failed("model_failure", str(e))
                usage.add(llm.model, resp.usage)
                messages.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason == "refusal":
                    self._emit(s, "agent_draft", "failed", "The model declined this request.", tier=tier.name)
                    return {"status": "refused", "reason": "The model declined this request.",
                            "usage": self._usage(s, tier.name, usage)}
                calls = [b for b in resp.content if b.get("type") == "tool_use"]
                if not calls:
                    text = " ".join(b.get("text", "") for b in resp.content if b.get("type") == "text").strip()
                    return failed("data_outage" if data_errors else "model_failure",
                                  text or "The agent stopped without a draft.")
                results, draft = [], None
                for call in calls:
                    with self.tracer.start_as_current_span(f"tool.{call['name']}"):
                        r = self.toolbox.run(call["name"], call.get("input", {}))
                    if r.is_error and call["name"] in DATA_TOOLS and "Historian" in r.content:
                        data_errors += 1
                    self._emit(s, "agent_draft", "running",
                               f"step {step + 1}: {call['name']}" + (f" failed: {r.content}" if r.is_error else ""),
                               tool=call["name"], is_error=r.is_error, ms=int((time.time() - t0) * 1000), tier=tier.name)
                    results.append({"type": "tool_result", "tool_use_id": call["id"], "content": r.content,
                                    **({"is_error": True} if r.is_error else {})})
                    draft = r.proposal or draft
                messages.append({"role": "user", "content": results})
                if draft is not None:
                    span.set_attribute("workorder.priority", draft.priority)
                    self._emit(s, "agent_draft", "done", f"Draft {draft.priority} ({tier.name}): {draft.problem_summary}",
                               usage=usage.as_dict(), tier=tier.name)
                    return {"outcome": "draft", "draft": draft.model_dump(mode="json"),
                            "usage": self._usage(s, tier.name, usage)}
            return failed("model_failure", f"No draft after {self.settings.max_agent_steps} steps.")

    @staticmethod
    def _usage(s: RunState, tier: str, u: Usage) -> dict:
        """Run usage: totals plus a per-tier breakdown (local tokens are free, frontier tokens are paid)."""
        total = dict(s.get("usage") or {})
        by = dict(total.get("by_tier") or {})
        mine = u.as_dict()
        prev = by.get(tier, {})
        by[tier] = {k: round(prev.get(k, 0) + v, 6) for k, v in mine.items()}
        out = {k: round(sum(t.get(k, 0) for t in by.values()), 6) for k in mine}
        return {**out, "by_tier": by}

    def reconcile(self, s: RunState) -> dict:
        with self._span("reconcile", s):
            issues = reconcile(WorkOrderDraft.model_validate(s["draft"]), self.db, self.settings.now, s["asset_id"])
            blocking = [i for i in issues if i["severity"] == "blocking"]
            conf = self.policy.confidence(issues)
            self._emit(s, "reconcile", "failed" if blocking else "warning" if issues else "done",
                       (f"{len(blocking)} blocking, {len(issues) - len(blocking)} warning(s)." if issues else
                        "Every value, part and safety field matches the source data.") + f" Confidence {conf:.2f}.",
                       issues=issues, confidence=conf)
            return {"issues": issues, "confidence": conf}

    def route(self, s: RunState) -> dict:
        with self._span("route", s) as span:
            idx = s.get("tier", 0)
            tier = self.tiers[idx]
            has_next = idx + 1 < len(self.tiers)
            outcome = s.get("outcome", "model_failure")
            conf = s.get("confidence") if outcome == "draft" else None
            # The risk of the work comes from the data (worst severity x criticality), not from the draft's own priority:
            # a model that under-rates a P1 must not slip past the P1 threshold.
            risk = expected_priority(s.get("worst", "normal"), self.db.asset(s["asset_id"]).criticality)
            d = decide(self.policy, tier.name, has_next, outcome, conf, risk)
            attempt = {"tier": tier.name, "model": tier.llm.model, "outcome": outcome, "confidence": conf, "risk": risk,
                       "threshold": d.threshold, "action": d.action, "reason": d.reason if outcome == "draft"
                       else f"{d.reason}: {s.get('reason', '')}".strip(": "),
                       "issues": [i["code"] for i in s.get("issues", [])] if outcome == "draft" else [],
                       "draft": s.get("draft"), "all_issues": s.get("issues", []) if outcome == "draft" else []}
            attempts = [*s.get("attempts", []), attempt]
            nxt = self.tiers[idx + 1] if d.action == "escalate" else None
            span.set_attribute("route.action", d.action)
            gov_route = {"accept": "review_queue", "escalate": "escalate_model", "human": "reject"}[d.action]
            attrs = {"gov.layer": 5, "gov.route": gov_route, "gov.confidence": conf if conf is not None else 0.0,
                     "gov.threshold": d.threshold if d.threshold is not None else 1.0, "gov.rule": d.reason,
                     "gov.tier": tier.name, "gov.model": tier.llm.model, "gov.risk": risk, "gov.issues": attempt["issues"],
                     "run.id": s["run_id"], "asset.id": s["asset_id"]}
            if nxt:
                attrs["gov.model_escalation"] = {"from": tier.llm.model, "to": nxt.llm.model, "reason": attempt["reason"]}
            self.gov.emit("confidence.route", self._trace_id(s), attrs, actor={"kind": "system", "id": "router"})

            if d.action == "escalate":
                self._emit(s, "route", "warning", f"{tier.name}: {attempt['reason']} → escalate to {nxt.name} ({nxt.llm.model}).",
                           **{k: v for k, v in attempt.items() if k not in ("draft", "all_issues")})
                return {"attempts": attempts, "tier": idx + 1, "next": "escalate", "draft": None, "issues": [],
                        "outcome": "", "confidence": None}
            if d.action == "accept":
                self._emit(s, "route", "done", f"{tier.name} draft accepted: {attempt['reason']}.",
                           **{k: v for k, v in attempt.items() if k not in ("draft", "all_issues")})
                self._waiting(s, s["draft"], s.get("issues", []), tier.name)
                return {"attempts": attempts, "next": "approval", "route": tier.name}
            # human: the planner takes over, with the best draft any tier produced (if one did)
            drafts = [a for a in attempts if a["draft"]]
            if not drafts:
                why = attempt["reason"]
                self._emit(s, "route", "failed", f"No tier produced a usable draft ({why}). Handed to a planner.",
                           **{k: v for k, v in attempt.items() if k not in ("draft", "all_issues")})
                return {"attempts": attempts, "next": "end", "route": "human", "status": "failed",
                        "reason": f"{s.get('reason') or why} Handed to a planner."}
            best = max(drafts, key=lambda a: a["confidence"] or 0)
            self._emit(s, "route", "warning",
                       f"Below every threshold; a planner must check this draft field by field (best: {best['tier']}, "
                       f"confidence {best['confidence']:.2f}).",
                       **{k: v for k, v in attempt.items() if k not in ("draft", "all_issues")})
            self._waiting(s, best["draft"], best["all_issues"], "human")
            return {"attempts": attempts, "next": "approval", "route": "human", "draft": best["draft"],
                    "issues": best["all_issues"], "confidence": best["confidence"]}

    def _waiting(self, s: RunState, draft: dict, issues: list[dict], route: str | None = None) -> None:
        can = not has_blocking(issues)
        route = route or s.get("route")
        note = " Escalated to a planner: the AI draft is below every confidence threshold." if route == "human" else ""
        self._emit(s, "approval", "waiting", ("Waiting for a planner." if can else
                   "Approval is disabled: the draft has blocking issues. Reject it or start a new run.") + note,
                   draft=draft, issues=issues, can_approve=can, route=route)

    def approval(self, s: RunState) -> dict:
        # The node re-runs from the top on resume, so the "waiting" event is emitted by the phase before it.
        decision = interrupt({"draft": s["draft"], "issues": s.get("issues", [])})
        kind = "correct" if decision["decision"] == "approve" and decision.get("edited_priority") else decision["decision"]
        waited = self.db.row("SELECT (extract(epoch FROM now() - max(created_at)) * 1000)::int AS ms FROM run_events "
                             "WHERE run_id = %s AND event->>'phase' = 'approval' AND event->>'status' = 'waiting'",
                             s["run_id"])
        wait_ms = (waited or {}).get("ms")
        self.db.exec("UPDATE runs SET decided_by = %s, decision = %s, wait_ms = coalesce(wait_ms, %s) WHERE run_id = %s",
                     decision.get("by"), kind, wait_ms, s["run_id"])
        self.gov.emit("hitl.decision", self._trace_id(s), {
            "gov.layer": 5, "gov.decision": kind, "gov.queue_time_ms": wait_ms or 0, "gov.route": s.get("route"),
            "gov.evidence_refs": [f"run:{s['run_id']}/draft", f"run:{s['run_id']}/reconcile"],
            "run.id": s["run_id"], "asset.id": s["asset_id"]},
            actor={"kind": "human", "id": decision.get("by") or "planner"})
        self._emit(s, "approval", "done", f"{decision['decision']} by {decision.get('by')}")
        return {"decision": decision, "status": "deciding"}

    def generate(self, s: RunState) -> dict:
        with self._span("generate", s):
            d = s["decision"]
            if d["decision"] == "reject":
                self._emit(s, "generate", "skipped", "Rejected by the planner; nothing is generated.")
                return {"status": "rejected", "reason": d.get("reason") or "Rejected by planner."}
            if s.get("work_order"):
                self._emit(s, "generate", "skipped", f"{s['work_order']} already generated; retrying the export.")
                return {}
            draft = WorkOrderDraft.model_validate(s["draft"])
            if d.get("edited_priority"):
                draft = draft.model_copy(update={"priority": d["edited_priority"]})
            wo = workorder.generate(self.db, s["run_id"], draft, d.get("by", "planner"), self.settings.now)
            self._emit(s, "generate", "done", f"{wo['work_order']}: {wo['title']}", work_order=wo["work_order"],
                       document_md=wo["document_md"])
            return {"work_order": wo["work_order"], "draft": draft.model_dump(mode="json")}

    def export(self, s: RunState) -> dict:
        with self._span("export", s):
            try:
                ref = self.cmms.export(s["work_order"], s["draft"])
            except CMMSTimeout as e:
                self._emit(s, "export", "failed", f"{e}. The work order is saved; approve again to retry the export.")
                self._waiting(s, s["draft"], s.get("issues", []))
                return {"status": "export_failed", "reason": f"{e}. Approve again to retry."}
            self.db.exec("UPDATE work_orders SET cmms_status = 'exported' WHERE work_order = %s", s["work_order"])
            self._emit(s, "export", "done", f"Exported as {ref}.")
            self._emit(s, "complete", "done", f"{s['work_order']} created.")
            return {"status": "created", "reason": ""}

    # --- graph ----------------------------------------------------------------------------------------------------
    def _build(self, checkpointer):
        g = StateGraph(RunState)
        order = ["guard", "load_asset", "data_quality", "detect_anomalies", "alert", "agent_draft", "reconcile",
                 "route", "approval", "generate", "export"]
        for name in order:
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "guard")
        g.add_conditional_edges("guard", lambda s: END if s.get("status") == "refused" else "load_asset")
        for a, b in pairwise(order[1:6]):
            g.add_edge(a, b)
        g.add_conditional_edges("agent_draft", lambda s: END if s.get("status") == "refused"
                                else "reconcile" if s.get("draft") else "route")
        g.add_edge("reconcile", "route")
        g.add_conditional_edges("route", lambda s: {"escalate": "agent_draft", "approval": "approval"}.get(s.get("next"), END))
        g.add_edge("approval", "generate")
        g.add_conditional_edges("generate", lambda s: END if s.get("status") == "rejected" else "export")
        g.add_conditional_edges("export", lambda s: "approval" if s.get("status") == "export_failed" else END)
        return g.compile(checkpointer=checkpointer)

    # --- entry points (generators of events, for SSE) -------------------------------------------------------------
    def _drive(self, run_id: str, payload, root: str) -> Iterator[dict]:
        cfg = {"configurable": {"thread_id": run_id}}
        span = self.tracer.start_span(root, attributes={"run.id": run_id})
        self._run_ctx[run_id] = trace.set_span_in_context(span)
        self.db.exec("UPDATE runs SET trace_id = coalesce(trace_id, %s) WHERE run_id = %s", trace_id_hex(span), run_id)
        status, v = "failed", {}
        try:
            yield from self.graph.stream(payload, cfg, stream_mode="custom")
        finally:
            # Always record where the run really is, also when the stream breaks (client gone, error): a run that was
            # claimed for a decision returns to awaiting_approval instead of staying stuck in "deciding".
            span.end()
            self._run_ctx.pop(run_id, None)
            snap = self.graph.get_state(cfg)
            v = snap.values or {}
            waiting = snap.next == ("approval",)
            status = "awaiting_approval" if waiting else v.get("status", "failed")
            if status == "deciding":
                # The decision was recorded but the stream broke before the run finished: the run must not take a
                # second decision. It can only be continued (POST /api/runs/{id}/continue/stream).
                status = "interrupted"
            attempts = [{k: a[k] for k in a if k not in ("draft", "all_issues")} for a in v.get("attempts", [])]
            self.db.exec("UPDATE runs SET status = %s, priority = %s, reason = %s, usage = %s, route = %s, "
                         "confidence = %s, attempts = %s, updated_at = now() WHERE run_id = %s", status,
                         (v.get("draft") or {}).get("priority"), v.get("reason", ""), Jsonb(v.get("usage", {})),
                         v.get("route"), v.get("confidence"), Jsonb(attempts), run_id)
        yield {"phase": "run", "status": status, "label": "Run status", "detail": v.get("reason", ""), "run_id": run_id}

    def start(self, run_id: str, asset_id: str, request: str, user: str) -> Iterator[dict]:
        self.db.exec("INSERT INTO runs (run_id, asset_id, request, started_by, status) VALUES (%s,%s,%s,%s,'running')",
                     run_id, asset_id, request, user)
        yield from self._drive(run_id, {"run_id": run_id, "asset_id": asset_id, "request": request, "started_by": user},
                               "run.start")

    def resume(self, run_id: str, decision: dict) -> Iterator[dict]:
        yield from self._drive(run_id, Command(resume=decision), "run.resume")   # the API has already claimed the run

    def resume_interrupted(self, run_id: str) -> Iterator[dict]:
        """Finish a run whose decision was already recorded (stream broke mid-way); takes no new decision."""
        yield from self._drive(run_id, None, "run.continue")

    def state(self, run_id: str) -> dict | None:
        snap = self.graph.get_state({"configurable": {"thread_id": run_id}})
        if not snap.values:
            return None
        return {**snap.values, "waiting": snap.next == ("approval",)}
