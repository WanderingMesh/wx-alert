#!/usr/bin/env python3

from __future__ import annotations

import argparse
import configparser
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

NWS_API_URL = "https://api.weather.gov/alerts/active"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.ini")
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_NTFY_TAGS = "warning,weather"
DEFAULT_NTFY_PRIORITY = "auto"
DEFAULT_DELAY_SECONDS = 1
DEFAULT_CHECK_INTERVAL_SECONDS = 3600

# Replace this contact address with a real monitored address before broad use.
NWS_HEADERS = {
    "User-Agent": "local-weather-alert-checker/1.0 (admin@example.org)",
    "Accept": "application/geo+json",
}

NTFY_PRIORITY_CHOICES = (
    "auto",
    "min",
    "low",
    "default",
    "high",
    "max",
    "urgent",
)

LOGGER = logging.getLogger("wx-alert")
STOP_EVENT = threading.Event()


class ConfigurationError(ValueError):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class FileConfiguration:
    latitude: float
    longitude: float
    ntfy_topic: str | None
    ntfy_server: str
    ntfy_token: str | None
    ntfy_tags: str
    ntfy_priority: str
    delay: int
    check_interval: int


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


def clean_optional(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip()
    return value or None


def validate_delay(value: int, source: str = "delay") -> int:
    if not 1 <= value <= 60:
        raise ConfigurationError(f"{source} must be between 1 and 60 seconds")
    return value


def delay_seconds(value: str) -> int:
    """argparse validator for the delay between separate alert messages."""
    try:
        delay = int(value)
        return validate_delay(delay, "delay")
    except (ValueError, ConfigurationError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def validate_check_interval(
    value: int,
    source: str = "check interval",
) -> int:
    if not 5 <= value <= 3600:
        raise ConfigurationError(
            f"{source} must be between 5 and 3600 seconds"
        )
    return value


def check_interval_seconds(value: str) -> int:
    """argparse validator for the interval between NWS checks."""
    try:
        interval = int(value)
        return validate_check_interval(interval, "check interval")
    except (ValueError, ConfigurationError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def latitude_value(value: str) -> float:
    try:
        latitude = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("latitude must be a number") from exc

    if not -90 <= latitude <= 90:
        raise argparse.ArgumentTypeError("latitude must be between -90 and 90")

    return latitude


def longitude_value(value: str) -> float:
    try:
        longitude = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("longitude must be a number") from exc

    if not -180 <= longitude <= 180:
        raise argparse.ArgumentTypeError("longitude must be between -180 and 180")

    return longitude


def ntfy_server_url(value: str) -> str:
    """Validate and normalize an ntfy server URL."""
    value = value.strip().rstrip("/")
    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(
            "ntfy server must be a complete HTTP or HTTPS URL"
        )

    return value


def _require_section(parser: configparser.ConfigParser, section: str) -> None:
    if not parser.has_section(section):
        raise ConfigurationError(f"configuration file is missing [{section}] section")


def _read_required_float(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
) -> float:
    try:
        raw = parser.get(section, option)
    except (configparser.NoSectionError, configparser.NoOptionError) as exc:
        raise ConfigurationError(f"missing required setting [{section}] {option}") from exc

    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"[{section}] {option} must be a number") from exc


def load_configuration(path: Path) -> FileConfiguration:
    """Read and validate the INI configuration file."""
    if not path.is_file():
        raise ConfigurationError(f"configuration file not found: {path}")

    parser = configparser.ConfigParser(interpolation=None)

    try:
        with path.open("r", encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except (OSError, configparser.Error) as exc:
        raise ConfigurationError(f"could not read configuration file {path}: {exc}") from exc

    for section in ("weather", "ntfy", "delivery"):
        _require_section(parser, section)

    latitude = _read_required_float(parser, "weather", "DEFAULT_LATITUDE")
    longitude = _read_required_float(parser, "weather", "DEFAULT_LONGITUDE")

    if not -90 <= latitude <= 90:
        raise ConfigurationError(
            "[weather] DEFAULT_LATITUDE must be between -90 and 90"
        )

    if not -180 <= longitude <= 180:
        raise ConfigurationError(
            "[weather] DEFAULT_LONGITUDE must be between -180 and 180"
        )

    topic = clean_optional(parser.get("ntfy", "TOPIC", fallback=""))

    server_raw = clean_optional(parser.get("ntfy", "SERVER", fallback=""))
    server = server_raw or DEFAULT_NTFY_SERVER
    try:
        server = ntfy_server_url(server)
    except argparse.ArgumentTypeError as exc:
        raise ConfigurationError(f"[ntfy] SERVER: {exc}") from exc

    token = clean_optional(parser.get("ntfy", "TOKEN", fallback=""))
    tags = parser.get("ntfy", "TAGS", fallback=DEFAULT_NTFY_TAGS).strip()

    priority = (
        parser.get("ntfy", "PRIORITY", fallback=DEFAULT_NTFY_PRIORITY)
        .strip()
        .lower()
        or DEFAULT_NTFY_PRIORITY
    )
    if priority not in NTFY_PRIORITY_CHOICES:
        choices = ", ".join(NTFY_PRIORITY_CHOICES)
        raise ConfigurationError(
            f"[ntfy] PRIORITY must be one of: {choices}"
        )

    try:
        delay = parser.getint(
            "delivery",
            "DELAY_SECONDS",
            fallback=DEFAULT_DELAY_SECONDS,
        )
    except ValueError as exc:
        raise ConfigurationError(
            "[delivery] DELAY_SECONDS must be an integer"
        ) from exc

    delay = validate_delay(delay, "[delivery] DELAY_SECONDS")

    try:
        check_interval = parser.getint(
            "delivery",
            "CHECK_INTERVAL",
            fallback=DEFAULT_CHECK_INTERVAL_SECONDS,
        )
    except ValueError as exc:
        raise ConfigurationError(
            "[delivery] CHECK_INTERVAL must be an integer"
        ) from exc

    check_interval = validate_check_interval(
        check_interval,
        "[delivery] CHECK_INTERVAL",
    )

    return FileConfiguration(
        latitude=latitude,
        longitude=longitude,
        ntfy_topic=topic,
        ntfy_server=server,
        ntfy_token=token,
        ntfy_tags=tags,
        ntfy_priority=priority,
        delay=delay,
        check_interval=check_interval,
    )


def parse_arguments() -> argparse.Namespace:
    """Load the config file, then allow explicit CLI options to override it."""
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        metavar="FILE",
        help=f"INI configuration file. Default: {DEFAULT_CONFIG_PATH}",
    )
    config_args, _ = config_parser.parse_known_args()

    try:
        config = load_configuration(config_args.config)
    except ConfigurationError as exc:
        config_parser.error(str(exc))

    parser = argparse.ArgumentParser(
        parents=[config_parser],
        description=(
            "Check for active National Weather Service alerts and optionally "
            "publish each alert to ntfy. Command-line values override config.ini."
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
            "ntfy notification priority. In auto mode, NWS severity is mapped "
            f"to an ntfy priority. Configured default: {config.ntfy_priority}."
        ),
    )

    parser.add_argument(
        "--ntfy-tags",
        default=config.ntfy_tags,
        metavar="TAGS",
        help="Comma-separated ntfy tags. Normally supplied by config.ini.",
    )

    args = parser.parse_args()

    if args.ntfy and args.ntfy_test:
        parser.error("--ntfy and --ntfy-test cannot be used together")

    if (args.ntfy or args.ntfy_test) and not clean_optional(args.ntfy_topic):
        parser.error(
            "[ntfy] TOPIC in the config file, or --ntfy-topic, is required "
            "when using --ntfy or --ntfy-test"
        )

    args.config = config_args.config
    args.ntfy_topic = clean_optional(args.ntfy_topic)
    args.ntfy_token = clean_optional(args.ntfy_token)
    args.ntfy_tags = args.ntfy_tags.strip()

    return args


def get_active_alerts(
    session: requests.Session,
    latitude: float,
    longitude: float,
) -> list[dict[str, Any]]:
    """Retrieve active NWS alerts covering the supplied coordinate."""
    response = session.get(
        NWS_API_URL,
        params={"point": f"{latitude:.4f},{longitude:.4f}"},
        headers=NWS_HEADERS,
        timeout=20,
    )
    response.raise_for_status()

    document = response.json()
    features = document.get("features", [])

    if not isinstance(features, list):
        raise ValueError("NWS response does not contain a valid features list")

    alerts: list[dict[str, Any]] = []

    for feature in features:
        if not isinstance(feature, dict):
            continue

        properties = feature.get("properties", {})

        if isinstance(properties, dict):
            alerts.append(properties)

    return alerts


def clean_field(value: Any, fallback: str) -> str:
    """Convert an alert field to normalized text."""
    if value is None:
        return fallback

    text = str(value).strip()
    return text if text else fallback


def build_compact_stdout_message(alert: dict[str, Any]) -> str:
    """Build the two-line compact record: event followed by headline."""
    event = clean_field(alert.get("event"), "Unknown weather alert")
    headline = clean_field(alert.get("headline"), "No headline provided.")
    return f"{event}\n{headline}"


def build_verbose_message(alert: dict[str, Any]) -> str:
    """Build complete human-readable output for one NWS alert."""
    event = clean_field(alert.get("event"), "Unknown")
    severity = clean_field(alert.get("severity"), "Unknown")
    urgency = clean_field(alert.get("urgency"), "Unknown")
    certainty = clean_field(alert.get("certainty"), "Unknown")
    message_type = clean_field(alert.get("messageType"), "Unknown")
    area = clean_field(alert.get("areaDesc"), "Unknown")
    sent = clean_field(alert.get("sent"), "Unknown")
    effective = clean_field(alert.get("effective"), "Unknown")
    onset = clean_field(alert.get("onset"), "Unknown")
    expires = clean_field(alert.get("expires"), "Unknown")
    ends = clean_field(alert.get("ends"), "Unknown")
    headline = clean_field(alert.get("headline"), "No headline provided.")
    alert_id = clean_field(alert.get("id") or alert.get("@id"), "Unknown")
    description = clean_field(
        alert.get("description"),
        "No description provided.",
    )
    instruction = clean_field(
        alert.get("instruction"),
        "No specific instructions provided.",
    )

    return (
        f"Event:        {event}\n"
        f"Severity:     {severity}\n"
        f"Urgency:      {urgency}\n"
        f"Certainty:    {certainty}\n"
        f"Message type: {message_type}\n"
        f"Area:         {area}\n"
        f"Sent:         {sent}\n"
        f"Effective:    {effective}\n"
        f"Onset:        {onset}\n"
        f"Expires:      {expires}\n"
        f"Ends:         {ends}\n"
        f"Headline:     {headline}\n"
        f"Alert ID:     {alert_id}\n"
        "\n"
        "Description:\n"
        f"{description}\n"
        "\n"
        "Instructions:\n"
        f"{instruction}"
    )


def ntfy_priority_for_alert(
    alert: dict[str, Any],
    configured_priority: str,
) -> str:
    """Map NWS severity to ntfy priority when configured as auto."""
    if configured_priority != "auto":
        return configured_priority

    severity = clean_field(alert.get("severity"), "Unknown").casefold()
    priority_map = {
        "extreme": "urgent",
        "severe": "high",
        "moderate": "default",
        "minor": "low",
        "unknown": "default",
    }
    return priority_map.get(severity, "default")


def build_ntfy_url(server: str, topic: str) -> str:
    """Build the ntfy topic URL."""
    encoded_topic = quote(topic.strip(), safe="-_")
    return f"{server.rstrip('/')}/{encoded_topic}"


def publish_ntfy_message(
    session: requests.Session,
    *,
    server: str,
    topic: str,
    title: str,
    message: str,
    priority: str,
    tags: str,
    token: str | None,
) -> str | None:
    """Publish one notification and return its ntfy message ID, if supplied."""
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "X-Title": title,
        "X-Priority": priority,
    }

    if tags:
        headers["X-Tags"] = tags

    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = session.post(
        build_ntfy_url(server, topic),
        headers=headers,
        data=message.encode("utf-8"),
        timeout=20,
    )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError:
        return None

    message_id = result.get("id")
    return str(message_id) if message_id else None


def publish_test_notification(
    session: requests.Session,
    args: argparse.Namespace,
) -> None:
    """Send a simple ntfy test notification."""
    LOGGER.info(
        "Sending ntfy test notification server=%s topic=%s",
        args.ntfy_server,
        args.ntfy_topic,
    )

    message_id = publish_ntfy_message(
        session,
        server=args.ntfy_server,
        topic=args.ntfy_topic,
        title="NWS Alert Test",
        message=(
            "ntfy delivery is working. No National Weather Service alert "
            "was required for this test."
        ),
        priority="default",
        tags=args.ntfy_tags,
        token=args.ntfy_token,
    )

    if message_id:
        LOGGER.info("ntfy test successful message_id=%s", message_id)
    else:
        LOGGER.info("ntfy test successful")


def alert_identity(alert: dict[str, Any]) -> str:
    """Return a stable identifier used to suppress duplicate notifications."""
    alert_id = clean_optional(
        str(alert.get("id") or alert.get("@id") or "")
    )
    if alert_id:
        return alert_id

    # Defensive fallback for malformed or incomplete alert records.
    return "|".join(
        (
            clean_field(alert.get("event"), "Unknown"),
            clean_field(alert.get("headline"), "No headline"),
            clean_field(alert.get("sent"), "Unknown"),
        )
    )


def process_alerts(
    session: requests.Session,
    alerts: list[dict[str, Any]],
    args: argparse.Namespace,
) -> tuple[int, set[str]]:
    """Print and optionally publish every new alert as a separate record."""
    failures = 0
    completed_ids: set[str] = set()
    total_alerts = len(alerts)

    for index, alert in enumerate(alerts, start=1):
        identity = alert_identity(alert)
        event = clean_field(alert.get("event"), "Unknown weather alert")
        headline = clean_field(alert.get("headline"), "No headline provided.")

        if args.verbose:
            stdout_message = build_verbose_message(alert)
            ntfy_message = stdout_message
        else:
            stdout_message = build_compact_stdout_message(alert)
            ntfy_message = headline

        print(stdout_message, flush=True)

        delivered = True
        if args.ntfy:
            priority = ntfy_priority_for_alert(alert, args.ntfy_priority)
            LOGGER.info(
                "Publishing new alert %d/%d event=%r priority=%s",
                index,
                total_alerts,
                event,
                priority,
            )

            try:
                message_id = publish_ntfy_message(
                    session,
                    server=args.ntfy_server,
                    topic=args.ntfy_topic,
                    title=event,
                    message=ntfy_message,
                    priority=priority,
                    tags=args.ntfy_tags,
                    token=args.ntfy_token,
                )

                if message_id:
                    LOGGER.info(
                        "ntfy delivery successful alert=%d/%d message_id=%s",
                        index,
                        total_alerts,
                        message_id,
                    )
                else:
                    LOGGER.info(
                        "ntfy delivery successful alert=%d/%d",
                        index,
                        total_alerts,
                    )

            except requests.RequestException as exc:
                delivered = False
                failures += 1
                LOGGER.error(
                    "ntfy delivery failed alert=%d/%d event=%r error=%s",
                    index,
                    total_alerts,
                    event,
                    exc,
                )

        if delivered:
            completed_ids.add(identity)

        if index < total_alerts:
            print(flush=True)
            LOGGER.info(
                "Waiting %d second(s) before the next notification",
                args.delay,
            )
            if STOP_EVENT.wait(args.delay):
                LOGGER.info("Stop requested during notification delay")
                break

    return failures, completed_ids


def perform_alert_check(
    session: requests.Session,
    args: argparse.Namespace,
    notified_alert_ids: set[str],
) -> tuple[int, set[str]]:
    """Perform one NWS query and deliver only alerts not already notified."""
    LOGGER.info(
        "Querying NWS active alerts latitude=%.5f longitude=%.5f",
        args.latitude,
        args.longitude,
    )

    try:
        alerts = get_active_alerts(
            session,
            args.latitude,
            args.longitude,
        )
    except requests.Timeout:
        LOGGER.error("NWS API request timed out")
        return 1, set()
    except requests.HTTPError as exc:
        status_code = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )
        response_text = (
            exc.response.text[:500]
            if exc.response is not None
            else ""
        )
        LOGGER.error(
            "NWS API failed HTTP status=%s response=%r error=%s",
            status_code,
            response_text,
            exc,
        )
        return 1, set()
    except requests.RequestException as exc:
        LOGGER.error("NWS API request failed error=%s", exc)
        return 1, set()
    except ValueError as exc:
        LOGGER.error("NWS returned invalid JSON error=%s", exc)
        return 1, set()

    new_alerts = [
        alert
        for alert in alerts
        if alert_identity(alert) not in notified_alert_ids
    ]

    LOGGER.info(
        "NWS returned %d active alert(s); %d new alert(s)",
        len(alerts),
        len(new_alerts),
    )

    if not new_alerts:
        LOGGER.info("No new NWS alerts; no notification sent")
        return 0, set()

    delivery_failures, completed_ids = process_alerts(
        session,
        new_alerts,
        args,
    )

    if delivery_failures:
        LOGGER.error(
            "Check completed with %d ntfy delivery failure(s); "
            "failed alerts will be retried on the next check",
            delivery_failures,
        )
        return 2, completed_ids

    LOGGER.info("Check completed successfully")
    return 0, completed_ids


