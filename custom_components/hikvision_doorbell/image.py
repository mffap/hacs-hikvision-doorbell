"""Image entity holding the snapshot taken when the doorbell rang."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HikvisionDoorbellConfigEntry
from .const import KIND_INDOOR
from .coordinator import signal_name
from .entity import HikvisionDoorbellEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.kind != KIND_INDOOR:
        async_add_entities([LastSnapshotImage(hass, coordinator)])


class LastSnapshotImage(HikvisionDoorbellEntity, ImageEntity):
    """The add-on's "Latest Snapshot", updated on every ring and on demand."""

    _attr_translation_key = "last_snapshot"
    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        HikvisionDoorbellEntity.__init__(self, coordinator, "last_snapshot")
        ImageEntity.__init__(self, hass)
        self._attr_image_last_updated = coordinator.last_snapshot_time

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_name(self.coordinator.config_entry.entry_id, "snapshot"),
                self._on_snapshot,
            )
        )

    @callback
    def _on_snapshot(self) -> None:
        self._attr_image_last_updated = self.coordinator.last_snapshot_time
        self._cached_image = None
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self.coordinator.last_snapshot
