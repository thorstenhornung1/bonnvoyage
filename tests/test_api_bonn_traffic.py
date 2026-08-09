"""Tests for the Bonn city traffic layer client."""

from __future__ import annotations

import json

import aiohttp
from aioresponses import aioresponses
import pytest

from custom_components.bonnvoyage.api.bonn_traffic import (
    TRAFFIC_URL,
    BonnTrafficClient,
)
from custom_components.bonnvoyage.api.exceptions import (
    ApiConnectionError,
    ApiParseError,
)

from .conftest import load_fixture_bytes


@pytest.fixture
def mocked_http():
    """Mock aiohttp on the transport level."""
    with aioresponses() as mock:
        yield mock


async def _fetch(mock: aioresponses, **response_kwargs):
    mock.get(TRAFFIC_URL, **response_kwargs)
    async with aiohttp.ClientSession() as session:
        return await BonnTrafficClient(session).async_fetch_segments()


async def test_parse_real_layer(mocked_http: aioresponses) -> None:
    """The real-world fixture parses into validated segments."""
    segments = await _fetch(mocked_http, body=load_fixture_bytes("verkehrslage.json"))

    assert len(segments) == 111

    risky = segments["100"]
    assert risky.status == "congestion_risk"
    assert risky.raw_status == "Staugefahr"
    assert risky.speed_kmh == 4
    assert risky.measured_at is not None
    assert risky.measured_at.tzinfo is not None
    assert 50.6 < risky.midpoint[0] < 50.85
    assert risky.points  # support points for closest-segment search

    normal = segments["144"]
    assert normal.status == "normal"

    unknown = segments["142"]
    assert unknown.status == "unknown"
    assert unknown.speed_kmh is None  # speed 0 means not measurable


async def test_http_error(mocked_http: aioresponses) -> None:
    """Non-200 responses raise the connection error."""
    with pytest.raises(ApiConnectionError):
        await _fetch(mocked_http, status=503)


async def test_invalid_json(mocked_http: aioresponses) -> None:
    """Broken JSON raises the parse error."""
    with pytest.raises(ApiParseError):
        await _fetch(mocked_http, body=b"<html>")


async def test_empty_layer_raises(mocked_http: aioresponses) -> None:
    """A layer without segments raises the parse error."""
    with pytest.raises(ApiParseError):
        await _fetch(
            mocked_http,
            body=b'{"type": "FeatureCollection", "features": []}',
        )


async def test_unusable_features_are_dropped(mocked_http: aioresponses) -> None:
    """Features without id/geometry or unknown vocab are handled."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "properties": {
                    "strecke_id": 1,
                    "verkehrsstatus": "völlig neues Vokabular",
                    "geschwindigkeit": 33,
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [[[7.10, 50.73], [7.11, 50.74]]],
                },
            },
            # no id
            {
                "properties": {"verkehrsstatus": "Stau"},
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [[[7.10, 50.73]]],
                },
            },
            # wrong geometry type
            {
                "properties": {"strecke_id": 2},
                "geometry": {"type": "Point", "coordinates": [7.10, 50.73]},
            },
            # outside Bonn
            {
                "properties": {"strecke_id": 3},
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [[[13.40, 52.52], [13.41, 52.53]]],
                },
            },
        ],
    }
    segments = await _fetch(mocked_http, body=json.dumps(payload).encode())
    assert set(segments) == {"1"}
    assert segments["1"].status == "unknown"  # unknown vocabulary maps down
    assert segments["1"].raw_status == "völlig neues Vokabular"
    assert segments["1"].speed_kmh == 33


async def test_more_edge_cases(mocked_http: aioresponses) -> None:
    """Non-collections, garbage features and broken timestamps."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            "garbage",
            {
                "properties": {"strecke_id": 9, "auswertezeit": "gestern"},
                "geometry": None,
            },
            {
                "properties": {"strecke_id": 10, "auswertezeit": "kaputt"},
                "geometry": {
                    "type": "MultiLineString",
                    # more than 50 points to hit the cap
                    "coordinates": [[[7.10 + i / 10000, 50.73] for i in range(60)]],
                },
            },
        ],
    }
    segments = await _fetch(mocked_http, body=json.dumps(payload).encode())
    assert set(segments) == {"10"}
    assert len(segments["10"].points) == 50  # capped
    assert segments["10"].measured_at is None  # broken timestamp


async def test_not_a_collection(mocked_http: aioresponses) -> None:
    """A non-FeatureCollection payload raises the parse error."""
    with pytest.raises(ApiParseError):
        await _fetch(mocked_http, body=b'{"type": "Feature"}')
