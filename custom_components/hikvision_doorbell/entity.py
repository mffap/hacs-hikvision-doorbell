"""Base entity for the Hikvision Doorbell integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HikvisionDoorbellCoordinator


class HikvisionDoorbellEntity(CoordinatorEntity[HikvisionDoorbellCoordinator]):
    """Common attributes for all entities of a doorbell device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HikvisionDoorbellCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.serial}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.device.serial)})
