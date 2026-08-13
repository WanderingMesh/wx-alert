"""Resolving which NWS zones to query, and remembering the answer.

The alert query has to name zones rather than a coordinate. A point query asks
the API to intersect the coordinate with each alert's geometry, which silently
excludes every storm-based warning whose polygon does not happen to cover that
exact spot -- the failure this module exists to prevent. A county query returns
both polygon warnings and zone products, because the API maps zone-based alerts
onto counties internally.

The distinction between the two UGC forms is the whole game:

    NVC031   county code, returns county-based *and* zone-based alerts
    NVZ003   forecast zone code, returns zone-based alerts only

A forecast zone code looks equally plausible in a config file and fails by
returning fewer alerts, with no error and nothing in the log. Tornado warnings
simply stop arriving. Everything here is arranged so that the county code is
derived automatically rather than typed by hand.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import requests

from .nws import NWS_HEADERS

LOGGER = logging.getLogger("wx-alert")

NWS_POINTS_URL = "https://api.weather.gov/points"

ZONE_CACHE_FILENAME = "resolved-zones.json"
ZONE_CACHE_VERSION = 1

# Two-letter state or marine prefix, a type letter, and three digits.
ZONE_PATTERN = re.compile(r"^[A-Z]{2}[CZ][0-9]{3}$")

# Coordinates are compared at the precision the alert query itself uses, so a
# cached resolution is reused unless the operator has really moved.
COORDINATE_PRECISION = 4


class ZoneResolutionError(RuntimeError):
    """Raised when the zones to query cannot be determined."""


def zone_cache_path(state_file: Path) -> Path:
    """Place the zone cache beside the state file, in the same writable volume."""
    return state_file.parent / ZONE_CACHE_FILENAME


def normalize_zone(value: str, source: str) -> str:
    """Validate and upper-case a single UGC zone code."""
    code = value.strip().upper()

    if not ZONE_PATTERN.match(code):
        raise ValueError(
            f"{source}: {value!r} is not a UGC zone code such as NVC031 "
            "(county) or NVZ003 (forecast zone)"
        )

    return code


def parse_zone_list(raw: str, source: str) -> tuple[str, ...]:
    """Parse a comma-separated zone list, preserving order and dropping repeats."""
    zones: list[str] = []

    for part in raw.split(","):
        if not part.strip():
            continue
        code = normalize_zone(part, source)
        if code not in zones:
            zones.append(code)

    return tuple(zones)


def _rounded(latitude: float, longitude: float) -> tuple[float, float]:
    return (
        round(latitude, COORDINATE_PRECISION),
        round(longitude, COORDINATE_PRECISION),
    )


def resolve_county_zone(
    session: requests.Session,
    latitude: float,
    longitude: float,
) -> str:
    """Ask NWS which county contains a coordinate, returning its UGC code.

    The /points endpoint reports the county, forecast zone, and fire weather
    zone containing a coordinate. Only the county is taken, because it is the
    one form that returns polygon warnings.
    """
    url = f"{NWS_POINTS_URL}/{latitude:.4f},{longitude:.4f}"

    response = session.get(url, headers=NWS_HEADERS, timeout=20)
    response.raise_for_status()

    document = response.json()
    properties = document.get("properties")

    if not isinstance(properties, dict):
        raise ZoneResolutionError(
            f"NWS /points response for {latitude:.4f},{longitude:.4f} "
            "contained no properties object"
        )

    county_url = properties.get("county")

    if not isinstance(county_url, str) or not county_url.strip():
        raise ZoneResolutionError(
            f"NWS reports no county for {latitude:.4f},{longitude:.4f}; "
            "the coordinate may be offshore or outside NWS coverage"
        )

    code = county_url.rstrip("/").rsplit("/", 1)[-1]

    try:
        return normalize_zone(code, "NWS /points county")
    except ValueError as exc:
        raise ZoneResolutionError(str(exc)) from exc


def load_cached_zone(
    path: Path,
    latitude: float,
    longitude: float,
) -> str | None:
    """Return a previously resolved county for these coordinates, if any.

    Any problem reading the cache returns None rather than raising: a corrupt
    cache should cost one API call, not a failed start.
    """
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable zone cache file=%s error=%s", path, exc)
        return None

    if not isinstance(raw, dict) or raw.get("version") != ZONE_CACHE_VERSION:
        return None

    cached_point = (raw.get("latitude"), raw.get("longitude"))
    if cached_point != _rounded(latitude, longitude):
        LOGGER.info(
            "Monitored point has changed since the zone cache was written; "
            "re-resolving file=%s",
            path,
        )
        return None

    county = raw.get("county")
    if not isinstance(county, str):
        return None

    try:
        return normalize_zone(county, "zone cache")
    except ValueError:
        return None


def save_cached_zone(
    path: Path,
    latitude: float,
    longitude: float,
    county: str,
) -> None:
    """Persist a resolved county. Failure is logged, never fatal."""
    cached_latitude, cached_longitude = _rounded(latitude, longitude)
    payload = {
        "version": ZONE_CACHE_VERSION,
        "latitude": cached_latitude,
        "longitude": cached_longitude,
        "county": county,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        # Zone boundaries do not move, so the only cost is one API call per
        # start. Not worth refusing to run over.
        LOGGER.warning("Could not write zone cache file=%s error=%s", path, exc)


def determine_zones(
    session: requests.Session,
    latitude: float,
    longitude: float,
    extra_zones: Sequence[str],
    cache_path: Path,
) -> tuple[str, ...]:
    """Decide the full set of zones to query, resolving the county as needed.

    Order of preference is cache, then the API, then whatever the operator
    configured explicitly. Falling back to an explicit list keeps a configured
    deployment running through an NWS outage; having neither is fatal, because
    an empty zone list would query nothing at all and report "no active
    alerts" indefinitely while the weather did as it pleased.
    """
    county = load_cached_zone(cache_path, latitude, longitude)

    if county:
        LOGGER.info("Using cached county zone=%s file=%s", county, cache_path)
    else:
        try:
            county = resolve_county_zone(session, latitude, longitude)
        except (requests.RequestException, ValueError, ZoneResolutionError) as exc:
            if not extra_zones:
                raise ZoneResolutionError(
                    "could not determine the county for "
                    f"{latitude:.4f},{longitude:.4f} ({exc}); set [weather] "
                    "ZONES explicitly to run without this lookup"
                ) from exc

            LOGGER.warning(
                "County resolution failed; continuing with the configured "
                "zones only zones=%s error=%s",
                ",".join(extra_zones),
                exc,
            )
            return tuple(extra_zones)

        LOGGER.info(
            "Resolved monitored point to county zone=%s latitude=%.4f "
            "longitude=%.4f",
            county,
            latitude,
            longitude,
        )
        save_cached_zone(cache_path, latitude, longitude, county)

    zones = [county]
    zones.extend(zone for zone in extra_zones if zone not in zones)

    if not any(zone[2] == "C" for zone in zones):
        # Reachable only if a future change stops prepending the county.
        LOGGER.warning(
            "No county zone in the query set; storm-based warnings such as "
            "Tornado Warnings will not be returned zones=%s",
            ",".join(zones),
        )

    return tuple(zones)
