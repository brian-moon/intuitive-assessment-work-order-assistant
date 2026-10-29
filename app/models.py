"""Domain models. Pydantic validates every record that crosses a boundary: fixture files, tool results, the
draft the model proposes, and API requests."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EnergySource = Literal["electrical", "hydraulic", "pneumatic", "mechanical", "thermal"]


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(pattern=r"^[A-Z]-\d{3}$")
    name: str
    type: str
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
    alarm_limit: float

    @property
    def over_limit(self) -> bool:
        return self.value > self.alarm_limit


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
    """What the agent proposes. Nothing is created until a planner approves it."""
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
