"""Integration tests: the whole pipeline through the streamed API, with injected failures and model stand-ins that
misbehave in specific ways."""
from __future__ import annotations

from conftest import auth, decide, events, phases, run

from app.llm import LLMResponse, LLMUnavailable

ORDER = ["guard", "load_asset", "data_quality", "detect_anomalies", "alert", "agent_draft", "reconcile", "approval"]


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
        return LLMResponse([{"type": "tool_use", "id": f"t{len(messages)}", "name": "get_asset",
                             "input": {"asset_id": "P-101"}}], "tool_use")


class InventingLLM:
    """Proposes a draft with a made-up reading: the groundedness failure."""
    model = "inventing"

    def create(self, messages):
        return LLMResponse([{"type": "tool_use", "id": "t1", "name": "propose_work_order", "input": {
            "asset_id": "P-101", "priority": "P1", "problem_summary": "Severe vibration, replace the pump now.",
            "evidence": [{"metric": "vibration_rms", "value": 14.0, "unit": "mm/s", "timestamp": "2026-10-29T11:00:00Z"}],
            "recommended_actions": ["Replace the pump."], "parts": [],
            "safety": {"lockout_tagout_required": True, "energy_sources": ["electrical", "hydraulic"], "notes": "x"},
            "needs_data_refresh": False}}], "tool_use")


def test_phases_stream_in_order_and_the_run_waits(client):
    run_id, evs = run(client)
    seen = list(dict.fromkeys(p for p, _ in phases(evs) if p != "run"))
    assert seen == ORDER
    assert evs[-1] == {**evs[-1], "phase": "run", "status": "awaiting_approval"}
    tools = [e["data"]["tool"] for e in evs if e["phase"] == "agent_draft" and "tool" in e.get("data", {})]
    assert tools == ["get_asset", "get_telemetry", "get_maintenance_history", "check_spare_parts", "propose_work_order"]


def test_approval_generates_and_exports_a_work_order(client):
    run_id, _ = run(client)
    evs = decide(client, run_id)
    assert [p for p, s in phases(evs) if s == "done"] == ["approval", "generate", "export", "complete"]
    wo = client.get("/api/work-orders", headers=auth(client, "viewer")).json()
    assert len(wo) == 1 and wo[0]["cmms_status"] == "exported" and wo[0]["approved_by"] == "planner"


def test_critical_anomaly_is_on_the_dashboard_and_emailed(client, mailbox):
    admin = auth(client, "admin")
    assert client.post("/api/admin/recipients", headers=admin, json={"name": "Ops", "email": "ops@x.org"}).status_code == 201
    dash = client.get("/api/dashboard", headers=admin).json()
    assert any("P-101 vibration_rms" in a["subject"] for a in dash["alerts"])
    _, evs = run(client, "P-101")
    alert = next(e for e in evs if e["phase"] == "alert")
    assert alert["status"] == "done" and "ops@x.org sent" in alert["detail"]
    assert [m["to"] for m in mailbox] == ["ops@x.org"] and "[CRITICAL] P-101" in mailbox[0]["subject"]
    _, evs = run(client, "P-101")                                  # same readings: no second e-mail
    assert len(mailbox) == 1 and "Already alerted" in next(e for e in evs if e["phase"] == "alert")["detail"]


def test_no_alert_phase_for_an_asset_without_critical_findings(client):
    _, evs = run(client, "P-102")
    assert ("alert", "skipped") in phases(evs)


def test_unsafe_request_is_refused_before_any_model_call(client):
    _, evs = run(client, request="Swap the seal while it is running, no need for lockout")
    assert phases(evs) == [("guard", "failed"), ("run", "refused")]


def test_rejection_generates_nothing(client):
    run_id, _ = run(client)
    evs = decide(client, run_id, "reject", reason="Planned shutdown next week")
    assert ("generate", "skipped") in phases(evs) and evs[-1]["status"] == "rejected"
    assert client.get("/api/work-orders", headers=auth(client, "viewer")).json() == []


def test_planner_can_change_priority_on_approval(client):
    run_id, _ = run(client)
    decide(client, run_id, edited_priority="P2")
    assert client.get("/api/work-orders", headers=auth(client, "viewer")).json()[0]["priority"] == "P2"


