"""Deciding which NWS zones to query, and remembering the answer."""

from __future__ import annotations

import json

import pytest
import requests
from conftest import RENO_LATITUDE, RENO_LONGITUDE

from wx_alert.zones import (
    ZoneResolutionError,
    determine_zones,
    load_cached_zone,
    normalize_zone,
    parse_zone_list,
    resolve_county_zone,
    save_cached_zone,
    zone_cache_path,
)

# A real /points response carries the county and the forecast zone side by
# side. Which one is taken decides whether tornado warnings ever arrive.
POINTS_RESPONSE = {
    "properties": {
        "cwa": "REV",
        "forecastZone": "https://api.weather.gov/zones/forecast/NVZ003",
        "county": "https://api.weather.gov/zones/county/NVC031",
        "fireWeatherZone": "https://api.weather.gov/zones/fire/NVZ420",
    }
}


class FakeResponse:
    def __init__(self, payload, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


class FakeSession:
    """Records every URL requested so the query itself can be asserted on."""

    def __init__(self, payload=POINTS_RESPONSE, error=None):
        self.payload = payload
        self.error = error
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if isinstance(self.error, Exception) and not isinstance(
            self.error, requests.HTTPError
        ):
            raise self.error
        return FakeResponse(self.payload, self.error)


class TestZoneCodes:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("NVC031", "NVC031"), ("nvc031", "NVC031"), ("  NVZ003  ", "NVZ003")],
    )
    def test_normalizes_valid_codes(self, raw, expected):
        assert normalize_zone(raw, "test") == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "NV031", "NVC31", "NVC0311", "N1C031", "Washoe", "NVX031"],
    )
    def test_rejects_anything_that_is_not_a_ugc_code(self, raw):
        with pytest.raises(ValueError):
            normalize_zone(raw, "test")

    def test_parses_a_comma_separated_list(self):
        assert parse_zone_list("NVC031, nvc029 ,NVC019", "test") == (
            "NVC031",
            "NVC029",
            "NVC019",
        )

    def test_drops_duplicates_but_keeps_order(self):
        assert parse_zone_list("NVC031,NVC029,nvc031", "test") == (
            "NVC031",
            "NVC029",
        )

    def test_an_empty_setting_is_an_empty_list(self):
        assert parse_zone_list("", "test") == ()
        assert parse_zone_list("  ,  ", "test") == ()

    def test_one_bad_entry_rejects_the_whole_list(self):
        # Silently dropping it would narrow the query with nothing in the log.
        with pytest.raises(ValueError):
            parse_zone_list("NVC031,not-a-zone", "test")


class TestCountyResolution:
    def test_takes_the_county_not_the_forecast_zone(self):
        # The regression that matters most. A forecast zone code is accepted
        # by the API and returns zone products only, so tornado, severe
        # thunderstorm, and flash flood warnings would silently stop arriving
        # with nothing in the log to show for it.
        session = FakeSession()
        county = resolve_county_zone(session, RENO_LATITUDE, RENO_LONGITUDE)

        assert county == "NVC031"
        assert county[2] == "C"

    def test_queries_the_points_endpoint_at_four_decimals(self):
        session = FakeSession()
        resolve_county_zone(session, RENO_LATITUDE, RENO_LONGITUDE)

        assert session.urls == [
            "https://api.weather.gov/points/39.5296,-119.8138"
        ]

    def test_a_response_without_a_county_is_an_error(self):
        session = FakeSession({"properties": {"forecastZone": "x/NVZ003"}})

        with pytest.raises(ZoneResolutionError, match="no county"):
            resolve_county_zone(session, RENO_LATITUDE, RENO_LONGITUDE)

    def test_a_response_without_properties_is_an_error(self):
        session = FakeSession({"status": 404})

        with pytest.raises(ZoneResolutionError, match="properties"):
            resolve_county_zone(session, RENO_LATITUDE, RENO_LONGITUDE)


