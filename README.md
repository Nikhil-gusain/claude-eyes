# AI Browser Controller

A plug-and-play browser automation layer that lets any AI agent — primarily **Claude** — control a real browser. It can navigate to pages, inspect their UI/UX, take screenshots, record sessions to MP4, and report back **structured JSON** that an agent can reason over. The same browser-control surface is exposed three ways — a FastAPI HTTP + WebSocket server, an MCP stdio server, and in-process AI adapters — so you can drop it into a Claude Desktop config, a custom agent loop, or your own backend without rewriting anything.

## Features

- **Full browser control** — launch/close, navigate (back/forward/refresh), tabs, click/double-click/right-click, hover, fill inputs, scroll, press keys, upload/download files, and waits — backed by Playwright.
- **Humanized interaction (anti bot-detection)** — on by default: typing is paced (~25 WPM with natural jitter), the mouse travels a curved, wobbling path to a random point inside the target (never a straight teleport to the exact center) and remembers where it was, and scrolling is lazy/incremental like a human discovering the page. Force on/off per call with `humanize`, or globally with `ABC_HUMANIZE`. Optionally drive real Chrome via `ABC_BROWSER_CHANNEL=chrome`.
- **Multiple Chrome profiles** — named, isolated profiles (each its own logins/cookies). The chosen profile is **remembered on disk and auto-reopens in later chats**; `open_browser` asks which profile to use (or random) the first time. Tools: `list_profiles`, `select_profile`, `create_profile`, and `login_session` (opens a headed window so a human can log in / sign up, then saves the session for future automated runs).
- **Event-driven waiting** — wait *quietly* for slow things instead of polling: `wait_for_stable` resolves when an element's text stops changing (perfect for an online AI's streamed answer), and `wait_for_response` resolves when a matching network response finishes streaming. Capped at 5 min by default (`ABC_MAX_WAIT_MS`); downloads get up to ~1h (`ABC_MAX_DOWNLOAD_WAIT_MS`). An SSE `/events` stream pushes progress to HTTP clients.
- **Safe downloads** — `download_file` keeps **verified real images only** by default: the saved bytes are inspected (magic number + full decode) and anything that is actually an executable/app/archive disguised as an image is deleted and reported as an error.
- **No-image mode (MarkItDown)** — a global toggle (`set_no_image_mode`) that suppresses pixel screenshots in favour of text, plus a `to_markdown` tool that converts images/PDF/Office/HTML (file or URL) to markdown via Microsoft's MarkItDown.
- **Visual intelligence / screenshots** — capture the viewport, the full scrollable page, or a single element; optional annotation with a label.
- **Screen recording to MP4** — record a browser session and finalize it to a video file (ffmpeg-backed).
- **MCP server** — every action is published as an MCP tool over stdio, so Claude Desktop (and any MCP client) can drive the browser directly.
- **FastAPI + WebSocket** — a REST surface plus a persistent `/ws` socket that speaks the same action vocabulary.
- **Claude & OpenAI adapters** — in-process adapters that map the browser actions to provider tool-calling so an agent can run an autonomous loop.
- **AI-friendly structured JSON** — every action, on every transport, returns the same success/error envelope so agents see one consistent contract.

## Architecture

```
ai-browser-controller/
├── app/
│   ├── browser/      # Playwright control, screenshots, recording, and the BrowserManager facade
│   ├── api/          # FastAPI server (server.py) + WebSocket route (websocket.py)
│   ├── agents/       # Claude & OpenAI adapters that map tools -> BrowserManager
│   ├── mcp/          # MCP stdio server exposing every action as a tool
│   ├── models/       # Pydantic command/response models (request + envelope shapes)
│   ├── storage/      # Saved artifacts: screenshots/, recordings/ (gitkept, contents ignored)
│   └── utils/        # config (settings), logger, and shared helpers (response envelopes)
├── tests/            # pytest suite (browser tests auto-skip without Playwright browsers)
├── start.py          # CLI entrypoint: `api` | `mcp` | `info`
├── requirements.txt
├── pyproject.toml
├── .env.example
└── README.md
```

Module map:

