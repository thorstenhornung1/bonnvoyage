"""Client for the transitous.org community transit API (MOTIS / DELFI data).

transitous.org is a community-run, key-free service under a fair use policy;
polling a handful of stops on a moderate interval is the intended use case.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any, Final

import aiohttp

from .exceptions import ApiParseError
from .http import async_get_capped
from .models import Departure, TransitStop
from .validation import coords_in_germany

_LOGGER = logging.getLogger(__name__)

BASE_URL: Final = "https://api.transitous.org/api/v1"
MAX_RESPONSE_BYTES: Final = 1024 * 1024

MAX_STOP_RESULTS: Final = 8
MAX_TEXT_LENGTH: Final = 100


class TransitousClient:
    """Async client for stop search and departures."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the client with an injected session."""
        self._session = session

    async def async_search_stops(self, query: str) -> list[TransitStop]:
        """Search stops by name (STOP results only)."""
        url = f"{BASE_URL}/geocode"
        raw = await async_get_capped(
            self._session, url, MAX_RESPONSE_BYTES, params={"text": query}
        )
        try:
            results = json.loads(raw)
        except ValueError as err:
            raise ApiParseError(f"Invalid geocode JSON: {err}") from err
        if not isinstance(results, list):
            raise ApiParseError("Geocode response is not a list")

        stops: list[TransitStop] = []
        for item in results:
            stop = self._parse_stop(item)
            if stop is not None:
                stops.append(stop)
            if len(stops) >= MAX_STOP_RESULTS:
                break
        return stops

    async def async_departures(self, stop_id: str, count: int) -> list[Departure]:
        """Fetch the next departures of a stop."""
        url = f"{BASE_URL}/stoptimes"
        raw = await async_get_capped(
            self._session,
            url,
            MAX_RESPONSE_BYTES,
            # aiohttp URL-encodes params itself (colons in DELFI stop ids)
            params={"stopId": stop_id, "n": str(count)},
        )
        try:
            data = json.loads(raw)
        except ValueError as err:
            raise ApiParseError(f"Invalid stoptimes JSON: {err}") from err
        stop_times = data.get("stopTimes") if isinstance(data, dict) else None
        if not isinstance(stop_times, list):
            raise ApiParseError("Stoptimes response has no stopTimes list")

        departures: list[Departure] = []
        for item in stop_times:
            departure = self._parse_departure(item)
            if departure is not None:
                departures.append(departure)
        departures.sort(key=lambda dep: dep.actual or dep.scheduled)
        return departures

    @staticmethod
    def _parse_stop(item: Any) -> TransitStop | None:
        """Parse one geocode result, or None if not a usable stop."""
        if not isinstance(item, dict) or item.get("type") != "STOP":
            return None
        stop_id = item.get("id")
        name = str(item.get("name") or "").strip()[:MAX_TEXT_LENGTH]
        try:
            latitude = float(item["lat"])
            longitude = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        if not stop_id or not name or not coords_in_germany(latitude, longitude):
            return None
        return TransitStop(
            stop_id=str(stop_id), name=name, latitude=latitude, longitude=longitude
        )

    @staticmethod
    def _parse_departure(item: Any) -> Departure | None:
        """Parse one stoptime, or None if unusable."""
        if not isinstance(item, dict):
            return None
        place = item.get("place") or {}
        scheduled = _parse_timestamp(place.get("scheduledDeparture"))
        if scheduled is None:
            return None
        line = str(item.get("routeShortName") or item.get("displayName") or "?")
        return Departure(
            line=line[:20],
            headsign=str(item.get("headsign") or "")[:MAX_TEXT_LENGTH],
            mode=str(item.get("mode") or "")[:20],
            scheduled=scheduled,
            actual=_parse_timestamp(place.get("departure")),
            realtime=item.get("realTime") is True,
            cancelled=bool(item.get("cancelled") or item.get("tripCancelled")),
        )


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp (Z suffix), or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
