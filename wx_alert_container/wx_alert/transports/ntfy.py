"""Delivery to an ntfy topic over HTTPS."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import requests

from ..formatting import (
    build_compact_ntfy_body,
    build_verbose_message,
    ntfy_priority_for_alert,
    ntfy_tags_for_alert,
)
from ..policy import StartupPolicy, should_suppress_on_startup
from ..text import clean_field
from .base import DeliveryContext, DeliveryResult

LOGGER = logging.getLogger("wx-alert")


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


class NtfyTransport:
    """Publishes each alert as one ntfy notification."""

    name = "ntfy"

    def __init__(
        self,
        session: requests.Session,
        *,
        server: str,
        topic: str,
        token: str | None,
        tags: str,
        priority: str,
        verbose: bool,
        startup_policy: StartupPolicy,
    ) -> None:
        self._session = session
        self._server = server
        self._topic = topic
        self._token = token
        self._tags = tags
        self._priority = priority
        self._verbose = verbose
        self._startup_policy = startup_policy

    def start(self) -> None:
        LOGGER.info(
            "ntfy transport ready server=%s topic=%s priority=%s",
            self._server,
            self._topic,
            self._priority,
        )

    def selftest(self) -> None:
        LOGGER.info(
            "Sending ntfy test notification server=%s topic=%s",
            self._server,
            self._topic,
        )

        message_id = publish_ntfy_message(
            self._session,
            server=self._server,
            topic=self._topic,
            title="NWS Alert Test",
            message=(
                "ntfy delivery is working. No National Weather Service alert "
                "was required for this test."
            ),
            priority="default",
            tags=self._tags,
            token=self._token,
        )

        if message_id:
            LOGGER.info("ntfy test successful message_id=%s", message_id)
        else:
            LOGGER.info("ntfy test successful")

    def deliver(
        self,
        alert: dict[str, Any],
        context: DeliveryContext,
    ) -> tuple[DeliveryResult, str]:
        suppress, reason = should_suppress_on_startup(
            alert,
            self._startup_policy,
            context.first_cycle,
            context.now,
            previously_handled=context.previously_handled,
        )
        if suppress:
            return DeliveryResult.SKIPPED, f"startup-stale: {reason}"

        event = clean_field(alert.get("event"), "Unknown weather alert")
        priority = ntfy_priority_for_alert(alert, self._priority)
        tags = ntfy_tags_for_alert(alert, self._tags)

        # The event is already the notification title, so the compact body
        # carries the area, the window, and the extracted facts rather than
        # the headline, which would restate the title. Verbose mode sends
        # the full record.
        message = (
            build_verbose_message(alert)
            if self._verbose
            else build_compact_ntfy_body(alert, context.now)
        )

        try:
            message_id = publish_ntfy_message(
                self._session,
                server=self._server,
                topic=self._topic,
                title=event,
                message=message,
                priority=priority,
                tags=tags,
                token=self._token,
            )
        except requests.RequestException as exc:
            return DeliveryResult.FAILED, str(exc)

        detail = f"priority={priority} tags={tags or 'none'}"
        if message_id:
            detail = f"{detail} message_id={message_id}"

        return DeliveryResult.SENT, detail

    def close(self) -> None:
        # The requests.Session is owned by the caller, which closes it as part
        # of its own context manager.
        return
