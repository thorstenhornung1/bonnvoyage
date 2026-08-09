"""Tests for BonnVoyage setup, unload and coordinator behavior."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.bonnvoyage.api.bcp import BCP_URL
from custom_components.bonnvoyage.const import (
    CONF_FACILITY_ID,
    PARKING_UPDATE_INTERVAL,
    SUBENTRY_TYPE_PARKING,
)

from .conftest import load_fixture_bytes, mock_config_entry


def _feed_timestamp() -> str:
    """Current time formatted as the feed does (local Bonn time)."""
    return (
        dt_util.utcnow()
        .astimezone(ZoneInfo("Europe/Berlin"))
        .strftime("%d.%m.%Y %H:%M")
    )


def _entry_with_markt():
    return mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={CONF_FACILITY_ID: "markt"},
                subentry_type=SUBENTRY_TYPE_PARKING,
                title="Marktgarage",
                unique_id="markt",
            )
        ],
    )


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _advance(hass: HomeAssistant, freezer, interval: timedelta) -> None:
    freezer.tick(interval)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_setup_creates_entities(hass: HomeAssistant, bcp_feed: None) -> None:
    """A parking subentry produces its sensors with correct values."""
    entry = _entry_with_markt()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    free = hass.states.get("sensor.marktgarage_free_spaces")
    assert free is not None
    assert free.state == "251"
    assert free.attributes["latitude"] == 50.73474
    assert free.attributes["longitude"] == 7.10323
    assert free.attributes["capacity"] == 287

    occupancy = hass.states.get("sensor.marktgarage_occupancy")
    assert occupancy is not None
    assert occupancy.state == "12.5"  # (287-251)/287

    trend = hass.states.get("sensor.marktgarage_trend")
    assert trend is not None
    assert trend.state == "unknown"  # buffer not filled yet

    assert hass.states.get("binary_sensor.marktgarage_open").state == "on"
    assert hass.states.get("binary_sensor.marktgarage_full").state == "off"


async def test_setup_retries_when_feed_down(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A dead feed during setup leads to setup retry."""
    aioclient_mock.get(BCP_URL, exc=aiohttp.ClientConnectionError("down"))
    entry = _entry_with_markt()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_entry(hass: HomeAssistant, bcp_feed: None) -> None:
    """The entry unloads cleanly."""
    entry = _entry_with_markt()
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_stale_tolerance_keeps_data_then_goes_unavailable(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """Brief feed outages keep stale data; persistent ones go unavailable."""
    aioclient_mock.get(BCP_URL, content=load_fixture_bytes("bcpext.xml"))
    entry = _entry_with_markt()
    await _setup(hass, entry)
    assert hass.states.get("sensor.marktgarage_free_spaces").state == "251"

    # Feed goes down: two refreshes are tolerated with stale data
    aioclient_mock.clear_requests()
    aioclient_mock.get(BCP_URL, exc=aiohttp.ClientConnectionError("down"))
    await _advance(hass, freezer, PARKING_UPDATE_INTERVAL)
    assert hass.states.get("sensor.marktgarage_free_spaces").state == "251"
    await _advance(hass, freezer, PARKING_UPDATE_INTERVAL)
    assert hass.states.get("sensor.marktgarage_free_spaces").state == "251"

    # Third consecutive failure: entities become unavailable
    await _advance(hass, freezer, PARKING_UPDATE_INTERVAL)
    assert hass.states.get("sensor.marktgarage_free_spaces").state == "unavailable"


async def test_missing_facility_is_unavailable_others_stay(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A facility vanishing from the feed only affects its own entities."""
    aioclient_mock.get(BCP_URL, content=load_fixture_bytes("bcpext.xml"))
    entry = mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={CONF_FACILITY_ID: facility_id},
                subentry_type=SUBENTRY_TYPE_PARKING,
                title=facility_id,
                unique_id=facility_id,
            )
            for facility_id in ("markt", "stadthaus")
        ],
    )
    await _setup(hass, entry)

    xml_without_markt = (
        '<?xml version="1.0" encoding="iso-8859-1"?><parkhaeuser><parkhaus>'
        "<bezeichnung>stadthaus</bezeichnung><gesamt>320</gesamt><frei>200</frei>"
        f"<status>0</status><zeitstempel>{_feed_timestamp()}</zeitstempel>"
        "<tendenz>3</tendenz></parkhaus></parkhaeuser>"
    ).encode("iso-8859-1")
    aioclient_mock.clear_requests()
    aioclient_mock.get(BCP_URL, content=xml_without_markt)
    await _advance(hass, freezer, PARKING_UPDATE_INTERVAL)

    assert hass.states.get("sensor.marktgarage_free_spaces").state == "unavailable"
    assert hass.states.get("sensor.stadthausgarage_free_spaces").state == "200"


async def test_trend_detects_filling(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A significant drop in free spaces over 15 minutes reports 'filling'."""
    entry = _entry_with_markt()

    def _feed(free: int) -> bytes:
        return (
            '<?xml version="1.0" encoding="iso-8859-1"?><parkhaeuser><parkhaus>'
            f"<bezeichnung>markt</bezeichnung><gesamt>287</gesamt><frei>{free}</frei>"
            f"<status>0</status><zeitstempel>{_feed_timestamp()}</zeitstempel>"
            "<tendenz>3</tendenz></parkhaus></parkhaeuser>"
        ).encode("iso-8859-1")

    aioclient_mock.get(BCP_URL, content=_feed(251))
    await _setup(hass, entry)
    assert hass.states.get("sensor.marktgarage_trend").state == "unknown"

    # Free spaces drop by 10+ per refresh over ~16 minutes
    for free in (240, 230, 220, 210, 200, 190, 180, 170):
        aioclient_mock.clear_requests()
        aioclient_mock.get(BCP_URL, content=_feed(free))
        await _advance(hass, freezer, PARKING_UPDATE_INTERVAL)

    assert hass.states.get("sensor.marktgarage_trend").state == "filling"

    # Stable values for another window flips the trend back to steady
    for _ in range(9):
        aioclient_mock.clear_requests()
        aioclient_mock.get(BCP_URL, content=_feed(170))
        await _advance(hass, freezer, PARKING_UPDATE_INTERVAL)

    assert hass.states.get("sensor.marktgarage_trend").state == "steady"


async def test_closed_facility_states(hass: HomeAssistant, bcp_feed: None) -> None:
    """A closed facility reports open=off, full=unknown, trend=unknown."""
    entry = mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={CONF_FACILITY_ID: "beethoven"},
                subentry_type=SUBENTRY_TYPE_PARKING,
                title="Beethoven-Parkhaus",
                unique_id="beethoven",
            )
        ],
    )
    await _setup(hass, entry)

    assert hass.states.get("binary_sensor.beethoven_parkhaus_open").state == "off"
    assert hass.states.get("binary_sensor.beethoven_parkhaus_full").state == "unknown"
    assert hass.states.get("sensor.beethoven_parkhaus_trend").state == "unknown"
    assert hass.states.get("sensor.beethoven_parkhaus_free_spaces").state == "0"


