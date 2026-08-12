"""The polling loop that turns NWS alerts into deliveries."""

from __future__ import annotations

import argparse
import logging
from typing import Any, Sequence

import requests

from .formatting import build_compact_stdout_message, build_verbose_message
from .nws import (
    alert_age_seconds,
    alert_class,
    alert_identity,
    alert_sent_time,
    format_age,
    get_active_alerts,
)
from .shutdown import STOP_EVENT
from .text import clean_field
from .transports.base import DeliveryResult, Transport

LOGGER = logging.getLogger("wx-alert")

# Exit and cycle status codes.
STATUS_OK = 0
STATUS_SOURCE_FAILURE = 1
STATUS_DELIVERY_FAILURE = 2


def fetch_alerts(
    session: requests.Session,
    latitude: float,
    longitude: float,
) -> list[dict[str, Any]] | None:
    """Query NWS, logging and absorbing every expected failure mode.

    Returns None when the query failed. An empty list is a valid result
    meaning "no active alerts", which is why None is used for failure.
    """
    LOGGER.info(
        "Querying NWS active alerts latitude=%.5f longitude=%.5f",
        latitude,
        longitude,
    )

    try:
        return get_active_alerts(session, latitude, longitude)
    except requests.Timeout:
        LOGGER.error("NWS API request timed out")
    except requests.HTTPError as exc:
        status_code = (
            exc.response.status_code if exc.response is not None else "unknown"
        )
        response_text = exc.response.text[:500] if exc.response is not None else ""
        LOGGER.error(
            "NWS API failed HTTP status=%s response=%r error=%s",
            status_code,
            response_text,
            exc,
        )
    except requests.RequestException as exc:
        LOGGER.error("NWS API request failed error=%s", exc)
    except ValueError as exc:
        LOGGER.error("NWS returned invalid JSON error=%s", exc)

    return None


def deliver_alert(
    alert: dict[str, Any],
    transports: Sequence[Transport],
) -> tuple[int, set[str]]:
    """Offer one alert to every transport.

    Transports are independent: a radio failure must not suppress the ntfy
    notification, and vice versa. Returns the failure count and the names of
    the transports that reached a terminal outcome for this alert.
    """
    event = clean_field(alert.get("event"), "Unknown weather alert")
    failures = 0
    settled: set[str] = set()

    for transport in transports:
        try:
            result, detail = transport.deliver(alert)
        except Exception as exc:  # noqa: BLE001 - a transport bug must not
            # take down the poller; the alert simply stays eligible for retry.
            failures += 1
            LOGGER.exception(
                "Transport raised unexpectedly transport=%s event=%r error=%s",
                transport.name,
                event,
                exc,
            )
            continue

        if result is DeliveryResult.SENT:
            settled.add(transport.name)
            LOGGER.info(
                "Delivery succeeded transport=%s event=%r %s",
                transport.name,
                event,
                detail,
            )
        elif result is DeliveryResult.SKIPPED:
            # A deliberate decision, not an error. Recorded as settled so the
            # alert is never reconsidered by this transport.
            settled.add(transport.name)
            LOGGER.info(
                "Delivery skipped transport=%s event=%r reason=%s",
                transport.name,
                event,
                detail,
            )
        else:
            failures += 1
            LOGGER.error(
                "Delivery failed transport=%s event=%r error=%s",
                transport.name,
                event,
                detail,
            )

    return failures, settled


def process_alerts(
    alerts: list[dict[str, Any]],
    transports: Sequence[Transport],
    args: argparse.Namespace,
) -> tuple[int, set[str]]:
    """Print and deliver every new alert as a separate record."""
    failures = 0
    completed_ids: set[str] = set()
    total_alerts = len(alerts)

    for index, alert in enumerate(alerts, start=1):
        identity = alert_identity(alert)
        event = clean_field(alert.get("event"), "Unknown weather alert")
        sent = alert_sent_time(alert)

        stdout_message = (
            build_verbose_message(alert)
            if args.verbose
            else build_compact_stdout_message(alert)
        )
        print(stdout_message, flush=True)

        LOGGER.info(
            "Processing alert %d/%d event=%r class=%s nws_sent=%s age=%s",
            index,
            total_alerts,
            event,
            alert_class(alert),
            sent.isoformat() if sent else "unknown",
            format_age(alert_age_seconds(alert)),
        )

        alert_failures, settled = deliver_alert(alert, transports)
        failures += alert_failures

        # Only suppress future notification when every transport reached a
        # terminal outcome. If any transport failed, the alert must remain
        # eligible so the next cycle retries it.
        if transports and len(settled) == len(transports):
            completed_ids.add(identity)
        elif not transports:
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
    transports: Sequence[Transport],
    notified_alert_ids: set[str],
) -> tuple[int, set[str]]:
    """Perform one NWS query and deliver only alerts not already notified."""
    alerts = fetch_alerts(session, args.latitude, args.longitude)
    if alerts is None:
        return STATUS_SOURCE_FAILURE, set()

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
        return STATUS_OK, set()

    delivery_failures, completed_ids = process_alerts(
        new_alerts,
        transports,
        args,
    )

    if delivery_failures:
        LOGGER.error(
            "Check completed with %d delivery failure(s); "
            "failed alerts will be retried on the next check",
            delivery_failures,
        )
        return STATUS_DELIVERY_FAILURE, completed_ids

    LOGGER.info("Check completed successfully")
    return STATUS_OK, completed_ids


def run_polling_loop(
    session: requests.Session,
    args: argparse.Namespace,
    transports: Sequence[Transport],
) -> int:
    """Poll until asked to stop, continuing through temporary failures."""
    notified_alert_ids: set[str] = set()

    if not args.loop:
        status, _completed = perform_alert_check(
            session,
            args,
            transports,
            notified_alert_ids,
        )
        return status

    LOGGER.info("Polling enabled check_interval=%d seconds", args.check_interval)
    check_number = 0

    while not STOP_EVENT.is_set():
        check_number += 1
        LOGGER.info("Beginning check cycle=%d", check_number)

        status, completed_ids = perform_alert_check(
            session,
            args,
            transports,
            notified_alert_ids,
        )
        notified_alert_ids.update(completed_ids)

        if status != STATUS_OK:
            LOGGER.warning(
                "Check cycle=%d finished with status=%d; polling will continue",
                check_number,
                status,
            )

        if STOP_EVENT.is_set():
            break

        LOGGER.info("Next NWS check in %d second(s)", args.check_interval)
        STOP_EVENT.wait(args.check_interval)

    LOGGER.info("wx-alert stopped cleanly")
    return STATUS_OK
