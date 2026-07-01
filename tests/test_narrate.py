"""Unit tests for the JSON->sentence narrator used by the Studio thinking window."""

from __future__ import annotations

from app.agents.narrate import narrateAction, narrateResult


def test_navigate_uses_url() -> None:
    assert narrateAction("navigate", {"url": "https://chatgpt.com"}) == "Navigating to https://chatgpt.com"


def test_click_prefers_description_then_selector() -> None:
    assert narrateAction("click", {"selector": "#login"}) == "Clicking #login"
    assert narrateAction("click_by_description", {"description": "the Login button"}) == "Clicking the Login button"


def test_fill_mentions_value_and_target() -> None:
    sentence = narrateAction("fill", {"selector": "#prompt", "value": "hello world"})
    assert "hello world" in sentence and "#prompt" in sentence


def test_press_keys() -> None:
    assert narrateAction("press_keys", {"keys": "Enter"}) == "Pressing Enter"


def test_long_value_is_truncated() -> None:
    sentence = narrateAction("navigate", {"url": "x" * 200})
    assert "…" in sentence and len(sentence) < 120


def test_unknown_tool_falls_back_to_humanized_name() -> None:
    # No bespoke template -> humanized name, optionally with a target.
    assert narrateAction("some_new_tool", {}) == "Some new tool"
    assert narrateAction("some_new_tool", {"selector": ".x"}) == "Some new tool .x"


def test_result_success_and_failure() -> None:
    assert narrateResult("navigate", {"success": True}) == "✓ done"
    assert narrateResult("navigate", {"success": False, "error": "boom"}) == "✗ boom"
    assert narrateResult("navigate", "not-a-dict") == "✓ done"
