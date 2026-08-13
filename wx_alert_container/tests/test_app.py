"""The polling loop's coordination of transports and state."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest
from conftest import RENO_LATITUDE, RENO_LONGITUDE

from wx_alert.app import (
    CONSOLE_TRANSPORT_NAME,
    deliver_alert,
    filter_by_proximity,
    process_alerts,
    settlement_names,
)
from wx_alert.health import evaluate, write_heartbeat
from wx_alert.state import AlertState
from wx_alert.transports.base import DeliveryContext, DeliveryResult


class RecordingTransport:
    """A transport whose outcome the test dictates."""

    def __init__(self, name, result=DeliveryResult.SENT, detail="ok"):
        self.name = name
        self.result = result
        self.detail = detail
        self.delivered = []

    def start(self):
        pass

    def selftest(self):
        pass

    def deliver(self, alert, context):
        self.delivered.append(alert)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, self.detail

    def close(self):
        pass


@pytest.fixture
def args(tmp_path):
    return argparse.Namespace(
        verbose=False,
        delay=1,
        state_file=tmp_path / "state.json",
        state_retention_days=14,
    )


@pytest.fixture
def context(now):
    return DeliveryContext(first_cycle=False, now=now)


def keep(alerts):
    return [alert["event"] for alert in alerts]


class TestProximityFilter:
    """Fetching by county is broad; this is what restores local relevance."""

    def test_keeps_a_nearby_warning(self, tornado_warning):
        # The alert this whole change exists to deliver: about 15 km east of
        # the monitored point, outside the polygon, well inside mesh range.
        kept = filter_by_proximity(
            [tornado_warning], RENO_LATITUDE, RENO_LONGITUDE, 50
        )
        assert keep(kept) == ["Tornado Warning"]

    def test_drops_the_same_warning_under_a_tight_radius(self, tornado_warning):
        kept = filter_by_proximity(
            [tornado_warning], RENO_LATITUDE, RENO_LONGITUDE, 5
        )
        assert kept == []

    def test_keeps_a_zone_product_that_has_no_polygon(self, flood_watch):
        # NWS already scoped it to the queried county. There is no geometry
        # to be far away, and dropping it would silence every watch.
        assert "geometry" not in flood_watch

        kept = filter_by_proximity(
            [flood_watch], RENO_LATITUDE, RENO_LONGITUDE, 1
        )
        assert keep(kept) == ["Flood Watch"]

    def test_a_zero_radius_disables_the_test(self, tornado_warning):
        kept = filter_by_proximity(
            [tornado_warning], RENO_LATITUDE, RENO_LONGITUDE, 0
        )
        assert keep(kept) == ["Tornado Warning"]

    def test_a_far_county_is_excluded(self, tornado_warning):
        # Washoe County runs 315 km north to the Oregon border. Without this,
        # a county query would put warnings from Gerlach on the radio.
        gerlach_latitude, gerlach_longitude = 40.6555, -119.3560

        kept = filter_by_proximity(
            [tornado_warning], gerlach_latitude, gerlach_longitude, 50
        )
        assert kept == []

    def test_preserves_order_and_identity(self, tornado_warning, flood_watch):
        alerts = [flood_watch, tornado_warning]
        kept = filter_by_proximity(alerts, RENO_LATITUDE, RENO_LONGITUDE, 50)

        assert keep(kept) == ["Flood Watch", "Tornado Warning"]
        assert kept[1] is tornado_warning


class TestSettlementNames:
    def test_uses_transport_names(self):
        transports = [RecordingTransport("ntfy"), RecordingTransport("meshcore")]
        assert settlement_names(transports) == ["ntfy", "meshcore"]

    def test_falls_back_to_the_console(self):
        # Without this, the outstanding check would run any() over an empty
        # sequence, match nothing, and print no alerts at all.
        assert settlement_names([]) == [CONSOLE_TRANSPORT_NAME]


class TestDeliverAlert:
    def test_records_a_success(self, flood_watch, context):
        state = AlertState()
        transport = RecordingTransport("ntfy")

        assert deliver_alert(flood_watch, [transport], state, context) == 0
        assert state.is_settled(flood_watch, "ntfy")

    def test_records_a_skip_as_settled(self, flood_watch, context):
        # A skip is a decision, so the alert must not be reconsidered.
        state = AlertState()
        transport = RecordingTransport("meshcore", DeliveryResult.SKIPPED, "filtered")

        deliver_alert(flood_watch, [transport], state, context)
        assert state.is_settled(flood_watch, "meshcore")

    def test_leaves_a_failure_unsettled(self, flood_watch, context):
        state = AlertState()
        transport = RecordingTransport("ntfy", DeliveryResult.FAILED, "boom")

        assert deliver_alert(flood_watch, [transport], state, context) == 1
        assert not state.is_settled(flood_watch, "ntfy")

    def test_one_transport_failing_does_not_affect_another(
        self, flood_watch, context
    ):
        # The whole reason state is per transport.
        state = AlertState()
        good = RecordingTransport("ntfy")
        bad = RecordingTransport("meshcore", DeliveryResult.FAILED, "no radio")

        assert deliver_alert(flood_watch, [good, bad], state, context) == 1
        assert state.is_settled(flood_watch, "ntfy")
        assert not state.is_settled(flood_watch, "meshcore")

    def test_a_raising_transport_is_contained(self, flood_watch, context):
        # A transport bug must not take down the poller.
        state = AlertState()
        broken = RecordingTransport("meshcore", RuntimeError("unexpected"))
        good = RecordingTransport("ntfy")

        assert deliver_alert(flood_watch, [broken, good], state, context) == 1
        assert state.is_settled(flood_watch, "ntfy")

    def test_reports_which_transports_failed(self, flood_watch, context):
        state = AlertState()
        bad = RecordingTransport("meshcore", DeliveryResult.FAILED, "no radio")
        failed: set[str] = set()

        deliver_alert(flood_watch, [bad], state, context, failed)
        assert failed == {"meshcore"}

    def test_skips_an_already_settled_alert(self, flood_watch, context):
        state = AlertState()
        state.record(flood_watch, "ntfy", "delivered")
        transport = RecordingTransport("ntfy")

        deliver_alert(flood_watch, [transport], state, context)
        assert transport.delivered == []

    def test_console_only_runs_are_recorded(self, flood_watch, context):
        state = AlertState()
        deliver_alert(flood_watch, [], state, context)
        assert state.is_settled(flood_watch, CONSOLE_TRANSPORT_NAME)


class TestNtfyRemainsReliableWhileTheRadioFails:
    """ntfy is the dependable path; the mesh is best effort.

    A channel broadcast is unacknowledged and can vanish, so the radio is
    expected to fail and retry. None of that may degrade ntfy, which is what
    makes it a usable fallback.
    """

    def test_a_retrying_radio_does_not_resend_on_ntfy(self, flood_watch, context):
        # The failure that matters: retrying the radio re-offers the alert, and
        # if settlement were global rather than per transport the operator
        # would get a duplicate push notification on every retry.
        state = AlertState()
        ntfy = RecordingTransport("ntfy")
        mesh = RecordingTransport("meshcore", DeliveryResult.FAILED, "no radio")

        for _ in range(3):
            deliver_alert(flood_watch, [ntfy, mesh], state, context)

        assert len(ntfy.delivered) == 1
        assert len(mesh.delivered) == 3
        assert state.is_settled(flood_watch, "ntfy")
        assert not state.is_settled(flood_watch, "meshcore")

    def test_ntfy_still_delivers_when_the_radio_raises(self, flood_watch, context):
        state = AlertState()
        mesh = RecordingTransport("meshcore", RuntimeError("serial port vanished"))
        ntfy = RecordingTransport("ntfy")

        # Radio first, so an unhandled exception would abort before ntfy ran.
        deliver_alert(flood_watch, [mesh, ntfy], state, context)

        assert len(ntfy.delivered) == 1
        assert state.is_settled(flood_watch, "ntfy")

    def test_a_radio_that_skips_does_not_suppress_ntfy(self, flood_watch, context):
        # The radio filters hard by class and severity, so most alerts never
        # reach it. Those must still go out over ntfy.
        state = AlertState()
        mesh = RecordingTransport("meshcore", DeliveryResult.SKIPPED, "filtered")
        ntfy = RecordingTransport("ntfy")

        assert deliver_alert(flood_watch, [mesh, ntfy], state, context) == 0
        assert len(ntfy.delivered) == 1


class TestProcessAlerts:
    def test_persists_after_each_alert(self, args, flood_watch, context, capsys):
        # A crash between two deliveries would otherwise replay the ones
        # already sent.
        state = AlertState()
        process_alerts([flood_watch], [RecordingTransport("ntfy")], state,
                       args, context)
        assert args.state_file.is_file()

    def test_prints_every_alert(self, args, flood_watch, context, capsys):
        process_alerts([flood_watch], [], AlertState(), args, context)
        assert "Flood Watch" in capsys.readouterr().out


class TestHealth:
    def test_a_fresh_heartbeat_is_healthy(self, tmp_path):
        path = tmp_path / "heartbeat.json"
        write_heartbeat(path, check_interval=300, cycle=1,
                        consecutive_failures={"ntfy": 0})

        import json

        healthy, _ = evaluate(
            json.loads(path.read_text()), datetime.now(timezone.utc)
        )
        assert healthy

    def test_a_stale_heartbeat_is_unhealthy(self, now):
        payload = {
            "updated_at": (now - timedelta(hours=2)).isoformat(),
            "check_interval": 300,
            "consecutive_failures": {},
        }
        healthy, detail = evaluate(payload, now)
        assert not healthy
        assert "exceeds" in detail

    def test_a_failing_transport_is_unhealthy(self, now):
        payload = {
            "updated_at": now.isoformat(),
            "check_interval": 300,
            "consecutive_failures": {"meshcore": 3},
        }
        healthy, detail = evaluate(payload, now)
        assert not healthy
        assert "meshcore=3" in detail

    def test_an_unreadable_timestamp_is_unhealthy(self, now):
        healthy, _ = evaluate({"updated_at": "nonsense"}, now)
        assert not healthy

    def test_writing_a_heartbeat_never_raises(self, tmp_path):
        # A heartbeat failure must not take down an otherwise working poller.
        unwritable = tmp_path / "file.txt"
        unwritable.write_text("not a directory", encoding="utf-8")
        write_heartbeat(unwritable / "heartbeat.json", check_interval=300,
                        cycle=1, consecutive_failures={})
