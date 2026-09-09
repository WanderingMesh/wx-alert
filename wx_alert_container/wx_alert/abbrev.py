"""Vocabulary and rules for compressing NWS prose into very few bytes.

A MeshCore channel message has roughly 120 to 140 bytes of usable text, and
an NWS product routinely runs to a thousand. Truncating the prose spends the
budget on whatever NWS happened to write first, which is usually a restatement
of the product name and the time already in the message header. What a reader
on a radio actually needs is the fact that distinguishes this alert from the
last one: where the storm is, how fast the wind will be, how cold it gets.

Two mechanisms work together here, and they are separated deliberately:

A *dictionary* replaces words with shorter equivalents. There are two of
them, because a word that is safe to shorten in a product name is not always
safe in prose. "Excessive Heat Warning" reads fine as "Excsv Heat Wrn", but
"caused by Excsv rainfall" reads as a typo. The product-name vocabulary is
applied only to the event name and area; the prose vocabulary only to body
text. Hazard words that carry the meaning of the alert (Flood, Fire, Tornado,
Heat, Wind, Freeze, Frost, Fog) appear in neither, so they can never be
abbreviated away.

*Rules* remove or restructure whole phrases the dictionary cannot help with:
sentences that carry no decision-relevant content ("Blowing dust can be
hazardous"), agency names followed by their own acronym, 12-hour clock stamps
with a redundant second time zone, and NWS boilerplate whose information is
already in the message header.

The vocabulary lives in code rather than configuration so that an operator
cannot, with the best of intentions, abbreviate "Flood" into something a
stranger on the channel will not recognise.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# --------------------------------------------------------------------------
# Dictionaries
# --------------------------------------------------------------------------

# Product-name vocabulary. Applied to the event name and the area description
# and nowhere else. Order matters where one pattern contains another:
# "thunderstorm" must be handled before "storm".
#
# Each entry is a regular expression fragment (matched as a whole word,
# case-insensitively) and its replacement. "{s}" in a replacement is expanded
# to "s" when the matched word was plural.
NAME_TERMS: tuple[tuple[str, str], ...] = (
    (r"thunderstorms?", "Tstorm{s}"),
    (r"storms?", "Strm{s}"),
    (r"weather", "Wx"),
    (r"extreme", "Xtrm"),
    (r"severe", "Svr"),
    (r"warning", "Wrn"),
    (r"watch", "Wtch"),
    (r"advisory", "Adv"),
    (r"statement", "Stmt"),
    (r"emergency", "Emrg"),
    (r"excessive", "Excsv"),
    (r"quality", "Qlty"),
    (r"hazards", "Hzrds"),
    (r"blowing", "Blwng"),
    (r"freezing", "Frzng"),
    (r"special", "Spcl"),
    (r"hydrologic", "Hydro"),
    (r"counties", "Cos"),
    (r"county", "Co"),
    (r"including", "incl"),
    (r"northern", "N"),
    (r"southern", "S"),
    (r"eastern", "E"),
    (r"western", "W"),
)

# Prose vocabulary. Applied to description and instruction text. Everything
# here is either a standard meteorological abbreviation (mph is already one),
# a unit, a compass point, or a word whose short form is unambiguous in
# context. Product-class words are included because NWS prose refers to the
# product by name ("For the High Wind Warning on Wednesday") and there is no
# reason to spell it out in the body when the header already abbreviates it.
#
# Numeric ranges are collapsed with the dictionary rather than a separate rule
# because they are just another substitution: "35 to 45 mph" -> "35-45 mph".
PROSE_TERMS: tuple[tuple[str, str], ...] = (
    (r"National Weather Service", "NWS"),
    (r"Doppler radar", "radar"),
    (r"thunderstorms?", "tstorm{s}"),
    (r"weather", "Wx"),
    (r"warning", "Wrn"),
    (r"watch", "Wtch"),
    (r"advisory", "Adv"),
    (r"statement", "Stmt"),
    (r"severe", "Svr"),
    (r"extreme", "Xtrm"),
    (r"counties", "Cos"),
    (r"county", "Co"),
    (r"miles?", "mi"),
    (r"feet", "ft"),
    (r"half inch", '1/2"'),
    (r"(\d+(?:\.\d+)?) inch(?:es)?", '\\1"'),
    (r"degrees", "deg"),
    (r"(\d+) percent", "\\1%"),
    (r"temperatures?", "temp{s}"),
    (r"relative humidity", "RH"),
    (r"humidity", "RH"),
    (r"visibility", "vis"),
    (r"approximately", "approx"),
    (r"through", "thru"),
    (r"until", "til"),
    (r"in excess of", "over"),
    (r"less than", "under"),
    (r"interstate (\d+)", "I-\\1"),
    (r"mile markers?", "MM"),
    (r"near", "nr"),
    (r"including", "incl"),
    (r"north ?east(?:ern)?", "NE"),
    (r"north ?west(?:ern)?", "NW"),
    (r"south ?east(?:ern)?", "SE"),
    (r"south ?west(?:ern)?", "SW"),
    (r"northern|north", "N"),
    (r"southern|south", "S"),
    (r"eastern|east", "E"),
    (r"western|west", "W"),
    (r"monday", "Mon"),
    (r"tuesday", "Tue"),
    (r"wednesday", "Wed"),
    (r"thursday", "Thu"),
    (r"friday", "Fri"),
    (r"saturday", "Sat"),
    (r"sunday", "Sun"),
    (r"one", "1"),
    (r"two", "2"),
    (r"three", "3"),
    (r"four", "4"),
    (r"five", "5"),
    (r"six", "6"),
    (r"seven", "7"),
    (r"eight", "8"),
    (r"nine", "9"),
    (r"ten", "10"),
    (r"wind gusts", "gusts"),
    (r"with gusts", ", gusts"),
    (r"gusts of", "gusts"),
    (r"conditions with", "-"),
    (r"is expected|are expected|is forecast", "expected"),
    (r"between (\d+) and (\d+)", "\\1-\\2"),
    (r"(\d+) to (\d+)", "\\1-\\2"),
    (r"as low as", "to"),
    (r"up to", "to"),
)

# Replacements that must not inherit the capital of the word they replace
# mid-sentence. Mostly function words: "Up to 15 percent" at the start of a
# bullet becomes "To 15%", which is right, while "as low as 30" in the middle
# of one would become "To 30", which is not. "radar" is here because its
# source, "Doppler radar", is always capitalised.
_NO_INHERITED_CASE = frozenset(
    {
        "to",
        "thru",
        "til",
        "nr",
        "over",
        "under",
        "incl",
        "expected",
        "radar",
        "-",
        ", gusts",
    }
)

_SENTENCE_BOUNDARY = re.compile(r"(?:^|[.!?]\s+)$")


def _compile(
    terms: Iterable[tuple[str, str]],
) -> tuple[tuple[re.Pattern[str], str], ...]:
    """Compile a dictionary once; every alert in a cycle goes through it."""
    return tuple(
        (re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE), replacement)
        for pattern, replacement in terms
    )


_NAME_COMPILED = _compile(NAME_TERMS)
_PROSE_COMPILED = _compile(PROSE_TERMS)


def _apply_terms(terms: Iterable[tuple[re.Pattern[str], str]], text: str) -> str:
    """Apply one dictionary, preserving the case of what was replaced.

    Case handling is what keeps the output readable rather than merely
    short. An all-caps source word (SPC watch text is all caps) produces an
    all-caps replacement; a capitalised word produces a capitalised one; a
    lower-case word is left lower-case. Function-word replacements are the
    exception described on _FUNCTION_WORDS.
    """
    for compiled, replacement in terms:

        def substitute(match: re.Match[str], replacement: str = replacement) -> str:
            source = match.group(0)
            result = re.sub(
                r"\\(\d)",
                lambda group: match.group(int(group.group(1))) or "",
                replacement,
            )
            result = result.replace("{s}", "s" if source.lower().endswith("s") else "")

            if result in _NO_INHERITED_CASE:
                at_sentence_start = bool(
                    _SENTENCE_BOUNDARY.search(match.string[: match.start()])
                )
                if at_sentence_start and result[0].isalpha():
                    return result[0].upper() + result[1:]
                return result

            if source.isupper() and len(source) > 1 and result.isalpha():
                return result.upper()
            if source[0].isupper() and result[0].islower():
                return result[0].upper() + result[1:]
            return result

        text = compiled.sub(substitute, text)

    # "with gusts" -> ", gusts" leaves the space that preceded "with".
    return re.sub(r"\s+,", ",", text)


def abbreviate_name(text: str) -> str:
    """Shorten a product name or area description."""
    return _apply_terms(_NAME_COMPILED, text)


def abbreviate_prose(text: str) -> str:
    """Shorten body text."""
    return _apply_terms(_PROSE_COMPILED, text)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

# "The Arizona Department of Environmental Quality (ADEQ)" -> "ADEQ". The
# agency went to the trouble of supplying its own acronym; use it. Lower-case
# connectives are allowed inside the name so "Department of" does not break
# the match.
_ACRONYM = re.compile(
    r"(?:The )?(?:[A-Z][\w'-]*(?: (?:of|and|for|the))? ){2,}\(([A-Z]{2,})\)"
)

# 12-hour clock as NWS writes it: "130 PM PDT", "1126 AM PDT", "8 PM MST".
# Some offices append a second zone in slashes for areas straddling a
# boundary, "/1126 AM MST/"; the reader is in one of those zones and the
# message header already uses the alert's own offset, so it is dropped.
_DUAL_ZONE = re.compile(r"\s*/\d{1,4} ?[AP]M [A-Z]{3,4}/")
_CLOCK = re.compile(r"\b(\d{1,2})(\d{2})? ?([AP]M)(?: [A-Z]{3,4})?\b")


def collapse_acronyms(text: str) -> str:
    return _ACRONYM.sub(r"\1", text)


def strip_dual_zone(text: str) -> str:
    """Remove the second time zone some offices add: "1126 AM PDT /1126 AM MST/".

    Exposed separately from convert_clock because the template parsers in
    mesh_format match on the 12-hour form and must run before conversion.
    """
    return _DUAL_ZONE.sub("", text)


def convert_clock(text: str) -> str:
    """Rewrite 12-hour NWS clock stamps as HH:MM, matching the header."""
    text = strip_dual_zone(text)

    def rewrite(match: re.Match[str]) -> str:
        hour = int(match.group(1))
        minute = match.group(2) or "00"
        meridiem = match.group(3).upper()
        if meridiem == "PM" and hour != 12:
            hour += 12
        if meridiem == "AM" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute}"

    return _CLOCK.sub(rewrite, text)


# Sentences matching any of these are dropped whole. Each is either advice so
# generic it applies to every alert of its kind (and so tells the reader
# nothing), administrative text about the product rather than the weather, or
# a restatement of something already in the header. Written to match both
# the abbreviated and the unabbreviated spelling, since the rules run on both.
SURPLUS_SENTENCES: tuple[str, ...] = (
    r"\bcan be hazardous\b",
    r"\bremain aware\b",
    r"\bmonitor (?:the )?(?:latest|later) forecasts?\b",
    r"\bcheck (?:weather\.gov|media)\b",
    r"\bfor updates\b",
    r"\bis recommended\b",
    r"\badverse health effects\b",
    r"\bis an air contaminant\b",
    r"\bwhich prompted the\b",
    r"\b(?:NWS|National Weather Service) in [A-Z][\w ]+ has issued\b",
    r"\bno longer in effect\b",
    r"\bthis includes the (?:counties|cities) of\b",
    r"\bfor the following areas\b",
    r"\bcombination of gusty winds and low (?:RH|humidity) can cause fire\b",
    r"\bheat related illnesses increase\b",
    r"\bincrease significantly\b",
    r"\bmost flood deaths occur\b",
)

# In-sentence rewrites. Applied after the surplus test so that a sentence is
# judged on what NWS wrote, and after the dictionary so that patterns can be
# written against the short forms.
SENTENCE_REWRITES: tuple[tuple[str, str], ...] = (
    # "X has issued a Y for Z through Saturday." from a non-NWS issuer (air
    # quality districts, mostly) becomes "Y thru Sat (X)." The NWS version of
    # this sentence is dropped by SURPLUS_SENTENCES instead, because the
    # issuer is implied and the product is already the header.
    (
        r"^(?!NWS)(.+?) has issued an? (.+?)(?: for [^.]+?)?( thru \w+)?\.$",
        r"\2\3 (\1).",
    ),
    (r"\bhas been allowed to expire\b", "expired"),
    (r"\bhas been cancelled\b", "cancelled"),
    (r"\bhas been upgraded to\b", "upgraded to"),
    (r"^Therefore, (?:the )?", "The "),
    (r"\bwhich is in effect\b.*", "."),
    (r"\bin effect (?:til|until) [^.]*", ""),
    (r"\bmovement was (\w+) at\b", r"moving \1 at"),
    (r"^For the [A-Z][\w ]+ on \w+, ", ""),
    (r" this (?:evening|afternoon|morning)\b", ""),
    (r"\bcaused by excessive rainfall\b", ""),
    (r"\bcontinues to be possible\b", "still possible"),
    (r"\bacross the warned area\b", ""),
    (r"\bor expected to begin shortly\b", ""),
    (r"\bmay result in\b", "may cause"),
    (r"\bwill result in\b", "will cause"),
    (r"\bin and (nr|near)\b", r"\1"),
    (r"\bdue to\b", "from"),
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    return [sentence for sentence in _SENTENCE_SPLIT.split(text.strip()) if sentence]


def compress_sentences(text: str) -> list[str]:
    """Drop surplus sentences and tighten the rest. Returns one item per sentence.

    Each surviving sentence is returned separately rather than re-joined so
    the caller can fit them to a byte budget one at a time and stop cleanly
    at a sentence boundary instead of mid-thought.
    """
    kept: list[str] = []

    for sentence in split_sentences(text):
        if any(
            re.search(pattern, sentence, re.IGNORECASE) for pattern in SURPLUS_SENTENCES
        ):
            continue

        for pattern, replacement in SENTENCE_REWRITES:
            sentence = re.sub(pattern, replacement, sentence, flags=re.IGNORECASE)

        # Rewrites that delete a phrase can leave a space before punctuation
        # or a doubled space behind.
        sentence = re.sub(r"\s+([,.;!?])", r"\1", sentence)
        sentence = re.sub(r"\s{2,}", " ", sentence).strip()

        if len(sentence) <= 3:
            continue
        if not sentence.endswith((".", "!", "?")):
            sentence += "."
        kept.append(sentence[0].upper() + sentence[1:])

    return kept


def polish(text: str, *, abbreviate: bool = True) -> list[str]:
    """The full pipeline for a piece of body text: rules, then dictionary, then rules.

    Acronyms and clock stamps are collapsed before the dictionary runs so the
    dictionary sees "8 PM" rather than something it has already altered. The
    sentence rules run last because several are written against the
    abbreviated spelling.
    """
    text = convert_clock(collapse_acronyms(text))
    if abbreviate:
        text = abbreviate_prose(text)
    return compress_sentences(text)
