"""Entry point: wire configuration into transports and run."""

from __future__ import annotations

import argparse
import logging
import sys
import time

import requests

from .app import STATUS_DELIVERY_FAILURE, STATUS_STATE_FAILURE, run_polling_loop
from .cli import parse_arguments
from .policy import StartupPolicy
from .radio import RadioResetError, hard_reset
from .ratelimit import RateLimiter, RelevanceFilter
from .scope import interpret_probe
from .shutdown import install_signal_handlers
from .state import StateError, load_state
from .transports.base import Transport, TransportError
from .transports.meshcore import MeshCoreTransport
from .transports.ntfy import NtfyTransport
from .zones import ZoneResolutionError, determine_zones, zone_cache_path

LOGGER = logging.getLogger("wx-alert")


def configure_logging() -> None:
    """Configure UTC, line-buffered operational logging for Docker logs."""
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )


def build_transports(
    session: requests.Session,
    args: argparse.Namespace,
) -> list[Transport]:
    """Construct every transport the user enabled."""
    transports: list[Transport] = []

    if args.ntfy or args.ntfy_test:
        transports.append(
            NtfyTransport(
                session,
                server=args.ntfy_server,
                topic=args.ntfy_topic,
                token=args.ntfy_token,
                tags=args.ntfy_tags,
                priority=args.ntfy_priority,
                verbose=args.verbose,
                startup_policy=StartupPolicy(
                    max_age_seconds=args.startup_max_age,
                    always_notify_warnings=args.notify_old_warnings_on_startup,
                ),
            )
        )

    if args.meshcore or args.meshcore_test:
        mesh = args.meshcore_config

        try:
            relevance = RelevanceFilter(
                minimum_class=mesh.minimum_class,
                minimum_severity=mesh.minimum_severity,
            )
        except ValueError as exc:
            raise TransportError(f"MeshCore filter is invalid: {exc}") from exc

        transports.append(
            MeshCoreTransport(
                port=args.meshcore_port or "",
                baud=mesh.baud,
                channel_index=args.meshcore_channel,
                scope=args.meshcore_scope,
                relevance=relevance,
                rate_limiter=RateLimiter(
                    min_interval_seconds=mesh.min_interval_seconds,
                    max_per_hour=mesh.max_per_hour,
                ),
                startup_policy=StartupPolicy(
                    max_age_seconds=args.meshcore_startup_max_age,
                    # ntfy's unconditional warning bypass is deliberately not
                    # applied to the radio: a duplicate push costs nothing,
                    # while replaying every hours-old warning after a restart
                    # costs everyone on the channel airtime.
                    always_notify_warnings=False,
                    # Instead the radio asks how much life a warning has left.
                    # Reusing the staleness limit as the threshold keeps this
                    # to one number an operator has to reason about: a warning
                    # is too old to replay after the limit has passed, and
                    # worth the airtime while it has at least that long left to
                    # run. The two together suppress an expiring warning from
                    # the backlog without suppressing an active one.
                    warning_min_remaining_seconds=args.meshcore_startup_max_age,
                ),
                connect_timeout=mesh.connect_timeout,
                send_timeout=mesh.send_timeout,
                repeat_sends=args.meshcore_repeat,
                repeat_min_delay=mesh.repeat_min_delay,
                repeat_max_delay=mesh.repeat_max_delay,
                auto_reset=mesh.auto_reset,
                reset_settle=mesh.reset_settle,
                dry_run=args.meshcore_dry_run,
                debug=args.verbose,
            )
        )

    return transports


def build_diagnostic_transport(args) -> MeshCoreTransport:
    """A transport built only to talk to the radio.

    The filtering and rate limiting the delivery path needs are irrelevant to
    a diagnostic and would only get in its way, so they are stubbed rather
    than read from the operator's configuration.
    """
    mesh = args.meshcore_config

    return MeshCoreTransport(
        port=args.meshcore_port or "",
        baud=mesh.baud,
        channel_index=args.meshcore_channel,
        # The probe sets its own scope on each leg; inheriting the configured
        # one would contaminate the control.
        scope=None,
        relevance=RelevanceFilter(
            minimum_class=mesh.minimum_class,
            minimum_severity=mesh.minimum_severity,
        ),
        rate_limiter=RateLimiter(min_interval_seconds=0, max_per_hour=0),
        startup_policy=StartupPolicy(
            max_age_seconds=0,
            always_notify_warnings=False,
        ),
        connect_timeout=mesh.connect_timeout,
        send_timeout=mesh.send_timeout,
        auto_reset=mesh.auto_reset,
        reset_settle=mesh.reset_settle,
        debug=args.verbose,
    )


