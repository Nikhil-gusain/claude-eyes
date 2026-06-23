"""Browser automation core: controller, manager, screenshots, recording."""

from app.browser.playwright_controller import PlaywrightController
from app.browser.screenshot_manager import ScreenshotManager
from app.browser.video_recorder import VideoRecorder
from app.browser.browser_manager import BrowserManager, getBrowserManager

__all__ = [
    "PlaywrightController",
    "ScreenshotManager",
    "VideoRecorder",
    "BrowserManager",
    "getBrowserManager",
]
