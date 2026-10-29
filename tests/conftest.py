"""Shared fixtures. Every test builds its own app with an isolated SQLite checkpoint file and the scripted model,
so tests are deterministic, need no API key and spend no tokens."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ROOT, Settings
from app.data_access import Repository
from app.faults import Faults
from app.tools import Toolbox

KEYS = {"planner-key": "planner", "viewer-key": "viewer"}
PLANNER = {"X-API-Key": "planner-key"}
VIEWER = {"X-API-Key": "viewer-key"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=ROOT / "data", db_path=str(tmp_path / "runs.sqlite"), llm="scripted", api_keys=KEYS)


@pytest.fixture
def repo(settings) -> Repository:
    return Repository.load(settings.data_dir)


@pytest.fixture
def toolbox(repo, settings) -> Toolbox:
    return Toolbox(repo, Faults(), settings.now, settings.stale_after_hours)


@pytest.fixture
def make_client(settings):
    def make(llm=None, s: Settings | None = None) -> TestClient:
        return TestClient(create_app(s or settings, llm=llm))
    return make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def start(client: TestClient, asset_id: str = "P-101", request: str = "Investigate the alarm and draft a work order.",
          headers: dict = PLANNER):
    return client.post("/api/runs", headers=headers, json={"asset_id": asset_id, "request": request})
