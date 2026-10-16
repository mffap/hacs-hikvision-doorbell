"""Call state sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HikvisionDoorbellConfigEntry
from .const import CALL_STATES
from .entity import HikvisionDoorbellEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CallStateSensor(entry.runtime_data)])


class CallStateSensor(HikvisionDoorbellEntity, SensorEntity):
    """idle / ringing / on_call, like the add-on's "Call state" sensor."""

    _attr_translation_key = "call_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CALL_STATES

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "call_state")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.call_state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "raw_status": data.raw_call_status,
            "last_ring": data.last_ring.isoformat() if data.last_ring else None,
        }