async def test_unknown_facility_creates_no_entities(
    hass: HomeAssistant, bcp_feed: None
) -> None:
    """A subentry pointing at a facility missing from the feed is skipped."""
    entry = mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={CONF_FACILITY_ID: "geisterhaus"},
                subentry_type=SUBENTRY_TYPE_PARKING,
                title="Geisterhaus",
                unique_id="geisterhaus",
            ),
            # Subentry type from a future milestone is skipped by the platforms
            ConfigSubentryData(
                data={},
                subentry_type="route",
                title="Zukunftsstrecke",
                unique_id="future_route",
            ),
        ],
    )
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert not [
        state
        for state in hass.states.async_all()
        if "geisterhaus" in state.entity_id or "zukunftsstrecke" in state.entity_id
    ]


async def test_occupancy_unknown_without_capacity(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Missing capacity leaves occupancy unknown but keeps free spaces."""
    xml = (
        '<?xml version="1.0" encoding="iso-8859-1"?><parkhaeuser><parkhaus>'
        "<bezeichnung>markt</bezeichnung><frei>100</frei>"
        f"<status>0</status><zeitstempel>{_feed_timestamp()}</zeitstempel>"
        "</parkhaus></parkhaeuser>"
    ).encode("iso-8859-1")
    aioclient_mock.get(BCP_URL, content=xml)
    entry = _entry_with_markt()
    await _setup(hass, entry)

    assert hass.states.get("sensor.marktgarage_free_spaces").state == "100"
    assert hass.states.get("sensor.marktgarage_occupancy").state == "unknown"
    free = hass.states.get("sensor.marktgarage_free_spaces")
    assert free.attributes["capacity"] is None


async def test_reload_on_options_change(hass: HomeAssistant, bcp_feed: None) -> None:
    """Changing options reloads the entry."""
    entry = _entry_with_markt()
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"area": {"latitude": 50.72, "longitude": 7.11, "radius": 5000}},
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.marktgarage_free_spaces").state == "251"


async def test_trend_detects_emptying(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A significant rise in free spaces over 15 minutes reports 'emptying'."""
    entry = _entry_with_markt()

    def _feed(free: int) -> bytes:
        return (
            '<?xml version="1.0" encoding="iso-8859-1"?><parkhaeuser><parkhaus>'
            f"<bezeichnung>markt</bezeichnung><gesamt>287</gesamt><frei>{free}</frei>"
            f"<status>0</status><zeitstempel>{_feed_timestamp()}</zeitstempel>"
            "<tendenz>3</tendenz></parkhaus></parkhaeuser>"
        ).encode("iso-8859-1")

    aioclient_mock.get(BCP_URL, content=_feed(100))
    await _setup(hass, entry)

    for free in (110, 120, 130, 140, 150, 160, 170, 180):
        aioclient_mock.clear_requests()
        aioclient_mock.get(BCP_URL, content=_feed(free))
        await _advance(hass, freezer, PARKING_UPDATE_INTERVAL)

    assert hass.states.get("sensor.marktgarage_trend").state == "emptying"
