"""Anthropic (Claude) provider adapter for the AI Browser Controller.

This module is the *single source of truth* for the browser tool registry that
both AI provider adapters share. It exposes:

* ``TOOL_SPECS`` — a canonical, provider-neutral list of tool specs (snake_case
  ``name``, ``description``, and JSON-schema ``parameters``). The tool *names*
  are snake_case because they are the external protocol names the AI emits;
  they are dispatched to the camelCase :class:`BrowserManager` methods.
* ``dispatchTool`` — an async dispatcher mapping a snake_case tool name to the
  matching ``getBrowserManager()`` method, returning its envelope dict.
* :class:`ClaudeAdapter` — wraps the Anthropic Python SDK and runs the manual
  agentic tool-use loop.

The default model is ``claude-opus-4-8`` and adaptive thinking
(``thinking={"type": "adaptive"}``) is enabled on every request, letting Claude
decide how much to reason per turn. The ``openai_adapter`` module imports the
canonical registry and dispatcher from here to stay DRY.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.browser import intelligence
from app.browser.browser_manager import getBrowserManager
from app.browser.session_pool import (
    closeSession,
    createSession,
    listSessions,
    switchSession,
)
from app.utils.error_handler import safeAsync
from app.utils.logger import getLogger

logger = getLogger("agents.claude")

# Default model for this adapter. Adaptive thinking is enabled on every call.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"

# System prompt for the agentic loop. The tools drive a real, automatable
# Chrome/Chromium via Playwright.
SYSTEM_PROMPT = (
    "You drive a real, automatable Chrome browser through these tools to complete "
    "the user's task. Work in small steps: read the page (read_page / extract_* / "
    "get_dom) to see what's there, then act (navigate, click, fill, scroll, "
    "press_keys), and verify the result before moving on. Prefer precise CSS "
    "selectors from what you actually observed over guessing.\n\n"
    "When a site needs a real human to log in or sign up (credentials, a captcha, "
    "a one-time code), do NOT try to type the user's secrets yourself: call "
    "login_session (optionally with the sign-in url and a profile) to open a "
    "headed window for the user to authenticate, then continue once they are "
    "logged in. Use set_stealth and set_humanize to control anti-bot stealth and "
    "human-like input defaults. Stop when the task is done and report what you found."
)


# --------------------------------------------------------------------------- #
# Canonical, provider-neutral tool registry (single source of truth)
# --------------------------------------------------------------------------- #
# Each entry: {"name": <snake_case protocol name>, "description": str,
#              "parameters": <JSON-schema object>}. Both adapters transform these
# into their own provider tool-schema format via buildTools().
TOOL_SPECS: list[dict[str, Any]] = [
    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    {
        "name": "open_browser",
        "description": "Launch a new browser instance. Optionally choose the browser type, headless mode, viewport size, and user agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "browserType": {
                    "type": "string",
                    "enum": ["chromium", "firefox", "webkit"],
                    "description": "Which browser engine to launch.",
                },
                "headless": {
                    "type": "boolean",
                    "description": "Run without a visible window when true.",
                },
                "viewportWidth": {"type": "integer", "description": "Viewport width in pixels."},
                "viewportHeight": {"type": "integer", "description": "Viewport height in pixels."},
                "userAgent": {"type": "string", "description": "Custom User-Agent header string."},
            },
            "required": [],
        },
    },
    {
        "name": "close_browser",
        "description": "Close the currently running browser instance and free its resources. The persistent profile (logins/cookies) is saved on close.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_headless",
        "description": "Switch a running browser between headless and headed WITHOUT losing state. Set headless=false to show a real window so a human can solve a captcha / 'are you human' check or log in manually; set true to hide it again.",
        "parameters": {
            "type": "object",
            "properties": {
                "headless": {"type": "boolean", "description": "true = no window, false = visible window for manual interaction."},
            },
            "required": ["headless"],
        },
    },
    {
        "name": "clear_profile",
        "description": "Wipe the persistent profile — logs out of everything for a fresh session (deletes all saved cookies/tokens).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # ------------------------------------------------------------------ #
    # Stealth / human-like input controls
    # ------------------------------------------------------------------ #
    {
        "name": "set_stealth",
        "description": "Toggle STEALTH (anti-bot fingerprinting hardening). Updates the stealth default applied on the next browser launch.",
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "true to enable stealth, false to disable."},
            },
            "required": ["enabled"],
        },
    },
    {
        "name": "set_humanize",
        "description": "Toggle HUMANIZED input (human-like cursor/typing/scrolling) as the default for click/fill/scroll (each tool can still override per-call).",
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "true for human-like input by default, false for instant."},
            },
            "required": ["enabled"],
        },
    },
    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    {
        "name": "navigate",
        "description": "Navigate the active tab to a URL. Launches the browser automatically if it is not already running.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The absolute URL to open."},
                "waitUntil": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                    "description": "When to consider navigation finished.",
                },
                "timeoutMs": {"type": "integer", "description": "Navigation timeout in milliseconds."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "navigate_back",
        "description": "Go back to the previous page in the active tab's history.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "navigate_forward",
        "description": "Go forward to the next page in the active tab's history.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "refresh",
        "description": "Reload the current page in the active tab.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #
    {
        "name": "open_new_tab",
        "description": "Open a new tab, optionally navigating it to a URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Optional URL to open in the new tab."},
            },
            "required": [],
        },
    },
    {
        "name": "switch_tab",
        "description": "Switch the active tab to the one at the given zero-based index.",
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Zero-based index of the tab to activate."},
            },
            "required": ["index"],
        },
    },
    {
        "name": "close_tab",
        "description": "Close a tab by zero-based index, or the active tab when no index is given.",
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Zero-based index of the tab to close."},
            },
            "required": [],
        },
    },
    # ------------------------------------------------------------------ #
    # Extraction / info
    # ------------------------------------------------------------------ #
    {
        "name": "get_title",
        "description": "Get the title of the current page.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_url",
        "description": "Get the URL of the current page.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "extract_text",
        "description": "Extract the visible text content of the current page.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "extract_links",
        "description": "Extract all hyperlinks (anchor elements) from the current page.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "extract_buttons",
        "description": "Extract all clickable button elements from the current page.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "extract_forms",
        "description": "Extract all form elements and their fields from the current page.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "extract_images",
        "description": "Extract all image elements (and their sources) from the current page.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_dom",
        "description": "Get the DOM/HTML of the page, optionally scoped to a CSS selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Optional CSS selector to scope the DOM to."},
            },
            "required": [],
        },
    },
    # ------------------------------------------------------------------ #
    # Interaction
    # ------------------------------------------------------------------ #
    {
        "name": "scroll",
        "description": "Scroll the page or a specific element. Use deltaX/deltaY for relative scrolling, or toTop/toBottom to jump.",
        "parameters": {
            "type": "object",
            "properties": {
                "deltaX": {"type": "integer", "description": "Horizontal scroll amount in pixels."},
                "deltaY": {"type": "integer", "description": "Vertical scroll amount in pixels."},
                "selector": {"type": "string", "description": "Optional CSS selector of the scroll container."},
                "toTop": {"type": "boolean", "description": "Scroll to the very top when true."},
                "toBottom": {"type": "boolean", "description": "Scroll to the very bottom when true."},
            },
            "required": [],
        },
    },
    {
        "name": "hover",
        "description": "Hover the mouse over the element matching a CSS selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the element to hover."},
                "timeoutMs": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "click",
        "description": "Click the element matching a CSS selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the element to click."},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Mouse button to use.",
                },
                "clickCount": {"type": "integer", "description": "Number of clicks."},
                "timeoutMs": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "double_click",
        "description": "Double-click the element matching a CSS selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the element to double-click."},
                "timeoutMs": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "right_click",
        "description": "Right-click (context-click) the element matching a CSS selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the element to right-click."},
                "timeoutMs": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "fill",
        "description": "Fill an input or textarea matching a CSS selector with a value.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the field to fill."},
                "value": {"type": "string", "description": "Text to enter into the field."},
                "clearFirst": {"type": "boolean", "description": "Clear existing content before filling."},
                "timeoutMs": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "upload_file",
        "description": "Upload one or more local files to a file input matching a CSS selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the file input."},
                "filePaths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of the files to upload.",
                },
            },
            "required": ["selector", "filePaths"],
        },
    },
    {
        "name": "download_file",
        "description": "Trigger a download by clicking an element, optionally saving to a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the element that starts the download."},
                "saveDir": {"type": "string", "description": "Optional directory to save the file into."},
                "timeoutMs": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "press_keys",
        "description": "Press a key or key combination, optionally focused on an element first.",
        "parameters": {
            "type": "object",
            "properties": {
                "keys": {"type": "string", "description": "Key or combination, e.g. 'Enter' or 'Control+A'."},
                "selector": {"type": "string", "description": "Optional CSS selector to focus before pressing."},
            },
            "required": ["keys"],
        },
    },
    # ------------------------------------------------------------------ #
    # Waits
    # ------------------------------------------------------------------ #
    {
        "name": "wait_for_element",
        "description": "Wait until an element matching a CSS selector reaches the given state.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the element to wait for."},
                "state": {
                    "type": "string",
                    "enum": ["attached", "detached", "visible", "hidden"],
                    "description": "Target state to wait for.",
                },
                "timeoutMs": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "wait_for_network_idle",
        "description": "Wait until network activity has been idle, up to an optional timeout.",
        "parameters": {
            "type": "object",
            "properties": {
                "timeoutMs": {"type": "integer", "description": "Timeout in milliseconds."},
            },
            "required": [],
        },
    },
    # ------------------------------------------------------------------ #
    # Visual intelligence
    # ------------------------------------------------------------------ #
    {
        "name": "take_screenshot",
        "description": "Capture a screenshot of the page, optionally full-page, scoped to a selector, or annotated.",
        "parameters": {
            "type": "object",
            "properties": {
                "fullPage": {"type": "boolean", "description": "Capture the full scrollable page."},
                "selector": {"type": "string", "description": "Optional CSS selector to screenshot only that element."},
                "annotate": {"type": "boolean", "description": "Overlay annotations on the screenshot."},
                "label": {"type": "string", "description": "Optional label/name for the screenshot file."},
            },
            "required": [],
        },
    },
    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    {
        "name": "start_recording",
        "description": "Start recording a video of the browser session.",
        "parameters": {
            "type": "object",
            "properties": {
                "fps": {"type": "integer", "description": "Frames per second for the recording."},
                "sessionName": {"type": "string", "description": "Optional session name for the recording file."},
            },
            "required": [],
        },
    },
    {
        "name": "stop_recording",
        "description": "Stop the active video recording and finalize the file.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    {
        "name": "status",
        "description": "Get a snapshot of the current browser state (tabs, running status, recording flag).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # ------------------------------------------------------------------ #
    # Aggregate read + network inspection
    # ------------------------------------------------------------------ #
    {
        "name": "read_page",
        "description": "Read the whole page in ONE call: title, URL, visible text, links, buttons, forms, and image count. Fastest way to understand a page before acting.",
        "parameters": {
            "type": "object",
            "properties": {
                "textLimit": {"type": "integer", "description": "Max characters of visible text to return."},
            },
            "required": [],
        },
    },
    {
        "name": "get_network",
        "description": "Return the network requests/responses the browser has made (URL, method, status, resource type, ok). Inspect API/XHR/fetch traffic and loaded assets.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max most-recent entries to return."},
                "urlContains": {"type": "string", "description": "Only return entries whose URL contains this substring."},
            },
            "required": [],
        },
    },
    {
        "name": "clear_network",
        "description": "Clear the captured network log (e.g. before triggering an action to isolate its traffic).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "clear_storage",
        "description": "Delete saved screenshots/recordings/downloads to free disk space. Optionally target a subset via 'kinds'.",
        "parameters": {
            "type": "object",
            "properties": {
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["screenshots", "recordings", "downloads"]},
                    "description": "Which storage kinds to clear. Defaults to all.",
                },
            },
            "required": [],
        },
    },
    # ------------------------------------------------------------------ #
    # Tab intelligence
    # ------------------------------------------------------------------ #
    {
        "name": "get_tabs",
        "description": "Summarise every open tab (index, title, URL, host, which is active). Use it to keep track of many tabs.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # ------------------------------------------------------------------ #
    # Accessibility & visual QA
    # ------------------------------------------------------------------ #
    {
        "name": "get_accessibility_tree",
        "description": "Return the page's accessibility tree (roles/names a screen reader sees) — often clearer than raw HTML for understanding structure.",
        "parameters": {
            "type": "object",
            "properties": {
                "interestingOnly": {"type": "boolean", "description": "Prune presentational nodes (default true)."},
                "root": {"type": "string", "description": "Optional CSS selector to scope the snapshot."},
            },
            "required": [],
        },
    },
    {
        "name": "audit_page",
        "description": "Audit the page for UI defects: horizontal overflow, hidden interactive elements, broken images, and low text contrast. Returns counts plus samples.",
        "parameters": {
            "type": "object",
            "properties": {
                "sampleLimit": {"type": "integer", "description": "Max text elements the contrast pass inspects."},
            },
            "required": [],
        },
    },
    {
        "name": "compare_screenshots",
        "description": "Compare two screenshot files and quantify what changed: visualDifferencePercent and changedRegions with bounding boxes. Use before/after take_screenshot files.",
        "parameters": {
            "type": "object",
            "properties": {
                "before": {"type": "string", "description": "Path to the baseline screenshot."},
                "after": {"type": "string", "description": "Path to the screenshot to compare."},
                "pixelThreshold": {"type": "integer", "description": "Per-pixel change sensitivity (0-765)."},
                "saveDiff": {"type": "boolean", "description": "Also write a change-mask PNG and return its path."},
            },
            "required": ["before", "after"],
        },
    },
    # ------------------------------------------------------------------ #
    # Browser-state snapshot (cookies + storage + open tabs)
    # ------------------------------------------------------------------ #
    {
        "name": "create_snapshot",
        "description": "Capture the full browser state (cookies, localStorage, sessionStorage, open-tab URLs) to a JSON file for later restore.",
        "parameters": {
            "type": "object",
            "properties": {
                "savePath": {"type": "string", "description": "Optional output path; omit to auto-name."},
            },
            "required": [],
        },
    },
    {
        "name": "restore_snapshot",
        "description": "Restore cookies + storage from a snapshot file made by create_snapshot (navigates to the snapshot URL first).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a snapshot JSON from create_snapshot."},
                "navigate": {"type": "boolean", "description": "Re-open the snapshot URL before restoring storage (default true)."},
            },
            "required": ["path"],
        },
    },
    # ------------------------------------------------------------------ #
    # Session replay (structured, replayable action log — not video)
    # ------------------------------------------------------------------ #
    {
        "name": "start_session",
        "description": "Start recording replayable actions (click/fill/navigate/...) as structured JSON steps. Unlike start_recording (video), this is machine-replayable.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Optional session name; omit to auto-generate."},
            },
            "required": [],
        },
    },
    {
        "name": "stop_session",
        "description": "Stop the structured action recording and return the captured steps.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_session",
        "description": "Save the recorded session to a JSON file for later load/replay.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional output path; omit to auto-name."},
            },
            "required": [],
        },
    },
    {
        "name": "load_session",
        "description": "Load a previously saved session JSON into memory, ready to replay.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a session JSON saved by save_session."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "replay_session",
        "description": "Replay a recorded session's steps in order against the live browser. Loads 'path' first if given, else replays the in-memory session.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional session JSON to load before replaying."},
                "delayMs": {"type": "integer", "description": "Pause between steps in milliseconds (default 500)."},
                "continueOnError": {"type": "boolean", "description": "Keep going after a failed step (default true)."},
            },
            "required": [],
        },
    },
    # ------------------------------------------------------------------ #
    # Browser memory
    # ------------------------------------------------------------------ #
    {
        "name": "remember_page",
        "description": "Save the CURRENT page into memory (title, URL, structure, screenshot) so you can recall it later without re-scraping.",
        "parameters": {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional labels to attach."},
                "withScreenshot": {"type": "boolean", "description": "Also store a screenshot (default true)."},
            },
            "required": [],
        },
    },
    {
        "name": "search_memory",
        "description": "Recall remembered pages matching a query (keyword-ranked). Avoids re-visiting pages you've already seen.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to match against remembered pages."},
                "limit": {"type": "integer", "description": "Max matches to return (default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_memory",
        "description": "List the most recently remembered pages.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max pages to list (default 50)."}},
            "required": [],
        },
    },
    {
        "name": "clear_memory",
        "description": "Forget all remembered pages.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # ------------------------------------------------------------------ #
    # Website Skill System (persistent per-domain operational knowledge)
    # ------------------------------------------------------------------ #
    {
        "name": "discover_page",
        "description": "Learn how the CURRENT page works (or navigate to 'url' first): purpose, UI, forms, navigation, inferred workflows. Saves a persistent route skill (LEARN mode). Safe & read-only — never clicks destructive controls.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Optional URL to navigate to before discovering."},
            },
            "required": [],
        },
    },
    {
        "name": "discover_website",
        "description": "Crawl internal routes from the current/start page (depth-bounded, safe) and learn a skill for each. Follows GET-like internal links only; never submits forms or clicks destructive UI.",
        "parameters": {
            "type": "object",
            "properties": {
                "startUrl": {"type": "string", "description": "Optional URL to start from."},
                "maxPages": {"type": "integer", "description": "Max pages to visit (default 10, hard cap 50)."},
            },
            "required": [],
        },
    },
    {
        "name": "update_skill",
        "description": "Record an outcome for a route's skill to tune its confidence. Pass success=true after a flow worked, success=false after it failed, or an explicit confidenceDelta. Low confidence triggers auto-rediscovery.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL of the route whose skill to update."},
                "success": {"type": "boolean", "description": "true bumps confidence, false drops it."},
                "confidenceDelta": {"type": "integer", "description": "Explicit confidence change (overrides success)."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_skills",
        "description": "List learned websites, or one domain's routes + workflows. Reads JSON indexes only (never scans folders).",
        "parameters": {
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Optional domain (e.g. 'github.com') to drill into."}},
            "required": [],
        },
    },
    {
        "name": "search_skills",
        "description": "Keyword-search learned skills across domains/routes via the JSON indexes.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to match against domains/routes/titles."},
                "limit": {"type": "integer", "description": "Max results (default 20)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "export_skills",
        "description": "Export learned skills (one domain or all) as a portable bundle, optionally writing to a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Optional single domain to export."},
                "savePath": {"type": "string", "description": "Optional file path to write the bundle to."},
            },
            "required": [],
        },
    },
    {
        "name": "import_skills",
        "description": "Import a skills bundle (inline 'bundle' dict or from 'path'). Skips existing files unless overwrite=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "bundle": {"type": "object", "description": "Inline bundle from export_skills."},
                "path": {"type": "string", "description": "Path to a bundle JSON file."},
                "overwrite": {"type": "boolean", "description": "Overwrite existing skill files (default false)."},
            },
            "required": [],
        },
    },
    {
        "name": "clear_skills",
        "description": "Forget one domain's skills (pass 'domain'), or wipe the entire skill store.",
        "parameters": {
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "Optional domain to clear; omit to wipe all."}},
            "required": [],
        },
    },
    {
        "name": "set_discovery_mode",
        "description": "Set the Website Skill System mode: OFF (disabled), READ_ONLY (read but never modify), or LEARN (read + discover + update — default).",
        "parameters": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["OFF", "READ_ONLY", "LEARN"], "description": "New mode."}},
            "required": ["mode"],
        },
    },
    {
        "name": "get_discovery_status",
        "description": "Report discovery mode, storage path, learned-website count, and thresholds.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # ------------------------------------------------------------------ #
    # OCR
    # ------------------------------------------------------------------ #
    {
        "name": "extract_text_from_screenshot",
        "description": "Screenshot the page (or an element) and OCR the text out of the pixels. For info inside images/canvas not in the DOM. Requires Tesseract.",
        "parameters": {
            "type": "object",
            "properties": {
                "fullPage": {"type": "boolean", "description": "Capture the full scrollable page."},
                "selector": {"type": "string", "description": "Optional CSS selector to OCR only that element."},
                "lang": {"type": "string", "description": "Tesseract language code (default eng)."},
            },
            "required": [],
        },
    },
    {
        "name": "read_image",
        "description": "OCR a local image file and return its text. Requires Tesseract.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Path to an image file on disk."},
                "lang": {"type": "string", "description": "Tesseract language code (default eng)."},
            },
            "required": ["source"],
        },
    },
    # ------------------------------------------------------------------ #
    # AI judgment (provider-backed): goal check, element finding, planning
    # ------------------------------------------------------------------ #
    {
        "name": "verify_goal",
        "description": "Look at the current page and judge whether a natural-language GOAL is met. Returns {success, confidence, reason}. Self-correcting automation.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The condition to check, in plain language."},
                "fullPage": {"type": "boolean", "description": "Verify against a full-page screenshot."},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "find_element",
        "description": "Find the element best matching a natural-language description (e.g. 'blue login button'). Returns a usable CSS selector + confidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What the element looks like / does."},
                "limit": {"type": "integer", "description": "Max interactive candidates to consider (default 60)."},
            },
            "required": ["description"],
        },
    },
    {
        "name": "click_by_description",
        "description": "Locate an element by natural-language description and CLICK it (find_element + click).",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Plain-language description of the element to click."},
                "limit": {"type": "integer", "description": "Max candidates to consider (default 60)."},
                "humanize": {"type": "boolean", "description": "Force human-like (true) or instant (false) clicking."},
            },
            "required": ["description"],
        },
    },
    {
        "name": "plan_actions",
        "description": "Ask for an ordered action PLAN to achieve a goal so you can inspect it before executing. Returns a JSON list of steps.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The high-level objective to plan for."},
                "includeContext": {"type": "boolean", "description": "Include current page context (default true)."},
            },
            "required": ["goal"],
        },
    },
    # ------------------------------------------------------------------ #
    # Workflows (named, saved sessions)
    # ------------------------------------------------------------------ #
    {
        "name": "save_workflow",
        "description": "Save the current recorded session as a named, re-runnable workflow (record once, run later with no AI).",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "A name for the workflow."}},
            "required": ["name"],
        },
    },
    {
        "name": "run_workflow",
        "description": "Replay a previously saved workflow by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The workflow to run."},
                "delayMs": {"type": "integer", "description": "Pause between steps (default 500)."},
                "continueOnError": {"type": "boolean", "description": "Keep going after a failed step (default true)."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_workflows",
        "description": "List saved workflows by name.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # ------------------------------------------------------------------ #
    # Browser sessions (pool of isolated browsers; one active at a time)
    # ------------------------------------------------------------------ #
    {
        "name": "create_session",
        "description": "Create a new, isolated browser session (own cookies/tabs/state) and make it active. Other tools act on the active session.",
        "parameters": {
            "type": "object",
            "properties": {
                "sessionId": {"type": "string", "description": "Optional id; omit to auto-generate."},
                "makeActive": {"type": "boolean", "description": "Make it active immediately (default true)."},
            },
            "required": [],
        },
    },
    {
        "name": "list_sessions",
        "description": "List all browser sessions (id, active flag, running, url, tab count).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "switch_session",
        "description": "Make a different browser session the active one.",
        "parameters": {
            "type": "object",
            "properties": {"sessionId": {"type": "string", "description": "The session id to activate."}},
            "required": ["sessionId"],
        },
    },
    {
        "name": "close_session",
        "description": "Close a browser session (or the active one when omitted).",
        "parameters": {
            "type": "object",
            "properties": {"sessionId": {"type": "string", "description": "The session to close; omit for active."}},
            "required": [],
        },
    },
]


# --------------------------------------------------------------------------- #
# Canonical async dispatcher (shared by both adapters)
# --------------------------------------------------------------------------- #
@safeAsync(action="dispatch_tool")
async def dispatchTool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Map a snake_case tool *name* to the matching ``BrowserManager`` method.

    Args:
        name: The snake_case tool name emitted by the AI (e.g. ``"navigate"``).
        arguments: The parsed tool input as a dict of keyword arguments.

    Returns:
        The AI-friendly envelope dict produced by the ``BrowserManager`` method.
        Unknown tool names yield a structured error envelope rather than raising.
    """
    arguments = arguments or {}
    manager = getBrowserManager()
    logger.info("Dispatching tool '%s' with args: %s", name, arguments)

    if name == "open_browser":
        return await manager.openBrowser(**arguments)
    if name == "close_browser":
        return await manager.closeBrowser()
    if name == "set_headless":
        return await manager.setHeadless(arguments.get("headless", True))
    if name == "clear_profile":
        return await manager.clearProfile()
    if name == "set_stealth":
        return await manager.setStealth(arguments["enabled"])
    if name == "set_humanize":
        return await manager.setHumanize(arguments["enabled"])
    if name == "navigate":
        return await manager.navigate(
            url=arguments["url"],
            waitUntil=arguments.get("waitUntil", "networkidle"),
            timeoutMs=arguments.get("timeoutMs"),
        )
    if name == "navigate_back":
        return await manager.navigateBack()
    if name == "navigate_forward":
        return await manager.navigateForward()
    if name == "refresh":
        return await manager.refresh()
    if name == "open_new_tab":
        return await manager.openNewTab(url=arguments.get("url"))
    if name == "switch_tab":
        return await manager.switchTab(index=arguments["index"])
    if name == "close_tab":
        return await manager.closeTab(index=arguments.get("index"))
    if name == "get_title":
        return await manager.getTitle()
    if name == "get_url":
        return await manager.getUrl()
    if name == "extract_text":
        return await manager.extractText()
    if name == "extract_links":
        return await manager.extractLinks()
    if name == "extract_buttons":
        return await manager.extractButtons()
    if name == "extract_forms":
        return await manager.extractForms()
    if name == "extract_images":
        return await manager.extractImages()
    if name == "get_dom":
        return await manager.getDom(selector=arguments.get("selector"))
    if name == "scroll":
        return await manager.scroll(**arguments)
    if name == "hover":
        return await manager.hover(selector=arguments["selector"], timeoutMs=arguments.get("timeoutMs"))
    if name == "click":
        return await manager.click(
            selector=arguments["selector"],
            button=arguments.get("button", "left"),
            clickCount=arguments.get("clickCount", 1),
            timeoutMs=arguments.get("timeoutMs"),
        )
    if name == "double_click":
        return await manager.doubleClick(selector=arguments["selector"], timeoutMs=arguments.get("timeoutMs"))
    if name == "right_click":
        return await manager.rightClick(selector=arguments["selector"], timeoutMs=arguments.get("timeoutMs"))
    if name == "fill":
        return await manager.fill(
            selector=arguments["selector"],
            value=arguments["value"],
            clearFirst=arguments.get("clearFirst", True),
            timeoutMs=arguments.get("timeoutMs"),
        )
    if name == "upload_file":
        return await manager.uploadFile(selector=arguments["selector"], filePaths=arguments["filePaths"])
    if name == "download_file":
        return await manager.downloadFile(
            selector=arguments["selector"],
            saveDir=arguments.get("saveDir"),
            timeoutMs=arguments.get("timeoutMs"),
        )
    if name == "press_keys":
        return await manager.pressKeys(keys=arguments["keys"], selector=arguments.get("selector"))
    if name == "wait_for_element":
        return await manager.waitForElement(
            selector=arguments["selector"],
            state=arguments.get("state", "visible"),
            timeoutMs=arguments.get("timeoutMs"),
        )
    if name == "wait_for_network_idle":
        return await manager.waitForNetworkIdle(timeoutMs=arguments.get("timeoutMs"))
    if name == "take_screenshot":
        return await manager.takeScreenshot(
            fullPage=arguments.get("fullPage", False),
            selector=arguments.get("selector"),
            annotate=arguments.get("annotate", False),
            label=arguments.get("label"),
        )
    if name == "start_recording":
        return await manager.startRecording(fps=arguments.get("fps"), sessionName=arguments.get("sessionName"))
    if name == "stop_recording":
        return await manager.stopRecording()
    if name == "status":
        return await manager.status()
    if name == "read_page":
        return await manager.readPage(textLimit=arguments.get("textLimit", 5000))
    if name == "get_network":
        return await manager.getNetwork(
            limit=arguments.get("limit", 100),
            urlContains=arguments.get("urlContains"),
        )
    if name == "clear_network":
        return await manager.clearNetwork()
    if name == "clear_storage":
        return await manager.clearStorage(arguments.get("kinds"))
    if name == "get_tabs":
        return await manager.getTabs()
    if name == "get_accessibility_tree":
        return await manager.getAccessibilityTree(
            arguments.get("interestingOnly", True), arguments.get("root")
        )
    if name == "audit_page":
        return await manager.auditPage(arguments.get("sampleLimit", 400))
    if name == "compare_screenshots":
        return await manager.compareScreenshots(
            arguments["before"],
            arguments["after"],
            pixelThreshold=arguments.get("pixelThreshold", 60),
            saveDiff=arguments.get("saveDiff", False),
        )
    if name == "create_snapshot":
        return await manager.createSnapshot(savePath=arguments.get("savePath"))
    if name == "restore_snapshot":
        return await manager.restoreSnapshot(
            path=arguments.get("path"),
            snapshot=arguments.get("snapshot"),
            navigate=arguments.get("navigate", True),
        )
    if name == "start_session":
        return await manager.startSession(arguments.get("name"))
    if name == "stop_session":
        return await manager.stopSession()
    if name == "get_session":
        return await manager.getSession()
    if name == "save_session":
        return await manager.saveSession(arguments.get("path"))
    if name == "load_session":
        return await manager.loadSession(arguments["path"])
    if name == "replay_session":
        return await manager.replaySession(
            path=arguments.get("path"),
            delayMs=arguments.get("delayMs", 500),
            continueOnError=arguments.get("continueOnError", True),
        )
    if name == "remember_page":
        return await manager.rememberPage(
            tags=arguments.get("tags"), withScreenshot=arguments.get("withScreenshot", True)
        )
    if name == "search_memory":
        return await manager.searchMemory(arguments["query"], limit=arguments.get("limit", 10))
    if name == "list_memory":
        return await manager.listMemory(limit=arguments.get("limit", 50))
    if name == "clear_memory":
        return await manager.clearMemory()
    if name == "discover_page":
        return await manager.discoverPage(arguments.get("url"))
    if name == "discover_website":
        return await manager.discoverWebsite(
            startUrl=arguments.get("startUrl"), maxPages=arguments.get("maxPages", 10)
        )
    if name == "update_skill":
        return await manager.updateSkill(
            arguments["url"], success=arguments.get("success"),
            confidenceDelta=arguments.get("confidenceDelta"),
        )
    if name == "list_skills":
        return await manager.listSkills(arguments.get("domain"))
    if name == "search_skills":
        return await manager.searchSkills(arguments["query"], limit=arguments.get("limit", 20))
    if name == "export_skills":
        return await manager.exportSkills(arguments.get("domain"), savePath=arguments.get("savePath"))
    if name == "import_skills":
        return await manager.importSkills(
            bundle=arguments.get("bundle"), path=arguments.get("path"),
            overwrite=arguments.get("overwrite", False),
        )
    if name == "clear_skills":
        return await manager.clearSkills(arguments.get("domain"))
    if name == "set_discovery_mode":
        return await manager.setDiscoveryMode(arguments["mode"])
    if name == "get_discovery_status":
        return await manager.getDiscoveryStatus()
    if name == "extract_text_from_screenshot":
        return await manager.extractTextFromScreenshot(
            fullPage=arguments.get("fullPage", False),
            selector=arguments.get("selector"),
            lang=arguments.get("lang", "eng"),
        )
    if name == "read_image":
        return await manager.readImage(arguments["source"], lang=arguments.get("lang", "eng"))
    if name == "verify_goal":
        return await manager.verifyGoal(arguments["goal"], fullPage=arguments.get("fullPage", False))
    if name == "find_element":
        return await manager.findElement(arguments["description"], limit=arguments.get("limit", 60))
    if name == "click_by_description":
        return await manager.clickByDescription(
            arguments["description"], limit=arguments.get("limit", 60),
            humanize=arguments.get("humanize"),
        )
    if name == "plan_actions":
        return await manager.planActions(
            arguments["goal"], includeContext=arguments.get("includeContext", True)
        )
    if name == "save_workflow":
        return await manager.saveWorkflow(arguments["name"])
    if name == "run_workflow":
        return await manager.runWorkflow(
            arguments["name"], delayMs=arguments.get("delayMs", 500),
            continueOnError=arguments.get("continueOnError", True),
        )
    if name == "list_workflows":
        return await manager.listWorkflows()
    if name == "create_session":
        return createSession(arguments.get("sessionId"), makeActive=arguments.get("makeActive", True))
    if name == "list_sessions":
        return listSessions()
    if name == "switch_session":
        return switchSession(arguments["sessionId"])
    if name == "close_session":
        return await closeSession(arguments.get("sessionId"))

    logger.warning("Unknown tool requested: %s", name)
    return {
        "success": False,
        "error": "unknown_tool",
        "details": f"No handler registered for tool '{name}'.",
    }