| Module | Responsibility |
| --- | --- |
| `app/browser/playwright_controller.py` | Thin async wrapper over Playwright (launch, navigate, interact, extract). Routes typing/clicking/scrolling through the humanizer and tracks cursor position. |
| `app/browser/humanize.py` | Human-like primitives: paced typing, curved/jittered mouse travel, lazy scroll. |
| `app/browser/profiles.py` | `ProfileManager` — named profiles under `storage/profiles/` and the persisted active-profile pointer (`getProfileManager()` singleton). |
| `app/browser/media.py` | `verifyImage` (download safety) and `toMarkdown` (MarkItDown, no-image mode). |
| `app/browser/screenshot_manager.py` | Capture and save screenshots (viewport / full page / element / annotated). |
| `app/browser/video_recorder.py` | Start/stop session recording and finalize the MP4 (ffmpeg). |
| `app/browser/browser_manager.py` | **Facade** the API, MCP, and adapters all call. Wraps every action in the AI-friendly envelope and serializes access behind an `asyncio.Lock`. Exposes `getBrowserManager()` singleton. |
| `app/api/server.py` | FastAPI app + REST routes; exposes `app` and `runServer()`. |
| `app/api/websocket.py` | `/ws` WebSocket route speaking `{"action", "params"}`. |
| `app/agents/claude_adapter.py` | `ClaudeAdapter` — builds tools and runs a Claude conversation loop. |
| `app/agents/openai_adapter.py` | OpenAI equivalent of the Claude adapter. |
| `app/mcp/mcp_server.py` | FastMCP server; exposes `mcp` and `main()` (stdio). |
| `app/models/` | `commands.py` (request models) and `responses.py` (envelope models). |
| `app/utils/config.py` | `settings` singleton, resolved from `ABC_*` env vars at import time. |
| `app/utils/logger.py` | `getLogger(name)` — shared, namespaced, colorized logging. |
| `app/utils/helpers.py` | `successResponse` / `errorResponse` envelope builders, path/dir helpers. |

### Design decision: naming conventions

Internal Python identifiers use **camelCase** per the project style guide (variables, functions, methods). The two places that use **snake_case** do so deliberately, because they are **external interface contracts** rather than internal code:

- **MCP tool names** — e.g. `open_browser`, `take_screenshot`, `extract_links`. These are the names AI clients call.
- **JSON contract keys** — e.g. `video_path`, `success`, `action`, `timestamp`. These are the keys agents read.

So a method named `takeScreenshot` (camelCase, internal) backs the MCP tool `take_screenshot` (snake_case, external) and the action label `take_screenshot` inside the JSON envelope. The boundary is intentional: change the internal name freely, keep the external contract stable.

## Requirements / Prerequisites

- **Python 3.12+**
- **ffmpeg** — a **system** dependency (not a pip package). Required for recording / MP4 encoding. Install it and make sure it is on your `PATH`:
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `sudo apt install ffmpeg`
  - Windows: download a build from <https://www.gyan.dev/ffmpeg/builds/> (or `winget install ffmpeg`) and add it to `PATH`.
- **Everything else is pip** — see `requirements.txt`. After installing, run `playwright install chromium` once to download the browser binary.

Everything is open-source and free, and is meant to be installed **only inside a virtualenv** (no global installs). It works on macOS, Linux, and Windows.

## Installation

> Always install inside a virtualenv — never globally.

### Standard `venv` flow

```bash
# 1. Create and activate a virtualenv
python3.12 -m venv venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows (PowerShell/cmd)

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Download the Chromium browser binary (one-time)
playwright install chromium
```

### Optional: `uv` flow

