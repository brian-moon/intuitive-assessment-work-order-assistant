"""Loads and validates the source data (fixtures standing in for a historian, a CMMS history and stores).

Bad rows are not passed on silently: each one is kept in ``quarantine`` with the reason, so the API and the UI
can show "1 reading rejected" instead of a wrong number.
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from .models import Asset, HistoryEntry, Reading, SparePart

log = logging.getLogger("app.data")


@dataclass
class Quarantined:
    source: str
    row: dict
    reason: str


@dataclass
class Repository:
    assets: dict[str, Asset]
    readings: list[Reading]
    history: list[HistoryEntry]
    parts: dict[str, SparePart]
    quarantine: list[Quarantined] = field(default_factory=list)

    # --- loading ------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, data_dir: Path) -> "Repository":
        quarantine: list[Quarantined] = []

        def parse(model, rows, source):
            good = []
            for row in rows:
                try:
                    good.append(model.model_validate(row))
                except ValidationError as e:
                    reason = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
                    quarantine.append(Quarantined(source, row, reason))
                    log.warning("quarantined %s row %s: %s", source, row, reason)
            return good

        assets = parse(Asset, json.loads((data_dir / "assets.json").read_text(encoding="utf-8")), "assets.json")
        with open(data_dir / "telemetry.csv", encoding="utf-8", newline="") as f:
            readings = parse(Reading, list(csv.DictReader(f)), "telemetry.csv")
        history = parse(HistoryEntry, json.loads((data_dir / "maintenance_history.json").read_text(encoding="utf-8")),
                        "maintenance_history.json")
        parts = parse(SparePart, json.loads((data_dir / "spare_parts.json").read_text(encoding="utf-8")), "spare_parts.json")
        return cls({a.asset_id: a for a in assets}, readings, history, {p.part_no: p for p in parts}, quarantine)

    # --- queries ------------------------------------------------------------------------------------------
    def readings_for(self, asset_id: str) -> list[Reading]:
        return sorted((r for r in self.readings if r.asset_id == asset_id), key=lambda r: (r.metric, r.timestamp))

    def latest_by_metric(self, asset_id: str) -> dict[str, Reading]:
        latest: dict[str, Reading] = {}
        for r in self.readings_for(asset_id):
            latest[r.metric] = r  # sorted by time, so the last one wins
        return latest

    def last_reading_at(self, asset_id: str) -> datetime | None:
        rs = self.readings_for(asset_id)
        return max((r.timestamp for r in rs), default=None)

    def is_stale(self, asset_id: str, now: datetime, hours: float) -> bool:
        last = self.last_reading_at(asset_id)
        return last is None or now - last > timedelta(hours=hours)

    def quarantined_for(self, asset_id: str) -> list[Quarantined]:
        return [q for q in self.quarantine if q.row.get("asset_id") == asset_id]

    def asset_summary(self, asset_id: str, now: datetime, stale_hours: float) -> dict:
        """The row the UI shows for one asset: derived only from validated data."""
        a = self.assets[asset_id]
        latest = self.latest_by_metric(asset_id)
        alarms = sorted(m for m, r in latest.items() if r.over_limit)
        last = self.last_reading_at(asset_id)
        return {
            "asset_id": a.asset_id,
            "name": a.name,
            "site": a.site,
            "criticality": a.criticality,
            "alarms": alarms,
            "status": "alarm" if alarms else "normal",
            "last_reading_at": last.isoformat() if last else None,
            "stale": self.is_stale(asset_id, now, stale_hours),
            "rejected_readings": len(self.quarantined_for(asset_id)),
        }
