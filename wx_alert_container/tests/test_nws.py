"""Alert interpretation: classification, timestamps, identity, fingerprints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wx_alert.nws import (
    alert_age_seconds,
    alert_fingerprint,
    alert_geometry,
    alert_identity,
    alert_sent_time,
    classify_event,
    format_age,
    get_active_alerts,
    parse_nws_datetime,
)


class TestClassifyEvent:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("Flash Flood Warning", "warning"),
            ("Tornado Warning", "warning"),
            ("Red Flag Warning", "warning"),
            ("Flood Watch", "watch"),
            ("Tornado Watch", "watch"),
            ("Wind Advisory", "advisory"),
            ("Special Weather Statement", "statement"),
            ("Hazardous Weather Outlook", "outlook"),
            ("Test Message", "other"),
        ],
    )
    def test_classifies_by_product_name(self, event, expected):
        assert classify_event(event) == expected

    def test_emergency_outranks_other_keywords(self):
        # "Flash Flood Emergency" is the most severe flood product there is.
        # It must never be demoted just because it lacks the word "warning".
        assert classify_event("Flash Flood Emergency") == "warning"

    def test_is_case_insensitive(self):
        assert classify_event("FLOOD WATCH") == "watch"

    def test_watch_is_not_confused_with_warning(self, flood_watch):
        # The regression this whole classification exists to prevent: NWS
        # issues Flood Watches with severity=Severe, so a severity-only map
        # rates them as highly as an active Flash Flood Warning.
        assert flood_watch["severity"] == "Severe"
        assert classify_event(flood_watch["event"]) == "watch"


class TestParseNwsDatetime:
    def test_parses_trailing_z(self):
        parsed = parse_nws_datetime("2026-08-12T18:15:00Z")
        assert parsed == datetime(2026, 8, 12, 18, 15, tzinfo=timezone.utc)

    def test_converts_offset_to_utc(self):
        parsed = parse_nws_datetime("2026-08-12T11:15:00-07:00")
        assert parsed == datetime(2026, 8, 12, 18, 15, tzinfo=timezone.utc)

    def test_assumes_utc_when_naive(self):
        parsed = parse_nws_datetime("2026-08-12T18:15:00")
        assert parsed.tzinfo is timezone.utc

    @pytest.mark.parametrize("value", ["", "   ", None, "not a date", "2026-13-45"])
    def test_returns_none_rather_than_raising(self, value):
        # A malformed timestamp must never take down a delivery cycle.
        assert parse_nws_datetime(value) is None


class TestAlertTiming:
    def test_prefers_sent_over_fallbacks(self):
        alert = {
            "sent": "2026-08-12T10:00:00Z",
            "effective": "2026-08-12T11:00:00Z",
            "onset": "2026-08-12T12:00:00Z",
        }
        assert alert_sent_time(alert).hour == 10

    def test_falls_back_through_effective_to_onset(self):
        assert alert_sent_time({"onset": "2026-08-12T12:00:00Z"}).hour == 12
        assert alert_sent_time({}) is None

    def test_age_is_clamped_at_zero(self, now):
        # An alert effective in the future is brand new, not negative age.
        future = {"sent": (now + timedelta(hours=2)).isoformat()}
        assert alert_age_seconds(future, now) == 0

    def test_age_is_none_when_unknown(self, now):
        assert alert_age_seconds({}, now) is None

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(None, "unknown"), (9, "9s"), (125, "2m05s"), (8400, "2h20m")],
    )
    def test_format_age(self, seconds, expected):
        assert format_age(seconds) == expected


class TestIdentityAndFingerprint:
    def test_identity_uses_the_nws_id(self, flood_watch):
        assert alert_identity(flood_watch) == flood_watch["id"]

    def test_identity_is_stable_when_content_changes(self, flood_watch):
        # Identity answers "same product?", not "same content?".
        edited = dict(flood_watch, description="entirely different text")
        assert alert_identity(edited) == alert_identity(flood_watch)

    def test_identity_falls_back_to_a_bounded_hash(self):
        generated = alert_identity({"event": "X", "sent": "2026-01-01T00:00:00Z"})
        assert generated.startswith("generated:")
        # Bounded length matters: this is used as a JSON object key.
        assert len(generated) == len("generated:") + 64

    def test_fallback_distinguishes_different_alerts(self):
        first = alert_identity({"event": "A", "sent": "2026-01-01T00:00:00Z"})
        second = alert_identity({"event": "B", "sent": "2026-01-01T00:00:00Z"})
        assert first != second

    def test_fingerprint_changes_when_content_changes(self, flood_watch):
        # NWS reissues products under the original ID, so identity alone
        # would silently swallow updates.
        edited = dict(flood_watch, description="updated text")
        assert alert_fingerprint(edited) != alert_fingerprint(flood_watch)

    def test_fingerprint_ignores_irrelevant_fields(self, flood_watch):
        # Fields not carried in any message must not force a re-send.
        noisy = dict(flood_watch, geocode={"UGC": ["NVZ001"]})
        assert alert_fingerprint(noisy) == alert_fingerprint(flood_watch)

    def test_fingerprint_is_stable_across_key_order(self, flood_watch):
        reordered = dict(reversed(list(flood_watch.items())))
        assert alert_fingerprint(reordered) == alert_fingerprint(flood_watch)


class RecordingSession:
    """Captures the outgoing query so the request itself can be asserted on."""

    def __init__(self, features):
        self.features = features
        self.params = None

    def get(self, url, params=None, **kwargs):
        self.params = params
        session = self

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"features": session.features}

        return Response()


class TestActiveAlertQuery:
    def test_queries_by_zone_rather_than_point(self):
        # A point query intersects the coordinate with each alert's polygon,
        # which excludes every storm-based warning that does not happen to
        # cover that exact spot. This is the bug the 1.2.0 work fixes.
        session = RecordingSession([])
        get_active_alerts(session, ["NVC031"])

        assert session.params == {"zone": "NVC031"}
        assert "point" not in session.params

    def test_sends_a_county_code_not_a_forecast_zone(self):
        # Regression guard. NVZ003 is accepted by the API and returns only
        # zone products, so tornado warnings would stop arriving silently.
        session = RecordingSession([])
        get_active_alerts(session, ["NVC031", "NVC029"])

        zones = session.params["zone"].split(",")
        assert any(zone[2] == "C" for zone in zones)

    def test_joins_several_zones_into_one_request(self):
        session = RecordingSession([])
        get_active_alerts(session, ["NVC031", "NVC029", "NVC019"])

        assert session.params == {"zone": "NVC031,NVC029,NVC019"}

    def test_an_empty_zone_list_is_refused(self):
        # Querying nothing returns nothing, which would look exactly like
        # quiet weather for as long as the container ran.
        with pytest.raises(ValueError, match="at least one"):
            get_active_alerts(RecordingSession([]), [])

    def test_geometry_is_carried_alongside_the_properties(self):
        geometry = {"type": "Polygon", "coordinates": [[[-119.0, 39.0]]]}
        session = RecordingSession(
            [{"properties": {"event": "Tornado Warning"}, "geometry": geometry}]
        )

        alerts = get_active_alerts(session, ["NVC031"])

        assert alert_geometry(alerts[0]) == geometry

    def test_a_zone_product_reports_no_geometry(self):
        session = RecordingSession(
            [{"properties": {"event": "Flood Watch"}, "geometry": None}]
        )

        alerts = get_active_alerts(session, ["NVC031"])

        assert alert_geometry(alerts[0]) is None

    def test_malformed_features_are_skipped(self):
        session = RecordingSession(
            ["not a dict", {"properties": None}, {"properties": {"event": "X"}}]
        )

        assert len(get_active_alerts(session, ["NVC031"])) == 1

    def test_a_non_list_features_value_is_rejected(self):
        with pytest.raises(ValueError, match="features"):
            get_active_alerts(RecordingSession({"unexpected": True}), ["NVC031"])
