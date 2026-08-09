"""Binary sensor platform for BonnVoyage parking facilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import ParkingFacility
from .const import (
    CONF_DELAY_ALERT_THRESHOLD,
    CONF_DELAY_THRESHOLD,
    CONF_FACILITY_ID,
    CONF_SEGMENT_ID,
    DEFAULT_DELAY_ALERT_MIN,
    DEFAULT_DELAY_THRESHOLD_MIN,
    DISRUPTED_SEGMENT_STATUSES,
    SUBENTRY_TYPE_PARKING,
    SUBENTRY_TYPE_ROUTE,
    SUBENTRY_TYPE_SEGMENT,
    SUBENTRY_TYPE_STOP,
)
from .coordinator import (
    BcpCoordinator,
    BonnTrafficCoordinator,
    BonnVoyageConfigEntry,
    ParkingData,
    RouteCoordinator,
    StopCoordinator,
)
from .entity import BonnVoyageParkingEntity
from .route_entity import (
    BonnVoyageRouteEntity,
    BonnVoyageSegmentEntity,
    BonnVoyageStopEntity,
)

PARALLEL_UPDATES = 0


def _is_full(data: ParkingData) -> bool | None:
    if data.status.is_open is not True or data.status.free is None:
        return None
    return data.status.free == 0


@dataclass(frozen=True, kw_only=True)
class BonnVoyageBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a BonnVoyage parking binary sensor."""

    value_fn: Callable[[ParkingData], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[BonnVoyageBinarySensorDescription, ...] = (
    BonnVoyageBinarySensorDescription(
        key="open",
        translation_key="open",
        value_fn=lambda data: data.status.is_open,
    ),
    BonnVoyageBinarySensorDescription(
        key="full",
        translation_key="full",
        value_fn=_is_full,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BonnVoyageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors for parking, segment and route subentries."""
    runtime = entry.runtime_data
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_SEGMENT:
            async_add_entities(
                [
                    BonnVoyageSegmentCongestionSensor(
                        runtime.traffic, subentry.data[CONF_SEGMENT_ID], subentry.title
                    )
                ],
                config_subentry_id=subentry.subentry_id,
            )
        elif subentry.subentry_type == SUBENTRY_TYPE_STOP:
            stop_coordinator = runtime.stops.get(subentry.subentry_id)
            if stop_coordinator is None:
                continue
            async_add_entities(
                [
                    BonnVoyageStopDelaySensor(
                        stop_coordinator,
                        subentry.subentry_id,
                        subentry.title,
                        subentry.data.get(
                            CONF_DELAY_ALERT_THRESHOLD, DEFAULT_DELAY_ALERT_MIN
                        ),
                    )
                ],
                config_subentry_id=subentry.subentry_id,
            )
        elif subentry.subentry_type == SUBENTRY_TYPE_ROUTE:
            route_coordinator = runtime.routes.get(subentry.subentry_id)
            if route_coordinator is None:
                continue
            async_add_entities(
                [
                    BonnVoyageRouteCongestionSensor(
                        route_coordinator,
                        subentry.subentry_id,
                        subentry.title,
                        subentry.data.get(
                            CONF_DELAY_THRESHOLD, DEFAULT_DELAY_THRESHOLD_MIN
                        ),
                    )
                ],
                config_subentry_id=subentry.subentry_id,
            )

    coordinator = runtime.parking
    if coordinator is None:
        return
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_PARKING:
            continue
        facility = coordinator.facilities.get(subentry.data[CONF_FACILITY_ID])
        if facility is None:
            continue
        async_add_entities(
            (
                BonnVoyageParkingBinarySensor(coordinator, facility, description)
                for description in BINARY_SENSOR_DESCRIPTIONS
            ),
            config_subentry_id=subentry.subentry_id,
        )


class BonnVoyageParkingBinarySensor(BonnVoyageParkingEntity, BinarySensorEntity):
    """Binary sensor for one aspect of a parking facility."""

    entity_description: BonnVoyageBinarySensorDescription

    def __init__(
        self,
        coordinator: BcpCoordinator,
        facility: ParkingFacility,
        description: BonnVoyageBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, facility, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the binary state."""
        data = self.parking_data
        if data is None:
            return None
        return self.entity_description.value_fn(data)


class BonnVoyageSegmentCongestionSensor(BonnVoyageSegmentEntity, BinarySensorEntity):
    """Congestion indicator of one watched city segment."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: BonnTrafficCoordinator,
        segment_id: str,
        segment_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, segment_id, segment_name, "segment_congestion")

    @property
    def is_on(self) -> bool | None:
        """On while the segment status is a disruption."""
        segment = self.segment
        if segment is None:
            return None
        if segment.status == "unknown":
            return None
        return segment.status in DISRUPTED_SEGMENT_STATUSES


class BonnVoyageRouteCongestionSensor(BonnVoyageRouteEntity, BinarySensorEntity):
    """Congestion indicator of one configured route."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: RouteCoordinator,
        subentry_id: str,
        route_name: str,
        delay_threshold_min: float,
    ) -> None:
        """Initialize the sensor with the configured delay threshold."""
        super().__init__(coordinator, subentry_id, route_name, "route_congestion")
        self._threshold_s = int(delay_threshold_min * 60)

    @property
    def is_on(self) -> bool | None:
        """On while the delay reaches the configured threshold."""
        result = self.coordinator.data
        if result is None or result.delay_s is None:
            return None
        return result.delay_s >= self._threshold_s

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Expose the configured threshold."""
        return {"delay_threshold_min": self._threshold_s // 60}


class BonnVoyageStopDelaySensor(BonnVoyageStopEntity, BinarySensorEntity):
    """Delay alert of one watched stop."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: StopCoordinator,
        subentry_id: str,
        stop_name: str,
        threshold_min: float,
    ) -> None:
        """Initialize the sensor with the configured threshold."""
        super().__init__(coordinator, subentry_id, stop_name, "stop_delay")
        self._threshold_s = int(threshold_min * 60)

    @property
    def is_on(self) -> bool | None:
        """On while any upcoming departure is delayed beyond the threshold."""
        delays = [dep.delay_s for dep in self.upcoming() if dep.delay_s is not None]
        if not delays:
            return None
        return max(delays) >= self._threshold_s

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Expose the configured threshold."""
        return {"delay_alert_threshold_min": self._threshold_s // 60}
