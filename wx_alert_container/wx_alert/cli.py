"""Command-line parsing.

The config file is loaded first and supplies every default, so an explicit
command-line option always wins over the file without either layer needing to
know about the other.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import (
    NTFY_PRIORITY_CHOICES,
    ConfigurationError,
    load_configuration,
    normalize_server_url,
    read_config_file,
    validate_check_interval,
    validate_delay,
    validate_latitude,
    validate_longitude,
)
from .text import clean_optional

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.ini"


def _argparse_wrapper(func, *args):
    """Adapt a ConfigurationError-raising validator to argparse's protocol."""

    def validator(value: str):
        try:
            return func(value, *args)
        except (ValueError, ConfigurationError) as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return validator


def delay_seconds(value: str) -> int:
    return _argparse_wrapper(lambda v: validate_delay(int(v), "delay"))(value)


def check_interval_seconds(value: str) -> int:
    return _argparse_wrapper(
        lambda v: validate_check_interval(int(v), "check interval")
    )(value)


def latitude_value(value: str) -> float:
    return _argparse_wrapper(lambda v: validate_latitude(float(v)))(value)


def longitude_value(value: str) -> float:
    return _argparse_wrapper(lambda v: validate_longitude(float(v)))(value)


def ntfy_server_url(value: str) -> str:
    return _argparse_wrapper(lambda v: normalize_server_url(v, "ntfy server"))(value)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Load the config file, then allow explicit CLI options to override it."""
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        metavar="FILE",
        help=f"INI configuration file. Default: {DEFAULT_CONFIG_PATH}",
    )
    config_args, _ = config_parser.parse_known_args(argv)

    try:
        parsed_file = read_config_file(config_args.config)
        config = load_configuration(parsed_file)
    except ConfigurationError as exc:
        config_parser.error(str(exc))

    parser = argparse.ArgumentParser(
        parents=[config_parser],
        description=(
            "Check for active National Weather Service alerts and publish "
            "each new or updated alert to the enabled transports. "
            "Command-line values override config.ini."
        ),
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Display and send complete alert information.",
    )

    parser.add_argument(
        "-d",
        "--delay",
        type=delay_seconds,
        default=config.delay,
        metavar="SECONDS",
        help=(
            "Delay between multiple alert messages, from 1 to 60 seconds. "
            f"Configured default: {config.delay}."
        ),
    )

    parser.add_argument(
        "--check-interval",
        type=check_interval_seconds,
        default=config.check_interval,
        metavar="SECONDS",
        help=(
            "Interval between NWS checks in polling mode, from 5 to 3600 "
            f"seconds. Configured default: {config.check_interval}."
        ),
    )

    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously, checking at the configured interval.",
    )
    run_mode.add_argument(
        "--once",
        action="store_true",
        help="Perform one NWS check and exit.",
    )

    parser.add_argument(
        "--latitude",
        type=latitude_value,
        default=config.latitude,
        metavar="LATITUDE",
        help=f"Latitude to monitor. Configured default: {config.latitude}",
    )

    parser.add_argument(
        "--longitude",
        type=longitude_value,
        default=config.longitude,
        metavar="LONGITUDE",
        help=f"Longitude to monitor. Configured default: {config.longitude}",
    )

    parser.add_argument(
        "--ntfy",
        action="store_true",
        help="Publish each active alert to ntfy.",
    )

    parser.add_argument(
        "--ntfy-test",
        action="store_true",
        help="Send one ntfy test notification and exit without querying NWS.",
    )

    parser.add_argument(
        "--ntfy-server",
        type=ntfy_server_url,
        default=config.ntfy_server,
        metavar="URL",
        help=f"ntfy server URL. Configured default: {config.ntfy_server}",
    )

    parser.add_argument(
        "--ntfy-topic",
        default=config.ntfy_topic,
        metavar="TOPIC",
        help="ntfy topic. Normally supplied by config.ini.",
    )

    parser.add_argument(
        "--ntfy-token",
        default=config.ntfy_token,
        metavar="TOKEN",
        help="Optional ntfy access token. Normally supplied by config.ini.",
    )

    parser.add_argument(
        "--ntfy-priority",
        choices=NTFY_PRIORITY_CHOICES,
        default=config.ntfy_priority,
        help=(
            "ntfy notification priority. In auto mode, the product class and "
            "NWS severity are mapped to a priority. Configured default: "
            f"{config.ntfy_priority}."
        ),
    )

    parser.add_argument(
        "--ntfy-tags",
        default=config.ntfy_tags,
        metavar="TAGS",
        help="Comma-separated base ntfy tags. Normally supplied by config.ini.",
    )

    args = parser.parse_args(argv)

    if args.ntfy and args.ntfy_test:
        parser.error("--ntfy and --ntfy-test cannot be used together")

    if (args.ntfy or args.ntfy_test) and not clean_optional(args.ntfy_topic):
        parser.error(
            "[ntfy] TOPIC in the config file, or --ntfy-topic, is required "
            "when using --ntfy or --ntfy-test"
        )

    args.config = config_args.config
    args.config_parser = parsed_file
    args.ntfy_topic = clean_optional(args.ntfy_topic)
    args.ntfy_token = clean_optional(args.ntfy_token)
    args.ntfy_tags = args.ntfy_tags.strip()

    return args
