"""FastAPI HTTP server for the AI Browser Controller.

This is the REST + static-file surface an AI agent (or any HTTP client) uses to
drive the browser. Every endpoint awaits the matching
:class:`~app.browser.browser_manager.BrowserManager` method and returns that
method's AI-friendly envelope verbatim, so the wire format is identical across
HTTP, WebSocket and MCP.

Saved artifacts are served back under ``/screenshots`` and ``/recordings`` so an
agent that receives a ``video_path`` or screenshot path in an envelope can fetch
the file over the same origin. The ``/ws`` WebSocket route is contributed by
:mod:`app.api.websocket`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.websocket import registerWebSocket
from app.browser.browser_manager import getBrowserManager
from app.models.commands import (
    ClickCommand,
    DownloadCommand,
    ExtractCommand,
    FillCommand,
    HoverCommand,
    LaunchCommand,
    NavigateCommand,
    PressKeysCommand,
    RecordingCommand,
    ScreenshotCommand,
    ScrollCommand,
    SelectCommand,
    TabCommand,
    UploadCommand,
    WaitForElementCommand,
)
from app.utils.config import settings
from app.utils.helpers import ensureDir, errorResponse
from app.utils.logger import getLogger

logger = getLogger("api.server")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup/shutdown side effects for the application.

    On startup we guarantee the screenshot and recording directories exist so
    static mounts and artifact writes never race a missing folder. On shutdown
    we close the browser best-effort so no Playwright process is leaked.
    """
    ensureDir(settings.screenshotDir)
    ensureDir(settings.recordingDir)
    logger.info(
        "Started ai-browser-controller v%s (screenshots=%s, recordings=%s)",
        __version__,
        settings.screenshotDir,
        settings.recordingDir,
    )
    try:
        yield
    finally:
        try:
            await getBrowserManager().closeBrowser()
            logger.info("Browser closed on shutdown")
        except Exception:  # noqa: BLE001 - shutdown must never raise
            logger.exception("Failed to close browser on shutdown")


