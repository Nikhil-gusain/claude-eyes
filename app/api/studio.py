"""Studio dashboard — a live "thinking window" for the Gemini browser agent.

Serves a single static page (``static/studio.html``) and a WebSocket that lets a
human watch the agent reason in plain sentences and steer it mid-task:

- client → server: ``{"type": "start", "task": str, "model"?: str, "maxTurns"?: int}``
                   ``{"type": "feedback", "text": str}``
- server → client: the live events emitted by
  :meth:`app.agents.gemini_adapter.GeminiAdapter.runConversation`
  (``thought`` / ``action`` / ``result`` / ``feedback`` / ``done`` / ``error``).

Each ``start`` builds a **fresh** adapter + feedback queue, so no context leaks
between tasks (per-task isolation). Only one task runs at a time — the browser is
a single shared resource — so a second ``start`` while busy is rejected.
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


@router.get("/studio")
async def studioPage() -> FileResponse:
    """Serve the single-file dashboard."""
    return FileResponse(_STATIC / "studio.html")


async def _runTask(
    websocket: WebSocket,
    task: str,
    model: str | None,
    maxTurns: int,
    feedback: "asyncio.Queue[str]",
) -> None:
    """Drive one Gemini conversation, streaming its events to the socket."""

    async def onEvent(event: dict[str, Any]) -> None:
        await websocket.send_json(event)

    # Imported here (not at module load) so the dashboard route stays importable
    # even if google-genai isn't installed; the error surfaces only on a real run.
    from app.agents.gemini_adapter import GeminiAdapter

    adapter = GeminiAdapter(model)  # fresh adapter per task -> no carried context
    try:
        await adapter.runConversation(task, maxTurns=maxTurns, onEvent=onEvent, feedback=feedback)
    except Exception as exc:  # noqa: BLE001 - already emitted as an 'error' event; log and stop
        logger.warning("Studio task failed: %s", exc)


@router.websocket("/studio/ws")
async def studioWs(websocket: WebSocket) -> None:
    """Bidirectional channel: events out, task/feedback in. One task at a time."""
    await websocket.accept()
    task: asyncio.Task[None] | None = None
    feedback: "asyncio.Queue[str] | None" = None
    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")

            if kind == "start":
                if task is not None and not task.done():
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
                feedback = asyncio.Queue()
                await websocket.send_json({"type": "status", "text": f"Starting: {prompt}"})
                task = asyncio.create_task(
                    _runTask(websocket, prompt, msg.get("model"), int(msg.get("maxTurns") or 12), feedback)
                )

            elif kind == "feedback":
                if task is None or task.done() or feedback is None:
                    await websocket.send_json(
                        {"type": "error", "text": "No task is running to send feedback to."}
                    )
                    continue
                feedback.put_nowait((msg.get("text") or "").strip())

            else:
                await websocket.send_json({"type": "error", "text": f"Unknown message type: {kind!r}"})
    except WebSocketDisconnect:
        logger.info("Studio socket disconnected")
    finally:
        if task is not None and not task.done():
            task.cancel()
