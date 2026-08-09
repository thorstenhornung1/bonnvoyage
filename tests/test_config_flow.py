"""Tests for the BonnVoyage config, subentry and options flows."""

from __future__ import annotations

import aiohttp
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bonnvoyage.api.bcp import BCP_URL
from custom_components.bonnvoyage.const import (
    CONF_AREA,
    CONF_FACILITY_ID,
    CONF_PARKINGS,
    DOMAIN,
    SUBENTRY_TYPE_PARKING,
)

from .conftest import AREA, load_fixture_bytes, mock_config_entry


async def _start_user_flow(hass: HomeAssistant):
    """Start the flow and complete the area step with defaults."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AREA: AREA}
    )


async def test_full_flow_creates_entry_with_subentries(
    hass: HomeAssistant, bcp_feed: None
) -> None:
    """Happy path: area -> parking selection -> entry with subentries."""
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "parking"

    # Defaults: facilities within 3 km of home, capped at 5, stale ones hidden
    defaults = result["data_schema"]({})
    preselected = defaults[CONF_PARKINGS]
    assert 0 < len(preselected) <= 5
    assert "karstadt" not in preselected
    assert "rheinaue" not in preselected  # farther than 3 km from home
    assert "markt" in preselected

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PARKINGS: ["markt", "stadthaus"]}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "routes_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_AREA: AREA}

    entry = result["result"]
    subentries = list(entry.subentries.values())
    assert {sub.unique_id for sub in subentries} == {"markt", "stadthaus"}
    assert {sub.subentry_type for sub in subentries} == {SUBENTRY_TYPE_PARKING}
    titles = {sub.title for sub in subentries}
    assert "Marktgarage" in titles


async def test_stale_facilities_are_hidden(hass: HomeAssistant, bcp_feed: None) -> None:
    """Facilities with dead feed timestamps are not offered at all."""
    result = await _start_user_flow(hass)
    options = result["data_schema"].schema[CONF_PARKINGS].config["options"]
    values = {option["value"] for option in options}
    assert "karstadt" not in values
    assert "markt" in values
    assert len(values) == 13


async def test_options_sorted_by_distance_with_labels(
    hass: HomeAssistant, bcp_feed: None
) -> None:
    """Facility options are distance-sorted and labeled with kilometers."""
    result = await _start_user_flow(hass)
    options = result["data_schema"].schema[CONF_PARKINGS].config["options"]
    assert "km)" in options[0]["label"]
    # Rheinaue is the farthest facility from the city center home
    assert options[-1]["value"] == "rheinaue"


async def test_feed_down_menu_retry_and_finish(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Feed failure leads to a retry/finish menu; both paths work."""
    aioclient_mock.get(BCP_URL, exc=aiohttp.ClientConnectionError("down"))
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "parking_unavailable"

    # Retry with the feed back up
    aioclient_mock.clear_requests()
    aioclient_mock.get(BCP_URL, content=load_fixture_bytes("bcpext.xml"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "parking"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "parking"


async def test_feed_down_finish_without_parkings(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Finishing without parking facilities creates an entry without subentries."""
    aioclient_mock.get(BCP_URL, exc=aiohttp.ClientConnectionError("down"))
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "routes_menu"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "routes_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not result["result"].subentries


async def test_only_stale_facilities_shows_menu(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A feed with only dead entries is treated like an unavailable feed."""
    xml = (
        b'<?xml version="1.0" encoding="iso-8859-1"?><parkhaeuser><parkhaus>'
        b"<bezeichnung>karstadt</bezeichnung><gesamt>99</gesamt><frei>0</frei>"
        b"<status>2</status><zeitstempel>25.07.2018 09:13</zeitstempel>"
        b"<tendenz>3</tendenz></parkhaus></parkhaeuser>"
    )
    aioclient_mock.get(BCP_URL, content=xml)
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "parking_unavailable"


async def test_single_instance_allowed(hass: HomeAssistant) -> None:
    """A second config entry is rejected."""
    mock_config_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_subentry_flow_adds_parking(hass: HomeAssistant, bcp_feed: None) -> None:
    """The subentry flow offers remaining facilities and adds one."""
    entry = mock_config_entry(
        subentries_data=[
            config_entries.ConfigSubentryData(
                data={CONF_FACILITY_ID: "markt"},
                subentry_type=SUBENTRY_TYPE_PARKING,
                title="Marktgarage",
                unique_id="markt",
            )
        ],
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_PARKING),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM

    options = result["data_schema"].schema[CONF_FACILITY_ID].config["options"]
    values = {option["value"] for option in options}
    assert "markt" not in values  # already configured
    assert "stadthaus" in values

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_FACILITY_ID: "stadthaus"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentries = list(entry.subentries.values())
    assert {sub.unique_id for sub in subentries} == {"markt", "stadthaus"}
    new = next(sub for sub in subentries if sub.unique_id == "stadthaus")
    assert new.title == "Stadthausgarage"


async def test_subentry_flow_aborts(hass: HomeAssistant, aioclient_mock) -> None:
    """The subentry flow aborts when the feed is down."""
    entry = mock_config_entry()
    entry.add_to_hass(hass)

    aioclient_mock.get(BCP_URL, exc=aiohttp.ClientConnectionError("down"))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_PARKING),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "bcp_unavailable"


async def test_subentry_flow_all_configured(
    hass: HomeAssistant, bcp_feed: None
) -> None:
    """The subentry flow aborts when every fresh facility is configured."""
    fresh_ids = [
        "beethoven",
        "beetplatz",
        "friedensplatz",
        "markt",
        "muensterplatz",
        "operngarage",
        "rabinstrasse",
        "rheinaue",
        "stadthaus",
        "stadthaus_unten",
        "unigarage",
        "hauptbahnhof",
        "bike_ride",
    ]
    entry = mock_config_entry(
        subentries_data=[
            config_entries.ConfigSubentryData(
                data={CONF_FACILITY_ID: facility_id},
                subentry_type=SUBENTRY_TYPE_PARKING,
                title=facility_id,
                unique_id=facility_id,
            )
            for facility_id in fresh_ids
        ],
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_PARKING),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "all_configured"


async def test_options_flow_updates_area(hass: HomeAssistant, bcp_feed: None) -> None:
    """The options flow stores a new message area."""
    entry = mock_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    new_area = {"latitude": 50.72, "longitude": 7.11, "radius": 5000}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_AREA: new_area}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_AREA: new_area}