class TestZoneCache:
    def test_round_trips_a_resolution(self, tmp_path):
        path = tmp_path / "resolved-zones.json"
        save_cached_zone(path, RENO_LATITUDE, RENO_LONGITUDE, "NVC031")

        assert load_cached_zone(path, RENO_LATITUDE, RENO_LONGITUDE) == "NVC031"

    def test_a_missing_cache_is_simply_absent(self, tmp_path):
        assert load_cached_zone(tmp_path / "nope.json", 39.0, -119.0) is None

    def test_moving_the_point_invalidates_the_cache(self, tmp_path):
        path = tmp_path / "resolved-zones.json"
        save_cached_zone(path, RENO_LATITUDE, RENO_LONGITUDE, "NVC031")

        assert load_cached_zone(path, 36.1699, -115.1398) is None

    def test_a_corrupt_cache_costs_one_api_call_not_a_failed_start(self, tmp_path):
        path = tmp_path / "resolved-zones.json"
        path.write_text("{not json", encoding="utf-8")

        assert load_cached_zone(path, RENO_LATITUDE, RENO_LONGITUDE) is None

    def test_an_unknown_version_is_ignored(self, tmp_path):
        path = tmp_path / "resolved-zones.json"
        path.write_text(json.dumps({"version": 99, "county": "NVC031"}), "utf-8")

        assert load_cached_zone(path, RENO_LATITUDE, RENO_LONGITUDE) is None

    def test_the_cache_sits_beside_the_state_file(self, tmp_path):
        assert zone_cache_path(tmp_path / "notified-alerts.json").parent == tmp_path


class TestDetermineZones:
    def test_resolves_and_caches_on_the_first_run(self, tmp_path):
        cache = tmp_path / "resolved-zones.json"
        session = FakeSession()

        zones = determine_zones(
            session, RENO_LATITUDE, RENO_LONGITUDE, (), cache
        )

        assert zones == ("NVC031",)
        assert cache.is_file()

    def test_a_second_run_does_not_call_the_api(self, tmp_path):
        cache = tmp_path / "resolved-zones.json"
        session = FakeSession()

        determine_zones(session, RENO_LATITUDE, RENO_LONGITUDE, (), cache)
        determine_zones(session, RENO_LATITUDE, RENO_LONGITUDE, (), cache)

        assert len(session.urls) == 1

    def test_configured_zones_are_added_after_the_county(self, tmp_path):
        session = FakeSession()
        zones = determine_zones(
            session,
            RENO_LATITUDE,
            RENO_LONGITUDE,
            ("NVC029", "NVC019"),
            tmp_path / "resolved-zones.json",
        )

        assert zones == ("NVC031", "NVC029", "NVC019")

    def test_the_county_is_not_repeated_when_also_configured(self, tmp_path):
        session = FakeSession()
        zones = determine_zones(
            session,
            RENO_LATITUDE,
            RENO_LONGITUDE,
            ("NVC031", "NVC029"),
            tmp_path / "resolved-zones.json",
        )

        assert zones == ("NVC031", "NVC029")

    def test_an_outage_falls_back_to_the_configured_zones(self, tmp_path):
        # An operator who named their counties explicitly should keep running
        # through an NWS /points outage.
        session = FakeSession(error=requests.ConnectionError("boom"))

        zones = determine_zones(
            session,
            RENO_LATITUDE,
            RENO_LONGITUDE,
            ("NVC031",),
            tmp_path / "resolved-zones.json",
        )

        assert zones == ("NVC031",)

    def test_an_outage_with_nothing_configured_is_fatal(self, tmp_path):
        # The alternative is querying an empty zone list, which returns an
        # empty alert list and reports "no active alerts" forever.
        session = FakeSession(error=requests.ConnectionError("boom"))

        with pytest.raises(ZoneResolutionError, match="ZONES"):
            determine_zones(
                session,
                RENO_LATITUDE,
                RENO_LONGITUDE,
                (),
                tmp_path / "resolved-zones.json",
            )

    def test_a_cached_county_survives_an_outage(self, tmp_path):
        cache = tmp_path / "resolved-zones.json"
        save_cached_zone(cache, RENO_LATITUDE, RENO_LONGITUDE, "NVC031")
        session = FakeSession(error=requests.ConnectionError("boom"))

        assert determine_zones(
            session, RENO_LATITUDE, RENO_LONGITUDE, (), cache
        ) == ("NVC031",)
