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
"delivered" for that reason.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from ..mesh_format import build_mesh_message, mesh_text_budget
from ..policy import StartupPolicy, should_suppress_on_startup
from ..ratelimit import RateLimiter, RelevanceFilter
from ..text import clean_field
from .base import DeliveryContext, DeliveryResult, TransportError

LOGGER = logging.getLogger("wx-alert")

# Used when the radio reports no name, so the budget stays conservative
# rather than optimistic.
_FALLBACK_NODE_NAME = "X" * 32


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
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


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
        dry_run: bool = False,
        debug: bool = False,
    ) -> None:
        self._port = port
        self._baud = baud
        self._channel_index = channel_index
        self._relevance = relevance
        self._rate_limiter = rate_limiter
        self._startup_policy = startup_policy
        self._connect_timeout = connect_timeout
        self._send_timeout = send_timeout
        self._dry_run = dry_run
        self._debug = debug

        self._bridge: _AsyncBridge | None = None
        self._meshcore: Any = None
        self._node_name = _FALLBACK_NODE_NAME
        self._budget = mesh_text_budget(_FALLBACK_NODE_NAME)

    # -- connection management -------------------------------------------

    def _connect(self) -> None:
        """Open the serial port and learn what we need from the radio."""
        from meshcore import EventType, MeshCore

        if self._bridge is None:
            self._bridge = _AsyncBridge()

        # create_serial returns None rather than raising when the device does
        # not answer, so the None check is load-bearing.
        meshcore = self._bridge.call(
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
        self._ensure_connected()
        text = "wx-alert test. No NWS alert was required for this message."
        self._transmit(text)
        LOGGER.info(
            "MeshCore test transmitted channel=%d bytes=%d",
            self._channel_index,
            len(text.encode("utf-8")),
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
            return DeliveryResult.SKIPPED, f"filtered: {reason}"

        suppress, reason = should_suppress_on_startup(
            alert,
            self._startup_policy,
            context.first_cycle,
            context.now,
        )
        if suppress:
            return DeliveryResult.SKIPPED, f"startup-stale: {reason}"

        allowed, reason = self._rate_limiter.check(context.now)
        if not allowed:
            return DeliveryResult.SKIPPED, f"rate-limit: {reason}"

        text = build_mesh_message(alert, self._budget, context.now)
        size = len(text.encode("utf-8"))

        if self._dry_run:
            LOGGER.info(
                "MeshCore dry-run channel=%d bytes=%d/%d text=%r",
                self._channel_index,
                size,
                self._budget,
                text,
            )
            self._rate_limiter.record(context.now)
            return DeliveryResult.SENT, f"dry-run bytes={size}/{self._budget}"

        try:
            self._ensure_connected()
            self._transmit(text)
        except TransportError as exc:
            self._drop_connection()
            return DeliveryResult.FAILED, str(exc)
        except Exception as exc:  # noqa: BLE001 - serial and asyncio raise
            # a wide variety of errors; all of them mean "try again later".
            self._drop_connection()
            return DeliveryResult.FAILED, f"{type(exc).__name__}: {exc}"

        self._rate_limiter.record(context.now)

        LOGGER.info(
            "MeshCore transmitted event=%r channel=%d bytes=%d/%d text=%r",
            event,
            self._channel_index,
            size,
            self._budget,
            text,
        )

        # "transmitted", not "delivered": a channel message is an
        # unacknowledged broadcast and nothing here can confirm reception.
        return DeliveryResult.SENT, f"transmitted bytes={size}/{self._budget}"

    def close(self) -> None:
        self._drop_connection()

        if self._bridge is not None:
            self._bridge.shutdown()
            self._bridge = None

    # -- internals --------------------------------------------------------

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
