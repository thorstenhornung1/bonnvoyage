"""Geolocation platform for BonnVoyage traffic messages."""

from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import (
    DOMAIN as GEO_LOCATION_DOMAIN,
    GeolocationEvent,
)
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.location import distance

from .api.models import TrafficMessage, TrafficSegment
from .const import DOMAIN, GEO_REMOVAL_GRACE_POLLS, MessageSource
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
    for source, coordinator in (
        (MessageSource.CITY, runtime.city),
        (MessageSource.AUTOBAHN, runtime.autobahn),
    ):
        _setup_coordinator(hass, entry, source, coordinator, async_add_entities)
    _setup_segments(hass, entry, runtime.traffic, async_add_entities)


@callback
def _remove_orphaned_entities(
    hass: HomeAssistant,
    entry: BonnVoyageConfigEntry,
    unique_id_prefix: str,
    keep_unique_ids: set[str],
) -> None:
    """Drop registry entries of one source that are not in the current data.

    After a restart or reload there is no previous data set to diff against,
    so registry entries of vanished messages would linger as unavailable
    entities. Only called after a fresh, complete fetch of that source; a
    failed or partial refresh never removes anything (0.7.1 wiped every
    geo_location entry on each setup).
    """
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == GEO_LOCATION_DOMAIN
            and reg_entry.platform == DOMAIN
            and reg_entry.unique_id.startswith(unique_id_prefix)
            and reg_entry.unique_id not in keep_unique_ids
        ):
            registry.async_remove(reg_entry.entity_id)


def _mappable(messages: dict[str, TrafficMessage] | None) -> dict[str, TrafficMessage]:
    """Messages that get a map entity (volatile INRIX ids are skipped)."""
    return {
        message_id: message
        for message_id, message in (messages or {}).items()
        if not message.volatile_id
    }


def _setup_segments(
    hass: HomeAssistant,
    entry: BonnVoyageConfigEntry,
    coordinator: BonnTrafficCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Track disrupted city segments inside the area."""
    known_ids: set[str] = set()
    orphans_removed = False

    @callback
    def _check_segments() -> None:
        nonlocal orphans_removed
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
        if not orphans_removed and coordinator.fetch_complete:
            orphans_removed = True
            prefix = f"{entry.entry_id}_segment_"
            _remove_orphaned_entities(
                hass, entry, prefix, {f"{prefix}{sid}" for sid in known_ids}
            )

    _check_segments()
    entry.async_on_unload(coordinator.async_add_listener(_check_segments))


def _setup_coordinator(
    hass: HomeAssistant,
    entry: BonnVoyageConfigEntry,
    source: MessageSource,
    coordinator: MessagesCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Track a coordinator and add entities for new messages."""
    known_ids: set[str] = set()
    orphans_removed = False

    @callback
    def _check_messages() -> None:
        nonlocal orphans_removed
        current = _mappable(coordinator.data)
        new_ids = set(current) - known_ids
        if new_ids:
            known_ids.update(new_ids)
            async_add_entities(
                BonnVoyageLocationEvent(
                    entry, coordinator, known_ids, current[message_id]
                )
                for message_id in new_ids
            )
        # Message ids carry the source as prefix ("city_…", "autobahn_…"),
        # so each source only reaps its own registry entries.
        if not orphans_removed and coordinator.fetch_complete:
            orphans_removed = True
            _remove_orphaned_entities(
                hass,
                entry,
                f"{entry.entry_id}_{source}_",
                {f"{entry.entry_id}_{mid}" for mid in known_ids},
            )

    _check_messages()
    entry.async_on_unload(coordinator.async_add_listener(_check_messages))


@callback
def _async_remove_entity(entity: GeolocationEvent) -> None:
    """Remove a vanished map entity together with its registry entry.

    Removing the registry entry makes Home Assistant force-remove the live
    entity (including its state).
    """
    registry = er.async_get(entity.hass)
    if registry.async_get(entity.entity_id) is not None:
        registry.async_remove(entity.entity_id)
    else:
        entity.hass.async_create_task(entity.async_remove(force_remove=True))


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
        self._absent_polls = 0
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
        """Update from coordinator data; self-remove once the message is gone.

        A message counts as gone only after GEO_REMOVAL_GRACE_POLLS
        consecutive polls that would have contained it: failed refreshes,
        stale bridging and skipped road/service requests do not count. A
        plain unload keeps the registry entries; the orphan cleanup after
        the next complete fetch reaps them then.
        """
        message = (self.coordinator.data or {}).get(self._message_id)
        if message is None:
            if self.coordinator.covers(self._message):
                self._absent_polls += 1
            if self._absent_polls >= GEO_REMOVAL_GRACE_POLLS:
                self._known_ids.discard(self._message_id)
                _async_remove_entity(self)
                return
        else:
            self._absent_polls = 0
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
    _attr_attribution = (
        "Datenquelle: Bundesstadt Bonn, Amt 66, https://opendata.bonn.de"
    )
    # Speed and measurement time change on nearly every 5-minute poll; keeping
    # them out of the recorder avoids a new attributes row per write.
    _unrecorded_attributes = frozenset({"speed_kmh", "measured_at"})

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
        self._absent_polls = 0
        self._attr_unique_id = f"{entry.entry_id}_segment_{segment.segment_id}"
        self._update_from_segment(segment)

    def _update_from_segment(self, segment: TrafficSegment) -> None:
        """Copy segment fields into entity attributes."""
        self._segment = segment
        # No speed in the name: it changed the friendly_name (and with it the
        # recorded attributes) on most polls. The speed is in speed_kmh.
        self._attr_name = segment.raw_status or segment.status
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

        Same lifecycle as the message events, including the grace period of
        GEO_REMOVAL_GRACE_POLLS complete polls.
        """
        segment = self.coordinator.disrupted_in_area().get(self._segment_id)
        if segment is None:
            if self.coordinator.fetch_complete:
                self._absent_polls += 1
            if self._absent_polls >= GEO_REMOVAL_GRACE_POLLS:
                self._known_ids.discard(self._segment_id)
                _async_remove_entity(self)
                return
        else:
            self._absent_polls = 0
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
