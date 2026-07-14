#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any
from urllib.parse import quote, urlparse

import requests

NWS_API_URL = "https://api.weather.gov/alerts/active"

# Default location: central Reno.
DEFAULT_LATITUDE = 45.80694 #39.5296
DEFAULT_LONGITUDE = -108.5422 #-119.8138

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


def delay_seconds(value: str) -> int:
    """Validate the delay between separate alert messages."""
    try:
        delay = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "delay must be an integer between 1 and 60 seconds"
        ) from exc

    if not 1 <= delay <= 60:
        raise argparse.ArgumentTypeError(
            "delay must be between 1 and 60 seconds"
        )

    return delay


def ntfy_server_url(value: str) -> str:
    """Validate and normalize an ntfy server URL."""
    value = value.strip().rstrip("/")
    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(
            "ntfy server must be a complete HTTP or HTTPS URL"
        )

    return value


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check for active National Weather Service alerts and optionally "
            "publish each alert to ntfy."
        )
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
        default=1,
        metavar="SECONDS",
        help=(
            "Delay between multiple alert messages, from 1 to 60 seconds. "
            "Default: 1 second."
        ),
    )

    parser.add_argument(
        "--latitude",
        type=float,
        default=DEFAULT_LATITUDE,
        help=f"Latitude to monitor. Default: {DEFAULT_LATITUDE}",
    )

    parser.add_argument(
        "--longitude",
        type=float,
        default=DEFAULT_LONGITUDE,
        help=f"Longitude to monitor. Default: {DEFAULT_LONGITUDE}",
    )

    parser.add_argument(
        "--ntfy",
        action="store_true",
        help="Publish each active alert to ntfy.",
    )

    parser.add_argument(
        "--ntfy-test",
        action="store_true",
        help="Send an ntfy test notification and exit.",
    )

    parser.add_argument(
        "--ntfy-server",
        type=ntfy_server_url,
        default=os.getenv("NTFY_SERVER", "https://ntfy.sh"),
        metavar="URL",
        help=(
            "ntfy server URL. May also be set with NTFY_SERVER. "
            "Default: https://ntfy.sh"
        ),
    )

    parser.add_argument(
        "--ntfy-topic",
        default=os.getenv("NTFY_TOPIC"),
        metavar="TOPIC",
        help="ntfy topic. May also be set with NTFY_TOPIC.",
    )

    parser.add_argument(
        "--ntfy-token",
        default=os.getenv("NTFY_TOKEN"),
        metavar="TOKEN",
        help=(
            "Optional ntfy access token. May also be set with NTFY_TOKEN. "
            "Using the environment variable is preferable."
        ),
    )

    parser.add_argument(
        "--ntfy-priority",
        choices=NTFY_PRIORITY_CHOICES,
        default=os.getenv("NTFY_PRIORITY", "auto"),
        help=(
            "ntfy notification priority. In auto mode, NWS severity is mapped "
            "to an ntfy priority. May also be set with NTFY_PRIORITY. "
            "Default: auto."
        ),
    )

    parser.add_argument(
        "--ntfy-tags",
        default=os.getenv("NTFY_TAGS", "warning,weather"),
        metavar="TAGS",
        help=(
            "Comma-separated ntfy tags. May also be set with NTFY_TAGS. "
            "Default: warning,weather"
        ),
    )

    args = parser.parse_args()

    if not -90 <= args.latitude <= 90:
        parser.error("--latitude must be between -90 and 90")

    if not -180 <= args.longitude <= 180:
        parser.error("--longitude must be between -180 and 180")

    if args.ntfy or args.ntfy_test:
        if not args.ntfy_topic:
            parser.error(
                "--ntfy-topic or the NTFY_TOPIC environment variable is "
                "required when using --ntfy or --ntfy-test"
            )

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
    """
    Build compact terminal output.

    The event and headline are on separate lines without field labels.
    """
    event = clean_field(
        alert.get("event"),
        "Unknown weather alert",
    )

    headline = clean_field(
        alert.get("headline"),
        "No headline provided.",
    )

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

    alert_id = clean_field(
        alert.get("id") or alert.get("@id"),
        "Unknown",
    )

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
    """
    Select the ntfy priority.

    In auto mode:

      NWS Extreme  -> ntfy urgent
      NWS Severe   -> ntfy high
      NWS Moderate -> ntfy default
      NWS Minor    -> ntfy low
      Unknown      -> ntfy default
    """
    if configured_priority != "auto":
        return configured_priority

    severity = clean_field(
        alert.get("severity"),
        "Unknown",
    ).casefold()

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
    """
    Publish one notification to ntfy.

    Returns the ntfy message ID when the server supplies one.
    """
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "X-Title": title,
        "X-Priority": priority,
    }

    if tags.strip():
        headers["X-Tags"] = tags.strip()

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

    print("ntfy test notification sent successfully.")

    if args.verbose and message_id:
        print(f"ntfy message ID: {message_id}")


