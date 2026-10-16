"""Live camera for door stations (JPEG snapshots + RTSP stream)."""

from __future__ import annotations

from urllib.parse import quote

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HikvisionDoorbellConfigEntry
from .api import ISAPIError
from .const import CONF_RTSP_PORT, DEFAULT_RTSP_PORT, KIND_INDOOR
from .entity import HikvisionDoorbellEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.kind != KIND_INDOOR:
        async_add_entities([DoorbellCamera(coordinator, int(entry.options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)))])


class DoorbellCamera(HikvisionDoorbellEntity, Camera):
    _attr_name = None  # use the device name
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator, rtsp_port: int) -> None:
        HikvisionDoorbellEntity.__init__(self, coordinator, "camera")
        Camera.__init__(self)
        self._rtsp_port = rtsp_port

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        try:
            return await self.coordinator.api.snapshot()
        except ISAPIError:
            return self.coordinator.last_snapshot

    async def stream_source(self) -> str | None:
        api = self.coordinator.api
        credentials = f"{quote(api.username, safe='')}:{quote(api.password, safe='')}"
        host = f"[{api.host}]" if ":" in api.host else api.host
        return f"rtsp://{credentials}@{host}:{self._rtsp_port}/Streaming/Channels/101"
