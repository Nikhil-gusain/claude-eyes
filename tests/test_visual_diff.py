"""Unit tests for the pixel-level visual-diff engine.

These are browser-free: they build small images with Pillow and assert the diff
math (percentage changed, region detection, identical/resized handling).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PIL.Image", reason="Pillow not installed")

from PIL import Image  # noqa: E402

from app.browser import visual_diff  # noqa: E402


def _save(path: Path, color, size=(64, 64)) -> str:
    Image.new("RGB", size, color).save(path)
    return str(path)


def test_identicalImagesReportZeroDifference(tmp_path: Path) -> None:
    a = _save(tmp_path / "a.png", (10, 120, 200))
    b = _save(tmp_path / "b.png", (10, 120, 200))
    result = visual_diff.compareImages(a, b)
    assert result["identical"] is True
    assert result["visualDifferencePercent"] == 0.0
    assert result["changedRegions"] == 0


def test_fullyDifferentImagesReportNearTotalChange(tmp_path: Path) -> None:
    a = _save(tmp_path / "a.png", (0, 0, 0))
    b = _save(tmp_path / "b.png", (255, 255, 255))
    result = visual_diff.compareImages(a, b)
    assert result["visualDifferencePercent"] == 100.0
    assert result["changedRegions"] > 0
    assert result["identical"] is False


def test_partialChangeIsLocalisedToRegions(tmp_path: Path) -> None:
    base = Image.new("RGB", (64, 64), (255, 255, 255))
    a = tmp_path / "a.png"
    base.save(a)
    # Paint a black square in the top-left quadrant only.
    changed = base.copy()
    for y in range(20):
        for x in range(20):
            changed.putpixel((x, y), (0, 0, 0))
    b = tmp_path / "b.png"
    changed.save(b)

    result = visual_diff.compareImages(str(a), str(b))
    assert 0 < result["visualDifferencePercent"] < 100
    assert result["changedRegions"] >= 1
    # Every reported region should sit within the changed quadrant.
    for region in result["regions"]:
        assert region["x"] < 32 and region["y"] < 32


def test_differingSizesAreAlignedNotErrored(tmp_path: Path) -> None:
    a = _save(tmp_path / "a.png", (50, 50, 50), size=(80, 40))
    b = _save(tmp_path / "b.png", (50, 50, 50), size=(64, 64))
    result = visual_diff.compareImages(a, b)
    assert result["resized"] is True
    assert result["comparedWidth"] == 64
    assert result["comparedHeight"] == 40


def test_saveDiffWritesMask(tmp_path: Path) -> None:
    a = _save(tmp_path / "a.png", (0, 0, 0))
    b = _save(tmp_path / "b.png", (255, 255, 255))
    result = visual_diff.compareImages(a, b, saveDiff=True)
    assert "diffPath" in result
    assert Path(result["diffPath"]).exists()


def test_missingFileRaises(tmp_path: Path) -> None:
    a = _save(tmp_path / "a.png", (0, 0, 0))
    with pytest.raises(FileNotFoundError):
        visual_diff.compareImages(a, str(tmp_path / "nope.png"))
