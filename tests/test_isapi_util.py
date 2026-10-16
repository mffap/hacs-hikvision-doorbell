"""Stdlib-only tests for the ISAPI protocol helpers."""
import hashlib
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "isapi_util", ROOT / "custom_components/hikvision_doorbell/isapi_util.py")
util = importlib.util.module_from_spec(spec)
sys.modules["isapi_util"] = util
spec.loader.exec_module(util)

XML_EVENT = b"""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
<ipAddress>192.168.1.10</ipAddress><channelID>1</channelID>
<dateTime>2026-10-12T17:00:00+02:00</dateTime><activePostCount>1</activePostCount>
<eventType>VMD</eventType><eventState>active</eventState><eventDescription>Motion alarm</eventDescription>
</EventNotificationAlert>"""

JSON_EVENT = (b'{"ipAddress":"192.168.1.10","dateTime":"2026-10-12T17:00:01+02:00",'
              b'"eventType":"AccessControllerEvent","eventState":"active",'
              b'"AccessControllerEvent":{"majorEventType":5,"subEventType":37,"doorNo":1,'
              b'"employeeNoString":"6","name":"Fab {x}"}}')

def multipart(parts, with_length=True, dashes=True):
    out = b""
    for ctype, body in parts:
        out += (b"--" if dashes else b"") + b"boundary\r\n"
        out += b"Content-Type: " + ctype + b"\r\n"
        if with_length:
            out += b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        out += b"\r\n" + body + b"\r\n"
    return out

def run_chunks(parser, data, size):
    parts = []
    for i in range(0, len(data), size):
        parts += parser.feed(data[i:i + size])
    return parts

def check_stream(**kw):
    data = multipart([(b"application/xml; charset=UTF-8", XML_EVENT),
                      (b"image/jpeg", b"\xff\xd8\xff\x00binary--boundary-ish\xff\xd9"),
                      (b"application/json", JSON_EVENT)], **kw)
    for size in (1, 7, 64, len(data)):
        p = util.MultipartStreamParser(boundary="boundary")
        parts = run_chunks(p, data, size)
        assert [x.content_type.split(";")[0] for x in parts] == \
            ["application/xml", "image/jpeg", "application/json"], (kw, size, parts)
        events = [util.normalize_event(x) for x in parts]
        assert events[0]["event_type"] == "vmd" and events[0]["channel"] == 1
        assert events[1] is None
        assert events[2]["major"] == 5 and events[2]["minor"] == 0x25
        assert events[2]["door"] == 1 and events[2]["employee_no"] == "6"

def test_multipart_variants():
    check_stream(with_length=True, dashes=True)
    check_stream(with_length=True, dashes=False)

def test_multipart_without_length():
    # no Content-Length: the image body must not contain the marker, use plain parts
    data = multipart([(b"application/xml", XML_EVENT), (b"application/json", JSON_EVENT)],
                     with_length=False)
    for size in (1, 5, len(data)):
        p = util.MultipartStreamParser(boundary="boundary")
        parts = run_chunks(p, data + b"--boundary\r\n", size)
        assert len(parts) == 2, (size, parts)
        assert util.normalize_event(parts[1])["minor"] == 37

def test_bare_stream():
    data = b"\r\n" + XML_EVENT + b"\r\n" + JSON_EVENT + b"\r\n" + XML_EVENT
    for size in (1, 11, len(data)):
        p = util.MultipartStreamParser(boundary=None)
        parts = run_chunks(p, data, size)
        assert len(parts) == 3
        assert util.normalize_event(parts[1])["name"] == "Fab {x}"

def test_boundary_header():
    assert util.boundary_from_content_type('multipart/mixed; boundary="boundary"') == "boundary"
    assert util.boundary_from_content_type("multipart/mixed;boundary=MIME_boundary") == "MIME_boundary"
    assert util.boundary_from_content_type("application/xml") is None

def test_digest_rfc2617_vector():
    # RFC 2617 section 3.5 example
    auth = util.DigestAuth("Mufasa", "Circle Of Life")
    assert auth.set_challenge('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                              'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
                              'opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    header = auth.header("GET", "/dir/index.html")
    params = util.parse_www_authenticate(header)[1]
    ha1 = hashlib.md5(b"Mufasa:testrealm@host.com:Circle Of Life").hexdigest()
    ha2 = hashlib.md5(b"GET:/dir/index.html").hexdigest()
    expected = hashlib.md5(f"{ha1}:{params['nonce']}:{params['nc']}:{params['cnonce']}:auth:{ha2}".encode()).hexdigest()
    assert params["response"] == expected and params["nc"] == "00000001"
    assert params["opaque"] == "5ccc069c403ebaf9f0171e9517f40e41"
    assert util.parse_www_authenticate(auth.header("GET", "/x"))[1]["nc"] == "00000002"
    assert not auth.set_challenge('Basic realm="x"')

def test_xml_and_status():
    body = b'<?xml version="1.0"?><ResponseStatus xmlns="http://www.hikvision.com/ver20/XMLSchema"><statusCode>4</statusCode><statusString>Invalid Operation</statusString><subStatusCode>notSupport</subStatusCode></ResponseStatus>'
    assert "Invalid Operation" in util.isapi_status_error("application/xml", body)
    ok = b'{"statusCode":1,"statusString":"OK","subStatusCode":"ok"}'
    assert util.isapi_status_error("application/json", ok) is None
    info = b'<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema"><deviceName>Door</deviceName><model>DS-KV6113</model><a>1</a><a>2</a></DeviceInfo>'
    data = util.parse_response_body("application/xml", info)
    assert data == {"DeviceInfo": {"deviceName": "Door", "model": "DS-KV6113", "a": ["1", "2"]}}
    cap = b'<RemoteControlDoor xmlns="x"><doorNo min="1" max="2"/></RemoteControlDoor>'
    assert util.find_xml(cap, "doorNo").attrib["max"] == "2"

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
