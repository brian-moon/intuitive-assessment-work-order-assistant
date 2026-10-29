"""Runtime settings, read once from the environment.

The clock is pinned (``APP_NOW``) so that "is this telemetry stale?" gives the same answer in tests, in the
evaluation and in the demo. In production it would be the real clock.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _now() -> datetime:
    raw = os.environ.get("APP_NOW", "2026-10-29T12:00:00Z")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("APP_DATA_DIR", ROOT / "data")))
    db_path: str = field(default_factory=lambda: os.environ.get("APP_DB", str(ROOT / "runs.sqlite")))
    now: datetime = field(default_factory=_now)
    stale_after_hours: float = 24.0
    # "scripted" = deterministic stand-in model (tests, CI, offline demo); "anthropic" = Claude Haiku 5.5.
    llm: str = field(default_factory=lambda: os.environ.get("APP_LLM", "scripted"))
    model: str = field(default_factory=lambda: os.environ.get("APP_MODEL", "claude-haiku-5-5"))
    max_agent_steps: int = 6
    max_output_tokens: int = 1500
    cmms_timeout_s: float = 2.0
    # API keys -> role. Demo values; a real deployment would use the company's identity provider.
    api_keys: dict[str, str] = field(default_factory=lambda: {
        os.environ.get("APP_PLANNER_KEY", "planner-demo-key"): "planner",
        os.environ.get("APP_VIEWER_KEY", "viewer-demo-key"): "viewer",
    })
