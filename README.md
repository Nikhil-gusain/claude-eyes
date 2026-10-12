<div align="center">

# 👁️ Claude Eyes

**Give your AI agent a real browser — and let it actually *see*.**

A plug-and-play browser layer that lets Claude (or any AI agent) navigate, inspect, click, fill, screenshot, record, and read the network of a real Chrome/Chromium browser, with **persistent logins** and **one consistent JSON contract** across MCP, REST, and WebSocket.

[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/powered%20by-Playwright-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/python/)
[![MCP](https://img.shields.io/badge/MCP-compatible-8A2BE2)](https://modelcontextprotocol.io/)
[![FastAPI](https://img.shields.io/badge/FastAPI-REST%20%2B%20WebSocket-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

[Quick start](#-quick-start) · [Why not Playwright MCP?](#-why-claude-eyes-over-playwright-mcp) · [MCP tools](#-mcp-tools) · [REST API](#-rest--websocket-api) · [Configuration](#%EF%B8%8F-configuration)

</div>

---

## ✨ What it does

Claude Eyes is a **UI/UX inspector for AI**. Your agent can look at the UI it just wrote, open `localhost`, read the DOM and network traffic, and iterate, so you don't have to screenshot or describe anything yourself.

```text
You:    "Run my dev server, open localhost:3000 and tell me if the hero spacing looks off."
Claude: navigate → screenshot (inline) → read_page → get_network → click → screenshot → answer
```

## 🚀 Highlights

| | |
|---|---|
| 🔐 **Persistent logins** | Uses an on-disk browser profile, so you log in once (Gmail, GitHub, dashboards) and stay logged in across runs. `clear_profile` gives you a fresh start. |
| 🪟 **Headless ↔ headed at runtime** | `set_headless(false)` opens a real window mid-session so a human can solve a captcha or do a first login, then hand control back. State survives the switch. |
| 🟢 **Real Chrome support** | Set `ABC_CHANNEL=chrome` to drive your installed Chrome with automation flags stripped, so Google sign-in works where bundled Chromium is rejected. |
| 🖼️ **Inline vision, zero disk churn** | `screenshot` returns the image straight to the model without writing a file. `take_screenshot` is the only tool that saves one. |
| 🎬 **On-demand MP4 recording** | `start_recording` / `stop_recording` return `{video_path, duration, resolution, fps}`. Falls back to WebM when ffmpeg is missing. |
| 📖 **One-call page understanding** | `read_page` returns title, URL, visible text, links, buttons, forms, and image count in a single round trip. |
| 🌐 **Network inspection** | `get_network` lists every request across tabs (method, status, type, URL filter). `clear_network` isolates the traffic of a single action. |
| 🔌 **Three transports, one contract** | MCP (stdio), FastAPI REST, and WebSocket all return the same `success / action / data` envelope. |
| 🤖 **Built-in agent adapters** | In-process Claude and OpenAI tool-calling adapters for running your own autonomous loop. |
| 🧹 **Storage hygiene** | `clear_storage` wipes screenshots, recordings, and downloads on demand. Nothing is captured automatically. |

## 🆚 Why Claude Eyes over Playwright MCP?

[Microsoft's Playwright MCP](https://github.com/microsoft/playwright-mcp) is excellent and mature. Claude Eyes is built on the same engine but targets a different workflow: **an agent that needs to see, stay logged in, and be embedded in your own stack.**

| Capability | Claude Eyes | Playwright MCP |
|---|:---:|:---:|
| Switch headless ↔ headed **mid-session**, keeping cookies and login | ✅ `set_headless` | ⚠️ chosen at launch |
| Human-in-the-loop hand-off for captchas and first logins | ✅ built in | ⚠️ manual restart |
| Real Chrome with automation banner/flags removed (Google login) | ✅ `ABC_CHANNEL` | ⚠️ channel only |
| On-demand video recording as a **tool call**, returning MP4 metadata | ✅ | ⚠️ config/flag driven |
| One-call page summary (`read_page`: text, links, buttons, forms) | ✅ | ⚠️ accessibility snapshot |
| High-level extractors (links, buttons, forms, images) | ✅ | ❌ (snapshot-based) |
| REST + WebSocket API besides MCP | ✅ | ❌ MCP only |
| Drop-in Claude / OpenAI tool-calling adapters | ✅ | ❌ |
| Uniform JSON success/error envelope on every action | ✅ | ❌ |
| Explicit storage and profile cleanup tools | ✅ `clear_storage`, `clear_profile` | ❌ |
| Pure Python, easy to extend | ✅ | ❌ Node/TypeScript |
| Accessibility-tree snapshots with element refs | ❌ (CSS selectors) | ✅ |
| Firefox / WebKit | ✅ | ✅ |

> **Choose Playwright MCP** if you want accessibility-tree-driven automation from a Node ecosystem.
> **Choose Claude Eyes** if you want a visual, login-persistent browser your agent can share with a human, and the same browser available over REST/WebSocket for your own services.

*Comparison reflects each project's documented defaults at the time of writing. Corrections are welcome via PR.*

## ⚡ Quick start

**Prerequisites:** Python 3.12+, and optionally [ffmpeg](https://ffmpeg.org/) on your `PATH` for MP4 output.

```bash
git clone https://github.com/Nikhil-gusain/claude-eyes.git
cd claude-eyes

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium       # one-time browser download

cp .env.example .env              # optional, every value has a default
```

### Add it to Claude Code

```bash
claude mcp add ai-browser --scope user -- \
  /absolute/path/to/claude-eyes/venv/bin/python \
  /absolute/path/to/claude-eyes/start.py mcp
```

Start a **new** session and the tools appear as `mcp__ai-browser__*`. See [MCP_SETUP.md](MCP_SETUP.md) for more detail.

### Add it to Claude Desktop

```json
{
  "mcpServers": {
    "ai-browser": {
      "command": "/absolute/path/to/claude-eyes/venv/bin/python",
      "args": ["/absolute/path/to/claude-eyes/start.py", "mcp"],
      "env": { "ABC_HEADLESS": "true", "ABC_CHANNEL": "chrome" }
    }
  }
}
```

> On Windows use `venv\\Scripts\\python.exe`.

### Try it

> *"Open github.com, read the page, and screenshot the header."*
> *"What API calls does localhost:3000 make on load?"*
> *"Open Gmail headed so I can log in, then switch back to headless."*

## 🧰 MCP tools

| Group | Tools |
|---|---|
| **Lifecycle** | `open_browser`, `close_browser`, `set_headless`, `clear_profile`, `status` |
| **Navigation** | `navigate`, `navigate_back`, `navigate_forward`, `refresh` |
| **Tabs** | `open_new_tab`, `switch_tab`, `close_tab` |
| **Read** | `read_page`, `get_title`, `get_url`, `extract_text`, `extract_links`, `extract_buttons`, `extract_forms`, `extract_images`, `get_dom` |
| **Network** | `get_network`, `clear_network` |
| **Interact** | `click`, `double_click`, `right_click`, `hover`, `fill`, `scroll`, `press_keys`, `upload_file`, `download_file` |
| **Wait** | `wait_for_element`, `wait_for_network_idle` |
| **Vision** | `screenshot` (inline), `take_screenshot` (saved file) |
| **Recording** | `start_recording`, `stop_recording` |
| **Housekeeping** | `clear_storage` |

## 🌐 REST & WebSocket API

```bash
python start.py api               # http://127.0.0.1:8000  (docs at /docs)
```

```bash
curl -X POST http://127.0.0.1:8000/navigate -H "Content-Type: application/json" \
     -d '{"url":"https://example.com"}'

curl -X POST http://127.0.0.1:8000/read-page -H "Content-Type: application/json" -d '{}'
curl "http://127.0.0.1:8000/network?urlContains=/api/"
```

<details>
<summary><b>All endpoints</b></summary>

| Area | Endpoints |
|---|---|
| Health | `GET /health`, `GET /status` |
| Browser | `POST /browser/open`, `/browser/close`, `/browser/set-headless`, `/browser/clear-profile` |
| Navigation | `POST /navigate`, `/navigate/back`, `/navigate/forward`, `/refresh` |
| Tabs | `POST /tabs/new`, `/tabs/switch`, `/tabs/close` |
| Read | `POST /extract` (`text`, `links`, `buttons`, `forms`, `images`, `dom`, `title`, `url`), `POST /read-page` |
| Interact | `POST /interact/click`, `/double-click`, `/right-click`, `/hover`, `/fill`, `/scroll`, `/press-keys`, `/upload`, `/download` |
| Wait | `POST /wait/element`, `/wait/network-idle` |
| Media | `POST /screenshot`, `/recording/start`, `/recording/stop`, `/storage/clear` |
| Network | `GET /network`, `POST /network/clear` |

</details>

**WebSocket** at `ws://127.0.0.1:8000/ws` speaks the same vocabulary:

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

`app.agents.openai_adapter` works the same way with `OPENAI_API_KEY`.

## 🔐 Persistent profile & privacy

- The profile (cookies, tokens, localStorage) lives in `app/storage/browser_profile/` by default. Override it with `ABC_USER_DATA_DIR`.
- **Everything under `app/storage/` is git-ignored.** Only empty `.gitkeep` placeholders are committed, so your logged-in sessions, screenshots, recordings, and downloads never reach the repo.
- `clear_profile` logs out of everything. `clear_storage` deletes saved media.
- Treat the profile folder like a password vault: anyone who has it is logged in as you.

## ⚙️ Configuration

All settings come from `ABC_*` environment variables (see [.env.example](.env.example)). Run `python start.py info` to print the resolved values.

| Variable | Default | Description |
|---|---|---|
| `ABC_BROWSER` | `chromium` | `chromium` \| `firefox` \| `webkit` |
| `ABC_CHANNEL` | — | Installed browser channel, e.g. `chrome` or `msedge` |
| `ABC_HEADLESS` | `true` | Start without a visible window |
| `ABC_USER_DATA_DIR` | `app/storage/browser_profile` | Persistent profile directory |
| `ABC_VIEWPORT_WIDTH` / `_HEIGHT` | `1280` / `800` | Viewport size in pixels |
| `ABC_TIMEOUT_MS` | `30000` | Default action/navigation timeout |
| `ABC_USER_AGENT` | — | Custom User-Agent |
| `ABC_RECORDING_FPS` | `24` | Recording frame rate |
| `ABC_FFMPEG` | `ffmpeg` | Path to the ffmpeg binary |
| `ABC_SCREENSHOT_DIR` / `ABC_RECORDING_DIR` | `app/storage/…` | Media output folders |
| `ABC_HOST` / `ABC_PORT` | `127.0.0.1` / `8000` | API bind address |
| `ABC_LOG_LEVEL` | `INFO` | `DEBUG` … `CRITICAL` |

## 🏗️ Architecture

```text
            ┌──────────────┐  ┌────────────────┐  ┌──────────────────────┐
            │  MCP (stdio) │  │ FastAPI + /ws  │  │ Claude/OpenAI adapters│
            └──────┬───────┘  └───────┬────────┘  └──────────┬───────────┘
                   └──────────────────┼──────────────────────┘
                                      ▼
                      BrowserManager (facade, asyncio.Lock,
                       uniform JSON envelope, singleton)
                                      ▼
           PlaywrightController · ScreenshotManager · VideoRecorder
                                      ▼
                  Persistent Chrome / Chromium / Firefox / WebKit
```

```text
claude-eyes/
├── app/
│   ├── agents/    # Claude & OpenAI tool-calling adapters
│   ├── api/       # FastAPI server + WebSocket route
│   ├── browser/   # Playwright control, screenshots, recording, BrowserManager facade
│   ├── mcp/       # MCP stdio server (FastMCP)
│   ├── models/    # Pydantic request / response models
│   ├── storage/   # Runtime data: profile, screenshots, recordings, downloads (git-ignored)
│   └── utils/     # Settings, logging, envelope helpers
├── tests/         # pytest suite (browser tests auto-skip without Playwright browsers)
├── start.py       # CLI: api | mcp | info
└── MCP_SETUP.md
```

Internal Python uses **camelCase**. MCP tool names and JSON keys use **snake_case** because they are the external contract.

## 🧪 Testing

```bash
pytest
```

Browser-dependent tests skip automatically when Playwright browsers are not installed.

## 💡 Why I built it

AI agents are only useful when they can interact with real systems. Claude Eyes is the browser-control layer that turns an agent from a text interface into something that can see and operate the web.

## 🗺️ Roadmap

- [ ] Browser pool: multiple isolated sessions keyed by session id
- [ ] Human approval between sensitive actions
- [ ] Accessibility audits and visual regression on top of the screenshot primitives
- [ ] Console log capture
- [ ] Remote / distributed browser execution

## 🤝 Contributing

Issues and PRs are welcome. Please run `pytest` before opening a PR, and never commit anything from `app/storage/`.

## 📄 License

[MIT](LICENSE) © Nikhil Gusain
