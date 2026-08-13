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
                relevance=relevance,
                rate_limiter=RateLimiter(
                    min_interval_seconds=mesh.min_interval_seconds,
                    max_per_hour=mesh.max_per_hour,
                ),
                startup_policy=StartupPolicy(
                    max_age_seconds=mesh.startup_max_age,
                    # The warning bypass is deliberately not applied to the
                    # radio. On ntfy a duplicate costs nothing; on a shared
                    # channel, replaying hours-old warnings after a restart
                    # costs everyone airtime.
                    always_notify_warnings=False,
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
