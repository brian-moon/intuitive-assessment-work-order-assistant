"""The agent's tools: four reads and one proposal. No tool writes anything; the write is a separate,
approval-gated step (see agent.py).

Token economy: tools return compact summaries (latest value and trend per metric, the last three work orders,
only parts that fit the asset type), never raw tables. Errors come back as short ``is_error`` results the model
can act on, never as stack traces.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from .data_access import Repository
from .faults import Faults
from .models import SparePart, WorkOrderDraft

ASSET_ID = {"type": "string", "description": "Asset id such as P-101", "pattern": "^[A-Z]-[0-9]{3}$"}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOL_SPECS: list[dict] = [
    {"name": "get_asset", "description": "Asset master data: name, type, site, criticality and energy sources.",
     "input_schema": _obj({"asset_id": ASSET_ID}, ["asset_id"])},
    {"name": "get_telemetry",
     "description": "Latest reading, alarm limit and short trend per metric, whether the data is stale, and how many "
                    "readings were rejected as malformed.",
     "input_schema": _obj({"asset_id": ASSET_ID}, ["asset_id"])},
    {"name": "get_maintenance_history", "description": "The last three work orders for the asset, newest first.",
     "input_schema": _obj({"asset_id": ASSET_ID}, ["asset_id"])},
    {"name": "check_spare_parts", "description": "Spare parts that fit this asset type, with stock on hand and lead time.",
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


class Toolbox:
    def __init__(self, repo: Repository, faults: Faults, now: datetime, stale_hours: float):
        self.repo, self.faults, self.now, self.stale_hours = repo, faults, now, stale_hours

    def run(self, name: str, args: dict) -> ToolResult:
        try:
            if name == "propose_work_order":
                draft = WorkOrderDraft.model_validate(args)
                return ToolResult("Draft received; it goes to the planner for approval.", proposal=draft)
            if name not in READ_TOOLS:
                raise ToolFailure(f"Unknown tool {name!r}.")
            asset_id = args.get("asset_id")
            if asset_id not in self.repo.assets:
                raise ToolFailure(f"No asset with id {asset_id!r}.")
            data = getattr(self, name)(asset_id)
            return ToolResult(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str))
        except ValidationError as e:
            return ToolResult("Invalid input: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                                             for x in e.errors()[:5]), is_error=True)
        except ToolFailure as e:
            return ToolResult(str(e), is_error=True)

    # --- read tools -----------------------------------------------------------------------------------------
    def get_asset(self, asset_id: str) -> dict:
        return self.repo.assets[asset_id].model_dump()

    def get_telemetry(self, asset_id: str) -> dict:
        if self.faults.telemetry_unavailable:
            raise ToolFailure("Historian unavailable (connection refused). Telemetry cannot be read right now.")
        metrics = {}
        for metric, latest in self.repo.latest_by_metric(asset_id).items():
            series = [r for r in self.repo.readings_for(asset_id) if r.metric == metric]
            metrics[metric] = {
                "latest": latest.value, "unit": latest.unit, "timestamp": latest.timestamp.isoformat().replace("+00:00", "Z"),
                "alarm_limit": latest.alarm_limit, "over_limit": latest.over_limit,
                "trend": [r.value for r in series[-3:]],
            }
        last = self.repo.last_reading_at(asset_id)
        return {"asset_id": asset_id, "metrics": metrics,
                "last_reading_at": last.isoformat().replace("+00:00", "Z") if last else None,
                "stale": self.repo.is_stale(asset_id, self.now, self.stale_hours),
                "rejected_readings": len(self.repo.quarantined_for(asset_id))}

    def get_maintenance_history(self, asset_id: str) -> dict:
        rows = sorted((h for h in self.repo.history if h.asset_id == asset_id), key=lambda h: h.date, reverse=True)
        return {"asset_id": asset_id, "work_orders": [h.model_dump() for h in rows[:3]]}

    def check_spare_parts(self, asset_id: str) -> dict:
        asset = self.repo.assets[asset_id]
        payload = [p.model_dump() for p in self.repo.parts.values() if asset.type in p.fits_types]
        if self.faults.parts_invalid_response:
            payload = [{"part_no": p["part_no"], "on_hand": "unknown"} for p in payload]  # contract broken upstream
        try:
            parts = [SparePart.model_validate(p) for p in payload]
        except ValidationError as e:
            raise ToolFailure("Stores service returned an invalid response; stock levels are unknown.") from e
        return {"asset_id": asset_id, "parts": [
            {"part_no": p.part_no, "description": p.description, "on_hand": p.on_hand, "lead_time_days": p.lead_time_days}
            for p in parts]}
