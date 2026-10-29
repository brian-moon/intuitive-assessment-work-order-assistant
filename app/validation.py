"""Deterministic checks on the model's draft, run before a planner sees it.

The model is asked to ground everything; this module verifies that it did. A blocking issue disables "approve"
(the planner can still reject), so an ungrounded or unsafe draft can never reach the CMMS.
"""
from __future__ import annotations

from datetime import datetime

from .data_access import Repository
from .models import WorkOrderDraft

EXPECTED_PRIORITY = {"high": "P1", "medium": "P2", "low": "P3"}


def check_draft(draft: WorkOrderDraft, repo: Repository, now: datetime, stale_hours: float) -> list[dict]:
    issues: list[dict] = []

    def add(severity: str, code: str, msg: str) -> None:
        issues.append({"severity": severity, "code": code, "message": msg})

    asset = repo.assets.get(draft.asset_id)
    if asset is None:
        add("blocking", "unknown_asset", f"Draft names an unknown asset {draft.asset_id!r}.")
        return issues

    readings = {(r.metric, r.value, r.timestamp) for r in repo.readings_for(asset.asset_id)}
    for ev in draft.evidence:
        if (ev.metric, ev.value, ev.timestamp) not in readings:
            add("blocking", "ungrounded_evidence",
                f"Evidence {ev.metric}={ev.value} at {ev.timestamp.isoformat()} does not match any recorded reading.")

    for line in draft.parts:
        part = repo.parts.get(line.part_no)
        if part is None or asset.type not in part.fits_types:
            add("blocking", "unknown_part", f"Part {line.part_no} is not a stocked part for a {asset.type}.")
        elif part.on_hand < line.qty:
            add("warning", "backorder", f"Part {line.part_no}: {part.on_hand} on hand, {line.qty} requested.")

    missing = set(asset.energy_sources) - set(draft.safety.energy_sources)
    if not draft.safety.lockout_tagout_required or missing:
        add("blocking", "unsafe_isolation",
            "Lockout/tagout must cover every energy source of the asset" + (f" (missing: {', '.join(sorted(missing))})."
                                                                          if missing else "."))

    stale = repo.is_stale(asset.asset_id, now, stale_hours)
    rejected = bool(repo.quarantined_for(asset.asset_id))
    if (stale or rejected) and not draft.needs_data_refresh:
        add("blocking", "data_quality_unflagged", "Telemetry is stale or partly rejected, but the draft does not say so.")

    over = any(r.over_limit for r in repo.latest_by_metric(asset.asset_id).values())
    expected = EXPECTED_PRIORITY[asset.criticality] if over else None
    if expected and draft.priority != expected:
        add("warning", "priority_mismatch", f"Priority {draft.priority}; the rule gives {expected} for this asset.")
    return issues


def has_blocking(issues: list[dict]) -> bool:
    return any(i["severity"] == "blocking" for i in issues)
