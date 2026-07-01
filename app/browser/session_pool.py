"""Browser session pool — many isolated browsers, one active at a time.

The project always described ``getBrowserManager()`` as the seam for scaling out
from a single process-wide browser to many. This is that pool: it keeps a
``BrowserManager`` per session id and tracks which one is *active*.
``getBrowserManager()`` now returns the active session's manager, so every
existing tool transparently drives the active browser — and new tools
(``create_session`` / ``switch_session`` / ``list_sessions`` / ``close_session``)
let an agent run several independent browsers and move between them.

Each session is a full ``BrowserManager`` with its own controller, recorder,
session log and lock, so sessions are isolated. ``BrowserManager`` is imported
lazily to avoid an import cycle with :mod:`app.browser.browser_manager`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.utils.error_handler import buildErrorEnvelope
from app.utils.helpers import generateSessionName, successResponse
from app.utils.logger import getLogger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.browser.browser_manager import BrowserManager

logger = getLogger("browser.pool")


class SessionPool:
    """Owns multiple :class:`BrowserManager` instances keyed by session id."""

    def __init__(self) -> None:
        self._sessions: dict[str, "BrowserManager"] = {}
        self._order: list[str] = []
        self._activeId: str | None = None

    # ------------------------------------------------------------------ #
    # Core resolution
    # ------------------------------------------------------------------ #
    def active(self) -> "BrowserManager":
        """Return the active session's manager, creating a default one if needed."""
        if self._activeId is None:
            self.create("default")
        return self._sessions[self._activeId]

    @property
    def activeId(self) -> str | None:
        return self._activeId

    def create(self, sessionId: str | None = None, makeActive: bool = True) -> str:
        """Create (or reuse) a session and optionally make it active."""
        from app.browser.browser_manager import BrowserManager  # noqa: PLC0415 - avoid cycle

        sessionId = sessionId or generateSessionName("session")
        if sessionId not in self._sessions:
            self._sessions[sessionId] = BrowserManager()
            self._order.append(sessionId)
            logger.info("Created browser session '%s' (%d total)", sessionId, len(self._sessions))
        if makeActive or self._activeId is None:
            self._activeId = sessionId
        return sessionId

    def switch(self, sessionId: str) -> str:
        if sessionId not in self._sessions:
            raise KeyError(f"No such session: {sessionId}")
        self._activeId = sessionId
        return sessionId

    async def close(self, sessionId: str | None = None) -> str:
        target = sessionId or self._activeId
        manager = self._sessions.get(target) if target else None
        if manager is None:
            raise KeyError(f"No such session: {sessionId}")
        try:
            await manager.closeBrowser()
        except Exception as exc:  # noqa: BLE001 - teardown is best-effort
            logger.warning("Error closing session '%s': %s", target, exc)
        del self._sessions[target]
        self._order.remove(target)
        if self._activeId == target:
            self._activeId = self._order[-1] if self._order else None
        return target

    async def closeAll(self) -> int:
        """Close every session (used on server shutdown). Returns how many closed."""
        count = 0
        for sessionId in list(self._order):
            try:
                await self.close(sessionId)
                count += 1
            except Exception:  # noqa: BLE001
                logger.exception("Failed to close session '%s'", sessionId)
        return count

    def _info(self, sessionId: str) -> dict[str, Any]:
        manager = self._sessions[sessionId]
        snapshot = manager.controller._stateSnapshot()  # noqa: SLF001 - internal read
        return {
            "sessionId": sessionId,
            "active": sessionId == self._activeId,
            "running": snapshot.get("running"),
            "url": snapshot.get("url"),
            "tabCount": snapshot.get("tabCount"),
            "profileName": snapshot.get("profileName"),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self._info(sid) for sid in self._order]


# --------------------------------------------------------------------- #
# Process-wide pool singleton
# --------------------------------------------------------------------- #
_poolSingleton: SessionPool | None = None


def getSessionPool() -> SessionPool:
    """Return the shared :class:`SessionPool`, creating it on first use."""
    global _poolSingleton
    if _poolSingleton is None:
        _poolSingleton = SessionPool()
    return _poolSingleton


# --------------------------------------------------------------------- #
# Enveloped session-management operations (used by every transport)
# --------------------------------------------------------------------- #
def createSession(sessionId: str | None = None, makeActive: bool = True) -> dict[str, Any]:
    try:
        pool = getSessionPool()
        sid = pool.create(sessionId, makeActive=makeActive)
        return successResponse("create_session", {
            "sessionId": sid, "active": pool.activeId == sid, "sessions": pool.list(),
        })
    except Exception as exc:  # noqa: BLE001
        return buildErrorEnvelope("create_session", exc)


def listSessions() -> dict[str, Any]:
    try:
        pool = getSessionPool()
        return successResponse("list_sessions", {
            "sessions": pool.list(), "activeId": pool.activeId, "count": len(pool.list()),
        })
    except Exception as exc:  # noqa: BLE001
        return buildErrorEnvelope("list_sessions", exc)


def switchSession(sessionId: str) -> dict[str, Any]:
    try:
        pool = getSessionPool()
        pool.switch(sessionId)
        return successResponse("switch_session", {"activeId": sessionId, "sessions": pool.list()})
    except Exception as exc:  # noqa: BLE001
        return buildErrorEnvelope("switch_session", exc)


async def closeSession(sessionId: str | None = None) -> dict[str, Any]:
    try:
        pool = getSessionPool()
        closed = await pool.close(sessionId)
        return successResponse("close_session", {
            "closed": closed, "activeId": pool.activeId, "sessions": pool.list(),
        })
    except Exception as exc:  # noqa: BLE001
        return buildErrorEnvelope("close_session", exc)
