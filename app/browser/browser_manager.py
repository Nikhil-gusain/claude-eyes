"""High-level facade over the browser stack.

``BrowserManager`` is the single object that the API server, the MCP server and
the AI adapters all talk to. It:

* owns one :class:`PlaywrightController`, one :class:`ScreenshotManager` and one
  :class:`VideoRecorder`;
* wraps every operation in the AI-friendly success/error envelope;
* serialises access with an ``asyncio.Lock`` so concurrent callers (HTTP +
  WebSocket + MCP at once) cannot corrupt browser state.

This is the seam for future expansion: a ``BrowserPool`` would manage many
managers keyed by session id, and ``getBrowserManager`` would resolve one per
session instead of returning a process-wide singleton.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.browser.playwright_controller import PlaywrightController
from app.browser.screenshot_manager import ScreenshotManager
from app.browser.video_recorder import VideoRecorder
from app.utils.config import settings
from app.utils.error_handler import BrowserNotRunningError, buildErrorEnvelope
from app.utils.helpers import successResponse
from app.utils.logger import getLogger

logger = getLogger("browser.manager")


class BrowserManager:
    """Process-wide coordinator returning enveloped results for every action."""

    def __init__(self) -> None:
        self.controller = PlaywrightController()
        self.screenshots = ScreenshotManager()
        self.recorder = VideoRecorder()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Envelope plumbing
    # ------------------------------------------------------------------ #
    async def _run(self, action: str, coro: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """Execute *coro* under the lock and wrap the outcome in an envelope."""
        async with self._lock:
            try:
                data = await coro()
                return successResponse(action, data)
            except Exception as exc:  # noqa: BLE001 - surfaced as structured error
                logger.exception("Action '%s' failed", action)
                return buildErrorEnvelope(action, exc)

    def _requireRunning(self) -> None:
        if not self.controller.isRunning:
            raise BrowserNotRunningError(
                "Browser is not running. Call open_browser first."
            )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def openBrowser(self, **kwargs: Any) -> dict[str, Any]:
        return await self._run(
            "open_browser",
            lambda: self.controller.launchBrowser(
                browserType=kwargs.get("browserType", settings.browserType),
                headless=kwargs.get("headless", settings.headless),
                viewportWidth=kwargs.get("viewportWidth"),
                viewportHeight=kwargs.get("viewportHeight"),
                userAgent=kwargs.get("userAgent"),
            ),
        )

    async def closeBrowser(self) -> dict[str, Any]:
        return await self._run("close_browser", self.controller.closeBrowser)

    async def setHeadless(self, headless: bool) -> dict[str, Any]:
        """Switch a running browser between headless and headed (state preserved)."""
        return await self._run("set_headless", lambda: self.controller.setHeadless(headless))

    async def clearProfile(self) -> dict[str, Any]:
        """Wipe the persistent profile — logs out of everything for a fresh session."""
        return await self._run("clear_profile", self.controller.clearProfile)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def navigate(self, url: str, waitUntil: str = "networkidle", timeoutMs: int | None = None) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            if not self.controller.isRunning:
                await self.controller.launchBrowser(
                    browserType=settings.browserType, headless=settings.headless
                )
            return await self.controller.openUrl(url, waitUntil=waitUntil, timeoutMs=timeoutMs)

        return await self._run("navigate", op)

    async def navigateBack(self) -> dict[str, Any]:
        return await self._run("navigate_back", self._guard(self.controller.navigateBack))

    async def navigateForward(self) -> dict[str, Any]:
        return await self._run("navigate_forward", self._guard(self.controller.navigateForward))

    async def refresh(self) -> dict[str, Any]:
        return await self._run("refresh", self._guard(self.controller.refreshPage))

    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #
    async def openNewTab(self, url: str | None = None) -> dict[str, Any]:
        return await self._run("open_new_tab", self._guard(lambda: self.controller.openNewTab(url)))

    async def switchTab(self, index: int) -> dict[str, Any]:
        return await self._run("switch_tab", self._guard(lambda: self.controller.switchTab(index)))

    async def closeTab(self, index: int | None = None) -> dict[str, Any]:
        return await self._run("close_tab", self._guard(lambda: self.controller.closeTab(index)))

    # ------------------------------------------------------------------ #
    # Extraction / info
    # ------------------------------------------------------------------ #
    async def getTitle(self) -> dict[str, Any]:
        return await self._run("get_title", self._guard(self.controller.getTitle))

    async def getUrl(self) -> dict[str, Any]:
        return await self._run("get_url", self._guard(self.controller.getUrl))

    async def extractText(self) -> dict[str, Any]:
        return await self._run("extract_text", self._guard(self.controller.extractText))

    async def extractLinks(self) -> dict[str, Any]:
        return await self._run("extract_links", self._guard(self.controller.extractLinks))

    async def extractButtons(self) -> dict[str, Any]:
        return await self._run("extract_buttons", self._guard(self.controller.extractButtons))

    async def extractForms(self) -> dict[str, Any]:
        return await self._run("extract_forms", self._guard(self.controller.extractForms))

    async def extractImages(self) -> dict[str, Any]:
        return await self._run("extract_images", self._guard(self.controller.extractImages))

    async def getDom(self, selector: str | None = None) -> dict[str, Any]:
        return await self._run("get_dom", self._guard(lambda: self.controller.getDom(selector)))

    # ------------------------------------------------------------------ #
    # Interaction
    # ------------------------------------------------------------------ #
    async def scroll(self, **kwargs: Any) -> dict[str, Any]:
        return await self._run(
            "scroll",
            self._guard(
                lambda: self.controller.scrollPage(
                    deltaX=kwargs.get("deltaX", 0),
                    deltaY=kwargs.get("deltaY", 0),
                    selector=kwargs.get("selector"),
                    toTop=kwargs.get("toTop", False),
                    toBottom=kwargs.get("toBottom", False),
                )
            ),
        )

    async def hover(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("hover", self._guard(lambda: self.controller.hoverElement(selector, timeoutMs)))

    async def click(self, selector: str, button: str = "left", clickCount: int = 1, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run(
            "click",
            self._guard(lambda: self.controller.clickElement(selector, button, clickCount, timeoutMs)),
        )

    async def doubleClick(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("double_click", self._guard(lambda: self.controller.doubleClickElement(selector, timeoutMs)))

    async def rightClick(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("right_click", self._guard(lambda: self.controller.rightClickElement(selector, timeoutMs)))

    async def fill(self, selector: str, value: str, clearFirst: bool = True, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run(
            "fill",
            self._guard(lambda: self.controller.fillInput(selector, value, clearFirst, timeoutMs)),
        )

    async def selectOption(self, selector: str, value: str | None = None, label: str | None = None, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run(
            "select_option",
            self._guard(lambda: self.controller.selectOption(selector, value, label, timeoutMs)),
        )

    async def uploadFile(self, selector: str, filePaths: list[str]) -> dict[str, Any]:
        return await self._run("upload_file", self._guard(lambda: self.controller.uploadFile(selector, filePaths)))

    async def downloadFile(self, selector: str, saveDir: str | None = None, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("download_file", self._guard(lambda: self.controller.downloadFile(selector, saveDir, timeoutMs)))

    async def pressKeys(self, keys: str, selector: str | None = None) -> dict[str, Any]:
        return await self._run("press_keys", self._guard(lambda: self.controller.pressKeys(keys, selector)))

    # ------------------------------------------------------------------ #
    # Waits
    # ------------------------------------------------------------------ #
    async def waitForElement(self, selector: str, state: str = "visible", timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("wait_for_element", self._guard(lambda: self.controller.waitForElement(selector, state, timeoutMs)))

    async def waitForNetworkIdle(self, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("wait_for_network_idle", self._guard(lambda: self.controller.waitForNetworkIdle(timeoutMs)))

    # ------------------------------------------------------------------ #
    # Visual intelligence
    # ------------------------------------------------------------------ #
    async def takeScreenshot(
        self,
        fullPage: bool = False,
        selector: str | None = None,
        annotate: bool = False,
        label: str | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            "take_screenshot",
            self._guard(
                lambda: self.screenshots.capture(
                    self.controller.activePage,
                    fullPage=fullPage,
                    selector=selector,
                    annotate=annotate,
                    label=label,
                )
            ),
        )

    async def captureScreenshotData(
        self, fullPage: bool = False, selector: str | None = None
    ) -> dict[str, Any]:
        """Capture a screenshot in memory (no file). ``data.image`` holds PNG bytes.

        Used by the MCP ``screenshot`` tool so an AI can *see* the page inline
        without persisting anything to disk.
        """
        return await self._run(
            "screenshot",
            self._guard(
                lambda: self.screenshots.captureBytes(
                    self.controller.activePage, fullPage=fullPage, selector=selector
                )
            ),
        )

    # ------------------------------------------------------------------ #
    # Storage maintenance
    # ------------------------------------------------------------------ #
    async def clearStorage(self, kinds: list[str] | None = None) -> dict[str, Any]:
        """Delete saved screenshots/recordings/downloads to free disk space.

        Does not require a running browser. ``kinds`` defaults to all three; pass
        a subset like ``["screenshots"]`` to target one. ``.gitkeep`` is preserved.
        """

        async def op() -> dict[str, Any]:
            mapping = {
                "screenshots": settings.screenshotDir,
                "recordings": settings.recordingDir,
                "downloads": settings.storageDir / "downloads",
            }
            targets = kinds or list(mapping.keys())
            removed = 0
            for kind in targets:
                directory = mapping.get(kind)
                if not directory or not Path(directory).exists():
                    continue
                for entry in Path(directory).iterdir():
                    if entry.is_file() and entry.name != ".gitkeep":
                        entry.unlink()
                        removed += 1
            return {"removed": removed, "kinds": targets}

        return await self._run("clear_storage", op)

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    async def startRecording(self, fps: int | None = None, sessionName: str | None = None) -> dict[str, Any]:
        return await self._run(
            "start_recording",
            self._guard(lambda: self.recorder.start(self.controller, fps=fps, sessionName=sessionName)),
        )

    async def stopRecording(self) -> dict[str, Any]:
        return await self._run("stop_recording", self._guard(lambda: self.recorder.stop(self.controller)))

    # ------------------------------------------------------------------ #
    # Network inspection
    # ------------------------------------------------------------------ #
    async def getNetwork(self, limit: int = 100, urlContains: str | None = None) -> dict[str, Any]:
        return await self._run(
            "get_network",
            self._guard(lambda: self.controller.getNetwork(limit, urlContains)),
        )

    async def clearNetwork(self) -> dict[str, Any]:
        return await self._run("clear_network", self._guard(self.controller.clearNetwork))

    # ------------------------------------------------------------------ #
    # Aggregate page read (one call returns title + url + text + structure)
    # ------------------------------------------------------------------ #
    async def readPage(self, textLimit: int = 5000) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            title = await self.controller.getTitle()
            url = await self.controller.getUrl()
            text = await self.controller.extractText()
            links = await self.controller.extractLinks()
            buttons = await self.controller.extractButtons()
            forms = await self.controller.extractForms()
            images = await self.controller.extractImages()
            return {
                "title": title.get("title"),
                "url": url.get("url"),
                "text": text.get("text", "")[:textLimit],
                "textLength": text.get("length", 0),
                "links": links.get("links", [])[:150],
                "linkCount": links.get("count", 0),
                "buttons": buttons.get("buttons", []),
                "forms": forms.get("forms", []),
                "imageCount": images.get("count", 0),
            }

        return await self._run("read_page", self._guard(op))

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    async def status(self) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            snapshot = self.controller._stateSnapshot()  # noqa: SLF001 - internal read
            snapshot["recording"] = self.recorder.isRecording
            return snapshot

        return await self._run("status", op)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _guard(self, coroFactory: Callable[[], Awaitable[dict[str, Any]]]) -> Callable[[], Awaitable[dict[str, Any]]]:
        """Wrap an operation so it first asserts the browser is running."""

        async def wrapper() -> dict[str, Any]:
            self._requireRunning()
            return await coroFactory()

        return wrapper


# --------------------------------------------------------------------- #
# Process-wide singleton accessor (future: resolve per-session from a pool)
# --------------------------------------------------------------------- #
_managerSingleton: BrowserManager | None = None


def getBrowserManager() -> BrowserManager:
    """Return the shared :class:`BrowserManager`, creating it on first use."""
    global _managerSingleton
    if _managerSingleton is None:
        _managerSingleton = BrowserManager()
    return _managerSingleton
