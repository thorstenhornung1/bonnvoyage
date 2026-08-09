"""Tests for diagnostics, repair issues and system health."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.bonnvoyage.const import DOMAIN, MESSAGES_UPDATE_INTERVAL
from custom_components.bonnvoyage.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import mock_config_entry
from .test_init import _advance, _setup
from .test_messages import _remock_feeds
from .test_routes import _entry_with_route


async def test_diagnostics_redacts_secrets(hass: HomeAssistant, aioclient_mock) -> None:
    """The API key and every coordinate are redacted."""
    from .conftest import load_fixture_bytes
    from .test_routes import ROUTE_URL

    aioclient_mock.get(ROUTE_URL, content=load_fixture_bytes("tomtom_route.json"))
    entry = _entry_with_route()
    await _setup(hass, entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    flat = str(diagnostics)
    assert "test-key" not in flat
    assert "50.7339" not in flat  # home / route coordinates
    assert "REDACTED" in flat

    assert diagnostics["sources"]["routing_provider"] == "tomtom"
    assert diagnostics["sources"]["parking"] is None  # no parking subentries
    assert diagnostics["sources"]["city_messages"]["last_update_success"] is True
    assert diagnostics["sources"]["city_traffic"]["items"] == 111
    route_info = next(iter(diagnostics["sources"]["routes"].values()))
    assert route_info["last_update_success"] is True


async def test_repair_issue_after_long_outage(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A source failing for over six hours raises a repair issue."""
    entry = mock_config_entry()
    await _setup(hass, entry)

    registry = ir.async_get(hass)
    issue_id = f"source_down_{DOMAIN}_city_messages"

    _remock_feeds(aioclient_mock, city_status=503)
    # 13 x 30 min = 6.5 h of continuous failure
    for _ in range(13):
        await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # Recovery deletes the issue
    _remock_feeds(aioclient_mock)
    await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_no_repair_issue_for_short_outage(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """Brief outages never raise an issue."""
    entry = mock_config_entry()
    await _setup(hass, entry)

    _remock_feeds(aioclient_mock, city_status=503)
    for _ in range(5):  # 2.5 h < 6 h
        await _advance(hass, freezer, MESSAGES_UPDATE_INTERVAL)

    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(DOMAIN, f"source_down_{DOMAIN}_city_messages") is None
    )


async def test_system_health(hass: HomeAssistant) -> None:
    """System health reports source status."""
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import get_system_health_info

    assert await async_setup_component(hass, "system_health", {})
    entry = mock_config_entry()
    await _setup(hass, entry)

    info = await get_system_health_info(hass, DOMAIN)

    assert "sources_ok" in info
    assert "city_traffic=True" in info["sources_ok"]
    assert info["routes_configured"] == 0
