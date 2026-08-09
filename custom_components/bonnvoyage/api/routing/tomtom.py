"""TomTom Routing API v1 adapter.

Response structure and error codes follow the official documentation
(docs.tomtom.com, checked 08/2026). Field names are documented; a live
contract test is pending until an API key is available.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Final

import aiohttp

from ..exceptions import (
    ApiAuthError,
    ApiParseError,
    ApiRateLimitError,
    ApiRouteNotFoundError,
)
from ..http import async_get_capped
from ..models import RoutePoint, RouteRequest, RouteResult

_LOGGER = logging.getLogger(__name__)

BASE_URL: Final = "https://api.tomtom.com/routing/1/calculateRoute"
MAX_RESPONSE_BYTES: Final = 512 * 1024

# Privacy: coordinates are rounded before leaving the house (~11 m precision).
COORDINATE_PRECISION: Final = 4

# Documented detailedError codes meaning "these points cannot be routed".
_NOT_ROUTABLE_CODES: Final = ("NO_ROUTE_FOUND", "MAP_MATCHING_FAILURE")

# Fixed points in central Bonn for key validation (public landmarks).
_VALIDATION_REQUEST: Final = RouteRequest(
    start=RoutePoint(50.7339, 7.0997),
    destination=RoutePoint(50.7320, 7.0965),
)


class TomTomProvider:
    """Adapter for the TomTom Routing API."""

    provider_id = "tomtom"
    supports_live_traffic = True

    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        """Initialize with an injected session and the API key."""
        self._session = session
        self._api_key = api_key
        # Global rate limiter: all route coordinators share one provider
        # instance; requests are serialized.
        self._semaphore = asyncio.Semaphore(1)

    async def async_validate_key(self) -> None:
        """Validate the key with a minimal fixed route."""
        await self.async_route(_VALIDATION_REQUEST)

    async def async_route(self, request: RouteRequest) -> RouteResult:
        """Calculate a route with live traffic."""
        locations = ":".join(
            _format_point(point)
            for point in (request.start, request.via, request.destination)
            if point is not None
        )
        url = f"{BASE_URL}/{locations}/json"
        async with self._semaphore:
            raw = await async_get_capped(
                self._session,
                url,
                MAX_RESPONSE_BYTES,
                params={
                    "key": self._api_key,
                    "traffic": "true",
                    "travelMode": "car",
                    "computeTravelTimeFor": "all",
                    "routeRepresentation": "summaryOnly",
                },
                status_handler=_handle_error_status,
            )
        return _parse_route(raw)


def _format_point(point: RoutePoint) -> str:
    """Format a point as lat,lon with privacy rounding."""
    return (
        f"{round(point.latitude, COORDINATE_PRECISION)},"
        f"{round(point.longitude, COORDINATE_PRECISION)}"
    )


def _handle_error_status(status: int, body: bytes) -> None:
    """Map HTTP error statuses to the exception taxonomy.

    Status codes are authoritative (documented); the body is parsed
    defensively only to detect not-routable errors.
    """
    if status in (401, 403):
        raise ApiAuthError("TomTom rejected the API key")
    if status == 429:
        raise ApiRateLimitError("TomTom rate limit exceeded")
    if status == 400:
        code = _detailed_error_code(body)
        if code in _NOT_ROUTABLE_CODES:
            raise ApiRouteNotFoundError(f"TomTom: {code}")


def _detailed_error_code(body: bytes) -> str | None:
    """Extract detailedError.code from an error body, defensively."""
    try:
        data = json.loads(body)
        code = data["detailedError"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(code)


def _parse_route(raw: bytes) -> RouteResult:
    """Parse the calculateRoute response into a RouteResult."""
    try:
        data = json.loads(raw)
        summary: Any = data["routes"][0]["summary"]
        travel_time = int(summary["travelTimeInSeconds"])
    except (ValueError, KeyError, IndexError, TypeError) as err:
        raise ApiParseError(f"Unexpected TomTom response: {err}") from err

    no_traffic = _optional_int(summary.get("noTrafficTravelTimeInSeconds"))
    delay = _optional_int(summary.get("trafficDelayInSeconds"))
    if delay is None and no_traffic is not None:
        delay = max(travel_time - no_traffic, 0)

    return RouteResult(
        travel_time_s=travel_time,
        no_traffic_time_s=no_traffic,
        delay_s=delay,
        length_m=_optional_int(summary.get("lengthInMeters")),
    )


def _optional_int(value: Any) -> int | None:
    """Coerce to int, or None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
