"""Domain models for the BonnVoyage API layer (Home Assistant free)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ParkingFacility:
    """Static master data of a parking facility."""

    facility_id: str
    name: str
    latitude: float | None
    longitude: float | None
    address: str | None = None
    operator: str = "Bonner City Parkraum GmbH"


@dataclass(frozen=True, slots=True)
class ParkingStatus:
    """Live status of a parking facility from the BCP feed."""

    facility_id: str
    capacity: int | None
    free: int | None
    is_open: bool | None
    raw_status: int | None
    raw_tendency: int | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class TrafficMessage:
    """A traffic message (roadwork, closure or warning) with a point location.

    ``source`` and ``kind`` carry the string values of the MessageSource and
    MessageKind enums; the API layer stays free of Home Assistant imports.
    """

    message_id: str
    source: str
    kind: str
    title: str
    latitude: float
    longitude: float
    description: str | None = None
    road: str | None = None
    is_blocked: bool | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    restriction: str | None = None
    address: str | None = None
    authority: str | None = None


@dataclass(frozen=True, slots=True)
class TrafficSegment:
    """Live traffic status of one city street segment.

    ``status`` carries a SegmentStatus enum string value; the API layer
    stays free of Home Assistant imports.
    """

    segment_id: str
    status: str
    raw_status: str
    speed_kmh: int | None
    measured_at: datetime | None
    midpoint: tuple[float, float]  # (latitude, longitude)
    points: tuple[tuple[float, float], ...]  # capped support points (lat, lon)


@dataclass(frozen=True, slots=True)
class RoutePoint:
    """A coordinate on a route."""

    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """Requested route between two points, optionally via a waypoint."""

    start: RoutePoint
    destination: RoutePoint
    via: RoutePoint | None = None


@dataclass(frozen=True, slots=True)
class RouteResult:
    """Result of one route calculation."""

    travel_time_s: int
    no_traffic_time_s: int | None = None
    delay_s: int | None = None
    length_m: int | None = None


@dataclass(frozen=True, slots=True)
class TransitStop:
    """A public transport stop from the transitous geocoder."""

    stop_id: str
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Departure:
    """One upcoming departure at a watched stop."""

    line: str
    headsign: str
    mode: str
    scheduled: datetime
    actual: datetime | None
    realtime: bool
    cancelled: bool

    @property
    def delay_s(self) -> int | None:
        """Delay in seconds, or None without realtime data."""
        if self.actual is None or not self.realtime:
            return None
        return max(int((self.actual - self.scheduled).total_seconds()), 0)
