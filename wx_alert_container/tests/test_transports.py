"""Transport behavior, exercised without a network or a serial port.

The MeshCore tests substitute a fake companion radio for the real one. This
covers everything except the serial layer itself: the async bridge, the
startup handshake, budget derivation from the node name, channel validation,
the gate ordering, and reconnection after a failure.
"""

from __future__ import annotations

import argparse
from datetime import timedelta

import pytest

from wx_alert.app import prioritize, process_alerts
from wx_alert.policy import StartupPolicy
from wx_alert.ratelimit import RateLimiter, RelevanceFilter
from wx_alert.state import AlertState
from wx_alert.transports.base import DeliveryContext, DeliveryResult, TransportError


# --------------------------------------------------------------------------
# Fake radio
# --------------------------------------------------------------------------


class FakeEvent:
    def __init__(self, type_, payload=None):
        self.type = type_
        self.payload = payload or {}


class FakeCommands:
    def __init__(self, radio):
        self._radio = radio

    async def get_channel(self, index):
        from meshcore import EventType

        if index not in self._radio.channels:
            return FakeEvent(EventType.ERROR, {"reason": "no such channel"})
        return FakeEvent(
            EventType.CHANNEL_INFO,
            {"channel_idx": index, "channel_name": self._radio.channels[index]},
        )

    async def send_chan_msg(self, channel, message, timestamp=None):
        from meshcore import EventType

        if self._radio.reject_next:
            self._radio.reject_next = False
            return FakeEvent(EventType.ERROR, {"reason": "tx queue full"})

        self._radio.sent.append((channel, message))
        return FakeEvent(EventType.OK)


class FakeRadio:
    def __init__(self, name="WX-Reno"):
        self.self_info = {"name": name}
        self.commands = FakeCommands(self)
        self.channels = {0: "Public"}
        self.sent = []
        self.reject_next = False
        self.disconnect_count = 0

    async def disconnect(self):
        self.disconnect_count += 1


@pytest.fixture
def radio():
    return FakeRadio()


class FakeClock:
    """A clock the transport reads and a wait the test does not sit through.

    The transport meters airtime against real time, so without this a test of
    a 60 second transmission gap would take 60 seconds. Waiting advances the
    clock instead of blocking, which is also the only way to assert that the
    transport waited rather than gave up.
    """

    def __init__(self, start):
        self.now = start
        self.waits: list[float] = []
        self.interrupted = False

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> bool:
        """Stand in for STOP_EVENT.wait, returning True when interrupted."""
        self.waits.append(seconds)
        if self.interrupted:
            return True
        self.advance(seconds)
        return False


@pytest.fixture
def clock(now):
    return FakeClock(now)


@pytest.fixture
def make_transport(radio, clock, monkeypatch):
    """Build a MeshCoreTransport wired to the fake radio."""
    import types

    import meshcore as meshcore_pkg

    from wx_alert.transports.meshcore import MeshCoreTransport

    async def fake_create_serial(port, baud=115200, **kwargs):
        # The real create_serial returns None rather than raising when the
        # device does not answer, which the transport relies on.
        return None if port == "/dev/absent" else radio

    monkeypatch.setattr(
        meshcore_pkg,
        "MeshCore",
        types.SimpleNamespace(create_serial=fake_create_serial),
        raising=False,
    )

    created = []

    def build(**overrides):
        settings = {
            "port": "/dev/fake",
            "baud": 115200,
            "channel_index": 0,
            "relevance": RelevanceFilter("warning", "severe"),
            "rate_limiter": RateLimiter(0, 0),
            "startup_policy": StartupPolicy(0, always_notify_warnings=False),
            "clock": clock,
            "sleep": clock.sleep,
        }
        settings.update(overrides)
        transport = MeshCoreTransport(**settings)
        created.append(transport)
        return transport

    yield build

    for transport in created:
        transport.close()