def process_alerts(
    session: requests.Session,
    alerts: list[dict[str, Any]],
    args: argparse.Namespace,
) -> int:
    """
    Print and optionally publish every alert as a separate record.

    Returns the number of ntfy delivery failures.
    """
    failures = 0
    total_alerts = len(alerts)

    for index, alert in enumerate(alerts):
        event = clean_field(
            alert.get("event"),
            "Unknown weather alert",
        )

        headline = clean_field(
            alert.get("headline"),
            "No headline provided.",
        )

        if args.verbose:
            stdout_message = build_verbose_message(alert)
            ntfy_message = stdout_message
        else:
            stdout_message = build_compact_stdout_message(alert)

            # The event is already used as the ntfy title, so only the
            # headline needs to be placed in the ntfy message body.
            ntfy_message = headline

        print(stdout_message, flush=True)

        if args.ntfy:
            try:
                message_id = publish_ntfy_message(
                    session,
                    server=args.ntfy_server,
                    topic=args.ntfy_topic,
                    title=event,
                    message=ntfy_message,
                    priority=ntfy_priority_for_alert(
                        alert,
                        args.ntfy_priority,
                    ),
                    tags=args.ntfy_tags,
                    token=args.ntfy_token,
                )

                if args.verbose:
                    if message_id:
                        print(
                            f"\nntfy delivery successful: {message_id}",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        print(
                            "\nntfy delivery successful.",
                            file=sys.stderr,
                            flush=True,
                        )

            except requests.RequestException as exc:
                failures += 1
                print(
                    f"ntfy delivery failed for {event}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

        if index < total_alerts - 1:
            # Visual separation between stdout records.
            print(flush=True)

            # Delay only between separate alerts, never after the last one.
            time.sleep(args.delay)

    return failures


def main() -> int:
    args = parse_arguments()

    with requests.Session() as session:
        if args.ntfy_test:
            try:
                publish_test_notification(session, args)
            except requests.Timeout:
                print(
                    "ntfy test request timed out.",
                    file=sys.stderr,
                )
                return 2
            except requests.HTTPError as exc:
                status_code = (
                    exc.response.status_code
                    if exc.response is not None
                    else "unknown"
                )
                print(
                    f"ntfy returned HTTP status {status_code}: {exc}",
                    file=sys.stderr,
                )
                return 2
            except requests.RequestException as exc:
                print(
                    f"ntfy test request failed: {exc}",
                    file=sys.stderr,
                )
                return 2

            return 0

        try:
            alerts = get_active_alerts(
                session,
                args.latitude,
                args.longitude,
            )
        except requests.Timeout:
            print(
                "NWS API request timed out.",
                file=sys.stderr,
            )
            return 1
        except requests.HTTPError as exc:
            status_code = (
                exc.response.status_code
                if exc.response is not None
                else "unknown"
            )
            print(
                f"NWS API returned HTTP status {status_code}: {exc}",
                file=sys.stderr,
            )
            return 1
        except requests.RequestException as exc:
            print(
                f"NWS API request failed: {exc}",
                file=sys.stderr,
            )
            return 1
        except ValueError as exc:
            print(
                f"NWS returned invalid JSON: {exc}",
                file=sys.stderr,
            )
            return 1

        if not alerts:
            if args.verbose:
                print("No active NWS alerts.")

            return 0

        if args.verbose:
            print(f"{len(alerts)} active NWS alert(s):\n")

        delivery_failures = process_alerts(
            session,
            alerts,
            args,
        )

        if delivery_failures:
            return 2

        return 0


if __name__ == "__main__":
    raise SystemExit(main())
