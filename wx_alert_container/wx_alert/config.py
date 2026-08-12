"""Configuration file loading and validation.

Validation is deliberately strict and fails at startup. A container that
refuses to boot on a bad setting is far easier to diagnose than one that runs
for a week and quietly never delivers anything.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .nws import EVENT_CLASS_RANK, SEVERITY_RANK
from .state import DEFAULT_RETENTION_DAYS, DEFAULT_STATE_FILE
from .text import clean_optional

DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_NTFY_TAGS = "weather"
DEFAULT_NTFY_PRIORITY = "auto"
DEFAULT_DELAY_SECONDS = 1
DEFAULT_CHECK_INTERVAL_SECONDS = 3600
DEFAULT_STARTUP_MAX_AGE_SECONDS = 900
DEFAULT_ALWAYS_NOTIFY_WARNINGS = True

# MeshCore defaults are deliberately conservative. Airtime on a shared LoRa
# channel is a common resource, so the out-of-box behavior carries only
# products that are actually happening, at a modest rate.
DEFAULT_MESHCORE_BAUD = 115200
DEFAULT_MESHCORE_CHANNEL = 0
DEFAULT_MESHCORE_MIN_CLASS = "warning"
DEFAULT_MESHCORE_MIN_SEVERITY = "severe"
DEFAULT_MESHCORE_MIN_INTERVAL = 30
DEFAULT_MESHCORE_MAX_PER_HOUR = 12
DEFAULT_MESHCORE_STARTUP_MAX_AGE = 900
DEFAULT_MESHCORE_SEND_TIMEOUT = 30
DEFAULT_MESHCORE_CONNECT_TIMEOUT = 30

NTFY_PRIORITY_CHOICES = (
    "auto",
    "min",
    "low",
    "default",
    "high",
    "max",
    "urgent",
)


class ConfigurationError(ValueError):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class MeshCoreConfiguration:
    """Settings for the MeshCore radio transport."""

    enabled: bool
    port: str | None
    baud: int
    channel_index: int
    minimum_class: str
    minimum_severity: str
    min_interval_seconds: int
    max_per_hour: int
    startup_max_age: int
    connect_timeout: int
    send_timeout: int


@dataclass(frozen=True)
class FileConfiguration:
    latitude: float
    longitude: float
    ntfy_topic: str | None
    ntfy_server: str
    ntfy_token: str | None
    ntfy_tags: str
    ntfy_priority: str
    ntfy_startup_max_age: int
    delay: int
    check_interval: int
    always_notify_warnings_on_startup: bool
    state_file: Path
    state_retention_days: int
    meshcore: MeshCoreConfiguration


def validate_delay(value: int, source: str = "delay") -> int:
    if not 1 <= value <= 60:
        raise ConfigurationError(f"{source} must be between 1 and 60 seconds")
    return value


def validate_check_interval(value: int, source: str = "check interval") -> int:
    if not 5 <= value <= 3600:
        raise ConfigurationError(
            f"{source} must be between 5 and 3600 seconds"
        )
    return value


def validate_nonnegative(value: int, source: str) -> int:
    if value < 0:
        raise ConfigurationError(f"{source} must be zero or greater")
    return value


def validate_retention_days(value: int, source: str = "retention days") -> int:
    if not 1 <= value <= 365:
        raise ConfigurationError(f"{source} must be between 1 and 365")
    return value


def validate_latitude(value: float, source: str = "latitude") -> float:
    if not -90 <= value <= 90:
        raise ConfigurationError(f"{source} must be between -90 and 90")
    return value


def validate_longitude(value: float, source: str = "longitude") -> float:
    if not -180 <= value <= 180:
        raise ConfigurationError(f"{source} must be between -180 and 180")
    return value


def normalize_server_url(value: str, source: str = "server") -> str:
    """Validate and normalize an HTTP(S) server URL."""
    value = value.strip().rstrip("/")
    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            f"{source} must be a complete HTTP or HTTPS URL"
        )

    return value


def load_meshcore_configuration(
    parser: configparser.ConfigParser,
) -> MeshCoreConfiguration:
    """Read the optional [meshcore] section.

    The section may be absent entirely, in which case the transport is simply
    disabled. When it is present and enabled, every setting is validated, so
    a typo cannot silently degrade into broadcasting the wrong products onto
    a shared channel.
    """
    enabled = _read_bool(parser, "meshcore", "ENABLED", False)
    port = clean_optional(parser.get("meshcore", "PORT", fallback=""))

    minimum_class = (
        parser.get("meshcore", "MIN_CLASS", fallback=DEFAULT_MESHCORE_MIN_CLASS)
        .strip()
        .lower()
        or DEFAULT_MESHCORE_MIN_CLASS
    )
    if minimum_class not in EVENT_CLASS_RANK:
        choices = ", ".join(sorted(EVENT_CLASS_RANK))
        raise ConfigurationError(f"[meshcore] MIN_CLASS must be one of: {choices}")

    minimum_severity = (
        parser.get(
            "meshcore",
            "MIN_SEVERITY",
            fallback=DEFAULT_MESHCORE_MIN_SEVERITY,
        )
        .strip()
        .lower()
        or DEFAULT_MESHCORE_MIN_SEVERITY
    )
    if minimum_severity not in SEVERITY_RANK:
        choices = ", ".join(sorted(SEVERITY_RANK))
        raise ConfigurationError(
            f"[meshcore] MIN_SEVERITY must be one of: {choices}"
        )

    channel_index = _read_int(
        parser, "meshcore", "CHANNEL_INDEX", DEFAULT_MESHCORE_CHANNEL
    )
    if not 0 <= channel_index <= 255:
        raise ConfigurationError(
            "[meshcore] CHANNEL_INDEX must be between 0 and 255"
        )

    baud = _read_int(parser, "meshcore", "BAUD", DEFAULT_MESHCORE_BAUD)
    if baud <= 0:
        raise ConfigurationError("[meshcore] BAUD must be a positive integer")

    return MeshCoreConfiguration(
        enabled=enabled,
        port=port,
        baud=baud,
        channel_index=channel_index,
        minimum_class=minimum_class,
        minimum_severity=minimum_severity,
        min_interval_seconds=validate_nonnegative(
            _read_int(
                parser,
                "meshcore",
                "MIN_SECONDS_BETWEEN_SENDS",
                DEFAULT_MESHCORE_MIN_INTERVAL,
            ),
            "[meshcore] MIN_SECONDS_BETWEEN_SENDS",
        ),
        max_per_hour=validate_nonnegative(
            _read_int(
                parser,
                "meshcore",
                "MAX_SENDS_PER_HOUR",
                DEFAULT_MESHCORE_MAX_PER_HOUR,
            ),
            "[meshcore] MAX_SENDS_PER_HOUR",
        ),
        startup_max_age=validate_nonnegative(
            _read_int(
                parser,
                "meshcore",
                "STARTUP_MAX_AGE_SECONDS",
                DEFAULT_MESHCORE_STARTUP_MAX_AGE,
            ),
            "[meshcore] STARTUP_MAX_AGE_SECONDS",
        ),
        connect_timeout=validate_nonnegative(
            _read_int(
                parser,
                "meshcore",
                "CONNECT_TIMEOUT",
                DEFAULT_MESHCORE_CONNECT_TIMEOUT,
            ),
            "[meshcore] CONNECT_TIMEOUT",
        ),
        send_timeout=validate_nonnegative(
            _read_int(
                parser,
                "meshcore",
                "SEND_TIMEOUT",
                DEFAULT_MESHCORE_SEND_TIMEOUT,
            ),
            "[meshcore] SEND_TIMEOUT",
        ),
    )


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
        raise ConfigurationError(
            f"missing required setting [{section}] {option}"
        ) from exc

    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"[{section}] {option} must be a number") from exc


def _read_int(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    fallback: int,
) -> int:
    try:
        return parser.getint(section, option, fallback=fallback)
    except ValueError as exc:
        raise ConfigurationError(
            f"[{section}] {option} must be an integer"
        ) from exc


def _read_bool(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    fallback: bool,
) -> bool:
    try:
        return parser.getboolean(section, option, fallback=fallback)
    except ValueError as exc:
        raise ConfigurationError(
            f"[{section}] {option} must be true or false"
        ) from exc


def read_config_file(path: Path) -> configparser.ConfigParser:
    """Open and parse the INI file, raising ConfigurationError on any problem."""
    if not path.is_file():
        raise ConfigurationError(f"configuration file not found: {path}")

    parser = configparser.ConfigParser(interpolation=None)

    try:
        with path.open("r", encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except (OSError, configparser.Error) as exc:
        raise ConfigurationError(
            f"could not read configuration file {path}: {exc}"
        ) from exc

    return parser


def load_configuration(parser: configparser.ConfigParser) -> FileConfiguration:
    """Validate the core configuration sections."""
    for section in ("weather", "ntfy", "delivery"):
        _require_section(parser, section)

    latitude = validate_latitude(
        _read_required_float(parser, "weather", "DEFAULT_LATITUDE"),
        "[weather] DEFAULT_LATITUDE",
    )
    longitude = validate_longitude(
        _read_required_float(parser, "weather", "DEFAULT_LONGITUDE"),
        "[weather] DEFAULT_LONGITUDE",
    )

    topic = clean_optional(parser.get("ntfy", "TOPIC", fallback=""))

    server_raw = clean_optional(parser.get("ntfy", "SERVER", fallback=""))
    server = normalize_server_url(
        server_raw or DEFAULT_NTFY_SERVER,
        "[ntfy] SERVER",
    )

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
        raise ConfigurationError(f"[ntfy] PRIORITY must be one of: {choices}")

    delay = validate_delay(
        _read_int(parser, "delivery", "DELAY_SECONDS", DEFAULT_DELAY_SECONDS),
        "[delivery] DELAY_SECONDS",
    )
    check_interval = validate_check_interval(
        _read_int(
            parser,
            "delivery",
            "CHECK_INTERVAL",
            DEFAULT_CHECK_INTERVAL_SECONDS,
        ),
        "[delivery] CHECK_INTERVAL",
    )

    startup_max_age = validate_nonnegative(
        _read_int(
            parser,
            "delivery",
            "STARTUP_MAX_AGE_SECONDS",
            DEFAULT_STARTUP_MAX_AGE_SECONDS,
        ),
        "[delivery] STARTUP_MAX_AGE_SECONDS",
    )
    always_notify_warnings = _read_bool(
        parser,
        "delivery",
        "ALWAYS_NOTIFY_WARNINGS_ON_STARTUP",
        DEFAULT_ALWAYS_NOTIFY_WARNINGS,
    )

    state_file_raw = parser.get(
        "state",
        "STATE_FILE",
        fallback=str(DEFAULT_STATE_FILE),
    ).strip()
    state_file = Path(state_file_raw or DEFAULT_STATE_FILE)

    retention_days = validate_retention_days(
        _read_int(parser, "state", "RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
        "[state] RETENTION_DAYS",
    )

    return FileConfiguration(
        latitude=latitude,
        longitude=longitude,
        ntfy_topic=topic,
        ntfy_server=server,
        ntfy_token=token,
        ntfy_tags=tags,
        ntfy_priority=priority,
        ntfy_startup_max_age=startup_max_age,
        delay=delay,
        check_interval=check_interval,
        always_notify_warnings_on_startup=always_notify_warnings,
        state_file=state_file,
        state_retention_days=retention_days,
        meshcore=load_meshcore_configuration(parser),
    )
