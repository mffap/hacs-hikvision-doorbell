"""Constants for the Hikvision Doorbell integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hikvision_doorbell"

# Config entry data
CONF_DEVICE_KIND: Final = "device_kind"
CONF_VERIFY_SSL: Final = "verify_ssl"

# Options
CONF_CALL_STATUS_INTERVAL: Final = "call_status_interval"
CONF_DOOR_COUNT: Final = "door_count"
CONF_COM_COUNT: Final = "com_count"
CONF_ENABLE_SCENES: Final = "enable_scenes"
CONF_ENABLE_ALARM: Final = "enable_alarm"
CONF_RING_RESET: Final = "ring_reset"
CONF_RTSP_PORT: Final = "rtsp_port"

DEFAULT_HTTP_PORT: Final = 80
DEFAULT_HTTPS_PORT: Final = 443
DEFAULT_RTSP_PORT: Final = 554
DEFAULT_CALL_STATUS_INTERVAL: Final = 0  # seconds, 0 = rely on the event stream only
FALLBACK_CALL_STATUS_INTERVAL: Final = 2  # used when the device has no event stream
DEFAULT_RING_RESET: Final = 60  # seconds, used when the device cannot report call status
AUTO: Final = -1  # door/com count auto-detection

# Device kinds
KIND_OUTDOOR: Final = "outdoor"
KIND_INDOOR: Final = "indoor"
KIND_ACCESS_CONTROL: Final = "access_control"
DEVICE_KINDS: Final = [KIND_OUTDOOR, KIND_INDOOR, KIND_ACCESS_CONTROL]

# Events fired on the Home Assistant bus
EVENT_DEVICE_EVENT: Final = f"{DOMAIN}_event"

# Call states exposed by the call state sensor
CALL_IDLE: Final = "idle"
CALL_RINGING: Final = "ringing"
CALL_ON_CALL: Final = "on_call"
CALL_UNKNOWN: Final = "unknown"
CALL_STATES: Final = [CALL_IDLE, CALL_RINGING, CALL_ON_CALL, CALL_UNKNOWN]

# Map raw `/ISAPI/VideoIntercom/callStatus` values to our states
CALL_STATUS_MAP: Final = {
    "idle": CALL_IDLE,
    "ring": CALL_RINGING,
    "ringing": CALL_RINGING,
    "calling": CALL_RINGING,
    "oncall": CALL_ON_CALL,
    "on_call": CALL_ON_CALL,
    "talking": CALL_ON_CALL,
}

# Scenes understood by /ISAPI/VideoIntercom/scene/nowMode (indoor stations)
SCENES: Final = ["atHome", "goOut", "goToBed", "custom"]

# Commands for /ISAPI/SecurityCP/AlarmControlByPhone (indoor stations)
ALARM_ARM: Final = "setupAlarm"
ALARM_DISARM: Final = "closeAlarm"

# Access control event codes (majorEventType 5, subEventType). Only the codes
# that are useful for automations get a dedicated event type, everything else
# is reported as `access_control_other` with the raw codes as attributes.
ACS_MAJOR_ALARM: Final = 0x1
ACS_MAJOR_EXCEPTION: Final = 0x2
ACS_MAJOR_OPERATION: Final = 0x3
ACS_MAJOR_EVENT: Final = 0x5

ACS_MINOR_DOORBELL_RINGING: Final = 0x25

ACS_EVENT_TYPES: Final[dict[int, str]] = {
    0x01: "card_valid",
    0x02: "card_and_password_valid",
    0x03: "card_and_password_invalid",
    0x06: "card_no_permission",
    0x08: "card_expired",
    0x09: "card_invalid",
    0x15: "lock_opened",
    0x16: "lock_closed",
    0x17: "exit_button_pressed",
    0x18: "exit_button_released",
    0x19: "door_opened",
    0x1A: "door_closed",
    0x1B: "door_opened_abnormally",
    0x1C: "door_open_timeout",
    0x26: "fingerprint_valid",
    0x27: "fingerprint_invalid",
    0x4B: "face_valid",
    0x4C: "face_invalid",
    0x65: "employee_and_password_valid",
    0x66: "employee_and_password_invalid",
}
ACS_EVENT_OTHER: Final = "access_control_other"

# Generic ISAPI event types we expose on the "Device event" event entity.
# Any other eventType is reported as `other`.
DEVICE_EVENT_TYPES: Final[dict[str, str]] = {
    "vmd": "motion",
    "motiondetection": "motion",
    "tamper": "tamper",
    "shelteralarm": "tamper",
    "io": "alarm_input",
    "videointercomalarm": "intercom_alarm",
    "zonealarm": "zone_alarm",
}
DEVICE_EVENT_OTHER: Final = "other"

# Events we never forward (keep-alives)
IGNORED_EVENT_TYPES: Final = {"videoloss", "heartbeat"}
