"""Tests for geo_location lifecycle, event entity and messages sensor."""

from __future__ import annotations

import json

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
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
    autobahn_overrides: dict[tuple[str, str], bytes | int] | None = None,
) -> None:
    """Re-mock all feeds after clear_requests with custom message payloads.

    ``autobahn_overrides`` maps (road, service) to a payload or an HTTP status.
    """
    overrides = autobahn_overrides or {}
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
            override = overrides.get((road, service))
            if isinstance(override, int):
                aioclient_mock.get(autobahn_url(road, service), status=override)
            elif override is not None:
                aioclient_mock.get(autobahn_url(road, service), content=override)
            elif autobahn_status != 200:
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
    """New messages create an entity at once; vanished ones after the grace."""
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

    # First absent poll: vanished messages are kept (grace period)
    registry = er.async_get(hass)
    assert hass.states.get("geo_location.neue_baustelle") is not None
    assert hass.states.get("geo_location.bennauerstrasse") is not None
    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS + 1

    # Second consecutive absent poll: removed together with the registry entry
    await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)
    assert len(_geo_states(hass)) == 2 + EXPECTED_AUTOBAHN
    assert hass.states.get("geo_location.bennauerstrasse") is None
    assert registry.async_get("geo_location.bennauerstrasse") is None


async def test_geo_location_reappearing_message_keeps_entity(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A message missing for a single poll keeps its entity and registry id."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    registry = er.async_get(hass)
    registry_id = registry.async_get("geo_location.bennauerstrasse").id

    _remock_feeds(aioclient_mock, city_content=_city_payload([]))
    await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)
    _remock_feeds(aioclient_mock)
    await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)
    # The absence counter restarts after the message came back
    _remock_feeds(aioclient_mock, city_content=_city_payload([]))
    await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)

    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS
    assert registry.async_get("geo_location.bennauerstrasse").id == registry_id
    assert not registry.deleted_entities


async def _poll_autobahn(hass: HomeAssistant, entry) -> None:
    """Run exactly one Autobahn poll (independent of the timer jitter)."""
    await entry.runtime_data.autobahn.async_refresh()
    await hass.async_block_till_done()


async def test_partial_autobahn_failure_keeps_entities(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A failed road/service request never removes that road's messages."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    autobahn_ids = {
        s.entity_id
        for s in _geo_states(hass)
        if s.attributes.get("message_source") == "autobahn"
    }
    assert len(autobahn_ids) == EXPECTED_AUTOBAHN

    # A565 closures and roadworks fail, the other seven requests succeed
    _remock_feeds(
        aioclient_mock,
        autobahn_overrides={("A565", "closure"): 500, ("A565", "roadworks"): 500},
    )
    for _ in range(4):
        await _poll_autobahn(hass, entry)

    for entity_id in autobahn_ids:
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state != "unavailable"
    assert not er.async_get(hass).deleted_entities

    # Once the road answers again without them, the grace period applies
    _remock_feeds(
        aioclient_mock,
        autobahn_overrides={
            ("A565", "closure"): b'{"closure": []}',
            ("A565", "roadworks"): b'{"roadworks": []}',
        },
    )
    await _poll_autobahn(hass, entry)
    assert all(hass.states.get(entity_id) for entity_id in autobahn_ids)
    await _poll_autobahn(hass, entry)
    assert not any(hass.states.get(entity_id) for entity_id in autobahn_ids)


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


def _inrix_warning(counter: int) -> bytes:
    """An A555 INRIX traffic warning inside the radius (id changes per update)."""
    return json.dumps(
        {
            "warning": [
                {
                    "identifier": f"INRIX--vi-avl.2026-10-20_{counter}.de0",
                    "title": "A555 | Köln -> Bonn",
                    "subtitle": "Stockender Verkehr",
                    "coordinate": {"lat": 50.7250, "long": 7.0750},
                }
            ]
        }
    ).encode()


def _track_registry_removals(hass: HomeAssistant) -> list[str]:
    """Collect entity_ids removed from the entity registry from now on.

    The registry re-uses the id of a deleted entry when it is re-created, so
    the id alone cannot show a delete-and-recreate cycle; the events can.
    """
    removed: list[str] = []

    @callback
    def _listener(event) -> None:
        if event.data["action"] == "remove":
            removed.append(event.data["entity_id"])

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _listener)
    return removed


