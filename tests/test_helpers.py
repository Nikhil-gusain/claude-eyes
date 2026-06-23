"""Unit tests for the AI-friendly envelope and filesystem helpers.

These have no third-party dependencies beyond pytest, so they run everywhere.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.utils.helpers import (
    ensureDir,
    errorResponse,
    generateSessionName,
    successResponse,
    utcTimestamp,
)


def _isIsoTimestamp(value: str) -> bool:
    """Return True if *value* parses as an ISO-8601 datetime string."""
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def test_success_response_shape_with_data():
    payload = {"url": "https://example.com", "title": "Example"}
    result = successResponse("navigate", payload)

    assert result["success"] is True
    assert result["action"] == "navigate"
    assert result["data"] == payload
    assert isinstance(result["timestamp"], str)
    assert _isIsoTimestamp(result["timestamp"])


def test_success_response_defaults_data_to_empty_dict():
    result = successResponse("status", None)

    assert result["success"] is True
    assert result["action"] == "status"
    assert result["data"] == {}


def test_error_response_without_action():
    result = errorResponse("boom", "stack trace here")

    assert result["success"] is False
    assert result["error"] == "boom"
    assert result["details"] == "stack trace here"
    assert _isIsoTimestamp(result["timestamp"])
    # action is omitted entirely when not supplied.
    assert "action" not in result


def test_error_response_includes_action_when_provided():
    result = errorResponse("click failed", "TimeoutError", action="click")

    assert result["success"] is False
    assert result["error"] == "click failed"
    assert result["details"] == "TimeoutError"
    assert result["action"] == "click"


def test_utc_timestamp_is_iso_string():
    stamp = utcTimestamp()
    assert isinstance(stamp, str)
    assert _isIsoTimestamp(stamp)


def test_generate_session_name_is_safe_unique_and_prefixed():
    firstName = generateSessionName("My Run!!")
    secondName = generateSessionName("My Run!!")

    # Prefix is sanitised to a filesystem-safe slug ("My-Run").
    assert firstName.startswith("My-Run-")
    # Only filesystem-safe characters survive.
    assert all(ch.isalnum() or ch in "-_" for ch in firstName)
    # Two successive calls must differ (the timestamp carries microseconds).
    assert firstName != secondName


def test_generate_session_name_default_prefix():
    name = generateSessionName()
    assert name.startswith("session-")


def test_ensure_dir_creates_directory(tmp_path: Path):
    target = tmp_path / "nested" / "screenshots"
    assert not target.exists()

    returned = ensureDir(target)

    assert returned == target
    assert target.exists()
    assert target.is_dir()
    # Idempotent: a second call must not raise.
    ensureDir(target)
    assert target.is_dir()
