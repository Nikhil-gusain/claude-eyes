"""Outbound response schemas.

These mirror the envelopes produced by :mod:`app.utils.helpers`. They are used
for OpenAPI documentation and for any caller that wants typed access to results.
The JSON contract keys intentionally follow the spec (e.g. ``video_path``).
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ActionResponse(BaseModel):
    """Standard success envelope."""

    success: bool = True
    action: str
    timestamp: str
    data: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Standard error envelope."""

    success: bool = False
    error: str
    details: str = ""
    timestamp: Optional[str] = None
    action: Optional[str] = None


class ScreenshotResult(BaseModel):
    """Payload returned inside ``data`` for any screenshot action."""

    path: str
    timestamp: str
    url: str
    width: int
    height: int


class RecordingResult(BaseModel):
    """Payload returned inside ``data`` when recording stops.

    Contract keys follow the spec exactly (``video_path``, ``duration``, ``fps``).
    """

    video_path: str
    duration: float
    resolution: str
    fps: int


class PageInfo(BaseModel):
    """Lightweight description of the active page/tab."""

    title: str
    url: str
    tabIndex: int
    tabCount: int