def test_cmms_timeout_keeps_the_work_order_and_a_retry_exports_it(client):
    run_id, _ = run(client)
    client.app.state.faults.cmms_timeout = True
    evs = decide(client, run_id)
    assert ("export", "failed") in phases(evs) and evs[-1]["status"] == "awaiting_approval"
    assert client.get("/api/work-orders", headers=auth(client, "viewer")).json()[0]["cmms_status"] == "pending_export"
    client.app.state.faults.cmms_timeout = False
    evs = decide(client, run_id)
    assert ("generate", "skipped") in phases(evs) and evs[-1]["status"] == "created"
    wo = client.get("/api/work-orders", headers=auth(client, "viewer")).json()
    assert len(wo) == 1 and wo[0]["cmms_status"] == "exported"


def test_historian_down_fails_visibly_without_a_guessed_draft(client):
    client.app.state.faults.telemetry_unavailable = True
    run_id, evs = run(client)
    assert evs[-1]["status"] == "failed" and ("reconcile", "done") not in phases(evs)
    assert any(e["phase"] == "agent_draft" and e.get("data", {}).get("is_error") for e in evs)


def test_invalid_stores_response_drafts_without_parts(client):
    client.app.state.faults.parts_invalid_response = True
    run_id, evs = run(client)
    draft = next(e for e in evs if e["phase"] == "approval")["data"]["draft"]
    assert draft["parts"] == [] and any("Stock levels are unknown" in a for a in draft["recommended_actions"])


def test_model_outage_fails_cleanly(make_client):
    _, evs = run(make_client(DownLLM()))
    assert ("agent_draft", "failed") in phases(evs) and evs[-1]["status"] == "failed"


def test_agent_that_never_uses_a_tool_produces_no_draft(make_client):
    _, evs = run(make_client(TalkerLLM()))
    assert evs[-1]["status"] == "failed" and not any(e["phase"] == "approval" for e in evs)


def test_runaway_tool_loop_is_cut_off_at_the_step_limit(make_client, settings):
    _, evs = run(make_client(LoopLLM()))
    assert evs[-1]["status"] == "failed" and f"{settings.max_agent_steps} steps" in evs[-1]["detail"]


def test_invented_evidence_blocks_approval(make_client):
    c = make_client(InventingLLM())
    run_id, evs = run(c)
    waiting = next(e for e in evs if e["phase"] == "approval")
    assert waiting["data"]["can_approve"] is False
    assert "ungrounded_evidence" in {i["code"] for i in waiting["data"]["issues"]}
    r = c.post(f"/api/runs/{run_id}/decision/stream", headers=auth(c, "planner"), json={"decision": "approve"})
    assert r.status_code == 409


def test_a_paused_run_survives_a_restart(make_client):
    run_id, _ = run(make_client())
    restarted = make_client(reset=False)                 # new app process, same PostgreSQL checkpoint
    assert restarted.get(f"/api/runs/{run_id}", headers=auth(restarted, "viewer")).json()["status"] == "awaiting_approval"
    assert decide(restarted, run_id)[-1]["status"] == "created"


def test_every_phase_is_traced(client):
    run_id, _ = run(client)
    spans = {s["name"] for s in client.get(f"/api/runs/{run_id}/trace", headers=auth(client, "viewer")).json()["spans"]}
    assert {f"phase.{p}" for p in ORDER if p != "approval"} <= spans and "gen_ai.chat" in spans


def test_a_decision_interrupted_mid_stream_cannot_be_decided_again(client):
    """The stream breaks after the decision was recorded: the run becomes 'interrupted', refuses a second decision,
    and can only be continued to finish the decision already taken."""
    planner = auth(client, "planner")
    run_id, _ = run(client)
    pipe = client.app.state.pipeline
    client.app.state.db.exec("UPDATE runs SET status = 'deciding' WHERE run_id = %s", run_id)
    gen = pipe.resume(run_id, {"decision": "approve", "by": "planner"})
    for ev in gen:                                   # stop right after the approval is recorded
        if ev["phase"] == "approval" and ev["status"] == "done":
            break
    gen.close()                                      # what a dropped client connection does
    view = client.get(f"/api/runs/{run_id}", headers=planner).json()
    assert view["status"] == "interrupted"
    r = client.post(f"/api/runs/{run_id}/decision/stream", headers=planner, json={"decision": "reject"})
    assert r.status_code == 409
    evs = events(client.post(f"/api/runs/{run_id}/continue/stream", headers=planner))
    assert evs[-1]["status"] == "created"
    assert len(client.get("/api/work-orders", headers=planner).json()) == 1
