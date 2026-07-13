# CuboAI Camera — Protocol Research Notes

Everything we have learned about how the CuboAI baby monitor communicates on the LAN —
what works, what didn't, and the wire formats — so a future contributor can understand the
system without repeating the reverse-engineering. All byte offsets are **plaintext after
`cuboai_pure.inv_transcode()`** unless noted. Verified against a CuboAI **Gen 3** camera
(firmware 3.0.1369); other models/firmware are untested.

> Placeholders: `<cam-ip>` = your camera's LAN IP, `<client-fp>` = the 6-byte client
> fingerprint the library derives from the host MAC. No real credentials/addresses appear
> here.

---

## Overview

CuboAI streams **HEVC (H.265)** video and **AAC-LC** audio and accepts IOCTL control
commands (night light, lullaby, detection features, …). It uses the **ThroughTek (TUTK)
Kalay P2P SDK** for all connectivity.

This project rebuilds the **entire LAN stack in pure Python** (`cuboai_pure.py` /
`cuboai_transport_py.py`) — discovery, the av-connect handshake, AV streaming, IOCTL
control, and selective-repeat retransmission — with **no native library**. (An optional
`ctypes` wrapper over the vendor `.so` exists in `cuboai_tutk.py` but is not needed.) The
earlier "wrap the native library" approach is now just a fallback.

---

## Architecture

### Connection modes

TUTK supports three: **0 = P2P** (UDP hole-punch), **1 = Relay** (via ThroughTek relay
servers), **2 = LAN** (direct UDP on the local network). The phone app connects in **LAN
mode (2)** when on the same network as the camera, and that is this project's target.

### LAN connect uses NO relay and NO crypto

An early theory had connect going through a "51cc HELLO → ECDH → relay/bootstrap-TLS"
path. That is **wrong for LAN**. On the LAN there is **no relay, no ECDH, no TLS** — it is
100 % direct UDP to the camera with `security_mode 0`. None of `RSAEncrypt/AESEncrypt/
AESDecrypt/SHA256` fire; traffic goes only to `<cam-ip>`. (A relay path exists for
off-LAN/WAN access — a region/account-specific `*.iotcplatform.com` host on ports 10240 /
3478 — but it is out of scope here and unreversed.)

### The wire transform — `TransCodePartial` (not encryption)

LAN frames are **not encrypted**. They are the real plaintext run through TUTK's
`TransCodePartial` — a fixed-key block scramble (per-16-byte-block ror/xor/shuffle; the
trailing `len % 16` bytes are XOR'd with the key, and for tail lengths **2/4/8** a `Swap`
byte-permutation is applied on top: `wire_tail = Swap(plain_tail XOR key)`). The key is
the classic TUTK easter-egg string. This is fully reversed and byte-identical to the
native `TransCodePartial` (encode) and `ReverseTransCodePartial` (decode) for every
length: `cuboai_pure.transcode()` / `inv_transcode()`. Decode a post-connect
data-channel frame (IOCTL / video / audio) with `inv_transcode(raw)`. (The tail Swap
applies to those data-channel frames only; the pre-session search probe/ack and the
keepalive/close frames are sent without it — `transcode(..., swap_tail=False)` and
`xor_frame` build those.)

### LAN discovery

Discovery sends an **nL probe** (88-byte fixed constant, magic `6e 6c`) as a UDP broadcast
to `<subnet>.255:32761`; the camera answers with an **NO response** (magic `4e 6f`) from a
dynamic port. **Gotcha:** on Linux/VMs the native library only broadcasts to the subnet
broadcast address — if the host doesn't forward broadcast to the camera, connect hangs.
The pure-Python path sends the probe **directly to `<cam-ip>:32761`** (unicast also works),
sidestepping the problem; the native fallback ships an `LD_PRELOAD` shim that rewrites the
broadcast `sendto()`.

---

## Connect handshake (pure Python)

The handshake is: **discover (nL probe → NO) → av-connect → `0x2041` grant → session ready.**

