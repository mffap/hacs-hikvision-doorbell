"""Coordinator: polls call status and consumes the device event stream."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DeviceInfo,
    HikvisionISAPI,
    ISAPIAuthError,
    ISAPIConnectionError,
    ISAPIError,
    ISAPINotSupported,
)
from .const import (
    ACS_EVENT_OTHER,
    ACS_EVENT_TYPES,
    ACS_MAJOR_EVENT,
    ACS_MINOR_DOORBELL_RINGING,
    CALL_IDLE,
    CALL_RINGING,
    CALL_STATUS_MAP,
    CALL_UNKNOWN,
    CONF_CALL_STATUS_INTERVAL,
    CONF_ENABLE_ALARM,
    CONF_ENABLE_SCENES,
    CONF_RING_RESET,
    DEFAULT_CALL_STATUS_INTERVAL,
    DEFAULT_RING_RESET,
    DEVICE_EVENT_OTHER,
    DEVICE_EVENT_TYPES,
    DOMAIN,
    EVENT_DEVICE_EVENT,
    IGNORED_EVENT_TYPES,
    KIND_INDOOR,
)

if TYPE_CHECKING:
    from . import HikvisionDoorbellConfigEntry

_LOGGER = logging.getLogger(__name__)

SLOW_POLL_SECONDS = 15  # scenes / alarm state, like the add-on default
STREAM_BACKOFF = (5, 10, 30, 60)
# Hikvision occasionally answers 401 for valid credentials (nonce rotation,
# session limits), so only ask for new credentials when it keeps happening.
AUTH_FAILURE_THRESHOLD = 3
AUTH_RETRY_SECONDS = 30  # slow down while failing so the device does not lock us out


@dataclass
class DoorbellState:
    call_state: str = CALL_UNKNOWN
    raw_call_status: str | None = None
    scene: str | None = None
    alarm_armed: bool | None = None
    stream_connected: bool = False
    last_ring: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def signal_name(entry_id: str, category: str) -> str:
    return f"{DOMAIN}_{entry_id}_{category}"


class HikvisionDoorbellCoordinator(DataUpdateCoordinator[DoorbellState]):
    """Keeps the doorbell state up to date."""

    config_entry: HikvisionDoorbellConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HikvisionDoorbellConfigEntry,
        api: HikvisionISAPI,
        device: DeviceInfo,
        kind: str,
        door_count: int,
        com_count: int,
    ) -> None:
        self.api = api
        self.device = device
        self.kind = kind
        self.door_count = door_count
        self.com_count = com_count
        options = entry.options
        self.poll_interval = int(options.get(CONF_CALL_STATUS_INTERVAL, DEFAULT_CALL_STATUS_INTERVAL))
        self.ring_reset = int(options.get(CONF_RING_RESET, DEFAULT_RING_RESET))
        self.scenes_enabled = kind == KIND_INDOOR and bool(options.get(CONF_ENABLE_SCENES, False))
        self.alarm_enabled = kind == KIND_INDOOR and bool(options.get(CONF_ENABLE_ALARM, False))
        self.call_status_supported = self.poll_interval > 0
        self._last_slow_poll = 0.0
        self._ring_reset_unsub: CALLBACK_TYPE | None = None
        self._stream_task: asyncio.Task | None = None
        self._stream_supported = True
        self._auth_failures = 0
        self.last_snapshot: bytes | None = None
        self.last_snapshot_time = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device.name}",
            update_interval=timedelta(
                seconds=self.poll_interval if self.poll_interval > 0 else SLOW_POLL_SECONDS
            ),
            always_update=False,
        )
        self.data = DoorbellState()

    @property
    def is_indoor(self) -> bool:
        return self.kind == KIND_INDOOR

    # ---------------------------------------------------------------- polling

    async def _async_update_data(self) -> DoorbellState:
        state = replace(self.data) if self.data else DoorbellState()
        try:
            if self.call_status_supported:
                try:
                    raw = await self.api.get_call_status()
                except ISAPINotSupported:
                    _LOGGER.info(
                        "%s does not report call status, relying on the event stream only",
                        self.device.name,
                    )
                    self.call_status_supported = False
                    self.update_interval = timedelta(seconds=SLOW_POLL_SECONDS)
                else:
                    state.raw_call_status = raw
                    new = CALL_STATUS_MAP.get(raw.replace(" ", "").lower(), CALL_UNKNOWN)
                    if new == CALL_UNKNOWN:
                        _LOGGER.debug("Unknown call status %r from %s", raw, self.device.name)
                    self._apply_call_state(state, new)
            elif state.call_state == CALL_UNKNOWN:
                state.call_state = CALL_IDLE

            now = time.monotonic()
            if (self.scenes_enabled or self.alarm_enabled) and now - self._last_slow_poll >= SLOW_POLL_SECONDS:
                self._last_slow_poll = now
                if self.scenes_enabled:
                    try:
                        state.scene = await self.api.get_scene()
                    except ISAPINotSupported:
                        self.scenes_enabled = False
                if self.alarm_enabled:
                    try:
                        state.alarm_armed = await self.api.get_alarm_armed()
                    except ISAPINotSupported:
                        self.alarm_enabled = False
        except ISAPIAuthError as err:
            self._auth_failures += 1
            if self._auth_failures >= AUTH_FAILURE_THRESHOLD:
                raise ConfigEntryAuthFailed(str(err)) from err
            _LOGGER.warning(
                "%s rejected the credentials (%d/%d), retrying in %ds: %s",
                self.device.name, self._auth_failures, AUTH_FAILURE_THRESHOLD, AUTH_RETRY_SECONDS, err,
            )
            self.update_interval = timedelta(seconds=AUTH_RETRY_SECONDS)
            raise UpdateFailed(str(err)) from err
        except ISAPIConnectionError as err:
            raise UpdateFailed(str(err)) from err
        except ISAPIError as err:
            raise UpdateFailed(str(err)) from err
        if self._auth_failures:
            self._auth_failures = 0
            self.update_interval = self._normal_update_interval
        return state

    @property
    def _normal_update_interval(self) -> timedelta:
        if self.call_status_supported:
            return timedelta(seconds=self.poll_interval)
        return timedelta(seconds=SLOW_POLL_SECONDS)

    # ---------------------------------------------------------- call handling

    def _apply_call_state(self, state: DoorbellState, new: str, source: dict[str, Any] | None = None) -> None:
        """Update `state` in place and fire the ring event on a new ring."""
        previous = state.call_state
        state.call_state = new
        if new == CALL_RINGING and previous != CALL_RINGING:
            state.last_ring = dt_util.utcnow()
            self.hass.loop.call_soon(self._on_ring, source or {})

    @callback
    def _on_ring(self, source: dict[str, Any]) -> None:
        _LOGGER.debug("%s is ringing", self.device.name)
        payload = {"source": "event_stream" if source else "call_status", **_public(source)}
        async_dispatcher_send(self.hass, signal_name(self.config_entry.entry_id, "ring"), payload)
        self.hass.bus.async_fire(
            EVENT_DEVICE_EVENT,
            {**payload, "device_id": self.device_id, "type": "ring", "device_name": self.device.name},
        )
        if not self.is_indoor:
            self.config_entry.async_create_background_task(
                self.hass, self._async_capture_ring_snapshot(), f"{DOMAIN} ring snapshot"
            )
        if not self.call_status_supported:
            # Without polling nobody tells us when the call ends
            if self._ring_reset_unsub:
                self._ring_reset_unsub()
            self._ring_reset_unsub = async_call_later(self.hass, self.ring_reset, self._reset_ring)

    @callback
    def _reset_ring(self, _now: Any) -> None:
        self._ring_reset_unsub = None
        if self.data.call_state == CALL_RINGING:
            self.async_set_updated_data(replace(self.data, call_state=CALL_IDLE))

    async def _async_capture_ring_snapshot(self) -> None:
        await self.async_capture_snapshot(raise_errors=False)

    async def async_capture_snapshot(self, raise_errors: bool = False) -> None:
        """Store a fresh JPEG for the "Last snapshot" image entity."""
        try:
            image = await self.api.snapshot()
        except ISAPIError as err:
            if raise_errors:
                raise HomeAssistantError(f"Could not capture snapshot: {err}") from err
            _LOGGER.debug("Could not capture snapshot: %s", err)
            return
        self.last_snapshot = image
        self.last_snapshot_time = dt_util.utcnow()
        async_dispatcher_send(self.hass, signal_name(self.config_entry.entry_id, "snapshot"))

    @property
    def device_id(self) -> str | None:
        device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, self.device.serial)})
        return device.id if device else None

    # ------------------------------------------------------------ event stream

    @callback
    def async_start_event_stream(self) -> None:
        self._stream_task = self.config_entry.async_create_background_task(
            self.hass, self._stream_loop(), f"{DOMAIN} event stream {self.device.name}"
        )

    async def async_shutdown(self) -> None:
        if self._ring_reset_unsub:
            self._ring_reset_unsub()
            self._ring_reset_unsub = None
        if self._stream_task:
            self._stream_task.cancel()
            self._stream_task = None
        await super().async_shutdown()

    async def _stream_loop(self) -> None:
        failures = 0
        while self._stream_supported:
            try:
                await self.api.listen_events(self._handle_event, self._set_stream_connected)
                failures = 0
            except ISAPINotSupported:
                _LOGGER.warning(
                    "%s does not provide an event stream; only polled data will be available",
                    self.device.name,
                )
                self._stream_supported = False
                return
            except ISAPIAuthError as err:
                failures = len(STREAM_BACKOFF)
                if self.call_status_supported and self.last_update_success:
                    # Polling with the same credentials works, so they are not
                    # wrong: the user probably lacks the event/notify permission.
                    _LOGGER.warning(
                        "Event stream of %s was refused (%s) although polling works; "
                        "check that the user may receive event notifications",
                        self.device.name, err,
                    )
                else:
                    self._auth_failures += 1
                    _LOGGER.warning(
                        "Event stream of %s rejected the credentials (%d/%d): %s",
                        self.device.name, self._auth_failures, AUTH_FAILURE_THRESHOLD, err,
                    )
                    if self._auth_failures >= AUTH_FAILURE_THRESHOLD:
                        self.config_entry.async_start_reauth(self.hass)
                        return
            except ISAPIError as err:
                failures += 1
                _LOGGER.debug("Event stream of %s dropped: %s", self.device.name, err)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive no matter what
                failures += 1
                _LOGGER.exception("Unexpected error in event stream of %s", self.device.name)
            delay = STREAM_BACKOFF[min(failures, len(STREAM_BACKOFF)) - 1] if failures else 1
            await asyncio.sleep(delay)

    @callback
    def _set_stream_connected(self, connected: bool) -> None:
        if connected and not self.call_status_supported:
            self._auth_failures = 0
        if self.data.stream_connected != connected:
            self.async_set_updated_data(replace(self.data, stream_connected=connected))

    @callback
    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event["event_type"]
        if event_type in IGNORED_EVENT_TYPES:
            return
        if event.get("event_state") == "inactive":
            return
        _LOGGER.debug("Event from %s: %s", self.device.name, _public(event))

        if event_type == "accesscontrollerevent":
            major, minor = event.get("major"), event.get("minor")
            if major == ACS_MAJOR_EVENT and minor == ACS_MINOR_DOORBELL_RINGING:
                state = replace(self.data)
                self._apply_call_state(state, CALL_RINGING, event)
                self.async_set_updated_data(state)
                return
            kind = ACS_EVENT_TYPES.get(minor) if major == ACS_MAJOR_EVENT else None
            category, type_ = "access_control", kind or ACS_EVENT_OTHER
        else:
            category = "device"
            type_ = DEVICE_EVENT_TYPES.get(event_type, DEVICE_EVENT_OTHER)

        payload = _public(event)
        async_dispatcher_send(
            self.hass, signal_name(self.config_entry.entry_id, category), type_, payload
        )
        self.hass.bus.async_fire(
            EVENT_DEVICE_EVENT,
            {**payload, "device_id": self.device_id, "type": type_, "device_name": self.device.name},
        )
        if self.call_status_supported:
            # Anything happening on an intercom may change the call state
            self.hass.async_create_task(self.async_request_refresh())


def _public(event: dict[str, Any]) -> dict[str, Any]:
    """Event attributes safe for the bus/state machine (no None values)."""
    return {key: value for key, value in event.items() if value is not None and key != "event_type_raw"}
