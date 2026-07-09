"""Parallel-tab smoke test: two tabs driven concurrently stay isolated.

Proves the core of the parallel-tabs feature:
* actions addressed by ``tab=`` hit the right page (no cross-contamination),
  even when the "active" tab is something else;
* two tabs navigate concurrently (per-tab locks, not one global lock);
* network capture is filtered per tab.

Auto-skips when Chromium isn't installed (same policy as the rest of the suite).
"""

from __future__ import annotations

import asyncio

import pytest

from app.browser.browser_manager import BrowserManager
from app.browser.tab_context import currentTab


def _payload(res: dict) -> dict:
    assert res.get("success"), res
    return res["data"]


async def _launch(mgr: BrowserManager) -> None:
    try:
        await mgr.controller.launchBrowser(headless=True)
    except Exception as exc:  # noqa: BLE001 - browsers not installed -> skip
        pytest.skip(f"Chromium unavailable: {exc}")


@pytest.mark.asyncio
async def test_parallel_tabs_stay_isolated() -> None:
    mgr = BrowserManager()
    await _launch(mgr)
    try:
        pageA = "data:text/html,<title>AAA</title><h1>alpha</h1>"
        pageB = "data:text/html,<title>BBB</title><h1>bravo</h1>"

        tabA = _payload(await mgr.openNewTab(makeActive=False))["tab"]
        tabB = _payload(await mgr.openNewTab(makeActive=True))["tab"]
        assert tabA != tabB

        # Drive both tabs at once. If they shared one lock this would serialise;
        # if they shared an "active tab" pointer the URLs would cross over.
        await asyncio.gather(
            mgr.navigate(pageA, waitUntil="domcontentloaded", tab=tabA),
            mgr.navigate(pageB, waitUntil="domcontentloaded", tab=tabB),
        )

        readA = _payload(await mgr.readPage(tab=tabA))
        readB = _payload(await mgr.readPage(tab=tabB))
        assert readA["title"] == "AAA" and "alpha" in readA["text"], readA
        assert readB["title"] == "BBB" and "bravo" in readB["text"], readB

        # Friendly name resolves to the same tab as the uuid handle.
        byName = _payload(await mgr.readPage(tab="tab-2"))  # tab-2 == first opened == tabA
        assert byName["title"] == "AAA"
    finally:
        await mgr.closeBrowser()


@pytest.mark.asyncio
async def test_network_is_scoped_per_tab() -> None:
    mgr = BrowserManager()
    await _launch(mgr)
    try:
        tabA = _payload(await mgr.openNewTab(makeActive=False))["tab"]
        tabB = _payload(await mgr.openNewTab(makeActive=True))["tab"]

        # Fabricate one network entry per tab directly on the log (no external
        # network needed) and confirm the per-tab filter separates them.
        ctrl = mgr.controller
        ctrl.networkLog[:] = [
            {"url": "https://a.test/x", "tabId": tabA},
            {"url": "https://b.test/y", "tabId": tabB},
        ]
        netA = _payload(await mgr.getNetwork(tab=tabA))
        netB = _payload(await mgr.getNetwork(tab=tabB))
        assert [r["url"] for r in netA["requests"]] == ["https://a.test/x"]
        assert [r["url"] for r in netB["requests"]] == ["https://b.test/y"]
        allNet = _payload(await mgr.getNetwork(allTabs=True, tab=tabA))
        assert allNet["returned"] == 2
    finally:
        await mgr.closeBrowser()


@pytest.mark.asyncio
async def test_contextvar_override_is_inherited() -> None:
    """The MCP/adapter path: the tab is set via currentTab (as safeAsync /
    dispatchTool do), and a manager call WITHOUT an explicit tab= still targets
    it. This is what makes every tab-addressable tool work without threading tab
    through every method."""
    mgr = BrowserManager()
    await _launch(mgr)
    try:
        tabA = _payload(await mgr.openNewTab(makeActive=False))["tab"]
        tabB = _payload(await mgr.openNewTab(makeActive=True))["tab"]  # active
        await mgr.navigate("data:text/html,<title>AAA</title>", waitUntil="domcontentloaded", tab=tabA)
        await mgr.navigate("data:text/html,<title>BBB</title>", waitUntil="domcontentloaded", tab=tabB)

        token = currentTab.set(tabA)  # simulate the transport setting the override
        try:
            read = _payload(await mgr.readPage())  # NO tab= -> inherits tabA
            assert read["title"] == "AAA", read
        finally:
            currentTab.reset(token)

        # With no override, falls back to the active tab (tabB).
        assert _payload(await mgr.readPage())["title"] == "BBB"
    finally:
        await mgr.closeBrowser()
