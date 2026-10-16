"""Config flow for the Hikvision Doorbell integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import HikvisionDoorbellConfigEntry, create_api
from .api import DeviceInfo, ISAPIAuthError, ISAPIConnectionError, ISAPIError
from .const import (
    AUTO,
    CONF_CALL_STATUS_INTERVAL,
    CONF_COM_COUNT,
    CONF_DEVICE_KIND,
    CONF_DOOR_COUNT,
    CONF_ENABLE_ALARM,
    CONF_ENABLE_SCENES,
    CONF_RING_RESET,
    CONF_RTSP_PORT,
    CONF_VERIFY_SSL,
    DEFAULT_CALL_STATUS_INTERVAL,
    DEFAULT_HTTP_PORT,
    DEFAULT_RING_RESET,
    DEFAULT_RTSP_PORT,
    DEVICE_KINDS,
    DOMAIN,
    KIND_ACCESS_CONTROL,
    KIND_INDOOR,
    KIND_OUTDOOR,
)

_LOGGER = logging.getLogger(__name__)


def guess_kind(info: DeviceInfo) -> str:
    """Guess the device kind from the model name (DS-KH = indoor, DS-K1T = terminal)."""
    model = (info.model or "").upper().replace(" ", "")
    device_type = (info.device_type or "").lower()
    if model.startswith("DS-KH") or "indoor" in device_type:
        return KIND_INDOOR
    if model.startswith("DS-K1T") or "accesscontrol" in device_type.replace(" ", ""):
        return KIND_ACCESS_CONTROL
    return KIND_OUTDOOR


def _connection_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_HTTP_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")): str,
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): str,
            vol.Required(CONF_SSL, default=defaults.get(CONF_SSL, False)): bool,
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, False)): bool,
        }
    )


class HikvisionDoorbellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._info: DeviceInfo | None = None

    async def _validate(self, data: dict[str, Any]) -> tuple[DeviceInfo | None, dict[str, str]]:
        api = create_api(self.hass, data)
        try:
            return await api.get_device_info(), {}
        except ISAPIAuthError:
            return None, {"base": "invalid_auth"}
        except ISAPIConnectionError:
            return None, {"base": "cannot_connect"}
        except ISAPIError as err:
            _LOGGER.warning("Unexpected ISAPI response while setting up: %s", err)
            return None, {"base": "unknown"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            info, errors = await self._validate(user_input)
            if info:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                self._data = user_input
                self._info = info
                return await self.async_step_device()
        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._info
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get("name") or self._info.name,
                data={**self._data, CONF_DEVICE_KIND: user_input[CONF_DEVICE_KIND]},
                options={
                    CONF_ENABLE_SCENES: user_input[CONF_DEVICE_KIND] == KIND_INDOOR,
                    CONF_ENABLE_ALARM: False,
                },
            )
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=self._info.name): str,
                    vol.Required(CONF_DEVICE_KIND, default=guess_kind(self._info)): SelectSelector(
                        SelectSelectorConfig(
                            options=DEVICE_KINDS,
                            translation_key=CONF_DEVICE_KIND,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={"model": self._info.model, "serial": self._info.serial},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            info, errors = await self._validate(data)
            if info:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            info, errors = await self._validate(data)
            if info:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(user_input or entry.data),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: HikvisionDoorbellConfigEntry) -> OptionsFlow:
        return HikvisionDoorbellOptionsFlow()


def _number(minimum: int, maximum: int, unit: str | None = None) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement=unit
        )
    )


class HikvisionDoorbellOptionsFlow(OptionsFlowWithReload):
    """Tune polling and entities."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={key: int(value) if isinstance(value, float) else value for key, value in user_input.items()}
            )

        options = self.config_entry.options
        indoor = self.config_entry.data.get(CONF_DEVICE_KIND) == KIND_INDOOR
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_CALL_STATUS_INTERVAL,
                default=options.get(CONF_CALL_STATUS_INTERVAL, DEFAULT_CALL_STATUS_INTERVAL),
            ): _number(0, 60, "s"),
            vol.Required(
                CONF_RING_RESET, default=options.get(CONF_RING_RESET, DEFAULT_RING_RESET)
            ): _number(5, 600, "s"),
            vol.Required(CONF_DOOR_COUNT, default=options.get(CONF_DOOR_COUNT, AUTO)): _number(AUTO, 8),
        }
        if indoor:
            schema[vol.Required(CONF_COM_COUNT, default=options.get(CONF_COM_COUNT, AUTO))] = _number(AUTO, 8)
            schema[vol.Required(CONF_ENABLE_SCENES, default=options.get(CONF_ENABLE_SCENES, True))] = bool
            schema[vol.Required(CONF_ENABLE_ALARM, default=options.get(CONF_ENABLE_ALARM, False))] = bool
        else:
            schema[
                vol.Required(CONF_RTSP_PORT, default=options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT))
            ] = _number(1, 65535)
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
