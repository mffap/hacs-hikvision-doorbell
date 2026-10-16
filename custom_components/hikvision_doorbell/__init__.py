"""Hikvision Doorbell integration for Home Assistant.

A native rewrite of the doorbell part of pergolafabio/Hikvision-Addons:
instead of an add-on using the Hikvision SDK and MQTT discovery, it talks to
the device over ISAPI (HTTP) and creates Home Assistant entities directly.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import HikvisionISAPI, ISAPIAuthError, ISAPIConnectionError, ISAPIError
from .const import (
    AUTO,
    CONF_COM_COUNT,
    CONF_DEVICE_KIND,
    CONF_DOOR_COUNT,
    CONF_VERIFY_SSL,
    DOMAIN,
    KIND_INDOOR,
    KIND_OUTDOOR,
)
from .coordinator import HikvisionDoorbellCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HikvisionDoorbellConfigEntry = ConfigEntry[HikvisionDoorbellCoordinator]

SERVICE_ISAPI_REQUEST = "isapi_request"
ATTR_DEVICE_ID = "device_id"
ATTR_METHOD = "method"
ATTR_PATH = "path"
ATTR_BODY = "body"

ISAPI_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_METHOD, default="GET"): vol.In(["GET", "PUT", "POST", "DELETE"]),
        vol.Required(ATTR_PATH): vol.All(cv.string, vol.Match(r"^/ISAPI/")),
        vol.Optional(ATTR_BODY): vol.Any(cv.string, dict),
    }
)


def create_api(hass: HomeAssistant, data: dict) -> HikvisionISAPI:
    """Build an API client from config entry data."""
    return HikvisionISAPI(
        async_create_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, False)),
        host=data[CONF_HOST],
        port=data[CONF_PORT],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        ssl=data.get(CONF_SSL, False),
        verify_ssl=data.get(CONF_VERIFY_SSL, False),
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-wide services."""

    async def isapi_request(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for_device(hass, call.data[ATTR_DEVICE_ID])
        try:
            response = await coordinator.api.request(
                call.data[ATTR_METHOD],
                call.data[ATTR_PATH],
                call.data.get(ATTR_BODY),
                raise_on_error=False,
            )
        except ISAPIError as err:
            raise HomeAssistantError(str(err)) from err
        return {
            "status": response.status,
            "content_type": response.content_type,
            "data": response.data if "image" not in response.content_type else None,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_ISAPI_REQUEST,
        isapi_request,
        schema=ISAPI_REQUEST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _coordinator_for_device(hass: HomeAssistant, device_id: str) -> HikvisionDoorbellCoordinator:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device {device_id}")
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN and hasattr(entry, "runtime_data") and entry.runtime_data:
            return entry.runtime_data
    raise ServiceValidationError(f"Device {device_id} is not a loaded Hikvision doorbell")


async def async_setup_entry(hass: HomeAssistant, entry: HikvisionDoorbellConfigEntry) -> bool:
    """Set up a doorbell from a config entry."""
    api = create_api(hass, entry.data)
    kind = entry.data.get(CONF_DEVICE_KIND, KIND_OUTDOOR)

    try:
        device = await api.get_device_info()
        door_count = int(entry.options.get(CONF_DOOR_COUNT, AUTO))
        if door_count == AUTO:
            door_count = await api.detect_door_count(device, kind == KIND_INDOOR)
        com_count = int(entry.options.get(CONF_COM_COUNT, AUTO))
        if com_count == AUTO:
            com_count = (device.alarm_out_num or 0) if kind == KIND_INDOOR else 0
    except ISAPIAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (ISAPIConnectionError, ISAPIError) as err:
        raise ConfigEntryNotReady(str(err)) from err

    _LOGGER.debug(
        "Setting up %s (%s, %s): %d door(s), %d com output(s)",
        device.name, device.model, kind, door_count, com_count,
    )

    coordinator = HikvisionDoorbellCoordinator(hass, entry, api, device, kind, door_count, com_count)
    await coordinator.async_config_entry_first_refresh()

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device.serial)},
        connections={(dr.CONNECTION_NETWORK_MAC, device.mac)} if device.mac else set(),
        manufacturer="Hikvision",
        name=entry.title or device.name,
        model=device.model,
        serial_number=device.serial,
        sw_version=device.firmware,
        configuration_url=api.base_url,
    )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_event_stream()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HikvisionDoorbellConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
