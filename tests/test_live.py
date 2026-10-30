"""Live mode: the telemetry simulator, the live event stream, history windows and alert e-mail cooldown."""
from __future__ import annotations

import random
from datetime import timedelta

import pytest
from conftest import auth, events

from app.models import MetricRule
from app.simulator import Simulator, value_for

RULES = [MetricRule(unit="mm/s", direction="high", warning=4.5, critical=7.1),
         MetricRule(unit="bar", direction="low", warning=2.5, critical=2.0),
         MetricRule(unit="degC", direction="high", warning=80, critical=95)]


@pytest.mark.parametrize("rule", RULES, ids=["high", "low", "temp"])
@pytest.mark.parametrize("status", ["normal", "warning", "critical"])
def test_simulated_values_land_in_the_requested_band(rule, status):
    rng = random.Random(7)
    assert {rule.severity(value_for(rule, status, rng)) for _ in range(300)} == {status}


def sim_for(client, seed=1):
    app = client.app
    return Simulator(app.state.db, app.state.notifier, lambda: app.state.settings.now, rng=random.Random(seed),
                     start_thread=False)


def test_an_event_is_stored_scanned_and_alerted(client, mailbox):
    admin = auth(client, "admin")
    client.post("/api/admin/recipients", headers=admin, json={"name": "Ops", "email": "ops@x.org"})
    sim = sim_for(client)
    sim.update("t", mix={"normal": 0, "warning": 0, "critical": 1})
    ev = sim.emit_one()
    assert ev["severity"] == "critical" and ev["status"] == "critical"
    db = client.app.state.db
    assert db.row("SELECT count(*) AS n FROM telemetry WHERE source = 'simulator'")["n"] == 1
    sev = db.row("SELECT severity FROM anomalies WHERE asset_id = %s AND metric = %s", ev["asset_id"], ev["metric"])
    assert sev["severity"] == "critical"
    assert any(ev["asset_id"] in m["subject"] and m["severity"] == "critical" for m in mailbox)


def test_email_cooldown_limits_repeats_but_the_dashboard_records_every_alert(client, mailbox):
    admin = auth(client, "admin")
    client.post("/api/admin/recipients", headers=admin, json={"name": "Ops", "email": "ops@x.org"})
    db, n = client.app.state.db, client.app.state.notifier
    mailbox.clear()
    base = client.app.state.settings.now
    for i in range(3):   # three new critical readings of the same metric within the cooldown
        db.exec("INSERT INTO telemetry (asset_id, ts, metric, value, unit) VALUES ('P-101', %s, 'vibration_rms', %s, 'mm/s')",
                base + timedelta(minutes=10 + i), 9.0 + i)
        from app import anomaly
        n.alert([f for f in anomaly.scan(db, base + timedelta(minutes=10 + i)) if f["asset_id"] == "P-101"])
    assert len(mailbox) == 1
    shown = db.row("SELECT count(*) AS n FROM notifications WHERE channel = 'dashboard' AND asset_id = 'P-101'")["n"]
    assert shown == 4                                     # the seed alert + three new readings


def test_simulator_api(client):
    viewer, planner = auth(client, "viewer"), auth(client, "planner")
    st = client.get("/api/simulator", headers=viewer).json()
    assert st["enabled"] is False and st["unit"] == "minute"
    assert client.put("/api/simulator", headers=viewer, json={"enabled": True}).status_code == 403
    r = client.put("/api/simulator", headers=planner, json={"enabled": True, "rate": 30, "unit": "hour"}).json()
    assert r["enabled"] is True and r["rate"] == 30 and r["unit"] == "hour" and r["updated_by"] == "planner"
    for bad in ({"rate": 0}, {"rate": 500, "unit": "minute"}, {"unit": "day"}, {"mix": {"normal": 1}}, {"speed": 1}):
        assert client.put("/api/simulator", headers=planner, json=bad).status_code == 422, bad
    ev = client.post("/api/simulator/emit", headers=planner).json()
    assert ev["severity"] in ("normal", "warning", "critical")


def test_live_stream_sends_backlog_new_readings_alerts_and_heartbeat(client):
    planner = auth(client, "planner")
    client.post("/api/simulator/emit", headers=planner)
    evs = events(client.get("/api/live/stream?seconds=1", headers=planner))
    kinds = {e["type"] for e in evs}
    assert {"reading", "heartbeat"} <= kinds
    backlog = [e for e in evs if e["type"] == "reading" and e["backlog"]]
    assert len(backlog) == 20 and {"severity", "asset_id", "metric", "value", "ts", "source"} <= set(backlog[0])
    assert client.get("/api/live/stream?seconds=1").status_code == 401


def test_history_window(client):
    viewer = auth(client, "viewer")
    q = "/api/history?start=2026-10-28T00:00:00Z&end=2026-10-30T00:00:00Z"
    all_ = client.get(q, headers=viewer).json()
    p101 = next(s for s in all_["summary"] if s["asset_id"] == "P-101")
    assert p101["readings"] >= 3 and p101["critical"] >= 1 and "readings" not in all_
    one = client.get(q + "&asset_id=P-101", headers=viewer).json()
    assert one["thresholds"]["vibration_rms"]["critical"] == 7.1
    assert [r["value"] for r in one["readings"] if r["metric"] == "vibration_rms"] == [4.2, 6.3, 8.4]
    assert one["readings"][-1]["severity"] in ("normal", "warning", "critical")
    assert client.get(q.replace("2026-10-30", "2026-10-27"), headers=viewer).status_code == 422     # end before start
    assert client.get(q + "&asset_id=Z-999", headers=viewer).status_code == 404
    assert client.get("/api/history?start=2026-01-01T00:00:00Z&end=2026-10-30T00:00:00Z", headers=viewer).status_code == 422


def test_live_clock_shifts_the_seed_data_to_now(make_client, settings):
    from dataclasses import replace
    c = make_client(s=replace(settings, pinned_now=None))
    db, now = c.app.state.db, c.app.state.settings.now
    last = db.row("SELECT max(ts) AS t FROM telemetry")["t"]
    assert timedelta(0) <= now - last < timedelta(hours=3)
    assets = c.get("/api/dashboard", headers=auth(c, "viewer")).json()["assets"]
    crit = {a["asset_id"] for a in assets if a["severity"] == "critical"}
    assert {"P-101", "C-201", "M-301", "S-401"} <= crit
