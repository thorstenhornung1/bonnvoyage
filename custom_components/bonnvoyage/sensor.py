"""Sensor platform for BonnVoyage parking facilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    PERCENTAGE,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import ParkingFacility, RouteResult
from .const import (
    CONF_FACILITY_ID,
    CONF_SEGMENT_ID,
    DOMAIN,
    MESSAGES_DEVICE_ID,
    SUBENTRY_TYPE_PARKING,
    SUBENTRY_TYPE_ROUTE,
    SUBENTRY_TYPE_SEGMENT,
    TRAFFIC_LEVEL_CONGESTED_RATIO,
    TRAFFIC_LEVEL_HEAVY_RATIO,
    TRAFFIC_LEVEL_QUEUING_RATIO,
    ParkingTrend,
    SegmentStatus,
    TrafficLevel,
)
from .coordinator import (
    AutobahnCoordinator,
    BcpCoordinator,
    BonnGeoCoordinator,
    BonnTrafficCoordinator,
    BonnVoyageConfigEntry,
    ParkingData,
    RouteCoordinator,
)
from .entity import BonnVoyageParkingEntity
from .route_entity import BonnVoyageRouteEntity, BonnVoyageSegmentEntity

PARALLEL_UPDATES = 0


def traffic_level(result: RouteResult) -> TrafficLevel | None:
    """Derive the congestion level from the live/no-traffic time ratio."""
    if not result.no_traffic_time_s:
        return None
    ratio = result.travel_time_s / result.no_traffic_time_s
    if ratio >= TRAFFIC_LEVEL_CONGESTED_RATIO:
        return TrafficLevel.CONGESTED
    if ratio >= TRAFFIC_LEVEL_QUEUING_RATIO:
        return TrafficLevel.QUEUING
    if ratio >= TRAFFIC_LEVEL_HEAVY_RATIO:
        return TrafficLevel.HEAVY
    return TrafficLevel.FREE_FLOW


@dataclass(frozen=True, kw_only=True)
class BonnVoyageSensorDescription(SensorEntityDescription):
    """Describes a BonnVoyage parking sensor."""

    value_fn: Callable[[ParkingData], StateType]


def _occupancy(data: ParkingData) -> StateType:
    if data.status.free is None or not data.status.capacity:
        return None
    occupied = data.status.capacity - data.status.free
    return round(occupied / data.status.capacity * 100, 1)


SENSOR_DESCRIPTIONS: tuple[BonnVoyageSensorDescription, ...] = (
    BonnVoyageSensorDescription(
        key="free_spaces",
        translation_key="free_spaces",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.free,
    ),
    BonnVoyageSensorDescription(
        key="occupancy",
        translation_key="occupancy",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_occupancy,
    ),
    BonnVoyageSensorDescription(
        key="trend",
        translation_key="trend",
        device_class=SensorDeviceClass.ENUM,
        options=[trend.value for trend in ParkingTrend],
        value_fn=lambda data: data.trend.value if data.trend else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BonnVoyageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up parking, message, segment and route sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            BonnVoyageActiveMessagesSensor(runtime.city, runtime.autobahn),
            BonnVoyageDisruptedSegmentsSensor(runtime.traffic),
        ]
    )

    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_SEGMENT:
            async_add_entities(
                (
                    BonnVoyageSegmentSensor(
                        runtime.traffic,
                        subentry.data[CONF_SEGMENT_ID],
                        subentry.title,
                        key,
                    )
                    for key in ("segment_status", "segment_speed")
                ),
                config_subentry_id=subentry.subentry_id,
            )
        elif subentry.subentry_type == SUBENTRY_TYPE_ROUTE:
            coordinator = runtime.routes.get(subentry.subentry_id)
            if coordinator is None:
                continue
            async_add_entities(
                (
                    BonnVoyageRouteSensor(
                        coordinator, subentry.subentry_id, subentry.title, key
                    )
                    for key in ("travel_time", "delay", "traffic_level")
                ),
                config_subentry_id=subentry.subentry_id,
            )

    coordinator_parking = runtime.parking
    if coordinator_parking is None:
        return
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_PARKING:
            continue
        facility = coordinator_parking.facilities.get(subentry.data[CONF_FACILITY_ID])
        if facility is None:
            continue
        async_add_entities(
            (
                BonnVoyageParkingSensor(coordinator_parking, facility, description)
                for description in SENSOR_DESCRIPTIONS
            ),
            config_subentry_id=subentry.subentry_id,
        )


class BonnVoyageParkingSensor(BonnVoyageParkingEntity, SensorEntity):
    """Sensor for one aspect of a parking facility."""

    entity_description: BonnVoyageSensorDescription

    def __init__(
        self,
        coordinator: BcpCoordinator,
        facility: ParkingFacility,
        description: BonnVoyageSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, facility, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        data = self.parking_data
        if data is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose coordinates on the primary sensor for map display."""
        if self.entity_description.key != "free_spaces":
            return None
        attributes: dict[str, Any] = {}
        if self._facility.latitude is not None:
            attributes[ATTR_LATITUDE] = self._facility.latitude
            attributes[ATTR_LONGITUDE] = self._facility.longitude
        if (data := self.parking_data) is not None:
            attributes["capacity"] = data.status.capacity
        return attributes or None


