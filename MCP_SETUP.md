# Using the browser as a tool in any Claude Code project

This server is a **UI/UX inspector for AI**: it lets Claude (or any MCP-capable
agent) *see* and drive a real browser directly — so Claude can look at the UI it
just wrote, navigate localhost, read the DOM and network, and iterate, **without
you screenshotting or describing visuals**.

## ✅ Already installed globally (user scope)

It was registered with:

```bash
claude mcp add ai-browser --scope user --env ABC_HEADLESS=true -- \
  "/Users/nikhil/Desktop/TESTING/claude eyes/ai-browser-controller/venv/bin/python" \
  "/Users/nikhil/Desktop/TESTING/claude eyes/ai-browser-controller/start.py" mcp
```

`--scope user` means it's available in **every** project. `claude mcp list` shows
it as `ai-browser … ✓ Connected`.

> **To use it in a chat: start a NEW Claude Code session.** MCP tools load when a
> session starts, so an already-open session won't see them until reopened.

Then just talk to it, e.g.:
- *"Run my dev server, open localhost:3000, and show me a screenshot of the page."*
- *"Look at the hero section and tell me if the spacing looks off."*
- *"What network/API calls does this page make on load?"*

The tools appear as `mcp__ai-browser__navigate`, `mcp__ai-browser__screenshot`, etc.

### Manage it

```bash
claude mcp list                 # health check
claude mcp get ai-browser       # show config
claude mcp remove ai-browser --scope user   # uninstall
```

## Behaviour that keeps your disk clean

- **`screenshot`** returns the image **inline** (Claude sees it) and writes
  **nothing to disk** — use this for "let me look". Storage never grows.
- **`take_screenshot`** is the only screenshot tool that saves a file (use it
  only when you explicitly want a path).
- **Recording** happens *only* when you call `start_recording` — never automatic.
- **`clear_storage`** deletes any saved screenshots/recordings/downloads on demand.
- Nothing is captured per-action automatically; the agent decides every step.

## Headed/headless switching & persistent login

- The browser uses a **persistent on-disk profile** (`app/storage/browser_profile/`,
  git-ignored). Cookies, tokens and logins (Gmail, etc.) **survive across runs** —
  log in once and you stay logged in. `clear_profile` wipes it for a fresh start.
- `open_browser(headless=false)` opens a **real visible window**. Use it (or flip a
  running browser with `set_headless(headless=false)`) when a **human must act** —
  solving a captcha / "are you human" page or a first-time login the AI shouldn't
  do. Switch back with `set_headless(headless=true)`; the login persists.
- The global install starts headless (`ABC_HEADLESS=true`); the AI can still flip to
  headed on demand via `set_headless`.

## The 39 tools at a glance

| Goal | Tool |
|---|---|
| See the page (inline, no file) | `screenshot` |
| Read everything at once | `read_page` |
| Raw HTML | `get_dom` |
| Network / requests | `get_network`, `clear_network` |
| Navigate | `navigate`, `navigate_back`, `navigate_forward`, `refresh` |
| Interact | `click`, `double_click`, `right_click`, `fill`, `hover`, `press_keys`, `scroll` |
| Tabs | `open_new_tab`, `switch_tab`, `close_tab` |
| Files | `upload_file`, `download_file` |
| Save a screenshot to disk | `take_screenshot` |
| Record a session | `start_recording`, `stop_recording` |
| Structured lists | `extract_links`, `extract_buttons`, `extract_forms`, `extract_images`, `extract_text` |
| Headed/headless + login | `open_browser(headless=…)`, `set_headless`, `clear_profile` |
| Lifecycle / housekeeping | `close_browser`, `status`, `clear_storage` |

## Claude Desktop (optional)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ai-browser": {
      "command": "/Users/nikhil/Desktop/TESTING/claude eyes/ai-browser-controller/venv/bin/python",
      "args": ["/Users/nikhil/Desktop/TESTING/claude eyes/ai-browser-controller/start.py", "mcp"],
      "env": { "ABC_HEADLESS": "false" }
    }
  }
}
```
