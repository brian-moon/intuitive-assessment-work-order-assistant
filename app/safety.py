"""Deterministic safety guard, run BEFORE the model sees the request.

A planner (or text pasted into the request) asking to skip lockout/tagout, bypass an interlock or work on live
equipment is refused outright: no model call, no tokens, a clear reason in the run history. The model's system
prompt says the same, but a rule that must never fail is not left to a model alone.
"""
from __future__ import annotations

import re

UNSAFE = [
    (re.compile(r"\b(skip|without|no need for|ignore|bypass)\b[^.]{0,30}\b(lockout|tagout|loto|lock[- ]?out)\b", re.I),
     "asks to skip lockout/tagout"),
    (re.compile(r"\b(bypass|jumper|defeat|override|disable)\b[^.]{0,30}\b(interlock|guard|safety|e-?stop|trip)\b", re.I),
     "asks to bypass a safety interlock"),
    (re.compile(r"\b(while|keep)\b[^.]{0,20}\b(running|energi[sz]ed|live)\b", re.I),
     "asks to work on running or energised equipment"),
    (re.compile(r"\bignore (all |the )?(previous|above|prior) instructions\b", re.I),
     "contains an instruction-override attempt"),
]


def check_request(text: str) -> str | None:
    """Return the refusal reason, or None when the request is acceptable."""
    for pattern, reason in UNSAFE:
        if pattern.search(text):
            return reason
    return None