def run_radio_diagnostic(args) -> int:
    """Run --meshcore-channels or --meshcore-scope-probe, then exit."""
    transport = build_diagnostic_transport(args)

    try:
        if args.meshcore_add_channel:
            name = args.meshcore_add_channel
            slot = transport.add_channel(name, slot=args.meshcore_channel_slot)
            print(f"{name} is on channel index {slot}.")
            print(f"Probe it with --meshcore-probe-channel {slot}")
            return 0

        if args.meshcore_channels:
            channels = transport.list_channels()
            if not channels:
                print("The radio reports no configured channels.")
                return STATUS_DELIVERY_FAILURE
            print("Channels configured on the radio:")
            for index, name in channels:
                print(f"  {index:>3}  {name}")
            return 0

        region = args.meshcore_scope_probe
        print(
            f"Probing region {region} on channel "
            f"{args.meshcore_probe_channel}. Two messages will be sent: an "
            "unscoped control, then the scoped test.\n"
        )

        control, scoped = transport.probe_scope(
            region,
            channel_index=args.meshcore_probe_channel,
            timeout=args.meshcore_probe_timeout,
        )

        for run in (control, scoped):
            print(f"{run.label}:")
            if run.replied:
                print(f"  reply after {run.hops}")
                print(f"  {run.text}")
            else:
                print(f"  no reply within {args.meshcore_probe_timeout:.0f}s")
            print()

        ok, verdict = interpret_probe(control, scoped)
        print(verdict)

        # A region that does not work is a failed check, not a failed run, but
        # a non-zero exit lets this be scripted.
        return 0 if ok else STATUS_DELIVERY_FAILURE
    except TransportError as exc:
        LOGGER.error("Radio diagnostic failed error=%s", exc)
        return STATUS_DELIVERY_FAILURE
    finally:
        transport.close()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_arguments(argv)
    install_signal_handlers()

    # Handled before anything else: the point of this mode is to recover a
    # radio that is too hung to connect to, so it must not depend on any
    # transport starting successfully.
    if args.meshcore_reset:
        LOGGER.info("Starting wx-alert mode=radio-reset port=%s", args.meshcore_port)
        try:
            hard_reset(args.meshcore_port, settle=args.meshcore_config.reset_settle)
        except RadioResetError as exc:
            LOGGER.error("Radio reset failed error=%s", exc)
            return STATUS_DELIVERY_FAILURE
        LOGGER.info("Radio reset issued; reconnect with --meshcore-test to verify")
        return 0

    # Also ahead of transport setup: these talk to the radio directly and have
    # nothing to do with delivering alerts.
    if (
        args.meshcore_channels
        or args.meshcore_scope_probe
        or args.meshcore_add_channel
    ):
        return run_radio_diagnostic(args)

    self_test = args.ntfy_test or args.meshcore_test
    mode = "self-test" if self_test else ("polling" if args.loop else "once")
    LOGGER.info("Starting wx-alert mode=%s config=%s", mode, args.config)

    with requests.Session() as session:
        try:
            transports = build_transports(session, args)
        except TransportError as exc:
            LOGGER.error("Transport initialization failed error=%s", exc)
            return STATUS_STATE_FAILURE

        if self_test:
            status = 0
            for transport in transports:
                try:
                    transport.selftest()
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    LOGGER.error(
                        "Transport self-test failed transport=%s error=%s",
                        transport.name,
                        exc,
                    )
                    status = STATUS_DELIVERY_FAILURE
                finally:
                    transport.close()
            return status

        try:
            state = load_state(args.state_file)
        except StateError as exc:
            # Refuse to start rather than run with no history. Continuing
            # would re-deliver every currently active alert.
            LOGGER.error("Persistent state initialization failed error=%s", exc)
            return STATUS_STATE_FAILURE

        try:
            args.query_zones = determine_zones(
                session,
                latitude=args.latitude,
                longitude=args.longitude,
                extra_zones=args.zones,
                cache_path=zone_cache_path(args.state_file),
            )
        except ZoneResolutionError as exc:
            # Refuse to start rather than query nothing. An empty zone list
            # returns an empty alert list, so the program would report "no
            # active alerts" forever while the weather did as it pleased.
            LOGGER.error("Could not determine which NWS zones to query: %s", exc)
            return STATUS_STATE_FAILURE

        LOGGER.info(
            "Monitoring zones=%s radius=%gkm point=%.4f,%.4f",
            ",".join(args.query_zones),
            args.alert_radius_km,
            args.latitude,
            args.longitude,
        )

        started: list[Transport] = []
        try:
            for transport in transports:
                transport.start()
                started.append(transport)
        except TransportError as exc:
            LOGGER.error("Transport startup failed error=%s", exc)
            for transport in started:
                transport.close()
            return STATUS_STATE_FAILURE

        try:
            return run_polling_loop(session, args, started, state)
        finally:
            for transport in started:
                transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
