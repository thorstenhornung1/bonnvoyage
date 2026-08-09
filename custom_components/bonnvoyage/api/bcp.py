"""Client for the BCP Bonn parking feed (bcpext.xml)."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Final
from xml.etree.ElementTree import (
    Element,
    ParseError,
)
from zoneinfo import ZoneInfo

import aiohttp
from defusedxml.ElementTree import fromstring

from .exceptions import ApiParseError
from .http import async_get_capped
from .models import ParkingFacility, ParkingStatus
from .validation import MAX_NAME_LENGTH, parse_bounded_int, slugify_id

_LOGGER = logging.getLogger(__name__)

BCP_URL: Final = "https://www.bcp-bonn.de/stellplatz/bcpext.xml"
MAX_RESPONSE_BYTES: Final = 512 * 1024

FEED_TIMEZONE: Final = ZoneInfo("Europe/Berlin")
TIMESTAMP_FORMAT: Final = "%d.%m.%Y %H:%M"

# Feed status semantics are undocumented; observed values: 0 = open, 2 = closed.
STATUS_OPEN: Final = 0
STATUS_CLOSED: Final = 2

# Static master data for the BCP facilities. The live feed carries no
# coordinates, and the facilities themselves have been stable for years, so
# they ship with the integration, keyed by the slugified feed identifier.
#
# NOTE: Coordinates are approximations for development. Verifying every entry
# against OpenStreetMap/official sources is a release gate (see PLAN.md M1).
STATIC_FACILITIES: Final[dict[str, ParkingFacility]] = {
    facility.facility_id: facility
    for facility in (
        ParkingFacility("beethoven", "Beethoven-Parkhaus", 50.7414, 7.1002),
        ParkingFacility("beetplatz", "Parkplatz Beethovenplatz", 50.7398, 7.0925),
        ParkingFacility("friedensplatz", "Friedensplatzgarage", 50.7352, 7.0975),
        ParkingFacility("karstadt", "Parkhaus Poststraße", 50.7346, 7.0965),
        ParkingFacility("markt", "Marktgarage", 50.7345, 7.1028),
        ParkingFacility("muensterplatz", "Münsterplatzgarage", 50.7336, 7.0961),
        ParkingFacility("operngarage", "Operngarage", 50.7381, 7.1046),
        ParkingFacility("rabinstrasse", "Parkhaus Rabinstraße", 50.7330, 7.0930),
        ParkingFacility("rheinaue", "Parkhaus Rheinaue", 50.7124, 7.1310),
        ParkingFacility("stadthaus", "Stadthausgarage", 50.7382, 7.0950),
        ParkingFacility("stadthaus_unten", "Stadthausgarage (unten)", 50.7382, 7.0950),
        ParkingFacility("unigarage", "Uni-Parkhaus", 50.7325, 7.0990),
        ParkingFacility("hauptbahnhof", "Parkhaus Hauptbahnhof", 50.7320, 7.0965),
        ParkingFacility("bike_ride", "Fahrradparkhaus Hauptbahnhof", 50.7325, 7.0975),
    )
}


class BcpClient:
    """Async client for the BCP Bonn parking feed."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the client with an injected session."""
        self._session = session

    async def async_fetch_statuses(self) -> dict[str, ParkingStatus]:
        """Fetch and parse the live feed, keyed by facility id."""
        raw = await async_get_capped(self._session, BCP_URL, MAX_RESPONSE_BYTES)
        return self._parse_feed(raw)

    def _parse_feed(self, raw: bytes) -> dict[str, ParkingStatus]:
        """Parse the XML feed into validated domain objects."""
        try:
            root = fromstring(raw)
        except (ParseError, ValueError) as err:
            raise ApiParseError(f"Invalid BCP XML: {err}") from err

        statuses: dict[str, ParkingStatus] = {}
        for node in root.iter("parkhaus"):
            status = self._parse_facility_node(node)
            if status is not None:
                statuses[status.facility_id] = status
        if not statuses:
            raise ApiParseError("BCP feed contained no parking facilities")
        return statuses

    def _parse_facility_node(self, node: Element) -> ParkingStatus | None:
        """Parse a single <parkhaus> element, or None if unusable."""
        name = (node.findtext("bezeichnung") or "").strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            return None
        facility_id = slugify_id(name)
        if not facility_id:
            return None

        capacity = parse_bounded_int(node.findtext("gesamt"))
        free = parse_bounded_int(node.findtext("frei"))
        if free is not None and capacity is not None and free > capacity:
            _LOGGER.debug(
                "Facility %s reports free (%s) > capacity (%s), clamping",
                facility_id,
                free,
                capacity,
            )
            free = capacity

        raw_status = parse_bounded_int(node.findtext("status"), maximum=99)
        is_open: bool | None = None
        if raw_status == STATUS_OPEN:
            is_open = True
        elif raw_status == STATUS_CLOSED:
            is_open = False

        return ParkingStatus(
            facility_id=facility_id,
            capacity=capacity,
            free=free,
            is_open=is_open,
            raw_status=raw_status,
            raw_tendency=parse_bounded_int(node.findtext("tendenz"), maximum=99),
            updated_at=self._parse_timestamp(node.findtext("zeitstempel")),
        )

    @staticmethod
    def _parse_timestamp(raw: str | None) -> datetime | None:
        """Parse the feed timestamp (local Bonn time) to an aware datetime."""
        if raw is None:
            return None
        try:
            return datetime.strptime(raw.strip(), TIMESTAMP_FORMAT).replace(
                tzinfo=FEED_TIMEZONE
            )
        except ValueError:
            return None
