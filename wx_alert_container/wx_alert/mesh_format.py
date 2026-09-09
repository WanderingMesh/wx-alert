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

Three decisions drive the design.

The budget is computed from the node name read off the radio at startup
rather than hardcoded, so renaming a node cannot silently start truncating.
The budget is counted in *bytes*, not characters: NWS text contains degree
signs, en dashes, and curly quotes, so text is folded to ASCII and any
truncation respects UTF-8 boundaries.

And the message is assembled from *facts*, not from the leading characters
of the prose. NWS products follow a small number of templates, and each one
puts its decision-relevant content in a known place: the HAZARD line and the
"located N miles east of TOWN, moving ..." sentence in a storm-based warning,
the WHAT and IMPACTS bullets in a zone product, the Winds and Humidity bullets
in a fire-weather product. Those are extracted, tightened with the vocabulary
in abbrev.py, and added in order of value until the budget runs out. The
prose that would otherwise fill the message — "The National Weather Service in
Reno has issued a", "Blowing dust can be hazardous" — is never a candidate.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from .abbrev import abbreviate_name, polish, strip_dual_zone
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

# Below this there is no room to say anything useful, so a trailing segment
# is dropped entirely rather than reduced to a stub.
_MIN_FILL_BYTES = 24

# An instruction sentence at most this long is treated as an imperative
# ("TAKE COVER NOW!") and placed ahead of the facts. Anything longer is
# advice, which trails them.
_IMPERATIVE_MAX_CHARS = 40

# A joined list of zone names longer than this collapses to "first +N".
_AREA_LIST_MAX_CHARS = 32

# An onset this close to now is treated as already in force; showing a start
# time a few minutes away would cost bytes to say "now".
_ONSET_GRACE = timedelta(minutes=30)

ELLIPSIS = "..."

# NWS prose is mostly ASCII but not reliably so. Folding these explicitly
# keeps one byte per character instead of letting NFKD produce something
# unreadable or multi-byte.
_ASCII_FOLD = {
    "\u00a0": " ",  # non-breaking space
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote
    "\u201c": '"',  # left double quote
    "\u201d": '"',  # right double quote
    "\u2026": "...",  # ellipsis
    "\u00b0": "deg",  # degree sign
    "\u00bd": "1/2",
    "\u00bc": "1/4",
    "\u00be": "3/4",
}


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


# Cut points in order of preference. A sentence boundary leaves a complete
# thought; a clause boundary leaves a complete phrase; a word boundary is the
# floor. Each is accepted only if it keeps at least half of what fits, so a
# single long clause does not collapse the whole segment to its first word.
_CUT_POINTS = (". ", "! ", "? ", "; ", ", ", " ")


