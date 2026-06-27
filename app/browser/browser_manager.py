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
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.browser import media
from app.browser import visual_diff
from app.browser.playwright_controller import PlaywrightController
from app.browser.profiles import getProfileManager
from app.browser.screenshot_manager import ScreenshotManager
from app.browser.session_recorder import SessionRecorder
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
        # Structured action log (replayable JSON), distinct from the video recorder.
        self.session = SessionRecorder()
        self.profiles = getProfileManager()
        # Global "no-image mode": when on, pixel screenshots are suppressed in
        # favour of MarkItDown text. Starts from the configured default.
        self.noImageMode: bool = settings.noImageMode
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Envelope plumbing
    # ------------------------------------------------------------------ #
    async def _run(
        self,
        action: str,
        coro: Callable[[], Awaitable[dict[str, Any]]],
        recordParams: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute *coro* under the lock and wrap the outcome in an envelope.

        When *recordParams* is supplied the action is replayable: on success it is
        appended to the active session recording (a no-op if not recording). Only
        successful actions are logged, so a replay never reproduces a failed step.
        """
        async with self._lock:
            try:
                data = await coro()
                if recordParams is not None:
                    self.session.record(action, recordParams)
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

        return await self._run(
            "navigate", op, recordParams={"url": url, "waitUntil": waitUntil, "timeoutMs": timeoutMs}
        )

    async def navigateBack(self) -> dict[str, Any]:
        return await self._run("navigate_back", self._guard(self.controller.navigateBack), recordParams={})

    async def navigateForward(self) -> dict[str, Any]:
        return await self._run("navigate_forward", self._guard(self.controller.navigateForward), recordParams={})

    async def refresh(self) -> dict[str, Any]:
        return await self._run("refresh", self._guard(self.controller.refreshPage), recordParams={})

    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #
    async def openNewTab(self, url: str | None = None) -> dict[str, Any]:
        return await self._run(
            "open_new_tab", self._guard(lambda: self.controller.openNewTab(url)),
            recordParams={"url": url},
        )

    async def switchTab(self, index: int) -> dict[str, Any]:
        return await self._run(
            "switch_tab", self._guard(lambda: self.controller.switchTab(index)),
            recordParams={"index": index},
        )

    async def closeTab(self, index: int | None = None) -> dict[str, Any]:
        return await self._run(
            "close_tab", self._guard(lambda: self.controller.closeTab(index)),
            recordParams={"index": index},
        )

    async def getTabs(self) -> dict[str, Any]:
        """Summarise every open tab — index, title, URL, host, and which is active."""
        return await self._run("get_tabs", self._guard(self.controller.getTabs))

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
            recordParams=kwargs,
        )

    async def hover(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run(
            "hover", self._guard(lambda: self.controller.hoverElement(selector, timeoutMs)),
            recordParams={"selector": selector, "timeoutMs": timeoutMs},
        )

    async def click(self, selector: str, button: str = "left", clickCount: int = 1, timeoutMs: int | None = None, humanize: bool | None = None) -> dict[str, Any]:
        return await self._run(
            "click",
            self._guard(lambda: self.controller.clickElement(selector, button, clickCount, timeoutMs, humanize)),
            recordParams={"selector": selector, "button": button, "clickCount": clickCount,
                          "timeoutMs": timeoutMs, "humanize": humanize},
        )

    async def doubleClick(self, selector: str, timeoutMs: int | None = None, humanize: bool | None = None) -> dict[str, Any]:
        return await self._run(
            "double_click", self._guard(lambda: self.controller.doubleClickElement(selector, timeoutMs, humanize)),
            recordParams={"selector": selector, "timeoutMs": timeoutMs, "humanize": humanize},
        )

    async def rightClick(self, selector: str, timeoutMs: int | None = None, humanize: bool | None = None) -> dict[str, Any]:
        return await self._run(
            "right_click", self._guard(lambda: self.controller.rightClickElement(selector, timeoutMs, humanize)),
            recordParams={"selector": selector, "timeoutMs": timeoutMs, "humanize": humanize},
        )

    async def fill(self, selector: str, value: str, clearFirst: bool = True, timeoutMs: int | None = None, humanize: bool | None = None) -> dict[str, Any]:
        return await self._run(
            "fill",
            self._guard(lambda: self.controller.fillInput(selector, value, clearFirst, timeoutMs, humanize)),
            recordParams={"selector": selector, "value": value, "clearFirst": clearFirst,
                          "timeoutMs": timeoutMs, "humanize": humanize},
        )

    async def uploadFile(self, selector: str, filePaths: list[str]) -> dict[str, Any]:
        return await self._run(
            "upload_file", self._guard(lambda: self.controller.uploadFile(selector, filePaths)),
            recordParams={"selector": selector, "filePaths": filePaths},
        )

    async def downloadFile(self, selector: str, saveDir: str | None = None, timeoutMs: int | None = None, imagesOnly: bool = True) -> dict[str, Any]:
        return await self._run("download_file", self._guard(lambda: self.controller.downloadFile(selector, saveDir, timeoutMs, imagesOnly)))

    async def pressKeys(self, keys: str, selector: str | None = None) -> dict[str, Any]:
        return await self._run(
            "press_keys", self._guard(lambda: self.controller.pressKeys(keys, selector)),
            recordParams={"keys": keys, "selector": selector},
        )

    # ------------------------------------------------------------------ #
    # Waits
    # ------------------------------------------------------------------ #
    async def waitForElement(self, selector: str, state: str = "visible", timeoutMs: int | None = None) -> dict[str, Any]:
        return await self._run(
            "wait_for_element", self._guard(lambda: self.controller.waitForElement(selector, state, timeoutMs)),
            recordParams={"selector": selector, "state": state, "timeoutMs": timeoutMs},
        )

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
    # Accessibility / page audit
    # ------------------------------------------------------------------ #
    async def getAccessibilityTree(
        self, interestingOnly: bool = True, root: str | None = None
    ) -> dict[str, Any]:
        """Return the page's accessibility tree (roles/names a screen reader sees)."""
        return await self._run(
            "get_accessibility_tree",
            self._guard(lambda: self.controller.getAccessibilityTree(interestingOnly, root)),
        )

    async def auditPage(self, sampleLimit: int = 400) -> dict[str, Any]:
        """Audit the page for overflow, hidden controls, broken images, low contrast."""
        return await self._run(
            "audit_page", self._guard(lambda: self.controller.auditPage(sampleLimit))
        )

    # ------------------------------------------------------------------ #
    # Visual diff (pure file comparison — no running browser required)
    # ------------------------------------------------------------------ #
    async def compareScreenshots(
        self,
        before: str,
        after: str,
        pixelThreshold: int = 60,
        saveDiff: bool = False,
    ) -> dict[str, Any]:
        """Compare two screenshot files and quantify their visual difference."""
        return await self._run(
            "compare_screenshots",
            lambda: self._async(
                visual_diff.compareImages(before, after, pixelThreshold, saveDiff)
            ),
        )

    # ------------------------------------------------------------------ #
    # Browser-state snapshot (cookies + storage + open tabs)
    # ------------------------------------------------------------------ #
    async def createSnapshot(self, savePath: str | None = None) -> dict[str, Any]:
        """Capture cookies/localStorage/sessionStorage/open-tabs; optionally save to JSON."""
        async def op() -> dict[str, Any]:
            self._requireRunning()
            snapshot = await self.controller.createSnapshot()
            result: dict[str, Any] = {
                "cookieCount": len(snapshot.get("cookies", [])),
                "localStorageKeys": len(snapshot.get("localStorage", {})),
                "sessionStorageKeys": len(snapshot.get("sessionStorage", {})),
                "tabCount": len(snapshot.get("tabs", [])),
                "origin": snapshot.get("origin"),
                "url": snapshot.get("url"),
            }
            # Always persist: a snapshot you can't restore later isn't useful.
            target = (
                Path(savePath)
                if savePath
                else settings.snapshotDir / f"snapshot-{snapshot['capturedAt'].replace(':', '-')}.json"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            result["path"] = str(target)
            return result

        return await self._run("create_snapshot", op)

    async def restoreSnapshot(
        self, path: str | None = None, snapshot: dict[str, Any] | None = None, navigate: bool = True
    ) -> dict[str, Any]:
        """Restore cookies + storage from a saved snapshot file (or inline dict)."""
        async def op() -> dict[str, Any]:
            self._requireRunning()
            data = snapshot
            if data is None:
                if not path:
                    raise ValueError("Provide either 'path' to a saved snapshot or an inline 'snapshot'.")
                source = Path(path)
                if not source.exists():
                    raise FileNotFoundError(f"Snapshot file not found: {path}")
                data = json.loads(source.read_text(encoding="utf-8"))
            return await self.controller.restoreSnapshot(data, navigate=navigate)

        return await self._run("restore_snapshot", op)

    # ------------------------------------------------------------------ #
    # Session replay (structured, replayable action log — not video)
    # ------------------------------------------------------------------ #
    async def startSession(self, name: str | None = None) -> dict[str, Any]:
        """Begin recording every replayable action to a structured session log."""
        return await self._run("start_session", lambda: self._async(self.session.start(name)))

    async def stopSession(self) -> dict[str, Any]:
        """Stop the structured session recording and return its steps."""
        return await self._run("stop_session", lambda: self._async(self.session.stop()))

    async def saveSession(self, path: str | None = None) -> dict[str, Any]:
        """Persist the current session to JSON for later load/replay."""
        return await self._run("save_session", lambda: self._async(self.session.save(path)))

    async def loadSession(self, path: str) -> dict[str, Any]:
        """Load a saved session JSON into memory (ready to replay)."""
        return await self._run("load_session", lambda: self._async(self.session.load(path)))

    async def getSession(self) -> dict[str, Any]:
        """Return the current in-memory session (steps + metadata)."""
        return await self._run("get_session", lambda: self._async(self.session.snapshot()))

    async def replaySession(
        self,
        path: str | None = None,
        delayMs: int = 500,
        continueOnError: bool = True,
    ) -> dict[str, Any]:
        """Re-run a recorded session's steps in order against the live browser.

        Loads *path* first when given, otherwise replays the in-memory session.
        Recording is paused during replay so replayed steps are not re-logged.
        Each step's outcome is reported; with ``continueOnError`` a failed step is
        recorded and replay proceeds, otherwise replay stops at the first failure.

        Unlike other actions this does NOT run under ``_run``'s lock: every step
        calls back into a manager method that acquires the lock itself, and
        ``asyncio.Lock`` is not reentrant — holding it here would deadlock. The
        per-step calls are still individually serialised by that same lock.
        """
        action = "replay_session"
        try:
            if path:
                self.session.load(path)
            steps = list(self.session.steps)
            dispatch = self._replayDispatch()
            results: list[dict[str, Any]] = []
            self.session.paused = True
            try:
                for i, step in enumerate(steps):
                    stepAction = step.get("action")
                    params = step.get("params", {})
                    handler = dispatch.get(stepAction)
                    if handler is None:
                        results.append({"step": i, "action": stepAction, "skipped": True,
                                        "reason": "not a replayable action"})
                        continue
                    envelope = await handler(params)
                    ok = bool(envelope.get("success"))
                    results.append({"step": i, "action": stepAction, "success": ok,
                                    "error": None if ok else envelope.get("error")})
                    if not ok and not continueOnError:
                        break
                    if delayMs:
                        await asyncio.sleep(delayMs / 1000)
            finally:
                self.session.paused = False
            succeeded = sum(1 for r in results if r.get("success"))
            return successResponse(
                action, {"replayed": len(results), "succeeded": succeeded, "results": results}
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as structured error
            logger.exception("Action '%s' failed", action)
            return buildErrorEnvelope(action, exc)

    def _replayDispatch(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
        """Map replayable action names to manager calls (used only by replay)."""
        return {
            "navigate": lambda p: self.navigate(
                p["url"], waitUntil=p.get("waitUntil", "networkidle"), timeoutMs=p.get("timeoutMs")
            ),
            "navigate_back": lambda p: self.navigateBack(),
            "navigate_forward": lambda p: self.navigateForward(),
            "refresh": lambda p: self.refresh(),
            "open_new_tab": lambda p: self.openNewTab(p.get("url")),
            "switch_tab": lambda p: self.switchTab(p["index"]),
            "close_tab": lambda p: self.closeTab(p.get("index")),
            "hover": lambda p: self.hover(p["selector"], timeoutMs=p.get("timeoutMs")),
            "click": lambda p: self.click(
                p["selector"], button=p.get("button", "left"),
                clickCount=p.get("clickCount", 1), timeoutMs=p.get("timeoutMs"),
                humanize=p.get("humanize"),
            ),
            "double_click": lambda p: self.doubleClick(
                p["selector"], timeoutMs=p.get("timeoutMs"), humanize=p.get("humanize")
            ),
            "right_click": lambda p: self.rightClick(
                p["selector"], timeoutMs=p.get("timeoutMs"), humanize=p.get("humanize")
            ),
            "fill": lambda p: self.fill(
                p["selector"], p["value"], clearFirst=p.get("clearFirst", True),
                timeoutMs=p.get("timeoutMs"), humanize=p.get("humanize"),
            ),
            "scroll": lambda p: self.scroll(**p),
            "press_keys": lambda p: self.pressKeys(p["keys"], selector=p.get("selector")),
            "upload_file": lambda p: self.uploadFile(p["selector"], p["filePaths"]),
            "wait_for_element": lambda p: self.waitForElement(
                p["selector"], state=p.get("state", "visible"), timeoutMs=p.get("timeoutMs")
            ),
        }

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    async def status(self) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            snapshot = self.controller._stateSnapshot()  # noqa: SLF001 - internal read
            snapshot["recording"] = self.recorder.isRecording
            snapshot["sessionRecording"] = self.session.recording
            snapshot["sessionSteps"] = len(self.session.steps)
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
