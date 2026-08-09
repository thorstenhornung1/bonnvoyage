"""Tests for the TomTom routing adapter.

Fixtures follow the documented response structure (docs.tomtom.com); a live
contract test is pending until an API key is available.
"""

from __future__ import annotations

import aiohttp
from aioresponses import aioresponses
import pytest

from custom_components.bonnvoyage.api.exceptions import (
    ApiAuthError,
    ApiConnectionError,
    ApiParseError,
    ApiRateLimitError,
    ApiRouteNotFoundError,
)
from custom_components.bonnvoyage.api.models import RoutePoint, RouteRequest
from custom_components.bonnvoyage.api.routing import create_provider
from custom_components.bonnvoyage.api.routing.tomtom import BASE_URL

from .conftest import load_fixture_bytes

REQUEST = RouteRequest(
    start=RoutePoint(50.73390001, 7.09970001),  # rounding must strip this
    destination=RoutePoint(50.7, 7.15),
)
ROUNDED_URL = f"{BASE_URL}/50.7339,7.0997:50.7,7.15/json"


@pytest.fixture
def mocked_http():
    """Mock aiohttp on the transport level."""
    with aioresponses() as mock:
        yield mock


async def _route(request: RouteRequest = REQUEST):
    async with aiohttp.ClientSession() as session:
        provider = create_provider("tomtom", session, "test-key-123")
        return await provider.async_route(request)


async def test_route_parses_and_rounds_coordinates(
    mocked_http: aioresponses,
) -> None:
    """Coordinates are rounded to 4 decimals; summary fields are parsed."""
    mocked_http.get(
        f"{ROUNDED_URL}?computeTravelTimeFor=all&key=test-key-123"
        "&routeRepresentation=summaryOnly&traffic=true&travelMode=car",
        body=load_fixture_bytes("tomtom_route.json"),
    )
    result = await _route()

    assert result.travel_time_s == 1685
    assert result.no_traffic_time_s == 1290
    assert result.delay_s == 312
    assert result.length_m == 24310

    # The request URL contains only rounded coordinates
    (request_key,) = mocked_http.requests.keys()
    assert "50.7339,7.0997:50.7,7.15" in str(request_key[1])
    assert "50.73390001" not in str(request_key[1])


async def test_via_point_in_url(mocked_http: aioresponses) -> None:
    """A via point lands between start and destination."""
    request = RouteRequest(
        start=RoutePoint(50.7339, 7.0997),
        destination=RoutePoint(50.7, 7.15),
        via=RoutePoint(50.72, 7.12),
    )
    url = f"{BASE_URL}/50.7339,7.0997:50.72,7.12:50.7,7.15/json"
    mocked_http.get(
        f"{url}?computeTravelTimeFor=all&key=test-key-123"
        "&routeRepresentation=summaryOnly&traffic=true&travelMode=car",
        body=load_fixture_bytes("tomtom_route.json"),
    )
    result = await _route(request)
    assert result.travel_time_s == 1685


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, b"", ApiAuthError),
        (401, b"", ApiAuthError),
        (429, b"", ApiRateLimitError),
        (400, load_fixture_bytes("tomtom_error_no_route.json"), ApiRouteNotFoundError),
        (400, b"not json at all", ApiConnectionError),
        (500, b"", ApiConnectionError),
    ],
)
async def test_error_mapping(
    mocked_http: aioresponses, status: int, body: bytes, expected: type[Exception]
) -> None:
    """HTTP statuses map onto the exception taxonomy."""
    mocked_http.get(
        f"{ROUNDED_URL}?computeTravelTimeFor=all&key=test-key-123"
        "&routeRepresentation=summaryOnly&traffic=true&travelMode=car",
        status=status,
        body=body,
    )
    with pytest.raises(expected) as exc_info:
        await _route()
    # Security: the API key must never leak into exception messages
    assert "test-key-123" not in str(exc_info.value)


async def test_unexpected_body_raises_parse_error(
    mocked_http: aioresponses,
) -> None:
    """A 200 response without routes raises the parse error."""
    mocked_http.get(
        f"{ROUNDED_URL}?computeTravelTimeFor=all&key=test-key-123"
        "&routeRepresentation=summaryOnly&traffic=true&travelMode=car",
        body=b'{"routes": []}',
    )
    with pytest.raises(ApiParseError):
        await _route()


async def test_validate_key_uses_fixed_probe(mocked_http: aioresponses) -> None:
    """Key validation runs a minimal fixed route."""
    mocked_http.get(
        f"{BASE_URL}/50.7339,7.0997:50.732,7.0965/json"
        "?computeTravelTimeFor=all&key=test-key-123"
        "&routeRepresentation=summaryOnly&traffic=true&travelMode=car",
        body=load_fixture_bytes("tomtom_route.json"),
    )
    async with aiohttp.ClientSession() as session:
        provider = create_provider("tomtom", session, "test-key-123")
        await provider.async_validate_key()
