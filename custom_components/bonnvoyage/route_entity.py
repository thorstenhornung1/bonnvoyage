"""Base entities for route and city segment devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import Departure, TrafficSegment
from .const import DOMAIN
from .coordinator import BonnTrafficCoordinator, RouteCoordinator, StopCoordinator


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


class BonnVoyageStopEntity(CoordinatorEntity[StopCoordinator]):
    """Base class for entities of one watched transit stop."""

    _attr_has_entity_name = True
    _attr_attribution = "transitous.org (DELFI)"

    def __init__(
        self,
        coordinator: StopCoordinator,
        subentry_id: str,
        stop_name: str,
        key: str,
    ) -> None:
        """Initialize the entity for one stop."""
        super().__init__(coordinator)
        self._attr_unique_id = f"stop_{subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"stop_{subentry_id}")},
            name=stop_name,
            entry_type=DeviceEntryType.SERVICE,
        )

    def upcoming(self) -> list[Departure]:
        """Non-cancelled upcoming departures."""
        return [dep for dep in (self.coordinator.data or []) if not dep.cancelled]


SEVERITY_ORDER = ("normal", "elevated", "congestion_risk", "congested")


class BonnVoyageCorridorEntity(CoordinatorEntity[BonnTrafficCoordinator]):
    """Base class for aggregated corridor (multi-segment) entities."""

    _attr_has_entity_name = True
    _attr_attribution = (
        "Datenquelle: Bundesstadt Bonn, Amt 66, https://opendata.bonn.de"
    )

    def __init__(
        self,
        coordinator: BonnTrafficCoordinator,
        subentry_id: str,
        corridor_name: str,
        segment_ids: list[str],
        segment_meta: dict[str, dict[str, str | None]],
        key: str,
    ) -> None:
        """Initialize the entity for one corridor."""
        super().__init__(coordinator)
        self._segment_ids = segment_ids
        self._segment_meta = segment_meta
        self._attr_unique_id = f"corridor_{subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"corridor_{subentry_id}")},
            name=corridor_name,
            entry_type=DeviceEntryType.SERVICE,
        )

    def corridor(self) -> list[TrafficSegment]:
        """Current data of the corridor segments (present ones only)."""
        data = self.coordinator.data or {}
        return [
            data[segment_id] for segment_id in self._segment_ids if segment_id in data
        ]

    @property
    def available(self) -> bool:
        """Unavailable once every corridor segment vanished from the layer."""
        return super().available and bool(self.corridor())

    def worst_status(self) -> str | None:
        """Most severe known status along the corridor."""
        statuses = [
            segment.status
            for segment in self.corridor()
            if segment.status in SEVERITY_ORDER
        ]
        if not statuses:
            return "unknown" if self.corridor() else None
        return max(statuses, key=SEVERITY_ORDER.index)

    def segment_summary(self) -> list[dict[str, object]]:
        """Per-segment detail list for attributes."""
        summary: list[dict[str, object]] = []
        for segment in self.corridor():
            meta = self._segment_meta.get(segment.segment_id, {})
            street = meta.get("street_name") or f"Abschnitt #{segment.segment_id}"
            summary.append(
                {
                    "segment": f"{street} ({meta.get('direction', '?')})",
                    "status": segment.status,
                    "speed_kmh": segment.speed_kmh,
                }
            )
        return summary