def createApp() -> FastAPI:
    """Build and return the configured FastAPI application."""
    app = FastAPI(
        title="AI Browser Controller",
        version=__version__,
        description="Plug-and-play browser automation for AI agents.",
        lifespan=lifespan,
    )

    # Permissive CORS: this is a local developer tool, not a public service.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Ensure artifact directories exist before mounting them as static roots.
    ensureDir(settings.screenshotDir)
    ensureDir(settings.recordingDir)
    app.mount(
        "/screenshots",
        StaticFiles(directory=str(settings.screenshotDir)),
        name="screenshots",
    )
    app.mount(
        "/recordings",
        StaticFiles(directory=str(settings.recordingDir)),
        name="recordings",
    )

    # ----------------------------------------------------------------- #
    # Health / status
    # ----------------------------------------------------------------- #
    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe — does not touch the browser."""
        return {
            "status": "ok",
            "service": "ai-browser-controller",
            "version": __version__,
        }

    @app.get("/status")
    async def getStatus() -> dict:
        logger.info("Request: status")
        return await getBrowserManager().status()

    # ----------------------------------------------------------------- #
    # Browser lifecycle
    # ----------------------------------------------------------------- #
    @app.post("/browser/open")
    async def openBrowser(command: LaunchCommand) -> dict:
        logger.info("Request: open_browser")
        return await getBrowserManager().openBrowser(
            browserType=command.browserType,
            headless=command.headless,
            viewportWidth=command.viewportWidth,
            viewportHeight=command.viewportHeight,
            userAgent=command.userAgent,
        )

    @app.post("/browser/close")
    async def closeBrowser() -> dict:
        logger.info("Request: close_browser")
        return await getBrowserManager().closeBrowser()

    @app.post("/browser/set-headless")
    async def setHeadless(headless: bool = False) -> dict:
        logger.info("Request: set_headless -> %s", headless)
        return await getBrowserManager().setHeadless(headless)

    @app.post("/browser/clear-profile")
    async def clearProfile() -> dict:
        logger.info("Request: clear_profile")
        return await getBrowserManager().clearProfile()

    # ----------------------------------------------------------------- #
    # Navigation
    # ----------------------------------------------------------------- #
    @app.post("/navigate")
    async def navigate(command: NavigateCommand) -> dict:
        logger.info("Request: navigate -> %s", command.url)
        return await getBrowserManager().navigate(
            command.url,
            waitUntil=command.waitUntil,
            timeoutMs=command.timeoutMs,
        )

    @app.post("/navigate/back")
    async def navigateBack() -> dict:
        logger.info("Request: navigate_back")
        return await getBrowserManager().navigateBack()

    @app.post("/navigate/forward")
    async def navigateForward() -> dict:
        logger.info("Request: navigate_forward")
        return await getBrowserManager().navigateForward()

    @app.post("/refresh")
    async def refresh() -> dict:
        logger.info("Request: refresh")
        return await getBrowserManager().refresh()

    # ----------------------------------------------------------------- #
    # Tabs
    # ----------------------------------------------------------------- #
    @app.post("/tabs/new")
    async def newTab(command: TabCommand) -> dict:
        logger.info("Request: open_new_tab")
        return await getBrowserManager().openNewTab(command.url)

    @app.post("/tabs/switch")
    async def switchTab(command: TabCommand) -> dict:
        logger.info("Request: switch_tab -> %s", command.index)
        if command.index is None:
            return errorResponse(
                error="Missing index",
                details="'index' is required to switch tabs.",
                action="switch_tab",
            )
        return await getBrowserManager().switchTab(command.index)

    @app.post("/tabs/close")
    async def closeTab(command: TabCommand) -> dict:
        logger.info("Request: close_tab -> %s", command.index)
        return await getBrowserManager().closeTab(command.index)

    # ----------------------------------------------------------------- #
    # Extraction / info
    # ----------------------------------------------------------------- #
    @app.post("/extract")
    async def extract(command: ExtractCommand) -> dict:
        logger.info("Request: extract -> %s", command.kind)
        manager = getBrowserManager()
        kind = command.kind
        if kind == "text":
            return await manager.extractText()
        if kind == "links":
            return await manager.extractLinks()
        if kind == "buttons":
            return await manager.extractButtons()
        if kind == "forms":
            return await manager.extractForms()
        if kind == "images":
            return await manager.extractImages()
        if kind == "dom":
            return await manager.getDom(command.selector)
        if kind == "title":
            return await manager.getTitle()
        if kind == "url":
            return await manager.getUrl()
        return errorResponse(
            error="Unknown extract kind",
            details=f"'{kind}' is not a supported extraction kind.",
            action="extract",
        )

    # ----------------------------------------------------------------- #
    # Interaction
    # ----------------------------------------------------------------- #
    @app.post("/interact/click")
    async def click(command: ClickCommand) -> dict:
        logger.info("Request: click -> %s", command.selector)
        return await getBrowserManager().click(
            command.selector,
            button=command.button,
            clickCount=command.clickCount,
            timeoutMs=command.timeoutMs,
        )

    @app.post("/interact/double-click")
    async def doubleClick(command: ClickCommand) -> dict:
        logger.info("Request: double_click -> %s", command.selector)
        return await getBrowserManager().doubleClick(
            command.selector, timeoutMs=command.timeoutMs
        )

    @app.post("/interact/right-click")
    async def rightClick(command: ClickCommand) -> dict:
        logger.info("Request: right_click -> %s", command.selector)
        return await getBrowserManager().rightClick(
            command.selector, timeoutMs=command.timeoutMs
        )

    @app.post("/interact/hover")
    async def hover(command: HoverCommand) -> dict:
        logger.info("Request: hover -> %s", command.selector)
        return await getBrowserManager().hover(
            command.selector, timeoutMs=command.timeoutMs
        )

    @app.post("/interact/fill")
    async def fill(command: FillCommand) -> dict:
        logger.info("Request: fill -> %s", command.selector)
        return await getBrowserManager().fill(
            command.selector,
            command.value,
            clearFirst=command.clearFirst,
            timeoutMs=command.timeoutMs,
        )

    @app.post("/interact/scroll")
    async def scroll(command: ScrollCommand) -> dict:
        logger.info("Request: scroll")
        return await getBrowserManager().scroll(
            deltaX=command.deltaX,
            deltaY=command.deltaY,
            selector=command.selector,
            toTop=command.toTop,
            toBottom=command.toBottom,
        )

    @app.post("/interact/press-keys")
    async def pressKeys(command: PressKeysCommand) -> dict:
        logger.info("Request: press_keys -> %s", command.keys)
        return await getBrowserManager().pressKeys(command.keys, selector=command.selector)

    @app.post("/interact/select")
    async def selectOption(command: SelectCommand) -> dict:
        logger.info("Request: select_option -> %s", command.selector)
        return await getBrowserManager().selectOption(
            command.selector, value=command.value, label=command.label, timeoutMs=command.timeoutMs
        )

    @app.post("/interact/upload")
    async def uploadFile(command: UploadCommand) -> dict:
        logger.info("Request: upload_file -> %s", command.selector)
        return await getBrowserManager().uploadFile(command.selector, command.filePaths)

    @app.post("/interact/download")
    async def downloadFile(command: DownloadCommand) -> dict:
        logger.info("Request: download_file -> %s", command.selector)
        return await getBrowserManager().downloadFile(
            command.selector,
            saveDir=command.saveDir,
            timeoutMs=command.timeoutMs,
        )

    # ----------------------------------------------------------------- #
    # Waits
    # ----------------------------------------------------------------- #
    @app.post("/wait/element")
    async def waitForElement(command: WaitForElementCommand) -> dict:
        logger.info("Request: wait_for_element -> %s", command.selector)
        return await getBrowserManager().waitForElement(
            command.selector,
            state=command.state,
            timeoutMs=command.timeoutMs,
        )

    @app.post("/wait/network-idle")
    async def waitForNetworkIdle() -> dict:
        logger.info("Request: wait_for_network_idle")
        return await getBrowserManager().waitForNetworkIdle()

    # ----------------------------------------------------------------- #
    # Visual intelligence
    # ----------------------------------------------------------------- #
    @app.post("/screenshot")
    async def takeScreenshot(command: ScreenshotCommand) -> dict:
        logger.info("Request: take_screenshot")
        return await getBrowserManager().takeScreenshot(
            fullPage=command.fullPage,
            selector=command.selector,
            annotate=command.annotate,
            label=command.label,
        )

    # ----------------------------------------------------------------- #
    # Recording
    # ----------------------------------------------------------------- #
    @app.post("/recording/start")
    async def startRecording(command: RecordingCommand) -> dict:
        logger.info("Request: start_recording")
        return await getBrowserManager().startRecording(
            fps=command.fps, sessionName=command.sessionName
        )

    @app.post("/recording/stop")
    async def stopRecording() -> dict:
        logger.info("Request: stop_recording")
        return await getBrowserManager().stopRecording()

    # ----------------------------------------------------------------- #
    # Aggregate read + network inspection
    # ----------------------------------------------------------------- #
    @app.post("/read-page")
    async def readPage(textLimit: int = 5000) -> dict:
        logger.info("Request: read_page")
        return await getBrowserManager().readPage(textLimit=textLimit)

    @app.get("/network")
    async def getNetwork(limit: int = 100, urlContains: str | None = None) -> dict:
        logger.info("Request: get_network")
        return await getBrowserManager().getNetwork(limit=limit, urlContains=urlContains)

    @app.post("/network/clear")
    async def clearNetwork() -> dict:
        logger.info("Request: clear_network")
        return await getBrowserManager().clearNetwork()

    # ----------------------------------------------------------------- #
    # Storage maintenance
    # ----------------------------------------------------------------- #
    @app.post("/storage/clear")
    async def clearStorage(kinds: list[str] | None = None) -> dict:
        logger.info("Request: clear_storage")
        return await getBrowserManager().clearStorage(kinds)

    # ----------------------------------------------------------------- #
    # Global error handlers — no request ever returns a raw stack trace.
    # Every failure becomes the same AI-friendly envelope the endpoints use.
    # ----------------------------------------------------------------- #
    @app.exception_handler(RequestValidationError)
    async def handleValidationError(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("Validation error on %s", request.url.path)
        return JSONResponse(
            status_code=422,
            content=errorResponse(
                error="Invalid request payload",
                details=str(exc.errors()),
                action=request.url.path,
            ),
        )

    @app.exception_handler(Exception)
    async def handleUnexpectedError(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content=errorResponse(
                error="Internal server error",
                details=f"{type(exc).__name__}: {exc}",
                action=request.url.path,
            ),
        )

    # ----------------------------------------------------------------- #
    # WebSocket route (contributed by app.api.websocket)
    # ----------------------------------------------------------------- #
    registerWebSocket(app)

    return app


# Module-level app so ``uvicorn app.api.server:app`` works out of the box.
app = createApp()


def runServer() -> None:
    """Run the development server with uvicorn using configured host/port."""
    logger.info("Serving on http://%s:%d", settings.apiHost, settings.apiPort)
    uvicorn.run(app, host=settings.apiHost, port=settings.apiPort)


if __name__ == "__main__":
    runServer()
