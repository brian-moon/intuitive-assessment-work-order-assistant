"""Reconciliation: the model's draft is checked against the source data with rules set per machine category.

The category table (``machine_categories``) supplies, for each kind of machine:

* the warning and critical thresholds per metric (which readings are anomalies, and how severe),
* the oldest acceptable data age (``max_data_age_h``): a spindle's 8 h is stricter than a gearbox's 48 h,
* the evidence tolerance: how far a quoted value may differ from the recorded one (0 = exact).

A blocking issue disables approval (the planner can still reject), so an ungrounded or unsafe draft can never become
a work order. Warnings are shown to the planner and do not block.
"""
from __future__ import annotations

from datetime import datetime

from .anomaly import RANK, classify_asset
from .db import Database
from .models import WorkOrderDraft

# Expected priority from the worst anomaly and the asset's criticality.
PRIORITY = {("critical", "high"): "P1", ("critical", "medium"): "P2", ("critical", "low"): "P3",
            ("warning", "high"): "P2", ("warning", "medium"): "P3", ("warning", "low"): "P4"}


def expected_priority(severity: str, criticality: str) -> str:
    return PRIORITY.get((severity, criticality), "P4")


# The rules, in the order they are shown. Each issue code below belongs to exactly one rule.
RULES = [
    {"id": "asset", "title": "Right asset", "codes": ["wrong_asset", "unknown_asset"], "severity": "blocking",
     "checks": "The draft is for the asset the planner asked about, and that asset exists."},
    {"id": "evidence", "title": "Evidence is grounded", "codes": ["ungrounded_evidence"], "severity": "blocking",
     "checks": "Every quoted reading matches a recorded reading: metric, timestamp and value within the category's "
               "tolerance."},
    {"id": "anomaly", "title": "The anomaly is addressed", "codes": ["anomaly_not_addressed"], "severity": "blocking",
     "checks": "The evidence cites at least one metric that is over its warning or critical threshold."},
    {"id": "parts", "title": "Parts exist and fit", "codes": ["unknown_part"], "severity": "blocking",
     "checks": "Every part is a stocked part listed for this machine category."},
    {"id": "isolation", "title": "Isolation is complete", "codes": ["unsafe_isolation"], "severity": "blocking",
     "checks": "Lockout/tagout is required and covers every energy source of the asset."},
    {"id": "data", "title": "Data quality is flagged", "codes": ["data_quality_unflagged"], "severity": "blocking",
     "checks": "If the telemetry is older than the category's data-age limit, or some readings were rejected, the "
               "draft says the data must be confirmed."},
    {"id": "stock", "title": "Parts are in stock", "codes": ["backorder"], "severity": "warning",
     "checks": "The quantity asked for is on hand; otherwise the planner is warned about the lead time."},
    {"id": "priority", "title": "Priority follows the rule", "codes": ["priority_mismatch"], "severity": "warning",
     "checks": "Worst severity x asset criticality: critical on high = P1, on medium = P2, on low = P3; warning on high "
               "= P2, on medium = P3, on low = P4; nothing abnormal = P4."},
]


def rule_card(db: Database, asset_id: str, now: datetime) -> dict:
    """The rules with the parameters that apply to this asset (its machine category's limits)."""
    asset = db.asset(asset_id)
    cat = db.category(asset.category)
    worst = max((f["severity"] for f in classify_asset(db, asset_id, now)), key=RANK.get, default="normal")
    params = {
        "asset": asset.asset_id,
        "evidence": f"tolerance {cat.evidence_tolerance:g} ({cat.label.lower()})",
        "anomaly": ", ".join(f"{m} warn {r.warning:g} / crit {r.critical:g} {r.unit}" for m, r in sorted(cat.metrics.items())),
        "parts": f"fits {cat.label.lower()}",
        "isolation": ", ".join(asset.energy_sources),
        "data": f"max age {cat.max_data_age_h:g} h",
        "stock": "on hand vs requested",
        "priority": f"{worst} on {asset.criticality} criticality = {expected_priority(worst, asset.criticality)}",
    }
    return {"asset_id": asset_id, "category": cat.label, "rules": [{**r, "parameter": params[r["id"]]} for r in RULES]}


def reconcile(draft: WorkOrderDraft, db: Database, now: datetime, requested_asset: str) -> list[dict]:
    issues: list[dict] = []

    def add(severity: str, code: str, msg: str) -> None:
        issues.append({"severity": severity, "code": code, "message": msg})

    if draft.asset_id != requested_asset:
        add("blocking", "wrong_asset", f"Draft is for {draft.asset_id}, but the planner asked about {requested_asset}.")
        return issues
    asset = db.asset(draft.asset_id)
    if asset is None:
        add("blocking", "unknown_asset", f"Draft names an unknown asset {draft.asset_id!r}.")
        return issues
    cat = db.category(asset.category)

    for ev in draft.evidence:
        if not db.reading_exists(asset.asset_id, ev.metric, ev.value, ev.timestamp, cat.evidence_tolerance):
            add("blocking", "ungrounded_evidence",
                f"Evidence {ev.metric}={ev.value:g} at {ev.timestamp.isoformat()} does not match a recorded reading "
                f"(tolerance {cat.evidence_tolerance:g} for {cat.label}).")

    for line in draft.parts:
        part = db.part(line.part_no)
        if part is None or asset.category not in part["fits_types"]:
            add("blocking", "unknown_part", f"Part {line.part_no} is not a stocked part for a {cat.label.lower()}.")
        elif part["on_hand"] < line.qty:
            add("warning", "backorder", f"Part {line.part_no}: {part['on_hand']} on hand, {line.qty} requested.")

    missing = set(asset.energy_sources) - set(draft.safety.energy_sources)
    if not draft.safety.lockout_tagout_required or missing:
        add("blocking", "unsafe_isolation", "Lockout/tagout must cover every energy source of the asset"
            + (f" (missing: {', '.join(sorted(missing))})." if missing else "."))

    findings = classify_asset(db, asset.asset_id, now)
    stale = db.is_stale(asset.asset_id, now)
    if (stale or db.rejections_for(asset.asset_id)) and not draft.needs_data_refresh:
        add("blocking", "data_quality_unflagged",
            f"Telemetry is older than {cat.max_data_age_h:g} h for a {cat.label.lower()}, or partly rejected, "
            "but the draft does not say so.")

    anomalous = {f["metric"] for f in findings if f["severity"] != "normal"}
    if anomalous and not anomalous & {e.metric for e in draft.evidence}:
        add("blocking", "anomaly_not_addressed",
            f"The draft cites none of the anomalous metrics ({', '.join(sorted(anomalous))}).")

    worst = max((f["severity"] for f in findings), key=RANK.get, default="normal")
    if worst != "normal":
        exp = expected_priority(worst, asset.criticality)
        if draft.priority != exp:
            add("warning", "priority_mismatch",
                f"Priority {draft.priority}; a {worst} anomaly on a {asset.criticality}-criticality asset gives {exp}.")
    return issues


def has_blocking(issues: list[dict]) -> bool:
    return any(i["severity"] == "blocking" for i in issues)