async def test_unknown_facility_gets_fallback_without_distance(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A facility missing from the static master data still shows up."""
    from .test_init import _feed_timestamp

    xml = (
        '<?xml version="1.0" encoding="iso-8859-1"?><parkhaeuser><parkhaus>'
        "<bezeichnung>testgarage</bezeichnung><gesamt>100</gesamt><frei>50</frei>"
        f"<status>0</status><zeitstempel>{_feed_timestamp()}</zeitstempel>"
        "<tendenz>3</tendenz></parkhaus><parkhaus>"
        "<bezeichnung>markt</bezeichnung><gesamt>287</gesamt><frei>251</frei>"
        f"<status>0</status><zeitstempel>{_feed_timestamp()}</zeitstempel>"
        "<tendenz>3</tendenz></parkhaus></parkhaeuser>"
    ).encode("iso-8859-1")
    aioclient_mock.get(BCP_URL, content=xml)

    result = await _start_user_flow(hass)
    options = result["data_schema"].schema[CONF_PARKINGS].config["options"]
    # Facilities without coordinates sort last and have no distance label
    assert options[-1]["value"] == "testgarage"
    assert "km)" not in options[-1]["label"]


async def test_options_flow_prefers_saved_options(
    hass: HomeAssistant, bcp_feed: None
) -> None:
    """A second options flow run starts from the previously saved area."""
    entry = mock_config_entry()
    entry.add_to_hass(hass)

    saved = {"latitude": 50.72, "longitude": 7.11, "radius": 5000}
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_AREA: saved}
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema_defaults = result["data_schema"]({})
    assert schema_defaults[CONF_AREA] == saved


async def test_subentry_flow_no_fresh_parkings(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The subentry flow aborts when the feed has only dead entries."""
    xml = (
        b'<?xml version="1.0" encoding="iso-8859-1"?><parkhaeuser><parkhaus>'
        b"<bezeichnung>karstadt</bezeichnung><gesamt>99</gesamt><frei>0</frei>"
        b"<status>2</status><zeitstempel>25.07.2018 09:13</zeitstempel>"
        b"<tendenz>3</tendenz></parkhaus></parkhaeuser>"
    )
    aioclient_mock.get(BCP_URL, content=xml)
    entry = mock_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_PARKING),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_parkings"


