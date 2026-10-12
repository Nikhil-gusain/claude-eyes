"""The task-scoped "current tab" — shared by every layer, zero heavy deps.

A tool call sets :data:`currentTab` to the tab it should act on; ``activePage``
(and the per-tab lock) read it. Kept in its own tiny module so the low-level
error decorator can import it without pulling in Playwright or creating a cycle.
``None`` means "use the session's active tab" (single-tab / main-agent default).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

currentTab: ContextVar[Optional[str]] = ContextVar("currentTab", default=None)
