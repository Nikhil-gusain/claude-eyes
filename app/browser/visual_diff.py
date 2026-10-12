"""Pixel-level visual diffing of two screenshots.

``take_screenshot`` lets an agent *see* a page; this lets it *compare* two
captures and quantify what changed — the difference between "I took a screenshot"
and "I can tell the submit button moved". It answers:

* how different are these two images, as a percentage of pixels?
* which regions changed (returned as a coarse grid of bounding boxes)?

It is deliberately DOM-free: it works on any two PNG/JPEG files, so it composes
with the existing screenshot tooling and needs no live browser. Element-level
added/removed reporting is intentionally out of scope here — that needs a DOM
diff, which :func:`BrowserManager.auditPage` / accessibility extraction cover.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

from app.utils.config import settings
from app.utils.helpers import ensureDir, utcTimestamp
from app.utils.logger import getLogger

logger = getLogger("browser.visualdiff")

# A changed pixel is one whose summed per-channel delta exceeds this (0-765).
# ~10% of full range keeps anti-aliasing / sub-pixel noise from counting.
_DEFAULT_PIXEL_THRESHOLD = 60
# The change map is coarsened to this many cells per axis; a cell counts as a
# "changed region" once enough of its pixels changed. Keeps regions meaningful
# rather than reporting thousands of single-pixel specks.
_GRID = 16
_CELL_TRIGGER_RATIO = 0.02


def compareImages(
    beforePath: str,
    afterPath: str,
    pixelThreshold: int = _DEFAULT_PIXEL_THRESHOLD,
    saveDiff: bool = False,
) -> dict[str, Any]:
    """Compare two image files and quantify their visual difference.

    Returns ``visualDifferencePercent`` (share of pixels that changed),
    ``changedRegions`` (count of coarse grid cells that changed) with their
    bounding boxes, and the compared dimensions. When *saveDiff* is set, a
    heat-map style diff PNG is written under ``settings.diffDir`` and its path
    returned. Images of differing sizes are aligned to the smaller common size
    before comparison so the call never errors on a resize.
    """
    before = _open(beforePath)
    after = _open(afterPath)

    # Align to the smaller common box so the pixel grids line up. A size change
    # is itself a real visual difference, so we report it.
    width = min(before.width, after.width)
    height = min(before.height, after.height)
    resized = before.size != after.size
    beforeC = before.crop((0, 0, width, height))
    afterC = after.crop((0, 0, width, height))

    diff = ImageChops.difference(beforeC, afterC)
    # Sum the channel deltas per pixel -> single-band intensity image.
    intensity = diff.convert("L") if diff.mode == "L" else _sumChannels(diff)

    totalPixels = width * height
    changedPixels = 0
    cellW = max(1, width // _GRID)
    cellH = max(1, height // _GRID)
    regions: list[dict[str, int]] = []

    pixels = intensity.load()
    cellChanged: dict[tuple[int, int], int] = {}
    for y in range(height):
        for x in range(width):
            if pixels[x, y] >= pixelThreshold:
                changedPixels += 1
                key = (x // cellW, y // cellH)
                cellChanged[key] = cellChanged.get(key, 0) + 1

    cellArea = cellW * cellH
    for (cx, cy), count in sorted(cellChanged.items()):
        if count >= cellArea * _CELL_TRIGGER_RATIO:
            regions.append(
                {
                    "x": cx * cellW,
                    "y": cy * cellH,
                    "width": cellW,
                    "height": cellH,
                    "changedPixels": count,
                }
            )

    percent = round((changedPixels / totalPixels) * 100, 2) if totalPixels else 0.0
    result: dict[str, Any] = {
        "visualDifferencePercent": percent,
        "changedRegions": len(regions),
        "regions": regions,
        "changedPixels": changedPixels,
        "comparedWidth": width,
        "comparedHeight": height,
        "resized": resized,
        "identical": changedPixels == 0,
        # The DOM-level added/removed split needs a DOM diff, not pixels; surfaced
        # honestly as empty here so callers don't mistake "[]" for "nothing added".
        "addedElements": [],
        "removedElements": [],
    }

    if saveDiff:
        result["diffPath"] = _saveHeatmap(intensity, pixelThreshold)
    logger.info(
        "Visual diff: %.2f%% changed across %d regions (%dx%d)",
        percent, len(regions), width, height,
    )
    return result


def _open(path: str) -> Image.Image:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Image not found: {path}")
    return Image.open(p).convert("RGB")


def _sumChannels(rgb: Image.Image) -> Image.Image:
    """Collapse an RGB difference image to a single intensity band (sum of channels)."""
    r, g, b = rgb.split()
    return ImageChops.add(ImageChops.add(r, g), b)


def _saveHeatmap(intensity: Image.Image, threshold: int) -> str:
    """Write a black/white mask of changed pixels and return its path."""
    ensureDir(settings.diffDir)
    mask = intensity.point(lambda v: 255 if v >= threshold else 0)
    target = settings.diffDir / f"diff-{utcTimestamp().replace(':', '-')}.png"
    mask.save(target)
    return str(target)
