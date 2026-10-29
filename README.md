<div align="center">

# 👁️ Claude Eyes

**Give your AI agent a real browser — and let it actually *see*.**

A plug-and-play browser layer that lets Claude, Gemini, or GPT navigate, inspect, click, type, screenshot, record, and learn real websites. It acts like a person (human-paced typing, curved mouse movement), keeps you **logged in** across runs, drives **many tabs in parallel**, can **draw tutorial overlays into its own screen recordings**, and returns **one consistent JSON contract** over MCP, REST, and WebSocket.

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/powered%20by-Playwright-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/python/)
[![MCP](https://img.shields.io/badge/MCP-91%20tools-8A2BE2)](https://modelcontextprotocol.io/)
[![FastAPI](https://img.shields.io/badge/FastAPI-REST%20%2B%20WebSocket-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-108%20passing-brightgreen)](tests/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-ff69b4.svg)](#-contributing)

[Quick start](#-quick-start) · [Why not Playwright MCP?](#-why-claude-eyes-over-playwright-mcp) · [Features](#-features) · [Tutorial overlays](#-tutorial-overlays--live-jscss-injection) · [MCP tools](#-mcp-tools) · [REST API](#-rest--websocket-api) · [Configuration](#%EF%B8%8F-configuration) · [Troubleshooting](#-troubleshooting)

</div>

---

## ✨ What it does

Claude Eyes is a **UI/UX inspector and web operator for AI**. Your agent can look at the UI it just wrote, open `localhost`, read the DOM and network traffic, log into real sites, and iterate, so you don't have to screenshot or describe anything yourself.

```text
You:    "Run my dev server, open localhost:3000 and tell me if the hero spacing looks off."
Claude: navigate → screenshot (inline) → read_page → audit_page → click → compare_screenshots → answer
```

## 🚀 Highlights

| | |
|---|---|
| 🧑 **Behaves like a human** | Types one key at a time at **~85 WPM** (a fast, skilled typist) with natural rhythm and thinking pauses. The mouse travels a curved, wobbling path to a random point inside the target. Scrolling happens in small, uneven steps. |
| 🥷 **Stealth fingerprint** | Removes `navigator.webdriver`, the automation banner and launch flags, and adds the browser properties real Chrome has. Pair it with your installed Chrome (`ABC_BROWSER_CHANNEL=chrome`). |
| 🔐 **Persistent logins & profiles** | Named Chrome profiles, each with its own cookies. The chosen one is remembered and reopens automatically. `login_session` opens a visible window so you can log in once. |
| 🗂️ **Parallel tabs** | Stable named tab handles with per-tab locks, so sub-agents can each drive their own tab at the same time without interfering. |
| 🪟 **Headless ↔ headed at runtime** | `set_headless(false)` opens a real window mid-session so a human can solve a captcha, then hands control back. |
| 🧠 **Learns websites** | The Website Skill System records how each site works (routes, forms, flows) and reuses it on later visits. Learned knowledge is versioned and scored by confidence. |
| ✅ **Self-correcting** | `verify_goal`, `find_element` / `click_by_description` ("the blue login button"), and `plan_actions`, using whichever model is driving. |
| 🔍 **Visual QA** | Inline screenshots, `compare_screenshots` visual diff, `audit_page` (overflow, broken images, contrast), accessibility tree, and OCR. |
| ⏳ **Smart waiting** | `wait_for_stable` waits until streamed text (such as another AI's answer) stops changing. `wait_for_response` waits for a matching network response. |
| 🎬 **Record & replay** | MP4 recording, replayable JSON action logs, named workflows, and browser-state snapshots. |
| 🎨 **Tutorial overlays** | `run_js`, `add_css`, and `add_init_js` let the agent draw captions, arrows, spotlights, zoom, and click markers on the page. They appear in the recorded video, and init scripts survive the recording relaunch. |
| 🔌 **Three transports, one contract** | MCP (stdio), FastAPI REST + SSE, and WebSocket all return the same `success / action / data` envelope. |
| 🤖 **Bring your own brain** | `python start.py agent "<task>"` runs an autonomous loop with **Gemini** (default), Claude, or OpenAI. The Studio web UI lets you watch the agent and steer it while it works. |

## 🆚 Why Claude Eyes over Playwright MCP?

[Microsoft's Playwright MCP](https://github.com/microsoft/playwright-mcp) is excellent and mature. Claude Eyes is built on the same engine but targets a different workflow: **an agent that needs to look human, stay logged in, learn sites, and be embedded in your own stack.**

| Capability | Claude Eyes | Playwright MCP |
|---|:---:|:---:|
| Human-paced typing, curved mouse paths, incremental scrolling | ✅ on by default | ❌ instant input |
| Stealth fingerprint patches (`navigator.webdriver`, automation flags) | ✅ | ❌ |
| Switch headless ↔ headed **mid-session**, keeping the login | ✅ `set_headless` | ⚠️ chosen at launch |
| Named, remembered login profiles + human login hand-off | ✅ | ⚠️ single profile dir |
| Parallel tab control for sub-agents (per-tab locks) | ✅ | ⚠️ one active tab |
| Persistent per-site knowledge (Website Skill System) | ✅ | ❌ |
| Goal verification and element finding by description | ✅ | ❌ |
| Visual diff, UI audit, OCR | ✅ | ❌ |
| Waiting for streamed text to finish (`wait_for_stable`) | ✅ | ❌ |
| On-demand MP4 recording + replayable action logs and workflows | ✅ | ⚠️ trace/video via config |
| REST + SSE + WebSocket API besides MCP | ✅ | ❌ MCP only |
| Built-in autonomous agent (Gemini / Claude / OpenAI) + web UI | ✅ | ❌ |
| Uniform JSON success/error envelope on every action | ✅ | ❌ |
| Pure Python, easy to extend | ✅ | ❌ Node/TypeScript |
| Accessibility tree | ✅ `get_accessibility_tree` | ✅ snapshot with element refs |
| Element targeting | CSS selectors or plain-language description | accessibility refs |

> **Choose Playwright MCP** if you want lean, accessibility-tree-driven automation in a Node ecosystem.
> **Choose Claude Eyes** if you want a human-looking, login-persistent browser that learns sites, verifies its own work, and is available over REST/WebSocket for your own services.

*Comparison reflects each project's documented defaults at the time of writing. Corrections are welcome via PR.*

## ⚡ Quick start

**Prerequisites:** Python 3.11+, and optionally [ffmpeg](https://ffmpeg.org/) (MP4 recording) and [Tesseract](https://github.com/tesseract-ocr/tesseract) (OCR).

```bash
git clone https://github.com/Nikhil-gusain/claude-eyes.git
cd claude-eyes

python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium       # one-time browser download

cp .env.example .env              # optional, every value has a default
```

On macOS/Linux, `./setup.sh` creates the venv, installs dependencies, and downloads Chromium in one step.

### Add it to Claude Code

The repo ships a project-level [`.mcp.json`](.mcp.json) (macOS/Linux paths), so opening the folder in Claude Code picks it up automatically. To use it from **every** project:

```bash
claude mcp add ai-browser --scope user -- \
  /absolute/path/to/claude-eyes/.venv/bin/python \
  /absolute/path/to/claude-eyes/start.py mcp
```

Start a **new** session and the tools appear as `mcp__ai-browser__*`. See [MCP_SETUP.md](MCP_SETUP.md) for more detail.

### Add it to Claude Desktop

```json
{
  "mcpServers": {
    "ai-browser": {
      "command": "/absolute/path/to/claude-eyes/.venv/bin/python",
      "args": ["/absolute/path/to/claude-eyes/start.py", "mcp"],
      "env": { "ABC_BROWSER_CHANNEL": "chrome", "ABC_HUMANIZE": "true" }
    }
  }
}
```

> On Windows use `.venv\\Scripts\\python.exe`.

### Or let a built-in agent drive

```bash
python start.py agent "go to example.com and tell me the heading"     # Gemini by default
python start.py agent --provider claude "summarize the top HN story"   # or openai
python start.py studio                                                  # web UI at http://127.0.0.1:8000/studio
```

See [usage.md](usage.md) for agent flags, API keys, and the Studio protocol.

## 🧩 Features

<details open>
<summary><b>🧑 Human-like behaviour & stealth</b></summary>

- **Typing** — one key at a time at an effective **~85 WPM**, with pauses included: faster than a good typist (60–75), well below competitive typists (120+). Each keystroke gap varies randomly, spaces and punctuation get a natural pause, there are occasional "thinking" pauses, and overall speed drifts ±10% from field to field. Tune it with `ABC_TYPING_WPM`.
- **Mouse** — moves along a curved path that speeds up and slows down, with slight wobble, to a random point inside the target rather than its exact centre. It remembers where the cursor was between actions.
- **Scrolling** — moves in small, uneven steps and scrolls a screen at a time to find an element, rather than jumping straight to it.
- **Stealth** — removes `--enable-automation` and the "controlled by automated software" banner, and patches `navigator.webdriver`, `window.chrome`, plugins, and languages.
- Toggle globally (`ABC_HUMANIZE`, `ABC_STEALTH`), at runtime (`set_humanize`, `set_stealth`), or per call (`humanize=false` for instant input).

> Hardened identity providers such as Google sign-in may still block automated browsers. For those sites, log in once by hand with `login_session` and reuse the saved profile.

</details>

<details>
<summary><b>🔐 Profiles, sessions & tabs</b></summary>

- **Multiple Chrome profiles** — `list_profiles`, `select_profile`, `create_profile`. The active profile is remembered on disk and reopens in later chats.
- **`login_session`** — opens a visible window so a human can log in or sign up, then saves the session for future automated runs.
- **Parallel tabs** — every tab-aware tool takes an optional `tab` handle. Per-tab locks let different tabs run truly concurrently, and network capture is scoped per tab. `open_new_tab(makeActive=false)` gives a sub-agent its own tab without stealing focus.
- **Session pool** — `create_session` / `switch_session` run several fully isolated browsers.
- **Snapshots** — `create_snapshot` / `restore_snapshot` save cookies, localStorage, sessionStorage, and open tabs to JSON.

</details>

<details>
<summary><b>👀 Seeing & understanding pages</b></summary>

- `screenshot` returns the image **inline** with nothing written to disk. `take_screenshot` saves a file.
- `read_page` returns title, URL, text, links, buttons, forms, and image count in one call.
- `compare_screenshots` returns the percentage of pixels changed, plus the changed regions.
- `audit_page` finds horizontal overflow, hidden interactive elements, broken images, and low contrast.
- `get_accessibility_tree`, `get_tabs`, `get_network` (filterable), and `extract_*` helpers.
- **No-image mode** — `set_no_image_mode` + `to_markdown` (MarkItDown) read images, PDFs, and Office files as text.
- **OCR** — `extract_text_from_screenshot` / `read_image` (Tesseract, optional).

</details>

<details>
<summary><b>🧠 Intelligence & memory</b></summary>

- **AI judgment** — `verify_goal` (`{success, confidence, reason}`), `find_element`, `click_by_description`, `plan_actions`. These use the model that is driving (set it explicitly with `ABC_AI_PROVIDER`).
- **Website Skill System** — `discover_page` / `discover_website` learn a site in safe stages: static content first, then navigation, interactive elements, and inferred workflows. Discovery never clicks delete, pay, or logout. Knowledge is merged and versioned, and each route's confidence rises on success and falls on failure. Modes: `OFF` / `READ_ONLY` / `LEARN`.
- **Browser memory** — `remember_page` / `search_memory` recall pages already seen instead of re-scraping them.

</details>

<details>
<summary><b>⏳ Waiting, downloads & recording</b></summary>

- `wait_for_stable` / `wait_for_response` wait for events instead of polling, capped by `ABC_MAX_WAIT_MS`. An SSE `/events` stream pushes progress to HTTP clients.
- **Safe downloads** — `download_file` keeps verified real images by default. It checks the file's actual bytes, and executables disguised as images are deleted.
- **Recording** — `start_recording` / `stop_recording` return `{video_path, duration, resolution, fps}`, as MP4 with ffmpeg or WebM without it.
- **Replay** — `start_session` → `save_session` → `replay_session`, and `save_workflow` / `run_workflow` to replay by name with no AI involved.

</details>

## 🎨 Tutorial overlays & live JS/CSS injection

Playwright's video has **no mouse cursor**, so a raw recording doesn't show where the agent clicked. Claude Eyes lets the agent draw on the page while it records, so it can produce a narrated walkthrough with captions, arrows, spotlights, zoom, and toasts.

| Tool | Scope | What it does |
|---|---|---|
| `run_js(script, arg?, tab?)` | Current page | Runs a function (`"(a) => …"`) or a bare expression (`"1+1"`) and returns `{ok, result}`. A JS exception returns `{ok: false, error}` instead of failing the call. Results over 20k characters are truncated. |
| `add_css(css, tab?)` | Current page | Injects a `<style>` tag. It's gone after the page navigates. |
| `add_init_js(script, name?)` | Every future page load, in every tab | Runs before the page's own scripts. Re-applied whenever the browser context relaunches, including on `start_recording` / `stop_recording`. Reusing a `name` replaces the earlier script. |
| `clear_init_js()` | — | Forgets every `add_init_js` script, from the next context launch on. |

**Example: show a click in a tutorial video**

```text
start_recording(sessionName="signup-tutorial")
add_init_js(name="caption", script="addEventListener('DOMContentLoaded', () => {
    const c = Object.assign(document.createElement('div'), { id: 'tut-caption' });
    c.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);'
        + 'padding:10px 18px;background:#111c;color:#fff;font:600 18px system-ui;'
        + 'border-radius:10px;z-index:2147483647';
    document.body.append(c);
})")
navigate("https://example.com/signup")
run_js("(t) => document.getElementById('tut-caption').textContent = t", arg="Step 1: open the sign-up form")
run_js("(sel) => { const r = document.querySelector(sel).getBoundingClientRect();
    const d = document.createElement('div');
    d.style.cssText = `position:fixed;left:${r.x + r.width/2 - 20}px;top:${r.y + r.height/2 - 20}px;`
        + 'width:40px;height:40px;border:3px solid #f43;border-radius:50%;z-index:2147483647';
    document.body.append(d); setTimeout(() => d.remove(), 1200); }", arg="#submit")
click("#submit")
stop_recording()
```

> **Tip:** to put an overlay on every page from the start, point `ABC_INIT_SCRIPT` at a JS file. It's injected before any `add_init_js` scripts.
>
> **Note:** Playwright can't remove an init script from a running context. When you replace a named script or call `clear_init_js`, the old version keeps running until the next relaunch, so write overlay scripts to be safe to run twice (for example, check for an existing element `id` first).

## 🧰 MCP tools

91 tools, all snake_case:

| Group | Tools |
|---|---|
| **Lifecycle & profiles** | `open_browser`, `close_browser`, `set_headless`, `set_humanize`, `set_stealth`, `clear_profile`, `list_profiles`, `select_profile`, `create_profile`, `login_session`, `status` |
| **Navigation & tabs** | `navigate`, `navigate_back`, `navigate_forward`, `refresh`, `open_new_tab`, `switch_tab`, `close_tab`, `get_tabs` |
| **Read** | `read_page`, `get_title`, `get_url`, `extract_text`, `extract_links`, `extract_buttons`, `extract_forms`, `extract_images`, `get_dom`, `get_accessibility_tree` |
| **Network** | `get_network`, `clear_network` |
| **Interact** | `click`, `double_click`, `right_click`, `hover`, `fill`, `scroll`, `press_keys`, `upload_file`, `download_file` |
| **Wait** | `wait_for_element`, `wait_for_network_idle`, `wait_for_stable`, `wait_for_response` |
| **Vision & QA** | `screenshot`, `take_screenshot`, `compare_screenshots`, `audit_page`, `to_markdown`, `set_no_image_mode`, `extract_text_from_screenshot`, `read_image` |
| **AI judgment** | `verify_goal`, `find_element`, `click_by_description`, `plan_actions` |
| **Website skills** | `discover_page`, `discover_website`, `update_skill`, `list_skills`, `search_skills`, `export_skills`, `import_skills`, `clear_skills`, `set_discovery_mode`, `get_discovery_status` |
| **Memory** | `remember_page`, `search_memory`, `list_memory`, `clear_memory` |
| **State & replay** | `create_snapshot`, `restore_snapshot`, `start_session`, `stop_session`, `save_session`, `load_session`, `replay_session`, `save_workflow`, `run_workflow`, `list_workflows` |
| **Recording & storage** | `start_recording`, `stop_recording`, `clear_storage` |
| **Page scripting & overlays** | `run_js`, `add_css`, `add_init_js`, `clear_init_js` |
| **Session pool** | `create_session`, `list_sessions`, `switch_session`, `close_session` |

## 🌐 REST & WebSocket API

```bash
python start.py api               # http://127.0.0.1:8000  (interactive docs at /docs)
```

```bash
curl -X POST http://127.0.0.1:8000/navigate -H "Content-Type: application/json" \
     -d '{"url":"https://example.com"}'

curl -X POST http://127.0.0.1:8000/read-page -H "Content-Type: application/json" -d '{}'
curl "http://127.0.0.1:8000/network?urlContains=/api/"

curl -X POST http://127.0.0.1:8000/inject/js -H "Content-Type: application/json" \
     -d '{"script":"document.title"}'      # also /inject/css, /inject/init-js, /inject/init-js/clear
```

The API exposes the same capabilities as the MCP tools (85 endpoints); browse them all at `/docs`. **WebSocket** at `ws://127.0.0.1:8000/ws` speaks the same vocabulary:

```python
await ws.send(json.dumps({"action": "navigate", "params": {"url": "https://example.com"}}))
reply = json.loads(await ws.recv())
```

### Response contract

Every action, on every transport, returns the same envelope:

```json
{ "success": true,  "action": "navigate", "timestamp": "2026-06-22T10:15:30Z",
  "data": { "url": "https://example.com", "title": "Example Domain", "status": 200 } }

{ "success": false, "action": "navigate", "timestamp": "2026-06-22T10:15:30Z",
  "error": "navigate failed", "details": "TimeoutError: Navigation timeout of 30000 ms exceeded" }
```

## 🤖 Agent adapters

```python
from app.agents.claude_adapter import ClaudeAdapter

adapter = ClaudeAdapter()                      # reads ANTHROPIC_API_KEY
result = await adapter.run(
    "Go to https://example.com, screenshot it, and summarize the page.",
    tools=adapter.buildTools(),
)
```

`gemini_adapter` (`GEMINI_API_KEY`, with key rotation via `GEMINI_API_KEYS`) and `openai_adapter` (`OPENAI_API_KEY`) share the same tool registry, so the browser behaves identically whichever model drives it.

## 🔐 Privacy

- Profiles (cookies, tokens, localStorage) live under `app/storage/` (`browser_profile/`, `profiles/`, or wherever `ABC_USER_DATA_DIR` points).
- **Everything under `app/storage/` is git-ignored.** Only empty `.gitkeep` placeholders are committed, so logged-in sessions, screenshots, recordings, snapshots, memory, and learned skills never reach the repo.
- `clear_profile` logs out of everything. `clear_storage` deletes saved media.
- Treat the profile folder like a password vault: anyone who has it is logged in as you.

## ⚙️ Configuration

All settings come from `ABC_*` environment variables; see [.env.example](.env.example) for the full annotated list. Run `python start.py info` to print the resolved values.

| Variable | Default | Description |
|---|---|---|
| `ABC_BROWSER` | `chromium` | `chromium` \| `firefox` \| `webkit` |
| `ABC_BROWSER_CHANNEL` | — | Installed browser, e.g. `chrome` or `msedge` (alias: `ABC_CHANNEL`) |
| `ABC_HEADLESS` | `true` | Start without a visible window |
| `ABC_HUMANIZE` | `true` | Human-like typing, mouse movement, and scrolling |
| `ABC_TYPING_WPM` | `85` | Effective typing speed, pauses included |
| `ABC_STEALTH` | `true` | Reduce the automation fingerprint |
| `ABC_USER_DATA_DIR` | `app/storage/browser_profile` | Default persistent profile directory |
| `ABC_PROFILES_DIR` | `app/storage/profiles` | Root for named profiles |
| `ABC_VIEWPORT_WIDTH` / `_HEIGHT` | `1280` / `800` | Viewport size in pixels |
| `ABC_TIMEOUT_MS` | `30000` | Default action/navigation timeout |
| `ABC_MAX_WAIT_MS` | `300000` | Ceiling for `wait_for_stable` / `wait_for_response` |
| `ABC_NO_IMAGE_MODE` | `false` | Prefer markdown over pixel screenshots |
| `ABC_DISCOVERY_MODE` | `LEARN` | Website skills: `OFF` \| `READ_ONLY` \| `LEARN` |
| `ABC_AI_PROVIDER` / `ABC_AI_MODEL` | `claude` / `claude-opus-4-8` | Model for `verify_goal`, `find_element`, `plan_actions` |
| `ABC_RECORDING_FPS` / `ABC_FFMPEG` | `24` / `ffmpeg` | Recording settings |
| `ABC_INIT_SCRIPT` | — | Path to a JS file injected into every page (e.g. an overlay for tutorial videos) |
| `ABC_HOST` / `ABC_PORT` | `127.0.0.1` / `8000` | API bind address |
| `ABC_LOG_LEVEL` | `INFO` | `DEBUG` … `CRITICAL` |

## 🏗️ Architecture

```text
   ┌──────────────┐  ┌──────────────────────┐  ┌──────────────────────────────┐
   │  MCP (stdio) │  │ FastAPI · /ws · SSE  │  │ Gemini / Claude / OpenAI     │
   │  91 tools    │  │ Studio web UI        │  │ adapters (start.py agent)    │
   └──────┬───────┘  └──────────┬───────────┘  └──────────────┬───────────────┘
          └─────────────────────┼─────────────────────────────┘
                                ▼
          SessionPool → BrowserManager (facade · per-tab locks · JSON envelope)
                                ▼
   PlaywrightController ─ humanize · stealth · profiles · tab registry · network log
                                ▼
   screenshots · visual diff · OCR · recording · replay · memory · website skills
                                ▼
                 Persistent Chrome / Chromium / Firefox / WebKit
```

```text
claude-eyes/
├── app/
│   ├── agents/    # Gemini, Claude & OpenAI tool-calling adapters
│   ├── api/       # FastAPI server, WebSocket, Studio UI
│   ├── browser/   # Controller, humanize, stealth, profiles, tabs, skills, diff, OCR, replay…
│   ├── mcp/       # MCP stdio server (FastMCP)
│   ├── models/    # Pydantic request / response models
│   ├── storage/   # Runtime data (git-ignored, folders kept via .gitkeep)
│   └── utils/     # Settings, logging, envelope helpers
├── tests/         # pytest suite (browser tests auto-skip without Chromium)
├── start.py       # CLI: api | mcp | agent | studio | info
├── MCP_SETUP.md   # Claude Code wiring
└── usage.md       # Agent CLI, API keys, Studio
```

Internal Python uses **camelCase**. MCP tool names and JSON keys use **snake_case** because they are the external contract.

## 🧪 Testing

```bash
pytest
```

The suite currently has 108 passing tests. Tests that need a browser skip automatically when Chromium is unavailable, and OCR tests skip without Tesseract.

## 🩺 Troubleshooting

| Symptom | Fix |
|---|---|
| Claude Code shows `ai-browser … Connection closed` on **Windows** | The bundled [`.mcp.json`](.mcp.json) uses the macOS/Linux path `.venv/bin/python`. Register a local override instead: `claude mcp add ai-browser --scope local -- .venv\Scripts\python.exe start.py mcp`. Then reconnect with `/mcp`. |
| New tools don't show up | MCP tool lists load when the server starts. Reconnect with `/mcp` or start a new session. |
| `Executable doesn't exist` / browser won't launch | Run `playwright install chromium`, or set `ABC_BROWSER_CHANNEL=chrome` to use your installed Chrome. |
| Recording comes back as `.webm`, not `.mp4` | ffmpeg isn't on `PATH`. Install it or set `ABC_FFMPEG` to its full path. |
| A site still detects automation | Use `ABC_BROWSER_CHANNEL=chrome`, keep `ABC_HUMANIZE=true`, and log in once by hand with `login_session`. |

## 💡 Why I built it

AI agents are only useful when they can interact with real systems. Claude Eyes is the browser-control layer that turns an agent from a text interface into something that can see, learn, and operate the web.

## 🗺️ Roadmap

- [ ] True concurrent execution across pooled sessions (not just tabs)
- [ ] Human approval between sensitive actions
- [x] Live JS/CSS injection and tutorial overlays for recordings
- [ ] Console log capture
- [ ] Remote / distributed browser execution

## 🤝 Contributing

Issues and PRs are welcome. Please run `pytest` before opening a PR, and never commit anything from `app/storage/`.

## 📄 License

[MIT](LICENSE) © Nikhil Gusain
