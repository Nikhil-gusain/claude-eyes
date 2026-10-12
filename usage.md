# Usage Guide

Practical "how do I actually use this" guide. For the full feature list and
module map see [README.md](README.md); for Claude Code MCP wiring see
[MCP_SETUP.md](MCP_SETUP.md).

## 1. Setup

```bash
python3.11 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

`ffmpeg` must be on your `PATH` (`brew install ffmpeg` / `apt install ffmpeg`) —
only needed for screen recording.

### Gemini API key

Gemini is the **default** brain driving the browser (no CLI flag needed). Get
a free key at <https://aistudio.google.com/apikey>, then put it in `.env`:

```bash
GEMINI_API_KEY=your-key-here
```

Optional, for heavier use:
- `GEMINI_API_KEYS=key1,key2,key3` — rotates to the next key on a 429 (free-tier
  rate limit), so you can stack several free keys.
- `GEMINI_MODELS=gemini-2.5-flash,gemini-2.5-pro` — fallback chain if a model
  returns 503 (overloaded). Defaults already cover this.
- `GOOGLE_API_KEY` also works, same variable either name.

Claude and OpenAI are alternative drivers (`ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` in `.env`), selectable with `--provider`.

## 2. CLI usage

Give it a task in plain English:

```bash
python start.py agent "go to example.com and tell me the heading"
```

The agent (Gemini by default) opens the browser, calls tools (navigate,
screenshot, click, extract, …) in a loop, and prints every tool call plus its
final answer.

Flags:

```bash
python start.py agent --provider gemini --model gemini-2.5-pro --max-turns 20 \
  "search for the weather in Tokyo and summarize it"

python start.py agent --provider claude "..."   # needs ANTHROPIC_API_KEY
python start.py agent --provider openai "..."   # needs OPENAI_API_KEY
```

- `--provider` — `gemini` (default) | `claude` | `openai`
- `--model` — override the default model for that provider
- `--max-turns` — stop after N model turns (default 12)

Other subcommands:

```bash
python start.py info      # print resolved config + storage paths
python start.py api       # run the REST + WebSocket server (see README for curl examples)
python start.py mcp       # run the MCP stdio server (for Claude Code/Desktop)
python start.py studio    # run the web frontend (see below)
```

## 3. Web frontend (Studio)

A browser-based control panel already ships with this repo — no extra build
step.

```bash
python start.py studio
```

Open **http://127.0.0.1:8000/studio**. Type a task, hit Run, and watch the
agent's thoughts/actions/results stream in live over a WebSocket
(`/studio/ws`); you can type a correction into the second box while it's
running (e.g. "use chrome, not dalle") and it gets folded into the next model
turn. One task runs at a time (shared browser).

It's a single static file — [app/api/static/studio.html](app/api/static/studio.html)
— served by [app/api/studio.py](app/api/studio.py). To build a different
frontend, point it at the same `/studio/ws` protocol:

```
→ {"type": "start", "task": "...", "model"?: "...", "maxTurns"?: 12}
→ {"type": "feedback", "text": "..."}
← {"type": "thought" | "action" | "result" | "feedback" | "status" | "done" | "error", "text": "..."}
```

Or drive individual browser actions directly over the plain REST/WebSocket API
(`/navigate`, `/screenshot`, `/extract`, `/ws`, …) documented in the README —
useful for a custom UI that doesn't need the autonomous agent loop, just raw
browser control.

## 4. Troubleshooting

- `error: No Gemini API key set` → check `GEMINI_API_KEY` is in `.env` and
  that you're running from a shell where `.env` gets loaded (or `export` it).
- `All Gemini models are unavailable (503)` → Google's models are overloaded;
  retry later or set `GEMINI_MODELS` to a different chain.
- `All Gemini keys are rate-limited` → add more keys to `GEMINI_API_KEYS` or
  wait for the free-tier quota to reset.
- Browser never shows up → it runs **headless by default**
  (`ABC_HEADLESS=true`); set `ABC_HEADLESS=false` in `.env` to watch it.
