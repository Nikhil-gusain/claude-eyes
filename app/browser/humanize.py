"""Human-like interaction primitives layered over Playwright.

Bot-detection systems look for tells that scream "automation": a whole string
appearing in an input at once, a cursor that teleports to the exact pixel-center
of a button, a scroll that snaps straight to its target. These helpers replace
those machine-perfect motions with human-shaped ones:

* :func:`humanType` — types character by character at roughly ``wpm`` words per
  minute, with per-keystroke jitter and the occasional "thinking" pause.
* :func:`humanMoveTo` — moves the mouse along a curved, slightly wobbling path
  (a quadratic Bézier with jitter) from wherever the cursor last was, never a
  straight line.
* :func:`humanClickSelector` — moves to a random point *inside* the target (not
  its exact center), pauses, then presses — used for buttons and inputs alike.
* :func:`humanScrollBy` / :func:`humanScrollToSelector` — scroll in small,
  variable steps with pauses, like a human discovering the page.

Every motion helper takes the cursor's last position (``fromXY``) and returns
its new position, so the caller (the controller) can remember where the cursor
is between actions. This is normal application code, so Python's ``random`` is
used directly for the natural variation.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Optional

from playwright.async_api import Page

from app.utils.config import settings
from app.utils.logger import getLogger

logger = getLogger("browser.humanize")

# Characters per "word" by the standard WPM convention (including the space).
CHARS_PER_WORD = 5


# --------------------------------------------------------------------------- #
# Typing
# --------------------------------------------------------------------------- #
def charDelaySeconds(wpm: int) -> float:
    """Average seconds between keystrokes for *wpm* words per minute.

    25 WPM -> 125 chars/min -> ~0.48 s/char. Guarded so a silly ``wpm`` can't
    divide-by-zero or hang the session.
    """
    wpm = max(1, min(wpm, 1000))
    charsPerSecond = (wpm * CHARS_PER_WORD) / 60.0
    return 1.0 / charsPerSecond


async def humanType(
    page: Page,
    text: str,
    wpm: int | None = None,
) -> None:
    """Type *text* into the focused element one key at a time, human-paced.

    Delays vary around the per-character average (Gaussian jitter), pauses are a
    touch longer after spaces/punctuation, and occasionally a longer "thinking"
    gap is inserted — the rhythm a real typist produces.
    """
    base = charDelaySeconds(wpm if wpm is not None else settings.typingWpm)
    for ch in text:
        await page.keyboard.type(ch)
        delay = random.gauss(base, base * 0.4)
        delay = max(base * 0.3, delay)
        if ch in " \t":
            delay += random.uniform(0, base * 0.5)
        elif ch in ".,!?;:\n":
            delay += random.uniform(base * 0.5, base * 1.5)
        if random.random() < 0.04:  # occasional pause, as if thinking
            delay += random.uniform(0.3, 1.1)
        await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# Mouse movement
# --------------------------------------------------------------------------- #
def _bezierPoints(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int,
) -> list[tuple[float, float]]:
    """Sample a jittered quadratic Bézier curve from *start* to *end*.

    The control point sits off the straight line by a random perpendicular
    offset, so the path bows to one side; small per-point jitter adds the
    natural wobble. Never returns a straight line.
    """
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy) or 1.0

    # Perpendicular unit vector to bow the curve sideways.
    px, py = -dy / dist, dx / dist
    bow = random.uniform(-0.25, 0.25) * dist
    midX, midY = (x0 + x1) / 2, (y0 + y1) / 2
    cx, cy = midX + px * bow, midY + py * bow

    points: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in/ease-out so the cursor accelerates then settles.
        t = t * t * (3 - 2 * t)
        mt = 1 - t
        x = mt * mt * x0 + 2 * mt * t * cx + t * t * x1
        y = mt * mt * y0 + 2 * mt * t * cy + t * t * y1
        if i < steps:  # leave the final landing point exact
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.5, 1.5)
        points.append((x, y))
    return points


async def humanMoveTo(
    page: Page,
    targetX: float,
    targetY: float,
    fromXY: tuple[float, float],
) -> tuple[float, float]:
    """Move the mouse to ``(targetX, targetY)`` along a curved, wobbling path.

    Returns the final cursor position so the caller can remember it.
    """
    dist = math.hypot(targetX - fromXY[0], targetY - fromXY[1])
    steps = max(12, min(int(dist / 8) + 1, 60))
    for x, y in _bezierPoints(fromXY, (targetX, targetY), steps):
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.004, 0.016))
    return (float(targetX), float(targetY))


async def _elementPoint(page: Page, selector: str, timeoutMs: int) -> tuple[float, float]:
    """Scroll *selector* into view and return a random point inside it."""
    locator = page.locator(selector).first
    await locator.wait_for(state="visible", timeout=timeoutMs)
    await locator.scroll_into_view_if_needed(timeout=timeoutMs)
    box = await locator.bounding_box()
    if not box:
        raise ValueError(f"Element has no bounding box: {selector}")
    # Aim for the central 60% of the element, not the exact center.
    x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
    y = box["y"] + box["height"] * random.uniform(0.2, 0.8)
    return (x, y)


async def humanClickSelector(
    page: Page,
    selector: str,
    fromXY: tuple[float, float],
    button: str = "left",
    clickCount: int = 1,
    timeoutMs: int = 30_000,
) -> tuple[float, float]:
    """Move to a random point inside *selector*, pause, then press.

    Works for buttons and text inputs alike (clicking an input focuses it for a
    subsequent :func:`humanType`). Returns the new cursor position.
    """
    target = await _elementPoint(page, selector, timeoutMs)
    pos = await humanMoveTo(page, target[0], target[1], fromXY)
    await asyncio.sleep(random.uniform(0.04, 0.18))  # settle before pressing
    for _ in range(max(1, clickCount)):
        await page.mouse.down(button=button)  # type: ignore[arg-type]
        await asyncio.sleep(random.uniform(0.03, 0.09))
        await page.mouse.up(button=button)  # type: ignore[arg-type]
        if clickCount > 1:
            await asyncio.sleep(random.uniform(0.05, 0.12))
    return pos


# --------------------------------------------------------------------------- #
# Scrolling
# --------------------------------------------------------------------------- #
async def humanScrollBy(page: Page, deltaY: int, deltaX: int = 0) -> None:
    """Scroll by ``deltaY``/``deltaX`` in small, variable steps with pauses.

    A human flicks the wheel several times rather than jumping the whole way at
    once; this breaks the delta into 60–160px notches with brief gaps.
    """
    remaining = deltaY
    direction = 1 if deltaY >= 0 else -1
    while abs(remaining) > 0:
        step = direction * min(abs(remaining), random.randint(60, 160))
        await page.mouse.wheel(0, step)
        remaining -= step
        await asyncio.sleep(random.uniform(0.05, 0.22))
    if deltaX:
        await page.mouse.wheel(deltaX, 0)


async def humanScrollToSelector(
    page: Page,
    selector: str,
    timeoutMs: int = 30_000,
    maxSteps: int = 40,
) -> bool:
    """Lazily scroll until *selector* enters the viewport (human discovery).

    Instead of snapping straight to the element, scroll a screenful at a time —
    pausing between flicks — until the element is visible or ``maxSteps`` is hit.
    Returns whether the element ended up in view.
    """
    locator = page.locator(selector).first
    await locator.wait_for(state="attached", timeout=timeoutMs)
    for _ in range(maxSteps):
        inView = await locator.evaluate(
            """(el) => {
                const r = el.getBoundingClientRect();
                return r.top >= 0 && r.bottom <= (window.innerHeight ||
                    document.documentElement.clientHeight);
            }"""
        )
        if inView:
            return True
        viewport = page.viewport_size or {"height": 800}
        await humanScrollBy(page, int(viewport["height"] * random.uniform(0.6, 0.9)))
    # Fall back to an exact scroll if discovery didn't land it.
    await locator.scroll_into_view_if_needed(timeout=timeoutMs)
    return False


def shouldHumanize(override: Optional[bool]) -> bool:
    """Resolve whether to humanize: explicit *override* wins over the setting."""
    return settings.humanize if override is None else override
