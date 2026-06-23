"""Small, dependency-free helpers shared across layers.

The most important pieces here are :func:`successResponse` and
:func:`errorResponse` which build the *AI-friendly* envelopes every tool,
endpoint, and adapter returns. Keeping the shape in one place guarantees Claude,
OpenAI, and raw HTTP clients all see identical structures.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcTimestamp() -> str:
    """ISO-8601 timestamp in UTC, e.g. ``2026-06-22T10:15:30.123456+00:00``."""
    return datetime.now(timezone.utc).isoformat()


def ensureDir(path: Path | str) -> Path:
    """Create *path* (and parents) if missing and return it as a ``Path``."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def generateSessionName(prefix: str = "session") -> str:
    """Build a filesystem-safe, time-ordered session identifier."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    safePrefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", prefix).strip("-") or "session"
    return f"{safePrefix}-{stamp}"


def successResponse(action: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Standard success envelope returned to any AI agent.

    Shape::

        {"success": True, "action": ..., "timestamp": ..., "data": {...}}
    """
    return {
        "success": True,
        "action": action,
        "timestamp": utcTimestamp(),
        "data": data or {},
    }


def errorResponse(error: str, details: str = "", action: str | None = None) -> dict[str, Any]:
    """Standard error envelope returned to any AI agent.

    Shape::

        {"success": False, "error": ..., "details": ...}
    """
    payload: dict[str, Any] = {
        "success": False,
        "error": error,
        "details": details,
        "timestamp": utcTimestamp(),
    }
    if action is not None:
        payload["action"] = action
    return payload
