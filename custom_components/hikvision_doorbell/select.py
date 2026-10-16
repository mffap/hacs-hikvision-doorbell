"""Scene select for indoor stations (at home / go out / go to bed / custom)."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HikvisionDoorbellConfigEntry
from .api import ISAPIError
from .const import SCENES
from .entity import HikvisionDoorbellEntity

PARALLEL_UPDATES = 1

# HA option keys must be lowercase snake_case
_TO_OPTION = {"atHome": "at_home", "goOut": "go_out", "goToBed": "go_to_bed", "custom": "custom"}
_FROM_OPTION = {v: k for k, v in _TO_OPTION.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.scenes_enabled:
        async_add_entities([SceneSelect(coordinator)])


class SceneSelect(HikvisionDoorbellEntity, SelectEntity):
    _attr_translation_key = "scene"
    _attr_options = [_TO_OPTION[s] for s in SCENES]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "scene")

    @property
    def current_option(self) -> str | None:
        return _TO_OPTION.get(self.coordinator.data.scene or "")

    async def async_select_option(self, option: str) -> None:
        scene = _FROM_OPTION[option]
        try:
            await self.coordinator.api.set_scene(scene)
        except ISAPIError as err:
            raise HomeAssistantError(f"Could not set scene: {err}") from err
        self.coordinator.async_set_updated_data(replace(self.coordinator.data, scene=scene))
