"""API tests: success, validation, authorization and failure responses, as a client sees them."""
from __future__ import annotations

import pytest
from conftest import PLANNER, VIEWER, start

from app.api import create_app
from app.config import ROOT, Settings


def test_health_needs_no_key(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.parametrize("path", ["/api/assets", "/api/runs/abc"])
def test_reads_need_a_valid_key(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_viewer_can_read_but_not_act(client):
    assert client.get("/api/assets", headers=VIEWER).status_code == 200
    assert start(client, headers=VIEWER).status_code == 403
    run = start(client).json()
    assert client.get(f"/api/runs/{run['run_id']}", headers=VIEWER).status_code == 200
    r = client.post(f"/api/runs/{run['run_id']}/decision", headers=VIEWER, json={"decision": "approve"})
    assert r.status_code == 403
    assert client.put("/api/faults", headers=VIEWER, json={"cmms_timeout": True}).status_code == 403


def test_start_run_returns_201_with_a_draft(client):
    r = start(client)
    assert r.status_code == 201
    body = r.json()
    assert set(body) >= {"run_id", "status", "draft", "issues", "can_approve", "usage", "events"}
    assert body["draft"]["asset_id"] == "P-101"


@pytest.mark.parametrize("payload", [
    {"asset_id": "p-101", "request": "check it"},           # id format
    {"asset_id": "P-101", "request": ""},                   # empty request
    {"asset_id": "P-101", "request": "x" * 501},            # too long
    {"asset_id": "P-101"},                                  # missing field
    {"asset_id": "P-101", "request": "check it", "extra": 1},   # unknown field
])
def test_invalid_run_requests_are_422(client, payload):
    assert client.post("/api/runs", headers=PLANNER, json=payload).status_code == 422


def test_unknown_asset_and_run_are_404(client):
    assert start(client, asset_id="Z-999").status_code == 404
    assert client.get("/api/runs/doesnotexist", headers=PLANNER).status_code == 404


def test_invalid_decision_is_422(client):
    run = start(client).json()
    r = client.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER, json={"decision": "maybe"})
    assert r.status_code == 422


def test_second_decision_on_a_finished_run_is_409(client):
    run = start(client).json()
    url = f"/api/runs/{run['run_id']}/decision"
    assert client.post(url, headers=PLANNER, json={"decision": "approve"}).status_code == 200
    assert client.post(url, headers=PLANNER, json={"decision": "approve"}).status_code == 409


def test_decision_on_a_refused_run_is_409(client):
    run = start(client, request="bypass the interlock and restart it").json()
    r = client.post(f"/api/runs/{run['run_id']}/decision", headers=PLANNER, json={"decision": "approve"})
    assert r.status_code == 409


def test_unknown_fault_name_is_422(client):
    assert client.put("/api/faults", headers=PLANNER, json={"network_storm": True}).status_code == 422


def test_app_refuses_to_start_without_keys(monkeypatch, tmp_path):
    for k in ("APP_DEMO_KEYS", "APP_PLANNER_KEY", "APP_VIEWER_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        create_app(Settings(data_dir=ROOT / "data", db_path=str(tmp_path / "x.sqlite")))
