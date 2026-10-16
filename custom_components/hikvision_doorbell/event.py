"""Event entities: doorbell ring, access control and generic device events."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HikvisionDoorbellConfigEntry
from .const import ACS_EVENT_OTHER, ACS_EVENT_TYPES, DEVICE_EVENT_OTHER, DEVICE_EVENT_TYPES, KIND_INDOOR
from .coordinator import signal_name
from .entity import HikvisionDoorbellEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[EventEntity] = [
        RingEvent(coordinator),
        DispatchedEvent(
            coordinator,
            "device_event",
            "device",
            sorted(set(DEVICE_EVENT_TYPES.values())),
            DEVICE_EVENT_OTHER,
        ),
    ]
    if coordinator.kind != KIND_INDOOR:
        entities.append(
            DispatchedEvent(
                coordinator,
                "access_control",
                "access_control",
                sorted(set(ACS_EVENT_TYPES.values())),
                ACS_EVENT_OTHER,
            )
        )
    async_add_entities(entities)


def _attributes(payload: dict[str, Any]) -> dict[str, Any]:
    # `raw` can be large; keep it but drop picture payloads if any slipped in
    return {k: v for k, v in payload.items() if not isinstance(v, (bytes, bytearray))}


class RingEvent(HikvisionDoorbellEntity, EventEntity):
    """Fires `ring` whenever somebody presses the call button."""

    _attr_translation_key = "doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "doorbell")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_name(self.coordinator.config_entry.entry_id, "ring"), self._on_ring
            )
        )

    @callback
    def _on_ring(self, payload: dict[str, Any]) -> None:
        self._trigger_event("ring", _attributes(payload))
        self.async_write_ha_state()


class DispatchedEvent(HikvisionDoorbellEntity, EventEntity):
    """Event entity fed by the event stream dispatcher."""

    def __init__(
        self, coordinator, key: str, category: str, event_types: list[str], other: str
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._attr_event_types = [*event_types, other]
        self._category = category
        self._other = other

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_name(self.coordinator.config_entry.entry_id, self._category),
                self._on_event,
            )
        )

    @callback
    def _on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type not in self._attr_event_types:
            event_type = self._other
        self._trigger_event(event_type, _attributes(payload))
        self.async_write_ha_state()
