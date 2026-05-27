# CuboAI Camera — Protocol Research Notes

This document describes everything we have learned about how the CuboAI baby
monitor communicates, what we tried, what worked, and what didn't. It is
intended to help future contributors understand the system without repeating
our reverse engineering work.

---

## Overview

The CuboAI camera is a baby monitor that streams HEVC (H.265) video and AAC
audio, and accepts control commands for its night light, lullaby player, and
various detection features. It uses the **ThroughTek (TUTK) Kalay P2P SDK**
for all connectivity — both cloud and LAN.

The integration works by wrapping the TUTK native library (`libIOTCAPIs_ALL.so`)
via Python `ctypes`, giving us full control over the camera without needing
to reverse-engineer the encrypted P2P protocol.

---

## Architecture

### Connection modes

TUTK supports three connection modes:
- **Mode 0: P2P** — direct UDP hole-punching between client and camera
- **Mode 1: Relay** — traffic routed through ThroughTek relay servers
- **Mode 2: LAN** — direct UDP on the local network

Confirmed via `IOTC_Session_Check` on a live session: the phone app connects
in **LAN mode (2)** when on the same network as the camera.

### The two channels

Even in LAN mode, TUTK uses two distinct channels:

1. **51cc AV negotiation channel** — UDP to ThroughTek relay servers
   (ports 10240 and 3478). This carries the HELLO/ECDH/NEGOTIATE handshake
   that establishes encryption keys. Confirmed via Frida: goes to relay even
   in LAN mode, never directly to the camera IP.

2. **nl/No data channel** — UDP directly to the camera on a dynamic port
   (typically 41344). Carries the encrypted IOTC session data (keepalives,
   IOCTL commands, video frames, audio frames).

### LAN discovery

The library discovers the camera on LAN by sending an **nL probe** (88 bytes,
fixed constant) as an IPv6 UDP broadcast to `::ffff:192.168.x.255:32761`.
The camera responds from a dynamic port with an **NO response**.

**Critical finding:** On Linux, the library only broadcasts to the network
broadcast address, not directly to the camera IP. If the host's network
doesn't forward broadcasts to the camera (e.g., in a VM), the probe gets no
response and the connection hangs indefinitely.

**Solution:** A small C `LD_PRELOAD` shim intercepts `sendto()` calls and
redirects broadcast probes on port 32761 to the camera IP directly. This shim
is compiled and installed automatically by `cuboai_tutk.py` when `camera_ip`
is provided.

---

## What we tried and what we learned

### Attempt 1: Pure Python transport

We initially tried to implement the entire TUTK protocol in pure Python
(see `cuboai_transport.py` — kept for reference). This required:
- LAN discovery via nL/NO probes ✅ (working)
- 51cc HELLO → AV_START → NEGOTIATE handshake ❌ (blocked)

The 51cc handshake goes to ThroughTek relay servers. The relay servers only
accept handshakes from clients that have first registered via a **bootstrap
TLS call** — a proprietary TLS protocol we could not fully reverse-engineer.
Without completing this handshake, we cannot derive the ECDH encryption key,
and therefore cannot encrypt the nl frame payloads.

**Decision:** Use the TUTK native library via ctypes instead. This handles
bootstrap, ECDH, and encryption internally.

### Attempt 2: ctypes with libIOTCAPIs_ALL.so ✅

Wrapping the native library via ctypes works reliably. Key discoveries:

- The x86-64 combined library (`libIOTCAPIs_ALL.so`, version 4.2.1.1-H)
  connects successfully despite the camera running a newer TUTK version
  (camera firmware 3.0.1369, TUTK version ~4.3.x).
- The `AVClientStartInConfig` struct is 48 bytes with specific field offsets
  confirmed via Frida on x86-64. The struct layout differs between TUTK
  library versions, causing failures if assumed incorrectly.
- `avClientStartEx` must be used (not `avClientStart`) for the extended
  configuration struct.
- `security_mode = 0` (NON-SECURE) must be set — the camera rejects
  connections with any other value.

### Audio codec discovery

Initial assumption: G.711 μ-law (standard TUTK audio codec).
Codec ID 0x0088 in the frame info suggested IMA ADPCM.
Actual format: **AAC-LC in ADTS container** — discovered by examining the
raw bytes and recognising the 0xFFF1 ADTS sync word.

The AAC frames are self-contained ADTS packets:
- Sample rate: 16000 Hz (idx=8 in the sample rate table)
- Channels: 1 (mono)
- Profile: AAC-LC
- Frame size: 448 bytes = 1024 samples = 64ms per frame

The frame info codec field (0x0088) does NOT correspond to standard TUTK codec
IDs for this camera — the codec identification must come from the ADTS header
itself.

### Audio requires video drain

