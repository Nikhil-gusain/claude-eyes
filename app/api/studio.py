"""Studio dashboard — a live "thinking window" for the Gemini browser agent.

Serves a single static page (``static/studio.html``) and a WebSocket that lets a
human watch the agent reason in plain sentences and steer it mid-task:

- client → server: ``{"type": "start", "task": str, "model"?: str, "maxTurns"?: int}``
                   ``{"type": "feedback", "text": str}``
- server → client: the live events emitted by
  :meth:`app.agents.gemini_adapter.GeminiAdapter.runConversation`
  (``thought`` / ``action`` / ``result`` / ``feedback`` / ``status`` / ``done`` / ``error``).

The running task lives in a module-level singleton (:class:`_ActiveRun`), not on
any one WebSocket connection, so a task keeps running if the connection that
started it drops — tab closed/refreshed, laptop slept, network blip. A run can
outlive any single browser tab. Reconnecting re-attaches to whatever is still
running and replays its full event log so the UI catches up. Only one task runs
at a time — the browser is a single shared resource — so a second ``start``
while busy is rejected.

ponytail: this state lives in process memory only. It survives a dropped
WebSocket, not a server restart — there's no on-disk persistence of an
in-flight conversation. Add that if a run needs to outlive the server process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.utils.logger import getLogger

logger = getLogger("api.studio")

router = APIRouter()

_STATIC = Path(__file__).parent / "static"


class _ActiveRun:
    """The one in-flight task, decoupled from any particular WebSocket.

    ``socket`` is whichever connection is currently watching, or ``None``
    between a disconnect and a reconnect — events keep appending to ``events``
    regardless, so a later reconnect can replay everything that was missed.
    """

    def __init__(self, feedback: "asyncio.Queue[str]") -> None:
        self.feedback = feedback
        self.events: list[dict[str, Any]] = []
        self.socket: WebSocket | None = None
        self.task: asyncio.Task[None] | None = None


_active: _ActiveRun | None = None


@router.get("/studio")
async def studioPage() -> FileResponse:
    """Serve the single-file dashboard."""
    return FileResponse(_STATIC / "studio.html")


async def _runTask(run: "_ActiveRun", task: str, model: str | None, maxTurns: int) -> None:
    """Drive one Gemini conversation; buffer events and forward to whatever socket is attached."""

    async def onEvent(event: dict[str, Any]) -> None:
        run.events.append(event)
        if run.socket is not None:
            await run.socket.send_json(event)

    # Imported here (not at module load) so the dashboard route stays importable
    # even if google-genai isn't installed; the error surfaces only on a real run.
    from app.agents.gemini_adapter import GeminiAdapter

    adapter = GeminiAdapter(model)  # fresh adapter per task -> no carried context
    try:
        await adapter.runConversation(task, maxTurns=maxTurns, onEvent=onEvent, feedback=run.feedback)
    except Exception as exc:  # noqa: BLE001 - already emitted as an 'error' event; log and stop
        logger.warning("Studio task failed: %s", exc)
    finally:
        global _active
        if _active is run:
            _active = None


@router.websocket("/studio/ws")
async def studioWs(websocket: WebSocket) -> None:
    """Bidirectional channel: events out, task/feedback in. One task at a time.

    A task started here keeps running (see :class:`_ActiveRun`) even after this
    connection closes — on disconnect we only detach the socket, never cancel
    the task. A fresh connection while a task is running re-attaches to it and
    replays the buffered log instead of starting anything new.
    """
    global _active
    await websocket.accept()

    if _active is not None and _active.task is not None and not _active.task.done():
        _active.socket = websocket
        await websocket.send_json(
            {"type": "status", "text": "Reattached to a task already in progress — replaying its log…"}
        )
        for event in _active.events:
            await websocket.send_json(event)

    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")

            if kind == "start":
                if _active is not None and _active.task is not None and not _active.task.done():
                    await websocket.send_json(
                        {"type": "error", "text": "A task is already running — let it finish first."}
                    )
                    continue
                prompt = (msg.get("task") or "").strip()
                if not prompt:
                    await websocket.send_json({"type": "error", "text": "Please enter a task."})
                    continue
                provider = (msg.get("provider") or "gemini").lower()
                if provider != "gemini":
                    await websocket.send_json(
                        {"type": "error", "text": "Studio currently drives the Gemini agent only."}
                    )
                    continue
                # Fresh per-task state — this is the per-task context isolation.
                run = _ActiveRun(asyncio.Queue())
                run.socket = websocket
                _active = run
                await websocket.send_json({"type": "status", "text": f"Starting: {prompt}"})
                run.task = asyncio.create_task(
                    _runTask(run, prompt, msg.get("model"), int(msg.get("maxTurns") or 100_000))
                )

            elif kind == "feedback":
                if _active is None or _active.task is None or _active.task.done():
                    await websocket.send_json(
                        {"type": "error", "text": "No task is running to send feedback to."}
                    )
                    continue
                _active.feedback.put_nowait((msg.get("text") or "").strip())

            else:
                await websocket.send_json({"type": "error", "text": f"Unknown message type: {kind!r}"})
    except WebSocketDisconnect:
        logger.info("Studio socket disconnected")
    finally:
        # Detach only — the background task (if any) keeps running so a tab
        # close/refresh/sleep doesn't kill a long task; see module docstring.
        if _active is not None and _active.socket is websocket:
            _active.socket = None
