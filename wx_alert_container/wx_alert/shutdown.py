"""Cooperative shutdown signalling.

This lives in its own module so that both the application loop and the
individual transports can wait on the same stop event without importing each
other. Every sleep in this program waits on STOP_EVENT rather than calling
time.sleep, so `docker stop` takes effect immediately instead of blocking for
a full poll interval.
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any

LOGGER = logging.getLogger("wx-alert")

STOP_EVENT = threading.Event()


def request_stop(signum: int, _frame: Any) -> None:
    """Handle `docker stop`, SIGTERM, and Ctrl-C cleanly."""
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)

    LOGGER.info("Received %s; stopping after current operation", signal_name)
    STOP_EVENT.set()


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
