"""Constants for the BonnVoyage integration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

DOMAIN: Final = "bonnvoyage"

CONF_AREA: Final = "area"
CONF_PARKINGS: Final = "parkings"
CONF_FACILITY_ID: Final = "facility_id"

SUBENTRY_TYPE_PARKING: Final = "parking"

DEFAULT_RADIUS_M: Final = 10_000
PRESELECT_RADIUS_M: Final = 3_000
PRESELECT_MAX: Final = 5

PARKING_UPDATE_INTERVAL: Final = timedelta(minutes=2)

# Number of consecutive failed refreshes tolerated before entities go unavailable
# (architecture review: stale tolerance "Parken 2").
PARKING_STALE_TOLERANCE: Final = 2

# A facility whose feed timestamp is older than this is considered stale at
# runtime and its entities become unavailable.
RUNTIME_STALE_AFTER: Final = timedelta(hours=2)

# Facilities whose feed timestamp is older than this are hidden from the
# config flow entirely (dead feed entries, e.g. "karstadt" frozen since 2018).
FLOW_STALE_MAX_AGE: Final = timedelta(hours=24)

# Trend calculation (see docs/spec-trend.md)
TREND_WINDOW: Final = timedelta(minutes=15)
TREND_MIN_SPAN: Final = timedelta(minutes=10)
TREND_MIN_DELTA_SPACES: Final = 5
TREND_MIN_DELTA_CAPACITY_FRACTION: Final = 0.03
TREND_BUFFER_MAX_SAMPLES: Final = 20


class ParkingTrend(StrEnum):
    """Occupancy trend of a parking facility."""

    FILLING = "filling"
    STEADY = "steady"
    EMPTYING = "emptying"


# --- Traffic messages (M2) ---

MESSAGES_UPDATE_INTERVAL: Final = timedelta(minutes=30)
MESSAGES_STALE_TOLERANCE: Final = 3

AUTOBAHN_ROADS: Final = ("A555", "A559", "A565")

EVENT_TYPE_NEW_MESSAGE: Final = "new_message"

# A geo_location entity is only removed after its message/segment has been
# absent for this many consecutive, complete polls (0.7.2: a single missing
# poll used to delete and re-create the entity, flooding the registry).
GEO_REMOVAL_GRACE_POLLS: Final = 2

# Identifier of the service device grouping the message sensor and event.
MESSAGES_DEVICE_ID: Final = "messages"


class MessageSource(StrEnum):
    """Origin of a traffic message."""

    CITY = "city"
    AUTOBAHN = "autobahn"


class MessageKind(StrEnum):
    """Kind of a traffic message."""

    ROADWORK = "roadwork"
    CLOSURE = "closure"
    WARNING = "warning"


# --- Routes (M3) ---

CONF_PROVIDER: Final = "provider"
CONF_API_KEY: Final = "api_key"
CONF_START: Final = "start"
CONF_DESTINATION: Final = "destination"
CONF_VIA: Final = "via"
CONF_DELAY_THRESHOLD: Final = "delay_threshold"
CONF_LOCATION: Final = "location"
CONF_NAME: Final = "name"
CONF_ADD_VIA: Final = "add_via"

SUBENTRY_TYPE_ROUTE: Final = "route"

ROUTE_UPDATE_INTERVAL: Final = timedelta(minutes=5)
# Architecture review: routes tolerate a single failed refresh.
ROUTE_STALE_TOLERANCE: Final = 1

# Route endpoints closer than this are rejected in the wizard.
MIN_ROUTE_DISTANCE_M: Final = 100

DEFAULT_DELAY_THRESHOLD_MIN: Final = 10

# Marker value for "pick on map" in the zone-or-map selectors.
PICK_ON_MAP: Final = "__map__"


class TrafficLevel(StrEnum):
    """Derived congestion level of a route."""

    FREE_FLOW = "free_flow"
    HEAVY = "heavy"
    QUEUING = "queuing"
    CONGESTED = "congested"


# travel_time / no_traffic_time ratio thresholds for the level derivation
TRAFFIC_LEVEL_HEAVY_RATIO: Final = 1.10
TRAFFIC_LEVEL_QUEUING_RATIO: Final = 1.35
TRAFFIC_LEVEL_CONGESTED_RATIO: Final = 1.75


# --- City traffic segments (M3, stadtplan.bonn.de Thema=19584) ---

SUBENTRY_TYPE_SEGMENT: Final = "segment"
CONF_SEGMENT_ID: Final = "segment_id"

SEGMENT_UPDATE_INTERVAL: Final = timedelta(minutes=5)
SEGMENT_STALE_TOLERANCE: Final = 3

# A map pick farther than this from every segment is rejected in the flow.
MAX_SEGMENT_PICK_DISTANCE_M: Final = 2_000


class SegmentStatus(StrEnum):
    """Traffic status of a city segment."""

    NORMAL = "normal"
    ELEVATED = "elevated"
    CONGESTION_RISK = "congestion_risk"
    CONGESTED = "congested"
    UNKNOWN = "unknown"


# Observed live values (08/2026). Unknown strings map to UNKNOWN and are
# logged; the nightly contract job surfaces new vocabulary.
SEGMENT_STATUS_MAP: Final[dict[str, SegmentStatus]] = {
    "normales Verkehrsaufkommen": SegmentStatus.NORMAL,
    "erhöhte Verkehrsbelastung": SegmentStatus.ELEVATED,
    "Staugefahr": SegmentStatus.CONGESTION_RISK,
    "Stau": SegmentStatus.CONGESTED,
    "aktuell nicht ermittelbar": SegmentStatus.UNKNOWN,
}

DISRUPTED_SEGMENT_STATUSES: Final = (
    SegmentStatus.ELEVATED,
    SegmentStatus.CONGESTION_RISK,
    SegmentStatus.CONGESTED,
)


# A source that stays broken longer than this raises a repair issue
# (architecture review: not before 6 hours).
REPAIR_OUTAGE_AFTER: Final = timedelta(hours=6)


# --- Public transport stops (M5, transitous.org / DELFI) ---

SUBENTRY_TYPE_STOP: Final = "stop"
CONF_STOP_ID: Final = "stop_id"
CONF_QUERY: Final = "query"
CONF_DELAY_ALERT_THRESHOLD: Final = "delay_alert_threshold"

STOP_UPDATE_INTERVAL: Final = timedelta(minutes=2)
STOP_STALE_TOLERANCE: Final = 2
DEPARTURES_COUNT: Final = 10

DEFAULT_DELAY_ALERT_MIN: Final = 5

CONF_STREET_NAME: Final = "street_name"
CONF_DIRECTION: Final = "direction"

# Corridor mode: segments along a start->destination line (M5c)
CONF_SEGMENT_IDS: Final = "segment_ids"
CONF_SEGMENT_META: Final = "segment_meta"
CORRIDOR_WIDTH_M: Final = 800
CORRIDOR_WIDTH_MIN_M: Final = 200
CORRIDOR_WIDTH_MAX_M: Final = 3_000
CONF_CORRIDOR_WIDTH: Final = "corridor_width"