# --- M3: provider, reauth, route wizard, segment subentry ---

from custom_components.bonnvoyage.const import (  # noqa: E402
    CONF_ADD_VIA,
    CONF_API_KEY,
    CONF_DELAY_THRESHOLD,
    CONF_DESTINATION,
    CONF_LOCATION,
    CONF_NAME,
    CONF_PROVIDER,
    CONF_SEGMENT_ID,
    CONF_START,
    CONF_VIA,
    PICK_ON_MAP,
    SUBENTRY_TYPE_ROUTE,
    SUBENTRY_TYPE_SEGMENT,
)

from .conftest import (  # noqa: E402
    TOMTOM_VALIDATION_URL,
    mock_message_feeds,
    tomtom_url,
)

WIZARD_DEST = {"latitude": 50.7, "longitude": 7.15}
WIZARD_ROUTE_URL = tomtom_url((50.7339, 7.0997), (50.7, 7.15))


def _entry_with_provider(**kwargs):
    return mock_config_entry(**kwargs)


def _provider_entry(**kwargs):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import AREA as _AREA

    return MockConfigEntry(
        domain=DOMAIN,
        title="BonnVoyage",
        data={"area": _AREA, CONF_PROVIDER: "tomtom", CONF_API_KEY: "old-key"},
        **kwargs,
    )


def _set_home_zone(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "zone.home",
        "zoning",
        {"latitude": 50.7339, "longitude": 7.0997, "friendly_name": "Zuhause"},
    )


async def test_initial_flow_with_provider(
    hass: HomeAssistant, bcp_feed: None, aioclient_mock
) -> None:
    """Setting up a provider during onboarding stores it in entry data."""
    aioclient_mock.get(
        TOMTOM_VALIDATION_URL, content=load_fixture_bytes("tomtom_route.json")
    )
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PARKINGS: ["markt"]}
    )
    assert result["step_id"] == "routes_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "provider"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "provider"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PROVIDER: "tomtom", CONF_API_KEY: "fresh-key"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].data[CONF_PROVIDER] == "tomtom"
    assert result["result"].data[CONF_API_KEY] == "fresh-key"


async def test_provider_step_error_paths(
    hass: HomeAssistant, bcp_feed: None, aioclient_mock
) -> None:
    """Auth, rate limit and connection errors keep the provider form open."""
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PARKINGS: []}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "provider"}
    )

    for status, error in (
        (403, "invalid_auth"),
        (429, "rate_limited"),
        (500, "cannot_connect"),
    ):
        aioclient_mock.clear_requests()
        mock_message_feeds(aioclient_mock)
        aioclient_mock.get(TOMTOM_VALIDATION_URL, status=status)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PROVIDER: "tomtom", CONF_API_KEY: "k"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}


