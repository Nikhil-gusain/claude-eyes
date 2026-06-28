"""Unit tests for the OCR layer's graceful degradation (browser-free).

We don't require Tesseract to be installed: the contract under test is that the
module reports availability honestly and returns structured errors (never raises)
when the engine or the file is missing. When Tesseract *is* present, a real OCR
of a rendered-text image is exercised too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.browser import ocr


def test_availabilityIsBoolean() -> None:
    assert isinstance(ocr.ocrAvailable(), bool)


def test_missingFileReturnsStructuredError(tmp_path: Path) -> None:
    result = ocr.extractText(str(tmp_path / "nope.png"))
    assert "error" in result
    assert result["error"] == "Image not found"


def test_unavailableEngineDegradesGracefully(tmp_path: Path) -> None:
    if ocr.ocrAvailable():
        pytest.skip("Tesseract is installed; the unavailable path can't be exercised")
    pytest.importorskip("PIL.Image")
    from PIL import Image

    img = tmp_path / "x.png"
    Image.new("RGB", (32, 32), (255, 255, 255)).save(img)
    result = ocr.extractText(str(img))
    assert result["ocrAvailable"] is False
    assert "error" in result and "details" in result
    # The error should point the user at how to enable it.
    assert "tesseract" in result["details"].lower() or "pytesseract" in result["details"].lower()


def test_realOcrWhenAvailable(tmp_path: Path) -> None:
    if not ocr.ocrAvailable():
        pytest.skip("Tesseract not installed")
    pytest.importorskip("PIL.Image")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (220, 60), (255, 255, 255))
    ImageDraw.Draw(img).text((10, 20), "HELLO", fill=(0, 0, 0))
    path = tmp_path / "hello.png"
    img.save(path)
    result = ocr.extractText(str(path))
    assert result["ocrAvailable"] is True
    assert "HELLO" in result["text"].upper()