`avRecvAudioData` only delivers data when `avRecvFrameData2` is also being
called concurrently. If only audio is requested, the camera stops sending
both streams. This is a TUTK protocol constraint — the camera multiplexes
audio and video over the same AV channel and expects both to be consumed.

Solution: `TUTKSession.audio_frames()` and `TUTKSession.av_frames()` both
run video receive in a background thread automatically.

### Resolution control

`IOTYPE_USER_IPCAM_SETRESOLUTION` (type 255) is sent during stream startup
but the camera ignores the resolution parameter. The streaming resolution is
fixed by the camera firmware / app configuration and cannot be changed via
IOCTL from our side. The `changeStreamType()` function in the app goes through
the proprietary `cloud.yunyun.cubo.camera.client.Client` class (native code),
not a standard IOCTL.

### Sleep mode payload

Initial assumption: simple 12-byte payload with on/off at offset 4.
Actual format: **96-byte payload** with:
- Bytes 0-3: Unix timestamp (LE32)
- Bytes 4-87: zeros
- Byte 88: on/off flag (1=ON, 0=OFF)
- Bytes 89-95: zeros

Confirmed by hooking `avSendIOCtrl` with Frida and toggling sleep mode in the
app. Our initial implementation sent the wrong payload and the camera silently
accepted it but didn't change state.

---

## IOCTL type codes — complete list

All codes discovered from decompiled app source (JADX on CuboAI app v2.23.2)
and confirmed/extended via Frida.

| Request | Response | Name | Status |
|---------|----------|------|--------|
| 255 | — | SETRESOLUTION | confirmed |
| 511 | — | IPCAM_START | confirmed |
| 768 | — | AUDIOSTART | confirmed |
| 2312 | 2313 | GET_DANGER_ZONE | untested |
| 2318 | 2319 | GET_YUN_WIFI | untested |
| 2324 | 2325 | GET_CRY_DETECT | confirmed (read) |
| 2326 | 2327 | SET_CRY_DETECT | untested |
| 2330 | 2331 | GET_SLEEP_SAFETY_SETTING | confirmed (read) |
| 2332 | 2333 | SET_SLEEP_SAFETY_SETTING | untested |
| 2336 | 2337 | GET_SLEEP_SAFETY_STATUS | confirmed (read) |
| 2344 | 2345 | GET_SLEEP_MODE (=PRIVACY_MODE) | confirmed |
| 2346 | 2347 | SET_SLEEP_MODE | confirmed |
| 2352 | 2353 | GET_DETECTION_ZONE | untested |
| 2368 | 2369 | GET_AUTO_CAPTURE | untested |
| 2370 | 2371 | SET_AUTO_CAPTURE | untested |
| 2380 | 2381 | GET_DETECTION_ZONE_V2 | untested |
| 2400 | 2401 | GET_UPDATE_INFO | confirmed (read) |
| 2404 | 2405 | GET_LULLABY_INFO | confirmed |
| 2406 | 2407 | GET_LIGHT_WAY_CONFIG | untested |
| 2434 | 2435 | SET_LULLABY_ACTION (play/stop) | confirmed |
| 2436 | 2437 | GET_LULLABY_VOL_DURATION | confirmed |
| 2438 | 2439 | SET_LULLABY_VOL_DURATION | confirmed |
| 2440 | 2441 | GET_LULLABY_SCHEDULE | confirmed |
| 2452 | 2453 | GET_COUGH_SETTING | confirmed (read) |
| 2454 | 2455 | SET_COUGH_SETTING | untested |
| 2458 | 2459 | GET_CONNECTED_USER | confirmed |
| 4352 | 4353 | GET_NIGHT_LIGHT_ON_OFF | confirmed |
| 4354 | 4355 | SET_NIGHT_LIGHT_ON_OFF | confirmed |
| 4362 | 4363 | GET_STATUS_LIGHT_ON_OFF | confirmed |
| 4364 | 4365 | SET_STATUS_LIGHT_ON_OFF | untested |
| 4366 | 4367 | GET_LIGHT_STYLE (brightness) | confirmed |
| 4368 | 4369 | SET_LIGHT_STYLE (brightness) | confirmed |
| 4372 | 4373 | GET_TEMP_HUMIDITY | confirmed |
| 4378 | 4379 | GET_HW_POLICY | untested |
| 4384 | 4385 | GET_HW_CONTROL | confirmed |
| 4612 | 4613 | GET_DANGER_ZONE_2 | untested |
| 4866 | 4867 | GET_MAT_CONFIG (breathing mat) | untested |
| 4868 | 4869 | GET_MAT_INFO | untested |
| 4876 | 4877 | GET_SMART_TEMP_INFO | untested |
| 4880 | 4881 | GET_SMART_TEMP_CONFIG | untested |

Notes:
- "Sleep mode" and "privacy mode" are the same IOCTL (2344/2346). The app
  uses the same `SMsgAVIoctrlGetPrivacyModeResp` struct for both and wraps it
  in `CameraSleepMode` for the UI.
