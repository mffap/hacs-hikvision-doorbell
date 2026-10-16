"""Protocol helpers for Hikvision ISAPI that only depend on the standard library.

Kept separate from the aiohttp client so they can be unit tested in isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# HTTP Digest authentication (RFC 7616), as used by Hikvision firmware
# ---------------------------------------------------------------------------

_AUTH_PARAM_RE = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))')


def parse_www_authenticate(header: str) -> tuple[str, dict[str, str]]:
    """Split a WWW-Authenticate header into its scheme and parameters."""
    scheme, _, rest = header.strip().partition(" ")
    params: dict[str, str] = {}
    for match in _AUTH_PARAM_RE.finditer(rest):
        key = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        params[key] = value
    return scheme.lower(), params


class DigestAuth:
    """Compute Digest `Authorization` headers, reusing the server nonce."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._challenge: dict[str, str] | None = None
        self._nonce_count = 0

    @property
    def has_challenge(self) -> bool:
        return self._challenge is not None

    def set_challenge(self, www_authenticate: str) -> bool:
        """Store a Digest challenge. Return False if the header is not Digest."""
        scheme, params = parse_www_authenticate(www_authenticate)
        if scheme != "digest" or "nonce" not in params:
            return False
        if self._challenge is None or self._challenge.get("nonce") != params["nonce"]:
            self._nonce_count = 0
        self._challenge = params
        return True

    def reset(self) -> None:
        self._challenge = None
        self._nonce_count = 0

    def _hash(self, data: str) -> str:
        assert self._challenge is not None
        algorithm = self._challenge.get("algorithm", "MD5").upper()
        if algorithm.startswith("SHA-256"):
            return hashlib.sha256(data.encode()).hexdigest()
        return hashlib.md5(data.encode()).hexdigest()  # noqa: S324 - mandated by the protocol

    def header(self, method: str, uri: str) -> str:
        """Return the value for the Authorization header."""
        if self._challenge is None:
            raise RuntimeError("No digest challenge received yet")
        challenge = self._challenge
        realm = challenge.get("realm", "")
        nonce = challenge["nonce"]
        qop_options = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
        algorithm = challenge.get("algorithm", "MD5")

        ha1 = self._hash(f"{self._username}:{realm}:{self._password}")
        if algorithm.upper().endswith("-SESS"):
            cnonce = os.urandom(8).hex()
            ha1 = self._hash(f"{ha1}:{nonce}:{cnonce}")
        ha2 = self._hash(f"{method.upper()}:{uri}")

        parts = [
            f'username="{self._username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f"algorithm={algorithm}",
        ]
        if "auth" in qop_options:
            self._nonce_count += 1
            nc = f"{self._nonce_count:08x}"
            cnonce = hashlib.md5(f"{time.time()}{os.urandom(8).hex()}".encode()).hexdigest()[:16]  # noqa: S324
            response = self._hash(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
            parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
        else:
            response = self._hash(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{response}"')
        if "opaque" in challenge:
            parts.append(f'opaque="{challenge["opaque"]}"')
        return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def xml_to_dict(element: ET.Element) -> Any:
    """Convert an XML element into plain Python data, dropping namespaces.

    Leaf nodes become strings, repeated children become lists.
    """
    children = list(element)
    if not children:
        return (element.text or "").strip()
    result: dict[str, Any] = {}
    for child in children:
        key = _strip_ns(child.tag)
        value = xml_to_dict(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def parse_xml(text: str | bytes) -> tuple[str, Any]:
    """Parse an XML document and return (root tag without namespace, data)."""
    root = ET.fromstring(text)
    return _strip_ns(root.tag), xml_to_dict(root)


def find_xml(text: str | bytes, name: str) -> ET.Element | None:
    """Find the first element named `name` (any namespace, any depth)."""
    root = ET.fromstring(text)
    if _strip_ns(root.tag) == name:
        return root
    return root.find(f".//{{*}}{name}")


def parse_response_body(content_type: str, body: bytes) -> Any:
    """Decode an ISAPI response into Python data where possible."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if "json" in content_type or text[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            pass
    if text.startswith("<"):
        try:
            tag, data = parse_xml(text)
        except ET.ParseError:
            return text
        return {tag: data}
    return text


def isapi_status_error(content_type: str, body: bytes) -> str | None:
    """Return an error description if an ISAPI ResponseStatus reports failure."""
    data = parse_response_body(content_type, body)
    if not isinstance(data, dict):
        return None
    status = data.get("ResponseStatus") if "ResponseStatus" in data else data
    if not isinstance(status, dict):
        return None
    code = str(status.get("statusCode", "1"))
    if code in ("0", "1"):
        return None
    return (
        f"{status.get('statusString') or status.get('subStatusCode') or 'error'}"
        f" (statusCode {code}, subStatusCode {status.get('subStatusCode', '-')})"
    )


# ---------------------------------------------------------------------------
# Event stream (/ISAPI/Event/notification/alertStream)
# ---------------------------------------------------------------------------


@dataclass
class StreamPart:
    """A single part extracted from the multipart event stream."""

    headers: dict[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").lower()


def boundary_from_content_type(content_type: str) -> str | None:
    match = re.search(r'boundary="?([^";]+)"?', content_type, re.IGNORECASE)
    return match.group(1) if match else None


@dataclass
class MultipartStreamParser:
    """Incremental parser for the endless multipart/mixed alert stream.

    Hikvision devices send `--boundary`, part headers (Content-Type and
    usually Content-Length), a blank line and the body. Some firmwares omit
    the leading dashes or Content-Length, both cases are handled. When the
    device does not use multipart at all (no boundary), bare XML/JSON
    documents are split on their closing tag/brace.
    """

    boundary: str | None
    max_buffer: int = 4 * 1024 * 1024
    _buffer: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[StreamPart]:
        self._buffer.extend(data)
        parts = self._parse_multipart() if self.boundary else self._parse_bare()
        if len(self._buffer) > self.max_buffer:
            # Something went wrong, avoid unbounded growth
            self._buffer.clear()
        return parts

    def _parse_multipart(self) -> list[StreamPart]:
        assert self.boundary
        parts: list[StreamPart] = []
        # Some firmwares send "--boundary", others just "boundary". Matching the
        # bare token works for both; the leading dashes end up as junk before it.
        marker = self.boundary.encode()
        if marker.startswith(b"--"):
            marker = marker[2:]
        marker_len = len(marker)
        while True:
            buf = self._buffer
            start = buf.find(marker)
            if start < 0:
                # Keep a tail in case the marker is split across chunks
                keep = marker_len + 2
                if len(buf) > keep:
                    del buf[: len(buf) - keep]
                return parts
            header_end = buf.find(b"\r\n\r\n", start)
            sep_len = 4
            if header_end < 0:
                header_end = buf.find(b"\n\n", start)
                sep_len = 2
            if header_end < 0:
                if start:
                    del buf[:start]
                return parts
            header_block = bytes(buf[start + marker_len : header_end])
            if header_block.startswith(b"--"):
                # Closing boundary: discard it
                del buf[: header_end + sep_len]
                continue
            headers = _parse_headers(header_block)
            body_start = header_end + sep_len
            length_str = headers.get("content-length")
            if length_str and length_str.strip().isdigit():
                length = int(length_str.strip())
                if len(buf) < body_start + length:
                    if start:
                        del buf[:start]
                    return parts
                body = bytes(buf[body_start : body_start + length])
                del buf[: body_start + length]
            else:
                nxt = buf.find(marker, body_start)
                if nxt < 0:
                    if start:
                        del buf[:start]
                    return parts
                end = nxt - 2 if buf[nxt - 2 : nxt] == b"--" else nxt
                body = bytes(buf[body_start:end])
                del buf[:end]
            if body.strip():
                parts.append(StreamPart(headers=headers, body=body.strip()))

    def _parse_bare(self) -> list[StreamPart]:
        parts: list[StreamPart] = []
        while True:
            buf = self._buffer
            text_start = next((i for i, c in enumerate(buf) if c in b"<{"), -1)
            if text_start < 0:
                buf.clear()
                return parts
            if text_start:
                del buf[:text_start]
            if buf[:1] == b"<":
                end_tag = b"</EventNotificationAlert>"
                end = buf.find(end_tag)
                if end < 0:
                    return parts
                end += len(end_tag)
                ctype = "application/xml"
            else:
                end = _json_object_end(buf)
                if end < 0:
                    return parts
                ctype = "application/json"
            parts.append(StreamPart(headers={"content-type": ctype}, body=bytes(buf[:end])))
            del buf[:end]


def _json_object_end(buf: bytearray) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(buf):
        if in_string:
            if escaped:
                escaped = False
            elif char == 0x5C:  # backslash
                escaped = True
            elif char == 0x22:  # quote
                in_string = False
            continue
        if char == 0x22:
            in_string = True
        elif char == 0x7B:
            depth += 1
        elif char == 0x7D:
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _parse_headers(block: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in block.decode("latin-1").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return headers


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value), 0) if isinstance(value, str) and value.startswith("0x") else int(value)
    except (TypeError, ValueError):
        return None


def normalize_event(part: StreamPart) -> dict[str, Any] | None:
    """Turn a stream part into a flat event dict, or None for non-event parts.

    The returned dict always has `event_type` (lowercase) and `raw`.
    Access control events additionally get `major`, `minor`, `door`,
    `employee_no`, `name` and `card_no` when available.
    """
    ctype = part.content_type
    if "image" in ctype:
        return None
    data: Any
    text = part.body.decode("utf-8", errors="replace").strip()
    try:
        if "json" in ctype or text.startswith("{"):
            data = json.loads(text)
        elif text.startswith("<"):
            _, data = parse_xml(text)
        else:
            return None
    except (ValueError, ET.ParseError):
        return None
    if not isinstance(data, dict):
        return None

    event_type = str(data.get("eventType") or "").strip()
    if not event_type:
        return None

    event: dict[str, Any] = {
        "event_type": event_type.lower(),
        "event_type_raw": event_type,
        "event_state": str(data.get("eventState") or "").lower() or None,
        "description": data.get("eventDescription"),
        "channel": _to_int(data.get("channelID") or data.get("dynChannelID")),
        "date_time": data.get("dateTime"),
        "raw": data,
    }

    acs = data.get("AccessControllerEvent")
    if isinstance(acs, dict):
        event["major"] = _to_int(acs.get("majorEventType"))
        event["minor"] = _to_int(acs.get("subEventType"))
        event["door"] = _to_int(acs.get("doorNo"))
        event["employee_no"] = acs.get("employeeNoString") or acs.get("employeeNo")
        event["name"] = acs.get("name")
        event["card_no"] = acs.get("cardNo")
        event["verify_mode"] = acs.get("currentVerifyMode")
    return event
