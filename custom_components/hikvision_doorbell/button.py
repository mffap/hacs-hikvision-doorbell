"""Buttons: open doors, call control, reboot, audio mute."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HikvisionDoorbellConfigEntry
from .api import ISAPIError
from .const import KIND_INDOOR
from .coordinator import HikvisionDoorbellCoordinator
from .entity import HikvisionDoorbellEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class DoorbellButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[HikvisionDoorbellCoordinator], Awaitable[None]]
    refresh: bool = False


BUTTONS: tuple[DoorbellButtonDescription, ...] = (
    DoorbellButtonDescription(
        key="answer_call",
        translation_key="answer_call",
        press_fn=lambda c: c.api.call_signal("answer"),
        refresh=True,
    ),
    DoorbellButtonDescription(
        key="reject_call",
        translation_key="reject_call",
        press_fn=lambda c: c.api.call_signal("reject"),
        refresh=True,
    ),
    DoorbellButtonDescription(
        key="hangup_call",
        translation_key="hangup_call",
        press_fn=lambda c: c.api.call_signal("hangUp"),
        refresh=True,
    ),
    DoorbellButtonDescription(
        key="mute_audio",
        translation_key="mute_audio",
        press_fn=lambda c: c.api.set_output_volume(0),
        entity_category=EntityCategory.CONFIG,
    ),
    DoorbellButtonDescription(
        key="unmute_audio",
        translation_key="unmute_audio",
        press_fn=lambda c: c.api.set_output_volume(7),
        entity_category=EntityCategory.CONFIG,
    ),
    DoorbellButtonDescription(
        key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        press_fn=lambda c: c.api.reboot(),
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = [DoorbellButton(coordinator, d) for d in BUTTONS]
    entities += [OpenDoorButton(coordinator, door) for door in range(1, coordinator.door_count + 1)]
    if coordinator.kind != KIND_INDOOR:
        entities.append(SnapshotButton(coordinator))
    async_add_entities(entities)


class DoorbellButton(HikvisionDoorbellEntity, ButtonEntity):
    entity_description: DoorbellButtonDescription

    def __init__(self, coordinator, description: DoorbellButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        try:
            await self.entity_description.press_fn(self.coordinator)
        except ISAPIError as err:
            raise HomeAssistantError(f"{self.entity_description.key} failed: {err}") from err
        if self.entity_description.refresh:
            await self.coordinator.async_request_refresh()


class OpenDoorButton(HikvisionDoorbellEntity, ButtonEntity):
    """Momentarily releases lock relay N (the add-on's "Door N relay" switch)."""

    _attr_translation_key = "open_door"

    def __init__(self, coordinator, door: int) -> None:
        super().__init__(coordinator, f"open_door_{door}")
        self._door = door
        self._attr_translation_placeholders = {"door": str(door)}

    async def async_press(self) -> None:
        try:
            await self.coordinator.api.open_door(self._door, self.coordinator.is_indoor)
        except ISAPIError as err:
            raise HomeAssistantError(f"Could not open door {self._door}: {err}") from err


class SnapshotButton(HikvisionDoorbellEntity, ButtonEntity):
    """Refresh the "Last snapshot" image on demand."""

    _attr_translation_key = "take_snapshot"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "take_snapshot")

    async def async_press(self) -> None:
        await self.coordinator.async_capture_snapshot(raise_errors=True)
