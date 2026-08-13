"""The polling loop that turns NWS alerts into deliveries."""

from __future__ import annotations

import argparse
import logging
from typing import Any, Sequence

import requests

from .formatting import build_compact_stdout_message, build_verbose_message
from .geo import geometry_distance_km
from .health import heartbeat_path, write_heartbeat
from .nws import (
    EVENT_CLASS_RANK,
    alert_age_seconds,
    alert_class,
    alert_geometry,
    alert_sent_time,
    alert_severity_rank,
    format_age,
    get_active_alerts,
)
from .shutdown import STOP_EVENT
from .state import AlertState, StateError, prune_state, save_state
from .text import clean_field
from .transports.base import DeliveryContext, DeliveryResult, Transport

LOGGER = logging.getLogger("wx-alert")

# Exit and cycle status codes.
STATUS_OK = 0
STATUS_SOURCE_FAILURE = 1
STATUS_DELIVERY_FAILURE = 2
STATUS_STATE_FAILURE = 3
STATUS_TRANSPORT_UNHEALTHY = 4

# With no transport enabled the program is a console viewer. Alert processing
# still needs a name to record against, or polling would reprint the same
# alert every cycle forever.
CONSOLE_TRANSPORT_NAME = "console"


def settlement_names(transports: Sequence[Transport]) -> list[str]:
    """Names an alert must be settled against before it is considered done."""
    return [transport.name for transport in transports] or [CONSOLE_TRANSPORT_NAME]


def fetch_alerts(
    session: requests.Session,
    zones: Sequence[str],
) -> list[dict[str, Any]] | None:
    """Query NWS, logging and absorbing every expected failure mode.

    Returns None when the query failed. An empty list is a valid result
    meaning "no active alerts", which is why None is used for failure.
    """
    LOGGER.info("Querying NWS active alerts zones=%s", ",".join(zones))

    try:
        return get_active_alerts(session, zones)
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


def filter_by_proximity(
    alerts: list[dict[str, Any]],
    latitude: float,
    longitude: float,
    radius_km: float,
) -> list[dict[str, Any]]:
    """Drop polygon warnings whose warned area is too far to be relevant.

    Alerts are fetched by county because that is the only query form that
    returns storm-based warnings at all, but a county can be 300 km long. This
    restores local relevance without narrowing the fetch.

    An alert with no polygon is kept unconditionally. Watches, advisories, and
    statements are issued to a whole zone, so NWS has already decided they
    apply to the queried county; there is no geometry to be far away.
    """
    if radius_km <= 0:
        return alerts

    kept: list[dict[str, Any]] = []

    for alert in alerts:
        distance = geometry_distance_km(alert_geometry(alert), latitude, longitude)

        if distance is None or distance <= radius_km:
            kept.append(alert)
            continue

        LOGGER.info(
            "Alert is outside the monitored radius event=%r distance=%.1fkm "
            "radius=%.1fkm area=%r",
            clean_field(alert.get("event"), "Unknown weather alert"),
            distance,
            radius_km,
            clean_field(alert.get("areaDesc"), "unknown area"),
        )

    return kept


