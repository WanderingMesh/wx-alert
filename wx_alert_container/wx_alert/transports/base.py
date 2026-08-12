"""The interface every delivery transport implements.

Transports differ enormously in their failure modes. ntfy either returns an
HTTP status or raises; a LoRa radio can be absent, busy, rate-limited by
policy, or simply unable to confirm anything at all. The application loop
should not know any of that, so all of it collapses into three outcomes.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable


class TransportError(RuntimeError):
    """Raised when a transport cannot be initialized from its configuration."""


class DeliveryResult(Enum):
    """The outcome of one delivery attempt.

    The distinction between SKIPPED and FAILED is what keeps state coherent.
    SKIPPED is a decision: this transport was never going to carry this alert,
    so recording it as attempted-and-finished is correct and it must not be
    retried. FAILED is an accident: the alert should stay eligible so the next
    poll cycle picks it up again.
    """

    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


@runtime_checkable
class Transport(Protocol):
    """A delivery destination for weather alerts."""

    name: str

    def start(self) -> None:
        """Acquire whatever this transport needs, and validate it.

        Called once before the first delivery cycle. Should raise
        TransportError on unrecoverable misconfiguration so the process fails
        loudly at boot rather than silently dropping alerts later.
        """

    def selftest(self) -> None:
        """Send one test message. Raises on failure."""

    def deliver(self, alert: dict[str, Any]) -> tuple[DeliveryResult, str]:
        """Attempt delivery of one alert.

        Returns the outcome and a short human-readable detail suitable for a
        log line or for recording in persistent state. Must not raise for
        ordinary delivery failures; return FAILED instead.
        """

    def close(self) -> None:
        """Release resources. Must be safe to call even if start() failed."""
