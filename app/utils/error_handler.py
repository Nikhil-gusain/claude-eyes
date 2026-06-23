"""Centralized error handling — the single point every layer routes through.

The whole project follows one rule: **an operation never raises to its caller;
it returns a structured envelope.** This module is where that guarantee is
implemented once and reused everywhere:

* :class:`BrowserControllerError` and its subclasses give callers typed,
  human-readable failures instead of raw Playwright/OS exceptions.
* :func:`buildErrorEnvelope` turns any exception into the canonical
  ``{"success": False, "error", "details", ...}`` envelope.
* :func:`safeAsync` / :func:`safeSync` / :func:`safe` are decorators that wrap a
  function so any exception it raises becomes that envelope. Decorate a handler
  with ``@safeAsync("navigate")`` and it can no longer crash its caller.

Usage across the codebase:

* ``BrowserManager._run`` routes every browser action through
  :func:`buildErrorEnvelope` (so the whole browser core is covered in one place).
* The FastAPI app registers global exception handlers built on the same builder.
* The WebSocket loop, the MCP tools, and the AI adapters use :func:`safeAsync`.

Keeping this in one module means the failure contract is defined once: change the
envelope shape here and every layer updates together.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Awaitable, Callable, TypeVar

from app.utils.helpers import errorResponse
from app.utils.logger import getLogger

logger = getLogger("utils.errors")

F = TypeVar("F", bound=Callable[..., Any])


# --------------------------------------------------------------------------- #
# Typed exception hierarchy
# --------------------------------------------------------------------------- #
class BrowserControllerError(Exception):
    """Base class for all expected, handled failures in the controller.

    Carrying a separate ``message`` and ``details`` lets :func:`buildErrorEnvelope`
    produce a clean, AI-readable error without leaking a raw traceback string.
    """

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class BrowserNotRunningError(BrowserControllerError):
    """Raised when an action needs a live browser but none is running."""


class NavigationError(BrowserControllerError):
    """Raised when a navigation (goto/back/forward/reload) fails."""


class ElementNotFoundError(BrowserControllerError):
    """Raised when a selector matches no element in time."""


class ExtractionError(BrowserControllerError):
    """Raised when reading page content/DOM fails."""


class ScreenshotError(BrowserControllerError):
    """Raised when capturing or annotating a screenshot fails."""


class RecordingError(BrowserControllerError):
    """Raised when starting/stopping/encoding a recording fails."""


# --------------------------------------------------------------------------- #
# Envelope builder — the one place exceptions become responses
# --------------------------------------------------------------------------- #
def buildErrorEnvelope(action: str, exc: BaseException) -> dict[str, Any]:
    """Convert *exc* into the canonical AI-friendly error envelope.

    Known :class:`BrowserControllerError` instances surface their curated
    ``message``/``details``; anything else is reported generically with its
    exception type so the caller still gets actionable information without a
    crash.
    """
    if isinstance(exc, BrowserControllerError):
        return errorResponse(
            error=exc.message,
            details=exc.details or type(exc).__name__,
            action=action,
        )
    return errorResponse(
        error=f"{action} failed",
        details=f"{type(exc).__name__}: {exc}",
        action=action,
    )


# --------------------------------------------------------------------------- #
# Decorators — the reusable "center point" applied across layers
# --------------------------------------------------------------------------- #
def safeAsync(action: str | None = None) -> Callable[[F], F]:
    """Wrap an async function so it returns an error envelope instead of raising.

    Args:
        action: Name recorded in the envelope's ``action`` field. Defaults to the
            wrapped function's ``__name__``.

    The wrapped coroutine is awaited inside a try/except; on any exception the
    error is logged once and a structured envelope is returned. If the function
    already returns an envelope on success, that value passes through untouched.
    """

    def decorator(fn: F) -> F:
        resolvedAction = action or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except BrowserControllerError as exc:
                logger.warning("Handled error in '%s': %s", resolvedAction, exc.message)
                return buildErrorEnvelope(resolvedAction, exc)
            except Exception as exc:  # noqa: BLE001 - intentional catch-all boundary
                logger.exception("Unhandled error in '%s'", resolvedAction)
                return buildErrorEnvelope(resolvedAction, exc)

        return wrapper  # type: ignore[return-value]

    return decorator


def safeSync(action: str | None = None) -> Callable[[F], F]:
    """Synchronous counterpart of :func:`safeAsync`."""

    def decorator(fn: F) -> F:
        resolvedAction = action or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except BrowserControllerError as exc:
                logger.warning("Handled error in '%s': %s", resolvedAction, exc.message)
                return buildErrorEnvelope(resolvedAction, exc)
            except Exception as exc:  # noqa: BLE001 - intentional catch-all boundary
                logger.exception("Unhandled error in '%s'", resolvedAction)
                return buildErrorEnvelope(resolvedAction, exc)

        return wrapper  # type: ignore[return-value]

    return decorator


def safe(action: str | None = None) -> Callable[[F], F]:
    """Pick :func:`safeAsync` or :func:`safeSync` automatically by function kind."""

    def decorator(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):
            return safeAsync(action)(fn)
        return safeSync(action)(fn)

    return decorator


async def runGuarded(action: str, coro: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """Await *coro* and convert any exception into an error envelope.

    A function-call form of :func:`safeAsync` for one-off awaits where adding a
    decorator is awkward (e.g. inside a loop).
    """
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - boundary
        logger.exception("Guarded call '%s' failed", action)
        return buildErrorEnvelope(action, exc)
