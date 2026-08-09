"""Geolocation platform for BonnVoyage traffic messages."""

from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.location import distance

from .api.models import TrafficMessage, TrafficSegment
from .const import DOMAIN, MessageSource
from .coordinator import (
    BonnTrafficCoordinator,
    BonnVoyageConfigEntry,
    MessagesCoordinator,
)

PARALLEL_UPDATES = 0

ATTRIBUTION = {
    MessageSource.CITY.value: "Stadt Bonn",
    MessageSource.AUTOBAHN.value: "Autobahn GmbH des Bundes",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BonnVoyageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up geolocation events for messages and disrupted segments."""
    runtime = entry.runtime_data
    for coordinator in (runtime.city, runtime.autobahn):
        _setup_coordinator(entry, coordinator, async_add_entities)
    _setup_segments(entry, runtime.traffic, async_add_entities)


def _setup_segments(
    entry: BonnVoyageConfigEntry,
    coordinator: BonnTrafficCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Track disrupted city segments inside the area."""
    known_ids: set[str] = set()

    @callback
    def _check_segments() -> None:
        current = coordinator.disrupted_in_area()
        new_ids = set(current) - known_ids
        if new_ids:
            known_ids.update(new_ids)
            async_add_entities(
                BonnVoyageSegmentLocationEvent(
                    entry, coordinator, known_ids, current[segment_id]
                )
                for segment_id in new_ids
            )

    _check_segments()
    entry.async_on_unload(coordinator.async_add_listener(_check_segments))


def _setup_coordinator(
    entry: BonnVoyageConfigEntry,
    coordinator: MessagesCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Track a coordinator and add entities for new messages."""
    known_ids: set[str] = set()

    @callback
    def _check_messages() -> None:
        current = coordinator.data or {}
        new_ids = set(current) - known_ids
        if new_ids:
            known_ids.update(new_ids)
            async_add_entities(
                BonnVoyageLocationEvent(
                    entry, coordinator, known_ids, current[message_id]
                )
                for message_id in new_ids
            )

    _check_messages()
    entry.async_on_unload(coordinator.async_add_listener(_check_messages))


class BonnVoyageLocationEvent(CoordinatorEntity[MessagesCoordinator], GeolocationEvent):
    """A traffic message shown on the map; state is the distance from home."""

    _attr_should_poll = False
    _attr_source = DOMAIN
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:traffic-cone"
    _unrecorded_attributes = frozenset(
        {"description", "restriction", "address", "authority"}
    )

    def __init__(
        self,
        entry: BonnVoyageConfigEntry,
        coordinator: MessagesCoordinator,
        known_ids: set[str],
        message: TrafficMessage,
    ) -> None:
        """Initialize from the current message state."""
        super().__init__(coordinator)
        self._message_id = message.message_id
        self._known_ids = known_ids
        self._attr_unique_id = f"{entry.entry_id}_{message.message_id}"
        self._attr_attribution = ATTRIBUTION.get(message.source)
        self._update_from_message(message)

    def _update_from_message(self, message: TrafficMessage) -> None:
        """Copy message fields into entity attributes."""
        self._message = message
        self._attr_name = message.title
        self._attr_latitude = message.latitude
        self._attr_longitude = message.longitude
        meters = distance(
            self.hass.config.latitude if self.hass else None,
            self.hass.config.longitude if self.hass else None,
            message.latitude,
            message.longitude,
        )
        self._attr_distance = None if meters is None else round(meters / 1000, 1)

    async def async_added_to_hass(self) -> None:
        """Compute the distance once the hass reference exists."""
        await super().async_added_to_hass()
        self._update_from_message(self._message)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update from coordinator data; self-remove if the message is gone.

        Removing the registry entry makes Home Assistant force-remove the
        live entity (including its state). A plain unload keeps the registry
        entries; the orphan cleanup in async_setup_entry reaps them then.
        """
        message = (self.coordinator.data or {}).get(self._message_id)
        if message is None:
            self._known_ids.discard(self._message_id)
            registry = er.async_get(self.hass)
            if registry.async_get(self.entity_id) is not None:
                registry.async_remove(self.entity_id)
            else:
                self.hass.async_create_task(self.async_remove(force_remove=True))
            return
        self._update_from_message(message)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return message details."""
        message = self._message
        attributes: dict[str, Any] = {
            "external_id": message.message_id,
            "message_source": message.source,
            "kind": message.kind,
        }
        for key, value in (
            ("road", message.road),
            ("is_blocked", message.is_blocked),
            ("description", message.description),
            ("restriction", message.restriction),
            ("address", message.address),
            ("authority", message.authority),
            ("starts_at", message.starts_at),
            ("ends_at", message.ends_at),
        ):
            if value is not None:
                attributes[key] = value
        return attributes


class BonnVoyageSegmentLocationEvent(
    CoordinatorEntity[BonnTrafficCoordinator], GeolocationEvent
):
    """A disrupted city segment on the map; state is the distance from home."""

    _attr_should_poll = False
    _attr_source = DOMAIN
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:car-brake-alert"
    _attr_attribution = "Stadt Bonn"

    def __init__(
        self,
        entry: BonnVoyageConfigEntry,
        coordinator: BonnTrafficCoordinator,
        known_ids: set[str],
        segment: TrafficSegment,
    ) -> None:
        """Initialize from the current segment state."""
        super().__init__(coordinator)
        self._segment_id = segment.segment_id
        self._known_ids = known_ids
        self._attr_unique_id = f"{entry.entry_id}_segment_{segment.segment_id}"
        self._update_from_segment(segment)

    def _update_from_segment(self, segment: TrafficSegment) -> None:
        """Copy segment fields into entity attributes."""
        self._segment = segment
        name = segment.raw_status or segment.status
        if segment.speed_kmh is not None:
            name = f"{name} ({segment.speed_kmh} km/h)"
        self._attr_name = name
        self._attr_latitude = segment.midpoint[0]
        self._attr_longitude = segment.midpoint[1]
        meters = distance(
            self.hass.config.latitude if self.hass else None,
            self.hass.config.longitude if self.hass else None,
            segment.midpoint[0],
            segment.midpoint[1],
        )
        self._attr_distance = None if meters is None else round(meters / 1000, 1)

    async def async_added_to_hass(self) -> None:
        """Compute the distance once the hass reference exists."""
        await super().async_added_to_hass()
        self._update_from_segment(self._segment)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update from coordinator data; self-remove once no longer disrupted.

        Same lifecycle as the message events: removing the registry entry
        makes Home Assistant force-remove the live entity.
        """
        segment = self.coordinator.disrupted_in_area().get(self._segment_id)
        if segment is None:
            self._known_ids.discard(self._segment_id)
            registry = er.async_get(self.hass)
            if registry.async_get(self.entity_id) is not None:
                registry.async_remove(self.entity_id)
            else:
                self.hass.async_create_task(self.async_remove(force_remove=True))
            return
        self._update_from_segment(segment)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return segment details."""
        segment = self._segment
        return {
            "segment_id": segment.segment_id,
            "message_source": "city_traffic",
            "status": segment.status,
            "raw_status": segment.raw_status,
            "speed_kmh": segment.speed_kmh,
            "measured_at": segment.measured_at,
        }
