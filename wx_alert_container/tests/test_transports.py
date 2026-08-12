"""Transport behavior, exercised without a network or a serial port.

The MeshCore tests substitute a fake companion radio for the real one. This
covers everything except the serial layer itself: the async bridge, the
startup handshake, budget derivation from the node name, channel validation,
the gate ordering, and reconnection after a failure.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from wx_alert.policy import StartupPolicy
from wx_alert.ratelimit import RateLimiter, RelevanceFilter
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


@pytest.fixture
def make_transport(radio, monkeypatch):
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

    def test_respects_the_rate_limiter(self, make_transport, radio,
                                       fresh_warning, now):
        transport = make_transport(
            rate_limiter=RateLimiter(min_interval_seconds=60, max_per_hour=0)
        )
        transport.start()

        first, _ = transport.deliver(fresh_warning, DeliveryContext(False, now))
        second, detail = transport.deliver(
            dict(fresh_warning, id="urn:oid:second"),
            DeliveryContext(False, now + timedelta(seconds=5)),
        )

        assert first is DeliveryResult.SENT
        assert second is DeliveryResult.SKIPPED
        assert "rate-limit" in detail
        assert len(radio.sent) == 1

    def test_a_skip_does_not_consume_airtime_budget(self, make_transport, radio,
                                                    fresh_warning, now):
        limiter = RateLimiter(min_interval_seconds=60, max_per_hour=0)
        transport = make_transport(rate_limiter=limiter)
        transport.start()

        transport.deliver(fresh_warning, DeliveryContext(False, now))
        transport.deliver(fresh_warning, DeliveryContext(False, now))

        # The second attempt was refused, so capacity returns 60s after the
        # first transmission rather than 60s after the refusal.
        assert limiter.seconds_until_ready(now + timedelta(seconds=61)) == 0

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

    def test_every_copy_is_identical(
        self, make_transport, radio, fresh_warning, now, no_repeat_delay
    ):
        transport = make_transport(repeat_sends=3)
        transport.start()
        transport.deliver(fresh_warning, DeliveryContext(False, now))

        assert len({message for _channel, message in radio.sent}) == 1

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
