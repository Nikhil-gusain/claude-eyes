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

import re
import shutil
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from app.browser import humanize as humanizer
from app.browser import stealth
from app.browser.media import verifyImage
from app.utils.config import settings
from app.utils.helpers import ensureDir, utcTimestamp
from app.utils.logger import getLogger

logger = getLogger("browser.controller")


def urlMatches(pattern: str, url: str, includeQuery: bool = False) -> bool:
    """Whether *url* matches *pattern* — by scheme+host+path, query excluded by default.

    ``wait_for_response`` used a plain substring over the *whole* URL, which let a
    pattern like ``"duckchat"`` latch onto an unrelated request that merely had
    ``cc=duckchat`` in its query string (e.g. a bot-detection beacon). Matching
    against the path (no query/fragment) by default fixes that. *pattern* is tried
    as a regex first (so callers can be precise, e.g. ``r"/backend-api/conversation$"``)
    and falls back to a plain substring if it is not valid regex. Set
    ``includeQuery=True`` to match against the full URL including the query.
    """
    if includeQuery:
        target = url
    else:
        parts = urlsplit(url)
        target = f"{parts.scheme}://{parts.netloc}{parts.path}"
    try:
        if re.search(pattern, target):
            return True
    except re.error:
        pass
    return pattern in target


# Dump localStorage + sessionStorage of the current origin into plain objects.
_STORAGE_DUMP_JS = """() => {
    const dump = (s) => { const o = {}; for (let i = 0; i < s.length; i++) {
        const k = s.key(i); o[k] = s.getItem(k); } return o; };
    let local = {}, session = {};
    try { local = dump(localStorage); } catch (e) {}
    try { session = dump(sessionStorage); } catch (e) {}
    return { local, session, origin: location.origin };
}"""

# Write storage key/values back (must run on the matching origin).
_STORAGE_RESTORE_JS = """(data) => {
    try { for (const [k, v] of Object.entries(data.local || {})) localStorage.setItem(k, v); } catch (e) {}
    try { for (const [k, v] of Object.entries(data.session || {})) sessionStorage.setItem(k, v); } catch (e) {}
    return true;
}"""

