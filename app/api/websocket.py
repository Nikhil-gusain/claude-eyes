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

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.browser.browser_manager import BrowserManager, getBrowserManager
from app.utils.helpers import errorResponse
from app.utils.logger import getLogger

logger = getLogger("api.websocket")

router = APIRouter()


class ConnectionManager:
    """Track active WebSocket connections and provide fan-out helpers."""

    def __init__(self) -> None:
        self.activeConnections: list[WebSocket] = []

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


def _buildDispatchTable() -> dict[str, DispatchFn]:
    """Construct the action-name -> manager-call dispatch table."""
    return {
        # Lifecycle
        "open_browser": lambda manager, params: manager.openBrowser(**params),
        "close_browser": lambda manager, params: manager.closeBrowser(),
        "set_headless": lambda manager, params: manager.setHeadless(params.get("headless", True)),
        "clear_profile": lambda manager, params: manager.clearProfile(),
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
        ),
        "double_click": lambda manager, params: manager.doubleClick(
            params["selector"], timeoutMs=params.get("timeoutMs")
        ),
        "right_click": lambda manager, params: manager.rightClick(
            params["selector"], timeoutMs=params.get("timeoutMs")
        ),
        "fill": lambda manager, params: manager.fill(
            params["selector"],
            params["value"],
            clearFirst=params.get("clearFirst", True),
            timeoutMs=params.get("timeoutMs"),
        ),
        "upload_file": lambda manager, params: manager.uploadFile(
            params["selector"], params["filePaths"]
        ),
        "download_file": lambda manager, params: manager.downloadFile(
            params["selector"],
            saveDir=params.get("saveDir"),
            timeoutMs=params.get("timeoutMs"),
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
