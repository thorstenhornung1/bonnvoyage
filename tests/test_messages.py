"""Tests for geo_location lifecycle, event entity and messages sensor."""

from __future__ import annotations

import json

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bonnvoyage.api.bcp import BCP_URL
from custom_components.bonnvoyage.api.bonn_geo import ROADWORKS_URL
from custom_components.bonnvoyage.api.bonn_traffic import TRAFFIC_URL
from custom_components.bonnvoyage.const import (
    AUTOBAHN_ROADS,
    CONF_AREA,
    DOMAIN,
    MESSAGES_UPDATE_INTERVAL,
)

from .conftest import (
    autobahn_url,
    load_fixture_bytes,
    mock_config_entry,
)
from .test_init import _advance, _entry_with_markt, _setup

# messages_feeds fixture (autouse): 9 city roadworks + 3 A565 messages inside
# the 10 km AREA radius (one closure at 11.6 km is filtered out).
EXPECTED_IN_RADIUS = 12
EXPECTED_CITY = 9
EXPECTED_AUTOBAHN = 3


def _geo_states(hass: HomeAssistant, include_segments: bool = False):
    states = [
        state
        for state in hass.states.async_all()
        if state.entity_id.startswith("geo_location.")
    ]
    if include_segments:
        return states
    return [
        state
        for state in states
        if state.state == "unavailable"
        or state.attributes.get("message_source") != "city_traffic"
    ]


def _city_payload(features: list[dict]) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": features}).encode()


def _feature(baustelle_id: int, name: str, lon: float = 7.10, lat: float = 50.73):
    return {
        "properties": {"baustelle_id": baustelle_id, "bezeichnung": name},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


def _remock_feeds(
    aioclient_mock,
    *,
    city_content: bytes | None = None,
    city_status: int = 200,
    autobahn_status: int = 200,
    traffic_status: int = 200,
    traffic_content: bytes | None = None,
    with_bcp: bool = False,
) -> None:
    """Re-mock all feeds after clear_requests with custom message payloads."""
    aioclient_mock.clear_requests()
    if traffic_status != 200:
        aioclient_mock.get(TRAFFIC_URL, status=traffic_status)
    else:
        aioclient_mock.get(
            TRAFFIC_URL,
            content=(
                traffic_content
                if traffic_content is not None
                else load_fixture_bytes("verkehrslage.json")
            ),
        )
    if with_bcp:
        aioclient_mock.get(BCP_URL, content=load_fixture_bytes("bcpext.xml"))
    if city_status != 200:
        aioclient_mock.get(ROADWORKS_URL, status=city_status)
    else:
        aioclient_mock.get(
            ROADWORKS_URL,
            content=(
                city_content
                if city_content is not None
                else load_fixture_bytes("baustellen.json")
            ),
        )
    for road in AUTOBAHN_ROADS:
        for service in ("warning", "roadworks", "closure"):
            if autobahn_status != 200:
                aioclient_mock.get(autobahn_url(road, service), status=autobahn_status)
            elif road == "A565" and service in ("roadworks", "closure"):
                aioclient_mock.get(
                    autobahn_url(road, service),
                    content=load_fixture_bytes(f"autobahn_{service}.json"),
                )
            else:
                aioclient_mock.get(
                    autobahn_url(road, service),
                    content=f'{{"{service}": []}}'.encode(),
                )


async def test_geo_location_entities_created(hass: HomeAssistant) -> None:
    """Messages inside the radius appear as geo_location entities."""
    entry = mock_config_entry()
    await _setup(hass, entry)

    states = _geo_states(hass)
    assert len(states) == EXPECTED_IN_RADIUS

    state = hass.states.get("geo_location.hermann_wandersleb_ring_b56")
    assert state is not None
    assert state.state == "1.7"  # km from home
    assert state.attributes["source"] == DOMAIN
    assert state.attributes["external_id"] == "city_8921"
    assert state.attributes["kind"] == "roadwork"
    assert state.attributes["authority"] == "Straßen NRW"

    autobahn = [s for s in states if s.attributes.get("message_source") == "autobahn"]
    assert len(autobahn) == EXPECTED_AUTOBAHN
    assert all(s.attributes["road"] == "A565" for s in autobahn)


async def test_geo_location_lifecycle(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """Vanished messages remove their entity; new ones create it."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS

    # Next refresh: city feed has one known and one brand-new roadwork
    _remock_feeds(
        aioclient_mock,
        city_content=_city_payload(
            [
                _feature(8921, "Hermann-Wandersleb-Ring (B56)", 7.0774, 50.7294),
                _feature(99999, "Neue Baustelle"),
            ]
        ),
    )
    await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)

    assert len(_geo_states(hass)) == 2 + EXPECTED_AUTOBAHN
    assert hass.states.get("geo_location.neue_baustelle") is not None
    assert hass.states.get("geo_location.bennauerstrasse") is None
    registry = er.async_get(hass)
    assert registry.async_get("geo_location.bennauerstrasse") is None


async def test_geo_location_source_isolation(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A dead city feed does not touch autobahn entities."""
    entry = mock_config_entry()
    await _setup(hass, entry)

    _remock_feeds(aioclient_mock, city_status=503)

    # Stale tolerance is 3: entities survive three failed refreshes ...
    for _ in range(3):
        await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)
    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS
    assert all(s.state != "unavailable" for s in _geo_states(hass))

    # ... the fourth marks the city entities unavailable; autobahn stays live
    await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)
    unavailable = [s for s in _geo_states(hass) if s.state == "unavailable"]
    live = [s for s in _geo_states(hass) if s.state != "unavailable"]
    assert len(unavailable) == EXPECTED_CITY
    assert len(live) == EXPECTED_AUTOBAHN
    assert all(s.attributes["message_source"] == "autobahn" for s in live)


