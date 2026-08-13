"""Per-transport policy for declining to deliver an alert."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .nws import (
    alert_age_seconds,
    alert_class,
    alert_remaining_seconds,
    format_age,
)


@dataclass(frozen=True)
class StartupPolicy:
    """Controls what a transport does with old alerts on its first cycle.

    NWS returns every *currently active* alert, not just newly issued ones. A
    restart therefore looks identical to a burst of fresh alerts, and without
    this a container recycled at 3am would announce a watch issued eight hours
    earlier as though it had just been issued.

    Persistent state handles the common case. This policy covers the first run
    against an empty state file, and any alert the state has never seen.

    A suppression here is deliberately terminal rather than deferred: the whole
    point is that the alert is old news, and postponing it to the second cycle
    would announce it a few minutes later and achieve nothing. That makes the
    decision below load-bearing, because there is no later chance to correct
    it.
    """

    max_age_seconds: int
    always_notify_warnings: bool

    # A warning with at least this much time left to run is announced despite
    # its age. Zero disables the test.
    #
    # This exists for the radio, which does not get the unconditional warning
    # bypass ntfy has. "How old is it" is the wrong question to ask about a
    # warning: a Tornado Warning issued 40 minutes ago and in force for
    # another 20 is not stale news, it is the most important thing happening.
    # A warning that expires in two minutes genuinely is not worth the
    # airtime, and this distinguishes the two without replaying a backlog.
    warning_min_remaining_seconds: int = 0

    @property
    def enabled(self) -> bool:
        return self.max_age_seconds > 0


def should_suppress_on_startup(
    alert: dict[str, Any],
    policy: StartupPolicy,
    first_cycle: bool,
    now: datetime | None = None,
    previously_handled: bool = False,
) -> tuple[bool, str]:
    """Decide whether an alert is too stale to announce on the first cycle.

    Returns the decision and a reason suitable for logging.
    """
    if not first_cycle or not policy.enabled:
        return False, ""

    # Persistent state proves this transport already had a turn at this alert,
    # so it is not part of a cold-start backlog. Either the product has been
    # reissued with new content, or the previous attempt failed and this is the
    # retry — and a radio that was unreachable while a warning was issued must
    # not have that warning quietly written off once it recovers.
    if previously_handled:
        return False, ""

    if alert_class(alert) == "warning":
        # Silently swallowing an active warning is the one failure mode worth
        # accepting duplicates to avoid.
        if policy.always_notify_warnings:
            return False, ""

        remaining = alert_remaining_seconds(alert, now)

        if (
            policy.warning_min_remaining_seconds > 0
            and remaining is not None
            and remaining >= policy.warning_min_remaining_seconds
        ):
            return False, ""

    age = alert_age_seconds(alert, now)

    if age is None:
        return True, "NWS issue time is unavailable"

    if age > policy.max_age_seconds:
        return True, (
            f"alert age {format_age(age)} exceeds startup limit "
            f"{format_age(policy.max_age_seconds)}"
        )

    return False, ""
