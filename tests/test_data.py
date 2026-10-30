"""Data-validation tests: the source files are loaded, checked and transformed correctly, and what the dashboard serves
matches the raw files (recomputed here from the CSV and categories.json, not from the app's own code)."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta

from conftest import auth, run

from app.config import ROOT
from app.db import Database

RAW = list(csv.DictReader(open(ROOT / "data" / "telemetry.csv", encoding="utf-8", newline="")))
CATS = json.loads((ROOT / "data" / "categories.json").read_text(encoding="utf-8"))
ASSETS = {a["asset_id"]: a for a in json.loads((ROOT / "data" / "assets.json").read_text(encoding="utf-8"))}
NOW = datetime.fromisoformat("2026-10-29T12:00:00+00:00")


def num(v):
    try:
        return float(v)
    except ValueError:
        return None


def expected_severity(cat: str, metric: str, value: float) -> str:
    rule = CATS[cat]["metrics"].get(metric)
    if not rule:
        return "normal"
    hi = rule["direction"] == "high"
    if (value > rule["critical"]) if hi else (value < rule["critical"]):
        return "critical"
    if (value > rule["warning"]) if hi else (value < rule["warning"]):
        return "warning"
    return "normal"


def test_every_raw_row_is_loaded_or_rejected(client):
    db = client.app.state.db
    loaded = db.row("SELECT count(*) AS n FROM telemetry")["n"]
    rejected = db.rows("SELECT source, row_data, reason FROM data_rejections")
    assert loaded + len([r for r in rejected if r["source"] == "telemetry.csv"]) == len(RAW)
    assert [(r["row_data"]["asset_id"], r["row_data"]["value"]) for r in rejected] == [("G-601", "n/a")]
    assert "valid number" in rejected[0]["reason"]


def test_reference_data_is_consistent(client):
    db = client.app.state.db
    assert {r["category"] for r in db.rows("SELECT category FROM assets")} <= set(CATS)
    history_parts = {p for r in db.rows("SELECT parts FROM maintenance_history") for p in r["parts"]}
    assert history_parts <= {r["part_no"] for r in db.rows("SELECT part_no FROM spare_parts")}
    for cat in CATS.values():
        for rule in cat["metrics"].values():
            assert (rule["critical"] > rule["warning"]) if rule["direction"] == "high" else (rule["critical"] < rule["warning"])


def test_bad_rows_in_other_sources_are_rejected(tmp_path, settings):
    for name in ("assets.json", "maintenance_history.json", "telemetry.csv", "categories.json"):
        (tmp_path / name).write_text((ROOT / "data" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "spare_parts.json").write_text('[{"part_no": "X-1", "description": "bad", "fits_types": [], '
                                               '"on_hand": -3, "location": "A", "lead_time_days": 1}]', encoding="utf-8")
    db = Database(settings.database_url)
    try:
        db.init_schema(); db.reset()
        counts = db.seed(tmp_path)
        assert counts["parts"] == 0
        assert db.row("SELECT count(*) AS n FROM data_rejections WHERE source = 'spare_parts.json'")["n"] == 1
    finally:
        db.reset(); db.close()


def test_dashboard_matches_the_source_rows(client):
    shown = {a["asset_id"]: a for a in client.get("/api/dashboard", headers=auth(client, "viewer")).json()["assets"]}
    assert set(shown) == set(ASSETS)
    for asset_id, a in shown.items():
        rows = [r for r in RAW if r["asset_id"] == asset_id and num(r["value"]) is not None]
        latest = {}
        for r in sorted(rows, key=lambda r: r["timestamp"]):
            latest[r["metric"]] = r
        cat = ASSETS[asset_id]["category"]
        sev = {m: expected_severity(cat, m, float(r["value"])) for m, r in latest.items()}
        worst = "critical" if "critical" in sev.values() else "warning" if "warning" in sev.values() else "normal"
        last = max(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) for r in rows)
        assert a["severity"] == worst, asset_id
        assert {f["metric"] for f in a["findings"]} == {m for m, s in sev.items() if s != "normal"}, asset_id
        assert a["stale"] == (NOW - last > timedelta(hours=CATS[cat]["max_data_age_h"])), asset_id
    assert shown["G-601"]["rejected"] == 1


def test_draft_evidence_is_shown_exactly_as_recorded(client):
    _, evs = run(client)
    draft = next(e for e in evs if e["phase"] == "approval")["data"]["draft"]
    raw = {(r["metric"], float(r["value"]), r["timestamp"]) for r in RAW if r["asset_id"] == "P-101"}
    for ev in draft["evidence"]:
        assert (ev["metric"], ev["value"], ev["timestamp"].replace("+00:00", "Z")) in raw


def test_generated_work_order_quotes_the_source_values(client):
    from conftest import decide
    run_id, _ = run(client)
    decide(client, run_id)
    number = client.get("/api/work-orders", headers=auth(client, "viewer")).json()[0]["work_order"]
    doc = client.get(f"/api/work-orders/{number}", headers=auth(client, "viewer")).json()["document_md"]
    assert "| vibration_rms | 8.4 mm/s | 2026-10-29 10:00 UTC |" in doc
