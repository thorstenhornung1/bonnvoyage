"""System health for BonnVoyage."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .api.bcp import BCP_URL
from .api.bonn_geo import ROADWORKS_URL
from .api.bonn_traffic import TRAFFIC_URL
from .const import DOMAIN
from .coordinator import BonnVoyageConfigEntry


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register the system health info callback."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Report reachability and source status."""
    info: dict[str, Any] = {
        "can_reach_bcp": system_health.async_check_can_reach_url(hass, BCP_URL),
        "can_reach_city_geo": system_health.async_check_can_reach_url(
            hass, ROADWORKS_URL
        ),
        "can_reach_city_traffic": system_health.async_check_can_reach_url(
            hass, TRAFFIC_URL
        ),
    }
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        typed_entry: BonnVoyageConfigEntry = entry
        runtime = typed_entry.runtime_data
        sources = {
            "parking": runtime.parking,
            "city_messages": runtime.city,
            "autobahn_messages": runtime.autobahn,
            "city_traffic": runtime.traffic,
        }
        info["sources_ok"] = ", ".join(
            f"{name}={coordinator.last_update_success}"
            for name, coordinator in sources.items()
            if coordinator is not None
        )
        info["routes_configured"] = len(runtime.routes)
    return info