The per-session secret is **`R = GenShortRandomID`** (a 16-bit value) carried at plaintext
`[56:58]` of the probe/ACK, plus the 6-byte **client fingerprint `<client-fp>`** at
`[58:64]` (derived from the host's first non-loopback MAC via a fixed byte permutation).
The camera reads `(R | fingerprint)` as the client-random-id, keys a pre-session on it, and
on the ACK drives the session to "connected" (`LAN_SEARCH_R_3 → _SetSendPath`).

The av-connect packet (598 bytes) is **not** "plaintext XOR a 16-byte key" — it is the real
av-connect (which contains the account + password in the clear) run through
`TransCodePartial`. The camera validates only the **16-byte header**, which must decode to
the camera's NO response; for a chosen NO there is a unique `R` that yields the required
header (recovered with a 64K lookup table, `build_R_table`). A 4-byte "tag" region is just
`rand()` and is **not** validated — there is no timestamp or nonce authenticator.

> The single historical blocker (≈8 sessions) was a builder bug: the probe/ACK are
> `transcode`'d (not the simpler `xor_frame`), and the code had randomised the wire region
> that *encodes* R+fingerprint, handing the camera garbage so the session never reached
> "connected" and the AV was silently dropped.

---

## The AV / data channel

Once connected, **all** AV traffic (IOCTL, video, audio) rides the IOTC LAN data channel as
`transcode`'d frames. Frame type **`0x0407` = client→camera**, **`0x0408` = camera→client**.
The embedded "session id" at `[12:14]` and `[20:22]` is just **R**; the client fingerprint
sits at `[22:28]`. There is no separate session-id handshake.

### Frame header

```
[0:2]   04 02                 frame type (07 04 client→cam / 08 04 cam→client at [8:10])
[4:6]   u16 len-16            payload length
[6:8]   u16 packet counter    bumps on EVERY send, including retransmits
[12:14] u16 R                 session id == client-random-id from connect()
[20:22] u16 R                 (repeated)    [22:28] <client-fp>
[28]    sub-type             0x0C = DATA   0x09 = ACK/SACK   0x0A/0x0B = clock frames
[32:34] u16 relseq           the SENDER's own reliable-frame counter (DATA and ACK alike;
                             +1 per new reliable frame, unchanged on a retransmit)
[34:36] u16                  0xFFFF on DATA frames, 0 on ACK frames
```

`[32:34]` is the **sender's reliable-frame sequence**, not a cumulative ACK. Each side runs
it 0,1,2,… across both its DATA and ACK frames (so a side's DATA frames show gaps where its
ACKs went). An earlier "dual-channel C/D bitmap" reading of this field was wrong.

### IOCTL request / response (sub-type 0x0C)

`build_ioctl_data(R, seq, relseq, frmno, io_type, payload)`:

```
[6:8]   seq        packet counter
[32:34] relseq     sender reliable-frame seq
[34:36] 0xFFFF     DATA marker
[45]    0x70       [48] 0x01
[46:48] frmno      IOCtrl FrmNo (0,1,2,… one per request)
[52:54] u16        avlen = 4 + len(payload)
[56:58] frmno      mirror == the AV message-index used for reassembly
[64:68] u32        io_type
[68:]              payload
```

Responses have the same shape with `io_type` at `[64:68] == request_io | 1` (GET request
even → response odd). The response payload is `dec[64 : 64 + avlen]`.

### Message index / reassembly (`[46:48]` / `[56:58]`)

On **camera AV DATA** frames, `[56:58]` is the **AV message-index** — the reassembly key.
One access unit (a whole HEVC picture, or one AAC-ADTS frame) is split across **all DATA
frames sharing the same `[56:58]`**; the next AU uses the next index. Reassembly model:
accumulate fragments per index, finalise an AU when a higher index arrives (robust to
out-of-band system frames interleaved mid-keyframe). Classify finished units by content:
HEVC starts `00 00 00 01`, AAC-ADTS starts `FF Fx`, everything else (system/login) is
skipped.

> The index is a **16-bit counter that wraps at 65536**. The reassembly accept-window must
> be compared **modularly** (`(idx - done_upto) & 0xFFFF`) or a long-running stream can stall
> at the wrap (~hourly at ~15 AU/s) — a known hardening item for 24/7 use.

---

## Video — HEVC

The camera streams **HEVC / H.265** Annex-B. The payload bytes are `dec[64 : 64 + avlen]`
(**not** `dec[68:]` — the Annex-B start code `00 00 00 01` sits at `[64:68]`).

- **Keyframe** AU: NAL **VPS(32) @0, SPS(33) @28, PPS(34) @77, IDR(19) @88**, starting
  `00 00 00 01 40 01 0c 01 ff ff …`. Spans ~35–70 DATA fragments sharing one message-index.
  `snapshot()` returns the first AU whose first 5 bytes are `00 00 00 01 40` (VPS).
- **P-frames**: a single NAL **type 1**, `00 00 00 01 02 01 d0 00 …`. The stream is
  **refs=1** (no B-frames), so DTS = PTS and one missing fragment greys its GOP tail.

Resolution and frame rate are **fixed by firmware**: `IPCAM_SETRESOLUTION` (0x00FF) is sent
at stream-start but the camera ignores the parameter (the app changes resolution through a
proprietary native `Client` class, not a standard IOCTL).

---

## Audio — AAC-LC

Audio is **AAC-LC in ADTS**, one self-contained ADTS packet per AV message: **16000 Hz,
mono, AAC-LC**, ~448 bytes / 1024 samples / **64 ms** per frame (≈15.6 frames/s). On AV DATA
frames `[64:66]` is the `FF F1` ADTS sync word (so the "io_type @[64:66]" field is a real
io_type only on IOCTL *response* frames, never on AV-data frames). The frame-info codec id
`0x0088` does **not** map to a standard TUTK codec id — identify audio from the ADTS header.

**Audio requires a video drain.** The camera multiplexes audio and video over the one AV
channel and stops sending **both** if video isn't being consumed. `av_frames()` /
`audio_frames()` drain both from a single reader loop. Audio is requested by default — the
`0x0300` stream-start IOCTL *is* `IPCAM_AUDIOSTART`.

---

## Combined A/V muxing and PTS

`cuboai_mpegts.TSMuxer` muxes the interleaved HEVC + AAC into a single **MPEG-TS** (separate
PIDs). `cuboai_pts.AVTimeline` gives both tracks a **shared, drift-free timeline** from the
camera's own clock (~0.2 ms/min A/V drift):

- **Video PTS** comes from the per-frame FRAMEINFO `timestamp_ms` (millisecond resolution,
  interpolated when a frame's timestamp is garbage — ~10 % are).
- **Audio PTS** comes from the second-resolution `ts_sec` plus an intra-second `frame_index
  × 64 ms`, **re-anchored every new second**. The re-anchor makes a lost audio AU a
  self-correcting ≤1 s gap instead of cumulative A/V drift.

> **Do not "fix" the audio sub-index ±56 ms oscillation.** Re-quantising 15.6 fr/s audio
> onto the 1 s `ts_sec` grid makes the audio PTS lead true time by 0..56 ms (mean −27.5 ms).
> It is **bounded and 8 s-periodic, not drift**, monotonic, and sub-perceptual (and re-timed
> away by WebRTC/Opus). It is the intentional price of the loss-resilient re-anchor; removing
> it reintroduces drift. A `DO NOT "FIX"` banner in `cuboai_pts.py` records the full reasoning.

For WebRTC, go2rtc transcodes AAC → Opus while the video stays a passthrough copy.

---

## Reliability: ACK, "arming", and retransmission

This is the part that took longest and was revised most. The AV phase uses three control
sub-types, all decodable with `inv_transcode`:

### `0x09` — SACK (a resend-REQUEST, not a cumulative ACK)

The host→cam `0x09` (52 bytes) is a **missing-fragment list**: the camera resends **exactly**
the fragments the SACK names.

```
[36:38] u16 C        una — lowest unacknowledged fragment (the low edge; HOLDS at a hole
                     until that hole's resend fills it)
[38:40] u16 D        high edge / received-up-to
[42:44] u16 count    number of SACK entries
[50:]   u16[count]   each entry = (missing_frag − C)
```

Encoding gotcha: **`count < 2` carries no entry** (the camera reads `[50:52]` as a
timestamp), so a single lone hole must be padded to `count ≥ 2`. Listing **received**
fragments instead of **missing** ones (an earlier reading) makes the camera waste resends
→ ~0 % recovery; listing missing fragments recovers ~76–84 % of losses at ~1.07×
redundancy (native parity). `_selective_ack` / `_compute_holes` implement this.

> An even earlier (session-12) model read a cumulative "data-ack" u32 at `[40:44]`; that was
> a workable approximation that the C/D + SACK model superseded.

### `0x0a` / `0x0b` — the clock echo that "arms" retransmission

The retransmit commitment is gated by a **client wire field**: the camera puts its **ms
clock** in cam→host `0x0a [36:38]`, and the reliable peer must **echo it back** in host→cam
`0x0b [36:38]`. Sending your own (e.g. Unix-epoch) value instead leaves the camera
withholding commitment — the perennial "resend floor". Echoing the camera's clock makes a
pure-Python session **arm** (retransmission engages). `_echo_cam_clock` (env
`CUBOAI_ECHO_CAMCLOCK`, default on) does this. *(This corrected a long-lived earlier
conclusion that the gate was "firmware-internal" and not client-reachable.)*

### Decode band under loss — client-unclosable below the network

Residual grey frames at ≥1 % loss are **not** a client bug. They are keyframe-loss-exposure
gated (a ~69-fragment keyframe loses ≥1 fragment with probability ≈ 1−(1−loss)^69) and
HEVC-GOP-cascade amplified (one incomplete mid-GOP AU greys its ~42-frame GOP tail on a
refs=1 stream). Three hard walls confirmed: FEC is **off** in this deployment and not
client-forceable; the camera **declines** to resend an already-delivered fragment; the
per-fragment resend cap is ~2–3. Real levers (lower network loss / vendor FEC + smaller
keyframes / a downstream error-concealing decoder) are out of client reach. The client-side
mitigations that *do* help — dynamic reassembly grace, in-order never-skip sealing,
clean-GOP gating — are in `cuboai_pure.py` (gated, with inline notes on the limits).

---

## Two-way audio (talkback) — reverse-engineered, UNTESTED

From the decompiled app: talk uses **SPEAKERSTART `0x350` / SPEAKERSTOP `0x351`**
(`SMsgAVIoctrlAVStream{channel}`, 8 bytes), an **AAC-LC** uplink (codec id `0x88`, **not**
G.711 as older TUTK research assumed), and a 24-byte `TalkFrameInfo` (codec id @0, timestamp
@12). It requires an **active LiveStreamState** — `avTalkId` resolves to `avLiveId` (same
channel, not a separate one) — so talk audio must be sent *during* a live stream via
`avSendAudioData`. This path is implemented (`send_audio_file`, transcoding to AAC-LC via
PyAV) and accepted on the wire, but **not yet confirmed audible from the camera speaker** —
treat as experimental.

---

## Other control payloads

- **Sleep / privacy mode** (`0x092A` get / `0x092C`... see table): a **96-byte** payload —
  `[0:4]` Unix timestamp LE, `[4:88]` zeros, `[88]` on/off flag (1/0), `[89:96]` zeros. (The
  initial "12-byte, flag @4" guess was silently accepted but did nothing.)
- **Lullaby schedule — read** (`get_lullaby_schedules`, io `0x098E`/resp `0x098F`): entries at
  **stride 100 from offset 8** — `enable@+0`, `name@+4` (40 B), `uuid@+44` (44 B),
  `days_mask@+88` (bitmask, `0x7f` = Mon–Sun), `start_hour@+89`, `start_minute@+90`,
  `ai_autoplay@+91`, `duration@+92` (LE32 **seconds**), `created@+96`. The `uuid` maps to a
  song via the app's lullaby catalog.
- **Lullaby schedule — write** (`SET_LULLABY_SCHEDULE`, io **`0x0990`**/resp `0x0991`,
  `build_set_lullaby_schedule_entry`): adds/edits/deletes **one** row (not the whole list) and
  is **NOT** a byte mirror of the read. The 148-byte payload is `id@0` (LE32, echoed) ·
  `action@4` (LE32: **0=ADD/edit, 1=DELETE**) · a **140-byte** entry blob @8. The entry differs
  from the read: it inserts a 40-byte `newName@+44` after `name`, so `uuid` moves to `+84`,
  `nMDay@+128`, `start_hour@+129`, `start_minute@+130`, `nAi@+131`, `duration@+132` (LE32
  seconds), and the trailing 4-byte `created` slot (`+136`) is **left zero** (the APK `toBytes`
  computes it but discards it). The camera **keys rows on `name`**: ADD-create uses
  `name`=display name with `newName`=`""`; ADD-edit uses `name`=existing name + `newName`=new
  name; DELETE sends a fresh entry with only `name` set (`enable` defaults 1). `nMDay` bit
  `0x80` = "use local time" (start time is local wall-clock); the low 7 bits are the day mask.
  RE'd from the APK smali; offline round-trip vs the read is field-faithful, but the **live
  write is still UNTESTED** — the CLI gates it behind `--i-understand-this-is-unsafe`.
- **Standard response prefix**: every `SMsg*Resp` begins `{id@0, result@4, …}` — a `result`
  word (0 on success) sits at offset 4 of most GET responses.
- **Wi-Fi** (`get_wifi`): SSID/IP/MAC plus, for the connected AP, **RSSI (dBm) @0xa0**,
  noise @0xa4, channel @0x94, frequency @0x98, quality % @0x9c. (`get_hw_control.wifi_strength`
  is a separate 0–100 quality percentage, not dBm.)

---

## IOCTL type codes

Discovered from the decompiled app (JADX, app v2.23.2) and confirmed/extended live. "read"
= GET verified; SET codes select a write op and are **not** safe to fire blindly even with an
empty payload. The CLI hides the untested/destructive SETs behind
`--i-understand-this-is-unsafe`.

| Req | Resp | Name | Status |
|----|----|----|----|
| 0x00FF | — | IPCAM_SETRESOLUTION | confirmed (param ignored) |
| 0x01FF | — | IPCAM_START | confirmed |
| 0x0300 | — | IPCAM_AUDIOSTART (stream-start) | confirmed |
| 0x0350 / 0x0351 | — | SPEAKERSTART / SPEAKERSTOP (talk) | RE'd, untested |
| 0x0908 | 0x0909 | GET_TEMP_HUMIDITY | read |
| 0x0934 | 0x0935 | GET_SESSION_STATS | read |
| 0x0946 | 0x0947 | GET_USER_LIST | read |
| 0x090E | 0x090F | GET_WIFI (incl. RSSI) | read |
| 0x0918 | 0x0919 | GET_CRY_DETECT / SET | read / gated |
| 0x0930 | 0x0931 | GET_SLEEP_SAFETY | read |
| 0x0938 | 0x0939 | GET_SLEEP/PRIVACY_MODE | confirmed |
| 0x093A | 0x093B | SET_SLEEP/PRIVACY_MODE | confirmed |
| 0x0960 | 0x0961 | GET_UPDATE_INFO | read |
| 0x0964 | 0x0965 | GET_LULLABY_INFO | confirmed |
| 0x0982 | 0x0983 | SET_LULLABY_ACTION (play/stop) | confirmed |
| 0x0984 | 0x0985 | GET_LULLABY_VOL_DURATION | confirmed |
| 0x0986 | 0x0987 | SET_LULLABY_VOL_DURATION | confirmed |
| 0x0988 | 0x0989 | GET_LULLABY_SCHEDULES | read (decoded) |
| 0x0990 | 0x0991 | SET_LULLABY_SCHEDULE (add/edit/delete one row) | RE'd, gated (untested live) |
| 0x0994 | 0x0995 | GET_COUGH_SETTING / SET | read / gated |
| 0x099A | 0x099B | GET_CONNECTED_USER | read |
| 0x1100 | 0x1101 | GET_NIGHT_LIGHT_ON_OFF / SET | confirmed |
| 0x110A | 0x110B | GET_STATUS_LIGHT_ON_OFF / SET | read / gated |
| 0x110E | 0x110F | GET_LIGHT_STYLE (brightness/RGB) / SET | read / RGB unverified |
| 0x1300 | 0x1301 | GET_HW_CONTROL (temp/humidity/wifi%) | read |
| 0x1302 | — | GET_MAT_CONFIG / SMART_TEMP (accessory) | untested (no accessory) |

*(IOTYPE values above are grouped by function; the exact decimal codes and the full 32-GET
surface live in `cuboai_messages.py` `GET_METHODS`. Detection-zone / "baby gate" /
firmware-update / format / set-password / set-wifi codes are deliberately **not** wired —
they are destructive and out of scope.)*

### Connected-user response

Type `GET_CONNECTED_USER`, 1000 bytes, up to 3 recent records from offset 128, each 120 B:
`email@0` (64 B), `conn_type@64` (LE32: 0=P2P/1=Relay/2=LAN), `unix_ts@68`, `session_uuid@72`
(45 B). These are recent session *history*, not currently-active connections.

---

## Diagnostics

`TUTKDirectSession.get_stats()` returns a cumulative read-only snapshot (frags recv/lost/
loss %, resend req/recovered, AUs video/audio/incomplete, keyframe-incomplete, emitted bytes,
gap now/max, PTS health, rtt EWMA) — lock-free (the reader thread is the sole writer).
`get_during_stream(name)` lets the reader thread issue a GET **during** a live stream without
a second socket sender racing it (the reader is the sole sender; a caller-side lock serialises
the single inject slot — verified safe under concurrent injects). Both feed the CLI's `--verbose` health
lines and `--benchmark` Wi-Fi-placement output.

---

## App source analysis

The APK decompiles with [JADX](https://github.com/skylot/jadx); the relevant logic is the
camera command factory + the `SMsg*` message classes (their `toBytes`/parsers, and
`BytesUtil.byteArrayToLeInt`/`leIntToByteArray`). The actual P2P session lifecycle lives in a
native `cloud.yunyun.cubo.camera.client.Client` class (not in the Java source) — which is why
the wire protocol had to be recovered by hooking the native library (Frida) and decoding UDP
captures rather than read from source.

---

## Copyright and legal notes

- **ThroughTek TUTK SDK** (`libIOTCAPIs_ALL.so` etc.): proprietary, owned by ThroughTek Co.
  Ltd. Not redistributable; must be extracted by the end user (only needed for the optional
  native backend — the pure-Python path needs no `.so`).
- **CuboAI app**: proprietary, owned by CuboAI Inc. Decompilation for personal
  interoperability is permitted in many jurisdictions (EU Software Directive Art. 6, US DMCA
  §1201(f)). The decompiled source is not redistributed.
- **This integration code**: original work, freely shareable. The IOCTL codes and protocol
  facts here are facts and not copyrightable.
- The **TransCode key** is a published ThroughTek protocol constant (needed to decode the
  wire format), not a secret.
- **Lullaby UUIDs** identify audio content owned by CuboAI / third parties; the UUIDs are not
  copyrightable but the audio they reference is.

---

## Status & future work

- **LAN connect + AV stack** — ✅ done in pure Python (connect, IOCTL, snapshot, HEVC video,
  AAC audio, combined A/V mux, loss recovery/arming). The default backend.
- **Detection-feature SETs** (cry/cough sensitivity) — implemented, gated.
- **Two-way audio** — RE'd (SPEAKERSTART + AAC-LC uplink during a live stream), implemented,
  **untested by ear**.
- **Detection zones / "baby gate"** — complex struct, not implemented.
- **Off-LAN / WAN access** — would need the relay + 51cc bootstrap path, unreversed, out of
  scope for a LAN tool.
- **Accessory configs** (breathing mat, Bluetooth smart-thermometer) — codes mapped but
  untestable without the hardware; SETs gated.
- **Home Assistant component** — the example CLI exercises every capability; a full HA
  integration would wrap them as sensor/light/media_player/camera entities.
