"""Runtime settings, read once from the environment.

Clock: the real clock by default (live mode). ``APP_NOW`` pins it, so that "is this telemetry stale?" gives the same
answer every time; tests and the evaluation pin it to ``SEED_ANCHOR``. With the real clock the seed data is shifted
at load so that its newest reading lands just before start-up: the demo opens with the same alarms, then ages.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


SEED_ANCHOR = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)   # the "now" the seed data was written for


def _pinned() -> datetime | None:
    raw = os.environ.get("APP_NOW", "")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC) if raw else None


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


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
    pinned_now: datetime | None = field(default_factory=_pinned)
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
    max_output_tokens: int = 4096   # a draft is ~1.5k tokens; a lower cap truncated tool input mid-call
    cmms_timeout_s: float = 2.0
    users: dict[str, tuple[str, str]] = field(default_factory=_users)
    # Signs sign-in tokens. Unset: generated once and kept in the database, so a restart does not sign everyone out.
    jwt_secret: str = field(default_factory=lambda: os.environ.get("APP_JWT_SECRET", ""))
    jwt_ttl_s: int = 8 * 3600
    # Gmail (or any SMTP) for alert e-mail. docker compose defaults to the local test inbox (host "mailpit", no login).
    # Unset = the e-mail channel is recorded as "disabled", nothing is sent. Empty port: 1025 for mailpit, else 587.
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT") or
                                                       (1025 if os.environ.get("SMTP_HOST") == "mailpit" else 587)))
    smtp_user: str = field(default_factory=lambda: os.environ.get("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER")
                           or "alerts@workorder-assistant.local")
    # Where mail sent to the local test inbox can be read (shown on the dashboard); empty when real mail is used.
    mail_inbox_url: str = field(default_factory=lambda: os.environ.get("MAIL_INBOX_URL", ""))
    public_url: str = field(default_factory=lambda: os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:8790"))
    # Recipients registered at start-up, "Name <a@b.org>, c@d.org" (more can be added on the Admin screen).
    alert_emails: str = field(default_factory=lambda: os.environ.get("ALERT_EMAILS", ""))
    # One e-mail per asset, metric and severity per recipient within this window; the dashboard still shows every alert.
    email_cooldown_min: int = field(default_factory=lambda: int(os.environ.get("ALERT_EMAIL_COOLDOWN_MIN", "15")))
    # Live telemetry simulator: on/off and rate are changed on the dashboard; these are the start-up values.
    sim_autostart: bool = field(default_factory=lambda: _env_bool("SIM_AUTOSTART", "1"))
    sim_rate: float = field(default_factory=lambda: float(os.environ.get("SIM_RATE", "6")))
    sim_unit: str = field(default_factory=lambda: os.environ.get("SIM_UNIT", "minute"))

    # Branding: a logo file you place in ./branding (not in the repository) and the names shown in the header.
    brand_name: str = field(default_factory=lambda: os.environ.get("APP_BRAND_NAME", "Work-order assistant"))
    brand_subtitle: str = field(default_factory=lambda: os.environ.get("APP_BRAND_SUBTITLE", "Maintenance operations"))
    brand_logo: str = field(default_factory=lambda: os.environ.get("APP_BRAND_LOGO", ""))
    brand_dir: Path = field(default_factory=lambda: Path(os.environ.get("APP_BRAND_DIR", ROOT / "branding")))

    @property
    def now(self) -> datetime:
        return self.pinned_now or datetime.now(UTC)

    @property
    def live(self) -> bool:
        return self.pinned_now is None
