"""Persistent street name cache for city traffic segments.

Segment geometry is static, so resolved names never expire. The cache fills
opportunistically with every flow run (UX review: no one ever waits for a
full background fill).
"""

from __future__ import annotations

from itertools import pairwise
import math

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api.models import TrafficSegment
from .const import DOMAIN

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.segment_names"

_COMPASS = ("N", "NO", "O", "SO", "S", "SW", "W", "NW")


def segment_direction(segment: TrafficSegment) -> str:
    """Compass bearing (8-wind rose) from first to last geometry point.

    The only reliable disambiguator for opposite-direction segment pairs
    that resolve to the same street name.
    """
    (lat1, lon1), (lat2, lon2) = segment.points[0], segment.points[-1]
    d_lon = math.radians(lon2 - lon1)
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    x = math.sin(d_lon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(
        lat2_r
    ) * math.cos(d_lon)
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    return _COMPASS[round(bearing / 45) % 8]


def point_to_segment_m(
    latitude: float, longitude: float, segment: TrafficSegment
) -> float:
    """Distance from a point to the segment polyline in meters.

    Planar approximation (fine at city scale): distances are computed
    against every polyline edge, not just the support points — long
    segments with sparse points would otherwise rank wrongly.
    """
    lat_scale = 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(latitude))

    def to_xy(lat: float, lon: float) -> tuple[float, float]:
        return ((lon - longitude) * lon_scale, (lat - latitude) * lat_scale)

    best = float("inf")
    points = [to_xy(lat, lon) for lat, lon in segment.points]
    for (x1, y1), (x2, y2) in pairwise(points):
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            dist = math.hypot(x1, y1)
        else:
            t = max(0.0, min(1.0, -(x1 * dx + y1 * dy) / length_sq))
            dist = math.hypot(x1 + t * dx, y1 + t * dy)
        best = min(best, dist)
    if len(points) == 1:
        best = math.hypot(*points[0])
    return best


def segment_length_km(segment: TrafficSegment) -> float:
    """Approximate polyline length in kilometers (planar, city scale)."""
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in pairwise(segment.points):
        lat_scale = 111_320.0
        lon_scale = 111_320.0 * math.cos(math.radians(lat1))
        total += math.hypot((lat2 - lat1) * lat_scale, (lon2 - lon1) * lon_scale)
    return round(total / 1000, 1)


class SegmentNameCache:
    """Store-backed street name cache keyed by segment id."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the cache."""
        self._store: Store[dict[str, str]] = Store(hass, STORE_VERSION, STORE_KEY)
        self._names: dict[str, str] | None = None

    async def async_get(self, segment_id: str) -> str | None:
        """Return the cached street name, if any."""
        if self._names is None:
            self._names = await self._store.async_load() or {}
        return self._names.get(segment_id)

    async def async_set(self, segment_id: str, name: str) -> None:
        """Store a resolved street name."""
        if self._names is None:
            self._names = await self._store.async_load() or {}
        self._names[segment_id] = name
        await self._store.async_save(dict(self._names))
