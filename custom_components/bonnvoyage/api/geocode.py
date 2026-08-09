"""Reverse geocoding of segment midpoints via OSM Nominatim (no API key).

Nominatim fair use: descriptive User-Agent, no bulk usage. Callers issue at
most a handful of sequential lookups per flow run (natural latency spacing),
and resolved names are cached forever — repeat traffic is near zero. Only
midpoints of public city measurement segments are ever geocoded, never a
user-picked point.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import aiohttp

from .http import async_get_capped

_LOGGER = logging.getLogger(__name__)

NOMINATIM_URL: Final = "https://nominatim.openstreetmap.org/reverse"
MAX_RESPONSE_BYTES: Final = 64 * 1024

# Street-level precision; verified to return the road name for segment
# midpoints (e.g. "Bornheimer Straße").
ZOOM_STREET: Final = "16"


class NominatimClient:
    """Minimal reverse geocoder honoring the fair use policy."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the client with an injected session."""
        self._session = session

    async def async_reverse_street(
        self, latitude: float, longitude: float
    ) -> str | None:
        """Return the street name for a point, or None on any failure.

        This helper never raises: naming is a nice-to-have and must not
        block the selection flow.
        """
        try:
            raw = await async_get_capped(
                self._session,
                NOMINATIM_URL,
                MAX_RESPONSE_BYTES,
                params={
                    "lat": f"{latitude:.5f}",
                    "lon": f"{longitude:.5f}",
                    "format": "jsonv2",
                    "zoom": ZOOM_STREET,
                },
            )
            data: Any = json.loads(raw)
        except Exception as err:
            _LOGGER.debug("Reverse geocoding failed: %s", err)
            return None
        if not isinstance(data, dict):
            return None
        road = data.get("address", {}).get("road") or data.get("name")
        if not road:
            return None
        return str(road)[:80]