def prioritize(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order alerts loudest first, by product class then CAP severity.

    NWS returns active alerts in no particular order of importance, and a
    county query returns several at once. Anything downstream that runs out of
    budget mid-batch — the radio's hourly cap, or a cycle cut short by a
    shutdown — therefore used to spend what it had on whichever product
    happened to be listed first. A Special Weather Statement could consume the
    airtime that a Tornado Warning in the same response needed.

    Python's sort is stable, so alerts of equal rank keep the order NWS gave.
    """
    return sorted(
        alerts,
        key=lambda alert: (
            -EVENT_CLASS_RANK[alert_class(alert)],
            -alert_severity_rank(alert),
        ),
    )


def deliver_alert(
    alert: dict[str, Any],
    transports: Sequence[Transport],
    state: AlertState,
    context: DeliveryContext,
    failed_transports: set[str] | None = None,
) -> int:
    """Offer one alert to every transport that has not already handled it.

    Transports are independent: a radio failure must not suppress the ntfy
    notification, and vice versa. Every terminal outcome is recorded against
    that transport alone. Returns the number of failures.
    """
    event = clean_field(alert.get("event"), "Unknown weather alert")
    failures = 0

    if not transports:
        state.record(alert, CONSOLE_TRANSPORT_NAME, "printed")
        return 0

    for transport in transports:
        if state.is_settled(alert, transport.name):
            LOGGER.debug(
                "Already handled transport=%s event=%r",
                transport.name,
                event,
            )
            continue

        try:
            result, detail = transport.deliver(
                alert,
                # Whether this transport has had a turn at this alert before is
                # what lets its startup staleness policy tell a cold-start
                # backlog from a retry after a failure.
                context.for_transport(state.has_record(alert, transport.name)),
            )
        except Exception as exc:  # noqa: BLE001 - a transport bug must not
            # take down the poller; the alert simply stays eligible for retry.
            failures += 1
            if failed_transports is not None:
                failed_transports.add(transport.name)
            LOGGER.exception(
                "Transport raised unexpectedly transport=%s event=%r error=%s",
                transport.name,
                event,
                exc,
            )
            continue

        if result is DeliveryResult.SENT:
            LOGGER.info(
                "Delivery succeeded transport=%s event=%r %s",
                transport.name,
                event,
                detail,
            )
            state.record(alert, transport.name, "delivered")
        elif result is DeliveryResult.SKIPPED:
            # A deliberate decision, not an error. Recorded so this transport
            # never reconsiders this version of the alert.
            LOGGER.warning(
                "Delivery skipped transport=%s event=%r reason=%s",
                transport.name,
                event,
                detail,
            )
            state.record(alert, transport.name, f"skipped:{detail}")
        elif result is DeliveryResult.DEFERRED:
            # Deliberately not recorded, and deliberately not a failure. The
            # transport would have carried this alert but could not right now,
            # for a reason that clears without intervention, so it stays
            # outstanding and is offered again next cycle. Recording it would
            # retire the alert over a condition that has already passed;
            # counting it as a failure would eventually restart the container
            # over one that was never an error.
            LOGGER.warning(
                "Delivery deferred to the next cycle transport=%s event=%r "
                "reason=%s",
                transport.name,
                event,
                detail,
            )
        else:
            failures += 1
            if failed_transports is not None:
                failed_transports.add(transport.name)
            LOGGER.error(
                "Delivery failed transport=%s event=%r error=%s",
                transport.name,
                event,
                detail,
            )

    return failures


def process_alerts(
    alerts: list[dict[str, Any]],
    transports: Sequence[Transport],
    state: AlertState,
    args: argparse.Namespace,
    context: DeliveryContext,
    failed_transports: set[str] | None = None,
) -> int:
    """Print and deliver every new alert as a separate record."""
    failures = 0
    total_alerts = len(alerts)

    for index, alert in enumerate(alerts, start=1):
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
            format_age(alert_age_seconds(alert, context.now)),
        )

        failures += deliver_alert(
            alert,
            transports,
            state,
            context,
            failed_transports,
        )

        # Persist after each alert rather than at the end of the cycle. A
        # crash between two deliveries would otherwise replay the ones
        # already sent.
        try:
            save_state(args.state_file, state)
        except StateError as exc:
            failures += 1
            LOGGER.error(
                "Alert was processed but state could not be saved; a restart "
                "may cause a duplicate notification error=%s",
                exc,
            )

        if index < total_alerts:
            print(flush=True)
            LOGGER.info(
                "Waiting %d second(s) before the next notification",
                args.delay,
            )
            if STOP_EVENT.wait(args.delay):
                LOGGER.info("Stop requested during notification delay")
                break

    return failures


def perform_alert_check(
    session: requests.Session,
    args: argparse.Namespace,
    transports: Sequence[Transport],
    state: AlertState,
    *,
    first_cycle: bool,
    failed_transports: set[str] | None = None,
) -> int:
    """Perform one NWS query and deliver whatever is outstanding."""
    alerts = fetch_alerts(session, args.query_zones)
    if alerts is None:
        return STATUS_SOURCE_FAILURE

    # Filtered before deduplication, so a distant warning is never recorded as
    # handled. If the storm moves closer on a later cycle it is still new work.
    alerts = filter_by_proximity(
        alerts,
        args.latitude,
        args.longitude,
        args.alert_radius_km,
    )

    removed = prune_state(state, args.state_retention_days)
    if removed:
        LOGGER.info("Pruned %d expired state record(s)", removed)

    context = DeliveryContext.create(first_cycle=first_cycle)

    # An alert is outstanding while any enabled transport has yet to reach a
    # terminal outcome for this exact version of it. Ordered loudest first, so
    # a transport that runs out of airtime part way through a batch spends what
    # it had on the most urgent products rather than the earliest-listed ones.
    names = settlement_names(transports)
    outstanding = prioritize(
        [
            alert
            for alert in alerts
            if any(not state.is_settled(alert, name) for name in names)
        ]
    )

    LOGGER.info(
        "NWS returned %d active alert(s); %d outstanding; %d already handled",
        len(alerts),
        len(outstanding),
        len(alerts) - len(outstanding),
    )

    if not outstanding:
        if removed:
            try:
                save_state(args.state_file, state)
            except StateError as exc:
                LOGGER.error("Could not save pruned state error=%s", exc)
                return STATUS_STATE_FAILURE
        LOGGER.info("Nothing outstanding; no notification sent")
        return STATUS_OK

    delivery_failures = process_alerts(
        outstanding,
        transports,
        state,
        args,
        context,
        failed_transports,
    )

    if delivery_failures:
        LOGGER.error(
            "Check completed with %d delivery failure(s); "
            "failed alerts will be retried on the next check",
            delivery_failures,
        )
        return STATUS_DELIVERY_FAILURE

    LOGGER.info("Check completed successfully")
    return STATUS_OK


def run_polling_loop(
    session: requests.Session,
    args: argparse.Namespace,
    transports: Sequence[Transport],
    state: AlertState,
) -> int:
    """Poll until asked to stop, continuing through temporary failures."""
    if not args.loop:
        return perform_alert_check(
            session,
            args,
            transports,
            state,
            first_cycle=True,
        )

    LOGGER.info(
        "Polling enabled check_interval=%d seconds state_file=%s",
        args.check_interval,
        args.state_file,
    )
    check_number = 0
    heartbeat = heartbeat_path(args.state_file)
    consecutive_failures: dict[str, int] = {}

    while not STOP_EVENT.is_set():
        check_number += 1
        LOGGER.info("Beginning check cycle=%d", check_number)

        failed_transports: set[str] = set()

        try:
            status = perform_alert_check(
                session,
                args,
                transports,
                state,
                first_cycle=(check_number == 1),
                failed_transports=failed_transports,
            )
        except StateError as exc:
            LOGGER.error("Persistent state operation failed error=%s", exc)
            status = STATUS_STATE_FAILURE

        for transport in transports:
            if transport.name in failed_transports:
                consecutive_failures[transport.name] = (
                    consecutive_failures.get(transport.name, 0) + 1
                )
            else:
                consecutive_failures[transport.name] = 0

        write_heartbeat(
            heartbeat,
            check_interval=args.check_interval,
            cycle=check_number,
            consecutive_failures=consecutive_failures,
        )

        if status != STATUS_OK:
            LOGGER.warning(
                "Check cycle=%d finished with status=%d; polling will continue",
                check_number,
                status,
            )

        # Exit rather than spin forever on a transport that will not recover
        # in place. The motivating case is a USB radio unplugged and plugged
        # back in: the container still holds the original device node, which
        # no longer exists, and no amount of reconnecting inside this process
        # will fix it. Exiting lets the restart policy recreate the container
        # against the current device.
        if args.exit_after_failed_cycles > 0:
            for name, count in consecutive_failures.items():
                if count >= args.exit_after_failed_cycles:
                    LOGGER.error(
                        "Transport %s has failed %d consecutive cycle(s); "
                        "exiting so the container restart policy can "
                        "reinitialize it",
                        name,
                        count,
                    )
                    return STATUS_TRANSPORT_UNHEALTHY

        if STOP_EVENT.is_set():
            break

        LOGGER.info("Next NWS check in %d second(s)", args.check_interval)
        STOP_EVENT.wait(args.check_interval)

    LOGGER.info("wx-alert stopped cleanly")
    return STATUS_OK
