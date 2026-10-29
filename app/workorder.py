"""Work-order generator: turns an approved draft into a numbered work-order document, stored in PostgreSQL.

The document is deterministic (no model call): everything in it comes from the approved draft and the asset record,
so what the planner approved is exactly what the technicians receive.
"""
from __future__ import annotations

from datetime import datetime

from psycopg.types.json import Jsonb

from .db import Database
from .models import Asset, WorkOrderDraft


def next_number(db: Database, now: datetime) -> str:
    prefix = f"WO-{now:%Y%m%d}-"
    r = db.row("SELECT count(*) AS n FROM work_orders WHERE work_order LIKE %s", prefix + "%")
    return f"{prefix}{r['n'] + 1:03d}"


def render(number: str, draft: WorkOrderDraft, asset: Asset, approved_by: str, now: datetime) -> str:
    lines = [
        f"# Work order {number}",
        "",
        f"**Asset** {asset.asset_id} {asset.name} ({asset.site}, {asset.line})  ",
        f"**Priority** {draft.priority}  ",
        f"**Approved by** {approved_by} on {now:%Y-%m-%d %H:%M} UTC",
        "",
        "## Problem",
        draft.problem_summary,
        "",
        "## Evidence",
        "| Metric | Value | Recorded |",
        "|---|---|---|",
        *[f"| {e.metric} | {e.value:g} {e.unit} | {e.timestamp:%Y-%m-%d %H:%M} UTC |" for e in draft.evidence],
        "",
        "## Actions",
        *[f"{i}. {a}" for i, a in enumerate(draft.recommended_actions, 1)],
        "",
        "## Parts",
        *([f"- {p.part_no} × {p.qty}" for p in draft.parts] or ["- none"]),
        "",
        "## Safety",
        f"- Lockout/tagout: {'required' if draft.safety.lockout_tagout_required else 'not listed'} for "
        f"{', '.join(draft.safety.energy_sources)}.",
        f"- {draft.safety.notes}",
    ]
    if draft.needs_data_refresh:
        lines += ["", "> Telemetry was stale or partly rejected: confirm the readings on site before starting work."]
    return "\n".join(lines) + "\n"


def generate(db: Database, run_id: str, draft: WorkOrderDraft, approved_by: str, now: datetime) -> dict:
    asset = db.asset(draft.asset_id)
    number = next_number(db, now)
    doc = render(number, draft, asset, approved_by, now)
    title = f"{draft.priority} {asset.asset_id} {asset.name}: {draft.evidence[0].metric}"
    db.exec("INSERT INTO work_orders (work_order, run_id, asset_id, priority, title, body, document_md, approved_by, "
            "cmms_status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending_export')",
            number, run_id, asset.asset_id, draft.priority, title, Jsonb(draft.model_dump(mode="json")), doc, approved_by)
    return {"work_order": number, "title": title, "document_md": doc}
