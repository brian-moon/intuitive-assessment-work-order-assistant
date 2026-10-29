"""Switchable failures, used by tests, the evaluation and the demo (``PUT /api/faults``).

Each flag makes one dependency misbehave the way it does in a plant: the historian is down, the stores service
returns a payload that breaks its contract, the CMMS hangs past its timeout, or the mail server is unreachable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Faults:
    telemetry_unavailable: bool = False
    parts_invalid_response: bool = False
    cmms_timeout: bool = False
    smtp_unavailable: bool = False

    def as_dict(self) -> dict:
        return asdict(self)
