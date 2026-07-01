"""AI Browser Controller — plug-and-play browser automation for AI agents."""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"


def _loadDotenv() -> None:
    """Load ``.env`` from the project root into ``os.environ`` (real env wins).

    Runs at package import — before ``app.utils.config`` reads ``ABC_*`` or any
    adapter reads ``GEMINI_API_KEY*`` — so a single ``.env`` configures every
    entry point (CLI, API, MCP). Tiny stdlib parser instead of a python-dotenv
    dependency: ``KEY=value`` lines, ``#`` comments, optional ``export`` prefix
    and surrounding quotes. Existing environment variables are never overwritten,
    so ``FOO=bar python start.py ...`` still overrides ``.env``.
    """
    envFile = Path(__file__).resolve().parent.parent / ".env"
    if not envFile.exists():
        return
    for line in envFile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_loadDotenv()
