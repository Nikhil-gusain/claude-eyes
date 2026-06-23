"""Central configuration.

All tunables live here so the rest of the codebase never hard-codes paths or
defaults. Values can be overridden through environment variables (prefixed with
``ABC_``) which makes the module friendly to containers and CI.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal


def _envBool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _envInt(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    """Runtime settings resolved once at import time.

    Attributes use camelCase per project convention. Paths are absolute and the
    storage directories are created lazily by :func:`app.utils.helpers.ensureDir`.
    """

    def __init__(self) -> None:
        # ----- Filesystem layout -------------------------------------------
        self.baseDir: Path = Path(__file__).resolve().parents[2]
        self.appDir: Path = self.baseDir / "app"
        self.storageDir: Path = self.appDir / "storage"
        self.screenshotDir: Path = Path(
            os.getenv("ABC_SCREENSHOT_DIR", str(self.storageDir / "screenshots"))
        )
        self.recordingDir: Path = Path(
            os.getenv("ABC_RECORDING_DIR", str(self.storageDir / "recordings"))
        )
        # Persistent browser profile (cookies, tokens, localStorage). Reusing one
        # directory across runs keeps the user logged into sites like Gmail.
        self.userDataDir: Path = Path(
            os.getenv("ABC_USER_DATA_DIR", str(self.storageDir / "browser_profile"))
        )

        # ----- Browser defaults --------------------------------------------
        self.browserType: Literal["chromium", "firefox", "webkit"] = os.getenv(
            "ABC_BROWSER", "chromium"
        )  # type: ignore[assignment]
        self.headless: bool = _envBool("ABC_HEADLESS", True)
        self.viewportWidth: int = _envInt("ABC_VIEWPORT_WIDTH", 1280)
        self.viewportHeight: int = _envInt("ABC_VIEWPORT_HEIGHT", 800)
        self.defaultTimeoutMs: int = _envInt("ABC_TIMEOUT_MS", 30_000)
        self.userAgent: str | None = os.getenv("ABC_USER_AGENT") or None

        # ----- Recording defaults ------------------------------------------
        self.recordingFps: int = _envInt("ABC_RECORDING_FPS", 24)
        self.ffmpegBinary: str = os.getenv("ABC_FFMPEG", "ffmpeg")

        # ----- API server defaults -----------------------------------------
        self.apiHost: str = os.getenv("ABC_HOST", "127.0.0.1")
        self.apiPort: int = _envInt("ABC_PORT", 8000)

        # ----- Logging ------------------------------------------------------
        self.logLevel: str = os.getenv("ABC_LOG_LEVEL", "INFO").upper()

    def asDict(self) -> dict:
        """Return a JSON-serialisable snapshot of the active settings."""
        return {
            "browserType": self.browserType,
            "headless": self.headless,
            "viewportWidth": self.viewportWidth,
            "viewportHeight": self.viewportHeight,
            "defaultTimeoutMs": self.defaultTimeoutMs,
            "recordingFps": self.recordingFps,
            "screenshotDir": str(self.screenshotDir),
            "recordingDir": str(self.recordingDir),
            "userDataDir": str(self.userDataDir),
            "apiHost": self.apiHost,
            "apiPort": self.apiPort,
            "logLevel": self.logLevel,
        }


# A single shared instance is imported everywhere.
settings = Settings()
