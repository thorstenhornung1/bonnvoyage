"""Client for the German Autobahn API (verkehr.autobahn.de)."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from typing import Any, Final

import aiohttp

from .exceptions import ApiConnectionError, BonnVoyageApiError
from .http import async_get_capped
from .models import TrafficMessage
from .validation import coords_in_germany

_LOGGER = logging.getLogger(__name__)

BASE_URL: Final = "https://verkehr.autobahn.de/o/autobahn"
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024

# String values of MessageSource.AUTOBAHN / MessageKind.* (API layer is HA free)
SOURCE_AUTOBAHN: Final = "autobahn"

# Service path -> (response key, message kind)
SERVICES: Final[dict[str, str]] = {
    "warning": "warning",
    "roadworks": "roadwork",
    "closure": "closure",
}

MAX_TEXT_LENGTH: Final = 500
MAX_DESCRIPTION_LINES: Final = 8

# INRIX traffic warnings ("INRIX--vi-avl.<date>_<counter>.de0") get a new
# counter on every feed update, so their identifier is not stable (live data
# 10/2026: about 80 % of the geo_location churn came from such ids).
# Stripping the counter is no reliable way out: the remainder only
# carries a date, so concurrent warnings would collapse into one id and the
# id would still change at midnight. These messages are flagged as volatile.
VOLATILE_ID_PREFIX: Final = "INRIX--vi-avl."


class AutobahnClient:
    """Async client for warnings, roadworks and closures per motorway."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the client with an injected session."""
        self._session = session
        # (road, kind) pairs whose request failed in the last fetch
        self.failed_services: frozenset[tuple[str, str]] = frozenset()

    async def async_fetch_messages(
        self, roads: tuple[str, ...]
    ) -> dict[str, TrafficMessage]:
        """Fetch all services for all roads; tolerate partial failures.

        Individual road/service failures are logged and skipped; only if every
        request fails is an ApiConnectionError raised. The skipped (road, kind)
        pairs of a partial result are kept in ``failed_services``, so callers
        can tell "message gone" from "road not fetched".
        """
        requests = [(road, service) for road in roads for service in SERVICES]
        results = await asyncio.gather(
            *(self._async_fetch_service(road, service) for road, service in requests),
            return_exceptions=True,
        )

        messages: dict[str, TrafficMessage] = {}
        failed: set[tuple[str, str]] = set()
        for (road, service), result in zip(requests, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, BonnVoyageApiError):
                    raise result
                failed.add((road, SERVICES[service]))
                _LOGGER.warning("Autobahn API %s/%s failed: %s", road, service, result)
                continue
            messages.update(result)

        if len(failed) == len(requests):
            raise ApiConnectionError("All Autobahn API requests failed")
        self.failed_services = frozenset(failed)
        return messages

    async def _async_fetch_service(
        self, road: str, service: str
    ) -> dict[str, TrafficMessage]:
        """Fetch and parse one road/service combination."""
        url = f"{BASE_URL}/{road}/services/{service}"
        raw = await async_get_capped(self._session, url, MAX_RESPONSE_BYTES)
        try:
            data = json.loads(raw)
        except ValueError as err:
            raise ApiConnectionError(f"Invalid JSON from {url}: {err}") from err

        items = data.get(service) if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ApiConnectionError(f"Unexpected payload from {url}")

        messages: dict[str, TrafficMessage] = {}
        for item in items:
            message = self._parse_item(item, road, SERVICES[service])
            if message is not None:
                messages[message.message_id] = message
        return messages

    def _parse_item(self, item: Any, road: str, kind: str) -> TrafficMessage | None:
        """Parse one message item, or None if unusable."""
        if not isinstance(item, dict):
            return None
        identifier = item.get("identifier")
        coordinate = item.get("coordinate") or {}
        try:
            latitude = float(coordinate["lat"])
            longitude = float(coordinate["long"])
        except (KeyError, TypeError, ValueError):
            return None
        if not identifier or not coords_in_germany(latitude, longitude):
            return None

        title = str(item.get("title") or f"{road} Meldung")[:MAX_TEXT_LENGTH]
        subtitle = str(item.get("subtitle") or "").strip()
        if subtitle:
            title = f"{title} ({subtitle})"[:MAX_TEXT_LENGTH]

        description_lines = item.get("description")
        description: str | None = None
        if isinstance(description_lines, list):
            description = (
                "\n".join(
                    str(line)
                    for line in description_lines[:MAX_DESCRIPTION_LINES]
                    if line
                )[:MAX_TEXT_LENGTH]
                or None
            )

        return TrafficMessage(
            message_id=f"autobahn_{identifier}",
            source=SOURCE_AUTOBAHN,
            kind=kind,
            title=title,
            latitude=latitude,
            longitude=longitude,
            description=description,
            road=road,
            is_blocked=_parse_bool(item.get("isBlocked")),
            starts_at=_parse_timestamp(item.get("startTimestamp")),
            volatile_id=str(identifier).startswith(VOLATILE_ID_PREFIX),
        )


def _parse_bool(value: Any) -> bool | None:
    """Parse a boolean that the API serializes as bool or string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
