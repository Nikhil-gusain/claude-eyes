"""Inbound command schemas.

Every HTTP endpoint and WebSocket message validates its payload against one of
these models before touching the browser. Field identifiers use camelCase to
match project convention; Pydantic still serialises them verbatim.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class LaunchCommand(BaseModel):
    """Parameters for launching a browser instance."""

    browserType: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    viewportWidth: int = Field(default=1280, ge=240, le=7680)
    viewportHeight: int = Field(default=800, ge=240, le=4320)
    userAgent: Optional[str] = None
    profile: Optional[str] = Field(
        default=None, description="Profile name or 'random'; omit to use the active one."
    )
    channel: Optional[str] = Field(
        default=None, description="Real-browser channel, e.g. 'chrome'."
    )


class ProfileSelectCommand(BaseModel):
    name: str = Field(..., description="Profile name to activate, or 'random'.")


class ProfileCreateCommand(BaseModel):
    name: str
    makeActive: bool = True


class LoginSessionCommand(BaseModel):
    """Open a headed browser on a profile for manual login/sign-up."""

    profile: Optional[str] = None
    url: Optional[str] = None


class WaitStableCommand(BaseModel):
    """Wait until a selector's text stops changing (e.g. a streamed AI answer)."""

    selector: str
    stableMs: int = Field(default=1200, ge=100, le=60_000)
    timeoutMs: Optional[int] = Field(default=None, ge=0, le=3_600_000)


class WaitResponseCommand(BaseModel):
    """Wait until a matching network response finishes streaming."""

    urlPattern: str
    timeoutMs: Optional[int] = Field(default=None, ge=0, le=3_600_000)


class MarkdownCommand(BaseModel):
    source: str = Field(..., description="Local file path or URL to convert.")


class NoImageModeCommand(BaseModel):
    enabled: bool = True


class NavigateCommand(BaseModel):
    url: str = Field(..., description="Absolute URL including scheme.")
    waitUntil: Literal["load", "domcontentloaded", "networkidle", "commit"] = "networkidle"
    timeoutMs: Optional[int] = Field(default=None, ge=0, le=300_000)


class TabCommand(BaseModel):
    """Tab management — index is required for switch/close operations."""

    index: Optional[int] = Field(default=None, ge=0)
    url: Optional[str] = None


class ClickCommand(BaseModel):
    selector: str
    timeoutMs: Optional[int] = Field(default=None, ge=0, le=300_000)
    button: Literal["left", "right", "middle"] = "left"
    clickCount: int = Field(default=1, ge=1, le=3)
    humanize: Optional[bool] = Field(
        default=None, description="Force human-like (true) or instant (false) clicking."
    )


class FillCommand(BaseModel):
    selector: str
    value: str
    clearFirst: bool = True
    timeoutMs: Optional[int] = Field(default=None, ge=0, le=300_000)
    humanize: Optional[bool] = Field(
        default=None, description="Force human-paced (true) or instant (false) typing."
    )


class HoverCommand(BaseModel):
    selector: str
    timeoutMs: Optional[int] = Field(default=None, ge=0, le=300_000)


class ScrollCommand(BaseModel):
    """Scroll by a pixel delta, to an element, or to an absolute position."""

    selector: Optional[str] = None
    deltaY: int = 0
    deltaX: int = 0
    toBottom: bool = False
    toTop: bool = False
    humanize: Optional[bool] = Field(
        default=None, description="Force lazy human-like (true) or instant (false) scrolling."
    )


class UploadCommand(BaseModel):
    selector: str
    filePaths: list[str] = Field(..., min_length=1)


class DownloadCommand(BaseModel):
    selector: str
    saveDir: Optional[str] = None
    timeoutMs: Optional[int] = Field(default=None, ge=0, le=3_600_000)
    imagesOnly: bool = Field(
        default=True, description="Keep only verified real images (blocks disguised apps)."
    )


class PressKeysCommand(BaseModel):
    """Press a key or chord, optionally focused on a selector."""

    keys: str = Field(..., description="Playwright key syntax, e.g. 'Control+A'.")
    selector: Optional[str] = None


class WaitForElementCommand(BaseModel):
    selector: str
    state: Literal["attached", "detached", "visible", "hidden"] = "visible"
    timeoutMs: Optional[int] = Field(default=None, ge=0, le=300_000)


class ScreenshotCommand(BaseModel):
    fullPage: bool = False
    selector: Optional[str] = Field(
        default=None, description="If set, capture only this element."
    )
    annotate: bool = Field(
        default=False, description="Overlay a label/box highlighting the selector."
    )
    label: Optional[str] = None


class RecordingCommand(BaseModel):
    fps: Optional[int] = Field(default=None, ge=1, le=60)
    sessionName: Optional[str] = None


class ExtractCommand(BaseModel):
    """Generic extraction request used by the unified extract endpoint."""

    kind: Literal[
        "text", "links", "buttons", "forms", "images", "dom", "title", "url"
    ]
    selector: Optional[str] = None
