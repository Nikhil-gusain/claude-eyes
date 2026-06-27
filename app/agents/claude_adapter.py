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

from app.browser.browser_manager import getBrowserManager
from app.utils.error_handler import safeAsync
from app.utils.logger import getLogger

logger = getLogger("agents.claude")

# Default model for this adapter. Adaptive thinking is enabled on every call.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"


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
