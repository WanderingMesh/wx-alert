"""Render an NWS alert into a single MeshCore channel message.

A MeshCore channel message is small and hard-capped, so this module is mostly
about spending a very tight byte budget well. The constraints come from the
firmware:

    MAX_PACKET_PAYLOAD  184 bytes   the whole packet payload
    MAX_TEXT_LEN        160 bytes   the plaintext before encryption

and the 160 bytes are not all ours. A channel message plaintext carries a
four-byte timestamp and a flags byte, the firmware appends a terminator, and
it prepends the sending node's own name followed by ": ". A node named
"WX-Reno" therefore leaves about 142 usable bytes; a node using the full
32-character name allowance leaves about 116.

Two consequences drive the design. The budget is computed from the node name
read off the radio at startup rather than hardcoded, so renaming a node cannot
silently start truncating. And the budget is counted in *bytes*, not
characters: NWS text contains degree signs, en dashes, and curly quotes, so
text is folded to ASCII and any truncation respects UTF-8 boundaries.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from .nws import parse_nws_datetime
from .text import clean_field, clean_optional

# Firmware limits. See src/helpers/BaseChatMesh.h in the MeshCore firmware.
MESH_MAX_TEXT_LEN = 160
_TIMESTAMP_BYTES = 4
_FLAGS_BYTES = 1
_TERMINATOR_BYTES = 1
_NAME_SEPARATOR = ": "

# Held back so that a firmware revision which adds a byte or two of framing
# does not immediately start dropping packets.
_SAFETY_MARGIN = 4

# Below this there is no room to say anything useful, so the trailing detail
# is dropped entirely rather than reduced to a stub.
_MIN_FILL_BYTES = 24

ELLIPSIS = "..."

# NWS prose is mostly ASCII but not reliably so. Folding these explicitly
# keeps one byte per character instead of letting NFKD produce something
# unreadable or multi-byte.
_ASCII_FOLD = {
    "\u00a0": " ",   # non-breaking space
    "\u2010": "-",   # hyphen
    "\u2011": "-",   # non-breaking hyphen
    "\u2012": "-",   # figure dash
    "\u2013": "-",   # en dash
    "\u2014": "-",   # em dash
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u2026": "...",  # ellipsis
    "\u00b0": "deg",  # degree sign
    "\u00bd": "1/2",
    "\u00bc": "1/4",
    "\u00be": "3/4",
}

# NWS descriptions are structured as "* WHAT...text", "* WHERE...", and so on.
# The WHAT section is the one-line summary of what is actually happening and
# is far denser than the headline, which merely restates the event and times.
_WHAT_SECTION = re.compile(r"\*\s*WHAT\s*\.\.\.\s*(.+?)(?:\n\s*\n|\n\s*\*|\Z)", re.S)


def normalize_ascii(text: str) -> str:
    """Fold text to plain ASCII and collapse whitespace.

    Every non-ASCII character costs two or three of our scarce bytes and may
    not render on every client, so none survive.
    """
    for source, replacement in _ASCII_FOLD.items():
        text = text.replace(source, replacement)

    # Decompose anything remaining (accented letters) and drop the marks.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # NWS wraps prose at column 68; newlines carry no meaning in a chat line.
    return re.sub(r"\s+", " ", text).strip()


def mesh_text_budget(node_name: str) -> int:
    """Bytes of message text available on a node with this name."""
    overhead = (
        _TIMESTAMP_BYTES
        + _FLAGS_BYTES
        + _TERMINATOR_BYTES
        + len(node_name.encode("utf-8"))
        + len(_NAME_SEPARATOR.encode("utf-8"))
        + _SAFETY_MARGIN
    )
    return max(0, MESH_MAX_TEXT_LEN - overhead)


def truncate_bytes(text: str, budget: int, ellipsis: str = ELLIPSIS) -> str:
    """Shorten text to fit a byte budget, preferring a word boundary.

    Slicing encoded bytes can land inside a multi-byte sequence, so the
    candidate is decoded with errors ignored, which discards any partial
    trailing character.
    """
    if budget <= 0:
        return ""

    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text

    if budget <= len(ellipsis):
        return encoded[:budget].decode("utf-8", "ignore")

    keep = budget - len(ellipsis)
    candidate = encoded[:keep].decode("utf-8", "ignore")

    # Only honour a word boundary when it does not throw away most of what we
    # kept; otherwise a single long token would collapse the whole segment.
    cut = candidate.rfind(" ")
    if cut > 0 and cut >= len(candidate) * 0.6:
        candidate = candidate[:cut]

    return candidate.rstrip(" ,;.:-") + ellipsis


def parse_nws_datetime_local(value: Any) -> datetime | None:
    """Parse an NWS timestamp preserving its original UTC offset.

    NWS emits offsets in the alert area's own local time, which is exactly
    what a person reading the message needs. Converting to UTC, as the rest
    of the program does for comparisons, would throw that away.
    """
    text = clean_optional(str(value)) if value is not None else None
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_expiry(alert: dict[str, Any], now: datetime | None = None) -> str:
    """Render when the alert ends, in the alert area's local time.

    The weekday is included only when the end is not today, since "til 21:00"
    is unambiguous within the current day and three bytes cheaper.
    """
    for field in ("ends", "expires"):
        local = parse_nws_datetime_local(alert.get(field))
        if local is not None:
            break
    else:
        return ""

    reference = now or datetime.now(local.tzinfo)
    if reference.tzinfo is not None:
        reference = reference.astimezone(local.tzinfo)

    if local.date() == reference.date():
        return f"til {local:%H:%M}"

    return f"til {local:%a %H:%M}"


def format_area(alert: dict[str, Any]) -> str:
    """Render the affected area, collapsing long multi-zone lists.

    areaDesc can list a dozen counties separated by semicolons. Naming the
    first and counting the rest costs a handful of bytes instead of a hundred.
    """
    area = normalize_ascii(clean_field(alert.get("areaDesc"), ""))
    if not area:
        return ""

    zones = [zone.strip() for zone in area.split(";") if zone.strip()]
    if len(zones) <= 1:
        return area

    return f"{zones[0]} +{len(zones) - 1}"


def extract_detail(alert: dict[str, Any]) -> str:
    """Pick the most useful free-text detail available.

    Preference order matters. "instruction" is the actionable "what to do"
    text and is the single most valuable thing to carry. When it is absent,
    the WHAT section of the description summarizes what is happening. The
    headline is deliberately never used: it restates the event name and the
    times already present in the message.
    """
    instruction = clean_optional(alert.get("instruction"))
    if instruction:
        return normalize_ascii(instruction)

    description = clean_optional(alert.get("description"))
    if description:
        match = _WHAT_SECTION.search(description)
        if match:
            return normalize_ascii(match.group(1))
        return normalize_ascii(description)

    return ""


def build_mesh_message(
    alert: dict[str, Any],
    budget: int,
    now: datetime | None = None,
) -> str:
    """Compose the largest useful message that fits the byte budget.

    Segments are added in descending order of value and dropped from the
    bottom when they do not fit, rather than composing the whole thing and
    blindly cutting the tail. That way running out of room costs the least
    important information instead of whatever happened to be last.
    """
    event = normalize_ascii(clean_field(alert.get("event"), "Weather alert"))

    # The event name alone must fit; nothing else is worth keeping without it.
    if len(event.encode("utf-8")) > budget:
        return truncate_bytes(event, budget)

    head = event

    # Expiry outranks area: the monitored point is fixed by configuration, so
    # the area is largely implied, while "until when" never is.
    expiry = format_expiry(alert, now)
    area = format_area(alert)

    for candidate in (
        f"{event}: {area}, {expiry}" if area and expiry else "",
        f"{event}, {expiry}" if expiry else "",
        f"{event}: {area}" if area else "",
    ):
        if candidate and len(candidate.encode("utf-8")) <= budget:
            head = candidate
            break

    detail = extract_detail(alert)
    if not detail:
        return head

    remaining = budget - len(head.encode("utf-8")) - len(". ".encode("utf-8"))
    if remaining < _MIN_FILL_BYTES:
        return head

    return f"{head}. {truncate_bytes(detail, remaining)}"
