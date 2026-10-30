"""Shared fixtures.

Tests run against a real PostgreSQL (``TEST_DATABASE_URL``, default: the docker-compose service on :5440, database
``wo_test``, created if missing). Every test gets a freshly seeded database, the scripted model and a recording e-mail
sender, so tests are deterministic, need no API key, spend no tokens and send no mail.
"""
from __future__ import annotations

import json
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ROOT, SEED_ANCHOR, Settings

ADMIN_URL = os.environ.get("TEST_ADMIN_URL", "postgresql://wo:wo-dev@127.0.0.1:5440/wo")
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://wo:wo-dev@127.0.0.1:5440/wo_test")
USERS = {"admin": ("admin-pass-1", "admin"), "planner": ("planner-pass-1", "planner"), "viewer": ("viewer-pass-1", "viewer")}


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    name = TEST_URL.rsplit("/", 1)[1]
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            c.execute(f'CREATE DATABASE "{name}"')
    yield


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=TEST_URL, data_dir=ROOT / "data", llm="scripted", users=USERS, pinned_now=SEED_ANCHOR,
                    jwt_secret="test-secret-for-the-suite-only-0123456789", sim_autostart=False)


class Mailbox(list):
    def __call__(self, to: str, subject: str, body: str, html: str | None = None, severity: str = "warning") -> None:
        self.append({"to": to, "subject": subject, "body": body, "html": html, "severity": severity})


@pytest.fixture
def mailbox() -> Mailbox:
    return Mailbox()


@pytest.fixture
def make_client(settings, mailbox):
    made = []

    def make(llm=None, reset=True, s: Settings | None = None, tiers=None, gov=None) -> TestClient:
        c = TestClient(create_app(s or settings, llm=llm, send=mailbox, reset=reset, tiers=tiers, gov=gov))
        made.append(c)
        return c
    yield make
    for c in made:
        c.app.state.close()


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def auth(client: TestClient, user: str) -> dict:
    token = client.post("/api/login", json={"username": user, "password": USERS[user][0]}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def events(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def run(client: TestClient, asset_id: str = "P-101", request: str = "Investigate the alarm and draft a work order.",
        user: str = "planner") -> tuple[str, list[dict]]:
    evs = events(client.post("/api/runs/stream", headers=auth(client, user),
                             json={"asset_id": asset_id, "request": request}))
    return evs[-1]["run_id"], evs


def decide(client: TestClient, run_id: str, decision: str = "approve", user: str = "planner", **extra) -> list[dict]:
    return events(client.post(f"/api/runs/{run_id}/decision/stream", headers=auth(client, user),
                              json={"decision": decision, **extra}))


def phases(evs: list[dict]) -> list[tuple[str, str]]:
    return [(e["phase"], e["status"]) for e in evs]
