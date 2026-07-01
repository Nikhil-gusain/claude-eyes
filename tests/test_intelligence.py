"""Unit tests for the AI-judgment helpers' graceful degradation + JSON parsing.

No API key or SDK is required: the contract is that every entry point returns a
structured 'unavailable' envelope (never raises) when Claude isn't configured,
and that the model-reply JSON extractor is robust to fences/prose.
"""

from __future__ import annotations

import pytest

from app.browser import intelligence


def test_availabilityIsBoolean() -> None:
    assert isinstance(intelligence.aiAvailable(), bool)


def test_entrypointsDegradeWhenUnavailable() -> None:
    if intelligence.aiAvailable():
        pytest.skip("Claude is configured; the unavailable path can't be exercised")
    for result in (
        intelligence.verifyGoal("button visible", b"\x89PNG fake"),
        intelligence.findElement("blue button", [{"index": 0, "selector": "#x"}]),
        intelligence.planActions("log in"),
    ):
        assert result["aiAvailable"] is False
        assert "error" in result and "details" in result


def test_extractJsonPlain() -> None:
    assert intelligence._extractJson('{"a": 1}') == {"a": 1}


def test_extractJsonFromFencedProse() -> None:
    reply = 'Sure!\n```json\n{"success": true, "confidence": 0.9}\n```\nDone.'
    data = intelligence._extractJson(reply)
    assert data["success"] is True and data["confidence"] == 0.9


def test_extractJsonArray() -> None:
    assert intelligence._extractJson('here: [1, 2, 3]') == [1, 2, 3]


def test_extractJsonRaisesWhenNone() -> None:
    with pytest.raises(ValueError):
        intelligence._extractJson("no json here at all")


def test_set_and_get_provider_roundtrip() -> None:
    """The active judgment provider is settable and case-insensitive."""
    original = intelligence.getAiProvider()
    try:
        intelligence.setAiProvider("gemini")
        assert intelligence.getAiProvider() == "gemini"
        intelligence.setAiProvider("OpenAI")
        assert intelligence.getAiProvider() == "openai"
        # Empty falls back to the configured default.
        intelligence.setAiProvider("")
        assert intelligence.getAiProvider() == intelligence.settings.aiProvider
    finally:
        intelligence.setAiProvider(original)


def test_unavailable_envelope_names_active_provider(monkeypatch) -> None:
    """When the active provider isn't ready, the envelope says which one."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    original = intelligence.getAiProvider()
    try:
        intelligence.setAiProvider("gemini")
        envelope = intelligence._unavailable()
        assert envelope["provider"] == "gemini"
        assert envelope["aiAvailable"] is False
        assert "google-genai" in envelope["details"] or "GEMINI_API_KEY" in envelope["details"]
    finally:
        intelligence.setAiProvider(original)
