# SENSORS.md — the public sensor API (`cuboai_sensors.py`)

For anyone wiring CuboAI camera data into Home Assistant (or any other integration) without
re-deriving the wire protocol. Two calls, both structured data — no printing, no CLI parsing.

```python
from cuboai_session import get_session
import cuboai_sensors as sensors

sess = get_session(uid, account, password, camera_ip=camera_ip)
sess.connect()

live = sensors.get_live_sensors(sess)          # instant — temp, humidity, wifi, lighting, ...
hist = sensors.get_history_sensors(sess)        # ~1 min lag — latest s_log (baby-present, noise, ...)

print(live.temperature_c.value, live.temperature_c.unit, live.temperature_c.age_s)
print(hist.baby_present.value, hist.baby_present.note, hist.baby_present.age_s)
```

Every value either call returns is a `Reading`:

```python
Reading(value, source,       # 'live' | 'history'
        ts_utc, age_s,       # measurement time (UTC) + seconds-old AT THE MOMENT OF THE CALL
        available,           # do we have a real value at all
        unit=None, stale=False, note=None)
```

`ts_local` is a derived property (`reading.ts_local`) for display. `age_s`/`ts_utc` are computed
once, when the call returns — if you hold onto a `Reading` and check it later, its age is stale
information about a moment in the past; re-fetch to get a current one.

## 1 — `get_live_sensors(sess, *, cache=None) -> LiveSensors`

Instant GET reads. No RDT, no DVR pull. `age_s` reflects only this call's own round trip
(effectively 0 — this is the "no lag" side of the API).

| Field | Value shape | Notes |
|---|---|---|
| `temperature_c` | float, °C | |
| `humidity_pct` | float, % | |
| `wifi` | dict: `quality_pct, ssid, ip, mac, rssi_dbm, noise_dbm, channel, frequency_mhz, radio_quality` | some sub-keys read `None` if this firmware doesn't populate them (e.g. `rssi_dbm`) — `quality_pct` (0–100) is the reliable signal metric |
| `sleep_mode` | bool | privacy/sleep mode; feed suspended while `True` |
| `night_light` | dict: `on (bool), brightness (0–100)` | **settable** via the CLI's `--night-light`/`--brightness` (SET_HW_CONTROL / SET_LIGHT_STYLE) |
| `status_light` | bool | **READ-ONLY** — see §3 |
| `firmware` | dict: `version, update_available, latest_version` | |
| `detection_config` | dict: `cry{enabled, ai_enabled, sensitivity, sensitivity_label}, cough{enabled, mode, sensitivity, sensitivity_label}, sleep_safety_enabled` | settable via the CLI's `--cry-detection`/`--cough-detection`/etc. |
| `baby_presence_alert_configured` | bool | **READ-ONLY** — see §3 |
| `sleep_safety_status` | dict: `status, active, remaining_time, duration` | live safe-sleep detection state |
| `feature_bitmap` | tuple of ints | per-feature capability flags; ordering not reverse-engineered — inspect, don't build entities keyed to a specific index |

A single field's GET can fail (camera busy, one dropped ioctl) without failing the whole call:
that field alone degrades to its last-known-good value (age grown, `stale=True`) instead of
going `None`. A field that has *never* succeeded reports `available=False`.

## 2 — `get_history_sensors(sess, *, hours_back=3, window=False, window_hours=1, cache=None) -> HistorySensors | HistoryWindow`

Reads the on-camera DVR's `s_log` manifest over RDT — a genuinely different data source from
§1, not just a slower version of it. **~1 minute lag**: the "growing" current hour serves after
the fact, so a `get_history_sensors()` call right now describes the camera's state roughly a
minute ago, not this instant.

Two shapes from one function:

- **`window=False` (default) — the sensor case.** Returns `HistorySensors`: the single freshest
  retrievable per-minute reading, every field a `Reading` (`source='history'`). This is what a
  HA sensor/binary_sensor should poll.
- **`window=True` — the charting/statistics case.** Returns `HistoryWindow`: a merged,
  minute-deduped list of `HistoryPoint` (bare values, no per-field `Reading` wrapper — the window
  itself carries `available`/`stale`/`fetched_at`) across `window_hours`.

`HistorySensors` fields — all footage/app-cross-checked (see confirmed-vs-inferred, §5):

| Field | Value | `.note` |
|---|---|---|
| `baby_present` | raw `bp` (1/2) | `'in crib'` / `'not in crib'` |
| `noise` | raw `na` (0–100) | `'elevated (>=60)'` when applicable |
| `motion` | raw `mo` | `'still'` / `'moving'` / `'strong (N)'` |
| `wellbeing` | raw `bw` | opaque firmware activity bit — see §5, don't over-interpret |
| `baby_event` | raw `be` | rare; unfired across the reference 72h capture |
| `privacy` | raw `pr` | `'sleep/privacy mode'` / `'recording'` |
| `temperature_c` / `humidity_pct` | float | historical te/hu, distinct from the live §1 reading |

### Why the live/history split is load-bearing

This is a baby monitor. `baby_present` shown as *now* when the reading is 40 minutes old is the
concrete failure mode this API is built to make structurally impossible: every history value
carries its own `age_s`, computed fresh at call time — an integrator has to actively throw that
field away to render it as live. Don't build an entity that surfaces `.value` without also
surfacing (or at least gating on) `.age_s`/`.available`/`.stale`.