# --------------------------------------------------------------------------
# ntfy
# --------------------------------------------------------------------------


class TestNtfyTransport:
    @pytest.fixture
    def transport(self):
        import requests

        from wx_alert.transports.ntfy import NtfyTransport

        return NtfyTransport(
            requests.Session(),
            server="https://ntfy.example",
            topic="test-topic",
            token=None,
            tags="weather",
            priority="auto",
            verbose=False,
            startup_policy=StartupPolicy(900, always_notify_warnings=True),
        )

    def test_reports_a_request_failure_rather_than_raising(
        self, transport, fresh_warning, now, monkeypatch
    ):
        # A delivery failure must leave the alert eligible for retry, not
        # take down the poll loop.
        import requests

        def explode(*args, **kwargs):
            raise requests.ConnectionError("network unreachable")

        monkeypatch.setattr("wx_alert.transports.ntfy.publish_ntfy_message", explode)

        result, detail = transport.deliver(
            fresh_warning, DeliveryContext(False, now)
        )
        assert result is DeliveryResult.FAILED
        assert "unreachable" in detail

    def test_applies_the_startup_policy(self, transport, flood_watch, now):
        result, detail = transport.deliver(flood_watch, DeliveryContext(True, now))
        assert result is DeliveryResult.SKIPPED
        assert "startup-stale" in detail

    def test_builds_a_url_safe_topic(self):
        from wx_alert.transports.ntfy import build_ntfy_url

        assert (
            build_ntfy_url("https://ntfy.sh/", "my topic")
            == "https://ntfy.sh/my%20topic"
        )


# --------------------------------------------------------------------------
# MeshCore
# --------------------------------------------------------------------------


class TestMeshCoreStartup:
    def test_derives_the_budget_from_the_node_name(self, make_transport, radio):
        from wx_alert.mesh_format import mesh_text_budget

        transport = make_transport()
        transport.start()
        assert transport._budget == mesh_text_budget("WX-Reno")

    def test_a_longer_node_name_reduces_the_budget(self, make_transport, radio):
        radio.self_info = {"name": "X" * 32}
        transport = make_transport()
        transport.start()
        assert transport._budget == pytest.approx(116, abs=2)

    def test_assumes_the_longest_name_when_none_is_reported(
        self, make_transport, radio
    ):
        # Guessing short would silently produce oversized packets.
        radio.self_info = {}
        transport = make_transport()
        transport.start()
        assert transport._budget <= 120

    def test_an_unresponsive_device_fails_at_startup(self, make_transport):
        transport = make_transport(port="/dev/absent")
        with pytest.raises(TransportError, match="no response"):
            transport.start()

    def test_a_missing_channel_fails_at_startup(self, make_transport):
        # Otherwise a typo would quietly broadcast into channel 0 for a week.
        transport = make_transport(channel_index=7)
        with pytest.raises(TransportError, match="channel index 7"):
            transport.start()

    def test_dry_run_never_opens_the_port(self, make_transport, radio):
        transport = make_transport(port="/dev/absent", dry_run=True)
        transport.start()
        assert transport._meshcore is None


