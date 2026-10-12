"""Media safety + conversion helpers.

Two jobs live here, both about handling files the browser pulls off the web:

* :func:`verifyImage` — confirm a downloaded file is *actually* an image and not
  an executable/app/archive wearing an image's name. It checks the real leading
  bytes (magic numbers) and then asks Pillow to fully decode the pixels. This is
  the guard behind the "downloads are images only" policy: a ``.png`` that is
  really a Mach-O binary or a ZIP is rejected.
* :func:`toMarkdown` — turn an image / PDF / Office doc / HTML page into markdown
  using Microsoft's open-source **MarkItDown**. This backs "no-image mode", where
  the agent reads text instead of looking at pixels.

MarkItDown is an optional dependency: if it is not installed, :func:`toMarkdown`
returns a structured error rather than raising, so the rest of the stack keeps
working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.utils.logger import getLogger

logger = getLogger("browser.media")

# Leading magic bytes for the image formats we accept as "real images".
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"\x00\x00\x01\x00", "ico"),
)

# Leading magic bytes that mark an executable/app/archive — never an image.
_DANGEROUS_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "Windows executable (PE)"),
    (b"\x7fELF", "Linux executable (ELF)"),
    (b"\xfe\xed\xfa\xce", "macOS executable (Mach-O)"),
    (b"\xfe\xed\xfa\xcf", "macOS executable (Mach-O 64)"),
    (b"\xcf\xfa\xed\xfe", "macOS executable (Mach-O LE)"),
    (b"\xca\xfe\xba\xbe", "macOS universal binary"),
    (b"PK\x03\x04", "ZIP/app archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"#!", "script"),
)


def _detectWebp(head: bytes) -> bool:
    """WebP is ``RIFF....WEBP`` — a container, so check both ends of the header."""
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def verifyImage(path: Path | str) -> dict[str, Any]:
    """Return whether *path* is a genuine, decodable image.

    Shape::

        {"isImage": bool, "format": str | None, "reason": str}

    The check is two-stage: reject known executable/archive signatures and
    require a known image signature, *then* confirm Pillow can actually decode
    the pixels (so a truncated or spoofed header still fails).
    """
    p = Path(path)
    if not p.exists():
        return {"isImage": False, "format": None, "reason": "file does not exist"}

    try:
        head = p.read_bytes()[:32]
    except OSError as exc:
        return {"isImage": False, "format": None, "reason": f"unreadable: {exc}"}

    for sig, label in _DANGEROUS_SIGNATURES:
        if head.startswith(sig):
            return {"isImage": False, "format": None, "reason": f"looks like a {label}"}

    detected: str | None = None
    for sig, fmt in _IMAGE_SIGNATURES:
        if head.startswith(sig):
            detected = fmt
            break
    if detected is None and _detectWebp(head):
        detected = "webp"
    if detected is None:
        return {"isImage": False, "format": None, "reason": "no known image signature"}

    # Final proof: the bytes must actually decode as an image.
    try:
        from PIL import Image

        with Image.open(p) as img:
            img.verify()
            fmt = (img.format or detected).lower()
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not a real image"
        return {"isImage": False, "format": None, "reason": f"failed to decode: {exc}"}

    return {"isImage": True, "format": fmt, "reason": "verified image"}


def markitdownAvailable() -> bool:
    """Return whether the base MarkItDown package can be imported.

    Note: this being ``True`` does NOT mean every format works — PDF/Office
    support comes from optional extras. Use :func:`markitdownFormats` for the
    honest per-format picture.
    """
    try:
        import markitdown  # noqa: F401
    except Exception:  # noqa: BLE001 - treat any import failure as unavailable
        return False
    return True


# Maps a human format label to the third-party module its MarkItDown converter
# needs. Base MarkItDown ships HTML/plain-text/CSV/JSON; the rest are extras
# (``pip install 'markitdown[pdf,docx,pptx,xlsx]'``).
# The import name is the converter's actual backend, which is NOT always the
# obvious one — MarkItDown reads .docx via `mammoth`, .pdf via `pdfminer`, etc.
_FORMAT_BACKENDS: dict[str, str] = {
    "html": "markitdown",
    "pdf": "pdfminer",
    "docx": "mammoth",
    "pptx": "pptx",
    "xlsx": "openpyxl",
    "xls": "xlrd",
}


def markitdownFormats() -> dict[str, bool]:
    """Probe which MarkItDown formats are actually usable in this environment.

    Returns a ``{format: bool}`` map so callers (and the agent) can tell that,
    e.g., base MarkItDown is installed but PDF support is not — instead of being
    misled by a single ``markitdownAvailable: true`` flag.
    """
    if not markitdownAvailable():
        return {fmt: False for fmt in _FORMAT_BACKENDS}
    import importlib.util

    formats: dict[str, bool] = {}
    for fmt, module in _FORMAT_BACKENDS.items():
        try:
            formats[fmt] = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            formats[fmt] = False
    return formats


def toMarkdown(source: str) -> dict[str, Any]:
    """Convert *source* (a local file path or URL) to markdown via MarkItDown.

    Returns ``{"markdown": str, "source": str, "chars": int}`` on success, or a
    structured ``{"error": ...}`` dict if MarkItDown is unavailable or fails.
    Supports images, PDF, Office docs, HTML and more — whatever MarkItDown
    handles.
    """
    if not markitdownAvailable():
        return {
            "error": "MarkItDown is not installed",
            "details": "Install it with `pip install markitdown` to use no-image mode.",
        }
    try:
        from markitdown import MarkItDown

        converter = MarkItDown()
        result = converter.convert(source)
        text = getattr(result, "text_content", "") or ""
    except Exception as exc:  # noqa: BLE001 - surface as structured error, never raise
        logger.warning("MarkItDown conversion failed for %s: %s", source, exc)
        detail = f"{type(exc).__name__}: {exc}"
        # A MissingDependency failure means an optional backend (e.g. PDF) is
        # absent — tell the caller exactly how to fix it rather than a bare error.
        if "MissingDependency" in type(exc).__name__ or "MissingDependency" in str(exc):
            return {
                "error": "missing format backend",
                "details": detail,
                "hint": "Install the optional extra, e.g. pip install 'markitdown[pdf,docx,pptx,xlsx]'.",
                "formats": markitdownFormats(),
            }
        return {"error": "conversion failed", "details": detail}

    return {"markdown": text, "source": source, "chars": len(text)}
