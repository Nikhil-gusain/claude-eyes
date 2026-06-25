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

from app.browser import media
from app.browser.playwright_controller import PlaywrightController
from app.browser.profiles import getProfileManager
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
        self.profiles = getProfileManager()
        # Global "no-image mode": when on, pixel screenshots are suppressed in
        # favour of MarkItDown text. Starts from the configured default.
        self.noImageMode: bool = settings.noImageMode
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
        async def op() -> dict[str, Any]:
            profile = kwargs.get("profile")
            resolved = self._resolveProfile(profile)
            if resolved is None:
                # No profile chosen and none active yet -> ask the user which to
                # use (req 7). The driving agent surfaces this to the human.
                return self._profileSelectionPayload()
            name, userDataDir = resolved
            self.profiles.setActiveProfile(name)
            return await self.controller.launchBrowser(
                browserType=kwargs.get("browserType", settings.browserType),
                headless=kwargs.get("headless", settings.headless),
                viewportWidth=kwargs.get("viewportWidth"),
                viewportHeight=kwargs.get("viewportHeight"),
                userAgent=kwargs.get("userAgent"),
                userDataDir=userDataDir,
                profileName=name,
                channel=kwargs.get("channel", settings.browserChannel),
            )

        return await self._run("open_browser", op)

    async def closeBrowser(self) -> dict[str, Any]:
        return await self._run("close_browser", self.controller.closeBrowser)

    # ------------------------------------------------------------------ #
    # Profiles (multi-account; the active one persists across chats)
    # ------------------------------------------------------------------ #
    def _resolveProfile(self, profile: str | None) -> tuple[str, "Path"] | None:
        """Resolve which profile to launch.

        Returns ``(name, userDataDir)`` for an explicit profile, ``"random"``, or
        the persisted active profile. Returns ``None`` when nothing is chosen and
        no active profile exists yet (caller should prompt the user).
        """
        if profile == "random":
            name = self.profiles.chooseRandom()
        elif profile:
            name = profile
        else:
            name = self.profiles.getActiveProfile()
            if not name:
                return None
        return name, self.profiles.resolveDir(name)

    def _profileSelectionPayload(self) -> dict[str, Any]:
        """Data telling the agent to ask the user which profile to use."""
        profiles = self.profiles.listProfiles()
        return {
            "status": "profile_selection_required",
            "message": (
                "No browser profile is selected yet. Ask the user which profile to "
                "use, or whether to choose one at random. If you have a tool to ask "
                "the user (e.g. AskUserQuestion), use it; otherwise present these "
                "options and wait for their reply. Then call open_browser with "
                "profile=<name> or profile='random', or select_profile first."
            ),
            "profiles": profiles,
            "options": [p["name"] for p in profiles] + ["random", "create a new one"],
        }

    async def listProfiles(self) -> dict[str, Any]:
        return await self._run(
            "list_profiles",
            lambda: self._async({"profiles": self.profiles.listProfiles(),
                                 "active": self.profiles.getActiveProfile()}),
        )

    async def selectProfile(self, name: str) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            chosen = self.profiles.chooseRandom() if name == "random" else name
            info = self.profiles.setActiveProfile(chosen)
            return {"active": info["active"], "path": info["path"]}

        return await self._run("select_profile", op)

    async def createProfile(self, name: str, makeActive: bool = False) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            info = self.profiles.createProfile(name)
            if makeActive:
                self.profiles.setActiveProfile(info["name"])
            return {**info, "active": self.profiles.getActiveProfile()}

        return await self._run("create_profile", op)

    async def getActiveProfile(self) -> dict[str, Any]:
        return await self._run(
            "get_active_profile",
            lambda: self._async({"active": self.profiles.getActiveProfile()}),
        )

    async def loginSession(self, profile: str | None = None, url: str | None = None) -> dict[str, Any]:
        """Open a HEADED browser on a chosen/new profile for manual login/signup.

        Use when a site needs a real human to log in or create an account (Google,
        etc.) before the agent can automate it. The window is visible so the user
        can type credentials / solve captchas; the persistent profile saves the
        resulting session for all future automated runs.
        """
        async def op() -> dict[str, Any]:
            name = self.profiles.chooseRandom() if profile == "random" else (profile or "")
            if not name:
                return self._profileSelectionPayload()
            self.profiles.setActiveProfile(name)
            if self.controller.isRunning:
                await self.controller.closeBrowser()
            snapshot = await self.controller.launchBrowser(
                browserType=settings.browserType,
                headless=False,  # must be visible for a human to act
                userDataDir=self.profiles.resolveDir(name),
                profileName=name,
                channel=settings.browserChannel,
            )
            if url:
                await self.controller.openUrl(url, waitUntil="domcontentloaded")
                snapshot["url"] = url
            snapshot["loginReady"] = True
            snapshot["note"] = (
                "Headed window open. The user should log in / sign up now; the "
                "session is saved to the profile for future automated runs."
            )
            return snapshot

        return await self._run("login_session", op)

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
                # Auto-launch uses the active profile if one is set, otherwise the
                # 'default' profile — so navigate never hard-blocks. Deliberate
                # profile selection happens via open_browser / select_profile.
                name = self.profiles.getActiveProfile() or self.profiles.chooseRandom()
                self.profiles.setActiveProfile(name)
                await self.controller.launchBrowser(
                    browserType=settings.browserType,
                    headless=settings.headless,
                    userDataDir=self.profiles.resolveDir(name),
                    profileName=name,
                    channel=settings.browserChannel,
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
                    humanize=kwargs.get("humanize"),
                )
            ),
        )

    async def hover(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("hover", self._guard(lambda: self.controller.hoverElement(selector, timeoutMs)))

    async def click(self, selector: str, button: str = "left", clickCount: int = 1, timeoutMs: int | None = None, humanize: bool | None = None) -> dict[str, Any]:
        return await self._run(
            "click",
            self._guard(lambda: self.controller.clickElement(selector, button, clickCount, timeoutMs, humanize)),
        )

    async def doubleClick(self, selector: str, timeoutMs: int | None = None, humanize: bool | None = None) -> dict[str, Any]:
        return await self._run("double_click", self._guard(lambda: self.controller.doubleClickElement(selector, timeoutMs, humanize)))

    async def rightClick(self, selector: str, timeoutMs: int | None = None, humanize: bool | None = None) -> dict[str, Any]:
        return await self._run("right_click", self._guard(lambda: self.controller.rightClickElement(selector, timeoutMs, humanize)))

    async def fill(self, selector: str, value: str, clearFirst: bool = True, timeoutMs: int | None = None, humanize: bool | None = None) -> dict[str, Any]:
        return await self._run(
            "fill",
            self._guard(lambda: self.controller.fillInput(selector, value, clearFirst, timeoutMs, humanize)),
        )

    async def uploadFile(self, selector: str, filePaths: list[str]) -> dict[str, Any]:
        return await self._run("upload_file", self._guard(lambda: self.controller.uploadFile(selector, filePaths)))

    async def downloadFile(self, selector: str, saveDir: str | None = None, timeoutMs: int | None = None, imagesOnly: bool = True) -> dict[str, Any]:
        return await self._run("download_file", self._guard(lambda: self.controller.downloadFile(selector, saveDir, timeoutMs, imagesOnly)))

    async def pressKeys(self, keys: str, selector: str | None = None) -> dict[str, Any]:
        return await self._run("press_keys", self._guard(lambda: self.controller.pressKeys(keys, selector)))

    # ------------------------------------------------------------------ #
    # Waits
    # ------------------------------------------------------------------ #
    async def waitForElement(self, selector: str, state: str = "visible", timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("wait_for_element", self._guard(lambda: self.controller.waitForElement(selector, state, timeoutMs)))

    async def waitForNetworkIdle(self, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run("wait_for_network_idle", self._guard(lambda: self.controller.waitForNetworkIdle(timeoutMs)))

    async def waitForStable(self, selector: str, stableMs: int = 1200, timeoutMs: int | None = None) -> dict[str, Any]:
        """Wait until a selector's text stops changing (e.g. a streamed AI answer)."""
        return await self._run(
            "wait_for_stable",
            self._guard(lambda: self.controller.waitForStable(selector, stableMs, timeoutMs)),
        )

    async def waitForResponse(self, urlPattern: str, timeoutMs: int | None = None, includeQuery: bool = False) -> dict[str, Any]:
        """Wait until a network response matching ``urlPattern`` finishes streaming."""
        return await self._run(
            "wait_for_response",
            self._guard(lambda: self.controller.waitForResponse(urlPattern, timeoutMs, includeQuery)),
        )

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
        if self.noImageMode:
            return await self._run("take_screenshot", lambda: self._async(self._noImageNotice()))
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
        if self.noImageMode:
            return await self._run("screenshot", lambda: self._async(self._noImageNotice()))
        return await self._run(
            "screenshot",
            self._guard(
                lambda: self.screenshots.captureBytes(
                    self.controller.activePage, fullPage=fullPage, selector=selector
                )
            ),
        )

    # ------------------------------------------------------------------ #
    # No-image mode (MarkItDown) — read text instead of pixels
    # ------------------------------------------------------------------ #
    @staticmethod
    def _noImageNotice() -> dict[str, Any]:
        return {
            "noImageMode": True,
            "note": (
                "No-image mode is ON, so screenshots are suppressed. Use read_page "
                "for the page's text, or to_markdown(<image/pdf/url>) to convert "
                "media to markdown. Turn this off with set_no_image_mode(false)."
            ),
        }

    async def setNoImageMode(self, enabled: bool) -> dict[str, Any]:
        """Toggle global no-image mode (suppress screenshots, prefer markdown)."""
        async def op() -> dict[str, Any]:
            self.noImageMode = enabled
            return {
                "noImageMode": enabled,
                "markitdownAvailable": media.markitdownAvailable(),
                # Honest per-format picture: base MarkItDown may be present while
                # PDF/Office backends are not.
                "markitdownFormats": media.markitdownFormats(),
            }

        return await self._run("set_no_image_mode", op)

    async def toMarkdown(self, source: str) -> dict[str, Any]:
        """Convert an image/PDF/Office/HTML file or URL to markdown via MarkItDown."""
        async def op() -> dict[str, Any]:
            result = media.toMarkdown(source)
            if "error" in result:
                raise ValueError(f"{result['error']}: {result.get('details', '')}")
            return result

        return await self._run("to_markdown", op)

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
            snapshot["activeProfile"] = self.profiles.getActiveProfile()
            snapshot["noImageMode"] = self.noImageMode
            return snapshot

        return await self._run("status", op)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    async def _async(data: dict[str, Any]) -> dict[str, Any]:
        """Wrap a ready value as an awaitable so it can flow through ``_run``."""
        return data

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
