"""Mock CMMS (the system of record for work orders). The only write in the whole workflow, and it runs only after a
planner has approved the draft."""
from __future__ import annotations

import itertools
import time

from .faults import Faults
from .models import WorkOrderDraft


class CMMSTimeout(Exception):
    pass


class MockCMMS:
    def __init__(self, faults: Faults, timeout_s: float):
        self.faults = faults
        self.timeout_s = timeout_s
        self.created: dict[str, dict] = {}
        self._ids = itertools.count(27001)

    def create_work_order(self, draft: WorkOrderDraft, approved_by: str) -> str:
        if self.faults.cmms_timeout:
            time.sleep(min(self.timeout_s, 0.05))  # keep tests fast; the point is the timeout path
            raise CMMSTimeout(f"CMMS did not answer within {self.timeout_s:.0f} s")
        wo = f"WO-{next(self._ids)}"
        self.created[wo] = {"work_order": wo, "approved_by": approved_by, **draft.model_dump(mode="json")}
        return wo
