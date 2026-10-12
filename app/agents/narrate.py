"""Turn tool-call JSON into plain human sentences for the thinking window.

The agent loop already knows exactly which tool the model asked for and with what
arguments (the canonical :data:`app.agents.claude_adapter.TOOL_SPECS` names). So a
readable "thinking window" needs no extra LLM call — we just template that JSON
into a sentence. This module is pure (no SDK, no I/O), so it imports anywhere and
is trivially unit-testable.

``narrateAction`` describes what the agent is about to do; ``narrateResult``
describes how the action's envelope came back. Anything without a bespoke template
falls back to a humanized tool name plus a compact argument summary, so a newly
added tool still reads sensibly without touching this file.
"""

from __future__ import annotations

from typing import Any


def _short(value: Any, limit: int = 80) -> str:
    """Compact, single-line string form of an argument value."""
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _target(args: dict[str, Any]) -> str:
    """Best human label for what an action targets (selector / description / text)."""
    for key in ("description", "selector", "text", "goal"):
        if args.get(key):
            return _short(args[key])
    return "the element"


# Bespoke templates keyed by tool name. Each takes the args dict and returns a
# sentence. Only the common, user-facing actions need one — the rest fall back.
_ACTION_TEMPLATES: dict[str, Any] = {
    "navigate": lambda a: f"Navigating to {_short(a.get('url', '?'))}",
    "navigate_back": lambda a: "Going back to the previous page",
    "navigate_forward": lambda a: "Going forward to the next page",
    "refresh": lambda a: "Refreshing the page",
    "open_browser": lambda a: "Opening the browser",
    "close_browser": lambda a: "Closing the browser",
    "open_new_tab": lambda a: f"Opening a new tab{(' at ' + _short(a['url'])) if a.get('url') else ''}",
    "switch_tab": lambda a: "Switching tabs",
    "click": lambda a: f"Clicking {_target(a)}",
    "double_click": lambda a: f"Double-clicking {_target(a)}",
    "right_click": lambda a: f"Right-clicking {_target(a)}",
    "click_by_description": lambda a: f"Clicking {_target(a)}",
    "hover": lambda a: f"Hovering over {_target(a)}",
    "fill": lambda a: f"Typing {_short(a.get('value', ''), 60)!r} into {_target(a)}",
    "press_keys": lambda a: f"Pressing {_short(a.get('keys', '?'))}",
    "scroll": lambda a: "Scrolling the page",
    "upload_file": lambda a: f"Uploading {_short(a.get('items', a.get('path', 'a file')))}",
    "download_file": lambda a: "Downloading the file",
    "read_page": lambda a: "Reading the page",
    "get_dom": lambda a: "Reading the page structure",
    "extract_text": lambda a: "Reading the page text",
    "extract_links": lambda a: "Collecting the links on the page",
    "extract_buttons": lambda a: "Looking at the buttons on the page",
    "extract_forms": lambda a: "Looking at the forms on the page",
    "extract_images": lambda a: "Looking at the images on the page",
    "take_screenshot": lambda a: "Taking a screenshot",
    "wait_for_element": lambda a: f"Waiting for {_target(a)}",
    "wait_for_network_idle": lambda a: "Waiting for the page to settle",
    "find_element": lambda a: f"Looking for {_target(a)}",
    "verify_goal": lambda a: f"Checking whether the goal is met: {_short(a.get('goal', ''))}",
    "login_session": lambda a: "Opening a window for you to log in",
    "discover_page": lambda a: "Studying how this page works",
    "discover_website": lambda a: "Studying how this website works",
}


def narrateAction(name: str, args: dict[str, Any] | None = None) -> str:
    """Return a human sentence for a tool call, e.g. 'Navigating to chatgpt.com'."""
    args = args or {}
    template = _ACTION_TEMPLATES.get(name)
    if template is not None:
        return template(args)
    # Fallback: humanize the snake_case name + the most telling argument.
    label = name.replace("_", " ").strip().capitalize()
    detail = _target(args) if args else ""
    return f"{label} {detail}".strip() if detail != "the element" else label


def narrateResult(name: str, result: Any) -> str:
    """Return a short status line for an action's envelope, e.g. '✓ done' / '✗ …'."""
    if not isinstance(result, dict):
        return "✓ done"
    if result.get("success") is False or result.get("error"):
        reason = result.get("error") or result.get("message") or "failed"
        return f"✗ {_short(reason, 120)}"
    return "✓ done"
