"""The agent's tools: four reads from the database and one proposal. No tool writes anything; the write happens in a
separate, approval-gated phase (see pipeline.py).

Token economy: tools return compact summaries (latest value, its severity against the category thresholds and a short
trend per metric; the last three work orders; only parts that fit the category), never raw tables. Errors come back
as short ``is_error`` results the model can act on, never as stack traces.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from .anomaly import classify_asset
from .db import Database
from .faults import Faults
from .models import SparePart, WorkOrderDraft

ASSET_ID = {"type": "string", "description": "Asset id such as P-101", "pattern": "^[A-Z]-[0-9]{3}$"}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOL_SPECS: list[dict] = [
    {"name": "get_asset", "description": "Asset master data: name, category, site, criticality and energy sources.",
     "input_schema": _obj({"asset_id": ASSET_ID}, ["asset_id"])},
    {"name": "get_telemetry",
     "description": "Per metric: latest reading, its severity against the machine-category thresholds, the thresholds "
                    "and a short trend; whether the data is stale for this category; how many readings were rejected.",
     "input_schema": _obj({"asset_id": ASSET_ID}, ["asset_id"])},
    {"name": "get_maintenance_history", "description": "The last three work orders for the asset, newest first.",
     "input_schema": _obj({"asset_id": ASSET_ID}, ["asset_id"])},
    {"name": "check_spare_parts", "description": "Spare parts that fit this asset's category, with stock and lead time.",
     "input_schema": _obj({"asset_id": ASSET_ID}, ["asset_id"])},
    {"name": "propose_work_order",
     "description": "Submit the work-order draft for planner review. Call it exactly once, at the end. Evidence must "
                    "quote readings exactly as get_telemetry returned them; parts must come from check_spare_parts.",
     "strict": True,
     "input_schema": _obj({
         "asset_id": ASSET_ID,
         "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
         "problem_summary": {"type": "string"},
         "evidence": {"type": "array", "items": _obj({
             "metric": {"type": "string"}, "value": {"type": "number"}, "unit": {"type": "string"},
             "timestamp": {"type": "string"}}, ["metric", "value", "unit", "timestamp"])},
         "recommended_actions": {"type": "array", "items": {"type": "string"}},
         "parts": {"type": "array", "items": _obj({"part_no": {"type": "string"}, "qty": {"type": "integer"}},
                                                   ["part_no", "qty"])},
         "safety": _obj({"lockout_tagout_required": {"type": "boolean"},
                         "energy_sources": {"type": "array", "items": {"type": "string", "enum": [
                             "electrical", "hydraulic", "pneumatic", "mechanical", "thermal"]}},
                         "notes": {"type": "string"}},
                        ["lockout_tagout_required", "energy_sources", "notes"]),
         "needs_data_refresh": {"type": "boolean"},
     }, ["asset_id", "priority", "problem_summary", "evidence", "recommended_actions", "parts", "safety",
         "needs_data_refresh"])},
]

READ_TOOLS = {"get_asset", "get_telemetry", "get_maintenance_history", "check_spare_parts"}


class ToolFailure(Exception):
    """A tool could not produce a valid result. The message is what the model sees."""


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    proposal: WorkOrderDraft | None = None


def _z(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


class Toolbox:
    def __init__(self, db: Database, faults: Faults, now):
        """``now``: a datetime (fixed) or a zero-argument callable (the live clock)."""
        self.db, self.faults = db, faults
        self._clock = now if callable(now) else (lambda: now)

    @property
    def now(self) -> datetime:
        return self._clock()

    def run(self, name: str, args: dict) -> ToolResult:
        try:
            if name == "propose_work_order":
                draft = WorkOrderDraft.model_validate(args)
                return ToolResult("Draft received; it goes to reconciliation and then to the planner.", proposal=draft)
            if name not in READ_TOOLS:
                raise ToolFailure(f"Unknown tool {name!r}.")
            asset_id = args.get("asset_id")
            if not isinstance(asset_id, str) or self.db.asset(asset_id) is None:
                raise ToolFailure(f"No asset with id {asset_id!r}.")
            data = getattr(self, name)(asset_id)
            return ToolResult(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str))
        except ValidationError as e:
            return ToolResult("Invalid input: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                                             for x in e.errors()[:5]), is_error=True)
        except ToolFailure as e:
            return ToolResult(str(e), is_error=True)

    # --- read tools -----------------------------------------------------------------------------------------------
    def get_asset(self, asset_id: str) -> dict:
        return self.db.asset(asset_id).model_dump()

    def get_telemetry(self, asset_id: str) -> dict:
        if self.faults.telemetry_unavailable:
            raise ToolFailure("Historian unavailable (connection refused). Telemetry cannot be read right now.")
        metrics = {}
        for f in classify_asset(self.db, asset_id, self.now):
            metrics[f["metric"]] = {"latest": f["value"], "unit": f["unit"], "timestamp": _z(f["ts"]),
                                    "severity": f["severity"], "warning": f["warning"], "critical": f["critical"],
                                    "direction": f["direction"], "trend": self.db.trend(asset_id, f["metric"])}
        last = self.db.last_reading_at(asset_id)
        return {"asset_id": asset_id, "metrics": metrics, "last_reading_at": _z(last) if last else None,
                "stale": self.db.is_stale(asset_id, self.now), "rejected_readings": self.db.rejections_for(asset_id)}

    def get_maintenance_history(self, asset_id: str) -> dict:
        return {"asset_id": asset_id, "work_orders": self.db.history(asset_id)}

    def check_spare_parts(self, asset_id: str) -> dict:
        payload = self.db.parts_for(self.db.asset(asset_id).category)
        if self.faults.parts_invalid_response:
            payload = [{"part_no": p["part_no"], "on_hand": "unknown"} for p in payload]  # contract broken upstream
        try:
            parts = [SparePart.model_validate({"location": "-", **p}) for p in payload]
        except ValidationError as e:
            raise ToolFailure("Stores service returned an invalid response; stock levels are unknown.") from e
        return {"asset_id": asset_id, "parts": [
            {"part_no": p.part_no, "description": p.description, "on_hand": p.on_hand, "lead_time_days": p.lead_time_days}
            for p in parts]}
