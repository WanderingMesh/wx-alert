"""The interface every delivery transport implements.

Transports differ enormously in their failure modes. ntfy either returns an
HTTP status or raises; a LoRa radio can be absent, busy, rate-limited by
policy, or simply unable to confirm anything at all. The application loop
should not know any of that, so all of it collapses into four outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class TransportError(RuntimeError):
    """Raised when a transport cannot be initialized from its configuration."""


@dataclass(frozen=True)
class DeliveryContext:
    """Per-cycle information a transport needs to make policy decisions.

    `now` is stamped once per cycle and passed in rather than read from the
    clock inside each transport, so that staleness decisions and rendered
    message timestamps are deterministic under test. It is deliberately not
    suitable for rate limiting: every alert in a batch shares this one value,
    so elapsed time between two transmissions measured against it is always
    zero. A transport that meters its own airtime reads a real clock instead.

    `previously_handled` says whether this transport has ever reached a
    terminal outcome for this alert before, which is what distinguishes an
    alert that predates the process from one it has already had a turn at.
    """

    first_cycle: bool
    now: datetime
    previously_handled: bool = False

    @classmethod
    def create(cls, first_cycle: bool) -> DeliveryContext:
        return cls(first_cycle=first_cycle, now=datetime.now(timezone.utc))

    def for_transport(self, previously_handled: bool) -> DeliveryContext:
        """Narrow the cycle context to one transport's history of one alert."""
        return replace(self, previously_handled=previously_handled)


class DeliveryResult(Enum):
    """The outcome of one delivery attempt.

    Three of these are terminal for this version of the alert and one is not,
    which is what keeps persistent state coherent.

    SENT and SKIPPED are both settled. SKIPPED is a decision: this transport
    was never going to carry this alert, so recording it as
    attempted-and-finished is correct and it must not be reconsidered.

    DEFERRED is the one outcome that must not be recorded. It means the
    transport would have carried the alert but could not right now, for a
    reason that clears on its own — an airtime budget that refills, for
    instance. Recording it would retire the alert permanently over a condition
    that lasted thirty seconds. Unlike FAILED it is not an error, so it must
    not count toward the consecutive-failure budget that restarts the
    container.

    FAILED is an accident: the alert stays eligible so the next poll cycle
    picks it up again.
    """

    SENT = "sent"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
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

    def deliver(
        self,
        alert: dict[str, Any],
        context: DeliveryContext,
    ) -> tuple[DeliveryResult, str]:
        """Attempt delivery of one alert.

        Returns the outcome and a short human-readable detail suitable for a
        log line or for recording in persistent state. Must not raise for
        ordinary delivery failures; return FAILED instead.

        Return DEFERRED rather than SKIPPED whenever the reason for declining
        is temporary, or the alert will be retired over a condition that no
        longer applies by the next cycle.
        """

    def close(self) -> None:
        """Release resources. Must be safe to call even if start() failed."""
