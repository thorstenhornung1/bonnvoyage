"""Event platform for BonnVoyage traffic messages."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EVENT_TYPE_NEW_MESSAGE, MESSAGES_DEVICE_ID
from .coordinator import (
    AutobahnCoordinator,
    BonnGeoCoordinator,
    BonnVoyageConfigEntry,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BonnVoyageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the new-message event entity."""
    runtime = entry.runtime_data
    async_add_entities([BonnVoyageNewMessageEvent(runtime.city, runtime.autobahn)])


class BonnVoyageNewMessageEvent(CoordinatorEntity[BonnGeoCoordinator], EventEntity):
    """Fires an event for every newly appearing traffic message."""

    _attr_has_entity_name = True
    _attr_translation_key = "new_message"
    _attr_unique_id = "new_message"

    def __init__(self, city: BonnGeoCoordinator, autobahn: AutobahnCoordinator) -> None:
        """Initialize with both message coordinators."""
        super().__init__(city)
        self._attr_event_types = [EVENT_TYPE_NEW_MESSAGE]
        self._autobahn = autobahn
        self._seen: set[str] | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, MESSAGES_DEVICE_ID)},
            name="Verkehrsmeldungen Bonn",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to the second coordinator and set the baseline."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._autobahn.async_add_listener(self._handle_coordinator_update)
        )
        self._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Available while at least one message source works."""
        return (
            self.coordinator.last_update_success or self._autobahn.last_update_success
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Diff current messages against the seen set; fire for new ones."""
        current = (self.coordinator.data or {}) | (self._autobahn.data or {})
        if self._seen is None:
            # First data set after (re)start: baseline only, no event storm.
            self._seen = set(current)
            self.async_write_ha_state()
            return

        for message_id in set(current) - self._seen:
            message = current[message_id]
            self._trigger_event(
                EVENT_TYPE_NEW_MESSAGE,
                {
                    "external_id": message.message_id,
                    "title": message.title,
                    "message_source": message.source,
                    "kind": message.kind,
                    "road": message.road,
                    "latitude": message.latitude,
                    "longitude": message.longitude,
                },
            )
            self.async_write_ha_state()

        self._seen = set(current)
        self.async_write_ha_state()
