"""Shared fixtures.

Tests never touch the network or a serial port. The NWS fixture is a real
captured response so the parsing tests exercise the field shapes NWS actually
emits, including quirks like an empty instruction and a description whose
sections are marked with "* WHAT...".
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def now() -> datetime:
    """A fixed clock, so age and rate-limit behavior is deterministic."""
    return datetime(2026, 8, 12, 18, 30, tzinfo=timezone.utc)


@pytest.fixture
def flood_watch() -> dict[str, Any]:
    """A real NWS Flood Watch captured from the live API."""
    alerts = json.loads(
        (FIXTURE_DIR / "nws_reno_flood_watch.json").read_text(encoding="utf-8")
    )
    return alerts[0]


@pytest.fixture
def tornado_warning() -> dict[str, Any]:
    """The real Tornado Warning that exposed the point-query bug.

    Issued by NWS Reno on 2026-08-13 for Lyon, Storey, and Washoe counties.
    Its polygon lies east of Reno, so a point query at the monitored
    coordinate returned nothing while the warning was active and the radio
    stayed silent. Captured in the flattened form get_active_alerts produces,
    with the GeoJSON geometry folded in beside the properties.
    """
    alerts = json.loads(
        (FIXTURE_DIR / "nws_reno_tornado_warning.json").read_text(encoding="utf-8")
    )
    return alerts[0]


# The monitored point in the deployment that missed the warning above.
RENO_LATITUDE = 39.5296
RENO_LONGITUDE = -119.8138


@pytest.fixture
def fresh_warning(now: datetime) -> dict[str, Any]:
    """A warning issued a minute ago, which every policy should let through."""
    issued = now - timedelta(minutes=1)
    return {
        "id": "urn:oid:test.warning",
        "event": "Flash Flood Warning",
        "severity": "Severe",
        "urgency": "Immediate",
        "certainty": "Likely",
        "messageType": "Alert",
        "areaDesc": "Washoe County",
        "sent": issued.isoformat(),
        "effective": issued.isoformat(),
        "expires": (now + timedelta(hours=3)).isoformat(),
        "ends": (now + timedelta(hours=3)).isoformat(),
        "headline": "Flash Flood Warning issued for Washoe County",
        "description": "* WHAT...Flash flooding is occurring.",
        "instruction": "Move to higher ground now.",
    }