class TestMeshCoreDelivery:
    def test_transmits_a_qualifying_alert(self, make_transport, radio,
                                          fresh_warning, now):
        transport = make_transport()
        transport.start()

        result, detail = transport.deliver(
            fresh_warning, DeliveryContext(False, now)
        )

        assert result is DeliveryResult.SENT
        assert "transmitted" in detail
        channel, message = radio.sent[-1]
        assert channel == 0
        assert "Flash Flood Warning" in message

    def test_the_message_never_exceeds_the_budget(self, make_transport, radio,
                                                  fresh_warning, now):
        transport = make_transport()
        transport.start()
        transport.deliver(fresh_warning, DeliveryContext(False, now))

        _channel, message = radio.sent[-1]
        assert len(message.encode("utf-8")) <= transport._budget

    def test_declines_an_irrelevant_alert(self, make_transport, radio,
                                          flood_watch, now):
        transport = make_transport()
        transport.start()

        result, detail = transport.deliver(flood_watch, DeliveryContext(False, now))

        assert result is DeliveryResult.SKIPPED
        assert "filtered" in detail
        assert radio.sent == []

    def test_filtering_happens_before_the_rate_limiter(
        self, make_transport, radio, flood_watch, fresh_warning, now
    ):
        # An irrelevant alert must not consume airtime budget.
        limiter = RateLimiter(min_interval_seconds=0, max_per_hour=1)
        transport = make_transport(
            relevance=RelevanceFilter("warning", "severe"),
            rate_limiter=limiter,
        )
        transport.start()

        transport.deliver(flood_watch, DeliveryContext(False, now))
        result, _ = transport.deliver(fresh_warning, DeliveryContext(False, now))

        assert result is DeliveryResult.SENT

    def test_waits_out_spacing_so_a_whole_batch_goes_out(
        self, make_transport, radio, fresh_warning, now, clock
    ):
        # A county query returns several alerts at once. Refusing every one
        # after the first would put a single alert per poll interval on the
        # air, and the rest were being recorded as handled and lost outright.
        transport = make_transport(
            rate_limiter=RateLimiter(min_interval_seconds=30, max_per_hour=0)
        )
        transport.start()

        first, _ = transport.deliver(fresh_warning, DeliveryContext(False, now))
        second, _ = transport.deliver(
            dict(fresh_warning, id="urn:oid:second"),
            DeliveryContext(False, now),
        )

        assert first is DeliveryResult.SENT
        assert second is DeliveryResult.SENT
        assert len(radio.sent) == 2
        # Held for the configured gap rather than transmitting back to back.
        assert clock.waits == [30]

    def test_the_cycle_timestamp_does_not_govern_spacing(
        self, make_transport, radio, fresh_warning, now
    ):
        # DeliveryContext.now is stamped once per cycle, so every alert in a
        # batch carries the same value. Metering against it reads zero elapsed
        # time between transmissions and refuses everything after the first.
        transport = make_transport(
            rate_limiter=RateLimiter(min_interval_seconds=1, max_per_hour=0)
        )
        transport.start()

        for index in range(4):
            result, _ = transport.deliver(
                dict(fresh_warning, id=f"urn:oid:{index}"),
                DeliveryContext(False, now),
            )
            assert result is DeliveryResult.SENT

        assert len(radio.sent) == 4

    def test_defers_rather_than_holding_the_cycle_open_too_long(
        self, make_transport, radio, fresh_warning, now, clock
    ):
        # Waiting is right for a 30 second gap and wrong for a ten minute one:
        # it would stall the next NWS query and every alert behind this one.
        transport = make_transport(
            rate_limiter=RateLimiter(min_interval_seconds=600, max_per_hour=0)
        )
        transport.start()

        transport.deliver(fresh_warning, DeliveryContext(False, now))
        result, detail = transport.deliver(
            dict(fresh_warning, id="urn:oid:second"),
            DeliveryContext(False, now),
        )

        assert result is DeliveryResult.DEFERRED
        assert "rate-limit" in detail
        assert clock.waits == []
        assert len(radio.sent) == 1

    def test_the_hourly_cap_defers_instead_of_waiting(
        self, make_transport, radio, fresh_warning, now, clock
    ):
        # Capacity can be nearly an hour away, which is far too long to hold a
        # poll cycle open for. The alert is offered again next cycle instead.
        transport = make_transport(
            rate_limiter=RateLimiter(min_interval_seconds=0, max_per_hour=1)
        )
        transport.start()

        transport.deliver(fresh_warning, DeliveryContext(False, now))
        result, detail = transport.deliver(
            dict(fresh_warning, id="urn:oid:second"),
            DeliveryContext(False, now),
        )

        assert result is DeliveryResult.DEFERRED
        assert "hourly cap" in detail
        assert clock.waits == []

    def test_a_shutdown_during_a_spacing_wait_defers(
        self, make_transport, radio, fresh_warning, now, clock
    ):
        # The alert stays outstanding for whoever starts next rather than being
        # written off because the container was asked to stop mid-gap.
        transport = make_transport(
            rate_limiter=RateLimiter(min_interval_seconds=30, max_per_hour=0)
        )
        transport.start()

        transport.deliver(fresh_warning, DeliveryContext(False, now))
        clock.interrupted = True

        result, detail = transport.deliver(
            dict(fresh_warning, id="urn:oid:second"),
            DeliveryContext(False, now),
        )

        assert result is DeliveryResult.DEFERRED
        assert "shutdown" in detail
        assert len(radio.sent) == 1

    def test_a_refusal_does_not_consume_airtime_budget(
        self, make_transport, radio, fresh_warning, now, clock
    ):
        limiter = RateLimiter(min_interval_seconds=0, max_per_hour=1)
        transport = make_transport(rate_limiter=limiter)
        transport.start()

        transport.deliver(fresh_warning, DeliveryContext(False, now))
        transport.deliver(fresh_warning, DeliveryContext(False, now))

        # Capacity returns an hour after the transmission that actually
        # happened, not an hour after the attempt that was refused.
        assert limiter.seconds_until_ready(now + timedelta(hours=1)) == 0

    def test_dry_run_records_the_send_without_transmitting(
        self, make_transport, radio, fresh_warning, now
    ):
        transport = make_transport(port="/dev/absent", dry_run=True)
        transport.start()

        result, detail = transport.deliver(
            fresh_warning, DeliveryContext(False, now)
        )

        assert result is DeliveryResult.SENT
        assert "dry-run" in detail
        assert radio.sent == []