async def test_event_fires_only_for_new_messages(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """The event entity stays silent on the baseline and fires on news."""
    entry = mock_config_entry()
    await _setup(hass, entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, "new_message")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "unknown"  # baseline, no event

    _remock_feeds(
        aioclient_mock,
        city_content=_city_payload([_feature(77777, "Frische Baustelle")]),
    )
    await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)

    event = hass.states.get(entity_id)
    assert event.state != "unknown"
    assert event.attributes["event_type"] == "new_message"
    assert event.attributes["external_id"] == "city_77777"
    assert event.attributes["title"] == "Frische Baustelle"


async def test_active_messages_sensor(hass: HomeAssistant) -> None:
    """The counter sums both sources and lists the messages as attribute."""
    entry = mock_config_entry()
    await _setup(hass, entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, "active_messages")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state.state == str(EXPECTED_IN_RADIUS)
    assert len(state.attributes["messages"]) == EXPECTED_IN_RADIUS


async def test_setup_survives_dead_message_feeds(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Dead message sources must not block the setup (non-critical)."""
    _remock_feeds(aioclient_mock, city_status=503, autobahn_status=503, with_bcp=True)

    entry = _entry_with_markt()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.marktgarage_free_spaces").state == "251"
    assert not _geo_states(hass)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, "active_messages")
    assert hass.states.get(entity_id).state == "unavailable"


async def test_setup_without_parking_subentries(hass: HomeAssistant) -> None:
    """An entry without parking subentries loads without touching BCP."""
    # BCP_URL is deliberately NOT mocked here: a request would fail the test.
    entry = mock_config_entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS


async def test_radius_change_filters_messages(hass: HomeAssistant) -> None:
    """A smaller radius via options reduces the message set after reload."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_AREA: {"latitude": 50.7339, "longitude": 7.0997, "radius": 2000}},
    )
    await hass.async_block_till_done()

    # Inside 2 km: Brüdergasse (0.3), Schubertstraße (1.3), Bennauerstraße (1.5),
    # Hermann-Wandersleb-Ring (1.7) — no autobahn message closer than 2.3 km
    assert len(_geo_states(hass)) == 4
