"""Unit tests for media safety + conversion helpers.

The download-safety guard is the important one: a file that is really an
executable/app/archive must be rejected even if it is named like an image, and a
genuine image must be accepted. MarkItDown conversion is exercised only when the
optional dependency is installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.browser import media


def test_rejectsDisguisedExecutable(tmp_path: Path) -> None:
    # A Windows PE ("MZ...") saved as a .png is NOT a real image.
    fake = tmp_path / "totally_an_image.png"
    fake.write_bytes(b"MZ\x90\x00" + b"\x00" * 64)
    verdict = media.verifyImage(fake)
    assert verdict["isImage"] is False
    assert "executable" in verdict["reason"].lower()


def test_rejectsZipArchive(tmp_path: Path) -> None:
    fake = tmp_path / "pic.jpg"
    fake.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    assert media.verifyImage(fake)["isImage"] is False


def test_rejectsRandomBytes(tmp_path: Path) -> None:
    fake = tmp_path / "noise.png"
    fake.write_bytes(b"not an image at all")
    assert media.verifyImage(fake)["isImage"] is False


def test_missingFile(tmp_path: Path) -> None:
    assert media.verifyImage(tmp_path / "nope.png")["isImage"] is False


def test_acceptsRealPng(tmp_path: Path) -> None:
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    real = tmp_path / "real.png"
    Image.new("RGB", (8, 8), (123, 222, 64)).save(real)
    verdict = media.verifyImage(real)
    assert verdict["isImage"] is True
    assert verdict["format"] == "png"


def test_markitdownFormatsReportsPerFormat() -> None:
    formats = media.markitdownFormats()
    # Always a dict keyed by the known formats, each a bool — never a single
    # misleading "available" flag.
    assert isinstance(formats, dict)
    assert set(formats) >= {"pdf", "docx", "pptx", "xlsx", "html"}
    assert all(isinstance(v, bool) for v in formats.values())
    if not media.markitdownAvailable():
        assert all(v is False for v in formats.values())


def test_toMarkdownDegradesGracefully(tmp_path: Path) -> None:
    sample = tmp_path / "note.txt"
    sample.write_text("hello world", encoding="utf-8")
    result = media.toMarkdown(str(sample))
    if not media.markitdownAvailable():
        assert "error" in result
    else:
        assert "markdown" in result
        assert isinstance(result["markdown"], str)