class ClaudeAdapter:
    """Adapter that drives a manual agentic tool-use loop with Anthropic's SDK.

    The adapter transforms the canonical :data:`TOOL_SPECS` into Anthropic
    ``input_schema`` tool definitions and runs the conversation loop against the
    Messages API with adaptive thinking enabled.
    """

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL) -> None:
        """Construct the adapter.

        Args:
            model: The Claude model id to use. Defaults to ``claude-opus-4-8``.
        """
        self.model: str = model
        self.apiKey: str | None = os.getenv("ANTHROPIC_API_KEY")

    def buildTools(self) -> list[dict[str, Any]]:
        """Build Anthropic tool definitions from the canonical registry.

        Returns:
            A list of dicts shaped as
            ``{"name", "description", "input_schema"}`` for the full tool set.
        """
        return [
            {
                "name": spec["name"],
                "description": spec["description"],
                "input_schema": spec["parameters"],
            }
            for spec in TOOL_SPECS
        ]

    async def dispatchTool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call to the shared :func:`dispatchTool`.

        Args:
            name: The snake_case tool name.
            arguments: The parsed tool input.

        Returns:
            The envelope dict from the ``BrowserManager``.
        """
        return await dispatchTool(name, arguments)

    async def runConversation(self, userPrompt: str, maxTurns: int = 10) -> dict[str, Any]:
        """Run the agentic tool-use loop for a single user prompt.

        Lazily imports the ``anthropic`` SDK so the module imports even when the
        SDK is not installed. Raises a clear :class:`RuntimeError` if the SDK or
        the API key is missing.

        Args:
            userPrompt: The user's natural-language instruction.
            maxTurns: Maximum number of assistant turns before stopping.

        Returns:
            A summary dict ``{"finalText", "turns", "toolCalls"}``.
        """
        try:
            import anthropic  # noqa: PLC0415 - lazy import by design
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "The 'anthropic' package is required to run a Claude conversation. "
                "Install it with: pip install anthropic"
            ) from exc

        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export your Anthropic API key before "
                "calling runConversation()."
            )

        # Judgment tools (verify_goal / find_element / plan_actions) this run
        # invokes should reason with Claude too — match the driver.
        intelligence.setAiProvider("claude")

        # Reads ANTHROPIC_API_KEY from the environment.
        client = anthropic.Anthropic()

        tools = self.buildTools()
        messages: list[dict[str, Any]] = [{"role": "user", "content": userPrompt}]
        toolCalls: list[dict[str, Any]] = []
        finalTextParts: list[str] = []
        turns = 0

        for _ in range(maxTurns):
            turns += 1
            response = client.messages.create(
                model=self.model,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )

            # Preserve the full assistant content (thinking + text + tool_use).
            messages.append({"role": "assistant", "content": response.content})

            # Collect any text blocks emitted this turn.
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    finalTextParts.append(block.text)

            if response.stop_reason == "tool_use":
                toolResults: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    logger.info("Claude requested tool '%s'", block.name)
                    result = await self.dispatchTool(block.name, block.input)
                    toolCalls.append({"name": block.name, "input": block.input, "result": result})
                    toolResults.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )
                messages.append({"role": "user", "content": toolResults})
                continue

            # Any non-tool stop reason (typically "end_turn") ends the loop.
            break

        return {
            "finalText": "".join(finalTextParts),
            "turns": turns,
            "toolCalls": toolCalls,
        }


async def main() -> None:
    """Usage example: construct the adapter and print its Anthropic tool schema.

    The API is intentionally NOT called here, since no key may be present in the
    environment. To exercise a real conversation, set ``ANTHROPIC_API_KEY`` and
    call ``await adapter.runConversation(...)``.
    """
    adapter = ClaudeAdapter()
    tools = adapter.buildTools()
    print(json.dumps(tools, indent=2))
    print(f"\nBuilt {len(tools)} Anthropic tool definitions for model '{adapter.model}'.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
