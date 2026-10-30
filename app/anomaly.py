"""Anomaly detection: the latest reading of every metric, classified by its machine category's thresholds.

Deterministic on purpose. Whether a value crosses a limit is arithmetic, not a judgement for a model; the model's job
starts after that (drafting the work order).
"""
from __future__ import annotations

from datetime import datetime

from .db import Database

RANK = {"normal": 0, "warning": 1, "critical": 2}


def classify_asset(db: Database, asset_id: str, now: datetime) -> list[dict]:
    asset = db.asset(asset_id)
    cat = db.category(asset.category)
    stale = db.is_stale(asset_id, now)
    out = []
    for metric, r in sorted(db.latest_readings(asset_id).items()):
        rule = cat.metrics.get(metric)
        severity = rule.severity(r["value"]) if rule else "normal"
        out.append({"asset_id": asset_id, "metric": metric, "severity": severity, "value": r["value"], "unit": r["unit"],
                    "ts": r["ts"], "source": r.get("source", "seed"), "threshold": rule.threshold_for(severity) if rule else None,
                    "warning": rule.warning if rule else None, "critical": rule.critical if rule else None,
                    "direction": rule.direction if rule else None, "stale": stale})
    return out


def scan(db: Database, now: datetime) -> list[dict]:
    """Classify every asset and store the result. Returns every non-normal finding."""
    findings = []
    with db.pool.connection() as c:
        c.execute("DELETE FROM anomalies")
        for asset_id in db.asset_ids():
            for f in classify_asset(db, asset_id, now):
                c.execute("INSERT INTO anomalies (asset_id, metric, severity, value, unit, ts, threshold, stale) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                          (f["asset_id"], f["metric"], f["severity"], f["value"], f["unit"], f["ts"], f["threshold"],
                           f["stale"]))
                if f["severity"] != "normal":
                    findings.append(f)
    return findings


def worst(findings: list[dict]) -> str:
    return max((f["severity"] for f in findings), key=RANK.get, default="normal")
