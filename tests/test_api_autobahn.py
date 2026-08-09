"""Tests for the Autobahn API client."""

from __future__ import annotations

import json

import aiohttp
from aioresponses import aioresponses
import pytest

from custom_components.bonnvoyage.api.autobahn import BASE_URL, AutobahnClient
from custom_components.bonnvoyage.api.exceptions import ApiConnectionError

from .conftest import load_fixture_bytes

ROADS = ("A555", "A565")
SERVICES = ("warning", "roadworks", "closure")


def _url(road: str, service: str) -> str:
    return f"{BASE_URL}/{road}/services/{service}"


def _mock_default(mock: aioresponses, *, fail_road: str | None = None) -> None:
    """Mock both roads; A565 serves fixtures, A555 is empty."""
    for service in SERVICES:
        if fail_road == "A555":
            mock.get(_url("A555", service), status=500)
        else:
            mock.get(_url("A555", service), body=json.dumps({service: []}).encode())
    mock.get(_url("A565", "warning"), body=b'{"warning": []}')
    mock.get(
        _url("A565", "roadworks"),
        body=load_fixture_bytes("autobahn_roadworks.json"),
    )
    mock.get(_url("A565", "closure"), body=load_fixture_bytes("autobahn_closure.json"))


@pytest.fixture
def mocked_http():
    """Mock aiohttp on the transport level."""
    with aioresponses() as mock:
        yield mock


async def _fetch(roads: tuple[str, ...] = ROADS):
    async with aiohttp.ClientSession() as session:
        return await AutobahnClient(session).async_fetch_messages(roads)


async def test_parse_real_fixtures(mocked_http: aioresponses) -> None:
    """Real-world fixtures parse into messages with stable ids."""
    _mock_default(mocked_http)
    messages = await _fetch()

    assert len(messages) == 4
    roadworks = [m for m in messages.values() if m.kind == "roadwork"]
    closures = [m for m in messages.values() if m.kind == "closure"]
    assert len(roadworks) == 2
    assert len(closures) == 2

    sample = next(iter(roadworks))
    assert sample.message_id.startswith("autobahn_")
    assert sample.source == "autobahn"
    assert sample.road == "A565"
    assert "A565" in sample.title
    assert "(" in sample.title  # subtitle appended
    assert sample.is_blocked is False
    assert sample.starts_at is not None
    assert sample.description is not None


async def test_partial_failure_returns_rest(mocked_http: aioresponses) -> None:
    """One failing road is tolerated; the rest is returned."""
    _mock_default(mocked_http, fail_road="A555")
    messages = await _fetch()
    assert len(messages) == 4


async def test_all_failed_raises(mocked_http: aioresponses) -> None:
    """If every request fails, a connection error is raised."""
    for road in ROADS:
        for service in SERVICES:
            mocked_http.get(_url(road, service), status=500)
    with pytest.raises(ApiConnectionError):
        await _fetch()


async def test_invalid_payloads_count_as_failures(
    mocked_http: aioresponses,
) -> None:
    """Broken JSON and unexpected payloads are per-request failures."""
    for service in SERVICES:
        mocked_http.get(_url("A555", service), body=b"not json")
        mocked_http.get(_url("A565", service), body=b'{"unexpected": true}')
    with pytest.raises(ApiConnectionError):
        await _fetch()


async def test_unusable_items_are_dropped(mocked_http: aioresponses) -> None:
    """Items without identifier or usable coordinates are dropped."""
    items = [
        # valid
        {
            "identifier": "ok",
            "title": "A555 | Test",
            "coordinate": {"lat": 50.72, "long": 7.08},
        },
        # no identifier
        {"title": "x", "coordinate": {"lat": 50.72, "long": 7.08}},
        # broken coordinate
        {"identifier": "bad", "coordinate": {"lat": "abc", "long": 7.08}},
        # far outside Germany
        {"identifier": "far", "coordinate": {"lat": 40.0, "long": -3.7}},
        # not a dict
        "garbage",
    ]
    for service in SERVICES:
        payload = {service: items if service == "warning" else []}
        mocked_http.get(_url("A555", service), body=json.dumps(payload).encode())
    messages = await _fetch(roads=("A555",))
    assert set(messages) == {"autobahn_ok"}
    assert messages["autobahn_ok"].kind == "warning"
    assert messages["autobahn_ok"].is_blocked is None