class TestMeshCoreStartupStaleness:
    """What the radio does with alerts that were already active at boot.

    The gate exists so a restart does not replay a backlog onto a shared
    channel. Suppression here is terminal, so every one of these decisions is
    final and there is no later cycle to correct it.
    """

    @pytest.fixture
    def policy(self):
        # As __main__ wires it: no unconditional warning bypass, and a warning
        # earns its airtime by still having time left to run.
        return StartupPolicy(
            900,
            always_notify_warnings=False,
            warning_min_remaining_seconds=900,
        )

    @pytest.fixture
    def old_warning(self, fresh_warning, now):
        """A warning issued four hours ago and in force for three more."""
        issued = now - timedelta(hours=4)
        return dict(fresh_warning, sent=issued.isoformat(),
                    effective=issued.isoformat())

    def test_a_stale_warning_still_in_force_is_broadcast(
        self, make_transport, radio, old_warning, now, policy
    ):
        # The failure this fixes: a warning issued before the container came up
        # and still running was suppressed on the first cycle, recorded as
        # handled, and never reconsidered. Age says it is old news; the three
        # hours it has left to run say it is the most important thing happening.
        transport = make_transport(startup_policy=policy)
        transport.start()

        result, _ = transport.deliver(old_warning, DeliveryContext(True, now))

        assert result is DeliveryResult.SENT

    def test_a_stale_warning_about_to_expire_is_suppressed(
        self, make_transport, radio, old_warning, now, policy
    ):
        # Nothing to act on, so it is not worth the airtime.
        expiring = dict(
            old_warning,
            expires=(now + timedelta(minutes=2)).isoformat(),
            ends=(now + timedelta(minutes=2)).isoformat(),
        )
        transport = make_transport(startup_policy=policy)
        transport.start()

        result, detail = transport.deliver(expiring, DeliveryContext(True, now))

        assert result is DeliveryResult.SKIPPED
        assert "startup-stale" in detail
        assert radio.sent == []

    def test_an_alert_this_transport_already_handled_is_not_stale(
        self, make_transport, radio, flood_watch, now, policy
    ):
        # State proves the radio already had a turn at this alert, so it is not
        # part of a cold-start backlog: either NWS reissued it, or the previous
        # attempt failed and this is the retry.
        transport = make_transport(
            startup_policy=policy,
            relevance=RelevanceFilter("watch", "unknown"),
        )
        transport.start()

        result, _ = transport.deliver(
            flood_watch,
            DeliveryContext(True, now, previously_handled=True),
        )

        assert result is DeliveryResult.SENT

    def test_a_stale_zone_product_is_still_suppressed(
        self, make_transport, radio, flood_watch, now, policy
    ):
        # The remaining-life bypass is for warnings only. A watch issued hours
        # ago is exactly the backlog this gate exists to keep off the air.
        transport = make_transport(
            startup_policy=policy,
            relevance=RelevanceFilter("watch", "unknown"),
        )
        transport.start()

        result, detail = transport.deliver(flood_watch, DeliveryContext(True, now))

        assert result is DeliveryResult.SKIPPED
        assert "startup-stale" in detail

    def test_the_policy_does_not_apply_after_the_first_cycle(
        self, make_transport, radio, old_warning, now, policy
    ):
        transport = make_transport(startup_policy=policy)
        transport.start()

        result, _ = transport.deliver(old_warning, DeliveryContext(False, now))

        assert result is DeliveryResult.SENT


