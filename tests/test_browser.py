"""Browser test: the critical user workflow, end to end in Chromium against the real server and the built React app.

sign in → the critical banner blinks → run the pipeline for the pump in alarm → phases stream in → the draft appears
with every reconciliation rule passed → approve → the work-order document → governance → the browser's back and
forward arrows return to the right screens. And live mode: simulator events reach the feed, the switch toggles,
history charts a past window, and "back" restores the scroll position.

Needs the built front end (``cd frontend && npm run build``) and Chromium (``playwright install chromium``).
Run with ``pytest -m browser``; screenshots go to ``reports/``.
"""
from __future__ import annotations

import os
import re
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
           "APP_RESET_ON_START": "1", "SIM_AUTOSTART": "0", "APP_JWT_SECRET": "browser-test-secret-0123456789abcdef"}
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


def sign_in(page, server, user="planner", password="planner-demo"):
    page.goto(server)
    page.get_by_test_id("username").fill(user)
    page.get_by_test_id("password").fill(password)
    page.get_by_test_id("sign-in").click()
    page.get_by_test_id("critical-banner").wait_for()


def test_planner_turns_an_alarm_into_a_work_order(server, page):
    REPORTS.mkdir(exist_ok=True)
    page.set_viewport_size({"width": 1366, "height": 900})
    sign_in(page, server)
    banner = page.get_by_test_id("critical-banner")
    assert "critical alert" in banner.inner_text()
    assert page.evaluate("getComputedStyle(document.querySelector('.banner')).animationName") == "blink"
    assert "critical" in page.get_by_test_id("asset-P-101").inner_text()
    page.screenshot(path=str(REPORTS / "1-dashboard.png"), full_page=True)

    page.get_by_test_id("open-P-101").click()
    expect(page).to_have_url(f"{server}/run/P-101")
    expect(page.get_by_test_id("reconciliation-card")).to_contain_text("Isolation is complete")
    page.get_by_test_id("start-run").click()
    page.get_by_test_id("draft").wait_for(timeout=20000)
    expect(page.get_by_test_id("run-status")).to_have_text("awaiting approval")
    expect(page).to_have_url(re.compile(rf"{server}/run/P-101/[0-9a-f]{{12}}$"))
    for phase in ("guard", "load_asset", "data_quality", "detect_anomalies", "alert", "agent_draft", "reconcile", "route"):
        assert "pending" not in page.get_by_test_id(f"phase-{phase}").get_attribute("class")
    assert "LLM" in page.get_by_test_id("phase-agent_draft").inner_text()
    assert "critic" in page.get_by_test_id("phase-reconcile").inner_text()
    assert page.get_by_test_id("priority").inner_text() == "P1"
    statuses = {page.get_by_test_id(f"rule-{r}").get_attribute("data-status")
                for r in ("asset", "evidence", "anomaly", "parts", "isolation", "data", "stock", "priority")}
    assert statuses == {"pass"}
    expect(page.get_by_test_id("confidence")).to_contain_text("1.00")
    page.screenshot(path=str(REPORTS / "2-draft.png"), full_page=True)

    page.get_by_test_id("approve").click()
    page.get_by_test_id("work-order-doc").wait_for(timeout=20000)
    doc = page.get_by_test_id("work-order-doc").inner_text()
    assert "Work order WO-" in doc and "vibration_rms" in doc and "Lockout/tagout: required" in doc
    expect(page.get_by_test_id("run-status")).to_have_text("created")
    page.screenshot(path=str(REPORTS / "3-work-order.png"), full_page=True)
    run_url = page.url

    page.get_by_test_id("tab-governance").click()
    expect(page.get_by_test_id("recent-routes")).to_contain_text("P-101")
    expect(page.get_by_test_id("ladder")).to_contain_text("Planner")
    page.screenshot(path=str(REPORTS / "4-governance.png"), full_page=True)

    # browser arrows: back to the run (its document reloads from the server), forward to governance
    page.go_back()
    expect(page).to_have_url(run_url)
    page.get_by_test_id("work-order-doc").wait_for(timeout=10000)
    page.go_forward()
    expect(page.get_by_test_id("ladder")).to_be_visible()


def test_live_mode_history_and_scroll_position(server, page):
    page.set_viewport_size({"width": 1366, "height": 700})
    sign_in(page, server)
    page.get_by_role("button", name="+1 event").click()
    page.get_by_role("button", name="+1 event").click()
    expect(page.get_by_test_id("feed").locator("li").first).to_be_visible(timeout=10000)
    switch = page.get_by_test_id("sim-switch")
    switch.click()
    expect(switch).to_have_attribute("aria-pressed", "true")
    switch.click()
    expect(switch).to_have_attribute("aria-pressed", "false")

    # history: a past window for one asset, kept in the URL
    page.get_by_test_id("mode-history").click()
    expect(page).to_have_url(f"{server}/dashboard/history")
    page.get_by_test_id("history-asset").select_option("P-101")
    expect(page).to_have_url(re.compile(r"/dashboard/history\?asset=P-101"))
    page.get_by_test_id("range-30d").click()
    expect(page.get_by_test_id("history-charts")).to_contain_text("vibration_rms")
    page.screenshot(path=str(REPORTS / "5-history.png"), full_page=True)
    page.go_back()
    expect(page.get_by_test_id("range-24h")).to_have_class("on")          # back restores the previous window
    page.get_by_test_id("mode-live").click()
    expect(page).to_have_url(f"{server}/dashboard")

    # scroll down, leave through the (sticky) menu, come back with the browser arrow: same place on the page
    page.evaluate("window.scrollTo(0, 400)")
    page.wait_for_timeout(300)
    y0 = page.evaluate("window.scrollY")
    assert y0 > 100
    page.get_by_test_id("tab-governance").click()
    expect(page.get_by_test_id("ladder")).to_be_visible()
    assert page.evaluate("window.scrollY") == 0
    page.go_back()
    expect(page).to_have_url(f"{server}/dashboard")
    # the same place on the page; the browser's scroll anchoring may nudge it by a few pixels if content above settles
    page.wait_for_function(f"Math.abs(window.scrollY - {y0}) <= 15", timeout=5000)
    page.go_forward()
    expect(page.get_by_test_id("ladder")).to_be_visible()
    page.go_back()
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(REPORTS / "6-live.png"))
