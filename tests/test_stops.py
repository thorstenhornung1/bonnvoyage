"""Tests for transit stops: API client, subentry flow and entities."""

from __future__ import annotations

import aiohttp
from aioresponses import aioresponses
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.bonnvoyage.api.exceptions import (
    ApiConnectionError,
    ApiParseError,
)
from custom_components.bonnvoyage.api.transitous import BASE_URL, TransitousClient
from custom_components.bonnvoyage.const import (
    CONF_DELAY_ALERT_THRESHOLD,
    CONF_QUERY,
    CONF_STOP_ID,
    SUBENTRY_TYPE_STOP,
)

from .conftest import load_fixture_bytes, mock_config_entry
from .test_init import _setup

STADTHAUS_ID = "de-DELFI_de:05314:61114:1:13"


# --- API client (aioresponses, transport level) ---


@pytest.fixture
def mocked_http():
    """Mock aiohttp on the transport level."""
    with aioresponses() as mock:
        yield mock


async def test_search_stops(mocked_http: aioresponses) -> None:
    """Geocode results are filtered to STOP entries."""
    mocked_http.get(
        f"{BASE_URL}/geocode?text=Bonn+Stadthaus",
        body=load_fixture_bytes("transitous_geocode.json"),
    )
    async with aiohttp.ClientSession() as session:
        stops = await TransitousClient(session).async_search_stops("Bonn Stadthaus")
    assert [stop.stop_id for stop in stops] == [
        STADTHAUS_ID,
        "de-DELFI_de:05913:132_G",
    ]
    assert stops[0].name == "Bonn Stadthaus"
    assert 50.7 < stops[0].latitude < 50.8


async def test_departures(mocked_http: aioresponses) -> None:
    """Departures parse, sort by actual time and expose delays."""
    mocked_http.get(
        f"{BASE_URL}/stoptimes?n=10&stopId={STADTHAUS_ID.replace(':', '%3A')}",
        body=load_fixture_bytes("transitous_stoptimes.json"),
    )
    async with aiohttp.ClientSession() as session:
        departures = await TransitousClient(session).async_departures(STADTHAUS_ID, 10)
    assert len(departures) == 10
    first = departures[0]
    assert first.line == "62"
    assert first.mode == "TRAM"
    assert first.delay_s == 120
    assert first.realtime is True
    # Sorted by actual departure
    times = [dep.actual or dep.scheduled for dep in departures]
    assert times == sorted(times)


async def test_api_errors(mocked_http: aioresponses) -> None:
    """Broken payloads raise the taxonomy errors."""
    mocked_http.get(f"{BASE_URL}/geocode?text=x", body=b"not json")
    mocked_http.get(f"{BASE_URL}/stoptimes?n=5&stopId=y", body=b'{"nope": 1}')
    mocked_http.get(f"{BASE_URL}/geocode?text=z", status=503)
    async with aiohttp.ClientSession() as session:
        client = TransitousClient(session)
        with pytest.raises(ApiParseError):
            await client.async_search_stops("x")
        with pytest.raises(ApiParseError):
            await client.async_departures("y", 5)
        with pytest.raises(ApiConnectionError):
            await client.async_search_stops("z")


# --- Subentry flow and entities (hass level, aioclient_mock) ---


def _mock_transitous(aioclient_mock) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/geocode",
        content=load_fixture_bytes("transitous_geocode.json"),
    )
    aioclient_mock.get(
        f"{BASE_URL}/stoptimes",
        content=load_fixture_bytes("transitous_stoptimes.json"),
    )


async def test_stop_subentry_flow(hass: HomeAssistant, aioclient_mock) -> None:
    """Search, pick and watch a stop."""
    _mock_transitous(aioclient_mock)
    entry = mock_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_QUERY: "Bonn Stadthaus", CONF_DELAY_ALERT_THRESHOLD: 5},
    )
    assert result["step_id"] == "select"
    # Sorted by distance: the Bonn stop comes first
    options = result["data_schema"].schema[CONF_STOP_ID].config["options"]
    assert options[0]["value"] == STADTHAUS_ID

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_STOP_ID: STADTHAUS_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(iter(entry.subentries.values()))
    assert subentry.title == "Bonn Stadthaus"
    assert subentry.unique_id == STADTHAUS_ID
    assert subentry.data[CONF_DELAY_ALERT_THRESHOLD] == 5


async def test_stop_subentry_flow_errors(hass: HomeAssistant, aioclient_mock) -> None:
    """No matches and duplicates surface as form errors; outages abort."""
    entry = mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={CONF_STOP_ID: STADTHAUS_ID, CONF_DELAY_ALERT_THRESHOLD: 5},
                subentry_type=SUBENTRY_TYPE_STOP,
                title="Bonn Stadthaus",
                unique_id=STADTHAUS_ID,
            )
        ],
    )
    entry.add_to_hass(hass)

    # Empty result list -> form error
    aioclient_mock.get(f"{BASE_URL}/geocode", content=b"[]")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_QUERY: "Nirgendwo", CONF_DELAY_ALERT_THRESHOLD: 5},
    )
    assert result["errors"] == {"base": "no_stops_found"}

    # Duplicate stop -> form error on select
    aioclient_mock.clear_requests()
    _mock_transitous(aioclient_mock)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_QUERY: "Bonn Stadthaus", CONF_DELAY_ALERT_THRESHOLD: 5},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_STOP_ID: STADTHAUS_ID}
    )
    assert result["errors"] == {"base": "already_configured"}

    # Service down -> abort
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/geocode", status=503)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_QUERY: "Bonn", CONF_DELAY_ALERT_THRESHOLD: 5},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "transitous_unavailable"


