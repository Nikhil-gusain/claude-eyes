#!/usr/bin/env python3
"""CLI entrypoint for the AI Browser Controller.

Usage::

    python start.py api      # run the FastAPI HTTP + WebSocket server
    python start.py mcp      # run the MCP stdio server (for Claude Desktop, etc.)
    python start.py agent    # let an AI (Gemini by default) drive the browser
    python start.py info     # print resolved settings and storage paths
    python start.py          # no subcommand -> print help

Internal identifiers use camelCase per the project style guide; the mandated
filename ``start.py`` and the argparse subcommand strings stay as-is because
those are an external interface.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.utils.config import settings
from app.utils.logger import getLogger

logger = getLogger("start")


def runApi(_args: argparse.Namespace) -> int:
    """Run the FastAPI server using the configured host/port."""
    logger.info("Launching FastAPI server on http://%s:%d", settings.apiHost, settings.apiPort)
    try:
        # Preferred path: delegate to the server module's own runner.
        from app.api.server import runServer

        runServer()
    except ImportError:
        # Fallback if runServer is unavailable: drive uvicorn directly.
        import uvicorn

        uvicorn.run(
            "app.api.server:app",
            host=settings.apiHost,
            port=settings.apiPort,
            reload=False,
        )
    return 0


def runStudio(_args: argparse.Namespace) -> int:
    """Run the web server and point the user at the Studio dashboard."""
    logger.info(
        "Browser Agent Studio: open http://%s:%d/studio",
        settings.apiHost,
        settings.apiPort,
    )
    return runApi(_args)


def runMcp(_args: argparse.Namespace) -> int:
    """Run the MCP stdio server."""
    logger.info(
        "Launching MCP stdio server (browser=%s, headless=%s)",
        settings.browserType,
        settings.headless,
    )
    from app.mcp.mcp_server import main as mcpMain

    mcpMain()
    return 0


def _buildAdapter(provider: str, model: str | None):
    """Construct the requested in-process AI adapter.

    Each adapter shares the same browser tool registry and dispatcher; only the
    backing LLM differs. The SDKs are imported lazily inside the adapters, so an
    unselected provider never needs its package installed.
    """
    if provider == "gemini":
        from app.agents.gemini_adapter import GeminiAdapter

        return GeminiAdapter(model) if model else GeminiAdapter()
    if provider == "claude":
        from app.agents.claude_adapter import ClaudeAdapter

        return ClaudeAdapter(model) if model else ClaudeAdapter()
    if provider == "openai":
        from app.agents.openai_adapter import OpenAIAdapter

        return OpenAIAdapter(model) if model else OpenAIAdapter()
    raise ValueError(f"Unknown provider '{provider}'. Use gemini, claude, or openai.")


def runAgent(args: argparse.Namespace) -> int:
    """Drive the browser autonomously with an AI agent for a single task.

    Builds the chosen adapter (Gemini by default), runs its agentic tool-use loop
    over the natural-language task, prints the final answer plus a one-line
    summary of every tool the model called, then closes the browser.
    """
    task = " ".join(args.task).strip()
    if not task:
        print("error: provide a task, e.g. python start.py agent \"go to example.com and read the heading\"")
        return 2

    async def drive() -> dict:
        adapter = _buildAdapter(args.provider, args.model)
        try:
            return await adapter.runConversation(task, maxTurns=args.maxTurns)
        finally:
            # Leave no orphaned browser behind after a one-shot CLI run.
            try:
                from app.browser.browser_manager import getBrowserManager

                await getBrowserManager().closeBrowser()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    logger.info("Running %s agent (model=%s) on task: %s", args.provider, args.model or "default", task)
    try:
        result = asyncio.run(drive())
    except RuntimeError as exc:
        # Missing SDK / API key surface as clear, actionable RuntimeErrors.
        print(f"error: {exc}")
        return 1

    print("\n=== Tool calls ===")
    for i, call in enumerate(result.get("toolCalls", []), 1):
        ok = call.get("result", {}).get("success")
        print(f"  {i:>2}. {call['name']}  ->  success={ok}")
    print(f"\n=== Final answer ({result.get('turns')} turns) ===")
    print(result.get("finalText") or "(the model returned no text)")
    return 0


def printInfo(_args: argparse.Namespace) -> int:
    """Print the resolved settings and storage paths."""
    resolved = settings.asDict()

    print("AI Browser Controller — resolved settings")
    print("=" * 44)
    for key, value in resolved.items():
        print(f"  {key:<18}: {value}")

    print()
    print("Storage paths")
    print("-" * 44)
    print(f"  screenshotDir     : {settings.screenshotDir}")
    print(f"  recordingDir      : {settings.recordingDir}")
    return 0


def buildParser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="start.py",
        description="AI Browser Controller — let any AI agent drive a real browser.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="{api,studio,mcp,agent,info}")

    apiParser = subparsers.add_parser("api", help="Run the FastAPI HTTP + WebSocket server.")
    apiParser.set_defaults(handler=runApi)

    studioParser = subparsers.add_parser(
        "studio",
        help="Run the web server with the Studio dashboard (live thinking window + steering).",
    )
    studioParser.set_defaults(handler=runStudio)

    mcpParser = subparsers.add_parser("mcp", help="Run the MCP stdio server.")
    mcpParser.set_defaults(handler=runMcp)

    agentParser = subparsers.add_parser(
        "agent",
        help="Let an AI agent drive the browser to complete a natural-language task.",
    )
    agentParser.add_argument(
        "task",
        nargs="+",
        help="The task to perform, e.g. \"go to example.com and tell me the heading\".",
    )
    agentParser.add_argument(
        "--provider",
        choices=["gemini", "claude", "openai"],
        default="gemini",
        help="Which LLM drives the browser (default: gemini).",
    )
    agentParser.add_argument(
        "--model",
        default=None,
        help="Override the model id (default: the provider's own default).",
    )
    agentParser.add_argument(
        "--max-turns",
        dest="maxTurns",
        type=int,
        default=12,
        help="Maximum model turns before stopping (default: 12).",
    )
    agentParser.set_defaults(handler=runAgent)

    infoParser = subparsers.add_parser("info", help="Print resolved settings and storage paths.")
    infoParser.set_defaults(handler=printInfo)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected subcommand."""
    parser = buildParser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is None:
        # No subcommand provided -> show help.
        parser.print_help()
        return 0

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