async def test_reauth_flow(hass: HomeAssistant, aioclient_mock) -> None:
    """Reauth validates the new key and updates the entry."""
    entry = _provider_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    # Wrong key first
    aioclient_mock.clear_requests()
    mock_message_feeds(aioclient_mock)
    aioclient_mock.get(TOMTOM_VALIDATION_URL, status=403)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    # Then a valid key
    aioclient_mock.clear_requests()
    mock_message_feeds(aioclient_mock)
    aioclient_mock.get(
        TOMTOM_VALIDATION_URL, content=load_fixture_bytes("tomtom_route.json")
    )
    aioclient_mock.get(BCP_URL, content=load_fixture_bytes("bcpext.xml"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new-key"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new-key"


async def test_route_wizard_zone_to_map(hass: HomeAssistant, aioclient_mock) -> None:
    """Happy path: zone start, map destination, probe route, subentry."""
    _set_home_zone(hass)
    entry = _provider_entry()
    entry.add_to_hass(hass)
    aioclient_mock.get(
        WIZARD_ROUTE_URL, content=load_fixture_bytes("tomtom_route.json")
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "start"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START: "zone.home"}
    )
    assert result["step_id"] == "dest"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_DESTINATION: PICK_ON_MAP}
    )
    assert result["step_id"] == "dest_map"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LOCATION: WIZARD_DEST}
    )
    assert result["step_id"] == "settings"
    # Name is prefilled from the endpoint labels
    defaults = result["data_schema"]({CONF_DELAY_THRESHOLD: 10})
    assert defaults[CONF_NAME].startswith("Zuhause →")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Pendelstrecke", CONF_DELAY_THRESHOLD: 10, CONF_ADD_VIA: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = next(
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_ROUTE
    )
    assert subentry.title == "Pendelstrecke"
    assert subentry.data[CONF_START]["latitude"] == 50.7339
    assert subentry.data[CONF_DESTINATION]["longitude"] == 7.15
    assert subentry.data[CONF_VIA] is None
    assert subentry.data[CONF_DELAY_THRESHOLD] == 10


async def test_route_wizard_with_via(hass: HomeAssistant, aioclient_mock) -> None:
    """The via checkbox adds a map step and the via point to the route."""
    _set_home_zone(hass)
    entry = _provider_entry()
    entry.add_to_hass(hass)
    via_url = tomtom_url((50.7339, 7.0997), (50.7, 7.15), via=(50.72, 7.12))
    aioclient_mock.get(via_url, content=load_fixture_bytes("tomtom_route.json"))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START: "zone.home"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_DESTINATION: PICK_ON_MAP}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LOCATION: WIZARD_DEST}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Über A565", CONF_DELAY_THRESHOLD: 15, CONF_ADD_VIA: True},
    )
    assert result["step_id"] == "via_map"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_LOCATION: {"latitude": 50.72, "longitude": 7.12}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_ROUTE
    )
    assert subentry.data[CONF_VIA] == {"latitude": 50.72, "longitude": 7.12}


async def test_route_wizard_inline_provider(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Without a configured provider the wizard starts with the key step."""
    _set_home_zone(hass)
    entry = mock_config_entry()  # no provider in data
    entry.add_to_hass(hass)
    aioclient_mock.get(
        TOMTOM_VALIDATION_URL, content=load_fixture_bytes("tomtom_route.json")
    )
    aioclient_mock.get(
        WIZARD_ROUTE_URL, content=load_fixture_bytes("tomtom_route.json")
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": "user"}
    )
    assert result["step_id"] == "provider"  # no dead end
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_PROVIDER: "tomtom", CONF_API_KEY: "inline-key"}
    )
    assert result["step_id"] == "start"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START: "zone.home"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_DESTINATION: PICK_ON_MAP}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LOCATION: WIZARD_DEST}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Neue Strecke", CONF_DELAY_THRESHOLD: 10, CONF_ADD_VIA: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The inline key was persisted on the parent entry
    assert entry.data[CONF_API_KEY] == "inline-key"
    assert entry.data[CONF_PROVIDER] == "tomtom"


async def test_route_wizard_points_too_close(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Endpoints closer than 100 m are rejected."""
    _set_home_zone(hass)
    entry = _provider_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START: "zone.home"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_DESTINATION: PICK_ON_MAP}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_LOCATION: {"latitude": 50.73391, "longitude": 7.09971}},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Zu kurz", CONF_DELAY_THRESHOLD: 10, CONF_ADD_VIA: False},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    assert result["errors"] == {"base": "points_too_close"}


