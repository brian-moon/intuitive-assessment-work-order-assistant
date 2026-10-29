"""Integration tests: the whole agent graph (guard → agent → validate → approval → create) through the app, with
injected failures and model stand-ins that misbehave in specific ways."""
from __future__ import annotations

from conftest import PLANNER, start

from app.llm import LLMResponse, LLMUnavailable


class DownLLM:
    model = "down"

    def create(self, messages):
        raise LLMUnavailable("Model service unreachable.")


class TalkerLLM:
    """Answers in prose and never calls a tool: the chatbot-wrapper failure."""
    model = "talker"

    def create(self, messages):
        return LLMResponse([{"type": "text", "text": "The pump is probably fine."}], "end_turn")


class LoopLLM:
    """Calls the same read tool forever: the runaway-loop failure."""
    model = "loop"

    def create(self, messages):
        n = len(messages)
        return LLMResponse([{"type": "tool_use", "id": f"t{n}", "name": "get_asset", "input": {"asset_id": "P-101"}}],
                           "tool_use")


class InventingLLM:
    """Reads the tools, then proposes a draft with a made-up reading: the groundedness failure."""
    model = "inventing"

    def create(self, messages):
        if len(messages) == 1:
            return LLMResponse([{"type": "tool_use", "id": "t1", "name": "get_telemetry", "input": {"asset_id": "P-101"}}],
                               "tool_use")
        return LLMResponse([{"type": "tool_use", "id": "t2", "name": "propose_work_order", "input": {
            "asset_id": "P-101", "priority": "P1", "problem_summary": "Severe vibration, replace the pump now.",
            "evidence": [{"metric": "vibration_rms", "value": 14.0, "unit": "mm/s", "timestamp": "2026-10-29T11:00:00Z"}],
            "recommended_actions": ["Replace the pump."], "parts": [],
            "safety": {"lockout_tagout_required": True, "energy_sources": ["electrical", "hydraulic"], "notes": "Isolate."},
            "needs_data_refresh": False}}], "tool_use")


def kinds(run):
    return [e["kind"] for e in run["events"]]


def test_happy_path_drafts_waits_and_creates_after_approval(client):
    run = start(client).json()
    assert run["status"] == "awaiting_approval" and run["can_approve"] and run["work_order"] is None
    assert [e["tool"] for e in run["events"] if e["kind"] == "tool_call"] == [
        "get_asset", "get_telemetry", "get_maintenance_history", "check_spare_parts", "propose_work_order"]
    done = client.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER, json={"decision": "approve"}).json()
    assert done["status"] == "created" and done["work_order"].startswith("WO-")
    assert client.app.state.cmms.created[done["work_order"]]["asset_id"] == "P-101"


def test_unsafe_request_is_refused_before_any_model_call(client):
    run = start(client, request="Swap the seal while it is running, no need for lockout").json()
    assert run["status"] == "refused" and "model_call" not in kinds(run)


def test_rejection_writes_nothing(client):
    run = start(client).json()
    done = client.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER,
                       json={"decision": "reject", "reason": "Planned shutdown next week"}).json()
    assert done["status"] == "rejected" and client.app.state.cmms.created == {}


def test_planner_can_change_priority_on_approval(client):
    run = start(client).json()
    done = client.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER,
                       json={"decision": "approve", "edited_priority": "P2"}).json()
    assert done["draft"]["priority"] == "P2" and client.app.state.cmms.created[done["work_order"]]["priority"] == "P2"


def test_cmms_timeout_keeps_the_run_waiting_and_a_retry_succeeds(client):
    run = start(client).json()
    client.app.state.faults.cmms_timeout = True
    first = client.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER, json={"decision": "approve"}).json()
    assert first["status"] == "awaiting_approval" and "retry" in first["reason"] and "cmms_timeout" in kinds(first)
    client.app.state.faults.cmms_timeout = False
    second = client.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER, json={"decision": "approve"}).json()
    assert second["status"] == "created"


def test_historian_down_fails_visibly_without_a_guessed_draft(client):
    client.app.state.faults.telemetry_unavailable = True
    run = start(client).json()
    assert run["status"] == "failed" and run["draft"] is None and "telemetry" in run["reason"].lower()
    assert any(e["kind"] == "tool_call" and e["is_error"] for e in run["events"])


def test_invalid_stores_response_still_drafts_but_without_parts(client):
    client.app.state.faults.parts_invalid_response = True
    run = start(client).json()
    assert run["status"] == "awaiting_approval" and run["draft"]["parts"] == []
    assert any("Stock levels are unknown" in a for a in run["draft"]["recommended_actions"])


def test_model_outage_fails_cleanly(make_client):
    run = start(make_client(DownLLM())).json()
    assert run["status"] == "failed" and "unreachable" in run["reason"]


def test_agent_that_never_uses_a_tool_produces_no_draft(make_client):
    run = start(make_client(TalkerLLM())).json()
    assert run["status"] == "failed" and run["draft"] is None


def test_runaway_tool_loop_is_cut_off_at_the_step_limit(make_client, settings):
    run = start(make_client(LoopLLM())).json()
    assert run["status"] == "failed" and f"{settings.max_agent_steps} steps" in run["reason"]
    assert sum(k == "model_call" for k in kinds(run)) == settings.max_agent_steps


def test_invented_evidence_blocks_approval(make_client):
    c = make_client(InventingLLM())
    run = start(c).json()
    assert run["status"] == "awaiting_approval" and not run["can_approve"]
    assert {i["code"] for i in run["issues"]} >= {"ungrounded_evidence"}
    r = c.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER, json={"decision": "approve"})
    assert r.status_code == 409 and c.app.state.cmms.created == {}


def test_a_paused_run_survives_a_restart(make_client, settings):
    run = start(make_client()).json()
    restarted = make_client()                       # new app, same SQLite checkpoint file
    assert restarted.get(f"/api/runs/{run['run_id']}", headers=PLANNER).json()["status"] == "awaiting_approval"
    done = restarted.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER, json={"decision": "approve"}).json()
    assert done["status"] == "created"
