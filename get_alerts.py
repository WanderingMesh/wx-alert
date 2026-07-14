#!/usr/bin/env python3

from __future__ import annotations

import sys
from typing import Any

import requests

API_URL = "https://api.weather.gov/alerts/active"

# Central Reno example. Replace with the exact point you want to monitor.
LATITUDE = 45.80694 #39.5296
LONGITUDE = -108.5422 #-119.8138

HEADERS = {
    "User-Agent": "local-weather-alert-checker/1.0 (admin@example.org)",
    "Accept": "application/geo+json",
}


def get_active_alerts(latitude: float, longitude: float) -> list[dict[str, Any]]:
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
    ]


def main() -> int:
    try:
        alerts = get_active_alerts(LATITUDE, LONGITUDE)
    except requests.RequestException as exc:
        print(f"NWS API request failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"NWS returned invalid JSON: {exc}", file=sys.stderr)
        return 1

    if not alerts:
        print("No active NWS alerts.")
        return 0

    print(f"{len(alerts)} active NWS alert(s):\n")

    for alert in alerts:
        print(f"Event:       {alert.get('event', 'Unknown')}")
        print(f"Severity:    {alert.get('severity', 'Unknown')}")
        print(f"Urgency:     {alert.get('urgency', 'Unknown')}")
        print(f"Certainty:   {alert.get('certainty', 'Unknown')}")
        print(f"Area:        {alert.get('areaDesc', 'Unknown')}")
        print(f"Expires:     {alert.get('expires', 'Unknown')}")
        print(f"Headline:    {alert.get('headline', 'No headline')}")
        print(f"Alert ID:    {alert.get('id', alert.get('@id', 'Unknown'))}")
        print()
        print(alert.get("description", "No description provided."))

        instruction = alert.get("instruction")
        if instruction:
            print("\nInstructions:")
            print(instruction)

        print("\n" + "-" * 72 + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
