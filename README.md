# cuboai-pure

A pure-Python transport for **CuboAI** baby-monitor cameras. It connects to the
camera over your LAN with no proprietary native library and produces a clean,
correctly-timed **HEVC (H.265) + AAC MPEG-TS** stream on stdout — ready for
[go2rtc](https://github.com/AlexxIT/go2rtc) to re-stream as WebRTC / HLS / MSE
(for example into Home Assistant).

It also includes a command-line utility for snapshots, recordings, status reads,
and camera controls (night light, volume, lullabies, detection settings, …).

> Independent interoperability software for a camera you own. Not affiliated with
> CuboAI / Cubo or ThroughTek. See `LICENSE`.

![Example output from cuboai_validate.py](screenshot.png)

Validated against a **CuboAI Gen 3** camera (firmware 3.0.1369). Other models / firmware are untested.

## Feature support

| Feature | Status |
|---|---|
| Temperature & humidity | ✅ |
| Night light on/off + brightness | ✅ |
| Night-light colour (RGB) | ⚠️ Set implemented, **unverified** (Gen 3 appears white-only) |
| Status LED | ✅ |
| Lullaby play / stop / select (34 songs) | ✅ |
| Lullaby volume + sleep timer | ✅ |
| Lullaby schedule (read + add / delete rows) | ✅ read + write (add/delete **live-confirmed**) |
| **Local DVR playback / rewind** (on-camera storage, no cloud) | ✅ retrieve a past moment to a playable `.ts` |
| **Detection history** (per-minute in-crib presence, motion, noise avg+peak, night-vision, temp/humidity) | ✅ read + ASCII / HTML charts |
| Sleep / privacy mode (suspends video) | ✅ |
| Cry / sleep-safety / cough detection status | ✅ read |
| Cry / cough detection sensitivity | ✅ set (gated) |
| Firmware version, session history, **session stats**, **user list** | ✅ read |
| Wi-Fi status incl. **RSSI / noise / channel** | ✅ read |
| JPEG snapshot | ✅ |
| HEVC (H.265) video + AAC audio (16 kHz mono), combined MPEG-TS | ✅ |
| Loss recovery (selective-repeat) + clean-GOP gating | ✅ |
| go2rtc streaming (video passthrough, audio → Opus) | ✅ |
| Two-way talk (send audio to the camera) | ✅ Working — AAC-LC, paced + loss-recovered (**pure backend only**) |
| Detection zones | 🔬 Complex struct, not implemented |
| Accessory / config SETs (sleep-mat, smart-temp, light RGB) | ⚠️ Implemented behind `--i-understand-this-is-unsafe`, **untested** |

## What it does

- **Direct LAN connection** — pure Python, no native library and no relay/cloud on
  the connect path.
- **Combined audio + video** — the camera interleaves HEVC video and AAC audio on
  one channel; both are muxed into a single MPEG-TS. Audio and video share one
  timeline (the camera's own clock), so they stay in sync.
- **Loss recovery** — selective-repeat retransmission and clean-GOP gating keep the
  picture clean on a lossy Wi-Fi link.
- **go2rtc friendly** — designed to run as a go2rtc `exec` source; go2rtc transcodes
  only the audio to Opus for WebRTC while the video stays a passthrough copy.

## Requirements

- **Python 3.8+** (standard library only for streaming). Tested on **Python 3.14**;
  works on 3.13+ where the stdlib `audioop` module was removed (none of the media
  paths use `audioop` — PyAV handles all transcoding).
- **[PyAV](https://pypi.org/project/av/)** (`pip install av`) — required for `--snapshot`
  (JPEG), `--record` (MP4), and `--talk` (it transcodes the audio file to **AAC-LC 16 kHz
  mono**, the camera's uplink codec). PyAV bundles its own ffmpeg libraries, so no separate
  ffmpeg binary is needed. Plain streaming does not need PyAV.

### Getting your camera credentials

You need your camera's **device UID**, **admin account** (`admin@...`), **admin password**,
and ideally its **LAN IP**. These live in the CuboAI cloud and are retrieved once from the
app's REST API (not part of this project). Intercept the CuboAI app's HTTPS traffic one time
(e.g. with mitmproxy / Charles); the app calls:

```
GET https://app-api.getcubo.com/prod/user/cameras
```

which returns a JSON array with your camera's `license_id` (→ UID), `dev_admin_id` (→ account),
and `dev_admin_pwd` (→ password). After that, everything runs locally on your LAN — no cloud on
the path. The credentials are provisioned at pairing and don't change unless the camera is reset
or re-paired. The LAN IP comes from your router / DHCP.

## Quickstart (go2rtc example)

1. Put these files in a directory, e.g. `~/cuboai/`.
2. Edit `cubo_go2rtc.sh` (or set the environment variables it reads) with your
   credentials and camera IP:

   ```bash
   export CUBO_UID="YOUR_UID"
   export CUBO_ACCOUNT="admin@YOUR_ACCOUNT"
   export CUBO_PASSWORD="YOUR_PASSWORD"
   export CUBO_CAMERA_IP="192.0.2.10"     # your camera's LAN IP
   ```

3. Point `go2rtc.yaml` at `cubo_go2rtc.sh` (edit the `exec:` path) and start go2rtc:

   ```yaml
   streams:
     cubo:
       - exec:/path/to/cubo_go2rtc.sh#killsignal=SIGTERM
       - ffmpeg:cubo#video=copy#audio=opus   # audio -> Opus for WebRTC; video stays HEVC copy
   ```

4. Open the go2rtc stream page (default `http://<host-ip>:1984/stream.html?src=cubo`)
   and choose WebRTC, MSE, or HLS.

### Running the streamer directly

`cubo_go2rtc.sh` just runs the entry point; you can call it yourself:

```bash
python3 cuboai_stream_video.py \
  --uid YOUR_UID --account admin@YOUR_ACCOUNT --password YOUR_PASSWORD \
  --camera-ip 192.0.2.10 > out.ts
```

Credentials may also come from the environment: `CUBOAI_UID`, `CUBOAI_ACCOUNT`,
`CUBOAI_PASSWORD`, `CUBOAI_CAMERA_IP`.

Key options:

| Option | Meaning |
| --- | --- |
| (default) | MPEG-TS with per-frame PTS, loss recovery, clean-GOP, and muxed AAC audio. |
| `--output-format annexb` | Raw HEVC Annex-B (FRAMEINFO trailer stripped, no container). |
| `--raw` / `--passthrough` | Byte-for-byte raw HEVC passthrough (no recovery, no strip). |
| `--defer-start` | Use the slower, native-matching startup timing instead of fast start. |
| `-v`, `--verbose` | Print periodic stream-health metrics (loss %, recovery, fps, bitrate, gaps, PTS health) to **stderr** — stdout stays the media stream. `--verbose-interval SECS` (default 5) sets the cadence; `--verbose-camera-stats` also folds in the camera's own session stats. |
| `--lib PATH` | Use a native TUTK library instead of pure Python (optional; see below). |

Set `CUBOAI_MUX_AUDIO=0` to ship video-only. Verbose output can also be enabled with
`CUBOAI_VERBOSE=1`; it is written only to stderr, so it never disturbs the stdout media pipe.

## Command-line utility

`cuboai_validate.py` is an **example CLI built on this library** — it shows how the library is used
and exercises its features: JPEG snapshots, synced MP4/HEVC/AAC recording, a full camera **status
card** (sensors, Wi-Fi incl. RSSI, lighting, detection settings, lullaby schedule, session stats,
connected users), camera **controls** (night light, volume, lullabies, detection sensitivity, …), a
**Wi-Fi placement benchmark**, and two-way **talk** (`--talk`, pure backend only). Credentials are
passed the same way as the streamer.

```bash
# Save a JPEG snapshot (needs PyAV)
python3 cuboai_validate.py --uid ... --account ... --password ... \
        --camera-ip 192.0.2.10 --snapshot snap.jpg

# Record 30 s of synced audio+video to MP4 (needs PyAV)
python3 cuboai_validate.py ... --record clip.mp4 --duration 30

# Turn the night light on and set volume
python3 cuboai_validate.py ... --night-light on --volume 40
```

With no capture/control flag it prints a **status card** — sensors, lighting, audio,
detection, network, plus a live **Session stats** block (connection mode/NAT, frame and
keyframe counts, the camera's resend-buffer pressure and send-error counters) and the
list of **connected users**.

Capture commands: `--snapshot`, `--record`, `--record-video`, `--record-audio`,
`--record-av`, `--stream-video`, `--stream-audio`, `--duration`, `--raw`.
Control commands: `--night-light`, `--brightness`, `--volume`, `--timer`, `--play`,
`--stop`, `--list-songs`, `--sleep-mode`, and many more under `--help`.

> **Two-way audio (`--talk`) — WORKING (pure Python, no native lib).** `--talk FILE` plays an
> audio file out the camera speaker; `--talk-loop` (+ optional `--talk-secs N`) streams it
> continuously. It is the av-connect handshake *reversed* on a separate channel: the client opens
> an AV server, the camera logs in and pulls **AAC-LC 16 kHz mono** audio (SPEAKERSTART → grant →
> AAC av-data, during an active live stream). Audio is paced on the exact 64 ms AAC frame grid and
> lost frames are recovered via the camera's selective-repeat (SACK) requests — verified audible
> end-to-end, smooth, and resilient to ~15% induced uplink loss. Transcoding uses PyAV (`pip install av`).
> Use **`--talk-gain MULT`** to set the volume (a linear multiplier — `0.5` = half, `2.0` = double;
> default `1.0`), which is the reliable lever since the camera's speaker level is firmware-managed.
> **Talk is pure-backend only** — the native `--lib` TUTK 4.2.1.1 library omits the 4.3.x av-server
> capability the camera's talk handshake needs, so `--talk` is rejected in `--lib` mode.

### Local DVR playback / rewind (on-camera, no cloud)

The camera keeps a continuous on-device recording (internal storage; ~18–72 h retention depending
on model). You can browse and retrieve past footage **locally over the LAN** — there is no cloud on
this path and no subscription is involved (the paywalled "Moments/Memory" HLS product is a separate
thing this project does not touch).

```bash
# List which past hours have retrievable footage (local time + UTC)
python3 cuboai_validate.py ... --camera-ip 192.0.2.10 --list-recordings --list-hours 6

# Retrieve ~30 s of footage from a past moment to a playable .ts (open in VLC)
python3 cuboai_validate.py ... --playback-from "14:05" --playback-duration 30 --playback-out clip.ts
#   --playback-from accepts 'YYYY-MM-DD HH:MM', 'HH:MM' (today), or relative '5m' (5 min ago);
#   times are LOCAL by default (add --playback-utc for UTC). There is no fast-forward/pause/speed
#   in the protocol — this retrieves a span of recorded footage, not an interactive scrubber.
```

Playback is **per client**: retrieving footage on one session does not interrupt the live feed on
other devices (your phone app, another Home Assistant client, …). A session that is *also* streaming
live has its own live stream briefly replaced by the recorded footage; it is restored automatically
on exit (including Ctrl-C).

### Detection history — LOCAL history, not a live reading

`--history` appends a per-minute **detection history** to the status card, pulled from the same
on-camera DVR manifest. Each field below was verified against real footage:

| field | meaning |
| --- | --- |
| baby in crib | detected in the crib (the app's white timeline bar) vs out |
| motion | still / moving |
| noise (avg + peak) | per-minute average and peak sound level |
| night vision | IR / dark vs daylight — *the official app does not surface this* |
| temperature / humidity | environment |
| sleep / privacy | recording vs privacy mode |

(The manifest also carries a firmware "activity" bit and a couple of structural counters that the app
uses only for internal timeline shading — surfaced raw, unlabelled.) Every value is labelled with its
reading time and **age**, and the whole section is marked *history* — deliberately kept separate from the
live sensors so a stale reading can never be mistaken for "now". `--history-hours N` widens the retrieved
window; `--history-chart FILE` writes a standalone self-contained HTML chart (inline SVG per numeric
series) alongside the terminal ASCII sparklines; `--history-raw-keys` dumps the raw manifest key set.

```bash
python3 cuboai_validate.py ... --camera-ip 192.0.2.10 --history --history-hours 1 --history-chart hist.html
```

> **What is NOT available locally.** The app's headline AI detections — **cry, cough, movement, caregiver
> visit, rollover, and face-covered** — are **cloud** products (server-computed sleep analysis fetched from
> Cubo's REST API with a cloud account token), *not* in the local DVR manifest. A camera-only local client
> like this one cannot reproduce them. What you get locally is the environmental / coarse-state set above —
> including night-vision and peak-noise, which the official app itself doesn't even display.
> **Note:** `--history` performs an RDT manifest pull that does not coexist with a live stream — run it
> when no stream from this same session is active.

### Wi-Fi placement & performance benchmark

`--benchmark` streams while sampling link quality, so you can compare camera
placements at a glance. Each interval prints the camera's Wi-Fi signal, the
client-side packet loss and recovery, frame rate and bitrate, and the camera's own
session stats; an exit summary gives the averages.

```bash
# Sample every 2 s until Ctrl-C
python3 cuboai_validate.py ... --camera-ip 192.0.2.10 --benchmark

# Bounded 30 s run, sampled every 3 s, logged to CSV for A/B location comparison
python3 cuboai_validate.py ... --benchmark 30 --benchmark-interval 3 --benchmark-csv loc_A.csv
```

Lower **loss %** and stronger signal mean a better location. The camera reports both a 0–100 Wi-Fi
**quality percentage** and, for the connected AP, the actual **RSSI (dBm)** plus noise, channel and
frequency — the status card and `get_wifi` surface all of these (so you can place by real RSSI, not
just the percentage), and client loss % remains a good independent proxy. The benchmark only
observes — it never changes any camera setting.

## Using the library from Python

```python
from cuboai_session import get_session

with get_session(uid, account, password, camera_ip="192.0.2.10") as sess:
    jpeg_or_hevc = sess.snapshot()                # one HEVC keyframe
    for kind, data in sess.av_frames(duration=10):
        ...                                       # kind is 'video' or 'audio'
```

### Sensor API (for Home Assistant / other integrations)

`cuboai_sensors.py` exposes the status card and detection-history data as two structured
functions instead of CLI text — this is what `--status`/`--history` render on top of:

```python
import cuboai_sensors as sensors

live = sensors.get_live_sensors(sess)     # instant: temp, humidity, wifi, lighting, firmware, ...
hist = sensors.get_history_sensors(sess)  # ~1 min lag: latest s_log (baby-present, noise, motion, ...)

print(live.temperature_c.value, live.temperature_c.age_s)
print(hist.baby_present.value, hist.baby_present.note, hist.baby_present.age_s)
```

Every value is a small `Reading` object (`value`, `age_s`, `available`, `unit`, `stale`, ...) so a
history reading can't be mistaken for a live one just by reading `.value`. See **[SENSORS.md](SENSORS.md)**
for the full field reference, poll-cadence guidance, and what's settable vs firmware read-only.

## Files

```
cuboai_pure.py          — pure-Python TUTK transport + AV engine (handshake, AV, IOCTL, recovery)
cuboai_transport_py.py  — PureSession wrapper over the engine
cuboai_session.py       — get_session() factory (pure by default; --lib / CUBOAI_LIB opts into native)
cuboai_messages.py      — IOCTL / Kalay message builders + parsers
cuboai_pts.py           — per-frame PTS clock + shared-base A/V timeline
cuboai_mpegts.py        — MPEG-TS muxer (HEVC video + AAC audio)
cuboai_playback.py      — local DVR: on-camera recording discovery, RDT manifest pull, rewind playback
cuboai_stream_video.py  — go2rtc exec entry point: combined A/V MPEG-TS to stdout
cuboai_sensors.py       — public sensor API (get_live_sensors / get_history_sensors) for integrations
cuboai_validate.py      — example CLI: snapshot, record, status card, controls, DVR/history, Wi-Fi benchmark
cuboai_tutk.py          — optional native TUTK backend (ctypes); not needed for normal use
cubo_go2rtc.sh          — go2rtc exec wrapper script
go2rtc.yaml             — example go2rtc stream config
tools/cubo_pcap_decode.py — dev tool: decode a camera pcap into labelled RDT / IOCTL / AV frames
PROTOCOL_RESEARCH.md    — how the LAN protocol works (wire formats + what we learned)
SENSORS.md              — cuboai_sensors.py field reference, poll cadences, settable vs read-only
```

## Optional native backend

By default everything runs in pure Python. If you have a compatible native TUTK
shared library, pass `--lib /path/to/library` (or set `CUBOAI_LIB`) to use it
instead. The library is **not** distributed with this project, and it is not
needed for normal use.

## How it works

CuboAI uses the ThroughTek **Kalay (TUTK)** P2P SDK — the same SDK behind many camera brands
(Wyze, Reolink, …) — and has no official API. The protocol was reverse-engineered by decompiling
the Android app (JADX), hooking the native library with Frida, and analysing UDP packet captures.
Unlike native-library integrations, this project rebuilds the **entire LAN stack** — discovery,
the av-connect handshake, AV streaming, IOCTL control, and selective-repeat retransmission — in
**pure Python**, so no proprietary `.so` is required. Once you have the device credentials, all
control and streaming happen locally on your LAN with no cloud on the path.

## Related projects

- [wyzecam](https://github.com/kroo/wyzecam) — same TUTK SDK, different camera.
- [docker-wyze-bridge](https://github.com/mrlt8/docker-wyze-bridge) — go2rtc + TUTK bridge.
- [getcubo](https://github.com/niruse/cuboai) — the original CuboAI Home Assistant integration that
  inspired this project.

## Notes

- Resolution and frame rate are fixed by the camera firmware and cannot be changed
  from the client.
- On a lossy network, occasional keyframe loss can briefly degrade the picture;
  this is a property of the camera's transport, not a bug in the client.
