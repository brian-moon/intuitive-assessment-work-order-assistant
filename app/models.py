"""Domain models. Pydantic validates every record that crosses a boundary: fixture files on load, tool results, the
draft the model proposes, and API requests."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EnergySource = Literal["electrical", "hydraulic", "pneumatic", "mechanical", "thermal"]
Severity = Literal["normal", "warning", "critical"]


class MetricRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: str
    direction: Literal["high", "low"]
    warning: float
    critical: float

    def severity(self, value: float) -> Severity:
        if self.direction == "high":
            return "critical" if value > self.critical else "warning" if value > self.warning else "normal"
        return "critical" if value < self.critical else "warning" if value < self.warning else "normal"

    def threshold_for(self, severity: Severity) -> float | None:
        return {"critical": self.critical, "warning": self.warning}.get(severity)


class Category(BaseModel):
    """Per machine category: the anomaly thresholds and the reconciliation rules for drafts about that category."""
    model_config = ConfigDict(extra="forbid")
    category: str
    label: str
    max_data_age_h: float = Field(gt=0)
    evidence_tolerance: float = Field(ge=0)
    metrics: dict[str, MetricRule]


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(pattern=r"^[A-Z]-\d{3}$")
    name: str
    category: str
    site: str
    line: str
    criticality: Literal["low", "medium", "high"]
    energy_sources: list[EnergySource]


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    timestamp: datetime
    metric: str
    value: float
    unit: str


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    date: str
    work_order: str
    summary: str
    parts: list[str]


class SparePart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_no: str
    description: str
    fits_types: list[str]
    on_hand: int = Field(ge=0)
    location: str
    lead_time_days: int = Field(ge=0)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    value: float
    unit: str
    timestamp: datetime


class PartLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_no: str
    qty: int = Field(ge=1, le=20)


class Safety(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lockout_tagout_required: bool
    energy_sources: list[EnergySource]
    notes: str


class WorkOrderDraft(BaseModel):
    """What the agent proposes. Nothing is generated until a planner approves it."""
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    priority: Literal["P1", "P2", "P3", "P4"]
    problem_summary: str = Field(min_length=10, max_length=600)
    evidence: list[Evidence] = Field(min_length=1)
    recommended_actions: list[str] = Field(min_length=1, max_length=8)
    parts: list[PartLine] = Field(default_factory=list)
    safety: Safety
    needs_data_refresh: bool = False


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(pattern=r"^[A-Z]-\d{3}$")
    request: str = Field(min_length=3, max_length=500)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=300)
    edited_priority: Literal["P1", "P2", "P3", "P4"] | None = None


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=254)
    min_severity: Literal["warning", "critical"] = "critical"

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL.match(v):
            raise ValueError("not a valid e-mail address")
        return v.lower()