# Visual-quality audit: overflow, hidden interactive elements, broken images,
# and approximate WCAG text/background contrast. Returns counts + bounded samples.
_AUDIT_JS = """(limit) => {
    const out = { overflowIssues: [], hiddenButtons: [], brokenImages: [], contrastProblems: [] };
    const CAP = 25;
    const vw = document.documentElement.clientWidth;

    if (document.documentElement.scrollWidth > vw + 1) {
        for (const el of document.querySelectorAll('body *')) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > vw + 2) {
                out.overflowIssues.push({ tag: el.tagName.toLowerCase(), id: el.id || null,
                    overflowBy: Math.round(r.right - vw) });
                if (out.overflowIssues.length >= CAP) break;
            }
        }
    }

    for (const el of document.querySelectorAll(
        'button, a[href], input[type=button], input[type=submit], [role=button]')) {
        const st = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        const hidden = st.display === 'none' || st.visibility === 'hidden' ||
            parseFloat(st.opacity) === 0 || (r.width === 0 && r.height === 0);
        if (hidden) {
            out.hiddenButtons.push({ tag: el.tagName.toLowerCase(),
                text: (el.innerText || el.value || '').trim().slice(0, 80), id: el.id || null });
            if (out.hiddenButtons.length >= CAP) break;
        }
    }

    for (const img of document.querySelectorAll('img')) {
        if (img.complete && img.naturalWidth === 0) {
            out.brokenImages.push({ src: img.src, alt: img.alt || null });
            if (out.brokenImages.length >= CAP) break;
        }
    }

    const parseRGB = (s) => {
        const m = s && s.match(/rgba?\\(([^)]+)\\)/);
        if (!m) return null;
        const p = m[1].split(',').map((x) => parseFloat(x.trim()));
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    };
    const lum = (c) => {
        const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
        return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    };
    const bgOf = (el) => {
        let n = el;
        while (n && n !== document.documentElement) {
            const bg = parseRGB(getComputedStyle(n).backgroundColor);
            if (bg && bg.a !== 0) return bg;
            n = n.parentElement;
        }
        return { r: 255, g: 255, b: 255, a: 1 };
    };
    let checked = 0;
    const els = Array.from(document.querySelectorAll(
        'p, span, a, li, h1, h2, h3, h4, h5, h6, button, label, td, th')).slice(0, limit);
    for (const el of els) {
        const txt = (el.textContent || '').trim();
        if (!txt) continue;
        const st = getComputedStyle(el);
        if (st.visibility === 'hidden' || st.display === 'none') continue;
        const fg = parseRGB(st.color);
        if (!fg) continue;
        const ratio = (() => {
            const l1 = lum(fg), l2 = lum(bgOf(el));
            return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        })();
        const size = parseFloat(st.fontSize);
        const bold = (parseInt(st.fontWeight) || 400) >= 700;
        const large = size >= 24 || (size >= 18.66 && bold);
        const need = large ? 3 : 4.5;
        checked++;
        if (ratio < need) {
            out.contrastProblems.push({ text: txt.slice(0, 60),
                ratio: Math.round(ratio * 100) / 100, required: need, fontSize: size });
            if (out.contrastProblems.length >= CAP) break;
        }
    }

    return {
        overflowIssues: out.overflowIssues.length,
        hiddenButtons: out.hiddenButtons.length,
        brokenImages: out.brokenImages.length,
        contrastProblems: out.contrastProblems.length,
        textElementsChecked: checked,
        details: out,
    };
}"""


