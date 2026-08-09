"""Base entities for route and city segment devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import TrafficSegment
from .const import DOMAIN
from .coordinator import BonnTrafficCoordinator, RouteCoordinator


class BonnVoyageRouteEntity(CoordinatorEntity[RouteCoordinator]):
    """Base class for entities of one configured route."""

    _attr_has_entity_name = True
    _attr_attribution = "© TomTom"

    def __init__(
        self,
        coordinator: RouteCoordinator,
        subentry_id: str,
        route_name: str,
        key: str,
    ) -> None:
        """Initialize the entity for one route."""
        super().__init__(coordinator)
        self._attr_unique_id = f"route_{subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"route_{subentry_id}")},
            name=route_name,
            entry_type=DeviceEntryType.SERVICE,
        )


class BonnVoyageSegmentEntity(CoordinatorEntity[BonnTrafficCoordinator]):
    """Base class for entities of one watched city segment."""

    _attr_has_entity_name = True
    # CC BY attribution wording mandated by the dataset page
    _attr_attribution = (
        "Datenquelle: Bundesstadt Bonn, Amt 66, https://opendata.bonn.de"
    )

    def __init__(
        self,
        coordinator: BonnTrafficCoordinator,
        segment_id: str,
        segment_name: str,
        key: str,
    ) -> None:
        """Initialize the entity for one segment."""
        super().__init__(coordinator)
        self._segment_id = segment_id
        self._attr_unique_id = f"segment_{segment_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"segment_{segment_id}")},
            name=segment_name,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def segment(self) -> TrafficSegment | None:
        """Return the current segment data, if present."""
        return (self.coordinator.data or {}).get(self._segment_id)

    @property
    def available(self) -> bool:
        """Unavailable if the segment vanished from the layer."""
        return super().available and self.segment is not None
