"""Data update coordinators for BonnVoyage."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.location import distance

from .api.autobahn import AutobahnClient
from .api.bcp import STATIC_FACILITIES, BcpClient
from .api.bonn_geo import BonnGeoClient
from .api.bonn_traffic import BonnTrafficClient
from .api.exceptions import ApiAuthError, BonnVoyageApiError
from .api.models import (
    Departure,
    ParkingFacility,
    ParkingStatus,
    RouteRequest,
    RouteResult,
    TrafficMessage,
    TrafficSegment,
)
from .api.routing import RouteProvider
from .api.transitous import TransitousClient
from .const import (
    AUTOBAHN_ROADS,
    CONF_AREA,
    DEPARTURES_COUNT,
    DOMAIN,
    MESSAGES_STALE_TOLERANCE,
    MESSAGES_UPDATE_INTERVAL,
    PARKING_STALE_TOLERANCE,
    PARKING_UPDATE_INTERVAL,
    REPAIR_OUTAGE_AFTER,
    ROUTE_STALE_TOLERANCE,
    ROUTE_UPDATE_INTERVAL,
    SEGMENT_STALE_TOLERANCE,
    SEGMENT_UPDATE_INTERVAL,
    STOP_STALE_TOLERANCE,
    STOP_UPDATE_INTERVAL,
    TREND_BUFFER_MAX_SAMPLES,
    TREND_MIN_DELTA_CAPACITY_FRACTION,
    TREND_MIN_DELTA_SPACES,
    TREND_MIN_SPAN,
    TREND_WINDOW,
    ParkingTrend,
)

_LOGGER = logging.getLogger(__name__)

type BonnVoyageConfigEntry = ConfigEntry[BonnVoyageRuntimeData]


@dataclass(slots=True)
class BonnVoyageRuntimeData:
    """Runtime data of the BonnVoyage config entry.

    ``parking`` is None when no parking subentries are configured; ``provider``
    and ``routes`` stay empty when no routing provider is set up.
    """

    parking: BcpCoordinator | None
    city: BonnGeoCoordinator
    autobahn: AutobahnCoordinator
    traffic: BonnTrafficCoordinator
    provider: RouteProvider | None = None
    routes: dict[str, RouteCoordinator] = field(default_factory=dict)
    stops: dict[str, StopCoordinator] = field(default_factory=dict)


class SourceHealthMixin:
    """Track persistent source outages and raise repair issues.

    A repair issue is created once a source has been failing for longer
    than REPAIR_OUTAGE_AFTER (architecture review: not before 6 hours)
    and deleted again on the first successful refresh.
    """

    hass: HomeAssistant
    name: str
    _down_since: float | None = None

    def _report_health(self, healthy: bool) -> None:
        issue_id = f"source_down_{self.name}"
        if healthy:
            if self._down_since is not None:
                self._down_since = None
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        now = dt_util.utcnow().timestamp()
        if self._down_since is None:
            self._down_since = now
            return
        if now - self._down_since >= REPAIR_OUTAGE_AFTER.total_seconds():
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="source_down",
                translation_placeholders={"source": self.name},
            )


@dataclass(frozen=True, slots=True)
class ParkingData:
    """Live status of one facility enriched with the computed trend."""

    status: ParkingStatus
    trend: ParkingTrend | None


class BcpCoordinator(SourceHealthMixin, DataUpdateCoordinator[dict[str, ParkingData]]):
    """Coordinator for the BCP Bonn parking feed."""

    config_entry: BonnVoyageConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BonnVoyageConfigEntry,
        client: BcpClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_parking",
            update_interval=PARKING_UPDATE_INTERVAL,
        )
        self._client = client
        self._failure_count = 0
        self._history: dict[str, deque[tuple[float, int]]] = {}
        self.facilities: dict[str, ParkingFacility] = {}

    async def _async_setup(self) -> None:
        """Load master data on first refresh."""
        statuses = await self._client.async_fetch_statuses()
        self.facilities = {
            facility_id: STATIC_FACILITIES.get(
                facility_id,
                ParkingFacility(facility_id, facility_id, None, None),
            )
            for facility_id in statuses
        }

    async def _async_update_data(self) -> dict[str, ParkingData]:
        """Fetch live statuses, tolerating brief outages with stale data."""
        try:
            statuses = await self._client.async_fetch_statuses()
        except BonnVoyageApiError as err:
            self._failure_count += 1
            self._report_health(False)
            if self.data and self._failure_count <= PARKING_STALE_TOLERANCE:
                _LOGGER.warning(
                    "BCP feed unavailable (%s/%s), keeping stale data: %s",
                    self._failure_count,
                    PARKING_STALE_TOLERANCE,
                    err,
                )
                return self.data
            raise UpdateFailed(f"BCP feed unavailable: {err}") from err

        self._failure_count = 0
        self._report_health(True)
        now = dt_util.utcnow().timestamp()
        return {
            facility_id: ParkingData(
                status=status,
                trend=self._update_trend(facility_id, status, now),
            )
            for facility_id, status in statuses.items()
        }

    def _update_trend(
        self, facility_id: str, status: ParkingStatus, now: float
    ) -> ParkingTrend | None:
        """Record the current sample and compute the trend.

        See docs/spec-trend.md: compare the current free count with the sample
        closest to TREND_WINDOW ago; hysteresis via a minimum delta of
        max(TREND_MIN_DELTA_SPACES, 3% of capacity). Returns None while the
        buffer does not yet span TREND_MIN_SPAN or the facility is closed.
        """
        if status.free is None or status.is_open is not True:
            self._history.pop(facility_id, None)
            return None

        buffer = self._history.setdefault(
            facility_id, deque(maxlen=TREND_BUFFER_MAX_SAMPLES)
        )
        buffer.append((now, status.free))

        target = now - TREND_WINDOW.total_seconds()
        reference = min(buffer, key=lambda sample: abs(sample[0] - target))
        if now - reference[0] < TREND_MIN_SPAN.total_seconds():
            return None

        threshold = TREND_MIN_DELTA_SPACES
        if status.capacity:
            threshold = max(
                threshold,
                round(status.capacity * TREND_MIN_DELTA_CAPACITY_FRACTION),
            )
        delta = status.free - reference[1]
        if delta <= -threshold:
            return ParkingTrend.FILLING
        if delta >= threshold:
            return ParkingTrend.EMPTYING
        return ParkingTrend.STEADY


class MessagesCoordinator(
    SourceHealthMixin, DataUpdateCoordinator[dict[str, TrafficMessage]]
):
    """Base coordinator for traffic message sources with radius filtering.

    Refresh failures never raise ConfigEntryNotReady semantics at setup time;
    message sources are non-critical (the coordinator retries on its interval).
    """

    config_entry: BonnVoyageConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: BonnVoyageConfigEntry, name: str
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=name,
            update_interval=MESSAGES_UPDATE_INTERVAL,
        )
        self._failure_count = 0

    async def _async_update_data(self) -> dict[str, TrafficMessage]:
        """Fetch messages, tolerating brief outages with stale data."""
        try:
            messages = await self._async_fetch()
        except BonnVoyageApiError as err:
            self._failure_count += 1
            self._report_health(False)
            if (
                self.data is not None
                and self._failure_count <= MESSAGES_STALE_TOLERANCE
            ):
                _LOGGER.warning(
                    "%s unavailable (%s/%s), keeping stale data: %s",
                    self.name,
                    self._failure_count,
                    MESSAGES_STALE_TOLERANCE,
                    err,
                )
                return self.data
            raise UpdateFailed(f"{self.name} unavailable: {err}") from err

        self._failure_count = 0
        self._report_health(True)
        return self._filter_by_area(messages)

    async def _async_fetch(self) -> dict[str, TrafficMessage]:
        """Fetch raw messages from the source."""
        raise NotImplementedError

    def _filter_by_area(
        self, messages: dict[str, TrafficMessage]
    ) -> dict[str, TrafficMessage]:
        """Keep only messages inside the configured area."""
        entry = self.config_entry
        area = entry.options.get(CONF_AREA, entry.data.get(CONF_AREA)) or {}
        latitude = area.get("latitude", self.hass.config.latitude)
        longitude = area.get("longitude", self.hass.config.longitude)
        radius_m = area.get("radius", 10_000)

        filtered: dict[str, TrafficMessage] = {}
        for message_id, message in messages.items():
            meters = distance(latitude, longitude, message.latitude, message.longitude)
            if meters is not None and meters <= radius_m:
                filtered[message_id] = message
        return filtered


class BonnGeoCoordinator(MessagesCoordinator):
    """Coordinator for the Bonn city roadworks layer."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BonnVoyageConfigEntry,
        client: BonnGeoClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, entry, f"{DOMAIN}_city_messages")
        self._client = client

    async def _async_fetch(self) -> dict[str, TrafficMessage]:
        """Fetch city roadworks."""
        return await self._client.async_fetch_roadworks()


