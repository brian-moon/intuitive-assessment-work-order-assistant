"""Live telemetry simulator: new readings at an adjustable rate, so the dashboard has something live to watch.

Each event picks a random asset and one of its category's metrics, draws a status from the mix (normal / warning /
critical), and places the value inside that status's band of the category thresholds. The reading is stored like any
other (``source = 'simulator'``), the anomaly scan runs, and a critical reading raises the dashboard alert and the
e-mails exactly as real telemetry would.

The switch, rate (per minute or per hour) and mix live in the ``simulator`` table, so they survive restarts and every
browser sees the same state. A background thread does the work; events are spaced at random (a Poisson process) at
the requested average rate. It starts enabled at 6 events per minute (``SIM_AUTOSTART=0`` starts it off).
"""
from __future__ import annotations

import logging
import math
import random
import threading
from collections.abc import Callable
from datetime import datetime

from psycopg.types.json import Jsonb

from . import anomaly
from .db import Database
from .models import MetricRule
from .notify import Notifier

log = logging.getLogger("app.simulator")
DEFAULT_MIX = {"normal": 0.75, "warning": 0.18, "critical": 0.07}
MAX_PER_MINUTE = 120


def value_for(rule: MetricRule, status: str, rng: random.Random) -> float:
    """A value that the rule classifies as ``status``."""
    w, c = rule.warning, rule.critical
    span = abs(c - w) or abs(w) * 0.2 or 1.0
    if rule.direction == "high":
        lo, hi = {"normal": (w - span, w - span * 0.05), "warning": (w + span * 0.05, c - span * 0.05),
                  "critical": (c + span * 0.05, c + span * 0.8)}[status]
    else:
        lo, hi = {"normal": (w + span * 0.05, w + span), "warning": (c + span * 0.05, w - span * 0.05),
                  "critical": (c - span * 0.8, c - span * 0.05)}[status]
    return round(rng.uniform(lo, hi), 2)


class Simulator:
    def __init__(self, db: Database, notifier: Notifier, clock: Callable[[], datetime], *, autostart: bool = False,
                 rate: float = 6, unit: str = "minute", rng: random.Random | None = None, start_thread: bool = True):
        self.db, self.notifier, self.clock = db, notifier, clock
        self.rng = rng or random.Random()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.db.exec("INSERT INTO simulator (id, enabled, rate, unit, mix) VALUES (1, %s, %s, %s, %s) "
                     "ON CONFLICT (id) DO NOTHING", autostart, rate, unit, Jsonb(DEFAULT_MIX))
        self._thread = None
        if start_thread:
            self._thread = threading.Thread(target=self._loop, name="telemetry-simulator", daemon=True)
            self._thread.start()

    # --- state ----------------------------------------------------------------------------------------------------
    def state(self) -> dict:
        s = self.db.row("SELECT enabled, rate, unit, mix, updated_by, updated_at FROM simulator WHERE id = 1")
        stats = self.db.row("SELECT count(*) FILTER (WHERE ts > now() - interval '1 hour') AS last_hour, max(ts) AS last_at "
                            "FROM telemetry WHERE source = 'simulator'")
        return {**s, "rate": float(s["rate"]), "events_last_hour": stats["last_hour"], "last_event_at": stats["last_at"],
                "max_per_minute": MAX_PER_MINUTE}

    def update(self, user: str, enabled: bool | None = None, rate: float | None = None, unit: str | None = None,
               mix: dict | None = None) -> dict:
        cur = self.state()
        unit = unit or cur["unit"]
        rate = cur["rate"] if rate is None else rate
        if unit not in ("minute", "hour") or rate <= 0 or (rate if unit == "minute" else rate / 60) > MAX_PER_MINUTE:
            raise ValueError(f"rate must be above 0 and at most {MAX_PER_MINUTE} per minute")
        if mix is not None:
            if (set(mix) != set(DEFAULT_MIX) or not all(math.isfinite(v) and 0 <= v <= 1000 for v in mix.values())
                    or sum(mix.values()) <= 0):
                raise ValueError("mix needs normal, warning and critical weights between 0 and 1000, not all zero")
            total = sum(mix.values())
            mix = {k: round(v / total, 4) for k, v in mix.items()}
        self.db.exec("UPDATE simulator SET enabled = %s, rate = %s, unit = %s, mix = %s, updated_by = %s, "
                     "updated_at = now() WHERE id = 1", cur["enabled"] if enabled is None else enabled, rate, unit,
                     Jsonb(mix or cur["mix"]), user)
        self._wake.set()
        log.info("simulator updated by %s: %s", user, self.state())
        return self.state()

    # --- events ---------------------------------------------------------------------------------------------------
    def emit_one(self) -> dict:
        """Generate one reading, store it, scan, alert. Returns the reading."""
        mix = self.state()["mix"]
        asset_id = self.rng.choice(self.db.asset_ids())
        asset = self.db.asset(asset_id)
        cat = self.db.category(asset.category)
        metric = self.rng.choice(sorted(cat.metrics))
        rule = cat.metrics[metric]
        status = self.rng.choices(list(mix), weights=list(mix.values()))[0]
        value = value_for(rule, status, self.rng)
        ts = self.clock()
        self.db.exec("INSERT INTO telemetry (asset_id, ts, metric, value, unit, source) VALUES (%s,%s,%s,%s,%s,'simulator') "
                     "ON CONFLICT (asset_id, ts, metric) DO NOTHING", asset_id, ts, metric, value, rule.unit)
        findings = anomaly.scan(self.db, ts)
        self.notifier.alert([f for f in findings if f["asset_id"] == asset_id and f["metric"] == metric])
        return {"asset_id": asset_id, "metric": metric, "value": value, "unit": rule.unit, "status": status,
                "severity": rule.severity(value), "ts": ts}

    def _interval(self, st: dict) -> float:
        per_s = st["rate"] / (60 if st["unit"] == "minute" else 3600)
        return min(self.rng.expovariate(per_s), 3 / per_s)       # random spacing, never more than 3x the mean

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                st = self.state()
                if not st["enabled"]:
                    self._wake.wait(timeout=5)
                    self._wake.clear()
                    continue
                if self._wake.wait(timeout=self._interval(st)):   # settings changed: re-read them first
                    self._wake.clear()
                    continue
                if not self._stop.is_set():
                    self.emit_one()
            except Exception:   # the simulator must never take the app down; log and keep going
                log.exception("simulator tick failed")
                self._stop.wait(5)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
