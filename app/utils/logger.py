"""Structured, colourised logging used across the project.

A single helper :func:`getLogger` returns module-scoped loggers that all share
one configured handler. Logging is wired up exactly once regardless of how many
modules import it.
"""

from __future__ import annotations

import logging
import sys

from app.utils.config import settings

_CONFIGURED = False

_LEVEL_COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[41m",  # red background
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """Formatter that tints the level name when writing to a TTY."""

    def __init__(self, useColor: bool) -> None:
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.useColor = useColor

    def format(self, record: logging.LogRecord) -> str:
        if self.useColor:
            color = _LEVEL_COLORS.get(record.levelname, "")
            record.levelname = f"{color}{record.levelname}{_RESET}"
        return super().format(record)


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_ColorFormatter(useColor=sys.stderr.isatty()))

    root = logging.getLogger("abc")
    root.setLevel(getattr(logging, settings.logLevel, logging.INFO))
    root.addHandler(handler)
    root.propagate = False

    _CONFIGURED = True


def getLogger(name: str) -> logging.Logger:
    """Return a namespaced child logger (e.g. ``abc.browser.manager``)."""
    _configure()
    return logging.getLogger(f"abc.{name}")
