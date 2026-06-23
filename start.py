#!/usr/bin/env python3
"""CLI entrypoint for the AI Browser Controller.

Usage::

    python start.py api      # run the FastAPI HTTP + WebSocket server
    python start.py mcp      # run the MCP stdio server (for Claude Desktop, etc.)
    python start.py info     # print resolved settings and storage paths
    python start.py          # no subcommand -> print help

Internal identifiers use camelCase per the project style guide; the mandated
filename ``start.py`` and the argparse subcommand strings stay as-is because
those are an external interface.
"""

from __future__ import annotations

import argparse
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
    subparsers = parser.add_subparsers(dest="command", metavar="{api,mcp,info}")

    apiParser = subparsers.add_parser("api", help="Run the FastAPI HTTP + WebSocket server.")
    apiParser.set_defaults(handler=runApi)

    mcpParser = subparsers.add_parser("mcp", help="Run the MCP stdio server.")
    mcpParser.set_defaults(handler=runMcp)

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
