"""Client for the Bonn city live traffic layer (stadtplan.bonn.de)."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any, Final

import aiohttp

from .exceptions import ApiParseError
from .http import async_get_capped
from .models import TrafficSegment
from .validation import coords_in_bonn

_LOGGER = logging.getLogger(__name__)

TRAFFIC_URL: Final = "https://stadtplan.bonn.de/geojson?Thema=19584"
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024

MAX_POINTS_PER_SEGMENT: Final = 50

# Observed vocabulary (08/2026); unknown strings map to "unknown".
# Values mirror const.SegmentStatus (the API layer stays HA free).
STATUS_MAP: Final[dict[str, str]] = {
    "normales Verkehrsaufkommen": "normal",
    "erhöhte Verkehrsbelastung": "elevated",
    "Staugefahr": "congestion_risk",
    "Stau": "congested",
    "aktuell nicht ermittelbar": "unknown",
}


class BonnTrafficClient:
    """Async client for the city traffic segment layer."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the client with an injected session."""
        self._session = session

    async def async_fetch_segments(self) -> dict[str, TrafficSegment]:
        """Fetch and parse all segments, keyed by segment id."""
        raw = await async_get_capped(self._session, TRAFFIC_URL, MAX_RESPONSE_BYTES)
        try:
            data = json.loads(raw)
        except ValueError as err:
            raise ApiParseError(f"Invalid traffic JSON: {err}") from err
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
            raise ApiParseError("Traffic response is not a FeatureCollection")

        segments: dict[str, TrafficSegment] = {}
        for feature in data.get("features", []):
            segment = self._parse_feature(feature)
            if segment is not None:
                segments[segment.segment_id] = segment
        if not segments:
            raise ApiParseError("Traffic layer contained no segments")
        return segments

    def _parse_feature(self, feature: Any) -> TrafficSegment | None:
        """Parse one segment feature, or None if unusable."""
        if not isinstance(feature, dict):
            return None
        properties = feature.get("properties") or {}
        raw_id = properties.get("strecke_id")
        if raw_id is None:
            return None

        points = self._extract_points(feature.get("geometry"))
        if not points:
            return None
        midpoint = points[len(points) // 2]
        if not coords_in_bonn(midpoint[0], midpoint[1]):
            _LOGGER.debug("Dropping segment %s outside Bonn: %s", raw_id, midpoint)
            return None

        raw_status = str(properties.get("verkehrsstatus") or "").strip()
        status = STATUS_MAP.get(raw_status)
        if status is None:
            _LOGGER.debug(
                "Unknown verkehrsstatus %r for segment %s", raw_status, raw_id
            )
            status = "unknown"

        speed = properties.get("geschwindigkeit")
        speed_kmh = speed if isinstance(speed, int) and 0 < speed <= 250 else None

        return TrafficSegment(
            segment_id=str(raw_id),
            status=status,
            raw_status=raw_status,
            speed_kmh=speed_kmh,
            measured_at=_parse_timestamp(properties.get("auswertezeit")),
            midpoint=midpoint,
            points=points,
        )

    @staticmethod
    def _extract_points(geometry: Any) -> tuple[tuple[float, float], ...]:
        """Return capped (lat, lon) support points of a MultiLineString."""
        if not isinstance(geometry, dict):
            return ()
        coordinates = geometry.get("coordinates")
        if geometry.get("type") != "MultiLineString" or not isinstance(
            coordinates, list
        ):
            return ()
        points: list[tuple[float, float]] = []
        try:
            for line in coordinates:
                for lon, lat in line:
                    points.append((float(lat), float(lon)))
                    if len(points) >= MAX_POINTS_PER_SEGMENT:
                        return tuple(points)
        except (TypeError, ValueError):
            return ()
        return tuple(points)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
