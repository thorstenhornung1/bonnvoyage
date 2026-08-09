"""Shared fixtures for the BonnVoyage tests."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bonnvoyage.api.autobahn import BASE_URL as AUTOBAHN_BASE_URL
from custom_components.bonnvoyage.api.bcp import BCP_URL
from custom_components.bonnvoyage.api.bonn_geo import ROADWORKS_URL
from custom_components.bonnvoyage.api.bonn_traffic import TRAFFIC_URL
from custom_components.bonnvoyage.api.routing.tomtom import (
    _VALIDATION_REQUEST,
    BASE_URL as TOMTOM_BASE_URL,
)
from custom_components.bonnvoyage.const import AUTOBAHN_ROADS, CONF_AREA, DOMAIN

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# The real feed fixture carries timestamps around 2026-08-09 07:15 local time;
# freezing the clock shortly after keeps staleness checks deterministic.
FROZEN_NOW = "2026-08-09T08:00:00+02:00"

BONN_HOME_LAT = 50.7339
BONN_HOME_LON = 7.0997

AREA = {"latitude": BONN_HOME_LAT, "longitude": BONN_HOME_LON, "radius": 10000}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading custom integrations in all tests."""
    yield


@pytest.fixture(autouse=True)
def frozen_time(freezer) -> None:
    """Freeze the clock right after the fixture feed timestamps."""
    freezer.move_to(FROZEN_NOW)


@pytest.fixture(autouse=True)
async def home_in_bonn(hass) -> None:
    """Place the Home Assistant home location in Bonn city center."""
    hass.config.latitude = BONN_HOME_LAT
    hass.config.longitude = BONN_HOME_LON


def load_fixture_bytes(name: str) -> bytes:
    """Load a fixture file as bytes."""
    return (FIXTURE_DIR / name).read_bytes()


@pytest.fixture
def bcp_feed(aioclient_mock) -> None:
    """Mock the BCP feed with the real-world XML fixture."""
    aioclient_mock.get(BCP_URL, content=load_fixture_bytes("bcpext.xml"))


def autobahn_url(road: str, service: str) -> str:
    """URL of one Autobahn API road/service combination."""
    return f"{AUTOBAHN_BASE_URL}/{road}/services/{service}"


def tomtom_url(
    start: tuple[float, float],
    dest: tuple[float, float],
    via: tuple[float, float] | None = None,
) -> str:
    """Deterministic TomTom URL for rounded coordinates."""
    points = [start] + ([via] if via else []) + [dest]
    locations = ":".join(f"{round(lat, 4)},{round(lon, 4)}" for lat, lon in points)
    return f"{TOMTOM_BASE_URL}/{locations}/json"


TOMTOM_VALIDATION_URL = tomtom_url(
    (_VALIDATION_REQUEST.start.latitude, _VALIDATION_REQUEST.start.longitude),
    (
        _VALIDATION_REQUEST.destination.latitude,
        _VALIDATION_REQUEST.destination.longitude,
    ),
)


def mock_message_feeds(aioclient_mock) -> None:
    """Mock the city feeds and all Autobahn API endpoints.

    A565 serves the real-world fixtures (2 roadworks, 2 closures — one of
    them outside the 10 km test radius); every other road is empty.
    Expected messages inside the AREA radius: 9 city + 3 autobahn; the
    traffic layer fixture carries 3 disrupted segments inside the radius.
    """
    aioclient_mock.get(ROADWORKS_URL, content=load_fixture_bytes("baustellen.json"))
    aioclient_mock.get(TRAFFIC_URL, content=load_fixture_bytes("verkehrslage.json"))
    for road in AUTOBAHN_ROADS:
        for service in ("warning", "roadworks", "closure"):
            if road == "A565" and service in ("roadworks", "closure"):
                content = load_fixture_bytes(f"autobahn_{service}.json")
            else:
                content = f'{{"{service}": []}}'.encode()
            aioclient_mock.get(autobahn_url(road, service), content=content)


@pytest.fixture(autouse=True)
def messages_feeds(aioclient_mock) -> None:
    """Mock the message feeds for every test by default."""
    mock_message_feeds(aioclient_mock)


def mock_config_entry(**kwargs) -> MockConfigEntry:
    """Build the standard mock entry for BonnVoyage."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="BonnVoyage",
        data={CONF_AREA: AREA},
        **kwargs,
    )
