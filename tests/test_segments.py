"""Tests for city traffic segments: automatic map entities and watched devices."""

from __future__ import annotations

import json

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bonnvoyage.const import (
    CONF_SEGMENT_ID,
    DOMAIN,
    SEGMENT_UPDATE_INTERVAL,
    SUBENTRY_TYPE_SEGMENT,
)

from .conftest import load_fixture_bytes, mock_config_entry
from .test_init import _advance, _setup

# Fixture: segments 49 (elevated, 19 km/h), 100 (congestion_risk, 4 km/h)
# and 127 (elevated, 23 km/h) are disrupted; all inside the 10 km radius.
EXPECTED_DISRUPTED = 3


def _segment_geo_states(hass: HomeAssistant):
    return [
        state
        for state in hass.states.async_all()
        if state.entity_id.startswith("geo_location.")
        and state.attributes.get("message_source") == "city_traffic"
    ]


def _all_normal_layer() -> bytes:
    """The real layer with every segment set to normal."""
    data = json.loads(load_fixture_bytes("verkehrslage.json"))
    for feature in data["features"]:
        feature["properties"]["verkehrsstatus"] = "normales Verkehrsaufkommen"
        feature["properties"]["geschwindigkeit"] = 55
    return json.dumps(data).encode()


async def test_disrupted_segments_on_map(hass: HomeAssistant) -> None:
    """Disrupted segments inside the radius appear as geo_location."""
    entry = mock_config_entry()
    await _setup(hass, entry)

    states = _segment_geo_states(hass)
    assert len(states) == EXPECTED_DISRUPTED

    risky = hass.states.get("geo_location.staugefahr_4_km_h")
    assert risky is not None
    assert risky.attributes["segment_id"] == "100"
    assert risky.attributes["status"] == "congestion_risk"
    assert risky.attributes["speed_kmh"] == 4
    assert float(risky.state) < 3  # ~1.3 km from home

    registry = er.async_get(hass)
    counter_id = registry.async_get_entity_id("sensor", DOMAIN, "disrupted_segments")
    assert hass.states.get(counter_id).state == str(EXPECTED_DISRUPTED)


async def test_segments_clear_when_traffic_normalizes(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """Recovered segments remove their map entity and reset the counter."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    assert len(_segment_geo_states(hass)) == EXPECTED_DISRUPTED

    from .test_messages import _remock_feeds

    _remock_feeds(aioclient_mock, traffic_content=_all_normal_layer())
    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)

    assert not _segment_geo_states(hass)
    registry = er.async_get(hass)
    counter_id = registry.async_get_entity_id("sensor", DOMAIN, "disrupted_segments")
    assert hass.states.get(counter_id).state == "0"


def _entry_with_segment(segment_id: str, title: str):
    return mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={CONF_SEGMENT_ID: segment_id},
                subentry_type=SUBENTRY_TYPE_SEGMENT,
                title=title,
                unique_id=segment_id,
            )
        ],
    )


async def test_watched_segment_device(hass: HomeAssistant) -> None:
    """A watched segment gets status, speed and congestion entities."""
    entry = _entry_with_segment("49", "Adenauerallee")
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    status = hass.states.get("sensor.adenauerallee_traffic_status")
    assert status is not None
    assert status.state == "elevated"
    assert status.attributes["raw_status"] == "erhöhte Verkehrsbelastung"
    assert status.attributes["segment_id"] == "49"
    assert "latitude" in status.attributes  # map position

    assert hass.states.get("sensor.adenauerallee_speed").state == "19"
    assert hass.states.get("binary_sensor.adenauerallee_congestion").state == "on"


async def test_watched_segment_unknown_status(hass: HomeAssistant) -> None:
    """A not-measurable segment reports unknown congestion."""
    entry = _entry_with_segment("142", "Messlücke")
    await _setup(hass, entry)

    assert hass.states.get("sensor.messlucke_traffic_status").state == "unknown"
    assert hass.states.get("sensor.messlucke_speed").state == "unknown"
    assert hass.states.get("binary_sensor.messlucke_congestion").state == "unknown"


async def test_watched_segment_recovers(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A watched segment flips back to normal after recovery."""
    entry = _entry_with_segment("100", "Nordbrücke")
    await _setup(hass, entry)
    assert hass.states.get("binary_sensor.nordbrucke_congestion").state == "on"

    from .test_messages import _remock_feeds

    _remock_feeds(aioclient_mock, traffic_content=_all_normal_layer())
    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)

    assert hass.states.get("binary_sensor.nordbrucke_congestion").state == "off"
    assert hass.states.get("sensor.nordbrucke_traffic_status").state == "normal"
