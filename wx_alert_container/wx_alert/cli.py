"""Command-line parsing.

The config file is loaded first and supplies every default, so an explicit
command-line option always wins over the file without either layer needing to
know about the other.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import (
    MAX_MESHCORE_REPEAT_SENDS,
    NTFY_PRIORITY_CHOICES,
    ConfigurationError,
    load_configuration,
    normalize_server_url,
    read_config_file,
    validate_check_interval,
    validate_delay,
    validate_latitude,
    validate_longitude,
    validate_nonnegative,
    validate_radius_km,
)
from .text import clean_optional
from .zones import parse_zone_list

LOGGER = logging.getLogger("wx-alert")

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


def nonnegative_seconds(value: str) -> int:
    return _argparse_wrapper(
        lambda v: validate_nonnegative(int(v), "value")
    )(value)


def zone_list(value: str) -> tuple[str, ...]:
    return _argparse_wrapper(lambda v: parse_zone_list(v, "zones"))(value)


def radius_km(value: str) -> float:
    return _argparse_wrapper(lambda v: validate_radius_km(float(v)))(value)


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
        "--zones",
        type=zone_list,
        default=config.zones,
        metavar="UGC[,UGC...]",
        help=(
            "Additional NWS UGC zones to query alongside the county "
            "containing the monitored point, which is resolved automatically. "
            "Use county codes such as NVC031 for neighbouring counties a "
            "wide-area mesh reaches into. Configured default: "
            f"{','.join(config.zones) or 'none'}."
        ),
    )

    parser.add_argument(
        "--alert-radius-km",
        type=radius_km,
        default=config.alert_radius_km,
        metavar="KM",
        help=(
            "Discard a polygon warning whose warned area is farther than "
            "this from the monitored point. Alerts issued to a whole zone "
            "carry no polygon and are always kept. 0 disables the test and "
            "keeps everything in the queried counties. Configured default: "
            f"{config.alert_radius_km:g}."
        ),
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

    parser.add_argument(
        "--state-file",
        type=Path,
        default=config.state_file,
        metavar="FILE",
        help=(
            "Persistent delivery history. Mount this path as a volume so it "
            f"survives container replacement. Configured default: {config.state_file}"
        ),
    )

    parser.add_argument(
        "--exit-after-failed-cycles",
        type=nonnegative_seconds,
        default=config.exit_after_failed_cycles,
        metavar="COUNT",
        help=(
            "Exit non-zero once a transport has failed this many consecutive "
            "cycles, so the container restart policy can reinitialize it. "
            "This is how a replugged USB radio is recovered. 0 disables it. "
            f"Configured default: {config.exit_after_failed_cycles}."
        ),
    )

    parser.add_argument(
        "--startup-max-age",
        type=nonnegative_seconds,
        default=config.ntfy_startup_max_age,
        metavar="SECONDS",
        help=(
            "On the first cycle after start, do not send a non-warning "
            "product older than this. 0 disables the policy. Configured "
            f"default: {config.ntfy_startup_max_age}."
        ),
    )

    parser.add_argument(
        "--notify-old-warnings-on-startup",
        action=argparse.BooleanOptionalAction,
        default=config.always_notify_warnings_on_startup,
        help=(
            "Allow active warnings to bypass the startup age limit. "
            f"Configured default: {config.always_notify_warnings_on_startup}."
        ),
    )

    mesh = parser.add_argument_group(
        "MeshCore",
        "Broadcast to a MeshCore channel via a USB companion radio.",
    )

    mesh.add_argument(
        "--meshcore",
        action=argparse.BooleanOptionalAction,
        default=config.meshcore.enabled,
        help=(
            "Broadcast qualifying alerts to a MeshCore channel. "
            f"Configured default: {config.meshcore.enabled}."
        ),
    )

    mesh.add_argument(
        "--meshcore-test",
        action="store_true",
        help=(
            "Connect to the radio, report what it says, transmit one test "
            "message, and exit without querying NWS."
        ),
    )

    mesh.add_argument(
        "--meshcore-dry-run",
        action="store_true",
        help=(
            "Render and log every message that would be transmitted, "
            "including its byte size, without opening the serial port. "
            "Use this to review formatting before going on the air."
        ),
    )

    mesh.add_argument(
        "--meshcore-port",
        default=config.meshcore.port,
        metavar="DEVICE",
        help=(
            "Serial device for the companion radio. Prefer a stable "
            "/dev/serial/by-id/ path over /dev/ttyACM0, which can change "
            f"across reboots. Configured default: {config.meshcore.port}"
        ),
    )

    mesh.add_argument(
        "--meshcore-channel",
        type=int,
        default=config.meshcore.channel_index,
        metavar="INDEX",
        help=(
            "Channel index to broadcast on. Configured default: "
            f"{config.meshcore.channel_index}."
        ),
    )

    mesh.add_argument(
        "--meshcore-repeat",
        type=int,
        default=config.meshcore.repeat_sends,
        metavar="COUNT",
        help=(
            "Number of times each message is transmitted. Channel messages "
            "are unacknowledged, so a second copy is the only defence against "
            "a lost one, at the cost of a duplicate for receivers. 1 disables "
            f"repeating. Configured default: {config.meshcore.repeat_sends}."
        ),
    )

    mesh.add_argument(
        "--meshcore-startup-max-age",
        type=nonnegative_seconds,
        default=config.meshcore.startup_max_age,
        metavar="SECONDS",
        help=(
            "On the first cycle after start, do not broadcast a product older "
            "than this. A warning still in force for at least this long is "
            "broadcast regardless of its age. 0 disables the policy, which is "
            "what a dry run wants when previewing current alerts. Separate "
            "from --startup-max-age, which governs ntfy. Configured default: "
            f"{config.meshcore.startup_max_age}."
        ),
    )

    mesh.add_argument(
        "--meshcore-reset",
        action="store_true",
        help=(
            "Reboot the radio over its serial control lines and exit. Use "
            "this when the firmware has hung: the USB device still exists and "
            "the port still opens, but the radio never answers. Recovers a "
            "remote host over SSH without physical access."
        ),
    )

    args = parser.parse_args(argv)

    if args.ntfy and args.ntfy_test:
        parser.error("--ntfy and --ntfy-test cannot be used together")

    if not 1 <= args.meshcore_repeat <= MAX_MESHCORE_REPEAT_SENDS:
        parser.error(
            "--meshcore-repeat must be between 1 and "
            f"{MAX_MESHCORE_REPEAT_SENDS}"
        )

    if (args.ntfy or args.ntfy_test) and not clean_optional(args.ntfy_topic):
        parser.error(
            "[ntfy] TOPIC in the config file, or --ntfy-topic, is required "
            "when using --ntfy or --ntfy-test"
        )

    args.meshcore_port = clean_optional(args.meshcore_port)
    meshcore_wanted = args.meshcore or args.meshcore_test

    # A dry run renders messages without touching hardware, so it is the one
    # way to exercise the transport with no port configured.
    if meshcore_wanted and not args.meshcore_port and not args.meshcore_dry_run:
        parser.error(
            "[meshcore] PORT in the config file, or --meshcore-port, is "
            "required when MeshCore is enabled"
        )

    if args.meshcore_reset and not args.meshcore_port:
        parser.error(
            "[meshcore] PORT in the config file, or --meshcore-port, is "
            "required by --meshcore-reset"
        )

    # --meshcore-test and --ntfy-test deliberately combine: verifying that a
    # radio failure still leaves ntfy working requires exercising both in the
    # same run.

    # Catching this here means an operator who mistypes a flag gets an error
    # rather than a container that polls NWS forever and delivers nothing.
    if not (args.ntfy or args.ntfy_test or meshcore_wanted or args.verbose):
        LOGGER.warning(
            "No transport is enabled; alerts will only be printed to stdout"
        )

    args.config = config_args.config
    args.config_parser = parsed_file
    args.ntfy_topic = clean_optional(args.ntfy_topic)
    args.ntfy_token = clean_optional(args.ntfy_token)
    args.ntfy_tags = args.ntfy_tags.strip()

    # Not exposed on the command line: changing retention per-run has no
    # sensible use, and it belongs with the rest of the state settings.
    args.state_retention_days = config.state_retention_days

    # Carried through so the transport can be built without re-reading the
    # file. Only the settings with a real per-run use get their own flag.
    args.meshcore_config = config.meshcore

    return args
