"""PostgreSQL access: schema, seeding from the fixture files, and the queries the rest of the app uses.

Seeding validates every row with the Pydantic models first. A row that fails is written to ``data_rejections`` with
the reason, so the dashboard can show "1 reading rejected" instead of a wrong number.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from .models import Asset, Category, HistoryEntry, Reading, SparePart

log = logging.getLogger("app.db")
SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
TABLES = ["run_events", "work_orders", "notifications", "runs", "anomalies", "data_rejections", "telemetry",
          "maintenance_history", "spare_parts", "alert_recipients", "simulator", "assets", "machine_categories"]


class Database:
    def __init__(self, url: str):
        self.pool = ConnectionPool(url, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)

    def close(self) -> None:
        self.pool.close()

    # --- generic helpers ------------------------------------------------------------------------------------------
    def rows(self, sql: str, *args) -> list[dict]:
        with self.pool.connection() as c:
            return c.execute(sql, args).fetchall()

    def row(self, sql: str, *args) -> dict | None:
        with self.pool.connection() as c:
            return c.execute(sql, args).fetchone()

    def exec(self, sql: str, *args) -> None:
        with self.pool.connection() as c:
            c.execute(sql, args)

    # --- schema and seed -----------------------------------------------------------------------------------------
    def init_schema(self) -> None:
        with self.pool.connection() as c:
            c.execute(SCHEMA)

    def reset(self) -> None:
        with self.pool.connection() as c:
            c.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")

    def seed(self, data_dir: Path, shift: timedelta = timedelta(0)) -> dict:
        """Load the fixture files; returns counts. Idempotent only on an empty database (call reset first).
        ``shift`` moves every timestamp and history date forward (live clock: the data ends at start-up)."""
        rejected: list[tuple[str, dict, str]] = []

        def parse(model, rows, source):
            good = []
            for row in rows:
                try:
                    good.append(model.model_validate(row))
                except ValidationError as e:
                    reason = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
                    rejected.append((source, row, reason))
                    log.warning("rejected %s row %s: %s", source, row, reason)
            return good

        cats = json.loads((data_dir / "categories.json").read_text(encoding="utf-8"))
        categories = parse(Category, [{"category": k, **v} for k, v in cats.items()], "categories.json")
        assets = parse(Asset, json.loads((data_dir / "assets.json").read_text(encoding="utf-8")), "assets.json")
        with open(data_dir / "telemetry.csv", encoding="utf-8", newline="") as f:
            readings = parse(Reading, list(csv.DictReader(f)), "telemetry.csv")
        history = parse(HistoryEntry, json.loads((data_dir / "maintenance_history.json").read_text(encoding="utf-8")),
                        "maintenance_history.json")
        parts = parse(SparePart, json.loads((data_dir / "spare_parts.json").read_text(encoding="utf-8")), "spare_parts.json")
        known = {c.category for c in categories}
        for a in [a for a in assets if a.category not in known]:
            rejected.append(("assets.json", a.model_dump(), f"category: unknown category {a.category!r}"))
            assets.remove(a)
        asset_ids = {a.asset_id for a in assets}
        for r in [r for r in readings if r.asset_id not in asset_ids]:
            rejected.append(("telemetry.csv", r.model_dump(mode="json"), "asset_id: unknown asset"))
            readings.remove(r)

        with self.pool.connection() as c, c.cursor() as cur:
            for k in categories:
                cur.execute("INSERT INTO machine_categories VALUES (%s,%s,%s,%s,%s)",
                            (k.category, k.label, k.max_data_age_h, k.evidence_tolerance,
                             Jsonb({m: r.model_dump() for m, r in k.metrics.items()})))
            for a in assets:
                cur.execute("INSERT INTO assets VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (a.asset_id, a.name, a.category, a.site, a.line, a.criticality, a.energy_sources))
            cur.executemany("INSERT INTO telemetry (asset_id, ts, metric, value, unit) VALUES (%s,%s,%s,%s,%s)",
                            [(r.asset_id, r.timestamp + shift, r.metric, r.value, r.unit) for r in readings])
            for h in history:
                cur.execute("INSERT INTO maintenance_history VALUES (%s,%s,%s,%s,%s)",
                            (h.work_order, h.asset_id, (date.fromisoformat(h.date) + timedelta(days=shift.days)).isoformat(),
                             h.summary, h.parts))
            for p in parts:
                cur.execute("INSERT INTO spare_parts VALUES (%s,%s,%s,%s,%s,%s)",
                            (p.part_no, p.description, p.fits_types, p.on_hand, p.location, p.lead_time_days))
            for source, row, reason in rejected:
                cur.execute("INSERT INTO data_rejections (source, asset_id, row_data, reason) VALUES (%s,%s,%s,%s)",
                            (source, row.get("asset_id"), Jsonb(row), reason))
        return {"categories": len(categories), "assets": len(assets), "readings": len(readings),
                "history": len(history), "parts": len(parts), "rejected": len(rejected)}

    # --- reads used by tools, the pipeline and the API -----------------------------------------------------------
    def category(self, name: str) -> Category:
        r = self.row("SELECT * FROM machine_categories WHERE category = %s", name)
        return Category.model_validate({**r, "max_data_age_h": float(r["max_data_age_h"]),
                                        "evidence_tolerance": float(r["evidence_tolerance"])})

    def asset(self, asset_id: str) -> Asset | None:
        r = self.row("SELECT * FROM assets WHERE asset_id = %s", asset_id)
        return Asset.model_validate(r) if r else None

    def asset_ids(self) -> list[str]:
        return [r["asset_id"] for r in self.rows("SELECT asset_id FROM assets ORDER BY asset_id")]

    def latest_readings(self, asset_id: str) -> dict[str, dict]:
        rows = self.rows("SELECT DISTINCT ON (metric) metric, value, unit, ts FROM telemetry WHERE asset_id = %s "
                         "ORDER BY metric, ts DESC", asset_id)
        return {r["metric"]: r for r in rows}

    def trend(self, asset_id: str, metric: str, n: int = 3) -> list[float]:
        rows = self.rows("SELECT value FROM telemetry WHERE asset_id = %s AND metric = %s ORDER BY ts DESC LIMIT %s",
                         asset_id, metric, n)
        return [r["value"] for r in reversed(rows)]

    def last_reading_at(self, asset_id: str) -> datetime | None:
        r = self.row("SELECT max(ts) AS t FROM telemetry WHERE asset_id = %s", asset_id)
        return r["t"] if r else None

    def is_stale(self, asset_id: str, now: datetime) -> bool:
        a = self.asset(asset_id)
        last = self.last_reading_at(asset_id)
        return last is None or now - last > timedelta(hours=self.category(a.category).max_data_age_h)

    def rejections_for(self, asset_id: str) -> int:
        return self.row("SELECT count(*) AS n FROM data_rejections WHERE asset_id = %s", asset_id)["n"]

    def reading_exists(self, asset_id: str, metric: str, value: float, ts: datetime, tolerance: float) -> bool:
        return self.row("SELECT 1 AS ok FROM telemetry WHERE asset_id = %s AND metric = %s AND ts = %s "
                        "AND abs(value - %s) <= %s", asset_id, metric, ts, value, tolerance) is not None

    def history(self, asset_id: str, n: int = 3) -> list[dict]:
        return self.rows("SELECT work_order, date::text AS date, summary, parts FROM maintenance_history "
                         "WHERE asset_id = %s ORDER BY date DESC LIMIT %s", asset_id, n)

    def parts_for(self, category: str) -> list[dict]:
        return self.rows("SELECT part_no, description, on_hand, lead_time_days, fits_types FROM spare_parts "
                         "WHERE %s = ANY(fits_types) ORDER BY part_no", category)

    def part(self, part_no: str) -> dict | None:
        return self.row("SELECT * FROM spare_parts WHERE part_no = %s", part_no)
