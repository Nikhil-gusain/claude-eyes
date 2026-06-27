"""End-to-end test for the new agent-intelligence features.

Mirrors ``test_browser.py``: it drives ONE real Playwright session against inline
``data:`` URLs (no network) and skips — rather than fails — when the browser
binaries are unavailable. A single combined test is used deliberately: launching and closing the same
persistent profile repeatedly races Chromium's profile lock, so we launch once
(via the same lazy-launch path ``test_browser.py`` uses) and exercise every
feature within that one session.

Covered: accessibility tree, page audit, tab summary, state snapshot, and
structured session record + replay.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("playwright", reason="Playwright not installed")

from app.browser.browser_manager import getBrowserManager  # noqa: E402

PAGE = (
    "<html><head><title>Feature Page</title></head><body>"
    "<h1>Welcome</h1>"
    '<button id="go">Go</button>'
    '<a href="https://example.com/x">link</a>'
    '<img src="data:image/png;base64,iVBORw0KGgo=" alt="broken">'
    "</body></html>"
)
DATA_URL = "data:text/html," + PAGE


@pytest.mark.asyncio
async def test_agentIntelligenceFeatures() -> None:
    manager = getBrowserManager()

    # Record a structured session first so the navigate/click below are captured
    # for the replay check at the end.
    assert (await manager.startSession("e2e"))["success"] is True

    # The first navigate lazily launches the browser (same path as test_browser.py).
    # A missing binary or a launch stall -> skip rather than hang/fail.
    try:
        nav = await asyncio.wait_for(manager.navigate(DATA_URL, waitUntil="load"), timeout=60)
    except asyncio.TimeoutError:
        await manager.closeBrowser()
        pytest.skip("Browser launch stalled (profile lock or missing binary)")
    if not nav.get("success"):
        await manager.closeBrowser()
        pytest.skip(f"Playwright browser not available ({nav.get('details')})")

    try:
        # --- Accessibility tree (aria_snapshot YAML) -------------------- #
        acc = await manager.getAccessibilityTree()
        assert acc["success"] is True
        assert acc["data"]["nodeCount"] >= 1
        assert isinstance(acc["data"]["tree"], str)
        assert "heading" in acc["data"]["tree"] or "button" in acc["data"]["tree"]

        # --- Page audit ------------------------------------------------- #
        audit = await manager.auditPage()
        assert audit["success"] is True
        for key in ("overflowIssues", "hiddenButtons", "brokenImages", "contrastProblems"):
            assert isinstance(audit["data"][key], int)
        assert "details" in audit["data"]

        # --- Tab summary ------------------------------------------------ #
        await manager.openNewTab(DATA_URL)
        tabs = await manager.getTabs()
        assert tabs["success"] is True
        assert tabs["data"]["count"] >= 2
        assert sum(1 for t in tabs["data"]["tabs"] if t["active"]) == 1
        await manager.closeTab()  # back to a single tab

        # --- Browser-state snapshot ------------------------------------- #
        snap = await manager.createSnapshot()
        assert snap["success"] is True
        assert "path" in snap["data"]
        assert "cookieCount" in snap["data"] and "tabCount" in snap["data"]

        # --- A recorded click, then stop + replay ----------------------- #
        await manager.click("#go")
        stop = await manager.stopSession()
        assert stop["success"] is True
        steps = stop["data"]["steps"]
        actions = [s["action"] for s in steps]
        assert "navigate" in actions and "click" in actions

        before = len(manager.session.steps)
        replay = await manager.replaySession(delayMs=0)
        assert replay["success"] is True
        assert replay["data"]["replayed"] == len(steps)
        assert replay["data"]["succeeded"] >= 1
        # Recording is paused during replay, so the log must not have grown.
        assert len(manager.session.steps) == before
    finally:
        await manager.closeBrowser()
