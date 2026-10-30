"""Model ladder: local model (Qwen, on premises) → frontier model (Claude Haiku 5.5) → planner.

The threshold is on a confidence that the pipeline computes from reconciliation (grounded evidence, known parts,
isolation, data age, priority rule), never on a number the model reports about itself. Policy: ``data/routing.json``.

Which tiers exist is decided by configuration, not by code: no ``LOCAL_LLM_BASE_URL`` means the frontier model is the
first tier; no ``ANTHROPIC_API_KEY`` either means the deterministic demo model is the only tier.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    start: float
    blocking_penalty: float
    warning_penalty: float
    accept: dict[str, float]
    by_priority: dict[str, dict[str, float]]
    escalate_on_data_outage: bool

    @classmethod
    def load(cls, path: Path) -> Policy:
        d = json.loads(path.read_text(encoding="utf-8"))
        c = d["confidence"]
        return cls(c["start"], c["blocking_penalty"], c["warning_penalty"], d["accept_threshold"],
                   d.get("priority_threshold", {}), d.get("escalate_on_data_outage", False))

    def as_dict(self) -> dict:
        return {"confidence": {"start": self.start, "blocking_penalty": self.blocking_penalty,
                               "warning_penalty": self.warning_penalty},
                "accept_threshold": self.accept, "priority_threshold": self.by_priority,
                "escalate_on_data_outage": self.escalate_on_data_outage}

    def confidence(self, issues: list[dict]) -> float:
        blocking = sum(i["severity"] == "blocking" for i in issues)
        warnings = len(issues) - blocking
        return round(max(0.0, self.start - blocking * self.blocking_penalty - warnings * self.warning_penalty), 3)

    def threshold(self, tier: str, priority: str | None) -> float:
        return self.by_priority.get(tier, {}).get(priority or "", self.accept.get(tier, self.accept.get("model", 0.7)))


@dataclass(frozen=True)
class Decision:
    action: str          # accept | escalate | human
    threshold: float | None
    reason: str


def decide(policy: Policy, tier: str, has_next: bool, outcome: str, confidence: float | None = None,
           priority: str | None = None) -> Decision:
    """outcome: draft | model_failure | data_outage."""
    if outcome == "draft":
        th = policy.threshold(tier, priority)
        if confidence is not None and confidence >= th:
            return Decision("accept", th, f"confidence {confidence:.2f} ≥ {th:.2f}")
        why = f"confidence {confidence:.2f} < {th:.2f}"
        return Decision("escalate" if has_next else "human", th, why)
    if outcome == "data_outage" and not policy.escalate_on_data_outage:
        return Decision("human", None, "source data unavailable: another model cannot fix it")
    return Decision("escalate" if has_next else "human", None, "no usable draft")
