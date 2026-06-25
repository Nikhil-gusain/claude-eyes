"""Unit tests for the human-like interaction helpers.

These cover the pure, browser-free pieces: the WPM->delay math, the humanize
toggle resolution, and the fact that the generated mouse path is a *curve* (with
jitter), never a straight line — which is the whole point for bot-detection.

The module imports Playwright at top level, so the suite skips cleanly when
Playwright is not installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="Playwright not installed")

from app.browser import humanize  # noqa: E402


def test_charDelayMatchesWpm() -> None:
    # 25 WPM -> 125 chars/min -> 0.48 s/char.
    assert humanize.charDelaySeconds(25) == pytest.approx(0.48, abs=1e-6)
    # Faster typing yields a smaller per-char delay.
    assert humanize.charDelaySeconds(60) < humanize.charDelaySeconds(25)


def test_charDelayGuardsAgainstNonsense() -> None:
    # Zero/negative WPM must not divide-by-zero or hang.
    assert humanize.charDelaySeconds(0) > 0
    assert humanize.charDelaySeconds(-10) > 0


def test_shouldHumanizeOverrideWins() -> None:
    assert humanize.shouldHumanize(True) is True
    assert humanize.shouldHumanize(False) is False
    # None defers to the configured default (a bool either way).
    assert isinstance(humanize.shouldHumanize(None), bool)


def test_bezierPathIsACurveNotAStraightLine() -> None:
    start, end = (0.0, 0.0), (200.0, 0.0)
    points = humanize._bezierPoints(start, end, steps=30)

    assert len(points) == 30
    # The path ends exactly on target (the final landing point is not jittered).
    assert points[-1] == pytest.approx(end, abs=1e-6)
    # A straight horizontal move would keep y == 0 throughout; a human curve must
    # bow away from the line at some point.
    maxYDeviation = max(abs(y) for _, y in points)
    assert maxYDeviation > 1.0
