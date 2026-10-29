"""Switchable failures, used by tests, the evaluation and the demo (``POST /api/faults``).

Each flag makes one dependency misbehave the way it does in a plant: the historian is down, the stores service
returns a payload that does not match its contract, or the CMMS hangs past its timeout.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Faults:
    telemetry_unavailable: bool = False
    parts_invalid_response: bool = False
    cmms_timeout: bool = False

    def as_dict(self) -> dict:
        return asdict(self)
