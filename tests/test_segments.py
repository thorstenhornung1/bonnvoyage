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

    # 0.7.2: no speed in the name (it churned the recorded attributes)
    risky = hass.states.get("geo_location.staugefahr")
    assert risky is not None
    assert risky.name == "Staugefahr"
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
    """Recovered segments remove their map entity after the grace period."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    assert len(_segment_geo_states(hass)) == EXPECTED_DISRUPTED

    from .test_messages import _remock_feeds

    _remock_feeds(aioclient_mock, traffic_content=_all_normal_layer())
    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)

    # First recovered poll: the counter reacts, the map entities stay (grace)
    registry = er.async_get(hass)
    counter_id = registry.async_get_entity_id("sensor", DOMAIN, "disrupted_segments")
    assert hass.states.get(counter_id).state == "0"
    assert len(_segment_geo_states(hass)) == EXPECTED_DISRUPTED

    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)
    assert not _segment_geo_states(hass)
    assert registry.async_get("geo_location.staugefahr") is None


async def test_segment_flapping_keeps_entity(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A segment missing for a single poll keeps its entity and registry id."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    registry = er.async_get(hass)
    registry_id = registry.async_get("geo_location.staugefahr").id

    from .test_messages import _remock_feeds

    _remock_feeds(aioclient_mock, traffic_content=_all_normal_layer())
    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)
    _remock_feeds(aioclient_mock)
    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)
    # The absence counter restarts: one more normal poll is not enough
    _remock_feeds(aioclient_mock, traffic_content=_all_normal_layer())
    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)

    assert len(_segment_geo_states(hass)) == EXPECTED_DISRUPTED
    assert registry.async_get("geo_location.staugefahr").id == registry_id
    assert not registry.deleted_entities


async def test_segments_survive_failed_traffic_feed(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """Failed traffic polls never count as absence."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    entity_ids = [state.entity_id for state in _segment_geo_states(hass)]

    from .test_messages import _remock_feeds

    _remock_feeds(aioclient_mock, traffic_status=503)
    for _ in range(5):  # beyond the stale tolerance: entities go unavailable
        await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)
    states = [hass.states.get(entity_id) for entity_id in entity_ids]
    assert all(state.state == "unavailable" for state in states)
    assert not er.async_get(hass).deleted_entities


async def test_segment_volatile_attributes_not_recorded(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """Speed and measurement time stay out of the recorded attributes."""
    from homeassistant.components.recorder.db_schema import StateAttributes
    from homeassistant.const import EVENT_STATE_CHANGED
    from homeassistant.core import Event

    def _recorded(entity_id: str) -> dict:
        """Attributes exactly as the recorder would store them."""
        state = hass.states.get(entity_id)
        event = Event(
            EVENT_STATE_CHANGED,
            {"entity_id": entity_id, "old_state": None, "new_state": state},
        )
        return json.loads(StateAttributes.shared_attrs_bytes_from_event(event, None))

    entry = mock_config_entry()
    await _setup(hass, entry)
    entity_id = "geo_location.staugefahr"
    assert hass.states.get(entity_id).attributes["speed_kmh"] == 4
    assert "measured_at" in hass.states.get(entity_id).attributes
    before = _recorded(entity_id)
    assert before["segment_id"] == "100"
    assert "speed_kmh" not in before
    assert "measured_at" not in before

    # A new speed and timestamp must not create a new attributes row
    data = json.loads(load_fixture_bytes("verkehrslage.json"))
    for feature in data["features"]:
        feature["properties"]["geschwindigkeit"] = 7

    from .test_messages import _remock_feeds

    _remock_feeds(aioclient_mock, traffic_content=json.dumps(data).encode())
    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)
    assert hass.states.get(entity_id).attributes["speed_kmh"] == 7
    assert _recorded(entity_id) == before


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


async def test_geocode_edge_cases() -> None:
    """Non-dict responses and missing road names return None."""
    import aiohttp
    from aioresponses import aioresponses

    from custom_components.bonnvoyage.api.geocode import (
        NOMINATIM_URL,
        NominatimClient,
    )

    with aioresponses() as mock:
        url = f"{NOMINATIM_URL}?format=jsonv2&lat=50.70000&lon=7.00000&zoom=16"
        mock.get(url, body=b"[1, 2]")
        mock.get(url, body=b'{"name": null, "address": {}}')
        mock.get(url, body=b'{"name": "Fallback-Name", "address": {}}')
        async with aiohttp.ClientSession() as session:
            client = NominatimClient(session)
            assert await client.async_reverse_street(50.7, 7.0) is None
            assert await client.async_reverse_street(50.7, 7.0) is None
            assert await client.async_reverse_street(50.7, 7.0) == "Fallback-Name"


def _corridor_entry():
    return mock_config_entry(
        subentries_data=[
            ConfigSubentryData(
                data={
                    "segment_ids": ["49", "100", "144"],
                    "segment_meta": {
                        "49": {"street_name": "Bornheimer Straße", "direction": "NO"},
                        "100": {"street_name": "Nordbrücke", "direction": "SW"},
                        "144": {"street_name": "Petersbergstraße", "direction": "S"},
                    },
                },
                subentry_type=SUBENTRY_TYPE_SEGMENT,
                title="Pendelachse Nord",
                unique_id=None,
            )
        ],
    )


async def test_corridor_aggregation(hass: HomeAssistant) -> None:
    """A corridor aggregates worst status, min speed and disruption count."""
    entry = _corridor_entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    # Fixture: 49=elevated(19), 100=congestion_risk(4), 144=normal(58)
    status = hass.states.get("sensor.pendelachse_nord_traffic_situation")
    assert status is not None
    assert status.state == "congestion_risk"  # worst along the corridor
    segments = status.attributes["segments"]
    assert len(segments) == 3
    assert segments[0]["segment"] == "Bornheimer Straße (NO)"

    assert hass.states.get("sensor.pendelachse_nord_slowest_segment").state == "4"
    assert hass.states.get("sensor.pendelachse_nord_disrupted_segments").state == "2"
    assert hass.states.get("binary_sensor.pendelachse_nord_congestion").state == "on"


async def test_corridor_recovers(hass: HomeAssistant, aioclient_mock, freezer) -> None:
    """A corridor flips back to normal after recovery."""
    entry = _corridor_entry()
    await _setup(hass, entry)

    from .test_messages import _remock_feeds

    _remock_feeds(aioclient_mock, traffic_content=_all_normal_layer())
    await _advance(hass, freezer, SEGMENT_UPDATE_INTERVAL)

    assert (
        hass.states.get("sensor.pendelachse_nord_traffic_situation").state == "normal"
    )
    assert hass.states.get("binary_sensor.pendelachse_nord_congestion").state == "off"
    assert hass.states.get("sensor.pendelachse_nord_disrupted_segments").state == "0"