class TestMeshCoreSurvivesARestartMidEvent:
    """The failure this change exists to fix, end to end through the app loop.

    Every piece was individually defensible and the combination was silent. A
    county query returns several active alerts at once; the container restarts
    mid-event, which the compose restart policy does by design; the first cycle
    judges everything already in progress too old; whatever survived that was
    refused for airtime spacing measured against a frozen clock. Both refusals
    were written to persistent state as settled, so nothing was ever
    reconsidered and the radio stayed quiet through the whole event.
    """

    @pytest.fixture
    def args(self, tmp_path):
        # delay=0 keeps the app's inter-notification pause out of the way, so
        # what governs the radio here is its own transmission spacing.
        return argparse.Namespace(
            verbose=False,
            delay=0,
            state_file=tmp_path / "state.json",
            state_retention_days=14,
        )

    @pytest.fixture
    def batch(self, fresh_warning, flood_watch, now):
        """Three products active at once, none of them newly issued."""
        issued = (now - timedelta(minutes=40)).isoformat()
        in_force = (now + timedelta(hours=2)).isoformat()

        return [
            dict(
                fresh_warning,
                id="urn:oid:flash-flood",
                event="Flash Flood Warning",
                sent=issued,
                effective=issued,
                expires=in_force,
                ends=in_force,
            ),
            dict(
                fresh_warning,
                id="urn:oid:tornado",
                event="Tornado Warning",
                severity="Extreme",
                sent=issued,
                effective=issued,
                expires=in_force,
                ends=in_force,
            ),
            flood_watch,
        ]

    @pytest.fixture
    def transport(self, make_transport):
        return make_transport(
            relevance=RelevanceFilter("statement", "unknown"),
            rate_limiter=RateLimiter(min_interval_seconds=30, max_per_hour=12),
            startup_policy=StartupPolicy(
                900,
                always_notify_warnings=False,
                warning_min_remaining_seconds=900,
            ),
        )

    def test_every_active_warning_reaches_the_radio(
        self, transport, radio, batch, args, now
    ):
        state = AlertState()
        transport.start()

        failures = process_alerts(
            prioritize(batch),
            [transport],
            state,
            args,
            DeliveryContext(True, now),
        )

        assert failures == 0
        transmitted = [message for _channel, message in radio.sent]
        # Loudest first: the Extreme tornado warning takes the airtime ahead of
        # the flash flood warning, and both go out in the one cycle.
        assert "Tornado Warning" in transmitted[0]
        assert any("Flash Flood Warning" in message for message in transmitted)

    def test_the_stale_watch_is_settled_and_the_warnings_are_not_lost(
        self, transport, radio, batch, args, now
    ):
        state = AlertState()
        transport.start()

        process_alerts(
            prioritize(batch), [transport], state, args,
            DeliveryContext(True, now),
        )

        # The watch is genuinely old news and stays suppressed, terminally.
        watch = batch[2]
        assert state.is_settled(watch, "meshcore")
        assert not any("Flood Watch" in message for _c, message in radio.sent)

        # The warnings were delivered, so they are settled for the right
        # reason rather than quietly written off.
        for alert in batch[:2]:
            assert state.is_settled(alert, "meshcore")

    def test_a_capped_alert_stays_outstanding_for_the_next_cycle(
        self, make_transport, radio, batch, args, now
    ):
        # With no capacity left, the alert must not be recorded as handled: on
        # the old behavior an alert that arrived with a full budget was retired
        # permanently and never transmitted at all.
        transport = make_transport(
            relevance=RelevanceFilter("statement", "unknown"),
            rate_limiter=RateLimiter(min_interval_seconds=0, max_per_hour=1),
            startup_policy=StartupPolicy(0, always_notify_warnings=False),
        )
        transport.start()
        state = AlertState()

        process_alerts(
            prioritize(batch), [transport], state, args,
            DeliveryContext(False, now),
        )

        assert len(radio.sent) == 1
        settled = [
            alert["event"] for alert in batch
            if state.is_settled(alert, "meshcore")
        ]
        # Only the one that actually went out.
        assert settled == ["Tornado Warning"]