class BonnVoyageActiveMessagesSensor(
    CoordinatorEntity[BonnGeoCoordinator], SensorEntity
):
    """Number of active traffic messages inside the configured area."""

    _attr_has_entity_name = True
    _attr_translation_key = "active_messages"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_unique_id = "active_messages"
    _unrecorded_attributes = frozenset({"messages"})

    def __init__(self, city: BonnGeoCoordinator, autobahn: AutobahnCoordinator) -> None:
        """Initialize with both message coordinators."""
        super().__init__(city)
        self._autobahn = autobahn
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, MESSAGES_DEVICE_ID)},
            name="Verkehrsmeldungen Bonn",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to the second coordinator as well."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._autobahn.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def available(self) -> bool:
        """Available while at least one message source works."""
        return (
            self.coordinator.last_update_success or self._autobahn.last_update_success
        )

    @property
    def native_value(self) -> int:
        """Return the total number of active messages."""
        return len(self.coordinator.data or {}) + len(self._autobahn.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Compact list of active messages (excluded from the recorder)."""
        messages = [
            {"id": message.message_id, "title": message.title, "kind": message.kind}
            for coordinator_data in (self.coordinator.data, self._autobahn.data)
            for message in (coordinator_data or {}).values()
        ]
        return {"messages": messages}


class BonnVoyageDisruptedSegmentsSensor(
    CoordinatorEntity[BonnTrafficCoordinator], SensorEntity
):
    """Number of disrupted city segments inside the configured area."""

    _attr_has_entity_name = True
    _attr_attribution = (
        "Datenquelle: Bundesstadt Bonn, Amt 66, https://opendata.bonn.de"
    )
    _attr_translation_key = "disrupted_segments"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_unique_id = "disrupted_segments"

    def __init__(self, coordinator: BonnTrafficCoordinator) -> None:
        """Initialize the counter."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, MESSAGES_DEVICE_ID)},
            name="Verkehrsmeldungen Bonn",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int:
        """Return the number of disrupted segments in the area."""
        return len(self.coordinator.disrupted_in_area())


class BonnVoyageSegmentSensor(BonnVoyageSegmentEntity, SensorEntity):
    """Status or speed sensor of one watched city segment."""

    def __init__(
        self,
        coordinator: BonnTrafficCoordinator,
        segment_id: str,
        segment_name: str,
        key: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, segment_id, segment_name, key)
        self._key = key
        if key == "segment_status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [status.value for status in SegmentStatus]
        else:
            self._attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> StateType:
        """Return status or speed."""
        segment = self.segment
        if segment is None:
            return None
        if self._key == "segment_status":
            return segment.status
        return segment.speed_kmh

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose position and raw status on the status sensor."""
        if self._key != "segment_status" or (segment := self.segment) is None:
            return None
        return {
            ATTR_LATITUDE: segment.midpoint[0],
            ATTR_LONGITUDE: segment.midpoint[1],
            "segment_id": segment.segment_id,
            "raw_status": segment.raw_status,
            "measured_at": segment.measured_at,
        }


class BonnVoyageRouteSensor(BonnVoyageRouteEntity, SensorEntity):
    """Travel time, delay or traffic level of one configured route."""

    def __init__(
        self,
        coordinator: RouteCoordinator,
        subentry_id: str,
        route_name: str,
        key: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, subentry_id, route_name, key)
        self._key = key
        if key == "traffic_level":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [level.value for level in TrafficLevel]
        else:
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
            self._attr_suggested_unit_of_measurement = UnitOfTime.MINUTES
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 0

    @property
    def native_value(self) -> StateType:
        """Return the value for this sensor."""
        result = self.coordinator.data
        if result is None:
            return None
        if self._key == "travel_time":
            return result.travel_time_s
        if self._key == "delay":
            return result.delay_s
        level = traffic_level(result)
        return level.value if level else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the route length on the travel time sensor."""
        if self._key != "travel_time":
            return None
        result = self.coordinator.data
        if result is None or result.length_m is None:
            return None
        return {"length_km": round(result.length_m / 1000, 1)}
