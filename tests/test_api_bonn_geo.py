"""Tests for the Bonn geoportal roadworks client."""

from __future__ import annotations

import json

import aiohttp
from aioresponses import aioresponses
import pytest

from custom_components.bonnvoyage.api.bonn_geo import (
    MAX_RESPONSE_BYTES,
    ROADWORKS_URL,
    BonnGeoClient,
)
from custom_components.bonnvoyage.api.exceptions import (
    ApiConnectionError,
    ApiParseError,
    ApiResponseTooLargeError,
)

from .conftest import load_fixture_bytes


@pytest.fixture
def mocked_http():
    """Mock aiohttp on the transport level."""
    with aioresponses() as mock:
        yield mock


async def _fetch(mock: aioresponses, **response_kwargs):
    """Run one client fetch against a mocked response."""
    mock.get(ROADWORKS_URL, **response_kwargs)
    async with aiohttp.ClientSession() as session:
        return await BonnGeoClient(session).async_fetch_roadworks()


async def test_parse_real_feed(mocked_http: aioresponses) -> None:
    """The real-world fixture parses into validated messages."""
    messages = await _fetch(mocked_http, body=load_fixture_bytes("baustellen.json"))

    assert len(messages) == 9
    first = messages["city_8921"]
    assert first.source == "city"
    assert first.kind == "roadwork"
    assert first.title == "Hermann-Wandersleb-Ring (B56)"
    assert first.latitude == pytest.approx(50.7294, abs=0.001)
    assert first.longitude == pytest.approx(7.0774, abs=0.001)
    assert first.authority == "Straßen NRW"
    assert first.restriction is not None
    assert first.starts_at is not None
    assert first.starts_at.year == 2024
    assert first.ends_at is not None
    assert first.ends_at.year == 2027


async def test_http_error(mocked_http: aioresponses) -> None:
    """Non-200 responses raise the connection error."""
    with pytest.raises(ApiConnectionError):
        await _fetch(mocked_http, status=503)


async def test_invalid_json(mocked_http: aioresponses) -> None:
    """Broken JSON raises the parse error."""
    with pytest.raises(ApiParseError):
        await _fetch(mocked_http, body=b"<html>not json</html>")


async def test_not_a_feature_collection(mocked_http: aioresponses) -> None:
    """A non-FeatureCollection payload raises the parse error."""
    with pytest.raises(ApiParseError):
        await _fetch(mocked_http, body=b'{"type": "Feature"}')


async def test_oversized_response(mocked_http: aioresponses) -> None:
    """Responses beyond the size cap raise the dedicated error."""
    with pytest.raises(ApiResponseTooLargeError):
        await _fetch(mocked_http, body=b"x" * (MAX_RESPONSE_BYTES + 1))


async def test_unusable_features_are_dropped(mocked_http: aioresponses) -> None:
    """Features without id/geometry or outside Bonn are dropped."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            # valid
            {
                "properties": {"baustelle_id": 1, "bezeichnung": "OK"},
                "geometry": {"type": "Point", "coordinates": [7.10, 50.73]},
            },
            # no id
            {
                "properties": {"bezeichnung": "no id"},
                "geometry": {"type": "Point", "coordinates": [7.10, 50.73]},
            },
            # no geometry
            {"properties": {"baustelle_id": 2, "bezeichnung": "no geo"}},
            # outside Bonn bounding box (Berlin)
            {
                "properties": {"baustelle_id": 3, "bezeichnung": "far"},
                "geometry": {"type": "Point", "coordinates": [13.40, 52.52]},
            },
            # unsupported geometry type
            {
                "properties": {"baustelle_id": 4, "bezeichnung": "multi"},
                "geometry": {"type": "MultiPoint", "coordinates": [[7.1, 50.73]]},
            },
            # not a dict
            "garbage",
        ],
    }
    messages = await _fetch(mocked_http, body=json.dumps(payload).encode())
    assert set(messages) == {"city_1"}


async def test_linestring_uses_first_point(mocked_http: aioresponses) -> None:
    """LineString geometries fall back to their first coordinate."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "properties": {"baustelle_id": 5, "bezeichnung": "line"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[7.11, 50.72], [7.12, 50.73]],
                },
            },
            {
                "properties": {"baustelle_id": 6, "bezeichnung": "poly"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[7.09, 50.74], [7.10, 50.74], [7.09, 50.75]]],
                },
            },
        ],
    }
    messages = await _fetch(mocked_http, body=json.dumps(payload).encode())
    assert messages["city_5"].longitude == 7.11
    assert messages["city_5"].latitude == 50.72
    assert messages["city_6"].latitude == 50.74


async def test_tolerant_date_parsing(mocked_http: aioresponses) -> None:
    """Broken dates become None instead of dropping the message."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "properties": {
                    "baustelle_id": 7,
                    "bezeichnung": "bad dates",
                    "von": "irgendwann",
                    "bis": "",
                },
                "geometry": {"type": "Point", "coordinates": [7.10, 50.73]},
            }
        ],
    }
    messages = await _fetch(mocked_http, body=json.dumps(payload).encode())
    message = messages["city_7"]
    assert message.starts_at is None
    assert message.ends_at is None
