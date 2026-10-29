"""Agent orchestration as an explicit LangGraph state machine.

    guard ──refused──► END
      │
    agent (model + read tools, bounded steps) ──no draft──► END   (failed: says what is missing)
      │
    validate (deterministic groundedness and safety checks)
      │
    approval (interrupt: waits for a planner, survives restarts via the SQLite checkpoint)
      │
    create (the only write: mock CMMS) ──timeout──► approval   (planner can retry)
      │
     END

Every step appends to ``events``: the run's execution history, returned by ``GET /api/runs/{id}``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .cmms import CMMSTimeout, MockCMMS
from .llm import LLM, LLMUnavailable, Usage
from .models import WorkOrderDraft
from .safety import check_request
from .tools import Toolbox
from .validation import check_draft, has_blocking

log = logging.getLogger("app.agent")


class RunState(TypedDict, total=False):
    run_id: str
    asset_id: str
    request: str
    status: str            # refused | failed | awaiting_approval | rejected | created
    reason: str
    messages: list[dict]
    draft: dict
    issues: list[dict]
    decision: dict
    work_order: str
    usage: dict
    events: list[dict]


def _event(state: RunState, kind: str, **data: Any) -> list[dict]:
    ev = {"t": round(time.time(), 3), "kind": kind, **data}
    log.info("run=%s %s %s", state.get("run_id"), kind, data)
    return [*state.get("events", []), ev]


def build_graph(llm: LLM, toolbox: Toolbox, cmms: MockCMMS, settings, checkpointer):
    def guard(state: RunState) -> dict:
        reason = check_request(state["request"])
        if reason:
            return {"status": "refused", "reason": f"Request refused: it {reason}.",
                    "events": _event(state, "refused", reason=reason)}
        return {"events": _event(state, "accepted")}

    def agent(state: RunState) -> dict:
        usage = Usage()
        messages = [{"role": "user", "content": f"Asset: {state['asset_id']}\nPlanner request: {state['request']}"}]
        events = state.get("events", [])
        for step in range(settings.max_agent_steps):
            t0 = time.time()
            try:
                resp = llm.create(messages)
            except LLMUnavailable as e:
                return {"status": "failed", "reason": str(e), "usage": usage.as_dict(),
                        "events": [*events, {"kind": "model_error", "step": step, "error": str(e)}]}
            usage.add(llm.model, resp.usage)
            events = [*events, {"kind": "model_call", "step": step, "stop_reason": resp.stop_reason,
                                "ms": int((time.time() - t0) * 1000), "usage": resp.usage}]
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return {"status": "refused", "reason": "The model declined this request.", "messages": messages,
                        "usage": usage.as_dict(), "events": events}
            calls = [b for b in resp.content if b.get("type") == "tool_use"]
            if not calls:
                text = " ".join(b.get("text", "") for b in resp.content if b.get("type") == "text").strip()
                return {"status": "failed", "reason": text or "The agent stopped without a draft.",
                        "messages": messages, "usage": usage.as_dict(), "events": events}
            results, draft = [], None
            for call in calls:
                t1 = time.time()
                r = toolbox.run(call["name"], call.get("input", {}))
                events = [*events, {"kind": "tool_call", "step": step, "tool": call["name"], "input": call.get("input"),
                                    "is_error": r.is_error, "ms": int((time.time() - t1) * 1000),
                                    **({"error": r.content} if r.is_error else {})}]
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": r.content,
                                **({"is_error": True} if r.is_error else {})})
                draft = r.proposal or draft
            messages.append({"role": "user", "content": results})   # all results of one turn in one message
            if draft is not None:
                return {"draft": draft.model_dump(mode="json"), "messages": messages, "usage": usage.as_dict(),
                        "events": [*events, {"kind": "drafted", "priority": draft.priority}]}
        return {"status": "failed", "reason": f"No draft after {settings.max_agent_steps} steps.",
                "messages": messages, "usage": usage.as_dict(), "events": events}

    def validate(state: RunState) -> dict:
        draft = WorkOrderDraft.model_validate(state["draft"])
        issues = check_draft(draft, toolbox.repo, settings.now, settings.stale_after_hours)
        return {"issues": issues, "status": "awaiting_approval",
                "events": _event(state, "validated", blocking=sum(i["severity"] == "blocking" for i in issues),
                                 warnings=sum(i["severity"] == "warning" for i in issues))}

    def approval(state: RunState) -> dict:
        decision = interrupt({"draft": state["draft"], "issues": state.get("issues", [])})
        return {"decision": decision, "events": _event(state, "decision", **decision)}

    def create(state: RunState) -> dict:
        d = state["decision"]
        if d["decision"] == "reject":
            return {"status": "rejected", "reason": d.get("reason") or "Rejected by planner.",
                    "events": _event(state, "rejected")}
        if has_blocking(state.get("issues", [])):   # defence in depth: the API already refuses this
            return {"status": "awaiting_approval", "reason": "Draft has blocking issues; it cannot be approved.",
                    "events": _event(state, "approve_blocked")}
        draft = WorkOrderDraft.model_validate(state["draft"])
        if d.get("edited_priority"):
            draft = draft.model_copy(update={"priority": d["edited_priority"]})
        try:
            wo = cmms.create_work_order(draft, approved_by=d.get("by", "planner"))
        except CMMSTimeout as e:
            return {"status": "awaiting_approval", "reason": f"{e}. Approve again to retry.",
                    "events": _event(state, "cmms_timeout", error=str(e))}
        return {"status": "created", "work_order": wo, "draft": draft.model_dump(mode="json"), "reason": "",
                "events": _event(state, "created", work_order=wo)}

    g = StateGraph(RunState)
    for name, fn in (("guard", guard), ("agent", agent), ("validate", validate), ("approval", approval),
                     ("create", create)):
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", lambda s: END if s.get("status") == "refused" else "agent")
    g.add_conditional_edges("agent", lambda s: "validate" if s.get("draft") and not s.get("status") else END)
    g.add_edge("validate", "approval")
    g.add_edge("approval", "create")
    g.add_conditional_edges("create", lambda s: "approval" if s.get("status") == "awaiting_approval" else END)
    return g.compile(checkpointer=checkpointer)


def resume(graph, run_id: str, decision: dict):
    return graph.invoke(Command(resume=decision), config={"configurable": {"thread_id": run_id}})
