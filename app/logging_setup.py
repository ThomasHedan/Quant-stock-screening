"""Logging configuration.

One place so that every entry point — the web app, the CLI, a test harness —
produces the same shape of line. Timestamps are UTC with an explicit offset,
matching everything else stored by this project; a log whose clock disagrees
with the data it describes is worse than no log.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

FORMAT: Final[str] = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"


class UtcFormatter(logging.Formatter):
    """Formatter that renders times in UTC rather than local time."""

    converter = staticmethod(__import__("time").gmtime)


def configure(level: str = "INFO", *, stream: object | None = None) -> None:
    """Install the root handler. Safe to call more than once."""
    handler = logging.StreamHandler(stream or sys.stderr)  # type: ignore[arg-type]
    handler.setFormatter(UtcFormatter(FORMAT, DATE_FORMAT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # These libraries are chatty at INFO and say nothing this app acts on.
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
