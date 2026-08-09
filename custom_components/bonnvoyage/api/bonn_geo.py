"""Client for roadworks GeoJSON from the Bonn city geoportal."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any, Final
from zoneinfo import ZoneInfo

import aiohttp

from .exceptions import ApiParseError
from .http import async_get_capped
from .models import TrafficMessage
from .validation import coords_in_bonn

_LOGGER = logging.getLogger(__name__)

ROADWORKS_URL: Final = "https://stadtplan.bonn.de/geojson?Thema=14403"
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024

CITY_TIMEZONE: Final = ZoneInfo("Europe/Berlin")
DATE_FORMAT: Final = "%d.%m.%Y"

# String values of MessageSource.CITY / MessageKind.ROADWORK (API layer is HA free)
SOURCE_CITY: Final = "city"
KIND_ROADWORK: Final = "roadwork"

MAX_TEXT_LENGTH: Final = 500


class BonnGeoClient:
    """Async client for the Bonn geoportal roadworks layer."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the client with an injected session."""
        self._session = session

    async def async_fetch_roadworks(self) -> dict[str, TrafficMessage]:
        """Fetch and parse roadworks, keyed by stable message id."""
        raw = await async_get_capped(self._session, ROADWORKS_URL, MAX_RESPONSE_BYTES)
        try:
            data = json.loads(raw)
        except ValueError as err:
            raise ApiParseError(f"Invalid roadworks JSON: {err}") from err

        if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
            raise ApiParseError("Roadworks response is not a FeatureCollection")

        messages: dict[str, TrafficMessage] = {}
        for feature in data.get("features", []):
            message = self._parse_feature(feature)
            if message is not None:
                messages[message.message_id] = message
        return messages

    def _parse_feature(self, feature: Any) -> TrafficMessage | None:
        """Parse one GeoJSON feature, or None if unusable."""
        if not isinstance(feature, dict):
            return None
        properties = feature.get("properties") or {}
        coords = self._extract_point(feature.get("geometry"))
        if coords is None:
            return None
        longitude, latitude = coords
        if not (
            isinstance(latitude, int | float) and isinstance(longitude, int | float)
        ) or not coords_in_bonn(float(latitude), float(longitude)):
            _LOGGER.debug("Dropping roadwork outside Bonn bounding box: %s", coords)
            return None

        raw_id = properties.get("baustelle_id")
        if raw_id is None:
            return None
        title = str(properties.get("bezeichnung") or "Baustelle")[:MAX_TEXT_LENGTH]

        return TrafficMessage(
            message_id=f"city_{raw_id}",
            source=SOURCE_CITY,
            kind=KIND_ROADWORK,
            title=title,
            latitude=float(latitude),
            longitude=float(longitude),
            description=_optional_text(properties.get("massnahme")),
            restriction=_optional_text(properties.get("sperrung")),
            address=_optional_text(properties.get("adresse")),
            authority=_optional_text(properties.get("traeger")),
            starts_at=_parse_date(properties.get("von")),
            ends_at=_parse_date(properties.get("bis")),
        )

    @staticmethod
    def _extract_point(geometry: Any) -> tuple[float, float] | None:
        """Return (lon, lat) for a geometry; first point for non-points."""
        if not isinstance(geometry, dict):
            return None
        coordinates = geometry.get("coordinates")
        if coordinates is None:
            return None
        geo_type = geometry.get("type")
        try:
            if geo_type == "Point":
                lon, lat = coordinates[0], coordinates[1]
            elif geo_type == "LineString":
                lon, lat = coordinates[0][0], coordinates[0][1]
            elif geo_type == "Polygon":
                lon, lat = coordinates[0][0][0], coordinates[0][0][1]
            else:
                return None
        except (TypeError, IndexError, KeyError):
            return None
        return (lon, lat)


def _optional_text(value: Any) -> str | None:
    """Return a length-capped string or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text[:MAX_TEXT_LENGTH] or None


def _parse_date(value: Any) -> datetime | None:
    """Parse a dd.mm.yyyy date in local Bonn time."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), DATE_FORMAT).replace(
            tzinfo=CITY_TIMEZONE
        )
    except ValueError:
        return None
