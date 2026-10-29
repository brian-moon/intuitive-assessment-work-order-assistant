"""Mock CMMS (the plant's system of record). The generated work order is exported to it after approval; a timeout
leaves the work order stored locally as ``pending_export`` and the run waiting, so the planner can retry."""
from __future__ import annotations

import time

from .faults import Faults


class CMMSTimeout(Exception):
    pass


class MockCMMS:
    def __init__(self, faults: Faults, timeout_s: float):
        self.faults, self.timeout_s = faults, timeout_s
        self.exported: dict[str, dict] = {}

    def export(self, work_order: str, payload: dict) -> str:
        if self.faults.cmms_timeout:
            time.sleep(min(self.timeout_s, 0.05))  # keep tests fast; the point is the timeout path
            raise CMMSTimeout(f"CMMS did not answer within {self.timeout_s:.0f} s")
        self.exported[work_order] = payload
        return f"CMMS-{len(self.exported):05d}"
