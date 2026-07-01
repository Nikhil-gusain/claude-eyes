"""Structured session recording — capture actions as replayable JSON.

Video recording (see :class:`app.browser.video_recorder.VideoRecorder`) gives a
human something to *watch*; this gives an agent something to *replay*. While a
session is recording, every replayable action the :class:`BrowserManager` runs is
appended here as a structured step::

    {"action": "click", "params": {"selector": "#login"}, "timestamp": ..., "offsetMs": 123}

The whole session serialises to JSON, so an agent can:

* reproduce a bug deterministically,
* turn a one-off exploration into a reusable workflow,
* audit exactly what it did and when.

This class is pure bookkeeping — it never touches the browser. The manager owns a
single instance and decides which actions are replayable; replay itself lives in
:meth:`BrowserManager.replaySession`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.utils.config import settings
from app.utils.helpers import ensureDir, generateSessionName, utcTimestamp
from app.utils.logger import getLogger

logger = getLogger("browser.session")


class SessionRecorder:
    """Records a chronological, replayable log of browser actions."""

    def __init__(self, outputDir: Path | None = None) -> None:
        self.outputDir: Path = ensureDir(outputDir or settings.sessionDir)
        self.recording: bool = False
        # ``paused`` lets replay run actions through the manager without those
        # replayed steps being appended back onto the log (which would otherwise
        # grow the session on every replay).
        self.paused: bool = False
        self.name: str | None = None
        self.startedAt: str | None = None
        self._startMonotonic: float = 0.0
        self.steps: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Recording lifecycle
    # ------------------------------------------------------------------ #
    def start(self, name: str | None = None) -> dict[str, Any]:
        """Begin a fresh recording, discarding any previous steps."""
        self.name = name or generateSessionName("session")
        self.startedAt = utcTimestamp()
        self._startMonotonic = time.monotonic()
        self.steps = []
        self.recording = True
        self.paused = False
        logger.info("Session recording started: %s", self.name)
        return {"recording": True, "name": self.name, "startedAt": self.startedAt}

    def record(self, action: str, params: dict[str, Any]) -> None:
        """Append one replayable step, if actively recording (and not paused)."""
        if not self.recording or self.paused:
            return
        self.steps.append(
            {
                "action": action,
                "params": {k: v for k, v in params.items() if v is not None},
                "timestamp": utcTimestamp(),
                "offsetMs": int((time.monotonic() - self._startMonotonic) * 1000),
            }
        )

    def stop(self) -> dict[str, Any]:
        """Stop recording and return a summary (the steps remain in memory)."""
        self.recording = False
        logger.info("Session recording stopped: %s (%d steps)", self.name, len(self.steps))
        return self._summary()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def save(self, path: str | None = None) -> dict[str, Any]:
        """Write the current session to disk as JSON and return its location."""
        if not self.steps and not self.name:
            raise ValueError("No session to save. Call start_session and run some actions first.")
        target = Path(path) if path else self.outputDir / f"{self.name or generateSessionName()}.json"
        ensureDir(target.parent)
        document = {
            "name": self.name,
            "startedAt": self.startedAt,
            "savedAt": utcTimestamp(),
            "stepCount": len(self.steps),
            "steps": self.steps,
        }
        target.write_text(json.dumps(document, indent=2), encoding="utf-8")
        logger.info("Saved session %s (%d steps) -> %s", self.name, len(self.steps), target)
        return {"path": str(target), "stepCount": len(self.steps), "name": self.name}

    def load(self, path: str) -> dict[str, Any]:
        """Load a saved session from disk into memory (does not start recording)."""
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError(f"Session file not found: {path}")
        document = json.loads(source.read_text(encoding="utf-8"))
        self.steps = list(document.get("steps", []))
        self.name = document.get("name")
        self.startedAt = document.get("startedAt")
        self.recording = False
        logger.info("Loaded session %s (%d steps) from %s", self.name, len(self.steps), path)
        return {"path": str(source), "stepCount": len(self.steps), "name": self.name}

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def _summary(self) -> dict[str, Any]:
        return {
            "recording": self.recording,
            "name": self.name,
            "startedAt": self.startedAt,
            "stepCount": len(self.steps),
            "steps": self.steps,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the current session state (steps + metadata)."""
        return self._summary()
