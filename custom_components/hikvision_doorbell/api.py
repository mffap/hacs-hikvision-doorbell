"""Async ISAPI client for Hikvision video intercoms.

Replaces the proprietary HCNetSDK (native .so libraries) used by the add-on
with plain HTTP calls, so the integration runs inside Home Assistant itself
on every architecture.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

import aiohttp

from .isapi_util import (
    DigestAuth,
    MultipartStreamParser,
    boundary_from_content_type,
    find_xml,
    isapi_status_error,
    normalize_event,
    parse_response_body,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
STREAM_READ_TIMEOUT = 120  # devices send keep-alives every few seconds


class ISAPIError(Exception):
    """Generic ISAPI failure."""


class ISAPIAuthError(ISAPIError):
    """Credentials rejected (or the account is locked)."""


class ISAPIConnectionError(ISAPIError):
    """Device unreachable."""


class ISAPINotSupported(ISAPIError):
    """The endpoint is not available on this device/firmware."""


@dataclass
class ISAPIResponse:
    status: int
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def data(self) -> Any:
        return parse_response_body(self.content_type, self.body)


@dataclass
class DeviceInfo:
    name: str
    model: str
    serial: str
    firmware: str | None
    mac: str | None
    device_type: str | None
    electro_lock_num: int | None
    alarm_out_num: int | None


class HikvisionISAPI:
    """Minimal async ISAPI client with Digest authentication."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        ssl: bool = False,
        verify_ssl: bool = False,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._scheme = "https" if ssl else "http"
        # Intercoms ship with self-signed certificates, so verification is opt-in
        self._ssl: bool = verify_ssl if ssl else True
        self._digest = DigestAuth(username, password)
        self._use_basic = False
        # Some firmwares reject a digest nonce once it was used; we then answer
        # a fresh challenge for every request instead of reusing the last one.
        self._reuse_nonce = True
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        default = 443 if self._scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port == default else f":{self.port}"
        return f"{self._scheme}://{host}{port}"

    # ------------------------------------------------------------------ core

    def _auth_headers(self, method: str, path: str, fresh: bool) -> dict[str, str]:
        if self._use_basic:
            return {"Authorization": aiohttp.BasicAuth(self.username, self.password).encode()}
        if self._digest.has_challenge and (fresh or self._reuse_nonce):
            return {"Authorization": self._digest.header(method, path)}
        return {}

    def _handle_challenge(self, response: aiohttp.ClientResponse) -> bool:
        """Learn the auth scheme from a 401. Return True if a retry makes sense."""
        for header in response.headers.getall("WWW-Authenticate", []):
            if self._digest.set_challenge(header):
                self._use_basic = False
                return True
        for header in response.headers.getall("WWW-Authenticate", []):
            if header.lower().startswith("basic"):
                self._use_basic = True
                return True
        return False

    async def _open(
        self,
        method: str,
        path: str,
        body: bytes | None,
        content_type: str | None,
        timeout: aiohttp.ClientTimeout,
    ) -> aiohttp.ClientResponse:
        """Send a request, answering the auth challenge. Caller must release()."""
        url = self.base_url + path
        detail = ""
        fresh = False  # holding a challenge received during this call
        reuse_rejected = False
        for attempt in range(3):
            headers = self._auth_headers(method, path, fresh)
            if content_type:
                headers["Content-Type"] = content_type
            try:
                response = await self._session.request(
                    method, url, data=body, headers=headers, timeout=timeout, ssl=self._ssl
                )
            except asyncio.TimeoutError as err:
                raise ISAPIConnectionError(f"Timeout talking to {self.host}") from err
            except aiohttp.ClientError as err:
                raise ISAPIConnectionError(f"Cannot connect to {self.host}: {err}") from err

            had_auth = "Authorization" in headers
            if response.status != 401:
                if reuse_rejected and self._reuse_nonce:
                    _LOGGER.debug("%s rejects reused nonces, answering a fresh challenge per request", self.host)
                    self._reuse_nonce = False
                return response
            retry = self._handle_challenge(response)
            try:
                detail = (await response.read())[:500].decode("utf-8", errors="replace").strip()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                detail = ""
            finally:
                response.release()
            _LOGGER.debug(
                "401 from %s %s (attempt %d, sent auth: %s, challenge: %s): %s",
                method, path, attempt + 1, had_auth,
                response.headers.getall("WWW-Authenticate", []), detail,
            )
            # Credentials computed from a challenge we just received are only
            # rejected when they are wrong; a reused nonce may simply be stale.
            if not retry or (had_auth and fresh):
                break
            if had_auth:
                reuse_rejected = True
            fresh = True
        # Start the next request from scratch: retrying with the rejected
        # challenge would cost an extra failed login on the device.
        self._digest.reset()
        message = "Authentication failed, check username and password"
        if "lock" in detail.lower():
            message = f"Device rejected the login (account or IP may be locked): {detail}"
        raise ISAPIAuthError(f"{message} [{method} {path}]")

    async def request(
        self,
        method: str,
        path: str,
        body: str | bytes | dict | None = None,
        content_type: str | None = None,
        timeout: float = REQUEST_TIMEOUT,
        raise_on_error: bool = True,
    ) -> ISAPIResponse:
        """Perform an ISAPI request and return the full response."""
        if not path.startswith("/"):
            path = "/" + path
        raw: bytes | None
        if isinstance(body, dict):
            raw = json.dumps(body).encode()
            content_type = content_type or "application/json"
        elif isinstance(body, str):
            raw = body.encode()
            if content_type is None:
                content_type = "application/json" if body.lstrip()[:1] in "{[" else "application/xml"
        else:
            raw = body
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with self._lock:
            response = await self._open(method.upper(), path, raw, content_type, client_timeout)
        try:
            payload = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ISAPIConnectionError(f"Error reading response from {self.host}: {err}") from err
        finally:
            response.release()

        result = ISAPIResponse(
            status=response.status,
            content_type=response.headers.get("Content-Type", "").lower(),
            body=payload,
        )
        if not raise_on_error:
            return result
        if response.status == 404:
            raise ISAPINotSupported(f"{method} {path} not supported by the device")
        error = isapi_status_error(result.content_type, payload)
        if response.status >= 400 or error:
            message = error or f"HTTP {response.status}"
            if error and ("notsupport" in error.lower() or "invalid operation" in error.lower()):
                raise ISAPINotSupported(f"{method} {path}: {message}")
            raise ISAPIError(f"{method} {path} failed: {message}")
        return result

    # ------------------------------------------------------------ device info

    async def get_device_info(self) -> DeviceInfo:
        response = await self.request("GET", "/ISAPI/System/deviceInfo")
        data = response.data or {}
        info = data.get("DeviceInfo", data) if isinstance(data, dict) else {}

        def as_int(key: str) -> int | None:
            try:
                return int(info.get(key))
            except (TypeError, ValueError):
                return None

        serial = info.get("serialNumber") or info.get("macAddress") or self.host
        return DeviceInfo(
            name=info.get("deviceName") or "Hikvision intercom",
            model=info.get("model") or "Unknown",
            serial=serial,
            firmware=" ".join(
                part for part in (info.get("firmwareVersion"), info.get("firmwareReleasedDate")) if part
            )
            or None,
            mac=info.get("macAddress"),
            device_type=info.get("deviceType"),
            electro_lock_num=as_int("electroLockNum"),
            alarm_out_num=as_int("alarmOutNum"),
        )

    async def detect_door_count(self, info: DeviceInfo, indoor: bool) -> int:
        """Best effort detection of the number of lock relays (like the add-on)."""
        try:
            response = await self.request("GET", "/ISAPI/AccessControl/RemoteControl/door/capabilities")
            for name in ("doorNo", "channelNo", "id"):
                element = find_xml(response.body, name)
                if element is None:
                    continue
                if "max" in element.attrib:
                    return max(0, int(element.attrib["max"]))
                if element.text and element.text.strip().isdigit():
                    return int(element.text.strip())
        except (ISAPIError, ValueError, SyntaxError) as err:
            _LOGGER.debug("Door capabilities not available: %s", err)
        if not indoor:
            if info.electro_lock_num is not None:
                return info.electro_lock_num
            try:
                response = await self.request("GET", "/ISAPI/System/IO/outputs")
                data = response.data
                ports = (data or {}).get("IOOutputPortList", {}).get("IOOutputPort", [])
                count = len(ports) if isinstance(ports, list) else 1
                if count:
                    return count
            except (ISAPIError, AttributeError) as err:
                _LOGGER.debug("IO outputs not available: %s", err)
        return 1

    # --------------------------------------------------------------- commands

    async def get_call_status(self) -> str:
        response = await self.request("GET", "/ISAPI/VideoIntercom/callStatus?format=json")
        data = response.data
        if isinstance(data, dict):
            status = data.get("CallStatus", data).get("status")
            if status:
                return str(status)
        raise ISAPIError(f"Unexpected call status response: {response.text[:200]}")

    async def call_signal(self, command: str) -> None:
        """answer / reject / hangUp / bellTimeout / deviceOnCall."""
        await self.request(
            "PUT", "/ISAPI/VideoIntercom/callSignal?format=json", {"CallSignal": {"cmdType": command}}
        )

    async def open_door(self, door: int, indoor: bool) -> None:
        """Open lock `door` (1-based)."""
        path = f"/ISAPI/AccessControl/RemoteControl/door/{door}"
        if indoor:
            body = (
                "<RemoteControlDoor><channelNo>1</channelNo><cmd>open</cmd>"
                "<controlType>monitor</controlType></RemoteControlDoor>"
            )
        else:
            body = "<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>"
        try:
            await self.request("PUT", path, body)
        except ISAPINotSupported:
            if indoor:
                raise
            # Older door stations: pulse the IO output instead
            _LOGGER.debug("RemoteControl/door not supported, falling back to IO output trigger")
            await self.request(
                "PUT",
                f"/ISAPI/System/IO/outputs/{door}/trigger",
                "<IOPortData><outputState>high</outputState></IOPortData>",
            )

    async def set_com_output(self, output: int, on: bool) -> None:
        """Switch a COM/alarm output of an indoor station (0-based like the add-on)."""
        await self.request(
            "PUT",
            f"/ISAPI/SecurityCP/control/outputs/{output}?format=json",
            {"OutputsCtrl": {"switch": "open" if on else "close"}},
        )

    async def reboot(self) -> None:
        await self.request("PUT", "/ISAPI/System/reboot")

    async def get_audio_out(self) -> dict[str, Any] | None:
        response = await self.request("GET", "/ISAPI/System/Audio/AudioOut/channels/1")
        data = response.data
        return data if isinstance(data, dict) else None

    async def set_output_volume(self, volume: int, talk_volume: int | None = None) -> None:
        """Set the speaker volume (0-10). The add-on's mute = volume 0."""
        if talk_volume is None:
            talk_volume = 7
            current = await self.get_audio_out()
            try:
                entry = current["AudioOut"]["AudioOutVolumelist"]["AudioOutVlome"]
                if isinstance(entry, list):
                    entry = entry[0]
                talk_volume = int(entry.get("talkVolume", talk_volume))
            except (KeyError, TypeError, ValueError):
                pass
        body = (
            "<AudioOut><id>1</id><AudioOutVolumelist><AudioOutVlome>"
            f"<type>audioOutput</type><volume>{int(volume)}</volume>"
            f"<talkVolume>{int(talk_volume)}</talkVolume>"
            "</AudioOutVlome></AudioOutVolumelist></AudioOut>"
        )
        await self.request("PUT", "/ISAPI/System/Audio/AudioOut/channels/1", body)

    async def get_scene(self) -> str | None:
        response = await self.request("GET", "/ISAPI/VideoIntercom/scene/nowMode")
        element = find_xml(response.body, "nowMode")
        return element.text.strip() if element is not None and element.text else None

    async def set_scene(self, scene: str) -> None:
        await self.request(
            "PUT",
            "/ISAPI/VideoIntercom/scene/nowMode",
            f"<SceneNowMode><nowMode>{escape(scene)}</nowMode></SceneNowMode>",
        )

    async def get_alarm_armed(self) -> bool | None:
        response = await self.request("GET", "/ISAPI/SecurityCP/AlarmControlByPhone")
        element = find_xml(response.body, "commandType")
        if element is None or not element.text:
            return None
        return element.text.strip() == "setupAlarm"

    async def set_alarm(self, command: str) -> None:
        await self.request(
            "PUT",
            "/ISAPI/SecurityCP/AlarmControlByPhone",
            f"<AlarmControlByPhoneCfg><commandType>{escape(command)}</commandType></AlarmControlByPhoneCfg>",
        )

    async def snapshot(self, channel: int = 101) -> bytes:
        response = await self.request(
            "GET", f"/ISAPI/Streaming/channels/{channel}/picture", timeout=20
        )
        if "image" not in response.content_type and not response.body.startswith(b"\xff\xd8"):
            raise ISAPIError(f"Snapshot did not return an image: {response.text[:200]}")
        return response.body

    # ------------------------------------------------------------ event stream

    async def listen_events(
        self,
        on_event: Callable[[dict[str, Any]], Awaitable[None] | None],
        on_connected: Callable[[bool], None] | None = None,
    ) -> None:
        """Consume /ISAPI/Event/notification/alertStream until it ends or fails.

        Raises ISAPIError subclasses on failure; the caller handles reconnects.
        """
        path = "/ISAPI/Event/notification/alertStream"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=REQUEST_TIMEOUT, sock_read=STREAM_READ_TIMEOUT)
        async with self._lock:
            response = await self._open("GET", path, None, None, timeout)
        try:
            if response.status == 404:
                raise ISAPINotSupported("Event stream not supported by this device")
            if response.status >= 400:
                raise ISAPIError(f"Event stream failed with HTTP {response.status}")
            boundary = boundary_from_content_type(response.headers.get("Content-Type", ""))
            parser = MultipartStreamParser(boundary=boundary)
            _LOGGER.debug("Connected to event stream of %s (boundary=%s)", self.host, boundary)
            if on_connected:
                on_connected(True)
            while True:
                try:
                    chunk = await response.content.readany()
                except asyncio.TimeoutError as err:
                    raise ISAPIConnectionError("Event stream timed out") from err
                except aiohttp.ClientError as err:
                    raise ISAPIConnectionError(f"Event stream error: {err}") from err
                if not chunk:
                    raise ISAPIConnectionError("Event stream closed by the device")
                for part in parser.feed(chunk):
                    event = normalize_event(part)
                    if event is None:
                        continue
                    result = on_event(event)
                    if asyncio.iscoroutine(result):
                        await result
        finally:
            response.release()
            if on_connected:
                on_connected(False)
