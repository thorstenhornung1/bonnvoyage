"""Tests for the BCP API client."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aioresponses import aioresponses
import pytest

from custom_components.bonnvoyage.api.bcp import BCP_URL, MAX_RESPONSE_BYTES, BcpClient
from custom_components.bonnvoyage.api.exceptions import (
    ApiConnectionError,
    ApiParseError,
    ApiResponseTooLargeError,
)

from .conftest import load_fixture_bytes

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def mocked_http() -> aioresponses:
    """Mock aiohttp on the transport level."""
    with aioresponses() as mock:
        yield mock


async def _fetch(mock: aioresponses, **response_kwargs):
    """Run one client fetch against a mocked response."""
    mock.get(BCP_URL, **response_kwargs)
    async with aiohttp.ClientSession() as session:
        return await BcpClient(session).async_fetch_statuses()


async def test_fetch_and_parse_real_feed(mocked_http: aioresponses) -> None:
    """The real-world fixture parses into validated domain objects."""
    statuses = await _fetch(mocked_http, body=load_fixture_bytes("bcpext.xml"))

    assert len(statuses) == 14

    markt = statuses["markt"]
    assert markt.capacity == 287
    assert markt.free == 251
    assert markt.is_open is True
    assert markt.raw_status == 0
    assert markt.updated_at == datetime(2026, 8, 9, 7, 15, tzinfo=BERLIN)

    beethoven = statuses["beethoven"]
    assert beethoven.is_open is False
    assert beethoven.free == 0

    # Umlaut-free slug from mixed-case feed id
    assert "rabinstrasse" in statuses
    # Entry frozen since 2018 still parses; staleness is handled downstream
    assert statuses["karstadt"].updated_at is not None
    assert statuses["karstadt"].updated_at.year == 2018


async def test_http_error_raises_connection_error(mocked_http: aioresponses) -> None:
    """Non-200 responses raise the connection error."""
    with pytest.raises(ApiConnectionError):
        await _fetch(mocked_http, status=503)


async def test_network_error_raises_connection_error(mocked_http: aioresponses) -> None:
    """Transport errors raise the connection error."""
    with pytest.raises(ApiConnectionError):
        await _fetch(mocked_http, exception=aiohttp.ClientConnectionError("boom"))


async def test_timeout_raises_connection_error(mocked_http: aioresponses) -> None:
    """Timeouts raise the connection error."""
    with pytest.raises(ApiConnectionError):
        await _fetch(mocked_http, exception=TimeoutError())


async def test_malformed_xml_raises_parse_error(mocked_http: aioresponses) -> None:
    """Broken XML raises the parse error."""
    with pytest.raises(ApiParseError):
        await _fetch(mocked_http, body=load_fixture_bytes("bcpext_malformed.xml"))


async def test_empty_feed_raises_parse_error(mocked_http: aioresponses) -> None:
    """A feed without facilities raises the parse error."""
    with pytest.raises(ApiParseError):
        await _fetch(mocked_http, body=load_fixture_bytes("bcpext_empty.xml"))


async def test_xxe_payload_is_rejected(mocked_http: aioresponses) -> None:
    """External entity payloads are rejected by defusedxml."""
    with pytest.raises(ApiParseError):
        await _fetch(mocked_http, body=load_fixture_bytes("bcpext_xxe.xml"))


async def test_oversized_response_is_rejected(mocked_http: aioresponses) -> None:
    """Responses beyond the size cap raise the dedicated error."""
    with pytest.raises(ApiResponseTooLargeError):
        await _fetch(mocked_http, body=b"x" * (MAX_RESPONSE_BYTES + 1))


async def test_invalid_values_are_sanitized(mocked_http: aioresponses) -> None:
    """Garbage values become None, free is clamped, empty names are dropped."""
    statuses = await _fetch(
        mocked_http, body=load_fixture_bytes("bcpext_invalid_values.xml")
    )

    # Facility without a name is dropped entirely
    assert len(statuses) == 2

    markt = statuses["markt"]
    assert markt.free == 287  # clamped: 999 > capacity 287

    friedensplatz = statuses["friedensplatz"]
    assert friedensplatz.capacity is None  # "abc"
    assert friedensplatz.free is None  # negative
    assert friedensplatz.is_open is None  # unknown status code 77
    assert friedensplatz.updated_at is None  # "gestern"
    assert friedensplatz.raw_tendency is None  # empty


async def test_edge_case_fields(mocked_http: aioresponses) -> None:
    """Missing fields become None; unusable names drop the facility."""
    xml = (
        '<?xml version="1.0" encoding="iso-8859-1"?><parkhaeuser>'
        "<parkhaus><bezeichnung>markt</bezeichnung><gesamt>287</gesamt>"
        "<frei>10</frei><status>0</status></parkhaus>"
        "<parkhaus><bezeichnung>###</bezeichnung><gesamt>10</gesamt>"
        "<frei>5</frei><status>0</status></parkhaus>"
        f"<parkhaus><bezeichnung>{'x' * 150}</bezeichnung><gesamt>10</gesamt>"
        "<frei>5</frei><status>0</status></parkhaus>"
        "</parkhaeuser>"
    ).encode("iso-8859-1")
    statuses = await _fetch(mocked_http, body=xml)

    assert set(statuses) == {"markt"}
    markt = statuses["markt"]
    assert markt.updated_at is None
    assert markt.raw_tendency is None


def test_coords_in_bonn_bounding_box() -> None:
    """The bounding box accepts Bonn and rejects far-away coordinates."""
    from custom_components.bonnvoyage.api.validation import coords_in_bonn

    assert coords_in_bonn(50.7339, 7.0997)
    assert not coords_in_bonn(52.52, 13.40)  # Berlin
