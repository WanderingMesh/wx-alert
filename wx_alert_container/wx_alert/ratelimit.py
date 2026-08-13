"""Airtime governance for radio transports.

On ntfy, bandwidth is free and a spare notification costs nothing. On a shared
LoRa channel it is the opposite: every channel message is flood-routed and
rebroadcast by every repeater that hears it, so one alert occupies the channel
for everyone in range, repeatedly. NWS can also return half a dozen active
products at once, and reissues warnings with lightly edited text throughout an
event.

Two independent gates therefore sit in front of the radio: a relevance filter
deciding whether an alert is worth any airtime at all, and a rate limiter
bounding how much airtime the program may consume regardless of relevance.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil
from typing import Any

from .nws import EVENT_CLASS_RANK, SEVERITY_RANK, alert_class, alert_severity_rank

LOGGER = logging.getLogger("wx-alert")


@dataclass(frozen=True)
class RelevanceFilter:
    """Decides whether an alert is important enough to broadcast.

    Both thresholds must be met. Class is checked first because it is the
    better signal: a Severe watch and a Severe warning demand very different
    responses, and only the product name distinguishes them.
    """

    minimum_class: str
    minimum_severity: str

    def __post_init__(self) -> None:
        if self.minimum_class not in EVENT_CLASS_RANK:
            choices = ", ".join(sorted(EVENT_CLASS_RANK))
            raise ValueError(f"minimum class must be one of: {choices}")
        if self.minimum_severity not in SEVERITY_RANK:
            choices = ", ".join(sorted(SEVERITY_RANK))
            raise ValueError(f"minimum severity must be one of: {choices}")

    def accepts(self, alert: dict[str, Any]) -> tuple[bool, str]:
        """Return whether to broadcast, and a reason when declining."""
        event_class = alert_class(alert)

        if EVENT_CLASS_RANK[event_class] < EVENT_CLASS_RANK[self.minimum_class]:
            return False, (
                f"product class {event_class} is below minimum "
                f"{self.minimum_class}"
            )

        if alert_severity_rank(alert) < SEVERITY_RANK[self.minimum_severity]:
            return False, (
                f"severity is below minimum {self.minimum_severity}"
            )

        return True, ""


@dataclass
class RateLimiter:
    """Bounds transmissions by minimum spacing and a rolling hourly cap.

    Nothing is queued here. A refusal is a refusal, reported to the caller and
    logged, because a weather alert delivered forty minutes late is worse than
    useless: the reader has either already seen the weather or acted on a stale
    picture. What the caller does about a refusal — wait it out, or offer the
    alert again on the next poll while NWS still lists it as active — is the
    caller's decision, and this class keeps no backlog of its own either way.
    """

    min_interval_seconds: int
    max_per_hour: int
    _sent_at: deque[datetime] = field(default_factory=deque, repr=False)

    def _expire(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=1)
        while self._sent_at and self._sent_at[0] < cutoff:
            self._sent_at.popleft()

    def cap_reached(self, now: datetime) -> bool:
        """Whether the rolling hourly cap is what blocks the next transmission.

        Callers need to tell the two limits apart because they clear on wildly
        different timescales. Minimum spacing is seconds and worth waiting out;
        hourly capacity can be most of an hour away and is not.
        """
        self._expire(now)

        return self.max_per_hour > 0 and len(self._sent_at) >= self.max_per_hour

    def check(self, now: datetime) -> tuple[bool, str]:
        """Test whether a transmission is allowed, without recording one."""
        self._expire(now)

        if self.cap_reached(now):
            oldest = self._sent_at[0]
            retry_in = int((oldest + timedelta(hours=1) - now).total_seconds())
            return False, (
                f"hourly cap of {self.max_per_hour} reached; "
                f"capacity returns in {max(0, retry_in)}s"
            )

        if self.min_interval_seconds > 0 and self._sent_at:
            elapsed = (now - self._sent_at[-1]).total_seconds()
            if elapsed < self.min_interval_seconds:
                wait = int(self.min_interval_seconds - elapsed)
                return False, (
                    f"minimum spacing of {self.min_interval_seconds}s not met; "
                    f"{wait}s remaining"
                )

        return True, ""

    def record(self, now: datetime) -> None:
        """Note that a transmission occurred."""
        self._expire(now)
        self._sent_at.append(now)

    def seconds_until_ready(self, now: datetime) -> int:
        """How long until the next transmission would be permitted.

        Rounded up. A caller that sleeps for this long then asks check() again
        must find capacity waiting for it, and truncating a 29.5 second wait to
        29 would send it back round for the remaining half second.
        """
        self._expire(now)

        waits = [0]

        if self.cap_reached(now):
            oldest = self._sent_at[0]
            waits.append(ceil((oldest + timedelta(hours=1) - now).total_seconds()))

        if self.min_interval_seconds > 0 and self._sent_at:
            elapsed = (now - self._sent_at[-1]).total_seconds()
            waits.append(ceil(self.min_interval_seconds - elapsed))

        return max(waits)
