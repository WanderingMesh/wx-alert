"""National Weather Service API access and alert interpretation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Sequence

import requests

from .text import clean_field, clean_optional

NWS_API_URL = "https://api.weather.gov/alerts/active"

# Where the alert's GeoJSON geometry is stashed on the flattened record.
GEOMETRY_KEY = "geometry"

# NWS asks every client to identify itself with a real, monitored contact
# address so they can reach operators of misbehaving clients.
NWS_HEADERS = {
    "User-Agent": "local-weather-alert-checker/1.0 (admin@example.org)",
    "Accept": "application/geo+json",
}

# Product-class tags this program derives from the event name. Any of these
# appearing in configured tags is stale and gets replaced per alert.
EVENT_CLASS_TAGS = {"warning", "watch", "advisory", "statement", "outlook", "other"}

# Ordered loudest to quietest. Used to compare a product against a configured
# minimum class threshold.
EVENT_CLASS_RANK = {
    "warning": 5,
    "watch": 4,
    "advisory": 3,
    "statement": 2,
    "outlook": 1,
    "other": 0,
}

# CAP severity ordered highest to lowest, for threshold comparisons.
SEVERITY_RANK = {
    "extreme": 4,
    "severe": 3,
    "moderate": 2,
    "minor": 1,
    "unknown": 0,
}


def get_active_alerts(
    session: requests.Session,
    zones: Sequence[str],
) -> list[dict[str, Any]]:
    """Retrieve active NWS alerts for the supplied UGC zones.

    Queried by zone rather than by point. A point query intersects the
    coordinate with each alert's polygon, which excludes every storm-based
    warning whose polygon does not cover that exact spot; a county query
    returns polygon warnings and zone products alike. See zones.py.

    The GeoJSON geometry is folded into each alert record so the caller can
    measure how far the warned area actually is. NWS puts geometry beside
    properties rather than inside it, and nothing in the NWS property set uses
    this name, so there is no field to collide with.
    """
    if not zones:
        raise ValueError("at least one NWS zone is required")

    response = session.get(
        NWS_API_URL,
        params={"zone": ",".join(zones)},
        headers=NWS_HEADERS,
        timeout=20,
    )
    response.raise_for_status()

    document = response.json()
    features = document.get("features", [])

    if not isinstance(features, list):
        raise ValueError("NWS response does not contain a valid features list")

    alerts: list[dict[str, Any]] = []

    for feature in features:
        if not isinstance(feature, dict):
            continue

        properties = feature.get("properties", {})

        if isinstance(properties, dict):
            properties[GEOMETRY_KEY] = feature.get("geometry")
            alerts.append(properties)

    return alerts


def parse_nws_datetime(value: Any) -> datetime | None:
    """Parse an NWS ISO-8601 timestamp into an aware UTC datetime.

    Anything unparseable returns None rather than raising, because a missing
    or malformed timestamp must never take down a delivery cycle.
    """
    text = clean_optional(str(value)) if value is not None else None
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def alert_sent_time(alert: dict[str, Any]) -> datetime | None:
    """Return when NWS issued this alert.

    "sent" is the true issue time. "effective" and "onset" are fallbacks for
    products that omit it, ordered from most to least reliable.
    """
    for field in ("sent", "effective", "onset"):
        parsed = parse_nws_datetime(alert.get(field))
        if parsed is not None:
            return parsed

    return None


def alert_expiry_time(alert: dict[str, Any]) -> datetime | None:
    """Return when this alert stops applying.

    "ends" is the meteorological end of the event where present; "expires" is
    the administrative expiry of the product and is always populated.
    """
    for field in ("ends", "expires"):
        parsed = parse_nws_datetime(alert.get(field))
        if parsed is not None:
            return parsed

    return None


def alert_age_seconds(
    alert: dict[str, Any],
    now: datetime | None = None,
) -> int | None:
    """Return the age of an alert in seconds, or None if the issue time is unknown."""
    sent = alert_sent_time(alert)
    if sent is None:
        return None

    current = now or datetime.now(timezone.utc)

    # Clamp negatives: an alert with a future effective time is not "negative
    # age", it is simply brand new.
    return max(0, int((current - sent).total_seconds()))


def alert_remaining_seconds(
    alert: dict[str, Any],
    now: datetime | None = None,
) -> int | None:
    """Return how much longer an alert applies, or None if that is unknown.

    The complement of alert_age_seconds, and the better question to ask about
    a product on a shared radio channel: when a warning was issued says
    nothing about whether it still matters, while the time it has left says
    exactly that. Zero means it has lapsed.
    """
    expiry = alert_expiry_time(alert)
    if expiry is None:
        return None

    current = now or datetime.now(timezone.utc)

    return max(0, int((expiry - current).total_seconds()))


def format_age(seconds: int | None) -> str:
    """Render an age in compact human-readable form for log lines."""
    if seconds is None:
        return "unknown"

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"

    return f"{secs}s"


def classify_event(event: str) -> str:
    """Classify an NWS product by name into its urgency class.

    CAP severity alone is not enough to decide how loudly to announce a
    product: a Flood Watch and a Flash Flood Warning can both carry
    severity=Severe while demanding very different responses. The product
    name is what actually distinguishes them.

    Order matters. "Emergency" outranks everything, and several product names
    contain more than one keyword.
    """
    value = event.casefold()

    if "emergency" in value or "warning" in value:
        return "warning"
    if "watch" in value:
        return "watch"
    if "advisory" in value:
        return "advisory"
    if "statement" in value:
        return "statement"
    if "outlook" in value:
        return "outlook"

    return "other"


def alert_class(alert: dict[str, Any]) -> str:
    """Convenience wrapper: classify an alert record directly."""
    return classify_event(clean_field(alert.get("event"), "Unknown weather alert"))


def alert_severity_rank(alert: dict[str, Any]) -> int:
    """Return the CAP severity of an alert as a comparable rank."""
    severity = clean_field(alert.get("severity"), "Unknown").casefold()
    return SEVERITY_RANK.get(severity, 0)


def alert_geometry(alert: dict[str, Any]) -> Any:
    """Return the alert's GeoJSON geometry, or None for a zone product.

    Absence is meaningful rather than exceptional: watches, advisories, and
    special weather statements are issued to whole zones and carry no polygon.
    """
    return alert.get(GEOMETRY_KEY)


def alert_identity(alert: dict[str, Any]) -> str:
    """Return a stable identifier for one NWS product.

    This answers "is this the same alert?", not "has it changed?". NWS reuses
    an ID across reissues of the same product, so pair this with
    alert_fingerprint to detect updates.
    """
    alert_id = clean_optional(
        str(alert.get("id") or alert.get("@id") or "")
    )
    if alert_id:
        return alert_id

    # Defensive fallback for malformed or incomplete alert records. Hashed
    # rather than concatenated so the identifier stays bounded in length and
    # safe to use as a JSON object key. The unit separator cannot appear in
    # NWS text, so it cannot cause field-boundary collisions.
    fallback = "\x1f".join(
        clean_field(alert.get(field), "")
        for field in ("event", "sent", "headline", "areaDesc")
    )
    return "generated:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def alert_fingerprint(alert: dict[str, Any]) -> str:
    """Hash the fields that make an alert meaningfully different.

    NWS reissues a product under its original ID when details change, so
    identity alone would silently swallow updates. Fields are serialized with
    sorted keys so the hash is stable across API field ordering.
    """
    fields = (
        "event",
        "headline",
        "severity",
        "urgency",
        "certainty",
        "sent",
        "effective",
        "onset",
        "expires",
        "ends",
        "description",
        "instruction",
        "messageType",
    )
    payload = {field: alert.get(field) for field in fields}
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
