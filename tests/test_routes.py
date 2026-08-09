"""Tests for configured routes: entities, values, auth failure, isolation."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntryState,
    ConfigSubentryData,
)
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bonnvoyage.const import (
    CONF_API_KEY,
    CONF_AREA,
    CONF_DELAY_THRESHOLD,
    CONF_DESTINATION,
    CONF_PROVIDER,
    CONF_SEGMENT_ID,
    CONF_START,
    CONF_VIA,
    DOMAIN,
    ROUTE_UPDATE_INTERVAL,
    SUBENTRY_TYPE_ROUTE,
)

from .conftest import (
    AREA,
    load_fixture_bytes,
    mock_config_entry,
    tomtom_url,
)
from .test_init import _advance, _setup

START = (50.7339, 7.0997)
DEST = (50.7, 7.15)
ROUTE_URL = tomtom_url(START, DEST)


def _entry_with_route(delay_threshold: int = 10) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="BonnVoyage",
        data={CONF_AREA: AREA, CONF_PROVIDER: "tomtom", CONF_API_KEY: "test-key"},
        subentries_data=[
            ConfigSubentryData(
                data={
                    CONF_START: {"latitude": START[0], "longitude": START[1]},
                    CONF_DESTINATION: {"latitude": DEST[0], "longitude": DEST[1]},
                    CONF_VIA: None,
                    CONF_DELAY_THRESHOLD: delay_threshold,
                },
                subentry_type=SUBENTRY_TYPE_ROUTE,
                title="Zuhause → Büro",
                unique_id=None,
            )
        ],
    )


async def test_route_entities_and_values(hass: HomeAssistant, aioclient_mock) -> None:
    """A route subentry produces duration, level and congestion entities."""
    aioclient_mock.get(ROUTE_URL, content=load_fixture_bytes("tomtom_route.json"))
    entry = _entry_with_route()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    travel = hass.states.get("sensor.zuhause_buro_travel_time")
    assert travel is not None
    # native seconds are converted to the suggested unit (minutes)
    assert float(travel.state) == pytest.approx(1685 / 60, abs=0.01)
    assert travel.attributes["unit_of_measurement"] == "min"
    assert travel.attributes["length_km"] == 24.3

    delay = hass.states.get("sensor.zuhause_buro_delay")
    assert float(delay.state) == pytest.approx(312 / 60, abs=0.01)

    # ratio 1685/1290 = 1.31 -> heavy
    assert hass.states.get("sensor.zuhause_buro_traffic_level").state == "heavy"

    # delay 5.2 min < threshold 10 min -> off
    congestion = hass.states.get("binary_sensor.zuhause_buro_congestion")
    assert congestion.state == "off"
    assert congestion.attributes["delay_threshold_min"] == 10


async def test_route_congestion_threshold(hass: HomeAssistant, aioclient_mock) -> None:
    """A lower threshold flips the congestion sensor on."""
    aioclient_mock.get(ROUTE_URL, content=load_fixture_bytes("tomtom_route.json"))
    entry = _entry_with_route(delay_threshold=5)
    await _setup(hass, entry)

    # delay 312 s >= 300 s -> on
    assert hass.states.get("binary_sensor.zuhause_buro_congestion").state == "on"


async def test_auth_failure_starts_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    """A rejected key aborts the setup and starts the reauth flow."""
    aioclient_mock.get(ROUTE_URL, status=403)
    entry = _entry_with_route()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


async def test_route_stale_tolerance_and_isolation(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """Route outages tolerate one refresh; segments stay unaffected."""
    aioclient_mock.get(ROUTE_URL, content=load_fixture_bytes("tomtom_route.json"))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="BonnVoyage",
        data={CONF_AREA: AREA, CONF_PROVIDER: "tomtom", CONF_API_KEY: "test-key"},
        subentries_data=[
            ConfigSubentryData(
                data={
                    CONF_START: {"latitude": START[0], "longitude": START[1]},
                    CONF_DESTINATION: {"latitude": DEST[0], "longitude": DEST[1]},
                    CONF_VIA: None,
                    CONF_DELAY_THRESHOLD: 10,
                },
                subentry_type=SUBENTRY_TYPE_ROUTE,
                title="Zuhause → Büro",
                unique_id=None,
            ),
            ConfigSubentryData(
                data={CONF_SEGMENT_ID: "49"},
                subentry_type="segment",
                title="Adenauerallee",
                unique_id="49",
            ),
        ],
    )
    await _setup(hass, entry)
    assert hass.states.get("sensor.zuhause_buro_travel_time").state != "unavailable"

    from .test_messages import _remock_feeds

    _remock_feeds(aioclient_mock)
    aioclient_mock.get(ROUTE_URL, exc=aiohttp.ClientConnectionError("down"))

    # First failure: stale data is kept
    await _advance(hass, freezer, ROUTE_UPDATE_INTERVAL)
    travel = hass.states.get("sensor.zuhause_buro_travel_time")
    assert travel.state != "unavailable"

    # Second consecutive failure: route entities go unavailable
    await _advance(hass, freezer, ROUTE_UPDATE_INTERVAL)
    assert hass.states.get("sensor.zuhause_buro_travel_time").state == "unavailable"

    # The watched segment keeps working (source isolation)
    assert hass.states.get("sensor.adenauerallee_traffic_status").state != "unavailable"


async def test_route_without_provider_is_skipped(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Route subentries without a provider load without route entities."""
    entry = mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={
                    CONF_START: {"latitude": START[0], "longitude": START[1]},
                    CONF_DESTINATION: {"latitude": DEST[0], "longitude": DEST[1]},
                    CONF_VIA: None,
                    CONF_DELAY_THRESHOLD: 10,
                },
                subentry_type=SUBENTRY_TYPE_ROUTE,
                title="Zuhause → Büro",
                unique_id=None,
            )
        ],
    )
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.zuhause_buro_travel_time") is None
