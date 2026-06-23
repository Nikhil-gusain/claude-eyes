"""Session recording built on Playwright's native context video capture.

Playwright records video per *browser context* as WebM. To honour an explicit
``startRecording``/``stopRecording`` API we (re)create the active context with
``record_video_dir`` enabled on start, then on stop we close the context to
flush the WebM.

Encoding strategy (robust by design):

* If a *capable* ``ffmpeg`` is available, transcode the WebM to an H.264 MP4.
* Otherwise keep the native WebM — it is already a fully playable file (Chrome,
  Firefox, VLC) — and return it with a truthful ``.webm`` extension rather than
  mislabeling WebM bytes as ``.mp4``.

Duration is measured by wall-clock time between start and stop, so it never
depends on OpenCV/ffprobe being installed. Resolution comes from the viewport
(optionally refined by OpenCV when present).

Returns the spec contract::

    {"video_path": ..., "duration": ..., "resolution": ..., "fps": ...}
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from app.utils.config import settings
from app.utils.error_handler import RecordingError
from app.utils.helpers import ensureDir, generateSessionName
from app.utils.logger import getLogger

logger = getLogger("browser.recorder")


class VideoRecorder:
    """Coordinates context-level video capture and optional MP4 transcoding."""

    def __init__(self, outputDir: Path | None = None) -> None:
        self.outputDir: Path = ensureDir(outputDir or settings.recordingDir)
        self.isRecording: bool = False
        self.sessionName: Optional[str] = None
        self.fps: int = settings.recordingFps
        self._rawDir: Optional[Path] = None
        self._startedAt: Optional[float] = None

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    async def start(self, controller: Any, fps: int | None = None, sessionName: str | None = None) -> dict[str, Any]:
        """Begin recording by re-creating the active context with video on.

        ``controller`` is a :class:`PlaywrightController`. The current URL is
        preserved across the context swap so recording feels seamless.
        """
        if self.isRecording:
            raise RecordingError("Recording already in progress")
        if not controller.isRunning:
            raise RecordingError("Browser is not running")

        self.fps = fps or settings.recordingFps
        self.sessionName = sessionName or generateSessionName("recording")
        self._rawDir = ensureDir(self.outputDir / f"{self.sessionName}-raw")

        currentUrl = controller.activePage.url
        await controller.beginVideoContext(self._rawDir)
        # Capture begins as soon as the recording context exists.
        self._startedAt = time.monotonic()
        if currentUrl and currentUrl != "about:blank":
            await controller.openUrl(currentUrl)

        self.isRecording = True
        logger.info("Recording started (session=%s, fps=%d)", self.sessionName, self.fps)
        return {
            "recording": True,
            "session_name": self.sessionName,
            "fps": self.fps,
        }

    async def stop(self, controller: Any) -> dict[str, Any]:
        """Stop recording, flush the file, and return its metadata."""
        if not self.isRecording:
            raise RecordingError("No recording in progress")

        viewportResolution = f"{controller.viewportWidth}x{controller.viewportHeight}"
        durationSeconds = 0.0
        if self._startedAt is not None:
            durationSeconds = max(0.0, time.monotonic() - self._startedAt)

        webmPath = await controller.endVideoContext()
        self.isRecording = False

        if webmPath is None or not Path(webmPath).exists():
            raise RecordingError("No video file was produced by the browser context")

        finalPath = await self._finalizeVideo(Path(webmPath))

        # Tidy up the raw working directory.
        if self._rawDir and self._rawDir.exists():
            shutil.rmtree(self._rawDir, ignore_errors=True)

        probedResolution = self._probeResolution(finalPath) or viewportResolution
        logger.info(
            "Recording saved %s (%.2fs, %s)", finalPath.name, durationSeconds, probedResolution
        )
        return {
            "video_path": str(finalPath),
            "duration": round(durationSeconds, 2),
            "resolution": probedResolution,
            "fps": self.fps,
        }

    # ------------------------------------------------------------------ #
    # Encoding
    # ------------------------------------------------------------------ #
    async def _finalizeVideo(self, source: Path) -> Path:
        """Produce the final artifact: MP4 if possible, otherwise native WebM."""
        binary = settings.ffmpegBinary
        ffmpegAvailable = bool(shutil.which(binary)) or Path(binary).exists()

        if ffmpegAvailable:
            mp4Path = self.outputDir / f"{self.sessionName}.mp4"
            if await self._transcodeToMp4(source, mp4Path):
                if mp4Path.exists() and mp4Path.stat().st_size > 1024:
                    return mp4Path
            logger.warning(
                "ffmpeg present but could not produce a valid MP4; keeping native WebM"
            )

        # Fallback: the WebM Playwright produced is already a playable file.
        webmPath = self.outputDir / f"{self.sessionName}.webm"
        shutil.copy(source, webmPath)
        return webmPath

    async def _transcodeToMp4(self, source: Path, destination: Path) -> bool:
        """Re-encode WebM -> H.264 MP4 at the configured FPS. Returns success.

        Deliberately avoids ``-movflags`` and other options that minimal ffmpeg
        builds (e.g. the one bundled with Playwright) reject.
        """
        cmd = [
            settings.ffmpegBinary,
            "-y",
            "-i",
            str(source),
            "-r",
            str(self.fps),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(destination),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except Exception as exc:  # noqa: BLE001 - ffmpeg may be missing/incompatible
            logger.warning("ffmpeg invocation failed: %s", exc)
            return False

        if process.returncode != 0:
            tail = stderr.decode(errors="ignore").strip().splitlines()[-1:] if stderr else []
            logger.warning("ffmpeg transcode failed: %s", " ".join(tail))
            return False
        return True

    @staticmethod
    def _probeResolution(path: Path) -> Optional[str]:
        """Best-effort resolution probe via OpenCV; ``None`` when unavailable."""
        try:
            import cv2  # type: ignore

            capture = cv2.VideoCapture(str(path))
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            capture.release()
            if width and height:
                return f"{width}x{height}"
        except Exception as exc:  # noqa: BLE001
            logger.debug("OpenCV resolution probe unavailable: %s", exc)
        return None
