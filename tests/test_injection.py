"""Live JS/CSS injection: run_js, add_css, add_init_js.

Covers the overlay-drawing tools: expression/function evaluation, JS errors
returned as data (not raised), CSS that really applies, and init scripts that
survive both a navigation and the start_recording -> stop_recording context
relaunch.

Auto-skips when Chromium isn't installed (same policy as the rest of the suite).
"""

from __future__ import annotations

import pytest

from app.browser.browser_manager import BrowserManager


def _payload(res: dict) -> dict:
    assert res.get("success"), res
    return res["data"]


async def _launch(mgr: BrowserManager) -> None:
    try:
        await mgr.controller.launchBrowser(headless=True)
    except Exception as exc:  # noqa: BLE001 - browsers not installed -> skip
        pytest.skip(f"Chromium unavailable: {exc}")


async def _marker(mgr: BrowserManager):
    return _payload(await mgr.runJs("window.__overlay"))["result"]


@pytest.mark.asyncio
async def test_run_js_add_css_and_init_js_survive_recording() -> None:
    mgr = BrowserManager()
    await _launch(mgr)
    try:
        page = "data:text/html,<title>T</title><h1 id=h>hi</h1>"
        _payload(await mgr.navigate(page, waitUntil="load"))

        # Bare expression and function source with an arg.
        assert _payload(await mgr.runJs("1+1")) == {"ok": True, "result": 2}
        assert _payload(await mgr.runJs("(a) => a.n * 3", {"n": 4}))["result"] == 12

        # A throwing script is data, not a tool failure.
        boom = _payload(await mgr.runJs("() => { throw new Error('boom') }"))
        assert boom["ok"] is False and "boom" in boom["error"], boom

        # Oversized results are truncated.
        big = _payload(await mgr.runJs("'x'.repeat(50000)"))
        assert big["truncated"] is True and len(big["result"]) == 20_000

        # add_css changes a computed style.
        assert _payload(await mgr.addCss("#h { color: rgb(255, 0, 0) }")) == {"ok": True}
        color = _payload(await mgr.runJs("getComputedStyle(document.getElementById('h')).color"))
        assert color["result"] == "rgb(255, 0, 0)"

        # add_init_js applies from the next load on, and survives a navigation.
        _payload(await mgr.addInitJs("window.__overlay = 'v1'", name="overlay"))
        _payload(await mgr.navigate(page + "<p>2</p>", waitUntil="load"))
        assert await _marker(mgr) == "v1"

        # Same name replaces the stored script; the relaunch drops the old one.
        _payload(await mgr.addInitJs("window.__overlay = 'v2'", name="overlay"))
        assert len(mgr.controller.initScripts) == 1

        _payload(await mgr.startRecording())
        try:
            _payload(await mgr.navigate(page + "<p>3</p>", waitUntil="load"))
            assert await _marker(mgr) == "v2"
        finally:
            _payload(await mgr.stopRecording())
        _payload(await mgr.navigate(page + "<p>4</p>", waitUntil="load"))
        assert await _marker(mgr) == "v2"

        # clear_init_js empties the list; gone after the next relaunch.
        assert _payload(await mgr.clearInitJs())["cleared"] == 1
        _payload(await mgr.startRecording())
        _payload(await mgr.stopRecording())
        _payload(await mgr.navigate(page + "<p>5</p>", waitUntil="load"))
        assert await _marker(mgr) is None
    finally:
        await mgr.closeBrowser()