- "Danger zone" = "Baby Gate" in the app UI — a virtual fence feature.
- "Breathing mat" requires a separate hardware accessory (not built into camera).
- "Smart temp" requires a separate Bluetooth thermometer accessory.

---

## Connected users response format

Type 2459, 1000 bytes. Contains up to 3 recent session records starting at
byte offset 128. Each record is 120 bytes:

```
offset 0:   email (64 bytes, null-terminated ASCII)
offset 64:  connection type (4 bytes LE32): 0=P2P, 1=Relay, 2=LAN
offset 68:  unix timestamp (4 bytes LE32)
offset 72:  session UUID (45 bytes, null-terminated ASCII)
```

The connection type matches the IOTC session mode values from `IOTC_Session_Check`.
Sessions are stored as a recent history, not as currently active connections.

---

## nl frame format

The TUTK library sends all session data wrapped in **nl frames** over UDP:

```
bytes 0-1:   magic: 6e 6c ('nl')
bytes 2-3:   flags (fd fe for data frames)
bytes 4-7:   session key (4 bytes, fixed per session)
bytes 8-11:  sequence number (LE32, incrementing)
bytes 12-31: session identification fields
bytes 32+:   encrypted IOTC payload
```

The payload is encrypted using a key derived from the ECDH handshake with
the relay server (51cc protocol). Without completing this handshake, the
payload cannot be constructed — this is why pure Python transport failed.

Camera responses come as **No frames** (magic: 4e 6f) with the same structure.

---

## Relay server details

- Hostname: `all-c-master-rylhwds4ctn75xv5puua.iotcplatform.com`
- Port: 10240 (51cc HELLO), 3478 (STUN/NEGOTIATE)
- The hostname is baked into `libTUTKGlobalAPIs.so` at file offset 0x4da160

The relay server only accepts 51cc HELLO from clients that have completed
a bootstrap TLS registration (proprietary protocol, not yet reversed). This
is what prevents pure Python from working — you cannot make a cold connection
to the relay without the bootstrap step.

---

## App source analysis

The CuboAI APK can be decompiled with [JADX](https://github.com/skylot/jadx)
to reveal the complete feature set. The relevant source is in the `getcubo/`
package within the decompiled output. Key files:

- `app/camera/CameraManager.java` — all camera command senders
- `app/viewmodel/CameraViewModel.java` — UI-to-camera binding, full feature list
- `app/livedata/Get*LiveData.java` — response handlers (contain IOCTL type IDs)
- `app/camera/LullabyManager.java` — lullaby catalog and playback logic

The app uses a `cloud.yunyun.cubo.camera.client.Client` class for the actual
P2P connection. This is native code (not in the decompiled source) and handles
the TUTK session lifecycle, encryption, and streaming.

---

## Copyright and legal notes

- **ThroughTek TUTK SDK** (`libIOTCAPIs_ALL.so` etc.): proprietary, owned by
  ThroughTek Co. Ltd. Cannot be redistributed. Must be extracted from the app
  by the end user.

- **CuboAI app** (`com.getcubo.app`): proprietary, owned by CuboAI Inc.
  Decompilation for personal interoperability is permitted in most jurisdictions
  (EU Software Directive Art. 6, US DMCA §1201(f)). The decompiled source
  should not be redistributed.

- **This integration code** (cuboai_*.py): original work, freely shareable.
  The IOCTL type codes and protocol details we discovered are facts and cannot
  be copyrighted.

- **The lullaby UUIDs**: extracted from the app's `LullabyManager.java`. These
  are identifiers for audio content owned by CuboAI/third parties. The UUIDs
  themselves are not copyrightable but the audio they reference is.

---

## Future work

1. **Pure Python transport (Option B)**: Reverse-engineer the bootstrap TLS
   protocol that the TUTK library performs before the 51cc handshake. This
   would eliminate the dependency on the native library entirely.

2. **SET commands for detection features**: Cry detection, cough detection,
   sleep safety settings — we can read these but haven't confirmed the SET
   payload formats via Frida.

3. **arm64 combined library**: The arm64 (Raspberry Pi) build comes as three
   separate `.so` files. Combining them or loading them in sequence should work
   but hasn't been tested on actual RPi hardware.

4. **Two-way audio**: `avSendAudioData` sends audio to the camera speaker.
   The expected format is G.711 μ-law 8kHz (confirmed from wyzecam research),
   but this hasn't been verified against this camera.

5. **go2rtc integration**: The stream scripts (`cuboai_stream_video.py`,
   `cuboai_stream_audio.py`) are written but not yet tested with actual go2rtc.

6. **Home Assistant custom component**: The validate script demonstrates all
   API capabilities. A full HA integration would wrap these in sensor, light,
   media_player, and camera entities.