[`uv`](https://github.com/astral-sh/uv) is a faster drop-in alternative:

```bash
uv venv                          # creates .venv
source .venv/bin/activate        # macOS/Linux  (Windows: .venv\Scripts\activate)
uv pip install -r requirements.txt
playwright install chromium
```

Then copy the example environment file and adjust as needed:

```bash
cp .env.example .env
```

## Usage

### Run the FastAPI server

```bash
python start.py api
```

By default it serves on `http://127.0.0.1:8000` (override with `ABC_HOST` / `ABC_PORT`).

Example `curl` calls:

```bash
# Liveness probe (does not touch the browser)
curl http://127.0.0.1:8000/health

# Navigate (auto-launches the browser if not already running)
curl -X POST http://127.0.0.1:8000/navigate \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'

# Screenshot the current page
curl -X POST http://127.0.0.1:8000/screenshot \
  -H "Content-Type: application/json" \
  -d '{}'

# Extract the visible text of the current page
curl -X POST http://127.0.0.1:8000/extract \
  -H "Content-Type: application/json" \
  -d '{"kind":"text"}'
```

Other extract kinds: `links`, `buttons`, `forms`, `images`, `dom`, `title`, `url`.

#### AI-friendly JSON response shape

Every action returns the same envelope so an agent always parses one structure:

```json
{
  "success": true,
  "action": "navigate",
  "timestamp": "2026-06-22T10:15:30.123456+00:00",
  "data": { "url": "https://example.com", "title": "Example Domain", "status": 200 }
}
```

On failure:

```json
{
  "success": false,
  "error": "navigate failed",
  "details": "TimeoutError: Navigation timeout of 30000 ms exceeded",
  "timestamp": "2026-06-22T10:15:30.123456+00:00",
  "action": "navigate"
}
```

The **screenshot** `data` payload looks like:

```json
{ "path": "app/storage/screenshots/shot-20260622-101530.png", "timestamp": "...", "url": "https://example.com", "width": 1280, "height": 800 }
```

The **recording** `data` payload looks like:

```json
{ "video_path": "app/storage/recordings/session-20260622-101530.mp4", "duration": 12.4, "resolution": "1280x800", "fps": 24 }
```

Saved artifacts are served back over the same origin under `/screenshots` and `/recordings`, so an agent that receives a `path` or `video_path` can fetch the file.

### WebSocket usage

Connect to `ws://127.0.0.1:8000/ws` and send action messages of the shape `{"action": ..., "params": {...}}`. Every reply is one of the same envelopes shown above.

```python
import asyncio
import json
import websockets


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8000/ws") as ws:
        await ws.send(json.dumps({
            "action": "navigate",
            "params": {"url": "https://example.com"},
        }))
        reply = json.loads(await ws.recv())
        print(reply["success"], reply["action"])


asyncio.run(main())
```

### MCP server

Run the MCP stdio server:

```bash
python start.py mcp
```

Register it with Claude Desktop by adding an entry to `claude_desktop_config.json`
(set `cwd` to the absolute path of this repo so `app` is importable):

```json
{
  "mcpServers": {
    "ai-browser-controller": {
      "command": "python",
      "args": ["start.py", "mcp"],
      "cwd": "/absolute/path/to/ai-browser-controller"
    }
  }
}
```

> Tip: point `command` at the Python interpreter inside your virtualenv
> (e.g. `/absolute/path/to/ai-browser-controller/venv/bin/python`) so the MCP
> client uses the environment where the dependencies are installed.

Exposed MCP tools (snake_case, the external contract):

`open_browser`, `close_browser`, `navigate`, `navigate_back`, `navigate_forward`,
`refresh`, `open_new_tab`, `switch_tab`, `close_tab`, `get_title`, `get_url`,
`extract_text`, `extract_links`, `extract_buttons`, `extract_forms`,
`extract_images`, `get_dom`, `scroll`, `hover`, `click`, `double_click`,
`right_click`, `fill`, `upload_file`, `download_file`, `press_keys`,
`wait_for_element`, `wait_for_network_idle`, `take_screenshot`,
`start_recording`, `stop_recording`, `status`.

### Claude adapter

The Claude adapter wires the browser tools into a Claude conversation loop. It needs `ANTHROPIC_API_KEY` in the environment and defaults to the `claude-opus-4-8` model.

```python
import asyncio
from app.agents.claude_adapter import ClaudeAdapter


async def main() -> None:
    adapter = ClaudeAdapter()  # reads ANTHROPIC_API_KEY; default model: claude-opus-4-8
    tools = adapter.buildTools()  # browser actions as Claude tool definitions
    result = await adapter.run(
        "Go to https://example.com, screenshot it, and tell me what the page is about.",
        tools=tools,
    )
    print(result)


asyncio.run(main())
```

The **OpenAI adapter** works analogously — import from `app.agents.openai_adapter`, build tools, and run a conversation. It reads `OPENAI_API_KEY` instead of `ANTHROPIC_API_KEY`.

## Workflow example

The core loop an agent runs is **navigate → screenshot → inspect → decide**:

1. **Navigate** to a target URL (`/navigate` or the `navigate` tool). The browser auto-launches if it is not already running.
2. **Screenshot** the page (`/screenshot` or `take_screenshot`) to see it visually, and/or **extract** structured data (`/extract` → `text`, `links`, `buttons`, `forms`, …).
3. **Inspect** the returned screenshot path and structured JSON to understand the UI/UX and what is actionable.
4. **Decide** the next action — `click`, `fill`, `scroll`, `navigate` again — and repeat. Optionally wrap the whole sequence in `start_recording` / `stop_recording` to capture an MP4 of the session and report the `video_path` back.

Because every step returns the same envelope, the agent always knows whether the step succeeded (`success`), what it did (`action`), and what it got back (`data`).

## Testing

```bash
pytest
```

Browser-dependent tests **auto-skip** if the Playwright browsers are not installed, so the suite runs cleanly even on a machine where you have not run `playwright install chromium`. Async tests run automatically (`asyncio_mode = "auto"` in `pyproject.toml`).

## Configuration

All runtime settings are resolved once at import time from `ABC_*` environment variables (see `.env.example`). They map directly onto the `settings` object in `app/utils/config.py`.

| Env var | Setting | Default | Meaning |
| --- | --- | --- | --- |
| `ABC_HOST` | `apiHost` | `127.0.0.1` | Host/interface the FastAPI server binds to. |
| `ABC_PORT` | `apiPort` | `8000` | Port the FastAPI server listens on. |
| `ABC_BROWSER` | `browserType` | `chromium` | Browser engine: `chromium` \| `firefox` \| `webkit`. |
| `ABC_BROWSER_CHANNEL` | `browserChannel` | _(bundled)_ | Real-browser channel, e.g. `chrome`, `msedge` (harder to bot-detect). |
| `ABC_HEADLESS` | `headless` | `true` | Run without a visible window. |
| `ABC_VIEWPORT_WIDTH` | `viewportWidth` | `1280` | Viewport width in pixels. |
| `ABC_VIEWPORT_HEIGHT` | `viewportHeight` | `800` | Viewport height in pixels. |
| `ABC_TIMEOUT_MS` | `defaultTimeoutMs` | `30000` | Default action/navigation timeout (ms). |
| `ABC_USER_AGENT` | `userAgent` | _(browser default)_ | Optional custom User-Agent. |
| `ABC_HUMANIZE` | `humanize` | `true` | Human-like typing/clicking/scrolling (anti bot-detection). |
| `ABC_TYPING_WPM` | `typingWpm` | `25` | Typing speed in words/min (with jitter). |
| `ABC_MAX_WAIT_MS` | `maxWaitMs` | `300000` | Ceiling for quiet/event-driven waits (5 min). |
| `ABC_MAX_DOWNLOAD_WAIT_MS` | `maxDownloadWaitMs` | `3600000` | Ceiling for downloads (1 h). |
| `ABC_NO_IMAGE_MODE` | `noImageMode` | `false` | Start in no-image mode (markdown over pixels). |
| `ABC_RECORDING_FPS` | `recordingFps` | `24` | Frames per second for recordings. |
| `ABC_FFMPEG` | `ffmpegBinary` | `ffmpeg` | Path to the ffmpeg binary (system dependency). |
| `ABC_SCREENSHOT_DIR` | `screenshotDir` | `app/storage/screenshots` | Where screenshots are saved. |
| `ABC_RECORDING_DIR` | `recordingDir` | `app/storage/recordings` | Where recordings are saved. |
| `ABC_PROFILES_DIR` | `profilesDir` | `app/storage/profiles` | Root for named browser profiles. |
| `ABC_ACTIVE_PROFILE_FILE` | `activeProfileFile` | `app/storage/active_profile.json` | Persisted active-profile pointer. |
| `ABC_LOG_LEVEL` | `logLevel` | `INFO` | Log verbosity: `DEBUG` … `CRITICAL`. |

Run `python start.py info` to print the resolved settings and storage paths.

## Future expansion hooks

The `BrowserManager` facade plus the `getBrowserManager()` singleton are the deliberate **seam** for scaling out — today it returns one process-wide manager, but a `BrowserPool` keyed by session id could resolve a manager per session without changing any caller. That single seam opens the door to:

- **Multi-browser sessions** and **browser pools** (concurrent, isolated sessions).
- **Autonomous agents** running long task loops via the adapters.
- **Visual QA**, **accessibility audits**, and **UI regression** testing built on the screenshot/extraction primitives.
- **Human-in-the-loop** approvals between agent steps.
- **Remote / distributed execution** of the browser layer behind the same API.

## License

MIT — open source. See `LICENSE` (or the SPDX identifier `MIT`) for details.
