"""Entry point: wire configuration into transports and run."""

from __future__ import annotations

import argparse
import logging
import sys
import time

import requests

from .app import STATUS_DELIVERY_FAILURE, run_polling_loop
from .cli import parse_arguments
from .shutdown import install_signal_handlers
from .transports.base import Transport, TransportError
from .transports.ntfy import NtfyTransport

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
            )
        )

    return transports


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_arguments(argv)
    install_signal_handlers()

    mode = "ntfy-test" if args.ntfy_test else ("polling" if args.loop else "once")
    LOGGER.info("Starting wx-alert mode=%s config=%s", mode, args.config)

    with requests.Session() as session:
        try:
            transports = build_transports(session, args)
        except TransportError as exc:
            LOGGER.error("Transport initialization failed error=%s", exc)
            return 3

        if args.ntfy_test:
            for transport in transports:
                try:
                    transport.selftest()
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    LOGGER.error(
                        "Transport self-test failed transport=%s error=%s",
                        transport.name,
                        exc,
                    )
                    return STATUS_DELIVERY_FAILURE
            return 0

        started: list[Transport] = []
        try:
            for transport in transports:
                transport.start()
                started.append(transport)
        except TransportError as exc:
            LOGGER.error("Transport startup failed error=%s", exc)
            for transport in started:
                transport.close()
            return 3

        try:
            return run_polling_loop(session, args, started)
        finally:
            for transport in started:
                transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
