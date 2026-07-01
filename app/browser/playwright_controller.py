"""Async Playwright wrapper around a single browser session.

This class is the low-level engine. It owns one Playwright instance, one
browser, one browser context and a list of pages (tabs). Every method returns
plain Python data (or raises) — the *envelope* shaping lives one layer up in
:class:`app.browser.browser_manager.BrowserManager`, which keeps this class
focused on driving the browser.

All public methods are async and use camelCase. The class is intentionally
single-session; :class:`BrowserManager` is the seam where browser pools and
multi-session support will later plug in.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from app.utils.config import settings
from app.utils.helpers import ensureDir, utcTimestamp
from app.utils.logger import getLogger

logger = getLogger("browser.controller")


class PlaywrightController:
    """Drives a single browser, context and its tabs."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._pages: list[Page] = []
        self._activeIndex: int = 0

        # Recording state is owned here because Playwright ties video capture to
        # the *context*; the VideoRecorder coordinates through these fields.
        self.recordVideoDir: Optional[Path] = None
        self.viewportWidth: int = settings.viewportWidth
        self.viewportHeight: int = settings.viewportHeight

        # Launch options remembered so we can relaunch the SAME persistent
        # profile when toggling headless/headed or (re)starting video recording.
        self.browserType: str = settings.browserType
        self.headless: bool = settings.headless
        self.userAgent: Optional[str] = settings.userAgent
        self.userDataDir: Path = settings.userDataDir

        # Rolling log of network activity, captured at the context level so it
        # spans every tab. Bounded so a long session can't exhaust memory.
        self.networkLog: list[dict[str, Any]] = []
        self.maxNetworkEntries: int = 1000

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def isRunning(self) -> bool:
        return self._context is not None

    @property
    def activePage(self) -> Page:
        if not self._pages:
            raise RuntimeError("No open page. Launch the browser and open a URL first.")
        return self._pages[self._activeIndex]

    async def launchBrowser(
        self,
        browserType: str = "chromium",
        headless: bool = True,
        viewportWidth: int | None = None,
        viewportHeight: int | None = None,
        userAgent: str | None = None,
        recordVideoDir: Path | None = None,
    ) -> dict[str, Any]:
        """Start Playwright and open a PERSISTENT browser profile.

        The session uses ``launch_persistent_context`` against a fixed
        user-data directory, so cookies, tokens and localStorage survive across
        process restarts — e.g. a Gmail login stays logged in next time.
        """
        if self.isRunning:
            logger.warning("launchBrowser called while a browser is already running")
            return self._stateSnapshot()

        self.networkLog.clear()
        self.browserType = browserType
        self.headless = headless
        self.viewportWidth = viewportWidth or settings.viewportWidth
        self.viewportHeight = viewportHeight or settings.viewportHeight
        self.userAgent = userAgent or settings.userAgent
        self.recordVideoDir = recordVideoDir

        self._playwright = await async_playwright().start()
        await self._launchPersistentContext()

        logger.info(
            "Launched %s persistent profile (headless=%s, %dx%d, dir=%s)",
            self.browserType,
            self.headless,
            self.viewportWidth,
            self.viewportHeight,
            self.userDataDir,
        )
        return self._stateSnapshot()

    async def _launchPersistentContext(self) -> BrowserContext:
        """(Re)launch the persistent context with the current launch options.

        Called on initial launch and whenever we must rebuild the context
        (toggling headless/headed, starting/stopping recording). The same
        ``userDataDir`` is reused every time, which is what preserves logins.
        """
        ensureDir(self.userDataDir)
        engine = getattr(self._playwright, self.browserType)

        contextArgs: dict[str, Any] = {
            "headless": self.headless,
            "viewport": {"width": self.viewportWidth, "height": self.viewportHeight},
            "accept_downloads": True,
        }
        if self.userAgent:
            contextArgs["user_agent"] = self.userAgent
        if self.recordVideoDir is not None:
            contextArgs["record_video_dir"] = str(self.recordVideoDir)
            contextArgs["record_video_size"] = {
                "width": self.viewportWidth,
                "height": self.viewportHeight,
            }

        self._context = await engine.launch_persistent_context(
            str(self.userDataDir), **contextArgs
        )
        self._context.set_default_timeout(settings.defaultTimeoutMs)
        self._browser = self._context.browser

        # Capture network activity for the whole context (all tabs).
        self._context.on("response", self._onResponse)
        self._context.on("requestfailed", self._onRequestFailed)

        # A persistent context opens with one page; reuse it (or create one).
        pages = list(self._context.pages)
        if not pages:
            pages = [await self._context.new_page()]
        self._pages = pages
        self._activeIndex = 0
        return self._context

    # ------------------------------------------------------------------ #
    # Network capture
    # ------------------------------------------------------------------ #
    def _recordNetwork(self, entry: dict[str, Any]) -> None:
        """Append one bounded network entry to the rolling log."""
        entry["timestamp"] = utcTimestamp()
        self.networkLog.append(entry)
        overflow = len(self.networkLog) - self.maxNetworkEntries
        if overflow > 0:
            del self.networkLog[:overflow]

    def _onResponse(self, response: Any) -> None:
        """Playwright ``response`` event handler — records completed responses."""
        try:
            request = response.request
            self._recordNetwork(
                {
                    "url": response.url,
                    "method": request.method,
                    "status": response.status,
                    "resourceType": request.resource_type,
                    "ok": response.ok,
                }
            )
        except Exception as exc:  # noqa: BLE001 - never let logging break the page
            logger.debug("Failed to record response: %s", exc)

    def _onRequestFailed(self, request: Any) -> None:
        """Playwright ``requestfailed`` event handler — records failed requests."""
        try:
            self._recordNetwork(
                {
                    "url": request.url,
                    "method": request.method,
                    "status": None,
                    "resourceType": request.resource_type,
                    "ok": False,
                    "failure": getattr(request.failure, "error_text", str(request.failure)),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to record failed request: %s", exc)

    async def getNetwork(self, limit: int = 100, urlContains: str | None = None) -> dict[str, Any]:
        """Return the most recent captured network entries (optionally filtered)."""
        entries = self.networkLog
        if urlContains:
            entries = [e for e in entries if urlContains in e.get("url", "")]
        sliced = entries[-limit:] if limit and limit > 0 else list(entries)
        return {"requests": sliced, "count": len(entries), "returned": len(sliced)}

    async def clearNetwork(self) -> dict[str, Any]:
        """Clear the network log and report how many entries were removed."""
        removed = len(self.networkLog)
        self.networkLog.clear()
        return {"cleared": removed}

    async def closeBrowser(self) -> dict[str, Any]:
        """Tear down the persistent context and Playwright (saving the profile).

        Closing the persistent context flushes cookies/tokens to ``userDataDir``,
        so the next launch restores the logged-in state.
        """
        for closer in (
            self._context.close if self._context else None,
            self._playwright.stop if self._playwright else None,
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.warning("Error during teardown: %s", exc)

        self._playwright = None
        self._browser = None
        self._context = None
        self._pages = []
        self._activeIndex = 0
        logger.info("Browser closed")
        return {"closed": True}

    # ------------------------------------------------------------------ #
    # Video context swapping (used by VideoRecorder)
    # ------------------------------------------------------------------ #
    async def beginVideoContext(self, recordVideoDir: Path) -> None:
        """Relaunch the persistent profile with video recording enabled.

        The same ``userDataDir`` is reused, so logins survive the swap. Open tabs
        collapse to a single page — recording captures one page at a time.
        """
        if self._context is not None:
            await self._context.close()
        self.recordVideoDir = recordVideoDir
        await self._launchPersistentContext()

    async def endVideoContext(self) -> Optional[str]:
        """Close the recording context and return the produced WebM path."""
        if self._context is None:
            return None
        page = self._pages[self._activeIndex] if self._pages else None
        video = page.video if page else None
        await self._context.close()

        videoPath: Optional[str] = None
        if video is not None:
            try:
                videoPath = await video.path()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not resolve video path: %s", exc)

        # Restore a normal (non-recording) persistent context.
        self.recordVideoDir = None
        await self._launchPersistentContext()
        return videoPath

    # ------------------------------------------------------------------ #
    # Headless / headed switching & profile reset
    # ------------------------------------------------------------------ #
    async def setHeadless(self, headless: bool) -> dict[str, Any]:
        """Switch a running browser between headless and headed without losing state.

        Useful when a page needs a human (captcha / "are you human" / a manual
        login): flip to headed so the window is visible, the human acts, then flip
        back. The persistent profile means any login performed survives.
        """
        if not self.isRunning:
            self.headless = headless
            return {"headless": headless, "running": False,
                    "note": "Applied on next open_browser."}
        if headless == self.headless:
            return {"headless": headless, "changed": False}

        currentUrl = self._pages[self._activeIndex].url if self._pages else None
        await self._context.close()
        self.headless = headless
        await self._launchPersistentContext()
        if currentUrl and currentUrl != "about:blank":
            await self.openUrl(currentUrl, waitUntil="domcontentloaded")
        return {"headless": headless, "changed": True, "url": currentUrl}

    async def clearProfile(self) -> dict[str, Any]:
        """Delete the persistent profile (logs out of everything, fresh session).

        Closes the browser first if running, then wipes ``userDataDir``.
        """
        wasRunning = self.isRunning
        if wasRunning:
            await self.closeBrowser()
        if Path(self.userDataDir).exists():
            shutil.rmtree(self.userDataDir, ignore_errors=True)
        ensureDir(self.userDataDir)
        return {"profileCleared": True, "path": str(self.userDataDir), "wasRunning": wasRunning}

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def openUrl(
        self,
        url: str,
        waitUntil: str = "networkidle",
        timeoutMs: int | None = None,
    ) -> dict[str, Any]:
        page = self.activePage
        response = await page.goto(
            url, wait_until=waitUntil, timeout=timeoutMs or settings.defaultTimeoutMs
        )
        status = response.status if response else None
        logger.info("Navigated to %s (status=%s)", url, status)
        return {"url": page.url, "title": await page.title(), "status": status}

    async def navigateBack(self) -> dict[str, Any]:
        page = self.activePage
        await page.go_back(wait_until="networkidle")
        return {"url": page.url, "title": await page.title()}

    async def navigateForward(self) -> dict[str, Any]:
        page = self.activePage
        await page.go_forward(wait_until="networkidle")
        return {"url": page.url, "title": await page.title()}

    async def refreshPage(self) -> dict[str, Any]:
        page = self.activePage
        await page.reload(wait_until="networkidle")
        return {"url": page.url, "title": await page.title()}

    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #
    async def openNewTab(self, url: str | None = None) -> dict[str, Any]:
        page = await self._context.new_page()  # type: ignore[union-attr]
        self._pages.append(page)
        self._activeIndex = len(self._pages) - 1
        if url:
            await page.goto(url, wait_until="networkidle")
        return self._stateSnapshot()

    async def switchTab(self, index: int) -> dict[str, Any]:
        if not 0 <= index < len(self._pages):
            raise IndexError(f"Tab index {index} out of range (0..{len(self._pages) - 1})")
        self._activeIndex = index
        await self._pages[index].bring_to_front()
        return self._stateSnapshot()

    async def closeTab(self, index: int | None = None) -> dict[str, Any]:
        target = self._activeIndex if index is None else index
        if not 0 <= target < len(self._pages):
            raise IndexError(f"Tab index {target} out of range")
        await self._pages[target].close()
        del self._pages[target]
        if not self._pages:
            self._activeIndex = 0
        else:
            self._activeIndex = min(self._activeIndex, len(self._pages) - 1)
        return self._stateSnapshot()

    # ------------------------------------------------------------------ #
    # Page info & extraction
    # ------------------------------------------------------------------ #
    async def getTitle(self) -> dict[str, Any]:
        return {"title": await self.activePage.title()}

    async def getUrl(self) -> dict[str, Any]:
        return {"url": self.activePage.url}

    async def extractText(self) -> dict[str, Any]:
        text = await self.activePage.evaluate("() => document.body ? document.body.innerText : ''")
        return {"text": text, "length": len(text)}

    async def extractLinks(self) -> dict[str, Any]:
        links = await self.activePage.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: (a.innerText || '').trim().slice(0, 200),
                href: a.href,
                title: a.title || null
            }))"""
        )
        return {"links": links, "count": len(links)}

    async def extractButtons(self) -> dict[str, Any]:
        buttons = await self.activePage.evaluate(
            """() => Array.from(document.querySelectorAll(
                "button, input[type=button], input[type=submit], [role=button]"
            )).map(b => ({
                text: (b.innerText || b.value || '').trim().slice(0, 200),
                type: b.getAttribute('type'),
                id: b.id || null,
                name: b.getAttribute('name'),
                disabled: b.disabled === true
            }))"""
        )
        return {"buttons": buttons, "count": len(buttons)}

    async def extractForms(self) -> dict[str, Any]:
        forms = await self.activePage.evaluate(
            """() => Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action || null,
                method: (f.method || 'get').toLowerCase(),
                id: f.id || null,
                name: f.getAttribute('name'),
                fields: Array.from(f.querySelectorAll('input, select, textarea')).map(i => ({
                    tag: i.tagName.toLowerCase(),
                    type: i.getAttribute('type'),
                    name: i.getAttribute('name'),
                    id: i.id || null,
                    placeholder: i.getAttribute('placeholder'),
                    required: i.required === true
                }))
            }))"""
        )
        return {"forms": forms, "count": len(forms)}

    async def extractImages(self) -> dict[str, Any]:
        images = await self.activePage.evaluate(
            """() => Array.from(document.querySelectorAll('img')).map(i => ({
                src: i.src,
                alt: i.alt || null,
                width: i.naturalWidth,
                height: i.naturalHeight
            }))"""
        )
        return {"images": images, "count": len(images)}

    async def getDom(self, selector: str | None = None) -> dict[str, Any]:
        if selector:
            element = await self.activePage.query_selector(selector)
            if element is None:
                raise ValueError(f"Selector not found: {selector}")
            html = await element.evaluate("el => el.outerHTML")
        else:
            html = await self.activePage.content()
        return {"html": html, "length": len(html), "selector": selector}

    # ------------------------------------------------------------------ #
    # Interaction
    # ------------------------------------------------------------------ #
    async def scrollPage(
        self,
        deltaX: int = 0,
        deltaY: int = 0,
        selector: str | None = None,
        toTop: bool = False,
        toBottom: bool = False,
    ) -> dict[str, Any]:
        page = self.activePage
        if selector:
            await page.locator(selector).scroll_into_view_if_needed()
        elif toTop:
            await page.evaluate("() => window.scrollTo(0, 0)")
        elif toBottom:
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        else:
            await page.mouse.wheel(deltaX, deltaY)
        position = await page.evaluate("() => ({ x: window.scrollX, y: window.scrollY })")
        return {"scroll": position}

    async def hoverElement(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        await self.activePage.hover(selector, timeout=timeoutMs or settings.defaultTimeoutMs)
        return {"hovered": selector}

    async def clickElement(
        self,
        selector: str,
        button: str = "left",
        clickCount: int = 1,
        timeoutMs: int | None = None,
    ) -> dict[str, Any]:
        await self.activePage.click(
            selector,
            button=button,  # type: ignore[arg-type]
            click_count=clickCount,
            timeout=timeoutMs or settings.defaultTimeoutMs,
        )
        return {"clicked": selector, "button": button, "clickCount": clickCount}

    async def doubleClickElement(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        await self.activePage.dblclick(selector, timeout=timeoutMs or settings.defaultTimeoutMs)
        return {"doubleClicked": selector}

    async def rightClickElement(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        await self.activePage.click(
            selector, button="right", timeout=timeoutMs or settings.defaultTimeoutMs
        )
        return {"rightClicked": selector}

    async def fillInput(
        self,
        selector: str,
        value: str,
        clearFirst: bool = True,
        timeoutMs: int | None = None,
    ) -> dict[str, Any]:
        page = self.activePage
        timeout = timeoutMs or settings.defaultTimeoutMs
        if clearFirst:
            await page.fill(selector, value, timeout=timeout)
        else:
            await page.click(selector, timeout=timeout)
            await page.type(selector, value)
        return {"filled": selector, "value": value}

    async def selectOption(
        self,
        selector: str,
        value: str | None = None,
        label: str | None = None,
        timeoutMs: int | None = None,
    ) -> dict[str, Any]:
        page = self.activePage
        timeout = timeoutMs or settings.defaultTimeoutMs
        if label is not None:
            chosen = await page.select_option(selector, label=label, timeout=timeout)
        elif value is not None:
            chosen = await page.select_option(selector, value=value, timeout=timeout)
        else:
            raise ValueError("selectOption requires 'value' or 'label'")
        return {"selected": chosen, "selector": selector}

    async def uploadFile(self, selector: str, filePaths: list[str]) -> dict[str, Any]:
        missing = [p for p in filePaths if not Path(p).exists()]
        if missing:
            raise FileNotFoundError(f"Files not found: {missing}")
        await self.activePage.set_input_files(selector, filePaths)
        return {"uploaded": filePaths, "selector": selector}

    async def downloadFile(
        self,
        selector: str,
        saveDir: str | None = None,
        timeoutMs: int | None = None,
    ) -> dict[str, Any]:
        page = self.activePage
        timeout = timeoutMs or settings.defaultTimeoutMs
        async with page.expect_download(timeout=timeout) as downloadInfo:
            await page.click(selector, timeout=timeout)
        download = await downloadInfo.value
        targetDir = Path(saveDir) if saveDir else settings.storageDir / "downloads"
        targetDir.mkdir(parents=True, exist_ok=True)
        destination = targetDir / download.suggested_filename
        await download.save_as(str(destination))
        return {"path": str(destination), "suggestedFilename": download.suggested_filename}

    async def pressKeys(self, keys: str, selector: str | None = None) -> dict[str, Any]:
        if selector:
            await self.activePage.press(selector, keys)
        else:
            await self.activePage.keyboard.press(keys)
        return {"pressed": keys, "selector": selector}

    # ------------------------------------------------------------------ #
    # Waits
    # ------------------------------------------------------------------ #
    async def waitForElement(
        self,
        selector: str,
        state: str = "visible",
        timeoutMs: int | None = None,
    ) -> dict[str, Any]:
        await self.activePage.wait_for_selector(
            selector, state=state, timeout=timeoutMs or settings.defaultTimeoutMs  # type: ignore[arg-type]
        )
        return {"selector": selector, "state": state}

    async def waitForNetworkIdle(self, timeoutMs: int | None = None) -> dict[str, Any]:
        await self.activePage.wait_for_load_state(
            "networkidle", timeout=timeoutMs or settings.defaultTimeoutMs
        )
        return {"networkIdle": True}

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _stateSnapshot(self) -> dict[str, Any]:
        page = self._pages[self._activeIndex] if self._pages else None
        return {
            "running": self.isRunning,
            "headless": self.headless,
            "browserType": self.browserType,
            "persistent": True,
            "userDataDir": str(self.userDataDir),
            "tabIndex": self._activeIndex,
            "tabCount": len(self._pages),
            "url": page.url if page else None,
        }