def request_stop(signum: int, _frame: Any) -> None:
    """Handle Docker stop, SIGTERM, and Ctrl-C cleanly."""
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)

    LOGGER.info("Received %s; stopping after current operation", signal_name)
    STOP_EVENT.set()


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def main() -> int:
    configure_logging()
    args = parse_arguments()
    install_signal_handlers()

    mode = "ntfy-test" if args.ntfy_test else ("polling" if args.loop else "once")
    LOGGER.info(
        "Starting wx-alert mode=%s config=%s",
        mode,
        args.config,
    )

    with requests.Session() as session:
        if args.ntfy_test:
            try:
                publish_test_notification(session, args)
            except requests.Timeout:
                LOGGER.error("ntfy test request timed out")
                return 2
            except requests.HTTPError as exc:
                status_code = (
                    exc.response.status_code
                    if exc.response is not None
                    else "unknown"
                )
                response_text = (
                    exc.response.text[:500]
                    if exc.response is not None
                    else ""
                )
                LOGGER.error(
                    "ntfy test failed HTTP status=%s response=%r error=%s",
                    status_code,
                    response_text,
                    exc,
                )
                return 2
            except requests.RequestException as exc:
                LOGGER.error("ntfy test request failed error=%s", exc)
                return 2

            return 0

        notified_alert_ids: set[str] = set()

        if not args.loop:
            status, _completed_ids = perform_alert_check(
                session,
                args,
                notified_alert_ids,
            )
            return status

        LOGGER.info(
            "Polling enabled check_interval=%d seconds",
            args.check_interval,
        )
        check_number = 0

        while not STOP_EVENT.is_set():
            check_number += 1
            LOGGER.info("Beginning check cycle=%d", check_number)

            status, completed_ids = perform_alert_check(
                session,
                args,
                notified_alert_ids,
            )
            notified_alert_ids.update(completed_ids)

            if status != 0:
                LOGGER.warning(
                    "Check cycle=%d finished with status=%d; polling will continue",
                    check_number,
                    status,
                )

            if STOP_EVENT.is_set():
                break

            LOGGER.info(
                "Next NWS check in %d second(s)",
                args.check_interval,
            )
            STOP_EVENT.wait(args.check_interval)

        LOGGER.info("wx-alert stopped cleanly")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
