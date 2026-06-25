"""Model Context Protocol (MCP) server exposing browser tools to AI agents.

This module wires the project's :class:`BrowserManager` to the MCP protocol via
FastMCP. Each browser action is published as an MCP *tool* whose name is the
snake_case identifier from the public spec (e.g. ``open_browser``), while the
backing Python function keeps the project-mandated camelCase identifier.

Every tool simply awaits the corresponding ``BrowserManager`` coroutine and
returns its structured envelope dict unchanged. The manager already wraps each
result in the AI-friendly ``{"success": ...}`` shape and serialises browser
access behind a lock, so the tools here stay thin pass-throughs.

The module-level :data:`mcp` object is importable as
``from app.mcp.mcp_server import mcp`` for embedding/testing, and :func:`main`
runs the server over stdio for use as a standalone MCP server process.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from app.browser.browser_manager import getBrowserManager
from app.utils.config import settings
from app.utils.error_handler import safeAsync
from app.utils.logger import getLogger

logger = getLogger("mcp.server")

mcp = FastMCP("ai-browser-controller")


# --------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------- #
@mcp.tool(name="open_browser")
@safeAsync(action="open_browser")
async def openBrowser(
    profile: str | None = None,
    headless: bool | None = None,
    browserType: str = "chromium",
    viewportWidth: int | None = None,
    viewportHeight: int | None = None,
    userAgent: str | None = None,
) -> dict[str, Any]:
    """Launch a PERSISTENT browser profile — headless by default, headed on demand.

    PROFILE SELECTION (important): a profile is one Chrome "user" with its own
    logins/cookies. If you do NOT pass ``profile`` and no profile is active yet,
    this returns ``status: "profile_selection_required"`` with the list of
    available profiles instead of launching. When that happens you MUST ask the
    user which profile to use (or to pick one at random):
    - If you have a way to ask the user (e.g. an AskUserQuestion tool), use it.
    - Otherwise, present the returned profile names plus "random" / "create a new
      one" to the user and STOP, waiting for their reply.
    Then call ``open_browser`` again with ``profile=<name>`` or ``profile="random"``
    (or call ``select_profile`` first). The chosen profile is remembered on disk,
    so later chats reuse the SAME profile automatically without re-asking.

    Logins (Gmail, etc.) PERSIST across runs within a profile. Use
    ``login_session`` for first-time manual logins/sign-ups.

    Args:
        profile: Profile name to use, or ``"random"`` to pick one. Omit to use the
            remembered active profile (or trigger selection if none is set yet).
        headless: ``False`` for a visible window (human/manual steps), ``True`` for
            none. Omit for the server default (``ABC_HEADLESS``).
        browserType: ``chromium`` (default), ``firefox``, or ``webkit``.
        viewportWidth / viewportHeight: optional window size in pixels.
        userAgent: optional custom User-Agent string.
    """
    kwargs: dict[str, Any] = {"browserType": browserType}
    if profile is not None:
        kwargs["profile"] = profile
    if headless is not None:
        kwargs["headless"] = headless
    if viewportWidth:
        kwargs["viewportWidth"] = viewportWidth
    if viewportHeight:
        kwargs["viewportHeight"] = viewportHeight
    if userAgent:
        kwargs["userAgent"] = userAgent
    return await getBrowserManager().openBrowser(**kwargs)


# --------------------------------------------------------------------- #
# Profiles (multi-account; the active one persists across chats)
# --------------------------------------------------------------------- #
@mcp.tool(name="list_profiles")
@safeAsync(action="list_profiles")
async def listProfiles() -> dict[str, Any]:
    """List the available browser profiles and which one is currently active.

    Use this to show the user their choices before opening a browser. Each
    profile is an isolated Chrome "user" with its own logins/cookies.
    """
    return await getBrowserManager().listProfiles()


@mcp.tool(name="select_profile")
@safeAsync(action="select_profile")
async def selectProfile(name: str) -> dict[str, Any]:
    """Set the active browser profile (remembered across chats).

    Args:
        name: The profile to activate, or ``"random"`` to pick an existing one.
            Creates the profile directory if it does not exist yet. After this,
            ``open_browser``/``navigate`` use this profile until changed.
    """
    return await getBrowserManager().selectProfile(name)


@mcp.tool(name="create_profile")
@safeAsync(action="create_profile")
async def createProfile(name: str, makeActive: bool = True) -> dict[str, Any]:
    """Create a new, empty browser profile (a fresh Chrome "user").

    Args:
        name: Name for the new profile (slugified to letters/digits/_/-).
        makeActive: When ``True`` (default), also make it the active profile.
    """
    return await getBrowserManager().createProfile(name, makeActive=makeActive)


@mcp.tool(name="login_session")
@safeAsync(action="login_session")
async def loginSession(profile: str | None = None, url: str | None = None) -> dict[str, Any]:
    """Open a VISIBLE browser on a profile so the USER can log in / sign up.

    Use this for sites that need a real human to authenticate (Google account,
    creating a new account, solving a captcha) before the agent can automate
    them. The window opens headed; the user logs in; the session is saved into
    the profile so all future automated runs are already logged in.

    Args:
        profile: Profile to log into (or ``"random"``). If omitted and none is
            active, returns ``profile_selection_required`` — ask the user first.
        url: Optional URL to open for the login (e.g. the site's sign-in page).
    """
    return await getBrowserManager().loginSession(profile=profile, url=url)


@mcp.tool(name="set_headless")
@safeAsync(action="set_headless")
async def setHeadless(headless: bool) -> dict[str, Any]:
    """Switch a running browser between headless and headed WITHOUT losing state.

    Flip to ``headless=False`` to pop up a real window so a human can solve a
    captcha / "are you human" page or log in manually; flip back to
    ``headless=True`` afterwards. Cookies/login survive the switch (persistent
    profile). If the browser isn't running, the choice applies on next
    ``open_browser``.

    Args:
        headless: ``True`` for no window, ``False`` for a visible window.
    """
    return await getBrowserManager().setHeadless(headless)


@mcp.tool(name="close_browser")
@safeAsync(action="close_browser")
async def closeBrowser() -> dict[str, Any]:
    """Close the browser and release all of its resources.

    The persistent profile is saved on close, so logins remain for next time.
    Stop any active recording first with ``stop_recording``.
    """
    return await getBrowserManager().closeBrowser()


@mcp.tool(name="clear_profile")
@safeAsync(action="clear_profile")
async def clearProfile() -> dict[str, Any]:
    """Wipe the persistent profile — logs out of EVERYTHING for a fresh session.

    Deletes all saved cookies/tokens/localStorage (closes the browser first if
    running). Use when you want to start clean or switch accounts.
    """
    return await getBrowserManager().clearProfile()


# --------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------- #
@mcp.tool(name="navigate")
@safeAsync(action="navigate")
async def navigate(
    url: str,
    waitUntil: str = "networkidle",
    timeoutMs: int | None = None,
) -> dict[str, Any]:
    """Navigate the active tab to ``url`` (auto-launches the browser if needed).

    Args:
        url: Absolute URL to open, e.g. ``https://example.com``.
        waitUntil: When to consider navigation finished. One of ``load``,
            ``domcontentloaded``, ``networkidle``, or ``commit``. Defaults to
            ``networkidle`` which waits for network activity to settle.
        timeoutMs: Optional navigation timeout in milliseconds; ``None`` uses the
            server default.
    """
    return await getBrowserManager().navigate(url, waitUntil=waitUntil, timeoutMs=timeoutMs)


@mcp.tool(name="navigate_back")
@safeAsync(action="navigate_back")
async def navigateBack() -> dict[str, Any]:
    """Go back one entry in the active tab's history (browser Back button)."""
    return await getBrowserManager().navigateBack()


@mcp.tool(name="navigate_forward")
@safeAsync(action="navigate_forward")
async def navigateForward() -> dict[str, Any]:
    """Go forward one entry in the active tab's history (browser Forward button)."""
    return await getBrowserManager().navigateForward()


@mcp.tool(name="refresh")
@safeAsync(action="refresh")
async def refresh() -> dict[str, Any]:
    """Reload the current page in the active tab."""
    return await getBrowserManager().refresh()


# --------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------- #
@mcp.tool(name="open_new_tab")
@safeAsync(action="open_new_tab")
async def openNewTab(url: str | None = None) -> dict[str, Any]:
    """Open a new browser tab and make it active.

    Args:
        url: Optional URL to load in the new tab. If omitted, an empty tab is
            opened.
    """
    return await getBrowserManager().openNewTab(url)


@mcp.tool(name="switch_tab")
@safeAsync(action="switch_tab")
async def switchTab(index: int = 0) -> dict[str, Any]:
    """Switch the active tab to the one at ``index`` (0-based).

    Args:
        index: Zero-based position of the tab to activate.
    """
    return await getBrowserManager().switchTab(index)


@mcp.tool(name="close_tab")
@safeAsync(action="close_tab")
async def closeTab(index: int | None = None) -> dict[str, Any]:
    """Close a tab by ``index``, or the active tab when ``index`` is omitted.

    Args:
        index: Zero-based position of the tab to close. ``None`` closes the
            currently active tab.
    """
    return await getBrowserManager().closeTab(index)


# --------------------------------------------------------------------- #
# Extraction / info
# --------------------------------------------------------------------- #
@mcp.tool(name="get_title")
@safeAsync(action="get_title")
async def getTitle() -> dict[str, Any]:
    """Return the title of the page in the active tab."""
    return await getBrowserManager().getTitle()


@mcp.tool(name="get_url")
@safeAsync(action="get_url")
async def getUrl() -> dict[str, Any]:
    """Return the current URL of the active tab."""
    return await getBrowserManager().getUrl()


@mcp.tool(name="extract_text")
@safeAsync(action="extract_text")
async def extractText() -> dict[str, Any]:
    """Extract the visible text content of the current page.

    Useful for reading and summarising page content without screenshots.
    """
    return await getBrowserManager().extractText()


@mcp.tool(name="extract_links")
@safeAsync(action="extract_links")
async def extractLinks() -> dict[str, Any]:
    """Extract all hyperlinks (anchor text and href) from the current page."""
    return await getBrowserManager().extractLinks()


@mcp.tool(name="extract_buttons")
@safeAsync(action="extract_buttons")
async def extractButtons() -> dict[str, Any]:
    """Extract clickable buttons from the current page.

    Returns labels and selectors you can pass to ``click`` to act on them.
    """
    return await getBrowserManager().extractButtons()


@mcp.tool(name="extract_forms")
@safeAsync(action="extract_forms")
async def extractForms() -> dict[str, Any]:
    """Extract forms and their input fields from the current page.

    Use the returned field selectors with ``fill`` to populate a form.
    """
    return await getBrowserManager().extractForms()


@mcp.tool(name="extract_images")
@safeAsync(action="extract_images")
async def extractImages() -> dict[str, Any]:
    """Extract images (source URL and alt text) from the current page."""
    return await getBrowserManager().extractImages()


@mcp.tool(name="get_dom")
@safeAsync(action="get_dom")
async def getDom(selector: str | None = None) -> dict[str, Any]:
    """Return the HTML/DOM of the page, or of a single matched element.

    Args:
        selector: Optional CSS selector. When provided, only the outer HTML of
            the first matching element is returned; otherwise the full page DOM.
    """
    return await getBrowserManager().getDom(selector)


@mcp.tool(name="read_page")
@safeAsync(action="read_page")
async def readPage(textLimit: int = 5000) -> dict[str, Any]:
    """Read the whole page in ONE call: title, URL, visible text, links, buttons,
    forms, and image count.

    This is the fastest way to understand a page without multiple round-trips —
    prefer it when you want to "see what's on the page" before deciding an action.

    Args:
        textLimit: Maximum characters of visible text to return (default 5000).
    """
    return await getBrowserManager().readPage(textLimit=textLimit)


# --------------------------------------------------------------------- #
# Network inspection
# --------------------------------------------------------------------- #
@mcp.tool(name="get_network")
@safeAsync(action="get_network")
async def getNetwork(limit: int = 100, urlContains: str | None = None) -> dict[str, Any]:
    """Return the network requests/responses the browser has made.

    Captures every request across all tabs (URL, HTTP method, status code,
    resource type, and whether it succeeded). Use this to inspect the API calls,
    XHR/fetch traffic, and assets a page loads.

    Args:
        limit: Maximum number of most-recent entries to return (default 100).
        urlContains: Optional substring filter — only entries whose URL contains
            this string are returned (e.g. ``"/api/"``).
    """
    return await getBrowserManager().getNetwork(limit=limit, urlContains=urlContains)


@mcp.tool(name="clear_network")
@safeAsync(action="clear_network")
async def clearNetwork() -> dict[str, Any]:
    """Clear the captured network log (e.g. before triggering a specific action
    so you can inspect just that action's traffic)."""
    return await getBrowserManager().clearNetwork()


# --------------------------------------------------------------------- #
# Interaction
# --------------------------------------------------------------------- #
@mcp.tool(name="scroll")
@safeAsync(action="scroll")
async def scroll(
    deltaY: int = 0,
    deltaX: int = 0,
    selector: str | None = None,
    toTop: bool = False,
    toBottom: bool = False,
    humanize: bool | None = None,
) -> dict[str, Any]:
    """Scroll the page or a scrollable element (human-like by default).

    By default scrolling is lazy and incremental — small wheel flicks with
    pauses, like a human discovering the page — rather than an instant jump, so
    it looks natural to bot-detection.

    Args:
        deltaY: Vertical scroll amount in pixels (positive scrolls down).
        deltaX: Horizontal scroll amount in pixels (positive scrolls right).
        selector: Optional CSS selector to scroll into view (lazily); defaults to
            the page/window.
        toTop: When ``True``, scroll to the top, ignoring the deltas.
        toBottom: When ``True``, scroll to the bottom, ignoring the deltas.
        humanize: Force human-like (``True``) or instant (``False``) scrolling;
            ``None`` uses the server default (``ABC_HUMANIZE``).
    """
    return await getBrowserManager().scroll(
        deltaX=deltaX,
        deltaY=deltaY,
        selector=selector,
        toTop=toTop,
        toBottom=toBottom,
        humanize=humanize,
    )


@mcp.tool(name="hover")
@safeAsync(action="hover")
async def hover(selector: str, timeoutMs: int | None = None) -> dict[str, Any]:
    """Move the mouse over the element matched by ``selector``.

    Args:
        selector: CSS selector of the element to hover.
        timeoutMs: Optional wait timeout in milliseconds; ``None`` uses the
            server default.
    """
    return await getBrowserManager().hover(selector, timeoutMs=timeoutMs)


@mcp.tool(name="click")
@safeAsync(action="click")
async def click(
    selector: str,
    button: str = "left",
    clickCount: int = 1,
    timeoutMs: int | None = None,
    humanize: bool | None = None,
) -> dict[str, Any]:
    """Click the element matched by ``selector`` (human-like cursor by default).

    By default the cursor travels a curved, slightly wobbling path from where it
    last was to a random point inside the target, pauses, then presses — never a
    straight teleport to the exact center — so cursor-movement bot checks pass.

    Args:
        selector: CSS selector of the element to click.
        button: Mouse button to use: ``left``, ``right``, or ``middle``.
        clickCount: Number of clicks to deliver (e.g. ``2`` for a double click).
        timeoutMs: Optional wait timeout in milliseconds; ``None`` uses the
            server default.
        humanize: Force human-like (``True``) or instant (``False``) clicking;
            ``None`` uses the server default (``ABC_HUMANIZE``).
    """
    return await getBrowserManager().click(
        selector, button=button, clickCount=clickCount, timeoutMs=timeoutMs, humanize=humanize
    )


@mcp.tool(name="double_click")
@safeAsync(action="double_click")
async def doubleClick(
    selector: str, timeoutMs: int | None = None, humanize: bool | None = None
) -> dict[str, Any]:
    """Double-click the element matched by ``selector`` (human-like by default).

    Args:
        selector: CSS selector of the element to double-click.
        timeoutMs: Optional wait timeout in milliseconds; ``None`` uses the
            server default.
        humanize: Force human-like (``True``) or instant (``False``) clicking;
            ``None`` uses the server default (``ABC_HUMANIZE``).
    """
    return await getBrowserManager().doubleClick(selector, timeoutMs=timeoutMs, humanize=humanize)


@mcp.tool(name="right_click")
@safeAsync(action="right_click")
async def rightClick(
    selector: str, timeoutMs: int | None = None, humanize: bool | None = None
) -> dict[str, Any]:
    """Right-click (context-menu click) the element matched by ``selector``.

    Args:
        selector: CSS selector of the element to right-click.
        timeoutMs: Optional wait timeout in milliseconds; ``None`` uses the
            server default.
        humanize: Force human-like (``True``) or instant (``False``) clicking;
            ``None`` uses the server default (``ABC_HUMANIZE``).
    """
    return await getBrowserManager().rightClick(selector, timeoutMs=timeoutMs, humanize=humanize)


@mcp.tool(name="fill")
@safeAsync(action="fill")
async def fill(
    selector: str,
    value: str,
    clearFirst: bool = True,
    timeoutMs: int | None = None,
    humanize: bool | None = None,
) -> dict[str, Any]:
    """Type ``value`` into the input/textarea matched by ``selector``.

    By default typing is HUMAN-PACED (~25 WPM with natural jitter and the odd
    pause) and the field is reached by moving the cursor there and clicking to
    focus — so it does not look like a machine pasted the whole string at once.

    Args:
        selector: CSS selector of the input element.
        value: Text to enter into the field.
        clearFirst: When ``True``, clear any existing content before typing.
        timeoutMs: Optional wait timeout in milliseconds; ``None`` uses the
            server default.
        humanize: Force human-paced (``True``) or instant (``False``) typing;
            ``None`` uses the server default (``ABC_HUMANIZE``).
    """
    return await getBrowserManager().fill(
        selector, value, clearFirst=clearFirst, timeoutMs=timeoutMs, humanize=humanize
    )


@mcp.tool(name="upload_file")
@safeAsync(action="upload_file")
async def uploadFile(selector: str, filePaths: list[str]) -> dict[str, Any]:
    """Set files on a file ``<input>`` matched by ``selector``.

    Args:
        selector: CSS selector of the file input element.
        filePaths: Absolute paths of the local files to upload.
    """
    return await getBrowserManager().uploadFile(selector, filePaths)


@mcp.tool(name="download_file")
@safeAsync(action="download_file")
async def downloadFile(
    selector: str,
    saveDir: str | None = None,
    timeoutMs: int | None = None,
    imagesOnly: bool = True,
) -> dict[str, Any]:
    """Click ``selector`` to trigger a download and save the resulting file SAFELY.

    By default only REAL images are kept: the saved file's actual bytes are
    inspected (magic number + full decode) and anything that is really an
    executable/app/archive disguised as an image — or any non-image — is deleted
    and reported as an error. This blocks "download this image" malware lures.
    Downloads may take a long time, so the wait ceiling is generous (up to ~1h,
    ``ABC_MAX_DOWNLOAD_WAIT_MS``).

    Args:
        selector: CSS selector of the element that initiates the download.
        saveDir: Optional directory to save into; ``None`` uses the server's
            default download directory.
        timeoutMs: Optional wait ceiling in ms; ``None`` uses the long download max.
        imagesOnly: Keep only verified images (default ``True``). Set ``False`` to
            allow any file (use only when you trust the source).
    """
    return await getBrowserManager().downloadFile(
        selector, saveDir=saveDir, timeoutMs=timeoutMs, imagesOnly=imagesOnly
    )


@mcp.tool(name="press_keys")
@safeAsync(action="press_keys")
async def pressKeys(keys: str, selector: str | None = None) -> dict[str, Any]:
    """Press a key or key combination, optionally targeting an element.

    Args:
        keys: Key or combination to press, e.g. ``Enter``, ``Tab``, or
            ``Control+A`` (Playwright key syntax).
        selector: Optional CSS selector to focus before pressing; ``None`` sends
            the keys to the currently focused element.
    """
    return await getBrowserManager().pressKeys(keys, selector=selector)


# --------------------------------------------------------------------- #
# Waits
# --------------------------------------------------------------------- #
@mcp.tool(name="wait_for_element")
@safeAsync(action="wait_for_element")
async def waitForElement(
    selector: str,
    state: str = "visible",
    timeoutMs: int | None = None,
) -> dict[str, Any]:
    """Wait for the element matched by ``selector`` to reach ``state``.

    Args:
        selector: CSS selector of the element to wait for.
        state: Target state: ``attached``, ``detached``, ``visible``, or
            ``hidden``. Defaults to ``visible``.
        timeoutMs: Optional wait timeout in milliseconds; ``None`` uses the
            server default.
    """
    return await getBrowserManager().waitForElement(selector, state=state, timeoutMs=timeoutMs)


@mcp.tool(name="wait_for_network_idle")
@safeAsync(action="wait_for_network_idle")
async def waitForNetworkIdle(timeoutMs: int | None = None) -> dict[str, Any]:
    """Wait until network activity on the active tab has gone idle.

    Args:
        timeoutMs: Optional wait timeout in milliseconds; ``None`` uses the
            server default.
    """
    return await getBrowserManager().waitForNetworkIdle(timeoutMs=timeoutMs)


@mcp.tool(name="wait_for_stable")
@safeAsync(action="wait_for_stable")
async def waitForStable(
    selector: str,
    stableMs: int = 1200,
    timeoutMs: int | None = None,
) -> dict[str, Any]:
    """Wait QUIETLY until an element's text stops changing, then return its text.

    This is the right way to wait for a slow online AI (ChatGPT, etc.): the
    answer streams in token by token, then stops. Point ``selector`` at the
    response container and this resolves once the text has been unchanged for
    ``stableMs`` — no polling loops, no fixed sleeps. Capped at 5 minutes by
    default (``ABC_MAX_WAIT_MS``).

    Args:
        selector: CSS selector of the element whose text settles (the AI answer).
        stableMs: How long the text must stay unchanged to count as done.
        timeoutMs: Hard cap in ms; ``None`` uses the server max-wait (5 min).
    """
    return await getBrowserManager().waitForStable(selector, stableMs=stableMs, timeoutMs=timeoutMs)


@mcp.tool(name="wait_for_response")
@safeAsync(action="wait_for_response")
async def waitForResponse(
    urlPattern: str, timeoutMs: int | None = None, includeQuery: bool = False
) -> dict[str, Any]:
    """Wait until a network response matching ``urlPattern`` FINISHES.

    Reads straight from the network layer: resolves the moment the matching
    (possibly streamed/SSE) response closes — e.g. when an online AI's backend
    has finished sending its answer. Often more reliable than watching the DOM.
    Capped at 5 minutes by default (``ABC_MAX_WAIT_MS``).

    MATCHING: ``urlPattern`` is matched (regex first, else substring) against the
    URL's scheme+host+**path** — the query string is EXCLUDED by default, so a
    loose pattern won't accidentally latch onto a query parameter of an unrelated
    request (e.g. a bot-detection beacon like ``/anomaly.js?cc=duckchat``). Prefer
    a path-anchored pattern such as ``"/backend-api/conversation"``. The returned
    ``url`` shows exactly what matched — check it. Set ``includeQuery=True`` only
    if the discriminating part really lives in the query string.

    Args:
        urlPattern: Regex or substring matched against the response URL's path.
        timeoutMs: Hard cap in ms; ``None`` uses the server max-wait (5 min).
        includeQuery: Match against the full URL (including query) instead of path.
    """
    return await getBrowserManager().waitForResponse(
        urlPattern, timeoutMs=timeoutMs, includeQuery=includeQuery
    )


# --------------------------------------------------------------------- #
# No-image mode (MarkItDown) — read text instead of pixels
# --------------------------------------------------------------------- #
@mcp.tool(name="to_markdown")
@safeAsync(action="to_markdown")
async def toMarkdown(source: str) -> dict[str, Any]:
    """Convert an image / PDF / Office doc / HTML page (file path or URL) to markdown.

    Backed by Microsoft's MarkItDown. Use it to read media as text — e.g. extract
    a PDF's contents, or describe a downloaded chart — without handling pixels.

    Args:
        source: A local file path or a URL to convert.
    """
    return await getBrowserManager().toMarkdown(source)


@mcp.tool(name="set_no_image_mode")
@safeAsync(action="set_no_image_mode")
async def setNoImageMode(enabled: bool) -> dict[str, Any]:
    """Toggle global no-image mode.

    When ON, pixel screenshots are suppressed and you are steered to text:
    ``read_page`` for page text and ``to_markdown`` to convert media. Turn it OFF
    to allow screenshots again.

    Args:
        enabled: ``True`` to enable no-image mode, ``False`` to disable.
    """
    return await getBrowserManager().setNoImageMode(enabled)


# --------------------------------------------------------------------- #
# Visual intelligence
# --------------------------------------------------------------------- #
@mcp.tool(name="take_screenshot")
@safeAsync(action="take_screenshot")
async def takeScreenshot(
    fullPage: bool = False,
    selector: str | None = None,
    annotate: bool = False,
    label: str | None = None,
) -> dict[str, Any]:
    """Capture a screenshot of the current page or a single element.

    Args:
        fullPage: When ``True``, capture the entire scrollable page rather than
            just the viewport.
        selector: Optional CSS selector to screenshot only that element.
        annotate: When ``True``, overlay visual annotations on the capture.
        label: Optional text label to include when annotating.
    """
    return await getBrowserManager().takeScreenshot(
        fullPage=fullPage,
        selector=selector,
        annotate=annotate,
        label=label,
    )


@mcp.tool(name="screenshot")
async def screenshot(fullPage: bool = False, selector: str | None = None) -> Image:
    """Capture a screenshot and return it INLINE as an image you can directly see.

    This is the primary "let me look at the page" tool — ideal for inspecting the
    UI/UX of something you just built (e.g. a local dev server). The image is
    returned in-memory and **nothing is written to disk**, so it never fills up
    storage. Use ``take_screenshot`` instead only when you explicitly want a saved
    file path.

    When no-image mode is ON, this returns a short text notice (no pixels) instead
    of an image — use ``read_page`` / ``to_markdown`` to read the page as text.

    Args:
        fullPage: When ``True``, capture the entire scrollable page.
        selector: Optional CSS selector to capture only that element.
    """
    result = await getBrowserManager().captureScreenshotData(fullPage=fullPage, selector=selector)
    if not result.get("success"):
        raise RuntimeError(result.get("details") or result.get("error") or "screenshot failed")
    data = result.get("data", {})
    # No-image mode short-circuits to a notice with no "image" key — return that
    # as text instead of trying to build an (absent) image block.
    if data.get("noImageMode") or "image" not in data:
        return data.get("note", "No-image mode is on; screenshots are suppressed.")
    return Image(data=data["image"], format="png")


@mcp.tool(name="clear_storage")
@safeAsync(action="clear_storage")
async def clearStorage(kinds: list[str] | None = None) -> dict[str, Any]:
    """Delete saved screenshots/recordings/downloads to free disk space.

    Use when you are done and want to leave no artifacts behind. ``kinds``
    defaults to all of ``["screenshots", "recordings", "downloads"]``; pass a
    subset to target one. Does not require the browser to be running.
    """
    return await getBrowserManager().clearStorage(kinds)


# --------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------- #
@mcp.tool(name="start_recording")
@safeAsync(action="start_recording")
async def startRecording(fps: int | None = None, sessionName: str | None = None) -> dict[str, Any]:
    """Start recording a video of the browser session.

    Args:
        fps: Optional frames-per-second for the recording; ``None`` uses the
            server default.
        sessionName: Optional name used for the output file; ``None``
            auto-generates one.
    """
    return await getBrowserManager().startRecording(fps=fps, sessionName=sessionName)


@mcp.tool(name="stop_recording")
@safeAsync(action="stop_recording")
async def stopRecording() -> dict[str, Any]:
    """Stop the active recording and finalise the video file."""
    return await getBrowserManager().stopRecording()


# --------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------- #
@mcp.tool(name="status")
@safeAsync(action="status")
async def status() -> dict[str, Any]:
    """Return a snapshot of the browser state (running, tabs, recording, etc.)."""
    return await getBrowserManager().status()


# --------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------- #
def main() -> None:
    """Run the MCP server over stdio transport."""
    logger.info(
        "Starting AI Browser Controller MCP server (browser=%s, headless=%s) over stdio",
        settings.browserType,
        settings.headless,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
