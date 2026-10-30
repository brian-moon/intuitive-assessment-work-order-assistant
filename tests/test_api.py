"""API tests: success, validation, authorization and failure responses, as a client sees them."""
from __future__ import annotations

import pytest
from conftest import auth, run

from app.api import create_app
from app.config import ROOT, Settings


def test_health_needs_no_sign_in(client):
    assert client.get("/health").json()["ok"] is True


def test_login(client):
    assert client.post("/api/login", json={"username": "planner", "password": "planner-pass-1"}).status_code == 200
    assert client.post("/api/login", json={"username": "planner", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"username": "ghost", "password": "x"}).status_code == 401
    assert client.post("/api/login", json={"username": "planner"}).status_code == 422


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/runs", "/api/work-orders"])
def test_reads_need_a_valid_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer forged.token.value"}).status_code == 401


def test_roles(client):
    viewer, planner, admin = auth(client, "viewer"), auth(client, "planner"), auth(client, "admin")
    body = {"asset_id": "P-101", "request": "check it"}
    assert client.get("/api/dashboard", headers=viewer).status_code == 200
    assert client.post("/api/runs/stream", headers=viewer, json=body).status_code == 403
    assert client.get("/api/admin/recipients", headers=planner).status_code == 403
    assert client.put("/api/admin/faults", headers=planner, json={"cmms_timeout": True}).status_code == 403
    assert client.get("/api/admin/recipients", headers=admin).status_code == 200
    run_id, _ = run(client)
    r = client.post(f"/api/runs/{run_id}/decision/stream", headers=viewer, json={"decision": "approve"})
    assert r.status_code == 403


@pytest.mark.parametrize("payload", [{"asset_id": "p-101", "request": "check it"}, {"asset_id": "P-101", "request": ""},
                                     {"asset_id": "P-101", "request": "x" * 501}, {"asset_id": "P-101"},
                                     {"asset_id": "P-101", "request": "check it", "extra": 1}])
def test_invalid_run_requests_are_422(client, payload):
    assert client.post("/api/runs/stream", headers=auth(client, "planner"), json=payload).status_code == 422


def test_not_found(client):
    planner = auth(client, "planner")
    assert client.post("/api/runs/stream", headers=planner, json={"asset_id": "Z-999", "request": "x y z"}).status_code == 404
    assert client.get("/api/runs/nope", headers=planner).status_code == 404
    assert client.get("/api/work-orders/WO-1", headers=planner).status_code == 404
    assert client.post("/api/alerts/999999/ack", headers=planner).status_code == 404


def test_decision_conflicts(client):
    planner = auth(client, "planner")
    run_id, _ = run(client)
    url = f"/api/runs/{run_id}/decision/stream"
    assert client.post(url, headers=planner, json={"decision": "maybe"}).status_code == 422
    assert client.post(url, headers=planner, json={"decision": "approve"}).status_code == 200
    assert client.post(url, headers=planner, json={"decision": "approve"}).status_code == 409
    refused, _ = run(client, request="bypass the interlock and restart it")
    assert client.post(f"/api/runs/{refused}/decision/stream", headers=planner, json={"decision": "approve"}).status_code == 409


def test_recipient_registry(client):
    admin = auth(client, "admin")
    r = client.post("/api/admin/recipients", headers=admin, json={"name": "Ops Lead", "email": " Ops@Plant.example "})
    assert r.status_code == 201 and r.json()["email"] == "ops@plant.example"
    dup = {"name": "Dup", "email": "ops@plant.example"}
    assert client.post("/api/admin/recipients", headers=admin, json=dup).status_code == 409
    for bad in ({"name": "X", "email": "not-an-email"}, {"name": "", "email": "a@b.org"},
                {"name": "X", "email": "a@b.org", "min_severity": "info"}):
        assert client.post("/api/admin/recipients", headers=admin, json=bad).status_code == 422
    rid = r.json()["id"]
    assert client.delete(f"/api/admin/recipients/{rid}", headers=admin).status_code == 200
    assert client.delete(f"/api/admin/recipients/{rid}", headers=admin).status_code == 404


def test_alert_acknowledgement_clears_the_banner(client):
    planner = auth(client, "planner")
    alerts = client.get("/api/dashboard", headers=planner).json()["alerts"]
    assert len(alerts) == 4
    assert client.post(f"/api/alerts/{alerts[0]['id']}/ack", headers=planner).status_code == 200
    assert len(client.get("/api/dashboard", headers=planner).json()["alerts"]) == 3
    assert client.post(f"/api/alerts/{alerts[0]['id']}/ack", headers=planner).status_code == 404


def test_scan_is_rate_limited(client):
    planner = auth(client, "planner")
    assert client.post("/api/scan", headers=planner).status_code == 200
    assert client.post("/api/scan", headers=planner).status_code == 429