class AutobahnCoordinator(MessagesCoordinator):
    """Coordinator for Autobahn API messages on the Bonn motorways."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BonnVoyageConfigEntry,
        client: AutobahnClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, entry, f"{DOMAIN}_autobahn_messages")
        self._client = client

    async def _async_fetch(self) -> dict[str, TrafficMessage]:
        """Fetch motorway messages."""
        return await self._client.async_fetch_messages(AUTOBAHN_ROADS)


class BonnTrafficCoordinator(
    SourceHealthMixin, DataUpdateCoordinator[dict[str, TrafficSegment]]
):
    """Coordinator for the city live traffic layer with radius filtering."""

    config_entry: BonnVoyageConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BonnVoyageConfigEntry,
        client: BonnTrafficClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_city_traffic",
            update_interval=SEGMENT_UPDATE_INTERVAL,
        )
        self._client = client
        self._failure_count = 0

    async def _async_update_data(self) -> dict[str, TrafficSegment]:
        """Fetch all segments, tolerating brief outages with stale data."""
        try:
            segments = await self._client.async_fetch_segments()
        except BonnVoyageApiError as err:
            self._failure_count += 1
            self._report_health(False)
            if self.data is not None and self._failure_count <= SEGMENT_STALE_TOLERANCE:
                _LOGGER.warning(
                    "%s unavailable (%s/%s), keeping stale data: %s",
                    self.name,
                    self._failure_count,
                    SEGMENT_STALE_TOLERANCE,
                    err,
                )
                return self.data
            raise UpdateFailed(f"{self.name} unavailable: {err}") from err
        self._failure_count = 0
        self._report_health(True)
        return segments

    def disrupted_in_area(self) -> dict[str, TrafficSegment]:
        """Disrupted segments inside the configured message area."""
        entry = self.config_entry
        area = entry.options.get(CONF_AREA, entry.data.get(CONF_AREA)) or {}
        latitude = area.get("latitude", self.hass.config.latitude)
        longitude = area.get("longitude", self.hass.config.longitude)
        radius_m = area.get("radius", 10_000)

        disrupted: dict[str, TrafficSegment] = {}
        for segment_id, segment in (self.data or {}).items():
            if segment.status not in ("elevated", "congestion_risk", "congested"):
                continue
            meters = distance(
                latitude, longitude, segment.midpoint[0], segment.midpoint[1]
            )
            if meters is not None and meters <= radius_m:
                disrupted[segment_id] = segment
        return disrupted


class RouteCoordinator(SourceHealthMixin, DataUpdateCoordinator[RouteResult]):
    """Coordinator for one configured route (one per route subentry)."""

    config_entry: BonnVoyageConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BonnVoyageConfigEntry,
        provider: RouteProvider,
        subentry_id: str,
        request: RouteRequest,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_route_{subentry_id}",
            update_interval=ROUTE_UPDATE_INTERVAL,
        )
        self._provider = provider
        self._request = request
        self._failure_count = 0

    async def _async_update_data(self) -> RouteResult:
        """Calculate the route; auth errors trigger reauth."""
        try:
            result = await self._provider.async_route(self._request)
        except ApiAuthError as err:
            raise ConfigEntryAuthFailed from err
        except BonnVoyageApiError as err:
            self._failure_count += 1
            self._report_health(False)
            if self.data is not None and self._failure_count <= ROUTE_STALE_TOLERANCE:
                _LOGGER.warning(
                    "%s unavailable (%s/%s), keeping stale data: %s",
                    self.name,
                    self._failure_count,
                    ROUTE_STALE_TOLERANCE,
                    err,
                )
                return self.data
            raise UpdateFailed(f"{self.name} unavailable: {err}") from err
        self._failure_count = 0
        self._report_health(True)
        return result


class StopCoordinator(SourceHealthMixin, DataUpdateCoordinator[list[Departure]]):
    """Coordinator for the departures of one watched transit stop."""

    config_entry: BonnVoyageConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BonnVoyageConfigEntry,
        client: TransitousClient,
        stop_id: str,
        subentry_id: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_stop_{subentry_id}",
            update_interval=STOP_UPDATE_INTERVAL,
        )
        self._client = client
        self._stop_id = stop_id
        self._failure_count = 0

    async def _async_update_data(self) -> list[Departure]:
        """Fetch departures, tolerating brief outages with stale data."""
        try:
            departures = await self._client.async_departures(
                self._stop_id, DEPARTURES_COUNT
            )
        except BonnVoyageApiError as err:
            self._failure_count += 1
            self._report_health(False)
            if self.data is not None and self._failure_count <= STOP_STALE_TOLERANCE:
                _LOGGER.warning(
                    "%s unavailable (%s/%s), keeping stale data: %s",
                    self.name,
                    self._failure_count,
                    STOP_STALE_TOLERANCE,
                    err,
                )
                return self.data
            raise UpdateFailed(f"{self.name} unavailable: {err}") from err
        self._failure_count = 0
        self._report_health(True)
        return departures
