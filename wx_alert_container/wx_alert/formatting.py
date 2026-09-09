"""Render NWS alerts for each output surface."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .mesh_format import alert_segments, format_window
from .nws import EVENT_CLASS_TAGS, classify_event
from .text import clean_field

# A push notification has no firmware cap, but a phone shows only the first
# few lines and a reader scrolls the rest. This is generous enough to carry
# every extracted fact and the full instruction, and tight enough that a
# fifteen-paragraph Air Quality Alert does not arrive as a wall of text.
NTFY_BODY_MAX_CHARS = 800


def build_compact_stdout_message(alert: dict[str, Any]) -> str:
    """Build the two-line compact record: event followed by headline."""
    event = clean_field(alert.get("event"), "Unknown weather alert")
    headline = clean_field(alert.get("headline"), "No headline provided.")
    return f"{event}\n{headline}"


def build_compact_ntfy_body(alert: dict[str, Any], now: datetime | None = None) -> str:
    """Build the body of a compact ntfy notification.

    The event is already the notification title, so repeating it in the body
    (which is what the NWS headline does, along with the issuing office and
    the times) wastes the two or three lines a phone shows before the user
    taps. Instead the body leads with where and until when, then carries the
    same extracted facts the radio sends, unabbreviated because there is
    room, and the full instruction.
    """
    area = clean_field(alert.get("areaDesc"), "").replace(";", ",")
    window = format_window(alert, now)
    header = " - ".join(part for part in (area, window) if part)

    facts = " ".join(alert_segments(alert, abbreviate=False))
    body = f"{header}\n\n{facts}" if header and facts else header or facts

    if len(body) > NTFY_BODY_MAX_CHARS:
        body = body[: NTFY_BODY_MAX_CHARS - 3].rstrip() + "..."

    return body or clean_field(alert.get("headline"), "No details provided.")


def build_verbose_message(alert: dict[str, Any]) -> str:
    """Build complete human-readable output for one NWS alert."""
    event = clean_field(alert.get("event"), "Unknown")
    severity = clean_field(alert.get("severity"), "Unknown")
    urgency = clean_field(alert.get("urgency"), "Unknown")
    certainty = clean_field(alert.get("certainty"), "Unknown")
    message_type = clean_field(alert.get("messageType"), "Unknown")
    area = clean_field(alert.get("areaDesc"), "Unknown")
    sent = clean_field(alert.get("sent"), "Unknown")
    effective = clean_field(alert.get("effective"), "Unknown")
    onset = clean_field(alert.get("onset"), "Unknown")
    expires = clean_field(alert.get("expires"), "Unknown")
    ends = clean_field(alert.get("ends"), "Unknown")
    headline = clean_field(alert.get("headline"), "No headline provided.")
    alert_id = clean_field(alert.get("id") or alert.get("@id"), "Unknown")
    description = clean_field(
        alert.get("description"),
        "No description provided.",
    )
    instruction = clean_field(
        alert.get("instruction"),
        "No specific instructions provided.",
    )

    return (
        f"Event:        {event}\n"
        f"Severity:     {severity}\n"
        f"Urgency:      {urgency}\n"
        f"Certainty:    {certainty}\n"
        f"Message type: {message_type}\n"
        f"Area:         {area}\n"
        f"Sent:         {sent}\n"
        f"Effective:    {effective}\n"
        f"Onset:        {onset}\n"
        f"Expires:      {expires}\n"
        f"Ends:         {ends}\n"
        f"Headline:     {headline}\n"
        f"Alert ID:     {alert_id}\n"
        "\n"
        "Description:\n"
        f"{description}\n"
        "\n"
        "Instructions:\n"
        f"{instruction}"
    )


def ntfy_priority_for_alert(
    alert: dict[str, Any],
    configured_priority: str,
) -> str:
    """Choose an ntfy priority using product class first, then CAP severity.

    Mapping severity alone over-promotes watches: NWS routinely issues a Flood
    Watch with severity=Severe, which a naive severity map turns into the same
    priority as an active Flash Flood Warning. Classifying the product first
    and using severity only to break ties within a class keeps the loudest
    priorities reserved for events that are actually happening.
    """
    if configured_priority != "auto":
        # "urgent" is an ntfy alias for max; normalize so downstream code and
        # logs only ever see the canonical name.
        return "max" if configured_priority == "urgent" else configured_priority

    event = clean_field(alert.get("event"), "Unknown weather alert")
    event_class = classify_event(event)
    severity = clean_field(alert.get("severity"), "Unknown").casefold()

    if "emergency" in event.casefold():
        return "max"
    if event_class == "warning":
        return "max" if severity in {"extreme", "severe"} else "high"
    if event_class == "watch":
        return "high" if severity in {"extreme", "severe"} else "default"
    if event_class == "advisory":
        return "default"
    if event_class in {"statement", "outlook"}:
        return "low"

    return "default" if severity in {"extreme", "severe", "moderate"} else "low"


def ntfy_tags_for_alert(alert: dict[str, Any], configured_tags: str) -> str:
    """Combine configured base tags with the alert's actual product class.

    Configured tags are treated as base tags only. Any product-class tag found
    there is stale by definition, since the correct class is derived per alert,
    so it is dropped before the real one is appended.
    """
    event = clean_field(alert.get("event"), "Unknown weather alert")
    event_class = classify_event(event)

    tags: list[str] = []
    seen: set[str] = set()

    for raw_tag in configured_tags.split(","):
        tag = raw_tag.strip()
        if not tag or tag.casefold() in EVENT_CLASS_TAGS:
            continue
        if tag.casefold() not in seen:
            tags.append(tag)
            seen.add(tag.casefold())

    if event_class not in seen:
        tags.append(event_class)

    return ",".join(tags)
