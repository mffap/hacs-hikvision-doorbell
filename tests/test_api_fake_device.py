"""Run the ISAPI client against a fake Hikvision intercom (needs aiohttp only).

Exercises Digest auth, error mapping, door detection/commands and the
multipart alert stream end to end over real HTTP.
"""
import asyncio
import hashlib
import json
import pathlib
import sys
import types

from aiohttp import ClientSession, web

PKG_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "hikvision_doorbell"
# Load api.py/isapi_util.py without executing the HA-dependent package __init__
pkg = types.ModuleType("hd")
pkg.__path__ = [str(PKG_DIR)]
sys.modules["hd"] = pkg
from hd.api import HikvisionISAPI, ISAPIAuthError, ISAPINotSupported  # noqa: E402

USER, PASSWORD, REALM, NONCE = "admin", "s3cret!", "DS-KV6113", "abc123nonce"
NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class FakeDevice:
    def __init__(self) -> None:
        self.call_status = "idle"
        self.requests: list[tuple[str, str, str]] = []
        self.auth_failures = 0
        self.stream_events: asyncio.Queue[bytes] = asyncio.Queue()

    def check_auth(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return False
        params = dict(
            (k.strip(), v.strip().strip('"'))
            for k, _, v in (p.partition("=") for p in header[7:].split(","))
        )
        ha1 = md5(f"{params['username']}:{REALM}:{PASSWORD}")
        ha2 = md5(f"{request.method}:{params['uri']}")
        expected = md5(f"{ha1}:{NONCE}:{params['nc']}:{params['cnonce']}:auth:{ha2}")
        ok = params["username"] == USER and params["response"] == expected and params["uri"] == request.path_qs
        if not ok:
            self.auth_failures += 1
        return ok

    @web.middleware
    async def auth(self, request: web.Request, handler):
        if not self.check_auth(request):
            return web.Response(
                status=401,
                headers={"WWW-Authenticate": f'Digest qop="auth", realm="{REALM}", nonce="{NONCE}", stale="FALSE"'},
            )
        body = await request.text()
        self.requests.append((request.method, request.path_qs, body))
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.auth])
        app.router.add_get("/ISAPI/System/deviceInfo", self.device_info)
        app.router.add_get("/ISAPI/VideoIntercom/callStatus", self.get_call_status)
        app.router.add_put("/ISAPI/VideoIntercom/callSignal", self.ok_json)
        app.router.add_get("/ISAPI/AccessControl/RemoteControl/door/capabilities", self.door_caps)
        app.router.add_put("/ISAPI/AccessControl/RemoteControl/door/{n}", self.ok_xml)
        app.router.add_get("/ISAPI/VideoIntercom/scene/nowMode", self.not_supported)
        app.router.add_get("/ISAPI/Streaming/channels/101/picture", self.picture)
        app.router.add_get("/ISAPI/Event/notification/alertStream", self.alert_stream)
        return app

    async def device_info(self, request):
        return web.Response(
            content_type="application/xml",
            text=f'<?xml version="1.0" encoding="UTF-8"?><DeviceInfo version="2.0" {NS}>'
            "<deviceName>Front door</deviceName><model>DS-KV6113-WPE1</model>"
            "<serialNumber>DS-KV6113-WPE120230101AAWRJ00000000</serialNumber>"
            "<macAddress>44:a6:42:00:00:01</macAddress><firmwareVersion>V2.2.53</firmwareVersion>"
            "<firmwareReleasedDate>build 230612</firmwareReleasedDate><deviceType>VIS</deviceType>"
            "<electroLockNum>2</electroLockNum></DeviceInfo>",
        )

    async def get_call_status(self, request):
        return web.json_response({"CallStatus": {"status": self.call_status}})

    async def ok_json(self, request):
        return web.json_response({"statusCode": 1, "statusString": "OK", "subStatusCode": "ok"})

    async def ok_xml(self, request):
        return web.Response(
            content_type="application/xml",
            text=f"<ResponseStatus {NS}><statusCode>1</statusCode><statusString>OK</statusString></ResponseStatus>",
        )

    async def not_supported(self, request):
        return web.Response(
            status=403,
            content_type="application/xml",
            text=f"<ResponseStatus {NS}><statusCode>4</statusCode><statusString>Invalid Operation</statusString>"
            "<subStatusCode>notSupport</subStatusCode></ResponseStatus>",
        )

    async def door_caps(self, request):
        return web.Response(
            content_type="application/xml",
            text=f'<RemoteControlDoor {NS}><doorNo min="1" max="2"/><cmd opt="open,close"/></RemoteControlDoor>',
        )

    async def picture(self, request):
        return web.Response(body=b"\xff\xd8\xff\xe0fakejpeg\xff\xd9", content_type="image/jpeg")

    async def alert_stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": 'multipart/mixed; boundary="boundary"'})
        await response.prepare(request)
        while True:
            body = await self.stream_events.get()
            if body == b"CLOSE":
                break
            ctype = b"application/json" if body.startswith(b"{") else b"application/xml"
            await response.write(
                b"--boundary\r\nContent-Type: " + ctype + b"; charset=\"UTF-8\"\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body + b"\r\n"
            )
        return response


