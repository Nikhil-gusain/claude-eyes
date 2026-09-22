# AI Browser Controller

A browser automation layer that lets AI agents control a real browser through Playwright.

The same browser actions are exposed through **MCP**, **FastAPI/WebSocket**, and in-process AI adapters.

## What it does

An agent can:

- Open and manage browser sessions
- Navigate and switch tabs
- Click, fill, hover, scroll, and press keys
- Upload and download files
- Extract text, links, forms, buttons, images, and DOM data
- Capture screenshots
- Record browser sessions to MP4
- Receive consistent structured JSON responses

## Architecture

```
AI Agent
   ├── MCP server
   ├── FastAPI / WebSocket
   └── Claude / OpenAI adapter
             ↓
       BrowserManager
             ↓
          Playwright
             ↓
        Real Browser
```

## AI-friendly contract

Every action returns a predictable success/error envelope:

```json
{
  "success": true,
  "action": "navigate",
  "data": {
    "url": "https://example.com",
    "title": "Example Domain"
  }
}
```

This gives an agent one consistent interface regardless of the transport being used.

## Quick start

Requirements:

- Python 3.12+
- Playwright
- Chromium
- ffmpeg for recording

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python start.py api
```

Run the MCP server with:

```bash
python start.py mcp
```

Run tests with:

```bash
pytest
```

## Why I built it

AI agents are useful only when they can interact with real systems. This project provides the browser-control layer needed to turn an agent from a text interface into an automation system.

## Future direction

- Multi-session browser pools
- Human approval between sensitive actions
- Remote browser execution
- Visual QA and UI testing
- Longer autonomous task workflows

MIT licensed.