class TestMeshCoreFailureHandling:
    def test_a_rejected_frame_reports_failure(self, make_transport, radio,
                                              fresh_warning, now):
        transport = make_transport()
        transport.start()
        radio.reject_next = True

        result, detail = transport.deliver(
            fresh_warning, DeliveryContext(False, now)
        )

        assert result is DeliveryResult.FAILED
        assert "tx queue full" in detail

    def test_a_failure_drops_the_connection(self, make_transport, radio,
                                            fresh_warning, now):
        # Reconnecting on the next attempt is what recovers from a radio that
        # was reset or briefly stopped responding.
        transport = make_transport()
        transport.start()
        radio.reject_next = True

        transport.deliver(fresh_warning, DeliveryContext(False, now))
        assert radio.disconnect_count == 1
        assert transport._meshcore is None

        result, _ = transport.deliver(fresh_warning, DeliveryContext(False, now))
        assert result is DeliveryResult.SENT

    def test_a_failure_does_not_consume_airtime_budget(
        self, make_transport, radio, fresh_warning, now
    ):
        # The alert will be retried, so charging it now would halve the
        # effective rate during an outage.
        limiter = RateLimiter(min_interval_seconds=0, max_per_hour=2)
        transport = make_transport(rate_limiter=limiter)
        transport.start()
        radio.reject_next = True

        transport.deliver(fresh_warning, DeliveryContext(False, now))
        assert limiter.check(now)[0]

    def test_close_is_safe_before_start(self, make_transport):
        make_transport().close()

    def test_close_is_idempotent(self, make_transport):
        transport = make_transport()
        transport.start()
        transport.close()
        transport.close()


# --------------------------------------------------------------------------
# Repeated transmission
# --------------------------------------------------------------------------


@pytest.fixture
def no_repeat_delay(monkeypatch):
    """Collapse the random gap so tests do not wait real seconds."""
    delays = []

    def instant(low, high):
        delays.append((low, high))
        return 0.0

    monkeypatch.setattr("wx_alert.transports.meshcore.random.uniform", instant)
    return delays


@pytest.fixture
def stop_event():
    """Hand back the global stop flag, always cleared afterwards."""
    from wx_alert.shutdown import STOP_EVENT

    STOP_EVENT.clear()
    yield STOP_EVENT
    STOP_EVENT.clear()


