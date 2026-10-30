"""Browser test: the critical user workflow, end to end in Chromium against the real server and the built React app.

sign in → the critical banner shows → run the pipeline for the pump in alarm → phases stream in → the draft appears →
approve → the work-order document appears.

Needs the built front end (``cd frontend && npm run build``) and Chromium (``playwright install chromium``).
Run with ``pytest -m browser``; screenshots go to ``reports/``.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from conftest import TEST_URL
from playwright.sync_api import expect

from app.config import ROOT

pytestmark = pytest.mark.browser
REPORTS = ROOT / "reports"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        pytest.skip("front end not built (cd frontend && npm run build)")
    port = _free_port()
    env = {**os.environ, "DATABASE_URL": TEST_URL, "APP_DEMO_USERS": "1", "APP_LLM": "scripted",
           "APP_RESET_ON_START": "1", "APP_JWT_SECRET": "browser-test-secret-0123456789abcdef"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if httpx.get(url + "/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("server did not start: " + proc.stdout.read().decode()[-2000:])
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def test_planner_turns_an_alarm_into_a_work_order(server, page):
    REPORTS.mkdir(exist_ok=True)
    page.goto(server)
    page.get_by_test_id("username").fill("planner")
    page.get_by_test_id("password").fill("planner-demo")
    page.get_by_test_id("sign-in").click()

    banner = page.get_by_test_id("critical-banner")
    banner.wait_for()
    assert "critical alert" in banner.inner_text()
    assert "critical" in page.get_by_test_id("asset-P-101").inner_text()
    page.screenshot(path=str(REPORTS / "1-dashboard.png"), full_page=True)

    page.get_by_test_id("open-P-101").click()
    page.get_by_test_id("start-run").click()
    page.get_by_test_id("draft").wait_for(timeout=20000)
    expect(page.get_by_test_id("run-status")).to_have_text("awaiting approval")
    for phase in ("guard", "load_asset", "data_quality", "detect_anomalies", "alert", "agent_draft", "reconcile"):
        assert "pending" not in page.get_by_test_id(f"phase-{phase}").get_attribute("class")
    assert page.get_by_test_id("priority").inner_text() == "P1"
    page.screenshot(path=str(REPORTS / "2-draft.png"), full_page=True)

    page.get_by_test_id("approve").click()
    page.get_by_test_id("work-order-doc").wait_for(timeout=20000)
    doc = page.get_by_test_id("work-order-doc").inner_text()
    assert "Work order WO-" in doc and "vibration_rms" in doc and "Lockout/tagout: required" in doc
    expect(page.get_by_test_id("run-status")).to_have_text("created")   # the last event of the stream
    page.screenshot(path=str(REPORTS / "3-work-order.png"), full_page=True)
