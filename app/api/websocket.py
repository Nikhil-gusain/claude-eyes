"""WebSocket interface for the browser controller.

This module exposes a single ``/ws`` endpoint that lets an AI agent drive the
:class:`~app.browser.browser_manager.BrowserManager` over a persistent socket
using the same action vocabulary as the MCP/tool layer. Every inbound message
has the shape ``{"action": str, "params": {...}}`` and every reply is one of the
AI-friendly envelopes produced by :func:`app.utils.helpers.successResponse` /
:func:`app.utils.helpers.errorResponse`.

A process-wide :class:`ConnectionManager` tracks live sockets so future features
(server-initiated events, broadcasts of recording progress, ...) have a single
place to fan out to all connected agents.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.browser.browser_manager import BrowserManager, getBrowserManager
from app.browser.session_pool import (
    closeSession,
    createSession,
    listSessions,
    switchSession,
)
from app.utils.helpers import errorResponse
from app.utils.logger import getLogger

logger = getLogger("api.websocket")

router = APIRouter()


class ConnectionManager:
    """Track active WebSocket connections and provide fan-out helpers."""

    def __init__(self) -> None:
        self.activeConnections: list[WebSocket] = []
        # SSE subscribers: each is an async queue fed by :meth:`publish`. SSE is a
        # lightweight, one-way (server -> client) push — ideal for streaming
        # progress / "long wait finished" events to plain HTTP clients without the
        # overhead of a full WebSocket.
        self.sseQueues: list[asyncio.Queue[dict[str, Any]]] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept *websocket* and register it as active."""
        await websocket.accept()
        self.activeConnections.append(websocket)
        logger.info("WebSocket connected (%d active)", len(self.activeConnections))

    def disconnect(self, websocket: WebSocket) -> None:
        """Drop *websocket* from the active set (idempotent)."""
        if websocket in self.activeConnections:
            self.activeConnections.remove(websocket)
        logger.info("WebSocket disconnected (%d active)", len(self.activeConnections))

    async def sendJson(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        """Send a single JSON envelope to one connection."""
        await websocket.send_json(payload)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Send a JSON envelope to every active connection, best-effort."""
        stale: list[WebSocket] = []
        for connection in list(self.activeConnections):
            try:
                await connection.send_json(payload)
            except Exception:  # noqa: BLE001 - a dead socket must not stop the rest
                logger.exception("Broadcast to a connection failed; dropping it")
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)
        self.publish(payload)

    # ------------------------------------------------------------------ #
    # SSE (server-sent events) — lightweight one-way push to HTTP clients
    # ------------------------------------------------------------------ #
    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        """Register and return a queue that receives every published event."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sseQueues.append(queue)
        logger.info("SSE subscriber added (%d active)", len(self.sseQueues))
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        """Drop an SSE subscriber queue (idempotent)."""
        if queue in self.sseQueues:
            self.sseQueues.remove(queue)
        logger.info("SSE subscriber removed (%d active)", len(self.sseQueues))

    def publish(self, payload: dict[str, Any]) -> None:
        """Fan *payload* out to every SSE subscriber (non-blocking, best-effort)."""
        for queue in list(self.sseQueues):
            try:
                queue.put_nowait(payload)
            except Exception:  # noqa: BLE001 - a full/closed queue must not stop the rest
                logger.debug("Failed to enqueue SSE payload for a subscriber")


# Process-wide connection registry shared across all ``/ws`` handlers.
connectionManager = ConnectionManager()


# --------------------------------------------------------------------- #
# Dispatch table: action-name -> coroutine factory over the manager
# --------------------------------------------------------------------- #
# Each factory receives the live ``BrowserManager`` plus the decoded ``params``
# dict and returns the awaitable for the matching manager method. Keeping a
# single table means both the WebSocket loop and any future caller share one
# authoritative mapping from the tool vocabulary to manager methods.
DispatchFn = Callable[[BrowserManager, dict[str, Any]], Awaitable[dict[str, Any]]]


async def _async(value: dict[str, Any]) -> dict[str, Any]:
    """Wrap an already-computed envelope as an awaitable for the dispatch table.

    The session-pool helpers are synchronous (they return a ready envelope); the
    dispatch table expects every handler to return an awaitable, so they pass
    through here.
    """
    return value


def _buildDispatchTable() -> dict[str, DispatchFn]:
    """Construct the action-name -> manager-call dispatch table."""
    return {
        # Lifecycle
        "open_browser": lambda manager, params: manager.openBrowser(**params),
        "close_browser": lambda manager, params: manager.closeBrowser(),
        "set_headless": lambda manager, params: manager.setHeadless(params.get("headless", True)),
        "clear_profile": lambda manager, params: manager.clearProfile(),
        # Profiles
        "list_profiles": lambda manager, params: manager.listProfiles(),
        "select_profile": lambda manager, params: manager.selectProfile(params["name"]),
        "create_profile": lambda manager, params: manager.createProfile(
            params["name"], makeActive=params.get("makeActive", True)
        ),
        "get_active_profile": lambda manager, params: manager.getActiveProfile(),
        "login_session": lambda manager, params: manager.loginSession(
            profile=params.get("profile"), url=params.get("url")
        ),
        # Navigation
        "navigate": lambda manager, params: manager.navigate(
            params["url"],
            waitUntil=params.get("waitUntil", "networkidle"),
            timeoutMs=params.get("timeoutMs"),
        ),
        "navigate_back": lambda manager, params: manager.navigateBack(),
        "navigate_forward": lambda manager, params: manager.navigateForward(),
        "refresh": lambda manager, params: manager.refresh(),
        # Tabs
        "open_new_tab": lambda manager, params: manager.openNewTab(params.get("url")),
        "switch_tab": lambda manager, params: manager.switchTab(params["index"]),
        "close_tab": lambda manager, params: manager.closeTab(params.get("index")),
        # Extraction / info
        "get_title": lambda manager, params: manager.getTitle(),
        "get_url": lambda manager, params: manager.getUrl(),
        "extract_text": lambda manager, params: manager.extractText(),
        "extract_links": lambda manager, params: manager.extractLinks(),
        "extract_buttons": lambda manager, params: manager.extractButtons(),
        "extract_forms": lambda manager, params: manager.extractForms(),
        "extract_images": lambda manager, params: manager.extractImages(),
        "get_dom": lambda manager, params: manager.getDom(params.get("selector")),
        # Interaction
        "scroll": lambda manager, params: manager.scroll(**params),
        "hover": lambda manager, params: manager.hover(
            params["selector"], timeoutMs=params.get("timeoutMs")
        ),
        "click": lambda manager, params: manager.click(
            params["selector"],
            button=params.get("button", "left"),
            clickCount=params.get("clickCount", 1),
            timeoutMs=params.get("timeoutMs"),
            humanize=params.get("humanize"),
        ),
        "double_click": lambda manager, params: manager.doubleClick(
            params["selector"], timeoutMs=params.get("timeoutMs"), humanize=params.get("humanize")
        ),
        "right_click": lambda manager, params: manager.rightClick(
            params["selector"], timeoutMs=params.get("timeoutMs"), humanize=params.get("humanize")
        ),
        "fill": lambda manager, params: manager.fill(
            params["selector"],
            params["value"],
            clearFirst=params.get("clearFirst", True),
            timeoutMs=params.get("timeoutMs"),
            humanize=params.get("humanize"),
        ),
        "upload_file": lambda manager, params: manager.uploadFile(
            params["selector"], params["filePaths"]
        ),
        "download_file": lambda manager, params: manager.downloadFile(
            params["selector"],
            saveDir=params.get("saveDir"),
            timeoutMs=params.get("timeoutMs"),
            imagesOnly=params.get("imagesOnly", True),
        ),
        "press_keys": lambda manager, params: manager.pressKeys(
            params["keys"], selector=params.get("selector")
        ),
        # Waits
        "wait_for_element": lambda manager, params: manager.waitForElement(
            params["selector"],
            state=params.get("state", "visible"),
            timeoutMs=params.get("timeoutMs"),
        ),
        "wait_for_network_idle": lambda manager, params: manager.waitForNetworkIdle(
            timeoutMs=params.get("timeoutMs")
        ),
        "wait_for_stable": lambda manager, params: manager.waitForStable(
            params["selector"],
            stableMs=params.get("stableMs", 1200),
            timeoutMs=params.get("timeoutMs"),
        ),
        "wait_for_response": lambda manager, params: manager.waitForResponse(
            params["urlPattern"],
            timeoutMs=params.get("timeoutMs"),
            includeQuery=params.get("includeQuery", False),
        ),
        # No-image mode (MarkItDown)
        "to_markdown": lambda manager, params: manager.toMarkdown(params["source"]),
        "set_no_image_mode": lambda manager, params: manager.setNoImageMode(
            params.get("enabled", True)
        ),
        # Visual intelligence
        "take_screenshot": lambda manager, params: manager.takeScreenshot(
            fullPage=params.get("fullPage", False),
            selector=params.get("selector"),
            annotate=params.get("annotate", False),
            label=params.get("label"),
        ),
        # Recording
        "start_recording": lambda manager, params: manager.startRecording(
            fps=params.get("fps"), sessionName=params.get("sessionName")
        ),
        "stop_recording": lambda manager, params: manager.stopRecording(),
        # Status
        "status": lambda manager, params: manager.status(),
        # Aggregate read + network inspection
        "read_page": lambda manager, params: manager.readPage(
            textLimit=params.get("textLimit", 5000)
        ),
        "get_network": lambda manager, params: manager.getNetwork(
            limit=params.get("limit", 100),
            urlContains=params.get("urlContains"),
        ),
        "clear_network": lambda manager, params: manager.clearNetwork(),
        "clear_storage": lambda manager, params: manager.clearStorage(params.get("kinds")),
        # Tab intelligence
        "get_tabs": lambda manager, params: manager.getTabs(),
        # Accessibility & visual QA
        "get_accessibility_tree": lambda manager, params: manager.getAccessibilityTree(
            params.get("interestingOnly", True), params.get("root")
        ),
        "audit_page": lambda manager, params: manager.auditPage(params.get("sampleLimit", 400)),
        "compare_screenshots": lambda manager, params: manager.compareScreenshots(
            params["before"],
            params["after"],
            pixelThreshold=params.get("pixelThreshold", 60),
            saveDiff=params.get("saveDiff", False),
        ),
        # Browser-state snapshot
        "create_snapshot": lambda manager, params: manager.createSnapshot(
            savePath=params.get("savePath")
        ),
        "restore_snapshot": lambda manager, params: manager.restoreSnapshot(
            path=params.get("path"),
            snapshot=params.get("snapshot"),
            navigate=params.get("navigate", True),
        ),
        # Session replay (structured action log)
        "start_session": lambda manager, params: manager.startSession(params.get("name")),
        "stop_session": lambda manager, params: manager.stopSession(),
        "get_session": lambda manager, params: manager.getSession(),
        "save_session": lambda manager, params: manager.saveSession(params.get("path")),
        "load_session": lambda manager, params: manager.loadSession(params["path"]),
        "replay_session": lambda manager, params: manager.replaySession(
            path=params.get("path"),
            delayMs=params.get("delayMs", 500),
            continueOnError=params.get("continueOnError", True),
        ),
        # Browser memory
        "remember_page": lambda manager, params: manager.rememberPage(
            tags=params.get("tags"), withScreenshot=params.get("withScreenshot", True)
        ),
        "search_memory": lambda manager, params: manager.searchMemory(
            params["query"], limit=params.get("limit", 10)
        ),
        "list_memory": lambda manager, params: manager.listMemory(limit=params.get("limit", 50)),
        "clear_memory": lambda manager, params: manager.clearMemory(),
        # Website Skill System
        "discover_page": lambda manager, params: manager.discoverPage(params.get("url")),
        "discover_website": lambda manager, params: manager.discoverWebsite(
            startUrl=params.get("startUrl"), maxPages=params.get("maxPages", 10)
        ),
        "update_skill": lambda manager, params: manager.updateSkill(
            params["url"], success=params.get("success"),
            confidenceDelta=params.get("confidenceDelta"),
        ),
        "list_skills": lambda manager, params: manager.listSkills(params.get("domain")),
        "search_skills": lambda manager, params: manager.searchSkills(
            params["query"], limit=params.get("limit", 20)
        ),
        "export_skills": lambda manager, params: manager.exportSkills(
            params.get("domain"), savePath=params.get("savePath")
        ),
        "import_skills": lambda manager, params: manager.importSkills(
            bundle=params.get("bundle"), path=params.get("path"),
            overwrite=params.get("overwrite", False),
        ),
        "clear_skills": lambda manager, params: manager.clearSkills(params.get("domain")),
        "set_discovery_mode": lambda manager, params: manager.setDiscoveryMode(params["mode"]),
        "get_discovery_status": lambda manager, params: manager.getDiscoveryStatus(),
        # OCR
        "extract_text_from_screenshot": lambda manager, params: manager.extractTextFromScreenshot(
            fullPage=params.get("fullPage", False),
            selector=params.get("selector"),
            lang=params.get("lang", "eng"),
        ),
        "read_image": lambda manager, params: manager.readImage(
            params["source"], lang=params.get("lang", "eng")
        ),
        # AI judgment (provider-backed)
        "verify_goal": lambda manager, params: manager.verifyGoal(
            params["goal"], fullPage=params.get("fullPage", False)
        ),
        "find_element": lambda manager, params: manager.findElement(
            params["description"], limit=params.get("limit", 60)
        ),
        "click_by_description": lambda manager, params: manager.clickByDescription(
            params["description"], limit=params.get("limit", 60), humanize=params.get("humanize")
        ),
        "plan_actions": lambda manager, params: manager.planActions(
            params["goal"], includeContext=params.get("includeContext", True)
        ),
        # Workflows
        "save_workflow": lambda manager, params: manager.saveWorkflow(params["name"]),
        "run_workflow": lambda manager, params: manager.runWorkflow(
            params["name"],
            delayMs=params.get("delayMs", 500),
            continueOnError=params.get("continueOnError", True),
        ),
        "list_workflows": lambda manager, params: manager.listWorkflows(),
        # Browser sessions (pool) — these act on the pool, not a single manager.
        "create_session": lambda manager, params: _async(
            createSession(params.get("sessionId"), makeActive=params.get("makeActive", True))
        ),
        "list_sessions": lambda manager, params: _async(listSessions()),
        "switch_session": lambda manager, params: _async(switchSession(params["sessionId"])),
        "close_session": lambda manager, params: closeSession(params.get("sessionId")),
    }


dispatchTable: dict[str, DispatchFn] = _buildDispatchTable()


async def dispatchAction(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Route *action* with *params* to the browser manager and return its envelope.

    Unknown actions yield an :func:`errorResponse`; missing required params raise
    ``KeyError`` which the caller surfaces as an error envelope too.
    """
    handler = dispatchTable.get(action)
    if handler is None:
        logger.warning("Received unknown WebSocket action '%s'", action)
        return errorResponse(
            error="Unknown action",
            details=f"'{action}' is not a recognised action.",
            action=action,
        )
    manager = getBrowserManager()
    logger.info("WebSocket action: %s", action)
    return await handler(manager, params)


@router.websocket("/ws")
async def websocketEndpoint(websocket: WebSocket) -> None:
    """Accept a connection and dispatch action messages until disconnect.

    Each inbound frame must be JSON of shape ``{"action": str, "params": {...}}``.
    Replies are AI-friendly envelopes. Per-message failures are reported as error
    envelopes so a single bad command never tears down the socket.
    """
    await connectionManager.connect(websocket)
    try:
        while True:
            message: Any = await websocket.receive_json()
            try:
                if not isinstance(message, dict):
                    raise ValueError("Message must be a JSON object.")
                action = message.get("action")
                if not isinstance(action, str) or not action:
                    raise ValueError("Message is missing a string 'action'.")
                params = message.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("'params' must be a JSON object.")
                response = await dispatchAction(action, params)
            except WebSocketDisconnect:
                raise
            except Exception as exc:  # noqa: BLE001 - never crash the socket on bad input
                logger.exception("Failed to handle WebSocket message")
                action = message.get("action") if isinstance(message, dict) else None
                response = errorResponse(
                    error="Failed to process message",
                    details=f"{type(exc).__name__}: {exc}",
                    action=action,
                )
            await connectionManager.sendJson(websocket, response)
    except WebSocketDisconnect:
        connectionManager.disconnect(websocket)
    except Exception:  # noqa: BLE001 - defensive cleanup for unexpected transport errors
        logger.exception("Unexpected WebSocket error; closing connection")
        connectionManager.disconnect(websocket)


def registerWebSocket(app: Any) -> None:
    """Attach the WebSocket router to *app* (used by the server factory)."""
    app.include_router(router)