class TestMeshCoreRepeats:
    def test_sends_one_copy_by_default(self, make_transport, radio,
                                       fresh_warning, now):
        transport = make_transport()
        transport.start()
        transport.deliver(fresh_warning, DeliveryContext(False, now))
        assert len(radio.sent) == 1

    def test_sends_the_configured_number_of_copies(
        self, make_transport, radio, fresh_warning, now, no_repeat_delay
    ):
        transport = make_transport(repeat_sends=2)
        transport.start()

        result, detail = transport.deliver(
            fresh_warning, DeliveryContext(False, now)
        )

        assert result is DeliveryResult.SENT
        assert "copies=2" in detail
        assert len(radio.sent) == 2

    def test_no_two_copies_are_identical(
        self, make_transport, radio, fresh_warning, now, no_repeat_delay
    ):
        # A receiver that deduplicates on message content would collapse
        # identical copies back into one, spending double the airtime to
        # deliver a single message. Distinct copies cannot be collapsed.
        transport = make_transport(repeat_sends=3)
        transport.start()
        transport.deliver(fresh_warning, DeliveryContext(False, now))

        messages = [message for _channel, message in radio.sent]
        assert len(messages) == 3
        assert len(set(messages)) == 3

    def test_copies_are_labelled_in_order(
        self, make_transport, radio, fresh_warning, now, no_repeat_delay
    ):
        transport = make_transport(repeat_sends=2)
        transport.start()
        transport.deliver(fresh_warning, DeliveryContext(False, now))

        messages = [message for _channel, message in radio.sent]
        assert messages[0].endswith(" 1/2")
        assert messages[1].endswith(" 2/2")

    def test_a_single_copy_carries_no_label(
        self, make_transport, radio, fresh_warning, now
    ):
        # Nothing to disambiguate, so the marker would be pure noise and would
        # cost bytes that the message body needs.
        transport = make_transport(repeat_sends=1)
        transport.start()
        transport.deliver(fresh_warning, DeliveryContext(False, now))

        assert not radio.sent[0][1].rstrip().endswith("1/1")

    def test_the_label_fits_inside_the_budget(
        self, make_transport, radio, now, no_repeat_delay, fresh_warning
    ):
        # A message rendered to the full budget would overflow the firmware's
        # limit once the marker was appended, so the budget has to be reserved
        # up front rather than trimmed afterwards.
        verbose = dict(
            fresh_warning,
            areaDesc="; ".join(f"Very Long County Name Number {n}" for n in range(12)),
            instruction="Take shelter immediately. " * 12,
        )
        transport = make_transport(repeat_sends=2)
        transport.start()
        transport.deliver(verbose, DeliveryContext(False, now))

        for _channel, message in radio.sent:
            assert len(message.encode("utf-8")) <= transport._budget

    def test_repeating_consumes_only_one_unit_of_airtime_budget(
        self, make_transport, radio, fresh_warning, now, no_repeat_delay
    ):
        # The cap counts alerts, not transmissions. Charging per copy would
        # silently halve the alert rate the operator configured.
        limiter = RateLimiter(min_interval_seconds=0, max_per_hour=2)
        transport = make_transport(repeat_sends=2, rate_limiter=limiter)
        transport.start()

        transport.deliver(fresh_warning, DeliveryContext(False, now))

        assert len(radio.sent) == 2
        assert limiter.check(now)[0]

    def test_the_gap_stays_within_the_configured_range(self, make_transport):
        transport = make_transport(repeat_min_delay=1, repeat_max_delay=12)
        for _ in range(50):
            assert 1 <= transport._repeat_delay() <= 12

    def test_reversed_bounds_do_not_produce_a_negative_gap(self, make_transport):
        transport = make_transport(repeat_min_delay=9, repeat_max_delay=2)
        assert 2 <= transport._repeat_delay() <= 9

    def test_a_failed_repeat_still_reports_success(
        self, make_transport, radio, fresh_warning, now, no_repeat_delay
    ):
        # The alert did go out. Reporting failure would requeue it and
        # retransmit the whole burst, producing more duplicates, not fewer.
        transport = make_transport(repeat_sends=2)
        transport.start()

        original = transport._transmit
        calls = {"n": 0}

        def fail_on_repeat(text):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("radio went away")
            original(text)

        transport._transmit = fail_on_repeat

        result, detail = transport.deliver(
            fresh_warning, DeliveryContext(False, now)
        )

        assert result is DeliveryResult.SENT
        assert "copies=1" in detail

    def test_a_failed_first_send_reports_failure(
        self, make_transport, radio, fresh_warning, now, no_repeat_delay
    ):
        transport = make_transport(repeat_sends=2)
        transport.start()
        radio.reject_next = True

        result, _ = transport.deliver(fresh_warning, DeliveryContext(False, now))

        assert result is DeliveryResult.FAILED
        assert radio.sent == []

    def test_shutdown_abandons_the_remaining_copies(
        self, make_transport, radio, fresh_warning, now, stop_event
    ):
        # A pending repeat must not hold the container open past its stop
        # grace period, or Docker escalates to SIGKILL.
        transport = make_transport(repeat_sends=3)
        transport.start()
        stop_event.set()

        result, detail = transport.deliver(
            fresh_warning, DeliveryContext(False, now)
        )

        assert result is DeliveryResult.SENT
        assert "copies=1" in detail
        assert len(radio.sent) == 1


