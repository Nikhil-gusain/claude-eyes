"""Regression tests for wait_for_response URL matching (bug: matched query strings).

A loose substring over the whole URL let ``wait_for_response("duckchat")`` latch
onto a bot-detection beacon ``https://duck.ai/anomaly.js?...cc=duckchat...``
instead of the chat backend. Matching must consider scheme+host+path and ignore
the query string by default, while still supporting regex and an opt-in to match
the full URL.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="Playwright not installed")

from app.browser.playwright_controller import urlMatches  # noqa: E402

ANOMALY = "https://duck.ai/anomaly.js?b=1&cc=duckchat&u=2"
BACKEND = "https://duck.ai/duckchat/v1/chat"


def test_doesNotMatchQueryStringByDefault() -> None:
    # The pattern appears only in the query of the anomaly beacon -> no match.
    assert urlMatches("duckchat", ANOMALY) is False


def test_matchesPath() -> None:
    # The same pattern is in the real backend's path -> match.
    assert urlMatches("duckchat", BACKEND) is True


def test_includeQueryOptIn() -> None:
    # Opting in lets the query participate again.
    assert urlMatches("cc=duckchat", ANOMALY, includeQuery=True) is True
    assert urlMatches("cc=duckchat", ANOMALY, includeQuery=False) is False


def test_regexSupported() -> None:
    assert urlMatches(r"/duckchat/v\d+/chat$", BACKEND) is True
    assert urlMatches(r"/backend-api/conversation$", BACKEND) is False


def test_invalidRegexFallsBackToSubstring() -> None:
    # An unbalanced '[' is not valid regex; must fall back to substring on path.
    assert urlMatches("duckchat[", BACKEND) is False
    assert urlMatches("v1/chat", BACKEND) is True