def _entry_with_stop(threshold: int = 5):
    return mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={
                    CONF_STOP_ID: STADTHAUS_ID,
                    CONF_DELAY_ALERT_THRESHOLD: threshold,
                },
                subentry_type=SUBENTRY_TYPE_STOP,
                title="Bonn Stadthaus",
                unique_id=STADTHAUS_ID,
            )
        ],
    )


async def test_stop_entities(hass: HomeAssistant, aioclient_mock) -> None:
    """A watched stop gets departure, delay and alert entities."""
    _mock_transitous(aioclient_mock)
    entry = _entry_with_stop(threshold=5)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    nxt = hass.states.get("sensor.bonn_stadthaus_next_departure")
    assert nxt is not None
    assert nxt.state == "2026-08-09T14:05:00+00:00"
    board = nxt.attributes["departures"]
    assert board[0]["line"] == "62"
    assert board[0]["delay_min"] == 2
    assert board[0]["realtime"] is True
    assert len(board) == 10

    # Fixture max delay: 120 s -> 2 min
    assert hass.states.get("sensor.bonn_stadthaus_largest_delay").state == "2.0"
    # 2 min < threshold 5 -> off
    assert hass.states.get("binary_sensor.bonn_stadthaus_delay").state == "off"


async def test_stop_delay_alert_triggers(hass: HomeAssistant, aioclient_mock) -> None:
    """A lower threshold flips the delay alert on."""
    _mock_transitous(aioclient_mock)
    entry = _entry_with_stop(threshold=2)
    await _setup(hass, entry)
    assert hass.states.get("binary_sensor.bonn_stadthaus_delay").state == "on"


async def test_parse_edge_cases(mocked_http: aioresponses) -> None:
    """Result cap, garbage entries and broken fields are handled."""
    import json as _json

    # geocode: non-list -> parse error; cap at 8 STOP results; broken items
    stops_payload = [
        {"type": "STOP", "id": f"s{i}", "name": f"Stop {i}", "lat": 50.7, "lon": 7.1}
        for i in range(12)
    ] + [
        {"type": "STOP", "id": "bad", "name": "NoCoords"},  # missing lat/lon
        {"type": "STOP", "id": "far", "name": "Far", "lat": 40.0, "lon": -3.7},
        {"type": "STOP", "id": "", "name": "NoId", "lat": 50.7, "lon": 7.1},
        "garbage",
    ]
    mocked_http.get(
        f"{BASE_URL}/geocode?text=cap", body=_json.dumps(stops_payload).encode()
    )
    mocked_http.get(f"{BASE_URL}/geocode?text=notalist", body=b'{"a": 1}')

    # stoptimes: broken/cancelled entries
    stoptimes_payload = {
        "stopTimes": [
            "garbage",
            {"place": {}},  # no scheduled departure
            {
                "place": {"scheduledDeparture": "kaputt"},
            },
            {
                "place": {"scheduledDeparture": "2026-08-09T15:00:00Z"},
                "displayName": "SEV 66",
                "tripCancelled": True,
            },
        ]
    }
    mocked_http.get(
        f"{BASE_URL}/stoptimes?n=5&stopId=edge",
        body=_json.dumps(stoptimes_payload).encode(),
    )

    async with aiohttp.ClientSession() as session:
        client = TransitousClient(session)
        stops = await client.async_search_stops("cap")
        assert len(stops) == 8  # capped, broken entries skipped
        with pytest.raises(ApiParseError):
            await client.async_search_stops("notalist")

        departures = await client.async_departures("edge", 5)
        assert len(departures) == 1
        assert departures[0].line == "SEV 66"  # displayName fallback
        assert departures[0].cancelled is True
        assert departures[0].delay_s is None  # no realtime data


async def test_remaining_parse_paths(mocked_http: aioresponses) -> None:
    """Invalid stoptimes JSON and lat/lon type errors."""
    import json as _json

    mocked_http.get(f"{BASE_URL}/stoptimes?n=3&stopId=broken", body=b"not json")
    payload = [
        {"type": "STOP", "id": "t", "name": "TypeErr", "lat": None, "lon": 7.1},
        {"type": "STOP", "id": "v", "name": "ValErr", "lat": "abc", "lon": 7.1},
    ]
    mocked_http.get(
        f"{BASE_URL}/geocode?text=badcoords", body=_json.dumps(payload).encode()
    )
    async with aiohttp.ClientSession() as session:
        client = TransitousClient(session)
        with pytest.raises(ApiParseError):
            await client.async_departures("broken", 3)
        assert await client.async_search_stops("badcoords") == []
