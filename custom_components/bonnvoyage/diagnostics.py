"""Diagnostics support for BonnVoyage."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .coordinator import BonnVoyageConfigEntry

# Privacy: the API key and every coordinate (home area, route endpoints)
# must never leave the house via a diagnostics dump.
TO_REDACT = {
    "api_key",
    "area",
    "start",
    "destination",
    "via",
    "latitude",
    "longitude",
}


def _coordinator_info(
    coordinator: DataUpdateCoordinator[Any] | None,
) -> dict[str, Any] | None:
    """Health summary of one coordinator without payload data."""
    if coordinator is None:
        return None
    data = coordinator.data
    return {
        "last_update_success": coordinator.last_update_success,
        "items": len(data) if isinstance(data, dict) else int(data is not None),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BonnVoyageConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for the config entry."""
    runtime = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
            "subentries": [
                {
                    "type": subentry.subentry_type,
                    "data": async_redact_data(dict(subentry.data), TO_REDACT),
                }
                for subentry in entry.subentries.values()
            ],
        },
        "sources": {
            "parking": _coordinator_info(runtime.parking),
            "city_messages": _coordinator_info(runtime.city),
            "autobahn_messages": _coordinator_info(runtime.autobahn),
            "city_traffic": _coordinator_info(runtime.traffic),
            "routes": {
                subentry_id: _coordinator_info(coordinator)
                for subentry_id, coordinator in runtime.routes.items()
            },
            "routing_provider": (
                runtime.provider.provider_id if runtime.provider else None
            ),
        },
    }
