"""Shared pytest configuration and fixtures for the AI Browser Controller suite.

Two responsibilities live here:

* Put the project root on ``sys.path`` so ``import app...`` resolves when
  ``pytest`` is invoked from the project root (the ``tests/`` package itself is
  a sibling of ``app/``).
* Expose a couple of small fixtures shared across modules.

Async tests are marked individually with ``@pytest.mark.asyncio`` (see the
``asyncio_mode`` setting below), so no manual ``event_loop`` fixture is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Make ``import app...`` work regardless of the current working directory.
# ``conftest.py`` lives in ``<projectRoot>/tests/``; the project root is its
# parent and must precede anything else on the path.
# ---------------------------------------------------------------------------
projectRoot = Path(__file__).resolve().parent.parent
if str(projectRoot) not in sys.path:
    sys.path.insert(0, str(projectRoot))


# Opt every plain ``async def test_...`` into pytest-asyncio when the plugin is
# configured for "auto" mode; harmless when tests are explicitly marked.
def pytest_configure(config: pytest.Config) -> None:
    iniValue = config.inicfg.get("asyncio_mode") if hasattr(config, "inicfg") else None
    if not iniValue:
        # Best-effort: set a sane default so an absent pytest.ini does not leave
        # async tests un-collected. Ignored if the plugin is missing.
        try:
            config.inicfg["asyncio_mode"] = "auto"  # type: ignore[index]
        except Exception:  # noqa: BLE001 - purely a convenience default
            pass


@pytest.fixture
def browserManager():
    """Return the process-wide :class:`BrowserManager` singleton.

    Imported lazily so collection never fails if the browser stack (or its
    transitive Playwright dependency) is unavailable in a given environment.
    """
    from app.browser.browser_manager import getBrowserManager

    return getBrowserManager()
