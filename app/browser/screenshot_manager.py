"""Screenshot capture with optional annotation.

Supports full-page, viewport and single-element captures. When ``annotate`` is
requested the element's bounding box is drawn onto the saved PNG using Pillow so
an AI reviewer can see exactly which region a selector resolved to.

Every capture returns the spec contract::

    {"path": ..., "timestamp": ..., "url": ..., "width": ..., "height": ...}
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Page

from app.utils.config import settings
from app.utils.helpers import ensureDir, utcTimestamp
from app.utils.logger import getLogger

logger = getLogger("browser.screenshot")


class ScreenshotManager:
    """Captures and persists screenshots for a given page."""

    def __init__(self, outputDir: Path | None = None) -> None:
        self.outputDir: Path = ensureDir(outputDir or settings.screenshotDir)

    async def capture(
        self,
        page: Page,
        fullPage: bool = False,
        selector: Optional[str] = None,
        annotate: bool = False,
        label: Optional[str] = None,
    ) -> dict[str, Any]:
        """Capture a screenshot and return its metadata envelope payload."""
        timestamp = utcTimestamp()
        fileName = f"screenshot-{timestamp.replace(':', '-')}.png"
        path = self.outputDir / fileName

        if selector:
            element = await page.query_selector(selector)
            if element is None:
                raise ValueError(f"Selector not found: {selector}")
            await element.screenshot(path=str(path))
        else:
            await page.screenshot(path=str(path), full_page=fullPage)

        if annotate and selector:
            await self._annotateElement(page, selector, path, label)

        width, height = self._imageSize(path)
        result = {
            "path": str(path),
            "timestamp": timestamp,
            "url": page.url,
            "width": width,
            "height": height,
        }
        logger.info("Saved screenshot %s (%dx%d)", path.name, width, height)
        return result

    async def captureBytes(
        self,
        page: Page,
        fullPage: bool = False,
        selector: Optional[str] = None,
    ) -> dict[str, Any]:
        """Capture a screenshot **in memory** and return raw PNG bytes.

        Nothing is written to disk — this is the ephemeral path used when an AI
        only needs to *look* at the page (e.g. UI/UX inspection) without leaving
        files behind. Returns ``{"image": <bytes>, "timestamp", "url", "width",
        "height"}``.
        """
        timestamp = utcTimestamp()
        if selector:
            element = await page.query_selector(selector)
            if element is None:
                raise ValueError(f"Selector not found: {selector}")
            data = await element.screenshot()
        else:
            data = await page.screenshot(full_page=fullPage)

        width, height = self._bytesSize(data)
        logger.info("Captured ephemeral screenshot (%dx%d, %d bytes)", width, height, len(data))
        return {
            "image": data,
            "timestamp": timestamp,
            "url": page.url,
            "width": width,
            "height": height,
        }

    @staticmethod
    def _bytesSize(data: bytes) -> tuple[int, int]:
        try:
            with Image.open(BytesIO(data)) as img:
                return img.width, img.height
        except Exception:  # noqa: BLE001 - size is informational only
            return 0, 0

    async def _annotateElement(
        self, page: Page, selector: str, path: Path, label: Optional[str]
    ) -> None:
        """Draw a red box (and optional label) around the selector on a full shot."""
        box = await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return { x: r.x, y: r.y, w: r.width, h: r.height };
            }""",
            selector,
        )
        if not box:
            return

        try:
            with Image.open(path).convert("RGB") as img:
                draw = ImageDraw.Draw(img)
                x0, y0 = box["x"], box["y"]
                x1, y1 = x0 + box["w"], y0 + box["h"]
                draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=3)
                caption = label or selector
                self._drawLabel(draw, caption, x0, max(0, y0 - 18))
                img.save(path)
        except Exception as exc:  # noqa: BLE001 - annotation is best-effort
            logger.warning("Annotation failed for %s: %s", selector, exc)

    @staticmethod
    def _drawLabel(draw: "ImageDraw.ImageDraw", text: str, x: float, y: float) -> None:
        try:
            font: Any = ImageFont.load_default()
        except Exception:  # noqa: BLE001
            font = None
        draw.rectangle([x, y, x + 8 * len(text) + 6, y + 16], fill=(255, 0, 0))
        draw.text((x + 3, y + 2), text, fill=(255, 255, 255), font=font)

    @staticmethod
    def _imageSize(path: Path) -> tuple[int, int]:
        with Image.open(path) as img:
            return img.width, img.height
