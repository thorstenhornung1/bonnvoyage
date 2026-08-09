"""Central validation helpers for untrusted API responses."""

from __future__ import annotations

import re

# Rough bounding box around Bonn; coordinates outside are rejected.
BONN_LAT_MIN = 50.60
BONN_LAT_MAX = 50.85
BONN_LON_MIN = 6.95
BONN_LON_MAX = 7.30

# Upper bounds for feed values; anything beyond is treated as garbage.
MAX_SPACES = 10_000
MAX_NAME_LENGTH = 100

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def slugify_id(raw: str) -> str:
    """Build a stable, safe facility id from a feed identifier."""
    slug = _SLUG_RE.sub("_", raw.strip().lower().translate(_UMLAUTS)).strip("_")
    return slug[:MAX_NAME_LENGTH]


def parse_bounded_int(raw: str | None, maximum: int = MAX_SPACES) -> int | None:
    """Parse an integer field, returning None for missing or absurd values."""
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    if not 0 <= value <= maximum:
        return None
    return value


def coords_in_bonn(latitude: float, longitude: float) -> bool:
    """Check that coordinates are inside the Bonn bounding box."""
    return (
        BONN_LAT_MIN <= latitude <= BONN_LAT_MAX
        and BONN_LON_MIN <= longitude <= BONN_LON_MAX
    )


def coords_in_germany(latitude: float, longitude: float) -> bool:
    """Plausibility check for motorway messages (Germany bounding box)."""
    return 47.0 <= latitude <= 56.0 and 5.0 <= longitude <= 16.0
