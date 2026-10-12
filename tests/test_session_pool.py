"""Unit tests for the browser session pool (no browser launch required).

Creating a session constructs a BrowserManager (which imports Playwright but does
NOT launch a browser), and closing a never-launched session is a clean no-op — so
the pool's create/list/switch/close/active logic is fully testable offline.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="Playwright not installed")

from app.browser.session_pool import SessionPool  # noqa: E402


@pytest.mark.asyncio
async def test_defaultSessionCreatedLazily() -> None:
    pool = SessionPool()
    assert pool.activeId is None
    manager = pool.active()  # lazily creates 'default'
    assert pool.activeId == "default"
    assert manager is pool.active()  # stable


@pytest.mark.asyncio
async def test_createSwitchList() -> None:
    pool = SessionPool()
    pool.create("a")
    pool.create("b")
    assert pool.activeId == "b"  # makeActive defaults true
    info = {s["sessionId"]: s for s in pool.list()}
    assert set(info) == {"a", "b"}
    assert info["b"]["active"] is True and info["a"]["active"] is False

    pool.switch("a")
    assert pool.activeId == "a"
    assert next(s for s in pool.list() if s["sessionId"] == "a")["active"] is True


@pytest.mark.asyncio
async def test_isolatedManagersPerSession() -> None:
    pool = SessionPool()
    pool.create("a")
    mgrA = pool.active()
    pool.create("b")
    mgrB = pool.active()
    assert mgrA is not mgrB


@pytest.mark.asyncio
async def test_closeReassignsActive() -> None:
    pool = SessionPool()
    pool.create("a")
    pool.create("b")  # active = b
    closed = await pool.close("b")
    assert closed == "b"
    assert pool.activeId == "a"
    assert [s["sessionId"] for s in pool.list()] == ["a"]


@pytest.mark.asyncio
async def test_closeUnknownRaises() -> None:
    pool = SessionPool()
    pool.create("a")
    with pytest.raises(KeyError):
        await pool.close("ghost")


@pytest.mark.asyncio
async def test_switchUnknownRaises() -> None:
    pool = SessionPool()
    pool.create("a")
    with pytest.raises(KeyError):
        pool.switch("ghost")
