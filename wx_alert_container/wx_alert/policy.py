"""Per-transport policy for declining to deliver an alert."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .nws import alert_age_seconds, alert_class, format_age


@dataclass(frozen=True)
class StartupPolicy:
    """Controls what a transport does with old alerts on its first cycle.

    NWS returns every *currently active* alert, not just newly issued ones. A
    restart therefore looks identical to a burst of fresh alerts, and without
    this a container recycled at 3am would announce a watch issued eight hours
    earlier as though it had just been issued.

    Persistent state handles the common case. This policy covers the first run
    against an empty state file, and any alert the state has never seen.
    """

    max_age_seconds: int
    always_notify_warnings: bool

    @property
    def enabled(self) -> bool:
        return self.max_age_seconds > 0


def should_suppress_on_startup(
    alert: dict[str, Any],
    policy: StartupPolicy,
    first_cycle: bool,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether an alert is too stale to announce on the first cycle.

    Returns the decision and a reason suitable for logging.
    """
    if not first_cycle or not policy.enabled:
        return False, ""

    if alert_class(alert) == "warning" and policy.always_notify_warnings:
        # Silently swallowing an active warning is the one failure mode worth
        # accepting duplicates to avoid.
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