def _seed_registry(hass: HomeAssistant, entry, unique_suffix: str, object_id: str):
    """Pre-create a geo_location registry entry as left by a previous run."""
    return er.async_get(hass).async_get_or_create(
        "geo_location",
        DOMAIN,
        f"{entry.entry_id}_{unique_suffix}",
        config_entry=entry,
        suggested_object_id=object_id,
    )


async def test_setup_keeps_existing_geo_entities(hass: HomeAssistant) -> None:
    """Setup reuses current registry entries and reaps only vanished ones."""
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    kept = _seed_registry(hass, entry, "city_8921", "hermann_wandersleb_ring_b56")
    stale = [
        _seed_registry(hass, entry, "city_424242", "alte_baustelle"),
        _seed_registry(
            hass, entry, "autobahn_INRIX--vi-avl.2026-10-20_4711.de0", "a555_stau"
        ),
        _seed_registry(hass, entry, "segment_999", "alter_abschnitt"),
    ]

    removed = _track_registry_removals(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert sorted(removed) == sorted(reg_entry.entity_id for reg_entry in stale)
    assert registry.async_get(kept.entity_id).id == kept.id
    assert hass.states.get(kept.entity_id).attributes["external_id"] == "city_8921"
    for reg_entry in stale:
        assert registry.async_get(reg_entry.entity_id) is None
        assert hass.states.get(reg_entry.entity_id) is None
    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS


async def test_reload_does_not_wipe_geo_entities(hass: HomeAssistant) -> None:
    """A reload keeps every geo_location registry entry (0.7.1 deleted all)."""
    entry = mock_config_entry()
    await _setup(hass, entry)
    registry = er.async_get(hass)
    before = {
        reg.entity_id: reg.id
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain == "geo_location"
    }
    assert len(before) == EXPECTED_IN_RADIUS + 3  # + 3 disrupted segments

    removed = _track_registry_removals(hass)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not removed

    after = {
        reg.entity_id: reg.id
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain == "geo_location"
    }
    assert after == before
    assert not registry.deleted_entities


async def test_setup_with_failed_sources_removes_nothing(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """Failed or partial first refreshes never reap registry entries."""
    _remock_feeds(
        aioclient_mock,
        city_status=503,
        autobahn_overrides={("A555", "warning"): 500},
    )
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    stale_city = _seed_registry(hass, entry, "city_424242", "alte_baustelle")
    stale_autobahn = _seed_registry(hass, entry, "autobahn_gone", "alte_sperrung")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get(stale_city.entity_id) is not None
    assert registry.async_get(stale_autobahn.entity_id) is not None

    # First complete fetch per source: now the vanished entries are reaped
    _remock_feeds(aioclient_mock)
    await entry.runtime_data.city.async_refresh()
    await _poll_autobahn(hass, entry)
    assert registry.async_get(stale_city.entity_id) is None
    assert registry.async_get(stale_autobahn.entity_id) is None
    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS


async def test_inrix_warnings_get_no_geo_entity(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """INRIX warnings stay in sensor and event but never reach the map."""
    _remock_feeds(
        aioclient_mock, autobahn_overrides={("A555", "warning"): _inrix_warning(1)}
    )
    entry = mock_config_entry()
    await _setup(hass, entry)

    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS
    registry = er.async_get(hass)
    sensor_id = registry.async_get_entity_id("sensor", DOMAIN, "active_messages")
    state = hass.states.get(sensor_id)
    assert state.state == str(EXPECTED_IN_RADIUS + 1)
    assert any("INRIX" in str(message) for message in state.attributes["messages"])

    # The feed bumps the counter: still no map entity, nothing deleted
    _remock_feeds(
        aioclient_mock, autobahn_overrides={("A555", "warning"): _inrix_warning(2)}
    )
    await _poll_autobahn(hass, entry)
    assert len(_geo_states(hass)) == EXPECTED_IN_RADIUS
    assert not registry.deleted_entities
    assert hass.states.get(sensor_id).state == str(EXPECTED_IN_RADIUS + 1)
