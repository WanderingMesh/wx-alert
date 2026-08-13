"""Broadcast to a MeshCore channel via a USB-attached companion radio.

Two impedance mismatches shape this module.

The meshcore library is asyncio-only while the rest of this program is
synchronous `requests`. Rather than convert everything, a single event loop
runs on a daemon thread and every call is marshalled onto it. The connection
is opened once and held: opening a serial port and completing the firmware
`appstart` handshake costs seconds, and repeatedly claiming and releasing the
port invites conflicts with anything else on the host.

A channel message is also fundamentally unlike an HTTP POST. It is a broadcast
with no acknowledgement, so `OK` from the radio means only that the frame was
accepted for transmission. Nothing here can ever report that a human received
anything, and the wording throughout says "transmitted" rather than
"delivered" for that reason. Because nothing is acknowledged, nothing can be
retried on demand either: the only defence against a lost message is to send
it more than once and accept that receivers may see it twice.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any

from ..mesh_format import build_mesh_message, mesh_text_budget
from ..policy import StartupPolicy, should_suppress_on_startup
from ..radio import RadioResetError, hard_reset
from ..ratelimit import RateLimiter, RelevanceFilter
from ..shutdown import STOP_EVENT
from ..text import clean_field
from .base import DeliveryContext, DeliveryResult, TransportError

LOGGER = logging.getLogger("wx-alert")

# Used when the radio reports no name, so the budget stays conservative
# rather than optimistic.
_FALLBACK_NODE_NAME = "X" * 32

# Longest the transport will hold a poll cycle open to satisfy minimum
# transmission spacing.
#
# Waiting is preferable to postponing: at the default 30 second spacing a batch
# of six alerts from a county query drains inside one cycle instead of taking
# six cycles and half an hour, by which point a Flash Flood Warning has
# expired. Past a couple of minutes that reverses, because the wait delays the
# next NWS query and every alert queued behind this one, so an unusually large
# MIN_SECONDS_BETWEEN_SENDS defers instead.
MAX_SPACING_WAIT_SECONDS = 120


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _AsyncBridge:
    """Runs an asyncio event loop on a background thread.

    Exists so the synchronous polling loop can drive an async library without
    the whole program becoming async, and without paying connection setup
    costs on every message.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="meshcore-loop",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro, timeout: float):
        """Run a coroutine on the loop thread and wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError(f"operation did not complete within {timeout}s")

    def shutdown(self) -> None:
        # The meshcore library leaves a reader task running on the loop.
        # Closing the loop without cancelling it first makes asyncio print
        # "Task was destroyed but it is pending!", which looks like a crash
        # and is deeply misleading in the logs of a failed radio connection.
        self._cancel_pending_tasks()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

    def _cancel_pending_tasks(self) -> None:
        async def cancel_all() -> None:
            pending = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            ]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(cancel_all(), self._loop).result(5)
        except Exception as exc:  # noqa: BLE001 - this is best-effort cleanup
            # on a loop that may already be stopping.
            LOGGER.debug("Error cancelling MeshCore tasks: %s", exc)


class MeshCoreTransport:
    """Broadcasts alerts to one MeshCore channel."""

    name = "meshcore"

    def __init__(
        self,
        *,
        port: str,
        baud: int,
        channel_index: int,
        relevance: RelevanceFilter,
        rate_limiter: RateLimiter,
        startup_policy: StartupPolicy,
        connect_timeout: float = 30.0,
        send_timeout: float = 30.0,
        repeat_sends: int = 1,
        repeat_min_delay: float = 1.0,
        repeat_max_delay: float = 12.0,
        auto_reset: bool = True,
        reset_settle: float = 3.0,
        dry_run: bool = False,
        debug: bool = False,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], bool] = STOP_EVENT.wait,
    ) -> None:
        self._port = port
        self._baud = baud
        self._channel_index = channel_index
        self._relevance = relevance
        self._rate_limiter = rate_limiter
        self._startup_policy = startup_policy
        self._connect_timeout = connect_timeout
        self._send_timeout = send_timeout
        self._repeat_sends = max(1, repeat_sends)
        self._repeat_min_delay = repeat_min_delay
        self._repeat_max_delay = repeat_max_delay
        self._auto_reset = auto_reset
        self._reset_settle = reset_settle
        self._dry_run = dry_run
        self._debug = debug

        # Airtime is metered against a real clock rather than the cycle
        # timestamp in DeliveryContext, and slept through an interruptible wait
        # rather than time.sleep, so a pending gap cannot hold the container
        # open past its stop grace period. Both are injectable for testing.
        self._clock = clock
        self._sleep = sleep

        self._bridge: _AsyncBridge | None = None
        self._meshcore: Any = None
        self._node_name = _FALLBACK_NODE_NAME
        self._budget = mesh_text_budget(_FALLBACK_NODE_NAME)

    # -- connection management -------------------------------------------

    def _open(self) -> Any:
        """Attempt one connection. Returns None when the radio does not answer."""
        from meshcore import MeshCore

        if self._bridge is None:
            self._bridge = _AsyncBridge()

        # create_serial returns None rather than raising when the device does
        # not answer, so the None check is load-bearing.
        return self._bridge.call(
            MeshCore.create_serial(
                self._port,
                self._baud,
                debug=self._debug,
                # The library re-sends appstart after reconnecting, which the
                # firmware requires to re-initialize the session.
                auto_reconnect=True,
                max_reconnect_attempts=3,
            ),
            timeout=self._connect_timeout,
        )

    def _connect(self) -> None:
        """Open the serial port and learn what we need from the radio."""
        from meshcore import EventType

        meshcore = self._open()

        # A silent radio behind a perfectly healthy device node means the
        # firmware has hung. Restarting the container cannot fix that, because
        # the device node is not the problem, so reboot the radio itself.
        if meshcore is None and self._auto_reset:
            LOGGER.warning(
                "No response from the radio on %s; the firmware appears hung, "
                "attempting a hard reset",
                self._port,
            )
            try:
                hard_reset(
                    self._port,
                    settle=self._reset_settle,
                    sleep=STOP_EVENT.wait,
                )
            except RadioResetError as exc:
                # Report the original symptom, not the recovery failure: the
                # radio being unreachable is what the operator needs to fix.
                LOGGER.error("Hard reset failed error=%s", exc)
            else:
                meshcore = self._open()
                if meshcore is not None:
                    LOGGER.info("Radio recovered after a hard reset")

        if meshcore is None:
            raise TransportError(
                f"no response from a MeshCore companion radio on {self._port}"
            )

        self._meshcore = meshcore

        # create_serial performs appstart internally, so self_info is already
        # populated by the time it returns.
        self_info = getattr(meshcore, "self_info", {}) or {}
        node_name = str(self_info.get("name") or "").strip()

        if node_name:
            self._node_name = node_name
        else:
            LOGGER.warning(
                "Radio did not report a node name; assuming the maximum "
                "length so the message budget stays conservative"
            )

        self._budget = mesh_text_budget(self._node_name)

        # Confirm the channel exists now, so a wrong index fails at startup
        # instead of quietly broadcasting into channel 0 for a week.
        channel = self._bridge.call(
            meshcore.commands.get_channel(self._channel_index),
            timeout=self._send_timeout,
        )

        if channel is None or channel.type == EventType.ERROR:
            detail = getattr(channel, "payload", "no response")
            raise TransportError(
                f"MeshCore channel index {self._channel_index} is not "
                f"readable: {detail}"
            )

        channel_name = (channel.payload or {}).get("channel_name", "")

        LOGGER.info(
            "MeshCore connected port=%s node=%r channel=%d name=%r "
            "text_budget=%d bytes",
            self._port,
            self._node_name,
            self._channel_index,
            channel_name,
            self._budget,
        )

    def _ensure_connected(self) -> None:
        if self._meshcore is None:
            self._connect()

    def _drop_connection(self) -> None:
        """Tear down after a failure so the next attempt reconnects cleanly."""
        if self._meshcore is not None and self._bridge is not None:
            try:
                self._bridge.call(self._meshcore.disconnect(), timeout=5)
            except Exception as exc:  # noqa: BLE001 - already in a failure path
                LOGGER.debug("Error while disconnecting MeshCore: %s", exc)

        self._meshcore = None

    # -- transport interface ---------------------------------------------

    def start(self) -> None:
        if self._dry_run:
            LOGGER.warning(
                "MeshCore dry-run enabled: messages will be rendered and "
                "logged but the radio will not be opened port=%s channel=%d",
                self._port,
                self._channel_index,
            )
            return

        self._connect()

    def selftest(self) -> None:
        text = "wx-alert test. No NWS alert was required for this message."

        # start() and deliver() both honour the dry run, and this has to as
        # well, or --meshcore-dry-run would open the port it promises not to.
        if self._dry_run:
            LOGGER.info(
                "MeshCore dry-run self-test channel=%d bytes=%d copies=%d "
                "text=%r",
                self._channel_index,
                len(text.encode("utf-8")),
                self._repeat_sends,
                text,
            )
            return

        self._ensure_connected()
        # Deliberately the same path a real alert takes, repeats included, so
        # the test proves what will actually happen rather than a simpler case.
        copies = self._transmit_burst(text)
        LOGGER.info(
            "MeshCore test transmitted channel=%d bytes=%d copies=%d",
            self._channel_index,
            len(text.encode("utf-8")) + self._copy_label_reserve(),
            copies,
        )

    def deliver(
        self,
        alert: dict[str, Any],
        context: DeliveryContext,
    ) -> tuple[DeliveryResult, str]:
        event = clean_field(alert.get("event"), "Unknown weather alert")

        # Gates run cheapest and most decisive first, so an irrelevant alert
        # never touches the rate limiter's budget or the radio.
        accepted, reason = self._relevance.accepts(alert)
        if not accepted:
            # Terminal: relevance is derived from the alert's own content, so
            # the answer cannot change while the content stays the same.
            return DeliveryResult.SKIPPED, f"filtered: {reason}"

        suppress, reason = should_suppress_on_startup(
            alert,
            self._startup_policy,
            context.first_cycle,
            context.now,
            previously_handled=context.previously_handled,
        )
        if suppress:
            return DeliveryResult.SKIPPED, f"startup-stale: {reason}"

        # Deferred, not skipped: an airtime budget refills. Recording this as
        # settled would retire the alert permanently over a gap of seconds,
        # which is how a batch of alerts used to lose everything but its first
        # member.
        allowed, reason = self._await_capacity()
        if not allowed:
            return DeliveryResult.DEFERRED, f"rate-limit: {reason}"

        # The copy marker is appended to each transmission, so the rendered
        # message has to leave room for it.
        reserve = self._copy_label_reserve()
        text = build_mesh_message(alert, max(1, self._budget - reserve), context.now)
        size = len(text.encode("utf-8")) + reserve

        if self._dry_run:
            LOGGER.info(
                "MeshCore dry-run channel=%d bytes=%d/%d text=%r",
                self._channel_index,
                size,
                self._budget,
                text,
            )
            self._rate_limiter.record(self._clock())
            return DeliveryResult.SENT, f"dry-run bytes={size}/{self._budget}"

        try:
            self._ensure_connected()
            copies = self._transmit_burst(text)
        except TransportError as exc:
            self._drop_connection()
            return DeliveryResult.FAILED, str(exc)
        except Exception as exc:  # noqa: BLE001 - serial and asyncio raise
            # a wide variety of errors; all of them mean "try again later".
            self._drop_connection()
            return DeliveryResult.FAILED, f"{type(exc).__name__}: {exc}"

        self._rate_limiter.record(self._clock())

        LOGGER.info(
            "MeshCore transmitted event=%r channel=%d bytes=%d/%d copies=%d "
            "text=%r",
            event,
            self._channel_index,
            size,
            self._budget,
            copies,
            text,
        )

        # "transmitted", not "delivered": a channel message is an
        # unacknowledged broadcast and nothing here can confirm reception.
        return (
            DeliveryResult.SENT,
            f"transmitted bytes={size}/{self._budget} copies={copies}",
        )

    def close(self) -> None:
        self._drop_connection()

        if self._bridge is not None:
            self._bridge.shutdown()
            self._bridge = None

    # -- internals --------------------------------------------------------

    def _await_capacity(self) -> tuple[bool, str]:
        """Obtain permission to transmit, waiting out minimum spacing.

        Measured against a real clock. DeliveryContext.now is stamped once per
        poll cycle and shared by every alert in it, so metering against it
        reads zero elapsed time between consecutive transmissions and refuses
        everything after the first for a spacing violation that real time had
        already satisfied.

        Spacing is waited out here rather than postponed to the next cycle so
        that a batch of alerts drains at the configured rate instead of one
        alert per poll interval. The hourly cap is not waited for: capacity can
        be nearly an hour away, and the alert is offered again next cycle for
        as long as NWS still lists it as active.
        """
        now = self._clock()
        allowed, reason = self._rate_limiter.check(now)

        if allowed:
            return True, ""

        if self._rate_limiter.cap_reached(now):
            return False, reason

        wait = self._rate_limiter.seconds_until_ready(now)

        if wait > MAX_SPACING_WAIT_SECONDS:
            return False, reason

        LOGGER.info(
            "Holding %ds for MeshCore transmission spacing (%s)",
            wait,
            reason,
        )

        if self._sleep(wait):
            # Shutdown, not a rate-limit decision. Deferring leaves the alert
            # outstanding for whoever starts next.
            return False, "shutdown requested while waiting for airtime"

        return self._rate_limiter.check(self._clock())

    def _copy_label(self, index: int) -> str:
        """The marker distinguishing one copy of a message from another.

        Mainly this is for the reader: seeing the same warning twice, they have
        no way to tell whether two separate things happened, and "1/2" answers
        that.

        It also guards against deduplication on the receiving side. The
        companion protocol tells client authors to discard duplicate incoming
        messages and suggests timestamp plus content as the key, but what any
        given client keys on is up to that client, and one keying on content
        alone would swallow every repeat. Distinct copies are immune to that
        without needing to know what is at the other end.

        Whether that actually happens here is unresolved; see "Repeats and
        deduplication" under Open questions in the readme. The marker is cheap
        enough to be worth keeping regardless.
        """
        if self._repeat_sends < 2:
            return ""
        return f" {index}/{self._repeat_sends}"

    def _copy_label_reserve(self) -> int:
        """Bytes to hold back from the message budget for the copy marker.

        Appending the marker to a message already rendered to the full budget
        would push it over the firmware's limit.
        """
        if self._repeat_sends < 2:
            return 0
        return max(
            len(self._copy_label(index).encode("utf-8"))
            for index in range(1, self._repeat_sends + 1)
        )

    def _repeat_delay(self) -> float:
        """Pick the gap before the next copy.

        Randomised rather than fixed for two reasons. A constant gap can
        phase-lock with another periodic sender on the channel, so a pair of
        transmissions that collide once will collide again on every repeat;
        jitter decorrelates them. It also spreads the load when several alerts
        are dispatched close together.
        """
        low = min(self._repeat_min_delay, self._repeat_max_delay)
        high = max(self._repeat_min_delay, self._repeat_max_delay)
        return random.uniform(low, high)

    def _transmit_burst(self, text: str) -> int:
        """Send the same text more than once. Returns the number of copies.

        Channel messages are unacknowledged, so a lost one is lost silently and
        cannot be detected, let alone retried on demand. Sending a second copy
        is the only available defence, and it costs receivers a duplicate.

        Each copy carries a distinct "n/N" marker, so that a receiver
        deduplicating on message content cannot collapse the copies back into
        one and quietly undo the whole exercise.
        """
        self._transmit(text + self._copy_label(1))
        copies = 1

        while copies < self._repeat_sends:
            if STOP_EVENT.wait(self._repeat_delay()):
                LOGGER.info(
                    "Stop requested; skipping %d remaining mesh repeat(s)",
                    self._repeat_sends - copies,
                )
                break

            try:
                self._transmit(text + self._copy_label(copies + 1))
            except Exception as exc:  # noqa: BLE001 - any failure here is
                # survivable, so it is logged rather than raised.
                #
                # The message already went out once. Reporting the delivery as
                # failed would put the alert back in the queue and transmit the
                # whole burst again next cycle, so a flaky repeat would produce
                # more duplicates rather than fewer.
                LOGGER.warning(
                    "MeshCore repeat %d/%d failed; the first copy was already "
                    "transmitted error=%s",
                    copies + 1,
                    self._repeat_sends,
                    exc,
                )
                break

            copies += 1

        return copies

    def _transmit(self, text: str) -> None:
        from meshcore import EventType

        if self._meshcore is None or self._bridge is None:
            raise TransportError("MeshCore transport is not connected")

        result = self._bridge.call(
            self._meshcore.commands.send_chan_msg(self._channel_index, text),
            timeout=self._send_timeout,
        )

        if result is None:
            raise TransportError("radio did not respond to the send command")

        if result.type == EventType.ERROR:
            raise TransportError(f"radio rejected the message: {result.payload}")