# --------------------------------------------------------------------------
# Recovering a hung radio
# --------------------------------------------------------------------------


class TestMeshCoreHangRecovery:
    @pytest.fixture
    def hung_once(self, monkeypatch, radio):
        """A radio that ignores the first connection, then answers."""
        import types

        import meshcore as meshcore_pkg

        state = {"attempts": 0}

        async def flaky_create_serial(port, baud=115200, **kwargs):
            state["attempts"] += 1
            return None if state["attempts"] == 1 else radio

        monkeypatch.setattr(
            meshcore_pkg,
            "MeshCore",
            types.SimpleNamespace(create_serial=flaky_create_serial),
            raising=False,
        )
        return state

    @pytest.fixture
    def recorded_reset(self, monkeypatch):
        resets = []
        monkeypatch.setattr(
            "wx_alert.transports.meshcore.hard_reset",
            lambda port, **kwargs: resets.append(port),
        )
        return resets

    def test_resets_the_radio_when_it_does_not_answer(
        self, make_transport, hung_once, recorded_reset
    ):
        # A silent radio behind a healthy device node is the case a container
        # restart cannot fix, so the transport has to reboot the hardware.
        transport = make_transport()
        transport.start()

        assert recorded_reset == ["/dev/fake"]
        assert transport._meshcore is not None

    def test_does_not_reset_when_disabled(
        self, make_transport, hung_once, recorded_reset
    ):
        transport = make_transport(auto_reset=False)
        with pytest.raises(TransportError, match="no response"):
            transport.start()
        assert recorded_reset == []

    def test_reports_the_original_fault_when_the_reset_does_not_help(
        self, make_transport, monkeypatch, recorded_reset
    ):
        transport = make_transport(port="/dev/absent")

        with pytest.raises(TransportError, match="no response"):
            transport.start()

        assert recorded_reset == ["/dev/absent"]

    def test_a_reset_failure_does_not_mask_the_real_problem(
        self, make_transport, monkeypatch
    ):
        from wx_alert.radio import RadioResetError

        def explode(port, **kwargs):
            raise RadioResetError("permission denied")

        monkeypatch.setattr("wx_alert.transports.meshcore.hard_reset", explode)
        transport = make_transport(port="/dev/absent")

        # The operator needs to know the radio is unreachable, which is the
        # actionable fault; the failed recovery attempt is a detail.
        with pytest.raises(TransportError, match="no response"):
            transport.start()
