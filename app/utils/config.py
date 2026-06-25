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
        # directory across runs keeps the user logged into sites like Gmail. This
        # stays the *default* profile path; named multi-profiles live under
        # ``profilesDir`` (see below) and are resolved by ``ProfileManager``.
        self.userDataDir: Path = Path(
            os.getenv("ABC_USER_DATA_DIR", str(self.storageDir / "browser_profile"))
        )
        # Root holding one persistent user-data dir per named profile, plus the
        # small JSON pointer recording which profile is currently active. The
        # active choice survives process restarts so the same profile reopens for
        # the user even after a chat ends and a new prompt starts.
        self.profilesDir: Path = Path(
            os.getenv("ABC_PROFILES_DIR", str(self.storageDir / "profiles"))
        )
        self.activeProfileFile: Path = Path(
            os.getenv("ABC_ACTIVE_PROFILE_FILE", str(self.storageDir / "active_profile.json"))
        )

        # ----- Browser defaults --------------------------------------------
        self.browserType: Literal["chromium", "firefox", "webkit"] = os.getenv(
            "ABC_BROWSER", "chromium"
        )  # type: ignore[assignment]
        # Optional real-browser channel (e.g. "chrome", "msedge"). Driving the
        # user's installed Chrome instead of bundled Chromium is harder for
        # bot-detection to flag; ``None`` uses Playwright's bundled engine.
        self.browserChannel: str | None = os.getenv("ABC_BROWSER_CHANNEL") or None
        self.headless: bool = _envBool("ABC_HEADLESS", True)
        self.viewportWidth: int = _envInt("ABC_VIEWPORT_WIDTH", 1280)
        self.viewportHeight: int = _envInt("ABC_VIEWPORT_HEIGHT", 800)
        self.defaultTimeoutMs: int = _envInt("ABC_TIMEOUT_MS", 30_000)
        self.userAgent: str | None = os.getenv("ABC_USER_AGENT") or None

        # ----- Humanization (anti bot-detection) ---------------------------
        # When on, typing is paced (~``typingWpm`` words/min with jitter), the
        # cursor travels a curved, wiggling path before clicking, and scrolling
        # is incremental rather than an instant jump. Disable for raw speed.
        self.humanize: bool = _envBool("ABC_HUMANIZE", True)
        self.typingWpm: int = _envInt("ABC_TYPING_WPM", 25)
        # Stealth shrinks the *automation fingerprint* (navigator.webdriver, the
        # --enable-automation switch, the "controlled by automated software"
        # banner, missing window.chrome). Humanization covers behaviour; this
        # covers the fingerprint hardened sites read. Best with channel=chrome.
        self.stealth: bool = _envBool("ABC_STEALTH", True)

        # ----- Event-driven waiting ----------------------------------------
        # Hard ceiling for "wait quietly for a slow thing" (e.g. an online AI's
        # streamed answer). Downloads may legitimately take far longer.
        self.maxWaitMs: int = _envInt("ABC_MAX_WAIT_MS", 300_000)  # 5 minutes
        self.maxDownloadWaitMs: int = _envInt("ABC_MAX_DOWNLOAD_WAIT_MS", 3_600_000)  # 1 hour

        # ----- No-image mode (MarkItDown) ----------------------------------
        # When on, the browser favours text: pixel screenshots are suppressed and
        # media (images/PDF/…) is converted to markdown via MarkItDown instead.
        self.noImageMode: bool = _envBool("ABC_NO_IMAGE_MODE", False)

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
            "browserChannel": self.browserChannel,
            "headless": self.headless,
            "viewportWidth": self.viewportWidth,
            "viewportHeight": self.viewportHeight,
            "defaultTimeoutMs": self.defaultTimeoutMs,
            "humanize": self.humanize,
            "typingWpm": self.typingWpm,
            "stealth": self.stealth,
            "maxWaitMs": self.maxWaitMs,
            "maxDownloadWaitMs": self.maxDownloadWaitMs,
            "noImageMode": self.noImageMode,
            "recordingFps": self.recordingFps,
            "screenshotDir": str(self.screenshotDir),
            "recordingDir": str(self.recordingDir),
            "userDataDir": str(self.userDataDir),
            "profilesDir": str(self.profilesDir),
            "apiHost": self.apiHost,
            "apiPort": self.apiPort,
            "logLevel": self.logLevel,
        }


# A single shared instance is imported everywhere.
settings = Settings()
