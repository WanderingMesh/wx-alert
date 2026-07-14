#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import requests

API_URL = "https://api.weather.gov/alerts/active"

# Central Reno example. Replace with the exact point you want to monitor.
LATITUDE = 45.80694 #39.5296
LONGITUDE = -108.5422 #-119.8138

# NWS requests should contain an identifiable User-Agent.
HEADERS = {
    "User-Agent": "local-weather-alert-checker/1.0 (admin@example.org)",
    "Accept": "application/geo+json",
}


def delay_seconds(value: str) -> int:
    """
    argparse validator for the delay between messages.
    """
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


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check for active National Weather Service alerts."
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Display complete alert information.",
    )

    parser.add_argument(
        "-d",
        "--delay",
        type=delay_seconds,
        default=3,
        metavar="SECONDS",
        help=(
            "Delay between multiple alert messages, from 1 to 60 seconds. "
            "Default: 1 second."
        ),
    )

    return parser.parse_args()


def get_active_alerts(
    latitude: float,
    longitude: float,
) -> list[dict[str, Any]]:
    """
    Retrieve active NWS alerts covering the supplied point.
    """
    response = requests.get(
        API_URL,
        params={"point": f"{latitude:.4f},{longitude:.4f}"},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()

    document = response.json()

    return [
        feature.get("properties", {})
        for feature in document.get("features", [])
        if isinstance(feature, dict)
    ]


def clean_field(value: Any, fallback: str) -> str:
    """
    Convert an alert field to clean text.

    This also removes leading and trailing whitespace that could interfere
    with message-length calculations or notification formatting.
    """
    if value is None:
        return fallback

    text = str(value).strip()
    return text if text else fallback


def build_compact_message(alert: dict[str, Any]) -> str:
    """
    Build the non-verbose message for one alert.

    No field labels are added. For example, the event field is emitted as:

        Extreme Heat Warning

    rather than:

        Event: Extreme Heat Warning
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
    """
    Build the complete human-readable output for one alert.
    """
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


def send_message(message: str) -> None:
    """
    Send or emit one alert message.

    Currently this writes one message to standard output. This function is
    deliberately isolated so it can later be replaced or extended with:

      * an ntfy HTTP POST
      * a MeshCore private-channel message
      * a MeshCore direct message
      * another notification transport
    """
    print(message, flush=True)


def process_alerts(
    alerts: list[dict[str, Any]],
    verbose: bool,
    delay: int,
) -> None:
    """
    Format and emit each alert as an independent message.
    """
    total_alerts = len(alerts)

    for index, alert in enumerate(alerts):
        if verbose:
            message = build_verbose_message(alert)
        else:
            message = build_compact_message(alert)

        send_message(message)

        # Separate records visually when writing to a terminal or log.
        if index < total_alerts - 1:
            print(flush=True)

            # Delay only between messages, never after the final message.
            time.sleep(delay)


def main() -> int:
    args = parse_arguments()

    try:
        alerts = get_active_alerts(LATITUDE, LONGITUDE)
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
        # Normal mode produces no output when there is nothing to notify.
        if args.verbose:
            print("No active NWS alerts.")

        return 0

    if args.verbose:
        print(f"{len(alerts)} active NWS alert(s):\n")

    process_alerts(
        alerts=alerts,
        verbose=args.verbose,
        delay=args.delay,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
