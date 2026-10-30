"""Runtime settings, read once from the environment.

The clock is pinned (``APP_NOW``) so that "is this telemetry stale?" gives the same answer in tests, in the
evaluation and in the demo. In production it would be the real clock.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _now() -> datetime:
    raw = os.environ.get("APP_NOW", "2026-10-29T12:00:00Z")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def _users() -> dict[str, tuple[str, str]]:
    """username -> (password, role). Demo users only with APP_DEMO_USERS=1; otherwise APP_USERS is required,
    as ``name:password:role,name:password:role``."""
    if os.environ.get("APP_DEMO_USERS") == "1":
        return {"admin": ("admin-demo", "admin"), "planner": ("planner-demo", "planner"),
                "viewer": ("viewer-demo", "viewer")}
    raw = os.environ.get("APP_USERS", "")
    users = {}
    for item in filter(None, (x.strip() for x in raw.split(","))):
        name, password, role = item.split(":", 2)
        if role not in ("admin", "planner", "viewer") or len(password) < 8:
            raise RuntimeError(f"APP_USERS entry for {name!r} needs a known role and a password of 8+ characters.")
        users[name] = (password, role)
    if not users:
        raise RuntimeError("Set APP_USERS (name:password:role,...) or APP_DEMO_USERS=1 for a local demo.")
    return users


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", "postgresql://wo:wo-dev@127.0.0.1:5440/wo"))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("APP_DATA_DIR", ROOT / "data")))
    now: datetime = field(default_factory=_now)
    # Model ladder (routing.py). auto = every configured tier: local (LOCAL_LLM_BASE_URL) -> frontier
    # (ANTHROPIC_API_KEY) -> planner; neither configured = deterministic demo model. frontier = skip the local tier.
    # scripted = demo model only (tests, CI).
    llm: str = field(default_factory=lambda: os.environ.get("APP_LLM", "auto"))
    model: str = field(default_factory=lambda: os.environ.get("APP_MODEL", "claude-haiku-5-5"))
    anthropic_configured: bool = field(default_factory=lambda: bool(os.environ.get("ANTHROPIC_API_KEY")))
    local_base_url: str = field(default_factory=lambda: os.environ.get("LOCAL_LLM_BASE_URL", ""))
    local_model: str = field(default_factory=lambda: os.environ.get("LOCAL_LLM_MODEL", "qwen3.5:9b"))
    local_token: str = field(default_factory=lambda: os.environ.get("LOCAL_LLM_TOKEN", ""))
    local_timeout_s: float = field(default_factory=lambda: float(os.environ.get("LOCAL_LLM_TIMEOUT_S", "90")))
    routing_file: Path = field(default_factory=lambda: Path(os.environ.get("APP_ROUTING", ROOT / "data" / "routing.json")))
    deployment: str = field(default_factory=lambda: os.environ.get("APP_DEPLOYMENT", "local"))
    # TrustPlane (governance observability): spans over OTLP and governance events to /v1/events. Unset = off.
    trustplane_url: str = field(default_factory=lambda: os.environ.get("TRUSTPLANE_URL", "").rstrip("/"))
    trustplane_token: str = field(default_factory=lambda: os.environ.get("TRUSTPLANE_TOKEN", ""))
    trustplane_producer: str = field(default_factory=lambda: os.environ.get("TRUSTPLANE_PRODUCER", "workorder-assistant"))
    max_agent_steps: int = 6
    max_output_tokens: int = 1500
    cmms_timeout_s: float = 2.0
    users: dict[str, tuple[str, str]] = field(default_factory=_users)
    jwt_secret: str = field(default_factory=lambda: os.environ.get("APP_JWT_SECRET") or secrets.token_hex(32))
    jwt_ttl_s: int = 8 * 3600
    # Gmail (or any SMTP) for alert e-mail. Unset = the e-mail channel is recorded as "disabled", nothing is sent.
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ.get("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")))
    public_url: str = field(default_factory=lambda: os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:8790"))
