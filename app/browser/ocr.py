"""OCR layer — read text baked into images/screenshots.

Many pages hide information inside images (charts, scanned docs, text rendered to
canvas). This extracts that text via Tesseract. Like the MarkItDown integration,
Tesseract is an OPTIONAL dependency: when the ``pytesseract`` package or the
``tesseract`` binary is missing, every call returns an honest error envelope
(with install hints) instead of raising — so the import never breaks the app.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.utils.logger import getLogger

logger = getLogger("browser.ocr")


def _tesseractBinary() -> str | None:
    """Resolve the tesseract executable, honouring pytesseract's configured path."""
    try:
        import pytesseract  # noqa: PLC0415 - optional dependency, imported lazily

        configured = getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
        # An absolute configured path that exists wins; otherwise look on PATH.
        if configured and Path(configured).is_file():
            return configured
        return shutil.which(configured) or shutil.which("tesseract")
    except Exception:  # noqa: BLE001 - pytesseract not installed
        return None


def ocrAvailable() -> bool:
    """Whether OCR can actually run (both the package and the binary are present)."""
    try:
        import pytesseract  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001
        return False
    return _tesseractBinary() is not None


def _unavailableError() -> dict[str, Any]:
    haveBinary = _tesseractBinary() is not None
    try:
        import pytesseract  # noqa: F401, PLC0415

        havePkg = True
    except Exception:  # noqa: BLE001
        havePkg = False
    missing = []
    if not havePkg:
        missing.append("the 'pytesseract' package (pip install pytesseract)")
    if not haveBinary:
        missing.append("the Tesseract binary (e.g. 'brew install tesseract')")
    return {
        "error": "OCR is unavailable",
        "details": "Install " + " and ".join(missing) + " to enable text-from-image.",
        "ocrAvailable": False,
    }


def extractText(imagePath: str, lang: str = "eng") -> dict[str, Any]:
    """OCR an image file and return its text.

    Returns ``{"text", "length", "words", "ocrAvailable": True}`` on success, or
    ``{"error", "details", "ocrAvailable": False}`` when Tesseract is unavailable
    or the file is missing/unreadable.
    """
    path = Path(imagePath)
    if not path.exists():
        return {"error": "Image not found", "details": str(imagePath), "ocrAvailable": ocrAvailable()}
    if not ocrAvailable():
        return _unavailableError()
    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        binary = _tesseractBinary()
        if binary:
            pytesseract.pytesseract.tesseract_cmd = binary
        with Image.open(path) as img:
            text = pytesseract.image_to_string(img, lang=lang)
    except Exception as exc:  # noqa: BLE001 - surface as structured error
        return {"error": "OCR failed", "details": f"{type(exc).__name__}: {exc}", "ocrAvailable": True}
    cleaned = text.strip()
    return {
        "text": cleaned,
        "length": len(cleaned),
        "words": len(cleaned.split()),
        "lang": lang,
        "ocrAvailable": True,
    }