async def main() -> None:
    device = FakeDevice()
    runner = web.AppRunner(device.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async with ClientSession() as session:
        # Wrong password: exactly one bad attempt reaches the device
        bad = HikvisionISAPI(session, "127.0.0.1", port, USER, "wrong")
        try:
            await bad.get_device_info()
            raise AssertionError("expected auth error")
        except ISAPIAuthError:
            pass
        assert device.auth_failures == 1, device.auth_failures

        api = HikvisionISAPI(session, "127.0.0.1", port, USER, PASSWORD)
        info = await api.get_device_info()
        assert info.model == "DS-KV6113-WPE1" and info.electro_lock_num == 2
        assert info.firmware == "V2.2.53 build 230612"
        assert await api.detect_door_count(info, indoor=False) == 2

        assert await api.get_call_status() == "idle"
        device.call_status = "ring"
        assert await api.get_call_status() == "ring"

        await api.call_signal("reject")
        assert device.requests[-1][2] == '{"CallSignal": {"cmdType": "reject"}}'
        await api.open_door(2, indoor=False)
        assert device.requests[-1][:2] == ("PUT", "/ISAPI/AccessControl/RemoteControl/door/2")
        assert "<cmd>open</cmd>" in device.requests[-1][2]

        try:
            await api.get_scene()
            raise AssertionError("expected not supported")
        except ISAPINotSupported:
            pass
        try:
            await api.request("GET", "/ISAPI/Does/Not/Exist")
            raise AssertionError("expected 404")
        except ISAPINotSupported:
            pass

        assert (await api.snapshot()).startswith(b"\xff\xd8")

        # Event stream
        received: list[dict] = []
        connected: list[bool] = []
        task = asyncio.create_task(api.listen_events(received.append, connected.append))
        await device.stream_events.put(
            f'<EventNotificationAlert version="2.0" {NS}><eventType>videoloss</eventType>'
            "<eventState>inactive</eventState></EventNotificationAlert>".encode()
        )
        await device.stream_events.put(
            json.dumps({"eventType": "AccessControllerEvent", "eventState": "active",
                        "AccessControllerEvent": {"majorEventType": 5, "subEventType": 37}}).encode()
        )
        await device.stream_events.put(
            json.dumps({"eventType": "AccessControllerEvent", "eventState": "active",
                        "AccessControllerEvent": {"majorEventType": 5, "subEventType": 21,
                                                  "doorNo": 1, "employeeNoString": "6",
                                                  "name": "Fabio"}}).encode()
        )
        # Requests keep working while the stream is open
        assert await api.get_call_status() == "ring"
        for _ in range(50):
            if len(received) == 3:
                break
            await asyncio.sleep(0.05)
        assert connected == [True], connected
        assert [e["event_type"] for e in received] == ["videoloss", "accesscontrollerevent", "accesscontrollerevent"]
        assert received[1]["minor"] == 0x25
        assert received[2]["door"] == 1 and received[2]["name"] == "Fabio"

        await device.stream_events.put(b"CLOSE")
        try:
            await asyncio.wait_for(task, 5)
            raise AssertionError("stream end should raise")
        except Exception as err:  # noqa: BLE001
            assert type(err).__name__ == "ISAPIConnectionError", repr(err)
        assert connected == [True, False]

    await runner.cleanup()
    print("fake device tests passed:", len(device.requests), "authenticated requests")


if __name__ == "__main__":
    asyncio.run(main())