def truncate_bytes(text: str, budget: int, ellipsis: str = ELLIPSIS) -> str:
    """Shorten text to fit a byte budget, preferring to end on a whole thought.

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

    for separator in _CUT_POINTS:
        cut = candidate.rfind(separator)
        if cut > 0 and cut >= len(candidate) * 0.5:
            candidate = candidate[:cut]
            break

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


# --------------------------------------------------------------------------
# Header segments: event, area, window
# --------------------------------------------------------------------------


def format_event(alert: dict[str, Any]) -> str:
    """The product name, abbreviated: "Severe Thunderstorm Warning" -> "Svr Tstorm Wrn"."""  # noqa: E501
    return abbreviate_name(
        normalize_ascii(clean_field(alert.get("event"), "Weather alert"))
    )


def format_area(alert: dict[str, Any]) -> str:
    """Render the affected area compactly.

    "County" becomes "Co", state suffixes go (the monitored point fixes the
    state), duplicates go (NWS sometimes lists a zone twice), and a short
    list of zones is joined with "/" so a three-county storm warning names all
    three. A long list collapses to the first zone and a count.
    """
    zones: list[str] = []

    for raw in normalize_ascii(clean_field(alert.get("areaDesc"), "")).split(";"):
        zone = raw.strip()
        if not zone:
            continue
        zone = re.sub(r", [A-Z]{2}$", "", zone)
        zone = abbreviate_name(zone)
        if zone not in zones:
            zones.append(zone)

    if not zones:
        return ""

    joined = "/".join(zones)
    if len(zones) == 1 or len(joined) <= _AREA_LIST_MAX_CHARS:
        return joined

    return f"{zones[0]} +{len(zones) - 1}"


def format_window(alert: dict[str, Any], now: datetime | None = None) -> str:
    """Render when the alert applies, in the alert area's local time.

    Start and end are shown as "HH:MM-HH:MM". A date is attached only to a
    side whose day differs from today, so a warning ending in an hour costs
    eleven bytes ("til 14:15") while a watch for tomorrow reads
    "09/02 12:00-21:00". Once an alert is in force its start is history and
    is omitted.

    "ends" is the meteorological end of the event where present; "expires" is
    the administrative expiry of the product and is always populated.
    """
    end = None
    for field in ("ends", "expires"):
        end = parse_nws_datetime_local(alert.get(field))
        if end is not None:
            break
    if end is None:
        return ""

    start = parse_nws_datetime_local(alert.get("onset") or alert.get("effective"))

    reference = now or datetime.now(end.tzinfo)
    if reference.tzinfo is not None:
        reference = reference.astimezone(end.tzinfo)
    today = reference.date()

    if start is not None and start <= reference + _ONSET_GRACE:
        start = None

    if start is None:
        end_text = f"{end:%H:%M}" if end.date() == today else f"{end:%H:%M} {end:%m/%d}"
        return f"til {end_text}"

    start_text = (
        f"{start:%H:%M}" if start.date() == today else f"{start:%m/%d} {start:%H:%M}"
    )

    if end.date() == start.date():
        end_text = f"{end:%H:%M}"
    elif start.date() == today:
        end_text = f"{end:%H:%M} {end:%m/%d}"
    else:
        end_text = f"{end:%m/%d} {end:%H:%M}"

    return f"{start_text}-{end_text}"


# --------------------------------------------------------------------------
# Body segments: the facts, extracted per product template
# --------------------------------------------------------------------------

# Storm-based warnings and many statements use the Impact-Based Warning
# template: a "* At HHMM, a <storm> was located <where>, moving <how>." line,
# then HAZARD/SOURCE/IMPACT, then "Locations impacted include...".
_IBW_LOCATION = re.compile(
    r"At \d{1,4} ?[AP]M \w+, "
    r"(?:(?:Doppler )?radar (?:was tracking|indicated) )?"
    r"(?:a |an )?(?P<what>[^.]+?)"
    r"(?: (?:was located|were located|was|were)(?P<near> near)? | along )"
    r"(?P<where>[^.]+?)"
    r"(?:, moving (?P<moving>.+?)|\. Movement was (?P<movement>.+?))?\."
)
_IBW_HAZARD = re.compile(r"HAZARD\.\.\.(.+?)\.(?:\s|$)")
_IBW_IMPACT = re.compile(r"IMPACT\.\.\.(.+?)\.(?:\s|$)")
_IBW_NEAR = re.compile(r"will be near\.\.\.\s*(.+?)\.(?:\s|$)")
_IBW_TOWNS = re.compile(
    r"[Ll]ocations (?:impacted|that will experience [^.]*?)\s*include\.{0,3}\s*"
    r"(.+?)\.(?:\s|$)"
)
_TOWN_LIST_MAX = 4

# Zone products use "* WHAT...", "* WHERE...", "* WHEN...", "* IMPACTS...".
# Fire-weather products use "* Affected Area...", "* Winds...",
# "* Humidity...", "* Impacts...". Same shape, different keys.
_BULLET = re.compile(
    r"\*\s*([A-Za-z][A-Za-z /]+?)\s*\.\.\.\s*(.+?)(?=\s\*\s[A-Za-z]|\Z)"
)

# Bullets whose content is already in the header (WHERE, WHEN, Affected Area,
# Timing) or is administrative (Changes, Source, Duration).
_SKIPPED_BULLETS = frozenset(
    {
        "where",
        "when",
        "affected area",
        "timing",
        "additional details",
        "duration",
        "changes",
        "source",
    }
)

# Bullets whose value is meaningless without its key: "15 to 25 mph" needs
# "Winds"; "10 to 15 percent" needs "RH".
_KEYED_BULLETS = {
    "winds": "Winds",
    "wind": "Winds",
    "humidity": "RH",
    "relative humidity": "RH",
    "temperature": "Temps",
    "temperatures": "Temps",
    "snow": "Snow",
    "ice": "Ice",
}

# Storm Prediction Center watches arrive as an all-caps banner naming the
# watch number and the cities covered. Nothing else in them is quantitative.
_SPC_NUMBER = re.compile(r"WATCH\s+(\d+)")
_SPC_CITIES = re.compile(r"CITIES OF (.+?)\.")

_CANCELLED = re.compile(
    r"allowed to expire|has been cancelled|threat has ended|no longer expected",
    re.IGNORECASE,
)
_CANCEL_REASON = re.compile(
    r"(weakened|moved out of|ended|receded|diminished)", re.IGNORECASE
)

# AWIPS product identifier ("TORREV", "AQAPSR") and "...HEADLINE BANNER..."
# that some offices lead with. Neither is prose.
_AWIPS_ID = re.compile(r"^[A-Z]{6}\s+")
_BANNER = re.compile(r"^\.\.\..+?\.\.\.\s*")


def _cancellation_segments(description: str) -> list[str] | None:
    """A cancellation says one thing; say it in one word."""
    if not _CANCELLED.search(description):
        return None
    reason = _CANCEL_REASON.search(description)
    suffix = f" ({reason.group(1).lower()})" if reason else ""
    return [f"Cancelled{suffix}."]


def _storm_segments(description: str) -> list[str]:
    """Facts from an Impact-Based Warning, most specific first.

    The location sentence is the single most valuable thing in the product:
    it is the only place the storm's position and motion appear. HAZARD names
    the threat more precisely than the location sentence's own description of
    the storm ("a severe thunderstorm capable of producing a tornado" versus
    "Tornado"), so it replaces that description when the description is long.
    IMPACT is kept only when it says something HAZARD did not.
    """
    segments: list[str] = []

    hazard_match = _IBW_HAZARD.search(description)
    hazard = hazard_match.group(1).strip() if hazard_match else ""

    location = _IBW_LOCATION.search(description)
    if location:
        what = location.group("what").strip()
        where = re.sub(r", or .+$", "", location.group("where").strip())
        where = re.sub(r"^a line extending from", "along line", where)
        if location.group("near"):
            where = f"near {where}"
        moving = location.group("moving") or location.group("movement")

        if len(what.split()) > 3 and hazard:
            what, hazard = hazard, ""

        sentence = f"{what} {where}".strip()
        if moving:
            sentence += f", moving {moving}"
        segments.append(sentence + ".")

    if hazard:
        segments.append(hazard + ".")

    near = _IBW_NEAR.search(description)
    if near:
        segments.append("Near " + re.sub(r"\s*around\s*", ", ", near.group(1)) + ".")

    towns = _IBW_TOWNS.search(description)
    if towns:
        names = [
            n.strip() for n in re.split(r",\s*|\s+and\s+", towns.group(1)) if n.strip()
        ]
        segments.append("Incl " + ", ".join(names[:_TOWN_LIST_MAX]) + ".")

    impact_match = _IBW_IMPACT.search(description)
    if impact_match:
        impact = impact_match.group(1).strip()
        already_said = any(
            impact.lower().startswith(segment.lower()[:20]) for segment in segments
        )
        if not already_said:
            segments.append(impact + ".")

    return segments


def _bullet_segments(description: str) -> list[str]:
    """Facts from a bulleted zone or fire-weather product."""
    segments: list[str] = []

    for raw_key, value in _BULLET.findall(description):
        key = raw_key.strip().lower()
        if key in _SKIPPED_BULLETS:
            continue
        value = value.strip()
        if key in _KEYED_BULLETS:
            value = f"{_KEYED_BULLETS[key]} {value}"
        segments.append(value)

    return segments


def _spc_segments(description: str) -> list[str]:
    segments: list[str] = []

    number = _SPC_NUMBER.search(description)
    if number:
        segments.append(f"SPC Wtch {number.group(1)}.")

    cities = _SPC_CITIES.search(description)
    if cities:
        names = re.sub(r",?\s+AND\s+", ", ", cities.group(1)).title()
        segments.append(f"Incl {names}.")

    return segments


def _raw_segments(description: str) -> list[str]:
    """Pick the extractor that matches the product's template."""
    cancelled = _cancellation_segments(description)
    if cancelled is not None:
        return cancelled
    if "HAZARD..." in description:
        return _storm_segments(description)
    if _BULLET.search(description):
        return _bullet_segments(description)
    if "HAS ISSUED" in description and "WATCH" in description:
        return _spc_segments(description)
    return [description] if description else []


