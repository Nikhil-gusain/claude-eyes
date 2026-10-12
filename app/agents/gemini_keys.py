"""Rotating pool of Gemini API keys to spread free-tier rate limits.

Set ``GEMINI_API_KEYS`` to a comma- or space-separated list of keys; the single
``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` vars are also folded in (deduped, order
preserved). On a rate-limit (429 / RESOURCE_EXHAUSTED) the pool advances to the
next key and retries, so a free-tier 429 on one key transparently falls through
to the next. A working key stays "current" — we only rotate on failure.

No SDK dependency here (only ``os``), so it imports anywhere safely.
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable, Iterator, TypeVar

T = TypeVar("T")

# Persistent across calls so an exhausted key isn't retried first every time.
_index = 0


def loadKeys() -> list[str]:
    """Return the configured Gemini keys, ordered and deduped."""
    keys: list[str] = []
    for token in os.getenv("GEMINI_API_KEYS", "").replace(",", " ").split():
        if token and token not in keys:
            keys.append(token)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.getenv(name)
        if value and value not in keys:
            keys.append(value)
    return keys


def _isRateLimit(exc: BaseException) -> bool:
    """True for rate-limit / quota errors, matched on text to avoid SDK coupling."""
    text = f"{getattr(exc, 'code', '')} {getattr(exc, 'status', '')} {exc}".upper()
    return any(m in text for m in ("429", "RESOURCE_EXHAUSTED", "RATE", "QUOTA"))


def _keyCycle() -> Iterator[str]:
    """Yield keys starting at the persistent index, advancing it as consumed.

    Advancing happens only when the consumer asks for the *next* key (i.e. after a
    failure), so a successful call leaves the index parked on the working key.
    """
    global _index
    keys = loadKeys()
    if not keys:
        raise RuntimeError(
            "No Gemini API key set. Export GEMINI_API_KEY or GEMINI_API_KEYS "
            "(comma/space-separated) — GOOGLE_API_KEY is also accepted."
        )
    for _ in range(len(keys)):
        yield keys[_index % len(keys)]
        _index = (_index + 1) % len(keys)


async def withRotation(call: Callable[[str], Awaitable[T]]) -> T:
    """Run async ``call(apiKey)``, rotating keys on rate-limit until one works."""
    last: BaseException | None = None
    for key in _keyCycle():
        try:
            return await call(key)
        except Exception as exc:  # noqa: BLE001 - re-raised unless rate-limited
            if not _isRateLimit(exc):
                raise
            last = exc
    raise RuntimeError("All Gemini keys are rate-limited.") from last


def withRotationSync(call: Callable[[str], T]) -> T:
    """Run sync ``call(apiKey)``, rotating keys on rate-limit until one works."""
    last: BaseException | None = None
    for key in _keyCycle():
        try:
            return call(key)
        except Exception as exc:  # noqa: BLE001 - re-raised unless rate-limited
            if not _isRateLimit(exc):
                raise
            last = exc
    raise RuntimeError("All Gemini keys are rate-limited.") from last


def demo() -> None:
    """Self-check: rotation walks keys on rate-limit and parks on a working one."""
    os.environ["GEMINI_API_KEYS"] = "k1,k2,k3"
    global _index
    _index = 0

    calls: list[str] = []

    def flaky(key: str) -> str:
        calls.append(key)
        if key in ("k1", "k2"):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return key

    assert withRotationSync(flaky) == "k3", calls
    assert calls == ["k1", "k2", "k3"], calls
    # Working key (k3) is now current — next call starts there.
    assert withRotationSync(lambda k: k) == "k3"

    # Non-rate-limit errors propagate, no rotation.
    try:
        withRotationSync(lambda k: (_ for _ in ()).throw(ValueError("boom")))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("gemini_keys demo OK")


if __name__ == "__main__":
    demo()
