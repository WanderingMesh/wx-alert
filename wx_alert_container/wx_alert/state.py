"""Persistent record of what has already been delivered, and to where.

Without this, deduplication lives only in process memory and every container
restart re-notifies every currently active alert. Over HTTPS that is noise.
Over a shared LoRa channel, with `restart: unless-stopped` and any crash loop,
it is a burst of flood-routed packets each time the container comes back.

State is per transport because transports fail independently. An ntfy success
paired with a radio failure has to be representable, or the next cycle either
duplicates the notification or permanently skips the broadcast.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .nws import alert_fingerprint, alert_identity, alert_sent_time, parse_nws_datetime
from .text import clean_field

LOGGER = logging.getLogger("wx-alert")

# Version 1 was the format used by an earlier unversioned build of this
# program: a single status and fingerprint per alert, with no transport
# breakdown. This repository never shipped it, but a /data volume from that
# build could still be mounted, so the loader recognizes and upgrades it.
STATE_VERSION = 2

DEFAULT_STATE_FILE = Path("/data/notified-alerts.json")
DEFAULT_RETENTION_DAYS = 14


class StateError(RuntimeError):
    """Raised when persistent state cannot be read or written safely."""


@dataclass
class AlertState:
    """In-memory view of the persisted delivery history."""

    alerts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(
        self,
        alert: dict[str, Any],
        transport_name: str,
        status: str,
    ) -> None:
        """Record a terminal outcome for one alert on one transport."""
        identity = alert_identity(alert)
        now = datetime.now(timezone.utc).isoformat()
        sent = alert_sent_time(alert)

        entry = self.alerts.setdefault(identity, {})
        entry["event"] = clean_field(alert.get("event"), "Unknown weather alert")
        entry["nws_sent"] = sent.isoformat() if sent else None
        entry["recorded_at"] = now

        transports = entry.setdefault("transports", {})
        transports[transport_name] = {
            "status": status,
            "fingerprint": alert_fingerprint(alert),
            "at": now,
        }

    def is_settled(self, alert: dict[str, Any], transport_name: str) -> bool:
        """Has this exact version of this alert already been handled here?

        Compares the content fingerprint, not just the identity, so a product
        reissued with changed details is treated as new work.
        """
        entry = self.alerts.get(alert_identity(alert))
        if not entry:
            return False

        record = entry.get("transports", {}).get(transport_name)
        if not record:
            return False

        return record.get("fingerprint") == alert_fingerprint(alert)

    def has_record(self, alert: dict[str, Any], transport_name: str) -> bool:
        """Has this transport ever reached a terminal outcome for this alert?

        Distinct from is_settled, which compares the content fingerprint and so
        goes false again the moment NWS reissues the product. This ignores the
        fingerprint and stays true across a reissue, which is what the startup
        staleness policy needs: it answers "has this transport already had its
        turn at this alert?" rather than "is this exact version done?".

        Deliberately per transport. An alert the radio has never recorded is
        genuinely new *to the radio* even if ntfy pushed it an hour ago, which
        is also what makes a migrated single-transport state file behave
        correctly rather than replaying a backlog on air.
        """
        entry = self.alerts.get(alert_identity(alert))
        if not entry:
            return False

        return transport_name in entry.get("transports", {})

    def has_seen(self, alert: dict[str, Any]) -> bool:
        """Has any transport previously handled this alert identity at all?

        Distinct from is_settled: this stays true across content updates.
        """
        return alert_identity(alert) in self.alerts


def _migrate_v1(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Fold a version 1 record into the per-transport layout.

    Version 1 only ever tracked ntfy, so its single status belongs under that
    transport. Leaving other transports absent is correct and meaningful: it
    marks them as never attempted, which subjects them to the startup
    staleness policy rather than replaying the backlog.
    """
    migrated: dict[str, dict[str, Any]] = {}

    for identity, record in raw.get("alerts", {}).items():
        if not isinstance(record, dict):
            continue

        migrated[identity] = {
            "event": record.get("event", "Unknown weather alert"),
            "nws_sent": record.get("nws_sent"),
            "recorded_at": record.get("recorded_at"),
            "transports": {
                "ntfy": {
                    "status": record.get("status", "delivered"),
                    "fingerprint": record.get("fingerprint"),
                    "at": record.get("recorded_at"),
                },
            },
        }

    return migrated


def load_state(path: Path) -> AlertState:
    """Read persisted state, upgrading older formats in place."""
    if not path.exists():
        LOGGER.info(
            "Persistent state not found; starting with an empty state file=%s",
            path,
        )
        return AlertState()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"could not read state file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise StateError(f"state file {path} does not contain a JSON object")

    version = raw.get("version")

    if version == 1:
        alerts = _migrate_v1(raw)
        LOGGER.info(
            "Migrated %d state record(s) from version 1 to version %d file=%s",
            len(alerts),
            STATE_VERSION,
            path,
        )
        state = AlertState(alerts=alerts)
        # Persist immediately so the upgrade is not repeated, and so a later
        # crash cannot leave a half-understood file behind.
        save_state(path, state)
        return state

    if version != STATE_VERSION:
        raise StateError(
            f"state file {path} has unsupported version {version!r}; "
            f"expected {STATE_VERSION}"
        )

    alerts = raw.get("alerts", {})
    if not isinstance(alerts, dict):
        raise StateError(f"state file {path} contains an invalid alerts object")

    LOGGER.info("Loaded persistent state file=%s records=%d", path, len(alerts))
    return AlertState(alerts=alerts)


def save_state(path: Path, state: AlertState) -> None:
    """Write state atomically.

    Written to a temporary file in the same directory and renamed, because
    os.replace is atomic within a filesystem. A crash mid-write therefore
    leaves the previous good file intact rather than a truncated one, which
    would fail to load and cause every active alert to be re-sent.
    """
    payload = {
        "version": STATE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "alerts": state.alerts,
    }

    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    except OSError as exc:
        # Best-effort cleanup; the original file is still valid either way.
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StateError(f"could not write state file {path}: {exc}") from exc


def prune_state(state: AlertState, retention_days: int) -> int:
    """Drop records older than the retention window. Returns the count removed.

    NWS alerts expire, so an unbounded history would grow forever for no
    benefit. Records with an unparseable timestamp are kept rather than
    discarded, since deleting one risks a duplicate notification.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0

    for identity in list(state.alerts):
        recorded_at = parse_nws_datetime(state.alerts[identity].get("recorded_at"))
        if recorded_at is not None and recorded_at < cutoff:
            del state.alerts[identity]
            removed += 1

    return removed
