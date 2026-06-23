"""Validation tests for the inbound Pydantic command models.

We assert both the happy path (valid payloads parse and apply defaults) and the
failure path (missing required fields / out-of-range values raise
``pydantic.ValidationError``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.commands import (
    ClickCommand,
    FillCommand,
    LaunchCommand,
    NavigateCommand,
    ScreenshotCommand,
    UploadCommand,
)


def test_click_command_minimal_valid():
    cmd = ClickCommand(selector="#x")
    assert cmd.selector == "#x"
    # Defaults defined on the model.
    assert cmd.button == "left"
    assert cmd.clickCount == 1
    assert cmd.timeoutMs is None


def test_click_command_rejects_invalid_button():
    with pytest.raises(ValidationError):
        ClickCommand(selector="#x", button="sideways")


def test_click_command_rejects_out_of_range_click_count():
    with pytest.raises(ValidationError):
        ClickCommand(selector="#x", clickCount=99)


def test_navigate_command_requires_url():
    with pytest.raises(ValidationError):
        NavigateCommand()  # type: ignore[call-arg]


def test_navigate_command_valid_with_defaults():
    cmd = NavigateCommand(url="https://example.com")
    assert cmd.url == "https://example.com"
    assert cmd.waitUntil == "networkidle"
    assert cmd.timeoutMs is None


def test_navigate_command_rejects_bad_wait_until():
    with pytest.raises(ValidationError):
        NavigateCommand(url="https://example.com", waitUntil="whenever")


def test_screenshot_command_defaults_full_page_false():
    cmd = ScreenshotCommand()
    assert cmd.fullPage is False
    assert cmd.selector is None
    assert cmd.annotate is False
    assert cmd.label is None


def test_screenshot_command_accepts_overrides():
    cmd = ScreenshotCommand(fullPage=True, selector="#hero", annotate=True, label="hero")
    assert cmd.fullPage is True
    assert cmd.selector == "#hero"
    assert cmd.annotate is True
    assert cmd.label == "hero"


def test_fill_command_requires_selector_and_value():
    cmd = FillCommand(selector="input[name=q]", value="hello")
    assert cmd.selector == "input[name=q]"
    assert cmd.value == "hello"
    assert cmd.clearFirst is True

    with pytest.raises(ValidationError):
        FillCommand(selector="input[name=q]")  # type: ignore[call-arg]


def test_launch_command_viewport_bounds_enforced():
    # Below the configured minimum width (240) must be rejected.
    with pytest.raises(ValidationError):
        LaunchCommand(viewportWidth=10)

    good = LaunchCommand(browserType="firefox", headless=False, viewportWidth=1024)
    assert good.browserType == "firefox"
    assert good.headless is False
    assert good.viewportWidth == 1024


def test_upload_command_requires_non_empty_file_list():
    with pytest.raises(ValidationError):
        UploadCommand(selector="input[type=file]", filePaths=[])

    cmd = UploadCommand(selector="input[type=file]", filePaths=["/tmp/a.png"])
    assert cmd.filePaths == ["/tmp/a.png"]
