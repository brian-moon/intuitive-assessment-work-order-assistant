"""The work-order pipeline: an explicit LangGraph state machine, one node per phase.

    guard ─refused→ END
    load_asset → data_quality → detect_anomalies → alert
    agent_draft (Claude Haiku 5.5 + read tools, bounded steps) ─no draft→ END
    reconcile (category rules: grounded evidence, parts, isolation, data age, priority)
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
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from opentelemetry import trace
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.types.json import Jsonb

from . import anomaly, workorder
from .cmms import CMMSTimeout, MockCMMS
from .db import Database
from .llm import LLM, LLMUnavailable, Usage
from .models import WorkOrderDraft
from .notify import Notifier
from .observability import trace_id_hex
from .reconcile import has_blocking, reconcile
from .safety import check_request
from .tools import Toolbox

log = logging.getLogger("app.pipeline")

LABELS = {
    "guard": "Safety check of the request", "load_asset": "Load asset and category rules",
    "data_quality": "Check data quality", "detect_anomalies": "Detect anomalies",
    "alert": "Alert on critical findings", "agent_draft": "Agent drafts the work order",
    "reconcile": "Reconcile the draft with the source data", "approval": "Planner approval",
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


class Pipeline:
    def __init__(self, db: Database, llm: LLM, toolbox: Toolbox, notifier: Notifier, cmms: MockCMMS, settings,
                 checkpointer, tracer):
        self.db, self.llm, self.toolbox, self.notifier, self.cmms = db, llm, toolbox, notifier, cmms
        self.settings, self.tracer = settings, tracer
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

    # --- phases ---------------------------------------------------------------------------------------------------
    def guard(self, s: RunState) -> dict:
        with self._span("guard", s):
            reason = check_request(s["request"])
            if reason:
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
        with self._span("agent_draft", s) as span:
            usage = Usage()
            messages = [{"role": "user", "content": f"Asset: {s['asset_id']}\nPlanner request: {s['request']}"}]
            self._emit(s, "agent_draft", "running", f"Model {self.llm.model}, at most {self.settings.max_agent_steps} steps.")
            for step in range(self.settings.max_agent_steps):
                t0 = time.time()
                try:
                    with self.tracer.start_as_current_span("gen_ai.chat") as g:
                        g.set_attribute("gen_ai.system", "anthropic")
                        g.set_attribute("gen_ai.request.model", self.llm.model)
                        resp = self.llm.create(messages)
                        for k, v in (("gen_ai.usage.input_tokens", "input_tokens"),
                                     ("gen_ai.usage.output_tokens", "output_tokens")):
                            g.set_attribute(k, int(resp.usage.get(v, 0) or 0))
                        g.set_attribute("gen_ai.response.finish_reasons", [resp.stop_reason])
                except LLMUnavailable as e:
                    self._emit(s, "agent_draft", "failed", str(e))
                    return {"status": "failed", "reason": str(e), "usage": usage.as_dict()}
                usage.add(self.llm.model, resp.usage)
                messages.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason == "refusal":
                    self._emit(s, "agent_draft", "failed", "The model declined this request.")
                    return {"status": "refused", "reason": "The model declined this request.", "usage": usage.as_dict()}
                calls = [b for b in resp.content if b.get("type") == "tool_use"]
                if not calls:
                    text = " ".join(b.get("text", "") for b in resp.content if b.get("type") == "text").strip()
                    self._emit(s, "agent_draft", "failed", text or "The agent stopped without a draft.")
                    return {"status": "failed", "reason": text or "The agent stopped without a draft.",
                            "messages": messages, "usage": usage.as_dict()}
                results, draft = [], None
                for call in calls:
                    with self.tracer.start_as_current_span(f"tool.{call['name']}"):
                        r = self.toolbox.run(call["name"], call.get("input", {}))
                    self._emit(s, "agent_draft", "running",
                               f"step {step + 1}: {call['name']}" + (f" failed: {r.content}" if r.is_error else ""),
                               tool=call["name"], is_error=r.is_error, ms=int((time.time() - t0) * 1000))
                    results.append({"type": "tool_result", "tool_use_id": call["id"], "content": r.content,
                                    **({"is_error": True} if r.is_error else {})})
                    draft = r.proposal or draft
                messages.append({"role": "user", "content": results})
                if draft is not None:
                    span.set_attribute("workorder.priority", draft.priority)
                    self._emit(s, "agent_draft", "done", f"Draft {draft.priority}: {draft.problem_summary}",
                               usage=usage.as_dict())
                    return {"draft": draft.model_dump(mode="json"), "messages": messages, "usage": usage.as_dict()}
            reason = f"No draft after {self.settings.max_agent_steps} steps."
            self._emit(s, "agent_draft", "failed", reason)
            return {"status": "failed", "reason": reason, "messages": messages, "usage": usage.as_dict()}

    def reconcile(self, s: RunState) -> dict:
        with self._span("reconcile", s):
            issues = reconcile(WorkOrderDraft.model_validate(s["draft"]), self.db, self.settings.now, s["asset_id"])
            blocking = [i for i in issues if i["severity"] == "blocking"]
            self._emit(s, "reconcile", "failed" if blocking else "warning" if issues else "done",
                       f"{len(blocking)} blocking, {len(issues) - len(blocking)} warning(s)." if issues else
                       "Every value, part and safety field matches the source data.", issues=issues)
            self._waiting(s, s["draft"], issues)
            return {"issues": issues}

    def _waiting(self, s: RunState, draft: dict, issues: list[dict]) -> None:
        can = not has_blocking(issues)
        self._emit(s, "approval", "waiting", "Waiting for a planner." if can else
                   "Approval is disabled: the draft has blocking issues. Reject it or start a new run.",
                   draft=draft, issues=issues, can_approve=can)

    def approval(self, s: RunState) -> dict:
        # The node re-runs from the top on resume, so the "waiting" event is emitted by the phase before it.
        decision = interrupt({"draft": s["draft"], "issues": s.get("issues", [])})
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
                 "approval", "generate", "export"]
        for name in order:
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "guard")
        g.add_conditional_edges("guard", lambda s: END if s.get("status") == "refused" else "load_asset")
        for a, b in zip(order[1:5], order[2:6]):
            g.add_edge(a, b)
        g.add_conditional_edges("agent_draft", lambda s: "reconcile" if s.get("draft") and not s.get("status") else END)
        g.add_edge("reconcile", "approval")
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
        try:
            for ev in self.graph.stream(payload, cfg, stream_mode="custom"):
                yield ev
        finally:
            span.end()
            self._run_ctx.pop(run_id, None)
        snap = self.graph.get_state(cfg)
        v = snap.values
        waiting = bool(snap.next) and snap.next[0] == "approval"
        status = "awaiting_approval" if waiting else v.get("status", "failed")
        self.db.exec("UPDATE runs SET status = %s, priority = %s, reason = %s, usage = %s, updated_at = now() "
                     "WHERE run_id = %s", status, (v.get("draft") or {}).get("priority"), v.get("reason", ""),
                     Jsonb(v.get("usage", {})), run_id)
        yield {"phase": "run", "status": status, "label": "Run status", "detail": v.get("reason", ""), "run_id": run_id}

    def start(self, run_id: str, asset_id: str, request: str, user: str) -> Iterator[dict]:
        self.db.exec("INSERT INTO runs (run_id, asset_id, request, started_by, status) VALUES (%s,%s,%s,%s,'running')",
                     run_id, asset_id, request, user)
        yield from self._drive(run_id, {"run_id": run_id, "asset_id": asset_id, "request": request, "started_by": user},
                               "run.start")

    def resume(self, run_id: str, decision: dict) -> Iterator[dict]:
        self.db.exec("UPDATE runs SET status = 'running', updated_at = now() WHERE run_id = %s", run_id)
        yield from self._drive(run_id, Command(resume=decision), "run.resume")

    def state(self, run_id: str) -> dict | None:
        snap = self.graph.get_state({"configurable": {"thread_id": run_id}})
        if not snap.values:
            return None
        waiting = bool(snap.next) and snap.next[0] == "approval"
        return {**snap.values, "waiting": waiting}
