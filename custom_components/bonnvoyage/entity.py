"""Base entity for BonnVoyage parking entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api.models import ParkingFacility
from .const import DOMAIN, RUNTIME_STALE_AFTER
from .coordinator import BcpCoordinator, ParkingData


class BonnVoyageParkingEntity(CoordinatorEntity[BcpCoordinator]):
    """Base class for entities belonging to one parking facility device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BcpCoordinator,
        facility: ParkingFacility,
        key: str,
    ) -> None:
        """Initialize the entity for one facility."""
        super().__init__(coordinator)
        self._facility = facility
        self._attr_unique_id = f"{facility.facility_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, facility.facility_id)},
            name=facility.name,
            manufacturer=facility.operator,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def parking_data(self) -> ParkingData | None:
        """Return the current data for this facility, if present."""
        return self.coordinator.data.get(self._facility.facility_id)

    @property
    def available(self) -> bool:
        """Unavailable if the facility is missing from the feed or stale."""
        if not super().available:
            return False
        data = self.parking_data
        if data is None:
            return False
        updated_at = data.status.updated_at
        return (
            updated_at is not None
            and dt_util.utcnow() - updated_at < RUNTIME_STALE_AFTER
        )