### Graceful degradation + pacing (handled internally — you don't need to know the details)

- A failed or paced-out pull returns the **last-known-good** reading/window with a **grown**
  `age_s` and `stale=True` — never an exception, never a value that looks fresher than it is,
  never a silent zero.
- Repeated calls inside a short internal window (a few seconds) reuse the last pull instead of
  hitting the camera again — polling faster than that just gets you the same (aging) data back,
  it does not wedge the camera. You do not need to know about `CUBOAI_LIST_PACE_S`, conn_id
  release timing, or the growing-hour behaviour; the API absorbs all of that.

## 3 — Settable vs firmware-owned vs read-only

| Field | Status |
|---|---|
| `night_light.on` / `.brightness`, `detection_config.cry/cough.*` | **settable** (via the existing CLI SET flags / `cuboai_messages.build_set_*`) |
| `status_light` | **read-only telemetry.** `SET_STATUS_LIGHT` is accepted by the camera but has no observed effect — don't build a switch for it. |
| `baby_presence_alert_configured` | **read-only telemetry, safety-relevant.** The camera accepts a `baby_presence_alert` write inside `SET_SLEEP_SAFETY` (`result=0`, looks like success) and then silently does not apply it (wire-confirmed 2026-07-25) — there is also no corresponding toggle in the official app. **Do not expose this as a switch entity**: a control that reports success and does nothing is worse than no control. |
| — | Never infer on/off state from a SET response. `SET_NIGHT_LIGHT_ON_OFF_RESP` is 12 zero bytes (`{id, result, reserved}` — no state echo; wire-proven 2026-07-25). The only correct pattern, and the one this module uses internally, is to read the state back via a GET. |

## 4 — Suggested poll cadences

| Call | Cadence | If you poll faster |
|---|---|---|
| `get_live_sensors` | 15–60s is plenty; it's a handful of lightweight GETs | No camera-side penalty, but no new information either — values just repeat |
| `get_history_sensors(window=False)` | 30–60s (matches the ~1 min DVR lag — polling faster gets you the same minute back) | Internally paced; you get cached data with a grown age, not a camera hammering |
| `get_history_sensors(window=True)` | Once per chart refresh (minutes), not a tight poll loop — each hour of window costs a real RDT pull | Same internal pacing/fallback as above, but each hour is a heavier operation than the live GETs |

## 5 — Confirmed vs inferred

- **`bp` (baby_present) is ground-truth confirmed** against the decompiled app
  (`TimelinePageAdapter.getMediaFilePaint`) and cross-checked against real recorded footage.
  `na`/`mo`/`bw`/`be`/`pr` are footage-cross-checked too, though `bw` in particular is an
  **opaque firmware activity bit** — the official app's only use of it is tinting a timeline
  tick, not a documented "wellbeing" score; treat its `.note` as a hint, not a diagnosis.
- **`ni`, `nm`, `se`, `ve` are captured by the underlying manifest but intentionally NOT
  exposed here.** They're real firmware fields the official app itself never reads (Room
  storage plumbing only — no UI/query/telemetry consumer), and their semantics, while
  plausible (`ni`=night-vision/IR, `nm`=noise peak, `se`=per-minute counter, `ve`=format
  version), are not to the same confirmation bar as the fields above. **Do not surface them as
  HA entities.** If you need to inspect them for diagnostics, use
  `cuboai_playback.raw_manifest_keys()` / `cuboai_validate.dump_history_raw_keys()` — the
  library's raw-key inspection path, deliberately separate from this sensor API.

## 6 — Cloud-only features (not reachable locally — don't look for them here)

"Caregiver Present" and the live cry/cough/movement *alerts* (the ones the app pushes in
real time) are **not** in the local `s_log` manifest at all. They ride a separate Region-events
feed that requires a Cubo cloud account and isn't pulled by this library. Don't spend time
looking for them in `get_history_sensors()` output — they structurally aren't there.

## 7 — What changed (for `niruse/cuboai` — this vintage vs. what you already integrated)

Since your fork last picked up this library, three things landed:

1. **DVR playback** (`cuboai_playback.PlaybackSession`) — seek/replay the camera's own
   18–72h on-camera recording buffer, no cloud, no SD card.
2. **Detection history** (`s_log`) — this file's `get_history_sensors()`, ~1-min-fresh
   per-minute baby-present/noise/motion/etc., the thing your HA fork doesn't have wired up yet.
3. Two **silent no-data bug fixes** worth knowing which vintage you're bundling, because both
   could previously present as "go2rtc never got an answer" with no error (cf.
   `niruse/cuboai` issue #83):
   - `CUBOAI_IDX_SEED` (default ON) — every AV read after the *first* in a session used to emit
     zero access units, silently, because of a per-session vs per-reader index mismatch. Hits
     exactly the "start a stream, it works, restart it, it doesn't" shape.
   - The SIGTERM teardown fix — a clean `disconnect()` now runs on SIGTERM (go2rtc's normal
     stop signal), where it previously could leave state behind across a stop/restart cycle.

Both are default-on, additive, and covered by `run_offline_tests.sh` — no config changes needed
to pick them up, just the current tree.