async def test_route_wizard_name_exists_and_not_found(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Duplicate names and unroutable points surface as form errors."""
    _set_home_zone(hass)
    entry = _provider_entry(
        subentries_data=[
            config_entries.ConfigSubentryData(
                data={},
                subentry_type=SUBENTRY_TYPE_ROUTE,
                title="Pendelstrecke",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(
        WIZARD_ROUTE_URL,
        status=400,
        content=load_fixture_bytes("tomtom_error_no_route.json"),
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START: "zone.home"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_DESTINATION: PICK_ON_MAP}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LOCATION: WIZARD_DEST}
    )

    # Duplicate name
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Pendelstrecke", CONF_DELAY_THRESHOLD: 10, CONF_ADD_VIA: False},
    )
    assert result["errors"] == {"base": "name_exists"}

    # Unroutable points
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Rheinquerung", CONF_DELAY_THRESHOLD: 10, CONF_ADD_VIA: False},
    )
    assert result["errors"] == {"base": "route_not_found"}


async def test_segment_subentry_flow(hass: HomeAssistant, aioclient_mock) -> None:
    """Point -> named candidates -> explicit pick -> prefilled name."""
    aioclient_mock.get(
        "https://nominatim.openstreetmap.org/reverse",
        json={"name": "Bornheimer Straße", "address": {"road": "Bornheimer Straße"}},
    )
    entry = mock_config_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SEGMENT), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # Point on segment 100's midpoint
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_LOCATION: {"latitude": 50.7443, "longitude": 7.0907}},
    )
    assert result["step_id"] == "select"
    options = result["data_schema"].schema["segment_id"].config["options"]
    # Candidates are named, carry direction, distance and length anchors
    assert options[0]["value"] == "100"
    assert "Bornheimer Straße" in options[0]["label"]
    assert "Richtung" in options[0]["label"]
    assert "m entfernt" in options[0]["label"]
    # Escape hatch is always the last option
    assert options[-1]["value"] == "__repick__"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SEGMENT_ID: "100"}
    )
    assert result["step_id"] == "name"
    defaults = result["data_schema"]({})
    assert defaults["name"].startswith("Bornheimer Straße (")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Nordbrücke stadteinwärts"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(iter(entry.subentries.values()))
    assert subentry.title == "Nordbrücke stadteinwärts"
    assert subentry.unique_id == "100"
    assert subentry.data[CONF_SEGMENT_ID] == "100"
    assert subentry.data["street_name"] == "Bornheimer Straße"
    assert subentry.data["direction"] in ("N", "NO", "O", "SO", "S", "SW", "W", "NW")


async def test_segment_subentry_errors(hass: HomeAssistant, aioclient_mock) -> None:
    """Far points, repick, duplicates and geocoder outages are handled."""
    # Geocoder down: candidates fall back to "Abschnitt #"
    aioclient_mock.get("https://nominatim.openstreetmap.org/reverse", status=503)
    entry = mock_config_entry(
        subentries_data=[
            config_entries.ConfigSubentryData(
                data={CONF_SEGMENT_ID: "100"},
                subentry_type=SUBENTRY_TYPE_SEGMENT,
                title="Nordbrücke",
                unique_id="100",
            )
        ],
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SEGMENT), context={"source": "user"}
    )

    # No segment within 2 km of a far corner
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_LOCATION: {"latitude": 50.84, "longitude": 7.29}},
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "no_segment_nearby"}

    # Valid point: fallback labels, watched marker on the configured segment
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_LOCATION: {"latitude": 50.7443, "longitude": 7.0907}},
    )
    assert result["step_id"] == "select"
    options = result["data_schema"].schema["segment_id"].config["options"]
    assert options[0]["label"].startswith("Abschnitt #100")
    assert "wird bereits beobachtet" in options[0]["label"]

    # Escape hatch returns to the map with the last point preserved
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SEGMENT_ID: "__repick__"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_LOCATION: {"latitude": 50.7443, "longitude": 7.0907}},
    )

    # Picking the watched segment is a field error, not a dead end
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SEGMENT_ID: "100"}
    )
    assert result["errors"] == {"base": "already_configured"}


async def test_segment_subentry_feed_down(hass: HomeAssistant, aioclient_mock) -> None:
    """A dead traffic layer aborts the segment flow."""
    from custom_components.bonnvoyage.api.bonn_traffic import TRAFFIC_URL

    entry = mock_config_entry()
    entry.add_to_hass(hass)

    aioclient_mock.clear_requests()
    aioclient_mock.get(TRAFFIC_URL, status=503)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SEGMENT), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "segments_unavailable"


async def test_route_wizard_map_start_zone_dest(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The map-start plus zone-destination combination works."""
    _set_home_zone(hass)
    hass.states.async_set(
        "zone.arbeit",
        "zoning",
        {"latitude": 50.7, "longitude": 7.15, "friendly_name": "Arbeit"},
    )
    entry = _provider_entry()
    entry.add_to_hass(hass)
    aioclient_mock.get(
        tomtom_url((50.72, 7.11), (50.7, 7.15)),
        content=load_fixture_bytes("tomtom_route.json"),
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START: PICK_ON_MAP}
    )
    assert result["step_id"] == "start_map"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LOCATION: {"latitude": 50.72, "longitude": 7.11}}
    )
    assert result["step_id"] == "dest"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_DESTINATION: "zone.arbeit"}
    )
    assert result["step_id"] == "settings"
    defaults = result["data_schema"]({CONF_DELAY_THRESHOLD: 10})
    assert defaults[CONF_NAME].endswith("→ Arbeit")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Karte → Arbeit", CONF_DELAY_THRESHOLD: 10, CONF_ADD_VIA: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_route_wizard_inline_provider_error(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A rejected key keeps the inline provider form open."""
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    aioclient_mock.get(TOMTOM_VALIDATION_URL, status=403)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": "user"}
    )
    assert result["step_id"] == "provider"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_PROVIDER: "tomtom", CONF_API_KEY: "bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_route_probe_error_paths(hass: HomeAssistant, aioclient_mock) -> None:
    """Auth, rate limit and connection errors during the probe route."""
    _set_home_zone(hass)
    entry = _provider_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROUTE), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_START: "zone.home"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_DESTINATION: PICK_ON_MAP}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_LOCATION: WIZARD_DEST}
    )

    for status, error in (
        (403, "invalid_auth"),
        (429, "rate_limited"),
        (502, "cannot_connect"),
    ):
        aioclient_mock.clear_requests()
        mock_message_feeds(aioclient_mock)
        aioclient_mock.get(WIZARD_ROUTE_URL, status=status)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {CONF_NAME: "Fehlerfall", CONF_DELAY_THRESHOLD: 10, CONF_ADD_VIA: False},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}


