"""Switches: indoor station COM outputs and alarm arming."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import HikvisionDoorbellConfigEntry
from .api import ISAPIError
from .const import ALARM_ARM, ALARM_DISARM
from .entity import HikvisionDoorbellEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [ComSwitch(coordinator, n) for n in range(coordinator.com_count)]
    if coordinator.alarm_enabled:
        entities.append(AlarmSwitch(coordinator))
    async_add_entities(entities)


class ComSwitch(HikvisionDoorbellEntity, SwitchEntity, RestoreEntity):
    """COM output relay. The device cannot report its state, so it is assumed."""

    _attr_translation_key = "com_output"
    _attr_assumed_state = True

    def __init__(self, coordinator, output: int) -> None:
        super().__init__(coordinator, f"com_output_{output}")
        self._output = output
        self._attr_translation_placeholders = {"output": str(output + 1)}
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"

    async def _set(self, on: bool) -> None:
        try:
            await self.coordinator.api.set_com_output(self._output, on)
        except ISAPIError as err:
            raise HomeAssistantError(f"Could not switch COM {self._output + 1}: {err}") from err
        self._attr_is_on = on
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class AlarmSwitch(HikvisionDoorbellEntity, SwitchEntity):
    """Arms / disarms the indoor station alarm (setupAlarm / closeAlarm)."""

    _attr_translation_key = "alarm"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "alarm")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.alarm_armed

    async def _set(self, armed: bool) -> None:
        try:
            await self.coordinator.api.set_alarm(ALARM_ARM if armed else ALARM_DISARM)
        except ISAPIError as err:
            raise HomeAssistantError(f"Could not change alarm state: {err}") from err
        self.coordinator.async_set_updated_data(replace(self.coordinator.data, alarm_armed=armed))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