def test_unknown_fault_is_422(client):
    assert client.put("/api/admin/faults", headers=auth(client, "admin"), json={"storm": True}).status_code == 422


def test_faults_can_be_read_and_switched_by_an_admin(client):
    admin = auth(client, "admin")
    assert client.get("/api/admin/faults", headers=admin).json()["cmms_timeout"] is False
    assert client.put("/api/admin/faults", headers=admin, json={"cmms_timeout": True}).json()["cmms_timeout"] is True
    assert client.get("/api/admin/faults", headers=auth(client, "planner")).status_code == 403


def test_app_refuses_to_start_without_users(monkeypatch):
    for k in ("APP_DEMO_USERS", "APP_USERS"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        create_app(Settings(data_dir=ROOT / "data"))


def test_governance_overview_shows_the_controls_at_work(make_client):
    from conftest import decide

    from app.llm import ScriptedLLM, Tier
    c = make_client(tiers=[Tier("local", ScriptedLLM("ungrounded", model="qwen-stand-in")),
                           Tier("frontier", ScriptedLLM(model="claude-stand-in"))])
    viewer = auth(c, "viewer")
    run_id, _ = run(c, "P-101")                                    # local blocked -> frontier accepted
    decide(c, run_id, edited_priority="P2")                        # planner corrects the priority
    run(c, request="bypass the interlock and restart it")          # refused by the guard
    g = c.get("/api/governance", headers=viewer).json()
    assert [t["tier"] for t in g["ladder"]] == ["local", "frontier"]
    assert g["ladder"][0]["threshold"] == 0.85 and g["ladder"][0]["by_priority"] == {"P1": 1.0}
    assert g["runs"]["by_route"] == {"frontier": 1, "refused": 1}
    assert g["escalations"] == {"local: ungrounded_evidence": 1}
    assert {"code": "ungrounded_evidence", "n": 1} in g["guardrails"]["blocking_issues"]
    assert g["guardrails"]["refused_requests"] == 1
    assert g["hitl"]["corrected"] == 1 and g["hitl"]["approved"] == 0
    assert set(g["cost"]["by_tier"]) == {"local", "frontier"}
    assert g["telemetry"]["enabled"] is False and g["telemetry"]["emitted"] >= 4
    assert g["recent"][1]["attempts"][0]["action"] == "escalate"
    assert c.get("/api/governance").status_code == 401


def test_health_reports_the_ladder(client):
    h = client.get("/health").json()
    assert h["ladder"] == [{"tier": "demo", "model": "scripted", "provider": "scripted"}] and h["demo_model"] is True


def test_branding_is_public_and_the_logo_is_optional(make_client, settings, tmp_path):
    from dataclasses import replace
    c = make_client()
    assert c.get("/api/branding").json() == {"name": "Work-order assistant", "subtitle": "Maintenance operations",
                                             "logo": None, "deployment": "local"}
    (tmp_path / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    c2 = make_client(s=replace(settings, brand_dir=tmp_path, brand_logo="../../logo.svg", brand_name="Plant X"))
    b = c2.get("/api/branding").json()
    assert b["logo"] == "/branding/logo.svg" and b["name"] == "Plant X"      # path parts are dropped
    assert c2.get("/branding/logo.svg").status_code == 200


def test_ack_all_and_config_view(client):
    planner, admin = auth(client, "planner"), auth(client, "admin")
    assert client.post("/api/alerts/ack-all", headers=planner).json() == {"acknowledged": 4}
    assert client.get("/api/dashboard", headers=planner).json()["alerts"] == []
    cfg = client.get("/api/admin/config", headers=admin).json()
    assert cfg["anthropic_api_key"] in ("set", "not set") and cfg["smtp"]["password"] in ("set", "not set")
    assert "SECRET" not in str(cfg) and client.get("/api/admin/config", headers=planner).status_code == 403


def test_test_alert_email_is_html_and_high_priority(client, mailbox):
    admin = auth(client, "admin")
    assert client.post("/api/admin/test-alert", headers=admin).status_code == 409          # nobody registered
    client.post("/api/admin/recipients", headers=admin, json={"name": "Ops", "email": "ops@x.org"})
    r = client.post("/api/admin/test-alert", headers=admin).json()["results"]
    assert r == [{"to": "ops@x.org", "status": "sent", "error": None}]
    m = mailbox[-1]
    assert m["severity"] == "critical" and "CRITICAL ALERT" in m["html"] and "[CRITICAL] TEST" in m["subject"]


def test_alert_emails_from_the_environment_are_registered(make_client, settings):
    from dataclasses import replace
    c = make_client(s=replace(settings, alert_emails="Ops Lead <Ops@Plant.example>, night@plant.example, not-an-email"))
    got = c.get("/api/admin/recipients", headers=auth(c, "admin")).json()
    assert [(r["name"], r["email"]) for r in got] == [("Ops Lead", "ops@plant.example"), ("night", "night@plant.example")]
