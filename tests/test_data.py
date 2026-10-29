"""Data-validation tests: the source data is loaded, checked and transformed correctly, and what the API serves to
the UI matches the source rows (recomputed here from the raw files, not from the app's own code)."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta

from conftest import VIEWER

from app.config import ROOT
from app.data_access import Repository

RAW = list(csv.DictReader(open(ROOT / "data" / "telemetry.csv", encoding="utf-8", newline="")))
NOW = datetime.fromisoformat("2026-10-29T12:00:00+00:00")


def _num(v):
    try:
        return float(v)
    except ValueError:
        return None


def test_every_raw_row_is_either_loaded_or_quarantined(repo):
    assert len(repo.readings) + len([q for q in repo.quarantine if q.source == "telemetry.csv"]) == len(RAW)


def test_only_the_malformed_reading_is_quarantined(repo):
    bad = [q for q in repo.quarantine if q.source == "telemetry.csv"]
    assert [(q.row["asset_id"], q.row["metric"], q.row["value"]) for q in bad] == [("G-601", "oil_temp", "n/a")]
    assert "valid number" in bad[0].reason


def test_reference_data_is_consistent(repo):
    assert set(repo.assets) == {r["asset_id"] for r in RAW}                       # every telemetry row has an asset
    assert all(h.asset_id in repo.assets for h in repo.history)
    assert {p for h in repo.history for p in h.parts} <= set(repo.parts)           # history parts exist in stores
    assert all(p.on_hand >= 0 and p.lead_time_days >= 0 for p in repo.parts.values())


def test_bad_rows_in_other_sources_are_quarantined_too(tmp_path):
    for name in ("assets.json", "maintenance_history.json", "spare_parts.json", "telemetry.csv"):
        (tmp_path / name).write_text((ROOT / "data" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "spare_parts.json").write_text('[{"part_no": "X-1", "description": "bad", "fits_types": [], '
                                               '"on_hand": -3, "location": "A", "lead_time_days": 1}]', encoding="utf-8")
    repo = Repository.load(tmp_path)
    assert repo.parts == {} and [q.source for q in repo.quarantine].count("spare_parts.json") == 1


def test_asset_list_shown_in_the_ui_matches_the_source_rows(client):
    body = client.get("/api/assets", headers=VIEWER).json()
    shown = {a["asset_id"]: a for a in body["assets"]}
    for asset_id in shown:
        rows = [r for r in RAW if r["asset_id"] == asset_id and _num(r["value"]) is not None]
        latest = {}
        for r in sorted(rows, key=lambda r: r["timestamp"]):
            latest[r["metric"]] = r
        expected_alarms = sorted(m for m, r in latest.items() if float(r["value"]) > float(r["alarm_limit"]))
        last = max(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) for r in rows)
        assert shown[asset_id]["alarms"] == expected_alarms, asset_id
        assert shown[asset_id]["status"] == ("alarm" if expected_alarms else "normal")
        assert shown[asset_id]["stale"] == (NOW - last > timedelta(hours=24)), asset_id
    assert shown["G-601"]["rejected_readings"] == 1
    assert body["rejected_rows"] == [{"source": "telemetry.csv", "asset_id": "G-601",
                                      "reason": body["rejected_rows"][0]["reason"]}]


def test_draft_evidence_is_shown_exactly_as_recorded(client):
    from conftest import start
    run = start(client).json()
    raw = {(r["metric"], float(r["value"]), r["timestamp"]) for r in RAW if r["asset_id"] == "P-101"}
    for ev in run["draft"]["evidence"]:
        assert (ev["metric"], ev["value"], ev["timestamp"].replace("+00:00", "Z")) in raw
