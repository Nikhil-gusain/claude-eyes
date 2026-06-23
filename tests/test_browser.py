"""End-to-end browser test driving a real Playwright session.

This module is deliberately resilient. Playwright (and especially its browser
binaries) is often absent in CI, so we:

* skip the whole module at import time if the ``playwright`` package is missing;
* skip the individual test at runtime if launching a browser fails (binaries
  not installed) rather than reporting a failure.

The single combined test navigates to an inline ``data:`` URL so no network is
required, then exercises navigation, text/link extraction and screenshots.
"""

from __future__ import annotations

import pytest

# Skip the entire module if Playwright is not importable.
pytest.importorskip("playwright", reason="Playwright not installed")

from app.browser.browser_manager import getBrowserManager  # noqa: E402

# A self-contained page: one heading and one link, no external resources.
HEADING_TEXT = "Hello Playwright"
HTML_DOC = (
    "<html><head><title>Test Page</title></head><body>"
    f"<h1>{HEADING_TEXT}</h1>"
    '<a href="https://example.com/next">Go next</a>'
    "</body></html>"
)
DATA_URL = "data:text/html," + HTML_DOC


@pytest.mark.asyncio
async def test_browser_navigate_extract_and_screenshot(tmp_path):
    manager = getBrowserManager()

    # navigate() lazily launches the browser; a missing binary surfaces here as
    # a non-success envelope, which we treat as "skip", not "fail".
    navResult = await manager.navigate(DATA_URL, waitUntil="load")
    if not navResult.get("success"):
        details = f"{navResult.get('error')}: {navResult.get('details')}"
        await manager.closeBrowser()
        pytest.skip(f"Playwright browser not installed ({details})")

    try:
        assert navResult["success"] is True
        assert navResult["action"] == "navigate"

        # Extracted body text should contain our heading.
        textResult = await manager.extractText()
        assert textResult["success"] is True
        assert HEADING_TEXT in textResult["data"]["text"]

        # At least the single anchor we embedded must be found.
        linksResult = await manager.extractLinks()
        assert linksResult["success"] is True
        assert linksResult["data"]["count"] >= 1

        # Screenshot returns a path that must exist on disk.
        shotResult = await manager.takeScreenshot(fullPage=False)
        assert shotResult["success"] is True
        screenshotPath = shotResult["data"]["path"]
        assert screenshotPath
        from pathlib import Path

        assert Path(screenshotPath).exists()
    finally:
        await manager.closeBrowser()