async def test_segment_geocoder_timeout_and_rename(
    hass: HomeAssistant, aioclient_mock, monkeypatch
) -> None:
    """A geocoder timeout falls back to segment labels; names must be unique."""
    from custom_components.bonnvoyage.api.geocode import NominatimClient

    async def _hang(self, latitude, longitude):
        raise TimeoutError

    monkeypatch.setattr(NominatimClient, "async_reverse_street", _hang)

    entry = mock_config_entry(
        subentries_data=[
            config_entries.ConfigSubentryData(
                data={CONF_SEGMENT_ID: "999"},
                subentry_type=SUBENTRY_TYPE_SEGMENT,
                title="Bestand",
                unique_id="999",
            )
        ],
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SEGMENT), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_LOCATION: {"latitude": 50.7443, "longitude": 7.0907}},
    )
    assert result["step_id"] == "select"
    options = result["data_schema"].schema[CONF_SEGMENT_ID].config["options"]
    assert options[0]["label"].startswith("Abschnitt #")

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SEGMENT_ID: options[0]["value"]}
    )
    assert result["step_id"] == "name"
    defaults = result["data_schema"]({})
    assert defaults["name"].startswith("Abschnitt #")

    # Duplicate name is rejected, then a unique one succeeds
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Bestand"}
    )
    assert result["errors"] == {"base": "name_exists"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Neue Strecke"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
