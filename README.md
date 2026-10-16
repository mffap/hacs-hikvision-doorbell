# Hikvision Doorbell (HACS integration)

A native Home Assistant integration for Hikvision video intercoms: door stations
(DS-KV / DS-KD / DS-KB), indoor stations (DS-KH) and access control terminals (DS-K1T).

It is a rewrite of the doorbell part of
[pergolafabio/Hikvision-Addons](https://github.com/pergolafabio/Hikvision-Addons).
The add-on runs the proprietary Hikvision HCNetSDK in a Docker container and talks to
Home Assistant over MQTT. This integration instead talks to the device directly over
**ISAPI** (the device's HTTP API), so:

- no add-on, no MQTT broker, no native libraries – works on HA OS, Container, Core, any CPU
- entities are created through a normal config flow (Settings → Devices & services)
- rings and other events come from the device's `alertStream` in real time; polling the call
  state is optional

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repository's URL, type **Integration**.
2. Install **Hikvision Doorbell** and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Hikvision Doorbell*.
4. Enter the IP, **web port (usually 80)**, username and password, then pick the device type.

Add one entry per device (door station and indoor station are separate entries, as in the add-on).

> Hikvision devices lock the account after several failed logins. The integration backs off
> after a rejected login and asks you to re-authenticate after three rejections in a row.

## Entities

| Entity | Devices | Replaces add-on entity |
|---|---|---|
| `sensor` Call state (`idle` / `ringing` / `on_call`) | all | Call state |
| `event` Doorbell (`ring`) | all | ring automation trigger |
| `event` Access control (door/lock opened & closed, card/face/fingerprint valid & invalid, exit button…) | outdoor, terminal | Door unlocked / ACS device triggers |
| `event` Device event (motion, tamper, alarm input, zone alarm, other) | all | motion / alarm device triggers |
| `button` Open door *N* | all | Door *N* relay switch |
| `button` Answer / Reject / Hang up call | all | same buttons |
| `button` Mute / Unmute audio output, Restart | all | same buttons / Reboot |
| `camera` live view (JPEG + RTSP stream) | outdoor, terminal | – |
| `image` Last snapshot (taken automatically on every ring) + `button` Take snapshot | outdoor, terminal | Latest Snapshot / Take Snapshot |
| `switch` COM *N* | indoor | Com *N* relay |
| `select` Scene (at home / go out / go to bed / custom) | indoor | Scene select and buttons |
| `switch` Alarm (setupAlarm / closeAlarm) | indoor, opt-in | Alarm select / buttons |
| `binary_sensor` Event stream (diagnostic) | all | Online state |

Every event is also fired on the bus as `hikvision_doorbell_event` with the raw device
payload, for automations that need fields not mapped above.

### Raw ISAPI requests

The add-on's "ISAPI request" text entity is a service with a response here:

```yaml
action: hikvision_doorbell.isapi_request
data:
  device_id: 0123456789abcdef
  method: PUT
  path: /ISAPI/VideoIntercom/callSignal?format=json
  body: '{"CallSignal":{"cmdType":"reject"}}'
response_variable: result
```

### Example automation

```yaml
triggers:
  - trigger: state
    entity_id: event.front_door_doorbell
actions:
  - action: notify.mobile_app_phone
    data:
      message: Someone is at the door
      data:
        image: /api/image_proxy/image.front_door_last_snapshot
```

## Options

| Option | Default | |
|---|---|---|
| Call status poll interval | 0 (off) | `0` relies on the event stream only. Set e.g. `2` to also poll `callStatus` (tracks answered/ended calls, but adds a request every few seconds). Devices without an event stream fall back to 2 s unless you set this explicitly |
| Reset 'ringing' after | 60 s | how long a ring stays `ringing` when the call status is not polled |
| Number of door relays | auto | auto uses door capabilities / `electroLockNum` / IO outputs, like the add-on |
| Number of COM outputs | auto | indoor only, from `alarmOutNum` |
| Scene selector / Alarm switch | on / off | indoor only |
| RTSP port | 554 | outdoor / terminal |

## Not ported

These add-on features depend on the native SDK or on Asterisk and have no ISAPI
equivalent, so they are intentionally left out:

- two-way audio, voice talk and broadcast (`start_voice_talk`, Broadcast On/Off/Audio Path)
- calling an indoor station from HA (Call On/Off) and SIP chime registration
- SDK-only fallbacks for very old firmware (`NET_DVR_RemoteControl`, SDK call signal)

If your device only works through the SDK on port 8000, keep using the add-on.

## Development

```bash
python tests/test_isapi_util.py          # protocol helpers, stdlib only
pip install aiohttp && python tests/test_api_fake_device.py   # client against a fake device
```

The GitHub workflow also runs the HACS and hassfest validators. Before publishing,
replace `YOUR_GITHUB_USER` in `manifest.json` and add yourself to `codeowners`.

## Credits

Based on the protocol knowledge in pergolafabio/Hikvision-Addons. That repository does
not declare a license; this integration is a from-scratch rewrite on ISAPI and contains
none of its code. Check with the original author before reusing anything from it directly.
