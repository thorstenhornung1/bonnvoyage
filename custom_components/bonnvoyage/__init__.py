"""The BonnVoyage integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.autobahn import AutobahnClient
from .api.bcp import BcpClient
from .api.bonn_geo import BonnGeoClient
from .api.bonn_traffic import BonnTrafficClient
from .api.models import RoutePoint, RouteRequest
from .api.routing import create_provider
from .api.transitous import TransitousClient
from .const import (
    CONF_API_KEY,
    CONF_DESTINATION,
    CONF_PROVIDER,
    CONF_START,
    CONF_STOP_ID,
    CONF_VIA,
    SUBENTRY_TYPE_PARKING,
    SUBENTRY_TYPE_ROUTE,
    SUBENTRY_TYPE_STOP,
)
from .coordinator import (
    AutobahnCoordinator,
    BcpCoordinator,
    BonnGeoCoordinator,
    BonnTrafficCoordinator,
    BonnVoyageConfigEntry,
    BonnVoyageRuntimeData,
    RouteCoordinator,
    StopCoordinator,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.GEO_LOCATION,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: BonnVoyageConfigEntry) -> bool:
    """Set up BonnVoyage from a config entry."""
    session = async_get_clientsession(hass)

    # Parking is setup-critical, but only when parking subentries exist.
    parking: BcpCoordinator | None = None
    if any(
        subentry.subentry_type == SUBENTRY_TYPE_PARKING
        for subentry in entry.subentries.values()
    ):
        parking = BcpCoordinator(hass, entry, BcpClient(session))
        await parking.async_config_entry_first_refresh()

    # Message and city traffic sources are non-critical: a failed first
    # refresh must not block the setup (they retry on their interval).
    city = BonnGeoCoordinator(hass, entry, BonnGeoClient(session))
    autobahn = AutobahnCoordinator(hass, entry, AutobahnClient(session))
    traffic = BonnTrafficCoordinator(hass, entry, BonnTrafficClient(session))
    await city.async_refresh()
    await autobahn.async_refresh()
    await traffic.async_refresh()

    runtime = BonnVoyageRuntimeData(
        parking=parking, city=city, autobahn=autobahn, traffic=traffic
    )

    # Routes are setup-critical once a provider is configured (auth failures
    # must surface as reauth).
    route_subentries = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_ROUTE
    ]
    provider_name = entry.data.get(CONF_PROVIDER)
    api_key = entry.data.get(CONF_API_KEY)
    if route_subentries and provider_name and api_key:
        runtime.provider = create_provider(provider_name, session, api_key)
        for subentry in route_subentries:
            coordinator = RouteCoordinator(
                hass,
                entry,
                runtime.provider,
                subentry.subentry_id,
                _route_request(subentry.data),
            )
            await coordinator.async_config_entry_first_refresh()
            runtime.routes[subentry.subentry_id] = coordinator
    elif route_subentries:
        _LOGGER.warning("Route subentries configured but no routing provider is set up")

    # Transit stops poll a community service without SLA: not setup-critical.
    stop_client = TransitousClient(session)
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_STOP:
            continue
        stop_coordinator = StopCoordinator(
            hass, entry, stop_client, subentry.data[CONF_STOP_ID], subentry.subentry_id
        )
        await stop_coordinator.async_refresh()
        runtime.stops[subentry.subentry_id] = stop_coordinator

    # Stale geo_location registry entries are reaped by the geo_location
    # platform per source, after that source's first complete fetch.
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _route_request(data: Any) -> RouteRequest:
    """Build a RouteRequest from route subentry data."""

    def _point(value: dict[str, float]) -> RoutePoint:
        return RoutePoint(value["latitude"], value["longitude"])

    via = data.get(CONF_VIA)
    return RouteRequest(
        start=_point(data[CONF_START]),
        destination=_point(data[CONF_DESTINATION]),
        via=_point(via) if via else None,
    )


async def _async_update_listener(
    hass: HomeAssistant, entry: BonnVoyageConfigEntry
) -> None:
    """Reload the entry when options or subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BonnVoyageConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