# Collect visible interactive elements, each with a unique-ish CSS selector and
# the cues an LLM needs to match a description ("blue login button").
_CANDIDATES_JS = """(limit) => {
    const cssPath = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1 && node !== document.body) {
            let sel = node.tagName.toLowerCase();
            const parent = node.parentElement;
            if (parent) {
                const sibs = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
                if (sibs.length > 1) sel += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
            }
            parts.unshift(sel);
            node = node.parentElement;
        }
        return parts.join(' > ');
    };
    const els = Array.from(document.querySelectorAll(
        'a[href], button, input, select, textarea, [role=button], [role=link], [role=tab], [onclick]'));
    const out = [];
    for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) continue;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        out.push({
            index: out.length,
            selector: cssPath(el),
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute('type'),
            text: (el.innerText || el.value || el.getAttribute('placeholder') || '').trim().slice(0, 80),
            role: el.getAttribute('role'),
            ariaLabel: el.getAttribute('aria-label'),
            placeholder: el.getAttribute('placeholder'),
            color: st.color,
            background: st.backgroundColor,
        });
        if (out.length >= limit) break;
    }
    return out;
}"""


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
        self.browserChannel: Optional[str] = settings.browserChannel
        self.headless: bool = settings.headless
        self.userAgent: Optional[str] = settings.userAgent
        self.userDataDir: Path = settings.userDataDir
        # Name of the active named profile backing ``userDataDir`` (if any).
        self.profileName: Optional[str] = None

        # Last known mouse position, so human-like moves curve from where the
        # cursor actually was rather than always starting at the origin.
        self._cursor: tuple[float, float] = (
            settings.viewportWidth / 2,
            settings.viewportHeight / 2,
        )

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
        userDataDir: Path | None = None,
        profileName: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Start Playwright and open a PERSISTENT browser profile.

        The session uses ``launch_persistent_context`` against a fixed
        user-data directory, so cookies, tokens and localStorage survive across
        process restarts — e.g. a Gmail login stays logged in next time.

        ``userDataDir``/``profileName`` select which named profile to drive (the
        :class:`~app.browser.profiles.ProfileManager` resolves these); omitting
        them keeps the legacy single-profile directory. ``channel`` (e.g.
        ``"chrome"``) drives a real installed browser instead of bundled
        Chromium — harder for bot-detection to flag.
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
        if userDataDir is not None:
            self.userDataDir = Path(userDataDir)
        if profileName is not None:
            self.profileName = profileName
        if channel is not None:
            self.browserChannel = channel
        self._cursor = (self.viewportWidth / 2, self.viewportHeight / 2)

        self._playwright = await async_playwright().start()
        await self._launchPersistentContext()

        logger.info(
            "Launched %s persistent profile (headless=%s, %dx%d, profile=%s, dir=%s)",
            self.browserType,
            self.headless,
            self.viewportWidth,
            self.viewportHeight,
            self.profileName or "<default>",
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
        if self.browserChannel:
            contextArgs["channel"] = self.browserChannel
        if self.userAgent:
            contextArgs["user_agent"] = self.userAgent
        # Stealth: strip the automation launch flags (Chromium-family only).
        if settings.stealth and self.browserType == "chromium":
            contextArgs["args"] = stealth.launchArgs()
            contextArgs["ignore_default_args"] = stealth.ignoreDefaultArgs()
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

        # Stealth: patch the common JS fingerprints (navigator.webdriver, etc.)
        # before any page script runs. Applies to every current and future page.
        if settings.stealth and self.browserType == "chromium":
            try:
                await self._context.add_init_script(stealth.STEALTH_INIT_JS)
            except Exception as exc:  # noqa: BLE001 - stealth is best-effort
                logger.debug("Failed to install stealth init script: %s", exc)

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
        humanize: bool | None = None,
    ) -> dict[str, Any]:
        page = self.activePage
        human = humanizer.shouldHumanize(humanize)
        if selector:
            if human:
                await humanizer.humanScrollToSelector(page, selector, settings.defaultTimeoutMs)
            else:
                await page.locator(selector).scroll_into_view_if_needed()
        elif toTop:
            # Lazy "scroll up to discover" rather than snapping to the very top.
            if human:
                await humanizer.humanScrollBy(page, -10_000_000)
            else:
                await page.evaluate("() => window.scrollTo(0, 0)")
        elif toBottom:
            if human:
                await humanizer.humanScrollBy(page, 10_000_000)
            else:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        elif human and (deltaY or deltaX):
            await humanizer.humanScrollBy(page, deltaY, deltaX)
        else:
            await page.mouse.wheel(deltaX, deltaY)
        position = await page.evaluate("() => ({ x: window.scrollX, y: window.scrollY })")
        return {"scroll": position, "humanized": human}

    async def hoverElement(self, selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
        await self.activePage.hover(selector, timeout=timeoutMs or settings.defaultTimeoutMs)
        return {"hovered": selector}

    async def clickElement(
        self,
        selector: str,
        button: str = "left",
        clickCount: int = 1,
        timeoutMs: int | None = None,
        humanize: bool | None = None,
    ) -> dict[str, Any]:
        timeout = timeoutMs or settings.defaultTimeoutMs
        human = humanizer.shouldHumanize(humanize)
        if human:
            self._cursor = await humanizer.humanClickSelector(
                self.activePage, selector, self._cursor,
                button=button, clickCount=clickCount, timeoutMs=timeout,
            )
        else:
            await self.activePage.click(
                selector,
                button=button,  # type: ignore[arg-type]
                click_count=clickCount,
                timeout=timeout,
            )
        return {"clicked": selector, "button": button, "clickCount": clickCount, "humanized": human}

    async def doubleClickElement(
        self, selector: str, timeoutMs: int | None = None, humanize: bool | None = None
    ) -> dict[str, Any]:
        timeout = timeoutMs or settings.defaultTimeoutMs
        human = humanizer.shouldHumanize(humanize)
        if human:
            self._cursor = await humanizer.humanClickSelector(
                self.activePage, selector, self._cursor, clickCount=2, timeoutMs=timeout
            )
        else:
            await self.activePage.dblclick(selector, timeout=timeout)
        return {"doubleClicked": selector, "humanized": human}

    async def rightClickElement(
        self, selector: str, timeoutMs: int | None = None, humanize: bool | None = None
    ) -> dict[str, Any]:
        timeout = timeoutMs or settings.defaultTimeoutMs
        human = humanizer.shouldHumanize(humanize)
        if human:
            self._cursor = await humanizer.humanClickSelector(
                self.activePage, selector, self._cursor, button="right", timeoutMs=timeout
            )
        else:
            await self.activePage.click(selector, button="right", timeout=timeout)
        return {"rightClicked": selector, "humanized": human}

    async def fillInput(
        self,
        selector: str,
        value: str,
        clearFirst: bool = True,
        timeoutMs: int | None = None,
        humanize: bool | None = None,
    ) -> dict[str, Any]:
        page = self.activePage
        timeout = timeoutMs or settings.defaultTimeoutMs
        human = humanizer.shouldHumanize(humanize)
        if human:
            # Move the cursor to the field and click to focus (like a person),
            # optionally select-all + delete to clear, then type at human speed.
            self._cursor = await humanizer.humanClickSelector(
                page, selector, self._cursor, timeoutMs=timeout
            )
            if clearFirst:
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.press("Delete")
            await humanizer.humanType(page, value)
        elif clearFirst:
            await page.fill(selector, value, timeout=timeout)
        else:
            await page.click(selector, timeout=timeout)
            await page.type(selector, value)
        return {"filled": selector, "value": value, "humanized": human}

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
        imagesOnly: bool = True,
    ) -> dict[str, Any]:
        """Trigger a download via *selector* and save it, safely.

        Downloads can legitimately take a long time, so the wait ceiling defaults
        to ``settings.maxDownloadWaitMs`` (an hour) rather than the short action
        timeout. By default only **real images** are kept: the saved file's true
        bytes are inspected and anything that is actually an executable/app/archive
        disguised as an image (or any non-image) is deleted and reported as an
        error — guarding against "download this image" lures that ship malware.
        """
        page = self.activePage
        timeout = timeoutMs or settings.maxDownloadWaitMs
        async with page.expect_download(timeout=timeout) as downloadInfo:
            await page.click(selector, timeout=settings.defaultTimeoutMs)
        download = await downloadInfo.value
        targetDir = Path(saveDir) if saveDir else settings.storageDir / "downloads"
        targetDir.mkdir(parents=True, exist_ok=True)
        destination = targetDir / download.suggested_filename
        await download.save_as(str(destination))

        if imagesOnly:
            verdict = verifyImage(destination)
            if not verdict["isImage"]:
                try:
                    destination.unlink()
                except OSError:
                    pass
                raise ValueError(
                    f"Refused download '{download.suggested_filename}': "
                    f"{verdict['reason']} (only verified images are allowed)."
                )
            return {
                "path": str(destination),
                "suggestedFilename": download.suggested_filename,
                "verifiedImage": True,
                "format": verdict.get("format"),
            }
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

    async def waitForStable(
        self,
        selector: str,
        stableMs: int = 1200,
        timeoutMs: int | None = None,
        pollMs: int = 300,
    ) -> dict[str, Any]:
        """Wait until *selector*'s text stops changing for ``stableMs``.

        This is the quiet way to wait for an online AI's streamed answer: the
        response text grows token by token, then stops. We poll the element's
        text length in-page and resolve once it has been unchanged for a stable
        window — no fixed sleeps, no guessing how long generation takes. Capped by
        ``settings.maxWaitMs`` (5 min) by default.
        """
        timeout = timeoutMs if timeoutMs is not None else settings.maxWaitMs
        page = self.activePage
        await page.locator(selector).first.wait_for(state="attached", timeout=timeout)
        # The in-page poller resolves true once length is unchanged for stableMs.
        result = await page.wait_for_function(
            """([sel, stable, poll]) => new Promise((resolve) => {
                const read = () => {
                    const el = document.querySelector(sel);
                    return el ? (el.innerText || el.textContent || '').length : -1;
                };
                let last = read();
                let still = 0;
                const id = setInterval(() => {
                    const now = read();
                    if (now === last) { still += poll; } else { still = 0; last = now; }
                    if (still >= stable) { clearInterval(id); resolve(now); }
                }, poll);
            })""",
            arg=[selector, stableMs, pollMs],
            timeout=timeout,
        )
        finalLength = await result.json_value()
        text = await page.evaluate(
            """(sel) => { const el = document.querySelector(sel);
                return el ? (el.innerText || el.textContent || '') : ''; }""",
            selector,
        )
        return {"selector": selector, "stable": True, "length": finalLength, "text": text}

    async def waitForResponse(
        self,
        urlPattern: str,
        timeoutMs: int | None = None,
        includeQuery: bool = False,
    ) -> dict[str, Any]:
        """Wait until a network response matching *urlPattern* finishes.

        Reads straight from the network layer (the same stream the Network tab
        shows): resolves when the matching request *finishes* — i.e. its
        streaming/SSE body has fully closed — which is the moment an online AI has
        finished answering. Capped by ``settings.maxWaitMs`` (5 min) by default.

        *urlPattern* is matched by :func:`urlMatches` — regex-or-substring against
        the URL's scheme+host+path, **excluding the query string** so a loose
        pattern can't latch onto a query parameter of an unrelated request (e.g. a
        bot-detection beacon). Pass ``includeQuery=True`` to match the full URL.
        """
        timeout = timeoutMs if timeoutMs is not None else settings.maxWaitMs
        page = self.activePage
        response = await page.wait_for_event(
            "response",
            predicate=lambda r: urlMatches(urlPattern, r.url, includeQuery),
            timeout=timeout,
        )
        # Await full completion of the (possibly streamed) body before returning.
        finished = True
        try:
            await response.finished()
        except Exception:  # noqa: BLE001 - body may already be consumed/closed
            finished = False
        return {
            "url": response.url,
            "status": response.status,
            "ok": response.ok,
            "finished": finished,
        }

    # ------------------------------------------------------------------ #
    # Accessibility / tab intelligence / page audit
    # ------------------------------------------------------------------ #
    async def getAccessibilityTree(
        self, interestingOnly: bool = True, root: str | None = None
    ) -> dict[str, Any]:
        """Return the page's accessibility tree (how a screen reader sees it).

        Agents often understand a page better through its accessibility tree —
        roles, names and structure — than through raw HTML. Backed by Playwright's
        ``aria_snapshot`` (the successor to the removed ``page.accessibility`` API),
        which returns a compact YAML tree of roles/names and is inherently
        interesting-only. ``root`` scopes the snapshot to one element's subtree.
        """
        page = self.activePage
        if root:
            handle = await page.query_selector(root)
            if handle is None:
                raise ValueError(f"Selector not found: {root}")
        locator = page.locator(root) if root else page.locator("body")
        tree = await locator.first.aria_snapshot()
        nodeCount = sum(1 for line in tree.splitlines() if line.strip().startswith("-"))
        return {
            "tree": tree,
            "format": "aria-yaml",
            "nodeCount": nodeCount,
            "root": root,
            "interestingOnly": True,
        }

    async def getTabs(self) -> dict[str, Any]:
        """Summarise every open tab (index, title, URL, host, which is active).

        Large agents lose track of tabs; this is the one call that says what each
        open tab actually is.
        """
        tabs: list[dict[str, Any]] = []
        for index, page in enumerate(self._pages):
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001 - a closing/blank tab must not break the list
                title = None
            url = page.url
            tabs.append(
                {
                    "index": index,
                    "title": title,
                    "url": url,
                    "host": urlsplit(url).netloc or None,
                    "active": index == self._activeIndex,
                }
            )
        return {"tabs": tabs, "count": len(tabs), "activeIndex": self._activeIndex}

    async def auditPage(self, sampleLimit: int = 400) -> dict[str, Any]:
        """Run a visual-quality audit of the current page (no screenshot needed).

        Counts (and samples) four common UI defects coding agents care about:
        horizontal overflow, interactive elements that are hidden, broken images,
        and low text/background contrast (approximate WCAG ratio). ``sampleLimit``
        bounds how many text elements the contrast pass inspects.
        """
        return await self.activePage.evaluate(_AUDIT_JS, sampleLimit)

    async def getInteractiveCandidates(self, limit: int = 60) -> dict[str, Any]:
        """List visible interactive elements with a computed CSS selector each.

        Powers natural-language element finding: each candidate carries its tag,
        visible text, role/aria-label, placeholder and computed colours plus a
        unique ``selector`` an agent (or the AI finder) can act on directly.
        """
        candidates = await self.activePage.evaluate(_CANDIDATES_JS, limit)
        return {"candidates": candidates, "count": len(candidates)}

    # ------------------------------------------------------------------ #
    # Browser-state snapshot (cookies + storage + open tabs)
    # ------------------------------------------------------------------ #
    async def createSnapshot(self) -> dict[str, Any]:
        """Capture cookies, localStorage, sessionStorage and open-tab URLs."""
        cookies = await self._context.cookies()  # type: ignore[union-attr]
        page = self.activePage
        storage = await page.evaluate(_STORAGE_DUMP_JS)
        return {
            "cookies": cookies,
            "localStorage": storage.get("local", {}),
            "sessionStorage": storage.get("session", {}),
            "origin": storage.get("origin"),
            "tabs": [p.url for p in self._pages],
            "activeIndex": self._activeIndex,
            "url": page.url,
            "capturedAt": utcTimestamp(),
        }

    async def restoreSnapshot(self, snapshot: dict[str, Any], navigate: bool = True) -> dict[str, Any]:
        """Restore cookies + storage from *snapshot* (re-opening its URL first).

        ``localStorage``/``sessionStorage`` are per-origin, so we navigate the
        active tab to the snapshot's URL before writing them back. Cookies are
        added context-wide regardless.
        """
        restored: dict[str, Any] = {
            "cookies": 0, "localStorage": 0, "sessionStorage": 0, "navigated": False,
        }
        cookies = snapshot.get("cookies") or []
        if cookies:
            await self._context.add_cookies(cookies)  # type: ignore[union-attr]
            restored["cookies"] = len(cookies)

        url = snapshot.get("url") or next(iter(snapshot.get("tabs") or []), None)
        page = self.activePage
        if navigate and url and url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded")
            restored["navigated"] = True

        local = snapshot.get("localStorage") or {}
        session = snapshot.get("sessionStorage") or {}
        if local or session:
            await page.evaluate(_STORAGE_RESTORE_JS, {"local": local, "session": session})
            restored["localStorage"] = len(local)
            restored["sessionStorage"] = len(session)
        restored["url"] = url
        return restored

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _stateSnapshot(self) -> dict[str, Any]:
        page = self._pages[self._activeIndex] if self._pages else None
        return {
            "running": self.isRunning,
            "headless": self.headless,
            "browserType": self.browserType,
            "browserChannel": self.browserChannel,
            "persistent": True,
            "profileName": self.profileName,
            "userDataDir": str(self.userDataDir),
            "humanize": settings.humanize,
            "stealth": settings.stealth,
            "tabIndex": self._activeIndex,
            "tabCount": len(self._pages),
            "url": page.url if page else None,
        }