def alert_segments(alert: dict[str, Any], *, abbreviate: bool = True) -> list[str]:
    """The body of the message as an ordered list of sentences.

    Order is by value to a reader who has only the header. A short imperative
    from the instruction ("TAKE COVER NOW!") comes first because it is the
    action. The product's own facts follow. Longer instruction sentences trail
    them, since they are advice and the facts are what make the advice
    concrete.
    """
    description = normalize_ascii(clean_field(alert.get("description"), ""))
    description = strip_dual_zone(_BANNER.sub("", _AWIPS_ID.sub("", description)))

    segments: list[str] = []
    for raw in _raw_segments(description):
        segments.extend(polish(raw, abbreviate=abbreviate))

    instruction = normalize_ascii(clean_field(alert.get("instruction"), ""))
    advice = polish(instruction, abbreviate=abbreviate) if instruction else []

    if advice and len(advice[0]) <= _IMPERATIVE_MAX_CHARS:
        segments.insert(0, advice.pop(0))

    return segments + advice


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _fits(text: str, budget: int) -> bool:
    return len(text.encode("utf-8")) <= budget


def build_mesh_message(
    alert: dict[str, Any],
    budget: int,
    now: datetime | None = None,
) -> str:
    """Compose the largest useful message that fits the byte budget.

    The header is event, area, and window. If that does not fit the area
    goes first: the monitored point is fixed by configuration so the area is
    largely implied, while "until when" never is. The event name alone is the
    floor.
    Body segments are appended in order of value until one does not fit; that
    one is truncated at a sentence or clause boundary if there is enough room
    for it to still say something, and everything after it is dropped.

    Duplicate segments are suppressed on their first three words, because
    HAZARD and IMPACT, or WHAT and the instruction, often open identically.
    """
    event = format_event(alert)
    if not _fits(event, budget):
        return truncate_bytes(event, budget)

    area = format_area(alert)
    window = format_window(alert, now)

    head = event
    for candidate in (
        f"{event}: {area} {window}" if area and window else "",
        f"{event} {window}" if window else "",
        f"{event}: {area}" if area else "",
    ):
        if candidate and _fits(candidate, budget):
            head = candidate
            break

    message = head
    seen_openings: set[str] = set()

    for segment in alert_segments(alert):
        opening = " ".join(re.findall(r"\w+", segment.lower())[:3])
        if opening in seen_openings:
            continue
        seen_openings.add(opening)

        separator = " " if message.endswith(("!", ".", "?")) else ". "
        candidate = f"{message}{separator}{segment}"
        if _fits(candidate, budget):
            message = candidate
            continue

        room = budget - len(f"{message}{separator}".encode())
        if room >= _MIN_FILL_BYTES:
            message = f"{message}{separator}{truncate_bytes(segment, room)}"
        break

    return message
