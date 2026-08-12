"""Liveness heartbeat and the Docker HEALTHCHECK entry point.

The runtime image has no shell and no curl, so the health check is this
module, run as `python -m wx_alert.health`. It is deliberately dependency-free
and reads only a file the main loop writes.

Two failures matter operationally and neither shows up as a crash. The poll
loop can wedge on a call that never returns, and the radio can vanish when its
USB device is unplugged and replugged, since the container holds the original
device node. Both leave a process that is running but no longer doing its job.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("wx-alert")

HEARTBEAT_FILENAME = "heartbeat.json"

# A cycle can legitimately run long, so allow several intervals to pass before
# calling the process wedged.
STALE_INTERVAL_MULTIPLIER = 3
MINIMUM_STALE_SECONDS = 120


def heartbeat_path(state_file: Path) -> Path:
    """Place the heartbeat beside the state file, in the same writable volume."""
    return state_file.parent / HEARTBEAT_FILENAME


def write_heartbeat(
    path: Path,
    *,
    check_interval: int,
    cycle: int,
    consecutive_failures: dict[str, int],
) -> None:
    """Record that a cycle completed. Never raises.

    A heartbeat write failure must not take down an otherwise working poller,
    so problems are logged and swallowed. A genuinely unwritable volume will
    surface as a stale heartbeat, which is the correct signal anyway.
    """
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "check_interval": check_interval,
        "cycle": cycle,
        "consecutive_failures": consecutive_failures,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        LOGGER.warning("Could not write heartbeat file=%s error=%s", path, exc)


def evaluate(payload: dict[str, Any], now: datetime) -> tuple[bool, str]:
    """Decide whether the recorded heartbeat represents a healthy process."""
    updated_raw = payload.get("updated_at")

    try:
        updated = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False, f"heartbeat timestamp is unreadable: {updated_raw!r}"

    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)

    interval = payload.get("check_interval") or 0
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = 0

    limit = max(MINIMUM_STALE_SECONDS, interval * STALE_INTERVAL_MULTIPLIER)
    age = (now - updated.astimezone(timezone.utc)).total_seconds()

    if age > limit:
        return False, (
            f"last cycle completed {int(age)}s ago, which exceeds the {limit}s limit"
        )

    failures = payload.get("consecutive_failures") or {}
    if isinstance(failures, dict):
        failing = {
            name: count
            for name, count in failures.items()
            if isinstance(count, int) and count > 0
        }
        if failing:
            detail = ", ".join(f"{n}={c}" for n, c in sorted(failing.items()))
            return False, f"transports failing for consecutive cycles: {detail}"

    return True, f"last cycle completed {int(age)}s ago"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0]) if argv else Path("/data") / HEARTBEAT_FILENAME

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"unhealthy: no heartbeat at {path}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unhealthy: cannot read {path}: {exc}", file=sys.stderr)
        return 1

    healthy, detail = evaluate(payload, datetime.now(timezone.utc))

    if healthy:
        print(f"healthy: {detail}")
        return 0

    print(f"unhealthy: {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
