#!/usr/bin/env python3
"""
cuboai_validate.py — CuboAI camera validation and control tool.

Defaults to the pure-Python session — no native library, and the library is NOT auto-discovered.
Pass --lib (or set CUBOAI_LIB) to explicitly opt into the native TUTK backend.

Capture commands run under the same production A/V profile as cuboai_stream_video (FRAMEINFO
strip + loss recovery), so the playable outputs (--snapshot, --record) are clean by default.
Add --raw to grab the unprocessed Annex-B bitstream (trailers present, no recovery) for inspection.

Usage:
    python3 cuboai_validate.py --uid YOUR_20CHAR_UID_HERE \\
                               --account admin@YOUR_ACCOUNT \\
                               --password YOUR_PASSWORD \\
                               --camera-ip 192.0.2.10 --record clip.mp4
                               [--lib /path/to/libIOTCAPIs_ALL.so]   # native opt-in

Capture (playable by default; --raw = unprocessed bitstream):
    --snapshot FILE          Save a JPEG snapshot (one keyframe → PyAV → JPEG)
    --record FILE            Record muxed audio+video to a playable .mp4 (camera-clock A/V sync)
    --record-video FILE      Record the raw HEVC video element to FILE
    --record-audio FILE      Record the AAC-ADTS audio element to FILE (e.g. audio.aac)
    --record-av BASE         Record both elements raw, separate: BASE.hevc + BASE.aac
    --stream-video           Stream HEVC to stdout (pipe to: ffplay -f hevc -i -)
    --stream-audio           Stream raw AAC-ADTS to stdout
    --duration SECS          Capture duration (default 10)
    --raw                    Unprocessed bitstream (no FRAMEINFO strip / no recovery)
    --talk FILE              Send audio to the camera speaker (two-way talk; PURE backend only)
    --talk-loop              Loop --talk continuously   --talk-secs N  stop after N seconds
    --talk-gain MULT         Talk volume multiplier (1.0=unchanged, 0.5=half — speaker_level is firmware-locked)

Control:
    --night-light on|off / --brightness 0-100 / --volume 0-100 / --volume-ramp SECS /
    --timer repeat|30min|60min /
    --play NAME / --stop / --sleep-mode on|off / --list-songs.  See --help for the full SET
    command group (night-vision, cry/cough detection, sleep-safety, comfort range, …).
    --no-status              Skip the status read (status + AV coexist since session 24)

Environment: CUBOAI_LIB, CUBOAI_UID, CUBOAI_ACCOUNT, CUBOAI_PASSWORD, CUBOAI_CAMERA_IP
"""
from __future__ import annotations
import argparse
import os
import platform
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cuboai_session import get_session   # auto-selects TUTKSession (--lib) or PureSession
from cuboai_stream_video import apply_env_profile, _clamp_env_knobs, _env_float   # shared A/V env profile + knob clamp
import cuboai_sensors   # public sensor API — single source for live GETs + s_log history (see cuboai_sensors.py)
from cuboai_messages import (
    build_get_hw_control,
    build_get_light_style,
    build_get_lullaby_vol_duration,
    build_set_night_light,
    build_set_light_style_brightness,
    build_set_lullaby_play,
    build_set_lullaby_stop,
    build_set_lullaby_vol_duration,
    build_get_cry_detect,
    build_get_sleep_safety_status,
    build_get_sleep_safety_setting,
    build_get_sleep_mode,
    build_set_sleep_mode,
    build_get_cough_setting,
    build_get_connected_users,
    HWControl,
    LightStyle,
    LullabyVolDuration,
    LullabySchedule,
    IOTYPE_USER_GET_HW_CONTROL_RESP,
    IOTYPE_USER_GET_LIGHT_STYLE_RESP,
    IOTYPE_USER_GET_LULLABY_VOL_DURATION_RESP,
    IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP,
    IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_REQ,
    IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_RESP,
    IOTYPE_USER_GET_UPDATE_INFO_REQ,
    IOTYPE_USER_GET_UPDATE_INFO_RESP,
    LULLABY_TIMER_REPEAT,
    LULLABY_TIMER_30MIN,
    LULLABY_TIMER_60MIN,
    LULLABY_CATALOG,
    get_song_name,
    GET_METHODS,
)


def find_song(query: str):
    q = query.lower().strip()
    for uuid, (key, name, category) in LULLABY_CATALOG.items():
        if q in name.lower() or q in key.lower():
            return uuid, name
    return None


# ── Status output ────────────────────────────────────────────────
# Every GET method (cuboai_messages.GET_METHODS) is read into one dict, then a
# clean human-readable status card is composed from the decoded fields, grouping
# related items and cross-referencing where one feature spans two responses
# (e.g. the lullaby song comes from get_lullaby but the volume from
# get_lullaby_schedule). See cuboai_messages for the per-field wire decoding.

_W = 16  # label column width


def _onoff(v):
    return 'ON' if v else ('OFF' if v is not None else '—')


def _read_all(sess) -> dict:
    """Read every GET method into {name: parsed_dict}. Failed/empty reads → None.

    Delegates to cuboai_sensors._sweep_gets — the one place that iterates GET_METHODS, shared
    with cuboai_sensors.get_live_sensors() so the full raw-response CLI view (this) and the
    curated, metadata-wrapped public API (get_live_sensors) can never drift on the wire reads."""
    return cuboai_sensors._sweep_gets(sess)


def _row(lines: list, label: str, value) -> None:
    if value is None or value == '':
        return
    lines.append(f"    {(label + ':'):<{_W}} {value}")


def _render_status(d: dict) -> str:
    """Compose the clean status card from the decoded GET responses (d)."""
    G = lambda name, key, default=None: (d.get(name) or {}).get(key, default)
    L = []
    bar = "  " + "═" * 44
    L.append("")
    L.append(bar)
    L.append("    📷 CuboAI Camera Status")
    L.append(bar)

    # ── Sensors ──────────────────────────────────────────────────
    temp = G('get_hw_control', 'temp_c')
    humid = G('get_hw_control', 'humidity_pct')
    wifi = G('get_hw_control', 'wifi_strength')
    ssid = G('get_hw_control', 'ssid') or G('get_wifi', 'ssid')
    t_lo, t_hi = G('get_hw_policy', 'temp_low_c'), G('get_hw_policy', 'temp_high_c')
    h_lo, h_hi = G('get_hw_policy', 'humi_low_pct'), G('get_hw_policy', 'humi_high_pct')
    sec = []
    if temp is not None:
        note = f"  (comfort {t_lo}–{t_hi}°C)" if t_lo is not None else ""
        _row(sec, "Temperature", f"{temp:.1f}°C{note}")
    if humid is not None:
        note = f"  (comfort {h_lo}–{h_hi}%)" if h_lo is not None else ""
        _row(sec, "Humidity", f"{round(humid)}%{note}")
    if wifi is not None:
        _row(sec, "WiFi", f"{wifi}%" + (f"  ({ssid})" if ssid else ""))
    if sec:
        L.append("\n  🌡️  Sensors"); L += sec

    # ── Lighting ─────────────────────────────────────────────────
    nl_on = G('get_hw_control', 'night_light_on')
    bright = G('get_light_style', 'brightness')
    nv = G('get_hw_control', 'night_vision')
    led = G('get_hw_control', 'status_light_on')
    sec = []
    if nl_on is not None:
        _row(sec, "Night light", _onoff(nl_on))
    if bright is not None:
        _row(sec, "Brightness", f"{bright}%")
    nls = (d.get('get_light_style') or {}).get('night_light')
    if nls and any(nls.get(c) for c in ('r', 'g', 'b')):
        _row(sec, "Light colour",
             f"#{(nls.get('r') or 0):02x}{(nls.get('g') or 0):02x}{(nls.get('b') or 0):02x}"
             f"  bri {nls.get('brightness')}  pattern {nls.get('pattern_id')}  [RGB unverified]")
    _row(sec, "Night vision", nv)
    if led is not None:
        _row(sec, "Status LED", _onoff(led))
    flip = G('get_hw_control', 'video_flip')
    if flip is not None:
        _row(sec, "Flip screen", _onoff(flip))
    if sec:
        L.append("\n  💡 Lighting"); L += sec

    # ── Comfort Range (temperature/humidity comfort-alert thresholds) ──
    hp = d.get('get_hw_policy') or {}
    sec = []
    if hp.get('temp_low_c') is not None:
        state = "alerts if outside" if hp.get('temp_alert') else "alert off"
        _row(sec, "Temperature", f"{hp['temp_low_c']}–{hp['temp_high_c']}°C  ({state})")
    if hp.get('humi_low_pct') is not None:
        state = "alerts if outside" if hp.get('humi_alert') else "alert off"
        _row(sec, "Humidity", f"{hp['humi_low_pct']}–{hp['humi_high_pct']}%   ({state})")
    if sec:
        L.append("\n  🌡️  Comfort Range"); L += sec

    # ── Audio ────────────────────────────────────────────────────
    song = G('get_lullaby', 'current_sound')
    playing = G('get_lullaby', 'is_playing')
    vol = G('get_lullaby_schedule', 'volume')
    timer = G('get_lullaby_schedule', 'timer')
    sec = []
    if song:
        state = "▶ playing" if playing else "⏹ stopped"
        v = f"  🔊 {vol}%" if vol is not None else ""
        _row(sec, "Playing", f"{song}  {state}{v}")
        if timer:
            _row(sec, "Timer", timer)
    scheds = (d.get('get_lullaby_schedules') or {}).get('schedules') or []
    for s in scheds[:3]:
        dm = s.get('days_mask', 0)
        days = "Mon–Sun" if (dm & 0x7f) == 0x7f else f"days 0x{dm:02x}"
        dur = s.get('duration_sec', 0); dh, dmin = dur // 3600, (dur % 3600) // 60
        ai = " +AI-autoplay" if s.get('ai_autoplay') else ""
        _row(sec, "Schedule",
             f"{s.get('sound') or s.get('name')} @ {s.get('start_hour', 0):02d}:"
             f"{s.get('start_minute', 0):02d}  {days}  for {dh}h{dmin:02d}m{ai}")
    if not scheds:
        sa = d.get('get_lullaby_schedule_action') or {}
        if sa.get('has_schedule') is not None:
            _row(sec, "Schedule", "configured" if sa.get('has_schedule') else "none scheduled")
    if sec:
        L.append("\n  🎵 Audio"); L += sec

    # ── Detection ────────────────────────────────────────────────
    sec = []
    cry = d.get('get_cry_detection')
    if cry is not None:
        v = "enabled" if cry.get('enabled') else "disabled"
        if cry.get('enabled'):
            if cry.get('sensitivity_label'):
                v += f"  (sensitivity: {cry['sensitivity_label']})"
            extra = []
            if cry.get('ai_enabled') is not None:
                extra.append(f"AI {'on' if cry['ai_enabled'] else 'off'}")
            if cry.get('dnn_confidence'):
                extra.append(f"conf≥{cry['dnn_confidence']:.2f}")
            if cry.get('hit_percentage'):
                extra.append(f"hit≥{cry['hit_percentage']:.0f}%")
            if cry.get('audio_filter_enable') is not None:
                extra.append(f"filter {'on' if cry['audio_filter_enable'] else 'off'}")
            if extra:
                v += "  [" + ", ".join(extra) + "]"
        _row(sec, "Cry", v)
    cough = d.get('get_cough_detection')
    if cough is not None:
        v = "enabled" if cough.get('enabled') else "disabled"
        if cough.get('enabled') and cough.get('mode_desc'):
            v += f"  ({cough['mode_desc']})"
        _row(sec, "Cough", v)
    danger = d.get('get_danger_zone')
    if danger is not None:
        if danger.get('configured'):
            nm = f": {danger['name']}" if danger.get('name') else ""
            _row(sec, "Danger zone", f"set{nm}")
        else:
            _row(sec, "Danger zone", "not set")
    dz = d.get('get_detection_zone_v2')
    if dz is not None:
        if dz.get('configured'):
            # normalized bounding box: the rectangle of the frame that is watched
            w = (dz['x_max'] - dz['x_min']) * 100
            h = (dz['y_max'] - dz['y_min']) * 100
            _row(sec, "Motion zone",
                 f"{w:.0f}%×{h:.0f}% box  "
                 f"(left {dz['x_min']*100:.0f}% → right {dz['x_max']*100:.0f}%, "
                 f"top {dz['y_min']*100:.0f}% → bottom {dz['y_max']*100:.0f}%)")
        else:
            _row(sec, "Motion zone", "full frame")
    ac = d.get('get_auto_capture')
    if ac is not None:
        _row(sec, "Auto capture", ac.get('desc'))
    if sec:
        L.append("\n  🔍 Detection"); L += sec

    # ── Sleep & Safety ───────────────────────────────────────────
    sm = G('get_sleep_mode', 'enabled')
    ss = d.get('get_sleep_safety_setting') or {}
    baby = ss.get('baby_presence_alert')
    sec = []
    if sm is not None:
        _row(sec, "Sleep mode", _onoff(sm) + (" (feed suspended)" if sm else ""))
    if ss.get('mode') is not None:
        _row(sec, "Sleep alerts", ss.get('mode_desc'))
    if baby is not None:
        _row(sec, "Baby presence", _onoff(baby))
    sslive = d.get('get_sleep_safety') or {}          # LIVE status (0x…GET_SLEEP_SAFETY_STATUS)
    if sslive.get('status') is not None:
        rt = sslive.get('remaining_time')
        _row(sec, "Safety (live)", f"status={sslive.get('status')}" + (f"  {rt}s left" if rt else ""))
    if sec:
        L.append("\n  😴 Sleep & Safety"); L += sec

    # ── Smart Accessories ────────────────────────────────────────
    stc = d.get('get_smart_temp_config')
    sti = d.get('get_smart_temp_info')
    mat = d.get('get_mat_info')
    sec = []
    if stc is not None and stc.get('enabled'):
        _row(sec, "Fever alert", f"high {stc.get('high_temp_c')}°C  low {stc.get('low_temp_c')}°C")
    elif stc is not None:
        _row(sec, "Fever alert", "disabled")
    if sti is not None:
        _row(sec, "Thermometer",
             f"{sti.get('temp_c')}°C  (battery {sti.get('battery')}%)" if sti.get('paired')
             else "not paired")
    if mat is not None:
        _row(sec, "Breathing mat",
             f"{mat.get('bpm')} bpm  ({mat.get('detect_state')})" if mat.get('connected')
             else "not connected")
    if sec:
        L.append("\n  🍼 Smart Accessories"); L += sec

    # ── Network ──────────────────────────────────────────────────
    wf = d.get('get_wifi') or {}
    sec = []
    _row(sec, "WiFi SSID", wf.get('ssid') or ssid)
    _row(sec, "IP address", wf.get('ip'))
    _row(sec, "Camera MAC", wf.get('mac'))
    # Connected-AP radio metrics (new parse_wifi keys @0x94..0xa4 — present only when the
    # response is long enough). DISTINCT from the WiFi % above (get_hw_control.wifi_strength,
    # a 0-100 quality percent). 0 = this firmware did not populate the field (confirm live).
    if 'strength' in wf:
        _row(sec, "Radio (AP)",
             f"RSSI={wf.get('strength')} dBm  quality={wf.get('quality')}  "
             f"noise={wf.get('noise')} dBm  ch {wf.get('channel')} ({wf.get('frequency')} MHz)")
    if sec:
        L.append("\n  📡 Network"); L += sec

    # ── Stream ───────────────────────────────────────────────────
    mp = d.get('get_media_profiles')
    sec = []
    if mp is not None and mp.get('width'):
        _row(sec, "Resolution", f"{mp['width']}×{mp['height']} @ {mp['fps']} fps")
        _row(sec, "Codec", mp.get('codec'))
        if mp.get('bitrate_kbps'):
            _row(sec, "Bitrate", f"~{mp['bitrate_kbps']/1000:.1f} Mbps (HD)")
        if mp.get('gop'):
            _row(sec, "Keyframe", f"every {mp['gop']} frames")
    if sec:
        L.append("\n  📺 Stream"); L += sec

    # ── Session stats (camera-side telemetry — 0x0934, undocumented) ──
    # The camera's own view of this session: connection mode + NAT, per-stream
    # frame/keyframe counts, its resend-FIFO pressure (resendBufferUsage) and
    # send-error counters. A health channel to cross-check our own loss/recovery.
    # Always surfaced (degrades to "n/a") so it's clear it was queried. NOTE: on this
    # firmware the VIDEO frm_count reads 0 (a session_id/index quirk) while audio advances —
    # a 0 here is NORMAL, not "broken"; empty error rings are expected.
    ssx = d.get('get_session_stats')
    sec = []
    if ssx and ssx.get('mode'):
        natv = ssx.get('nat')
        _row(sec, "Connection", f"{ssx.get('mode')}" + (f"  (NAT {natv})" if natv else "  (NAT 0)"))
        vstat = ssx.get('video') or {}
        astat = ssx.get('audio') or {}
        vp = [f"frames={vstat.get('frm_count', 0)}"]
        if vstat.get('key_frm_count') is not None:
            vp.append(f"keyframes={vstat.get('key_frm_count')}")
        if vstat.get('resendBufferUsage') is not None:
            vp.append(f"resendBuf={vstat.get('resendBufferUsage')}")
        if vstat.get('send_err_count') is not None:
            vp.append(f"send_err={vstat.get('send_err_count')}")
        errs = [str(e.get('code')) for e in (vstat.get('errors') or []) if e.get('code')]
        if errs:
            vp.append("errors=[" + ",".join(errs[:6]) + "]")
        _row(sec, "Video", "  ".join(vp))
        ap = [f"frames={astat.get('frm_count', 0)}"]
        if astat.get('send_err_count') is not None:
            ap.append(f"send_err={astat.get('send_err_count')}")
        _row(sec, "Audio", "  ".join(ap))
        if not (vstat.get('frm_count') or astat.get('frm_count')):
            _row(sec, "(note)", "per-session counters — richer mid-stream (see --benchmark)")
    else:
        _row(sec, "Session stats", "n/a (no response)")
    L.append("\n  📊 Session stats"); L += sec

    # ── Users ────────────────────────────────────────────────────
    # Prefer get_user_list (0x0946, JSON {"users":[…]}); fall back to the documented
    # get_connected_users (0x099a, which returns count 0 on this firmware).
    # Always surfaced (degrades to "n/a"). Prefer the JSON user list; fall back to 0x099a.
    ul = d.get('get_user_list') or {}
    users = sorted(set(ul.get('users') or []))
    cu = d.get('get_connected_users') or {}
    L.append("\n  👥 Users")
    if users:
        _row(L, "Connected", f"{len(users)}: {', '.join(users)}")
    elif cu.get('count'):
        accs = cu.get('accounts') or []
        _row(L, "Connected", f"{cu.get('count')}" + (f": {', '.join(accs)}" if accs else ""))
    else:
        _row(L, "Connected", "n/a")

    # ── System ───────────────────────────────────────────────────
    fw = (G('get_hw_control', 'firmware')
          or G('get_lightweight_status', 'firmware')
          or G('check_firmware_update', 'current_version'))
    upd = G('check_firmware_update', 'update_available')
    events = G('get_event_list', 'count')
    sec = []
    if fw:
        tag = "✅ up to date" if upd is False else (
            f"⬆ update → {G('check_firmware_update','latest_version')}" if upd else "")
        _row(sec, "Firmware", f"{fw}  {tag}".rstrip())
    if events is not None:
        _row(sec, "Recent events", str(events))
    fs = d.get('get_feature_support') or {}
    flags = fs.get('flags')
    if flags:
        _row(sec, "Capabilities", f"{sum(1 for f in flags if f)}/{len(flags)} feature flags set "
                                  "(0x1316 map; ordering not decoded)")
    if sec:
        L.append("\n  ℹ️  System"); L += sec

    L.append("")
    L.append(bar)
    return "\n".join(L)


# ── Detection history (s_log) — LOCAL per-minute history, NOT a live reading ──────
# The s_log fields are a per-minute detection/presence history (bp/na/mo/bw/be/pr + te/hu),
# retrieved by pulling the on-camera DVR manifest over RDT. This section is a thin PRESENTATION
# layer over cuboai_sensors.get_history_sensors() — the pull, retry/fallback, pacing, and
# value->label mapping all live there now (see cuboai_sensors.py); every Reading it hands back
# already carries its own ts/age/stale, so this file only formats. ni/nm/se/ve are intentionally
# NOT rendered here anymore (deliberately not modelled as entities — see cuboai_sensors.py /
# SENSORS.md); cuboai_playback.dump_history_raw_keys() remains the place to inspect them raw.
_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(vals):
    """Unicode block sparkline for a numeric series; None entries render as a gap (space)."""
    nums = [v for v in vals if v is not None]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    out = []
    for v in vals:
        if v is None:
            out.append(" ")
        else:
            out.append(_SPARK[int((v - lo) / span * (len(_SPARK) - 1) + 0.5)])
    return "".join(out)


def _render_history(hs) -> list:
    """Render the s_log HISTORY section from a cuboai_sensors.HistorySensors reading — clearly
    separated from the live sensors above. Every field is a Reading; age/staleness/labels are
    read straight off it (cuboai_sensors did the pull + mapping, this only formats)."""
    head = "\n  🕒 Detection History (s_log — LOCAL history, NOT a live reading)"
    if not hs.baby_present.available:
        return [head, "    (no manifest retrievable this pull — history unavailable; "
                      "last-good ages until the next successful pull)"]
    ts, age = hs.baby_present.ts_utc, hs.baby_present.age_s
    am, asec = divmod(int(age), 60)
    age_str = f"{am} min {asec:02d}s ago" if am else f"{asec}s ago"
    loc = ts.astimezone()
    tzhint = "" if loc.utcoffset() else "  (host TZ=UTC; camera locale ≈UTC+1 — set $TZ for true local)"
    stale_note = "  [STALE — last pull failed, showing cached]" if hs.baby_present.stale else ""
    L = [head, f"    as of {loc:%H:%M} local / {ts:%H:%M} UTC  —  {age_str}{tzhint}{stale_note}"]
    bp = hs.baby_present
    if bp.value is not None:
        L.append(f"    {'Baby in crib:':<{_W}} {bp.note or 'unknown'} (bp={bp.value})")
    na = hs.noise
    if na.value is not None:
        L.append(f"    {'Noise avg:':<{_W}} {na.value}{'  (' + na.note + ')' if na.note else ''}")
    mo = hs.motion
    if mo.value is not None:
        L.append(f"    {'Motion:':<{_W}} {mo.note} (mo={mo.value})")
    bw = hs.wellbeing
    if bw.value is not None:
        L.append(f"    {'Activity (bw):':<{_W}} bw={bw.value}  [{bw.note}]")
    pr = hs.privacy
    if pr.value is not None:
        L.append(f"    {'Sleep/privacy:':<{_W}} {pr.note} (pr={pr.value})")
    be = hs.baby_event
    if be.value is not None:
        L.append(f"    {'Baby event (be):':<{_W}} {be.value}  (rare; unfired in the reference 72h capture)")
    if hs.temperature_c.value is not None:
        L.append(f"    {'Temp (hist):':<{_W}} {hs.temperature_c.value}°C")
    if hs.humidity_pct.value is not None:
        L.append(f"    {'Humidity (hist):':<{_W}} {hs.humidity_pct.value}%")
    return L


def dump_history_raw_keys(transport, hours_back=1, timeout=10):
    """Diagnostic (read-only, standalone): pull the manifest(s) and report the RAW s_log key inventory
    THIS firmware actually emits vs what parse_manifest models — settles whether ni/nm/se/ve (or any
    other key) are present in OUR pulls, distinct from 'unconfirmed meaning'. Never raises."""
    import cuboai_playback as pb, datetime as _dt, time as _t
    now = _dt.datetime.now(_dt.timezone.utc)
    reports = []
    for h in range(max(int(hours_back), 1)):
        if h:
            _t.sleep(0.5)
        diag = {}
        try:
            pb.pull_manifest(transport, now - _dt.timedelta(hours=h), timeout=timeout,
                             retries=0, diag=diag)
        except Exception:
            pass
        raw = diag.get("raw_json")
        if raw:
            reports.append(pb.raw_manifest_keys(raw))
    print("\n  🔎 Manifest RAW-key inventory (what the firmware emits vs what we model)")
    if not reports:
        print("    (no manifest retrievable this pull — nothing to inspect; try when no stream is active)")
        return
    top, rec, unmapped, ranges, nrec = set(), set(), set(), {}, 0
    for r in reports:
        top |= set(r["top_level"]); rec |= set(r["record_keys"]); unmapped |= set(r["unmapped"])
        nrec += r["n_records"]
        for k, (lo, hi, n) in r["numeric_ranges"].items():
            a, b, c = ranges.get(k, (lo, hi, 0)); ranges[k] = (min(a, lo), max(b, hi), c + n)
    modelled = set(pb._EVENT_KEYS) | {"ts", "te", "hu"}
    print(f"    hours pulled: {len(reports)}   s_log records: {nrec}")
    print(f"    top-level keys : {sorted(top)}")
    print(f"    record keys    : {sorted(rec)}")
    print(f"    modelled       : {sorted(rec & modelled)}")
    print(f"    UNMAPPED keys  : {sorted(unmapped) if unmapped else '(none — we model every emitted key)'}")
    for k in sorted(unmapped):
        lo_hi = ranges.get(k)
        print(f"      {k}: range {lo_hi[0]}..{lo_hi[1]} over {lo_hi[2]} recs" if lo_hi else f"      {k}: (non-numeric)")
    for k in ("ni", "nm", "se", "ve"):
        print(f"    {k}: {'PRESENT' if k in rec else 'ABSENT'} in this firmware's manifests")


def _history_series(points):
    """The numeric s_log series worth charting → [(label, unit, [values-aligned-to-points])].
    `points` is a cuboai_sensors.HistoryWindow.points list."""
    return [
        ("Temp",      "°C", [p.temperature_c for p in points]),
        ("Humidity",  "%",  [p.humidity_pct for p in points]),
        ("Noise avg", "",   [p.noise for p in points]),
    ]


def _render_history_charts(window) -> list:
    """Terminal ASCII sparkline per numeric s_log series over a cuboai_sensors.HistoryWindow, with
    a LOCAL-time axis (UTC alongside). HISTORY — the header carries the window's start/end time +
    reading count so staleness stays visible; a chart never implies a live reading."""
    points = window.points
    if len(points) < 2:
        return []
    t0 = points[0].ts_utc.astimezone()
    t1 = points[-1].ts_utc.astimezone()
    u0, u1 = points[0].ts_utc, points[-1].ts_utc
    span_min = int((u1 - u0).total_seconds()) // 60 + 1
    end_age = int(points[-1].age_s)
    stale_note = "  [STALE — last pull failed, showing cached]" if window.stale else ""
    L = ["\n  📈 History charts (s_log window — LOCAL history, NOT a live reading)",
         f"    window {t0:%Y-%m-%d %H:%M}→{t1:%H:%M} local  ({u0:%H:%M}→{u1:%H:%M} UTC)  ·  "
         f"{len(points)} readings / ~{span_min} min  ·  newest {end_age//60}m{end_age%60:02d}s ago"
         f"{stale_note}"]
    any_series = False
    for name, unit, vals in _history_series(points):
        nums = [v for v in vals if v is not None]
        if not nums:
            continue
        any_series = True
        lo, hi, last = min(nums), max(nums), nums[-1]
        L.append(f"    {name:<9} {_sparkline(vals)}  "
                 f"{lo:g}–{hi:g}{unit} (last {last:g}{unit})")
    if not any_series:
        return []
    width = len(points)
    al, ar = f"{t0:%H:%M}", f"{t1:%H:%M}"
    pad = max(width - len(al) - len(ar), 1)
    L.append(f"    {'':<9} {al}{' ' * pad}{ar}  (local)")
    return L


def _write_history_html(window, path):
    """Write a self-contained (no external deps/CDN) HTML chart of a cuboai_sensors.HistoryWindow —
    inline SVG line charts per numeric series, LOCAL-time x-axis. Richer view of the same data as
    the ASCII charts."""
    import html
    points = window.points
    t0 = points[0].ts_utc.astimezone()
    t1 = points[-1].ts_utc.astimezone()
    W, H, PADX, PADY = 720, 140, 48, 18
    def svg(vals, unit, color):
        pts = [(i, v) for i, v in enumerate(vals) if v is not None]
        if len(pts) < 2:
            return "<p style='opacity:.6'>(not enough data)</p>"
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        xlo, xhi = 0, max(len(vals) - 1, 1)
        ylo, yhi = min(ys), max(ys); yspan = (yhi - ylo) or 1.0
        def X(i): return PADX + (i - xlo) / (xhi - xlo or 1) * (W - 2 * PADX)
        def Y(v): return H - PADY - (v - ylo) / yspan * (H - 2 * PADY)
        poly = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in pts)
        grid = (f"<line x1='{PADX}' y1='{H-PADY}' x2='{W-PADX}' y2='{H-PADY}' class='ax'/>"
                f"<text x='{PADX-6}' y='{Y(yhi):.1f}' class='yl'>{yhi:g}{unit}</text>"
                f"<text x='{PADX-6}' y='{Y(ylo):.1f}' class='yl'>{ylo:g}{unit}</text>"
                f"<text x='{PADX}' y='{H-4}' class='xl' style='text-anchor:start'>{t0:%H:%M}</text>"
                f"<text x='{W-PADX}' y='{H-4}' class='xl' style='text-anchor:end'>{t1:%H:%M}</text>")
        return (f"<svg viewBox='0 0 {W} {H}' width='100%' preserveAspectRatio='xMidYMid meet'>"
                f"{grid}<polyline points='{poly}' fill='none' stroke='{color}' "
                f"stroke-width='2' stroke-linejoin='round'/></svg>")
    end_age = int(points[-1].age_s)
    blocks = []
    for (name, unit, vals), color in zip(_history_series(points),
                                         ("#e06c3b", "#3b82e0", "#8a3be0")):
        if not any(v is not None for v in vals):
            continue
        blocks.append(f"<section><h2>{html.escape(name)}</h2>{svg(vals, unit, color)}</section>")
    doc = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CuboAI s_log history</title>
<style>
 :root{{color-scheme:light dark}}
 body{{font:14px/1.5 system-ui,sans-serif;margin:1.5rem;max-width:820px}}
 h1{{font-size:1.15rem;margin:.2rem 0}} h2{{font-size:.95rem;margin:.8rem 0 .2rem}}
 .meta{{opacity:.7;font-size:.85rem;margin-bottom:1rem}}
 section{{border:1px solid #8884;border-radius:8px;padding:.6rem .8rem;margin:.7rem 0}}
 .ax{{stroke:#8886;stroke-width:1}} .yl{{fill:#888;font-size:10px;text-anchor:end;dominant-baseline:middle}}
 .xl{{fill:#888;font-size:10px}}
</style>
<h1>CuboAI detection history (s_log)</h1>
<p class="meta">LOCAL history — NOT a live reading. Window {t0:%Y-%m-%d %H:%M}→{t1:%H:%M} local
 ({len(points)} readings). Newest reading {end_age//60}m{end_age%60:02d}s before this file was written.</p>
{''.join(blocks) or '<p>(no numeric series in this window)</p>'}
</html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def print_status(sess, history=False, history_hours=1, history_chart=None) -> None:
    """Read every GET method and print the clean CuboAI status card. When `history` (opt-in),
    append the s_log HISTORY section + charts via cuboai_sensors.get_history_sensors — an extra
    RDT manifest pull (~1 min lag). OPT-IN because the RDT pull does NOT coexist with a live
    stream (Phase-3 live test: its pull fails and the feed is disturbed); only run it when no
    stream is active."""
    print(_render_status(_read_all(sess)))
    if history:
        window = cuboai_sensors.get_history_sensors(sess, window=True, window_hours=history_hours)
        if window.points:
            # derive the "latest reading" section from the window we already have — no 2nd pull
            latest = cuboai_sensors.history_sensors_from_point(window.points[-1], stale=window.stale)
        else:
            latest = cuboai_sensors.get_history_sensors(sess, window=False)   # deeper hours-back search
        print("\n".join(_render_history(latest)))
        if len(window.points) >= 2:
            print("\n".join(_render_history_charts(window)))
        if history_chart and window.points:
            path = os.path.expanduser(history_chart)
            try:
                _write_history_html(window, path)
                print(f"    📈 wrote chart: {path}  ({len(window.points)} readings)")
            except Exception as e:
                print(f"    ⚠ chart write failed: {e}")


def take_snapshot(sess, path: str) -> None:
    print("📸 Taking snapshot...", flush=True)
    try:
        path = sess.save_snapshot(os.path.expanduser(path), timeout_sec=20.0)
        print(f"   ✅ Saved JPEG: {path} ({os.path.getsize(path)//1024} KB)")
    except ImportError:
        print("❌ Snapshot requires PyAV: pip install av")
    except TimeoutError as e:
        print(f"   ❌ {e}")
    except Exception as e:
        print(f"   ❌ Snapshot failed: {e}")


# ── WiFi-placement / performance benchmark ───────────────────────────────────
# Read-only: streams (so the engine's loss/recovery counters advance) while polling
# the camera's WiFi signal + 0x0934 session-stats at a modest cadence, printing one
# metrics block per interval and a comparison summary on exit. Client-side counters are
# free (get_stats reads in-memory ints); the camera GETs are INJECTED onto the engine's
# reader thread (get_during_stream) so they never race the AV socket.
#
# RSSI note (confirmed from parse_wifi/parse_hw_control): the camera reports WiFi as a
# 0-100 quality PERCENT (get_hw_control.wifi_strength); get_wifi carries SSID/IP/MAC but
# NO dBm RSSI field. So signal% leads, and client-side loss% is the placement proxy the
# brief calls for (lower loss% = better placement).

def _color(s, c, enable):
    if not enable:
        return s
    codes = {'g': '\033[32m', 'y': '\033[33m', 'r': '\033[31m'}
    return codes.get(c, '') + s + '\033[0m'


def _sig_band(pct):
    """(label, colour) for a WiFi quality percent. green ≥70, yellow 40-69, red <40."""
    if pct is None:
        return 'n/a', 'y'
    if pct >= 70:
        return f'{pct}%', 'g'
    if pct >= 40:
        return f'{pct}%', 'y'
    return f'{pct}%', 'r'


def _loss_band(p):
    """colour for an interval loss% — green <1%, yellow 1-5%, red >5%."""
    return 'g' if p < 1.0 else ('y' if p <= 5.0 else 'r')


def run_benchmark(transport, interval=2.0, cap=None, csv_path=None):
    """Read-only WiFi-placement/perf benchmark (see the block comment above)."""
    import cuboai_pure as cp
    import threading
    import csv as _csv
    if not hasattr(transport, 'get_stats'):
        print("❌ --benchmark requires the pure-Python backend (omit --lib / CUBOAI_LIB).")
        return
    color = sys.stdout.isatty() and not csv_path
    print(f"\n📶 WiFi-placement benchmark — sampling every {interval:g}s"
          + (f" for {cap:g}s" if cap else " (Ctrl-C to stop)"), flush=True)
    print("   signal% = camera WiFi quality (no dBm RSSI on this camera); "
          "loss% = client-side placement proxy\n", flush=True)

    # Background consumer: drain av_frames so the engine's reader thread stays alive and
    # the counters advance. Frames are discarded — observe-only, no media is written.
    stop = threading.Event()

    def _drain():
        # duration=None so the stream stays alive for the whole benchmark (incl. the final
        # sample); the main loop ends it via `stop` (checked each queue tick) at the cap.
        try:
            for _ in transport.av_frames(duration=None):
                if stop.is_set():
                    break
        except Exception:
            pass

    th = threading.Thread(target=_drain, daemon=True)
    th.start()

    csvf = csvw = None
    if csv_path:
        csvf = open(os.path.expanduser(csv_path), 'w', newline='')
        csvw = _csv.writer(csvf)
        csvw.writerow(['t', 'elapsed_s', 'wifi_pct', 'ssid', 'loss_pct', 'recovery_pct',
                       'fps', 'bitrate_kbps', 'resend_buffer', 'send_err', 'mode', 'nat',
                       'video_frames', 'audio_frames', 'gap_now', 'kf_incomplete'])

    # Let the reader come up + a little video flow before the first sample (also keeps
    # get_during_stream on the inject path rather than the pre-stream direct-ioctl path).
    time.sleep(min(interval, 1.0))

    t0 = time.time()
    prev = first_snap = None
    sig_samples, loss_samples, fps_samples = [], [], []
    nsamples = 0
    try:
        while True:
            tick = time.time()
            hw = transport.get_during_stream('get_hw_control', timeout=1.5) or {}
            ssx = transport.get_during_stream('get_session_stats', timeout=1.5) or {}
            cur = transport.get_stats()
            if first_snap is None:
                first_snap = cur
            d = cp.stats_delta(prev, cur)
            prev = cur
            elapsed = tick - t0

            wifi = hw.get('wifi_strength')
            ssid = hw.get('ssid')
            vstat = ssx.get('video') or {}
            astat = ssx.get('audio') or {}
            rbu = vstat.get('resendBufferUsage')
            serr = vstat.get('send_err_count')
            mode = ssx.get('mode')
            nat = ssx.get('nat')

            sig_s, sig_c = _sig_band(wifi)
            loss_s = f"{d['loss_pct']:4.1f}%"
            ssid_s = f"({ssid})  " if ssid else ""
            # loss% is the per-interval signal (responsive to placement); recovery% is shown
            # cumulative (a per-interval ratio of two counters is noisy / can exceed 100%).
            line = (f"  t={elapsed:5.1f}s  WiFi {_color(sig_s.ljust(5), sig_c, color)} {ssid_s}"
                    f"loss {_color(loss_s, _loss_band(d['loss_pct']), color)}  "
                    f"recov {cur['recovery_pct']:5.1f}%  fps {d['fps']:4.1f}  "
                    f"{d['bitrate_kbps'] / 1000.0:4.1f}Mbps")
            if mode:
                line += f"  [{mode}{('/NAT' + str(nat)) if nat else ''}]"
            if rbu:
                line += f"  rbuf {rbu}"
            if serr:
                line += f"  serr {serr}"
            print(line, flush=True)

            if wifi is not None:
                sig_samples.append(wifi)
            loss_samples.append(d['loss_pct'])
            fps_samples.append(d['fps'])
            nsamples += 1
            if csvw:
                csvw.writerow([f"{tick:.3f}", f"{elapsed:.1f}", wifi, ssid,
                               f"{d['loss_pct']:.2f}", f"{d['recovery_pct']:.1f}",
                               f"{d['fps']:.1f}", f"{d['bitrate_kbps']:.0f}", rbu, serr,
                               mode, nat, vstat.get('frm_count'), astat.get('frm_count'),
                               cur['gap_now'], d['kf_incomplete']])
                csvf.flush()

            if cap and elapsed >= cap:
                break
            dt = interval - (time.time() - tick)        # the GETs already used some of it
            if dt > 0:
                time.sleep(dt)
    except KeyboardInterrupt:
        print("\n  (interrupted)", flush=True)
    finally:
        stop.set()
        th.join(timeout=2.0)         # M4: don't leave the drain thread attached to the live camera
        if csvf:
            csvf.close()

    # ── summary (compare location A vs B at a glance) ──
    total = time.time() - t0
    span = cp.stats_delta(first_snap, transport.get_stats()) if first_snap else {}

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print("\n  " + "─" * 50)
    print("  📶 Benchmark summary")
    print(f"     Duration        {total:.0f}s  ({nsamples} samples)")
    if sig_samples:
        print(f"     WiFi signal     avg {_avg(sig_samples):.0f}%   "
              f"min {min(sig_samples)}%   max {max(sig_samples)}%")
    else:
        print("     WiFi signal     n/a (camera reports no signal field)")
    print(f"     Loss            avg {_avg(loss_samples):.2f}%")
    print(f"     Recovery        {span.get('recovery_pct', 100.0):.1f}%   "
          f"({span.get('recovery_events', 0)} of {span.get('frags_lost', 0)} lost fragments recovered)")
    print(f"     Frame rate      avg {_avg(fps_samples):.1f} fps")
    print(f"     Video AUs       {span.get('au_video', 0)}  "
          f"(incomplete {span.get('au_incomplete', 0)}, "
          f"keyframe-incomplete {span.get('kf_incomplete', 0)})")
    if csv_path:
        print(f"     CSV             {os.path.expanduser(csv_path)}")
    print("  " + "─" * 50)
    print("  Compare locations by WiFi% (higher is better) and loss% (lower is better).",
          flush=True)


def _validate_startup(args, uid, account, password, camera_ip):
    """Startup-only input validation: stderr only, never the per-frame hot path or stdout. Hard-fails
    (exit 2) on malformed required input with a clear message; clamps out-of-range env knobs."""
    import re as _re
    errs = []
    if not camera_ip:
        errs.append("--camera-ip (or CUBOAI_CAMERA_IP) is required — the pure backend connects "
                    "directly to the camera (no LAN broadcast discovery). The IP comes from the REST API.")
    elif _re.fullmatch(r'[\d.]+', camera_ip):          # looks like an IP -> must be a valid IPv4
        octs = camera_ip.split('.')
        if not (len(octs) == 4 and all(o.isdigit() and 0 <= int(o) <= 255 for o in octs)):
            errs.append(f"--camera-ip {camera_ip!r} is not a valid IPv4 address.")
    elif not _re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]{0,253}', camera_ip):
        errs.append(f"--camera-ip {camera_ip!r} is not a valid IPv4 address or hostname.")
    miss = [n for n, v in (('uid', uid), ('account', account), ('password', password)) if not v]
    if miss:
        errs.append(f"missing credential(s): {', '.join(miss)} "
                    "(pass --uid/--account/--password or CUBOAI_UID/ACCOUNT/PASSWORD).")
    elif not _re.fullmatch(r'[A-Za-z0-9]{16,24}', uid):
        errs.append(f"--uid {uid!r} looks malformed (expected ~20 alphanumeric characters).")
    for name, val in (('--brightness', getattr(args, 'brightness', None)),
                      ('--volume', getattr(args, 'volume', None)),
                      ('--mic-volume', getattr(args, 'mic_volume', None)),
                      ('--speaker-volume', getattr(args, 'speaker_volume', None))):
        if val is not None and not (0 <= val <= 100):
            errs.append(f"{name} {val} out of range [0,100].")
    if getattr(args, 'duration', None) is not None and args.duration <= 0:
        errs.append(f"--duration must be > 0 (got {args.duration}).")
    if getattr(args, 'volume_ramp', None) is not None:
        if args.volume_ramp <= 0:
            errs.append(f"--volume-ramp must be > 0 (got {args.volume_ramp}).")
        if getattr(args, 'volume', None) is None:
            errs.append("--volume-ramp requires --volume (the target level to ramp toward).")
    if getattr(args, 'volume_ramp_step', None) is not None and args.volume_ramp_step <= 0:
        errs.append(f"--volume-ramp-step must be > 0 (got {args.volume_ramp_step}).")
    for name in ('benchmark_interval',):
        v = getattr(args, name, None)
        if v is not None and v <= 0:
            errs.append(f"--{name.replace('_', '-')} must be > 0 (got {v}).")
    for opt in ('record', 'snapshot', 'record_video', 'record_audio', 'record_av'):
        p = getattr(args, opt, None)
        if p:
            d = os.path.dirname(os.path.abspath(os.path.expanduser(p))) or '.'
            if not (os.path.isdir(d) and os.access(d, os.W_OK)):
                errs.append(f"--{opt.replace('_', '-')} {p!r}: directory {d!r} is not writable.")
    if errs:
        for e in errs:
            print("Error: " + e, file=sys.stderr)
        sys.exit(2)
    _clamp_env_knobs()


# ══ Playback / rewind (local DVR retrieval — read-class) ════════════════════════════
def _parse_time_arg(s, as_utc=False):
    """Resolve a human time to epoch SECONDS. LOCAL time by default (the host's timezone, which is
    fredde's — the camera clock is ≈UTC+1; inputs are wall-clock, converted to a UTC epoch here).
    Accepts 'YYYY-MM-DD HH:MM[:SS]', 'HH:MM[:SS]' (today), relative '-15m'/'-2h'/'-90s', or 'now'.
    as_utc=True interprets an ABSOLUTE string as UTC instead of local."""
    import datetime as _dt, re as _re
    s = (s or '').strip()
    if s.lower() == 'now':
        return int(time.time())
    # relative "ago": '5m' / '90s' / '2h' / '5 min ago'. The leading '-' is OPTIONAL and the NO-DASH
    # form is recommended — argparse treats a bare '-5m' after a flag as an option and errors; '5m'
    # always works ('-5m' still parses when spelled --playback-from=-5m).
    m = _re.fullmatch(r'-?\s*(\d+)\s*([smhd])[a-z ]*', s.lower())
    if m:
        mult = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[m.group(2)]
        return int(time.time()) - int(m.group(1)) * mult
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            dt = _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if '%Y' not in fmt:                       # time-only -> today
            td = _dt.date.today(); dt = dt.replace(year=td.year, month=td.month, day=td.day)
        if as_utc:
            return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp())
        return int(dt.timestamp())                # naive datetime -> HOST LOCAL time
    raise ValueError(f"unrecognized time '{s}' — use 'YYYY-MM-DD HH:MM', 'HH:MM', or a relative '-15m'")


def _fmt_both(epoch):
    """One epoch as LOCAL time with UTC alongside — the anti-timezone-confusion echo."""
    import datetime as _dt
    loc = _dt.datetime.fromtimestamp(int(epoch)).astimezone()
    utc = _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc)
    return f"{loc:%Y-%m-%d %H:%M:%S %Z} (UTC {utc:%Y-%m-%d %H:%M:%S})"


def list_recordings(transport, hours=6):
    """--list-recordings: pull the recent hourly manifests over RDT and print the retrievable
    footage timeline in LOCAL time (UTC alongside). Read-class; no AV stream. The CURRENT (growing)
    UTC hour is not served over RDT (HELLOs, no DATA), so it is skipped + flagged."""
    import cuboai_playback as pb, datetime as _dt
    now = int(time.time())
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    cur_hour = now_utc.replace(minute=0, second=0, microsecond=0)
    print("\n📼 Recorded footage timeline  (local time; UTC alongside)")
    print("   Retention: up to ~72 h back (Gen3; older units ~18 h). Local viewing is free.")
    print(f"   Now: {_fmt_both(now)}")
    print(f"   ℹ the current hour ({cur_hour:%H}:00 UTC / {cur_hour.astimezone():%H}:00 local) isn't")
    print(f"     manifest-LISTED below, but playback CAN retrieve it — footage plays right up to")
    print(f"     ~1 min behind live. Freshest retrievable via --playback-from: ≈ {_fmt_both(now - 60)}.\n")
    # NOTE on the manifest flake (2026-07-18): never_started (~55 HELLOs, 0 DATA) is a camera-side
    # conn_id WEDGE that is PACING-sensitive — issuing pulls too fast reuses an unreleased RDT
    # conn_id and starves (see the PACE_S comment below). It is NOT loss, NOT our handshake (HELLOs
    # are byte-identical serve vs starve), NOT the resendBufferUsage gauge (which predicts nothing).
    # Rapid retry makes it WORSE. This scan paces itself; a starved hour usually clears if you rerun
    # the whole command after a short wait (a fresh conn_id). The honest signal is stopped_reason.
    cov = pb.CoverageModel()
    # PACING (2026-07-18, decode run): the manifest flake is a camera-side conn_id WEDGE, not loss
    # and not our handshake (served vs starved HELLOs are byte-identical bar the conn_id counter).
    # After serving/attempting an RDT transfer the camera needs TIME to release that conn_id before
    # it can serve the next; a pull issued too soon reuses the still-open conn_id and STARVES (the
    # camera HELLOs it forever, never sends DATA, and won't advance the counter). Evidence: back-to-
    # back pulls (~2s) froze the camera on one conn_id → ~0 served; 120s-spaced pulls advanced the
    # conn_id each time → ~2/3 served. RAPID RETRY MAKES IT WORSE (0/8 converted, drove the camera
    # to no_manifest). So: ONE attempt per hour (retries=0) + a gap between hours so the camera
    # releases each conn_id. PACE_S is deliberately conservative; a full scan trades time for
    # reliability. Sensors (latest hour only, naturally paced) are unaffected.
    # 2026-07-18c: the conn_id WEDGE is now FIXED at the source — RdtReceiver defaults to the native
    # RDT_Destroy CLOSE (releases the conn_id, no freeze) + native client-initiated OPEN (retransmit-
    # until-established). So PACE_S matches NATIVE's own gap: RDT_Destroy → Thread.sleep(500ms) → next
    # RDT_Create. Since we now send the byte-identical CLOSE, the camera releases as fast for us as for
    # the app, so 0.5s is the parity value (wire A/B served 5/5 at 2.5s; native proves 0.5s in the app;
    # the intermediate isn't separately wire-pinned — raise this if a fast scan ever restarts wedging).
    # Only fast SERVED pulls are governed by this gap; starved pulls already burn the recv timeout.
    PACE_S = _env_float("CUBOAI_LIST_PACE_S", 0.5)   # B-8/R3: defensive parse (also clamped at startup)
    # NATIVE-MATCH scan path (default ON): NativeScanSession runs a persistent single-reader thread
    # (the native IOTC service-thread analog) so the whole scan rides ONE session with no reader gap,
    # like the app. The camera still won't serve a 2nd DownloadFile on a session (a camera-side limit,
    # proven even with this native architecture), so the service does a clean reader-coordinated
    # reconnect per file — fast (~4s/hour) and, unlike the inline ioctl() reconnect, it fully quiesces
    # the reader before the socket close/reopen (candidate fix for the macOS scan). =0 => legacy path.
    inner = getattr(transport, "_inner", transport)
    svc = None
    if os.environ.get("CUBOAI_RDT_SCAN_SERVICE", "1") == "1":
        try: svc = pb.NativeScanSession(inner).start()
        except Exception: svc = None
    try:
        for h in range(1, max(1, hours) + 1):     # start at 1 => skip the growing current hour
            if h > 1 and PACE_S > 0 and svc is None:
                time.sleep(PACE_S)                 # legacy path: let the camera release the prev conn_id
            hdt = now_utc - _dt.timedelta(hours=h)
            loc = hdt.astimezone()
            # print the hour + flush BEFORE the pull so a multi-second pull never looks locked up.
            print(f"   {loc:%Y-%m-%d %H}:00 local  (UTC {hdt:%H}:00)   …", end="", flush=True)
            diag = {}
            if svc is not None:
                recs, resp = svc.download_manifest(hdt, timeout=6, diag=diag)
            else:
                recs, resp = pb.pull_manifest(transport, hdt, timeout=6, retries=0, diag=diag)
            n = cov.add_manifest(recs) if recs else 0
            # per-pull truth (workorder Step 1): order, hour, rdtChannel, stopped_reason, data, elapsed
            reason = diag.get("stopped_reason") or "?"
            ch = diag.get("rdtChannel")
            det = (f"ch={ch} reason={reason} data={diag.get('data_seen')} "
                   f"hello={diag.get('hellos_seen')} cid={diag.get('first_hello_cid')}"
                   f" tries={diag.get('attempts')} bytes={diag.get('got_bytes')}/"
                   f"{diag.get('file_size')} {diag.get('elapsed', 0):.1f}s")
            if n:
                tag = f"✅ {n:2d}/60 min with footage"
            elif diag.get("stopped_reason") in ("no_manifest",) or (
                    resp is not None and getattr(resp, 'file_size', 0) <= 0):
                tag = "— no manifest (idle / not recorded)"
            elif diag.get("stopped_reason") == "ioctl_timeout":
                tag = "✗ 0x910 timed out (camera busy / IO window)"
            elif diag.get("stopped_reason") == "never_started":
                tag = "✗ RDT never started (HELLOs, no DATA)"
            elif diag.get("stopped_reason") == "short_read":
                tag = "✗ short read (DATA lost, unrecovered)"
            else:
                tag = "— (unavailable)"
            print(f"\r   {loc:%Y-%m-%d %H}:00 local  (UTC {hdt:%H}:00)   {tag}   [{det}]")
    finally:
        # B-5: a pull raising mid-scan must NOT leak the reader thread + socket ownership.
        if svc is not None:
            try: svc.close()
            except Exception: pass
    if not cov.count:
        print("\n   ⚠ No footage could be pulled — every hour returned never_started (HELLOs, 0 DATA).")
        print("     The camera's RDT is wedged on a conn_id (a prior/too-fast pull it hasn't released")
        print("     yet). WAIT ~1-2 min for it to release, then rerun (rapid retry only prolongs it;")
        print(f"     raise CUBOAI_LIST_PACE_S above the current {os.environ.get('CUBOAI_LIST_PACE_S','8')}s to space the scan more).")
        print("     --playback-from can still work when listing fails — it attempts the pull anyway.")
        return
    lo, hi = cov.span()
    print(f"\n   ✅ Retrievable range:")
    print(f"        earliest      : {_fmt_both(lo)}")
    print(f"        latest LISTED : {_fmt_both(hi + 60)}   ({cov.count} minutes manifest-listed)")
    print(f"        latest PLAYS  : ≈ {_fmt_both(now - 60)}   (current hour plays but isn't listed)")
    print("   Retrieve any moment up to ~1 min behind live:")
    print(f"        cuboai_validate ... --playback-from '<local time or 5m>' --playback-out clip.ts")


def do_playback(transport, args, parser):
    """--playback-from: retrieve recorded video+audio for a range and WRITE a playable .ts (VLC).
    Read-class (no device state mutated). GUARANTEES live restore on EVERY exit path — normal end,
    error, and SIGINT/SIGTERM (a human will Ctrl-C this). Validates against coverage first + echoes
    the resolved time (local AND UTC) so a timezone slip is visible, not a phantom failure."""
    import cuboai_playback as pb, datetime as _dt, signal, threading
    inner = getattr(transport, "_inner", transport)

    # Install the interrupt handler EARLY (before the manifest pull) so a Ctrl-C anywhere sets the
    # stop flag instead of raising an uncaught KeyboardInterrupt. pbs may still be None here; the
    # handler only sets the flag — the finally does the actual close()/restore once pbs exists.
    stop_flag = threading.Event(); pbs = None; _prev = {}
    def _handler(signum, _frame):
        print(f"\n⚠  signal {signum} received — stopping playback and restoring live…",
              file=sys.stderr, flush=True)
        stop_flag.set()
    for _s in (signal.SIGINT, signal.SIGTERM):
        _prev[_s] = signal.signal(_s, _handler)
    def _restore_handlers():
        for _s, _h in _prev.items():
            signal.signal(_s, _h)

    # 1) resolve the target (LOCAL by default; --playback-utc = UTC input) + span
    try:
        t_from = _parse_time_arg(args.playback_from, as_utc=args.playback_utc)
    except ValueError as e:
        print(f"❌ --playback-from: {e}"); sys.exit(2)
    if args.playback_to:
        try:
            t_to = _parse_time_arg(args.playback_to, as_utc=args.playback_utc)
        except ValueError as e:
            print(f"❌ --playback-to: {e}"); sys.exit(2)
        rec_secs = t_to - t_from
        if rec_secs <= 0:
            print("❌ --playback-to must be AFTER --playback-from"); sys.exit(2)
    else:
        rec_secs = float(args.playback_duration or 30)
    now = int(time.time())

    print("\n⏪ Rewind request  (echo — check the timezone!)")
    print(f"   from : {_fmt_both(t_from)}"
          f"{'   [input read as UTC]' if args.playback_utc else '   [input read as LOCAL]'}")
    print(f"   span : {rec_secs:.0f} s of recorded footage  (through {_fmt_both(t_from + int(rec_secs))})")

    # 2) validate cheaply — recency + retention ONLY. We deliberately do NOT pre-pull the hourly
    # manifest here: the 0x910/RDT pull is flaky on WiFi and its timeout TEARS DOWN the session
    # socket (fredde's Mac: transport.ioctl disconnects on timeout → start() hit a None socket),
    # and playback does not need it — 0x31a serves footage right up to ~30 s behind live, INCLUDING
    # the current growing hour (whose manifest isn't served but whose minutes play). So the only
    # gates are the recency floor (the actively-written edge, 60 s for margin) and retention; if the
    # exact minute has no footage, the pull simply returns 0 AUs and we say so. --list-recordings
    # is the separate "browse the range" tool.
    RECENCY_FLOOR_S = 60
    if t_from > now - RECENCY_FLOOR_S:
        print(f"❌ too recent — the last ~{RECENCY_FLOOR_S}s is still being written. Ask for a moment at "
              f"least ~1 min ago (playback serves right up to ~1 min behind live)."); sys.exit(2)
    if now - t_from > pb.RETENTION_72H_S:
        print(f"❌ beyond retention (~72 h). Earliest retrievable ≈ {_fmt_both(now - pb.RETENTION_72H_S)}.")
        sys.exit(2)

    # 3) warn — CORRECTLY SCOPED (2026-07-23). Playback is PER-CLIENT, not a global camera state:
    # the camera serves DVR and live to DIFFERENT client sessions simultaneously (fredde runs DVR on
    # a phone daily while the iPad/HA keeps streaming live, uninterrupted). So OTHER clients are NOT
    # affected — the old "the nursery has NO live feed" was false. And this invocation only replaces a
    # live stream it is ITSELF rendering: with the playback flags do_playback returns BEFORE the
    # --stream-video path, so a bare rewind has no local live output to interrupt. Only warn about an
    # interruption when this session is actually streaming; otherwise just clarify the per-client scope.
    if args.stream_video:
        print(f"\n⚠  THIS SESSION's live stream will be replaced by recorded footage for ~{rec_secs:.0f}s; "
              "it is restored automatically on exit (including Ctrl-C).", file=sys.stderr, flush=True)
        print("   (Per-client: OTHER devices — iPad / Home Assistant — keep their own live feed.)",
              file=sys.stderr, flush=True)
    else:
        print("\nℹ  Playback is per-client — retrieving recorded footage here does NOT interrupt live on "
              "other devices (iPad / Home Assistant). This invocation isn't streaming, so nothing local "
              "is interrupted; the session's live channel is restored on exit.", file=sys.stderr, flush=True)

    if stop_flag.is_set():          # cancelled during the manifest pull — playback never started
        _restore_handlers()
        print("\n(cancelled before playback started; live was not interrupted.)")
        return

    # A timed-out 0x910 manifest pull makes transport.ioctl DISCONNECT the session socket (its
    # timeout path calls disconnect(), leaving inner._sock=None) — seen on fredde's Mac/WiFi as
    # "'NoneType' has no attribute 'sendto'" when start() tried to send. The manifest is only an
    # advisory coverage hint (playback doesn't need it), so revive the session before playing.
    if getattr(inner, '_sock', None) is None:
        print("   ⚠ session socket dropped during the manifest pull — reconnecting…",
              file=sys.stderr, flush=True)
        try:
            transport.connect()
        except Exception as e:
            _restore_handlers()
            print(f"❌ could not reconnect after the manifest timeout: {e}")
            sys.exit(2)

    out_path = os.path.expanduser(args.playback_out)
    pbs = pb.PlaybackSession(transport, log=lambda *a: print("   [pb]", *a, flush=True))
    wall0 = time.time(); summary = None; served_target = t_from
    try:
        # 0x31a can return -1 (refused) when the exact moment is momentarily too fresh (the
        # record→playable lag varies), so back off to slightly older targets before giving up.
        # A persistent -1 across all backoffs is usually a BUSY camera (the app/another viewer is
        # open) or an unreleased prior session — reported clearly below.
        # start() retries the SAME target patiently (~8×, for the fresh-footage-finalizing -1). If
        # even that fails, fall back ONCE to a target 2 min older (settled footage always plays).
        N = None
        for back in (0, 120):
            if stop_flag.is_set():
                break
            try:
                N = pbs.start(t_from - back, disable_timecontrol=0)   # real-time pacing (dtc tested lossy)
                served_target = t_from - back
                if back:
                    print(f"   ⚠ the requested moment was still refused after retries; served {back}s "
                          f"earlier ({_fmt_both(served_target)}) instead.", file=sys.stderr, flush=True)
                break
            except RuntimeError as e:
                if 'rejected' in str(e).lower() and back < 120:
                    time.sleep(0.6); continue
                raise
        print(f"   channel N assigned: {N}", flush=True)
        wall_cap = rec_secs * 3.0 + 25              # safety cap (idle_timeout also ends EOS)
        with open(out_path, "wb") as w:
            summary = pb.mux_playback_stream(pbs, w, record_seconds=rec_secs, duration=wall_cap,
                                             stop_flag=stop_flag,
                                             log=lambda *a: print("   [mux]", *a, flush=True))
    except Exception as e:
        if 'rejected' in str(e).lower():
            print("❌ the camera REFUSED playback for that time (result -1), even a few minutes back.",
                  file=sys.stderr)
            print("   Most likely: the CuboAI APP or another viewer is open — CLOSE it, then retry.",
                  file=sys.stderr)
            print("   Also try: a time further back (e.g. --playback-from 10m), or wait ~60 s for a",
                  file=sys.stderr)
            print("   previous session to release.", file=sys.stderr)
        else:
            print(f"❌ playback error: {e!r}", file=sys.stderr)
    finally:
        try:
            pbs.close(restore_live=True)              # GUARANTEED live restore (every exit path)
        except Exception as e:
            print(f"   ⚠ live restore raised: {e!r}", file=sys.stderr)
        _restore_handlers()
    wall = time.time() - wall0

    # 4) summary fredde can sanity-check
    print("\n📊 Playback result")
    print(f"   channel N     : {getattr(pbs, 'channel', None)}")
    if summary:
        vlo, vhi = summary['v_ts_min'], summary['v_ts_max']
        print(f"   video AUs     : {summary['video']}  (keyframes {summary['keyframes']})")
        print(f"   audio AUs     : {summary['audio']}")
        if vlo:
            print(f"   1st frame ts  : {_fmt_both(vlo)}")
            print(f"   last frame ts : {_fmt_both(vhi)}")
            drift = vlo - served_target
            print(f"   → {'✅ CONTENT IS FROM THE PAST' if abs(drift) <= 300 and (now - vlo) > 60 else '⚠ CHECK'}: "
                  f"first frame {drift:+d}s vs served target, and {now - vlo}s before now (not ≈live).")
        elif summary['video'] == 0:
            print("   → ⚠ NO recorded frames arrived. Either the camera has no footage for that exact "
                  "time, or the request didn't engage. Try a nearby time, or --list-recordings.")
        print(f"   pacing        : real-time (camera paces playback; wall ≈ footage span)")
    print(f"   wall time     : {wall:.1f}s")
    try:
        sz = os.path.getsize(out_path)
    except OSError:
        sz = 0
    print(f"   output        : {out_path}  ({sz / 1024:.0f} KiB)")
    if stop_flag.is_set():
        print("   (stopped early by signal)")

    # 5) confirm live restored + healthy
    try:
        n = 0
        for k, _u, _fi in inner.av_frames_timed(duration=4.0):
            if k == 'video':
                n += 1
        print(f"   live restored : {'✅ healthy' if n else '⚠ NO live frames — check the app!'} ({n} AUs)")
    except Exception as e:
        print(f"   live restored : ⚠ confirm failed: {e!r}")


def main():
    parser = argparse.ArgumentParser(
        description="CuboAI camera validation and control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    conn = parser.add_argument_group("Connection")
    conn.add_argument('--lib',       metavar='PATH', help='Path to libIOTCAPIs_ALL.so')
    conn.add_argument('--uid',       metavar='UID',  help='Device UID (license_id)')
    conn.add_argument('--account',   metavar='STR',  help='dev_admin_id')
    conn.add_argument('--password',  metavar='STR',  help='dev_admin_pwd')
    conn.add_argument('--camera-ip', metavar='IP',   help='Camera LAN IP (enables broadcast redirect on Linux)')
    conn.add_argument('--channels',  metavar='DIGITS', default=None,
                      help='(pure backend) AV channels to open as single digits, e.g. "0123", "01", "1"; '
                           'default (omitted) = ch0..39, native\'s full av-connect set (S70)')
    conn.add_argument('-v', '--verbose', action='store_true',
                      help='(pure backend) print a connect/stream trace (channels, grant, ACK/gap state)')
    # defer-start (pure backend) — same naming/default as cuboai_stream_video. By DEFAULT video
    # starts fast (0x0300+0x01FF up front, first frame in ~0.5-2 s) so short captures don't race a
    # ~5 s window. --defer-start re-enables the deliberate ~5 s native startup defer for wire-
    # fidelity. The S82 wire-fidelity (ACK timestamp / NAK cadence / SACK list) stays ON either way —
    # it affects resend efficiency, never whether AV works.
    conn.add_argument('--defer-start', action='store_true',
                      help='(pure backend) re-enable the ~5 s native startup defer (wire-fidelity); '
                           'default starts video immediately. Also via CUBOAI_DEFER_START=1.')
    conn.add_argument('--no-defer-start', action='store_true', help=argparse.SUPPRESS)  # back-compat no-op

    parser.add_argument('--snapshot',    metavar='FILE',              help='Save JPEG snapshot to FILE')
    parser.add_argument('--record',       metavar='FILE',              help='Record muxed audio+video to a playable .mp4')
    parser.add_argument('--record-video', metavar='FILE',              help='Record the raw HEVC video element to file')
    parser.add_argument('--duration',    type=float, default=10.0,    metavar='SECS')
    parser.add_argument('--stream-video', action='store_true',        help='Stream HEVC video to stdout (pipe to ffplay -f hevc -i -)')
    parser.add_argument('--talk',          metavar='FILE',              help='Send audio file to the camera speaker (two-way talk; pure backend only, not with --lib)')
    parser.add_argument('--talk-loop',     action='store_true',         help='Loop the --talk file continuously (until --talk-secs or Ctrl-C)')
    parser.add_argument('--talk-secs',     type=float, metavar='SECS',  help='Stop --talk after SECS (default: once through the file)')
    parser.add_argument('--talk-gain',     type=float, default=1.0, metavar='MULT',
                        help='Talk volume multiplier (1.0=unchanged, e.g. 0.5 to halve; the camera speaker_level is firmware-locked)')
    parser.add_argument('--record-audio',  metavar='FILE',              help='Record AAC audio to file (e.g. audio.aac)')
    parser.add_argument('--record-av',     metavar='BASE',              help='Record both streams: BASE.hevc + BASE.aac')
    parser.add_argument('--stream-audio',  action='store_true',         help='Stream raw AAC-ADTS to stdout')
    parser.add_argument('--raw', '--passthrough', dest='raw', action='store_true',
                        help='Capture the unprocessed Annex-B bitstream (no FRAMEINFO strip / no '
                             'recovery) for inspection. Default = the production profile (clean, playable).')
    parser.add_argument('--night-light', choices=['on','off'],        help='Night light on/off')
    parser.add_argument('--brightness',  type=int,   metavar='0-100', help='Night light brightness %%')
    parser.add_argument('--volume',      type=int,   metavar='0-100', help='Lullaby volume %%')
    parser.add_argument('--volume-ramp', type=float,  metavar='SECS',
                        help='Ramp the lullaby volume from the current level to --volume over SECS '
                             '(wall-clock — finishes in ~SECS regardless of SET round-trip time). '
                             'Requires --volume.')
    parser.add_argument('--volume-ramp-step', type=float, metavar='SECS', default=0.4,
                        help='Minimum seconds between volume SETs during --volume-ramp (flood cap; '
                             'default 0.4). Total ramp time stays ~--volume-ramp; lowering this only '
                             'adds resolution.')
    parser.add_argument('--timer',       choices=['repeat','30min','60min'])
    parser.add_argument('--play',        metavar='NAME',              help='Play lullaby by name')
    parser.add_argument('--stop',        action='store_true',         help='Stop lullaby')
    parser.add_argument('--sleep-mode',  choices=['on','off'],        help='Sleep/privacy mode')
    # ── new SET commands (2026-05-31) ──────────────────────────────────────
    setg = parser.add_argument_group("SET commands (new)")
    setg.add_argument('--night-vision', choices=['auto','on','off'],
                      help='Night-vision/IR mode (SET_HW_CONTROL; accepted but firmware-managed on this device)')
    setg.add_argument('--status-light', choices=['on','off'],
                      help='Camera-body status LED (accepted but firmware-managed on this device)')
    setg.add_argument('--video-flip',   choices=['on','off'],
                      help='Vertical image flip (SET_HW_CONTROL)')
    setg.add_argument('--mic-volume',     type=int, metavar='N',
                      help='Mic level via SET_HW_CONTROL (firmware-managed on this device)')
    setg.add_argument('--speaker-volume', type=int, metavar='N',
                      help='Speaker level via SET_HW_CONTROL (firmware-managed on this device)')
    setg.add_argument('--cry-detection',  choices=['on','off'], help='Cry detection on/off')
    setg.add_argument('--cry-sensitivity',choices=['low','medium','high'],
                      help='Cry detection sensitivity (low/medium/high → wire 3/2/1)')
    setg.add_argument('--cough-detection',choices=['on','off'], help='Cough detection on/off')
    setg.add_argument('--cough-mode',     choices=['always','in-crib'],
                      help='Cough alert mode: always alert vs only when baby is in crib')
    setg.add_argument('--cough-sensitivity', choices=['low','medium','high'],
                      help='Cough detection sensitivity (low/medium/high → wire 3/2/1)')
    setg.add_argument('--flip-screen',    choices=['on','off'],
                      help='Vertical image flip (alias of --video-flip; SET_HW_CONTROL video_v_flip)')
    setg.add_argument('--sleep-alerts',   choices=['covered-only','covered-and-rollover','off'],
                      help='Sleep-safety mode: covered-face-only vs covered-face+rollover (or off)')
    setg.add_argument('--safety-alert',        choices=['on','off'], help='Sleep-safety: safety/rollover alert (low-level)')
    setg.add_argument('--cover-alert',         choices=['on','off'], help='Sleep-safety: cover alert (low-level)')
    setg.add_argument('--baby-presence-alert', choices=['on','off'], help='Sleep-safety: baby presence alert')
    setg.add_argument('--danger-zone-alert',   choices=['on','off'],
                      help='Danger-zone alert on/off (toggles roi.enable; full polygon needs the region grid, not wired)')
    setg.add_argument('--comfort-temp-low',  type=int, metavar='C',   help='Comfort range: low temperature (°C)')
    setg.add_argument('--comfort-temp-high', type=int, metavar='C',   help='Comfort range: high temperature (°C)')
    setg.add_argument('--comfort-humi-low',  type=int, metavar='PCT', help='Comfort range: low humidity (%%)')
    setg.add_argument('--comfort-humi-high', type=int, metavar='PCT', help='Comfort range: high humidity (%%)')
    setg.add_argument('--auto-capture', choices=['off','motion','schedule','both'],
                      help='Auto event-snapshot mode (off/motion/schedule/both)')
    setg.add_argument('--schedule-volume', type=int, metavar='0-100',
                      help='Lullaby schedule volume (the volume GET_LULLABY_SCHEDULE reports)')
    setg.add_argument('--temp-alert', choices=['on','off'], help='Environment: temperature comfort alert on/off')
    setg.add_argument('--temp-low',   type=int, metavar='C', help='Environment: low temperature threshold (C)')
    setg.add_argument('--temp-high',  type=int, metavar='C', help='Environment: high temperature threshold (C)')
    setg.add_argument('--humi-alert', choices=['on','off'], help='Environment: humidity comfort alert on/off')
    setg.add_argument('--humi-low',   type=int, metavar='PCT', help='Environment: low humidity threshold (pct)')
    setg.add_argument('--humi-high',  type=int, metavar='PCT', help='Environment: high humidity threshold (pct)')

    # ── Lullaby schedule writes (SET_LULLABY_SCHEDULE 0x0990) ───────────────────
    # LIVE-CONFIRMED (2026-07-10): add + delete store exactly and are honored by the app, and the
    # duration round-trips correctly since the tail-Swap transcode fix. So the old
    # --i-understand-this-is-unsafe gate is REMOVED from these rows. The flag itself is kept (a
    # reserved acknowledgement gate) for any genuinely destructive future write; it is a no-op today.
    pbg = parser.add_argument_group("Playback / rewind (local DVR — READ-class; per-client)")
    pbg.add_argument('--list-recordings', action='store_true',
                     help='Print the retrievable footage timeline (local time + UTC). Standalone.')
    pbg.add_argument('--list-hours', type=int, default=6, metavar='N',
                     help='How many past hours to scan for --list-recordings (default 6).')
    pbg.add_argument('--playback-from', metavar='T',
                     help="Rewind START. LOCAL time by default: 'YYYY-MM-DD HH:MM', 'HH:MM' (today), "
                          "or relative '5m' = 5 min ago (no leading dash). Add --playback-utc for UTC.")
    pbg.add_argument('--playback-to', metavar='T',
                     help='Rewind END time (same formats). Overrides --playback-duration.')
    pbg.add_argument('--playback-duration', type=float, default=30, metavar='SECS',
                     help='Seconds of RECORDED footage to retrieve from --playback-from (default 30). '
                          'No FF/pause/speed in the protocol — this is footage span.')
    pbg.add_argument('--playback-out', metavar='FILE',
                     help='Write the playable .ts here (open in VLC on the Mac).')
    pbg.add_argument('--playback-utc', action='store_true',
                     help='Interpret --playback-from/--playback-to as UTC instead of local time.')

    gate = parser.add_argument_group("Lullaby schedule (SET_LULLABY_SCHEDULE 0x0990 — live-confirmed add/delete)")
    gate.add_argument('--i-understand-this-is-unsafe', dest='unsafe', action='store_true',
                      help='Reserved acknowledgement gate for genuinely destructive writes; currently a '
                           'no-op (the lullaby-schedule writes below are live-confirmed and un-gated).')
    gate.add_argument('--add-lullaby-schedule', metavar='SONG',
                      help='Add a lullaby schedule playing SONG (catalog name or UUID). '
                           'Use with --schedule-name/--schedule-start/--schedule-duration/'
                           '--schedule-days/--schedule-ai/--schedule-disable/--schedule-local-time.')
    gate.add_argument('--delete-lullaby-schedule', metavar='NAME',
                      help='Delete the lullaby schedule row whose name == NAME.')
    gate.add_argument('--schedule-name', metavar='NAME',
                      help='Schedule row name / identity key (default: the song name).')
    gate.add_argument('--schedule-start', metavar='HH:MM', help='Schedule start time (24h).')
    gate.add_argument('--schedule-duration', metavar='MIN', type=int,
                      help='Schedule play length in minutes.')
    gate.add_argument('--schedule-days', metavar='SPEC', default='all',
                      help="Day bitmask: 'all' (0x7f = Mon-Sun), or a raw mask "
                           "(decimal or 0xNN). Per-day bit order is unverified.")
    gate.add_argument('--schedule-ai', choices=['on', 'off'], default='off',
                      help='AI auto-play flag for the schedule (default off).')
    gate.add_argument('--schedule-disable', action='store_true',
                      help='Create the schedule row in the disabled state (enable=0).')
    gate.add_argument('--schedule-local-time', action='store_true',
                      help='Send start time as LOCAL wall-clock (sets the nMDay 0x80 '
                           'use-local-time bit) instead of verbatim/UTC.')
    parser.add_argument('--list-songs',  action='store_true',         help='List all songs')
    parser.add_argument('--no-status',   action='store_true',         help='Skip status read (optional since session 24; status + AV now coexist)')
    parser.add_argument('--history',     action='store_true',         help='Append the s_log detection-history section + charts to --status (an RDT manifest pull; OPT-IN because it does NOT coexist with a live stream — proven to fail its pull + disturb the feed. Use only when no stream is active.)')
    parser.add_argument('--history-hours', type=int, default=1, metavar='N', help='How many hours of s_log to retrieve+chart for --history (default 1 = the current growing hour, a single pull). >1 pulls + merges older hours (paced).')
    parser.add_argument('--history-chart', metavar='FILE', help='Also write a standalone self-contained HTML chart of the --history window to FILE (inline SVG per numeric series; open in any browser).')
    parser.add_argument('--history-raw-keys', action='store_true', help='Diagnostic (read-only, standalone): pull the manifest and report the RAW s_log key set the firmware emits vs what we model (settles whether ni/nm/se/ve etc. are actually present). Honors --history-hours.')

    # ── WiFi-placement / performance benchmark (read-only) ─────────────────
    bench = parser.add_argument_group("Benchmark (WiFi placement & performance)")
    bench.add_argument('--benchmark', nargs='?', type=float, const=0.0, default=None, metavar='SECS',
                       help='Stream + print a metrics block every --benchmark-interval seconds '
                            '(WiFi signal%%, client loss%%, recovery, fps, bitrate, camera resend-buffer), '
                            'then a summary on exit. Optional SECS bounds the run (default: until Ctrl-C). '
                            'Read-only; pure-Python backend only.')
    bench.add_argument('--benchmark-interval', type=float, default=2.0, metavar='SECS',
                       help='Seconds between benchmark metric blocks (default 2).')
    bench.add_argument('--benchmark-csv', metavar='FILE',
                       help='Append each benchmark sample as a CSV row to FILE (for comparing locations).')

    args = parser.parse_args()

    if args.list_songs:
        print("\nAvailable lullaby songs:")
        cur_cat = None
        for uuid, (key, name, category) in LULLABY_CATALOG.items():
            if category != cur_cat:
                cur_cat = category
                print(f"\n  {category.upper()}")
            print(f"    {name:<42} {uuid}")
        print()
        return

    # ── Broadcast redirect shim (Linux LAN) ─────────────────────
    camera_ip = getattr(args, 'camera_ip', None) or os.environ.get('CUBOAI_CAMERA_IP')
    # The broadcast-redirect shim is ONLY needed for the native TUTK library (it
    # broadcasts discovery). Pure Python unicasts straight to camera_ip, so skip the
    # shim+execve entirely in pure mode (the execve also breaks stdout piping).
    # Native is now an EXPLICIT opt-in (--lib / CUBOAI_LIB) — the library is never
    # auto-discovered (matches the pure-by-default get_session call below).
    _will_use_native = bool(args.lib or os.environ.get('CUBOAI_LIB'))
    if camera_ip and platform.system() == 'Linux' and _will_use_native:
        shim_path = '/tmp/cuboai_redirect.so'
        shim_src  = '/tmp/cuboai_redirect.c'
        if not os.path.exists(shim_path):
            import subprocess, textwrap
            c_src = textwrap.dedent("""
                #define _GNU_SOURCE
                #include <dlfcn.h>
                #include <string.h>
                #include <sys/socket.h>
                #include <netinet/in.h>
                typedef ssize_t (*sendto_t)(int,const void*,size_t,int,const struct sockaddr*,socklen_t);
                static sendto_t real_sendto=NULL;
                static unsigned char cam_ip[4]={0,0,0,0};
                void set_camera_ip(unsigned char a,unsigned char b,unsigned char c,unsigned char d){cam_ip[0]=a;cam_ip[1]=b;cam_ip[2]=c;cam_ip[3]=d;}
                ssize_t sendto(int fd,const void*buf,size_t len,int flags,const struct sockaddr*addr,socklen_t al){
                    if(!real_sendto)real_sendto=dlsym(RTLD_NEXT,"sendto");
                    if(addr&&addr->sa_family==AF_INET6&&len==88&&cam_ip[0]){
                        struct sockaddr_in6*s6=(struct sockaddr_in6*)addr;
                        if(ntohs(s6->sin6_port)==32761&&s6->sin6_addr.s6_addr[15]==255){
                            struct sockaddr_in6 c=*s6;
                            memset(c.sin6_addr.s6_addr,0,10);c.sin6_addr.s6_addr[10]=0xff;c.sin6_addr.s6_addr[11]=0xff;
                            memcpy(c.sin6_addr.s6_addr+12,cam_ip,4);
                            return real_sendto(fd,buf,len,flags,(struct sockaddr*)&c,al);
                        }
                    }
                    return real_sendto(fd,buf,len,flags,addr,al);
                }
            """)
            with open(shim_src, 'w') as f:
                f.write(c_src)
            subprocess.run(['gcc','-shared','-fPIC','-O2','-o',shim_path,shim_src,'-ldl'],
                           capture_output=True)

        if os.path.exists(shim_path):
            if shim_path not in os.environ.get('LD_PRELOAD', ''):
                import sys as _sys
                env = os.environ.copy()
                env['LD_PRELOAD'] = (shim_path + ':' + env.get('LD_PRELOAD','')).strip(':')
                env['CUBOAI_CAMERA_IP'] = camera_ip
                os.execve(_sys.executable, [_sys.executable] + _sys.argv, env)
            else:
                import ctypes, importlib
                shim = ctypes.CDLL(shim_path)
                shim.set_camera_ip.argtypes = [ctypes.c_ubyte] * 4
                shim.set_camera_ip(*map(int, camera_ip.split('.')))
                import cuboai_tutk
                importlib.reload(cuboai_tutk)
                global TUTKSession
                TUTKSession = cuboai_tutk.TUTKSession

    # ── Connection params ────────────────────────────────────────
    lib_path = args.lib      or os.environ.get('CUBOAI_LIB')
    uid      = args.uid      or os.environ.get('CUBOAI_UID')
    account  = args.account  or os.environ.get('CUBOAI_ACCOUNT')
    password = args.password or os.environ.get('CUBOAI_PASSWORD')
    _validate_startup(args, uid, account, password, camera_ip)   # startup-only; stderr; exits on bad input

    missing = [k for k,v in [('--uid',uid),('--account',account),('--password',password)] if not v]
    if missing:
        print(f"❌ Missing: {', '.join(missing)}")
        print("   Set via args or env: CUBOAI_UID, CUBOAI_ACCOUNT, CUBOAI_PASSWORD")
        sys.exit(1)

    if args.brightness is not None and not 0 <= args.brightness <= 100:
        parser.error("--brightness must be 0-100")
    if args.volume is not None and not 0 <= args.volume <= 100:
        parser.error("--volume must be 0-100")
    if args.talk_gain < 0:
        parser.error("--talk-gain must be >= 0 (1.0 = unchanged, <1 quieter, >1 louder)")
    if args.playback_from and not args.playback_out:
        parser.error("--playback-from requires --playback-out FILE (the .ts to write)")

    # Lullaby-schedule writes are LIVE-CONFIRMED (add/delete store exactly, duration honored since the
    # tail-Swap fix) — no longer gated. The --i-understand-this-is-unsafe flag is retained but a no-op.

    # ── Connect ──────────────────────────────────────────────────
    channels = [int(c) for c in args.channels] if args.channels else None   # S62
    # defer-start: same naming/default as cuboai_stream_video. Default = start fast (_defer=False, so
    # 0x0300+0x01FF go up front and the first frame lands in ~0.5-2 s — short captures don't race a
    # ~5 s window). --defer-start (or CUBOAI_DEFER_START) re-enables the ~5 s native defer (_defer=None
    # → follow full_fidelity). full_fidelity (S82 ACK ts / NAK cadence / SACK) stays ON either way.
    defer  = args.defer_start or os.environ.get('CUBOAI_DEFER_START', '0') != '0'
    _defer = None if defer else False
    # Install the same A/V env profile cuboai_stream_video ships: default = production (FRAMEINFO
    # strip + loss recovery, so --snapshot/--record are clean & playable); --raw = the
    # unprocessed Annex-B passthrough. MUST run before get_session() (the engine reads the gates at
    # construction). Explicit env vars still win in the default branch; --raw hard-forces.
    apply_env_profile(args.raw)
    transport = get_session(uid, account, password, lib_path=lib_path, camera_ip=camera_ip,
                            channels=channels, verbose=args.verbose,
                            auto_discover_lib=False,   # pure by default; --lib/CUBOAI_LIB = native opt-in
                            defer_stream_start=_defer, defer_video_start_late=_defer)
    is_pure = type(transport).__name__ == 'PureSession'
    print(f"\nConnecting to {uid}...", flush=True)
    try:
        transport.connect()
        if is_pure:
            print("✅ Connected (pure Python)", flush=True)
            if transport.session_hdr:
                print(f"   session_hdr: {transport.session_hdr.hex()}")
            print(flush=True)
        else:
            print("✅ Connected\n", flush=True)
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    # Both backends now follow the same feature flow — PureSession implements
    # ioctl/snapshot/av_frames/audio_frames in pure Python (only --talk differs).
    try:
        # ── Benchmark (its own read-only mode; skips the rest) ───────────────
        if args.benchmark is not None:
            run_benchmark(transport, interval=args.benchmark_interval,
                          cap=(args.benchmark or None), csv_path=args.benchmark_csv)
            return

        # ── Playback / rewind (read-class; each returns, skipping status/SETs) ──
        if args.list_recordings:
            list_recordings(transport, hours=args.list_hours)
            return
        if args.playback_from:
            do_playback(transport, args, parser)
            return
        if args.history_raw_keys:
            dump_history_raw_keys(transport, hours_back=args.history_hours)
            return

        if not args.no_status:
            print_status(transport, history=args.history,
                         history_hours=args.history_hours, history_chart=args.history_chart)

        # ── Snapshot ─────────────────────────────────────────────
        if args.snapshot:
            take_snapshot(transport, args.snapshot)

        # ── Talk (PURE-PYTHON-ONLY two-way audio: AAC-LC out the camera speaker) ──
        # The native (--lib) backend cannot perform the camera's talk handshake (the WYZE 4.2.1.1
        # lib omits the 4.3.x av-server capability word -> avServStartEx times out), so talk is
        # gated to the pure backend (same discriminator as --benchmark).
        if args.talk and not hasattr(transport, 'get_stats'):
            print("\n🎤 Talk is a PURE-PYTHON-only feature — omit --lib / CUBOAI_LIB to use --talk "
                  "(the native TUTK 4.2.1.1 lib can't do the camera's talk handshake).")
        elif args.talk:
            mode = ("looping" if args.talk_loop else "once") + \
                   (f", {args.talk_secs:g}s" if args.talk_secs else "") + \
                   (f", gain {args.talk_gain:g}" if args.talk_gain != 1.0 else "")
            print(f"\n🎤 Talk → camera speaker: {args.talk} ({mode})", flush=True)
            def _talk_status(d):
                print(f"   …sent {d['sent']} frames, delivered {d['delivered']}, "
                      f"resends {d.get('resends', 0)}", flush=True)
            try:
                n = transport.send_audio_file(args.talk, loop=args.talk_loop,
                                              max_secs=args.talk_secs, on_status=_talk_status,
                                              gain=args.talk_gain)
                print(f"   ✅ Talk done ({n} audio frames)")
            except KeyboardInterrupt:
                transport.stop_audio()
                print("   ⏹  Talk stopped")
            except FileNotFoundError as e:
                print(f"   ❌ {e}")
            except RuntimeError as e:
                print(f"   ❌ {e}")
            except ImportError as e:
                print(f"   ❌ Talk needs PyAV for transcoding: {e}  (pip install av)")

        # ── Record video element (raw HEVC) ──────────────────────
        if args.record_video:
            path = os.path.expanduser(args.record_video)
            print(f"🎥 Recording {args.duration}s raw HEVC video element to {path}...", flush=True)
            count = 0
            with open(path, 'wb') as f:
                for ftype, data in transport.av_frames(duration=args.duration):
                    if ftype == 'video':
                        f.write(data)
                        count += 1
            print(f"   ✅ {count} video frames ({os.path.getsize(path)//1024} KB)")

        # ── Record audio (uses av_frames to avoid wasting video) ────
        if args.record_audio:
            path = os.path.expanduser(args.record_audio)
            print(f"🎤 Recording {args.duration}s AAC audio to {path}...", flush=True)
            a_count = 0
            with open(path, 'wb') as fa:
                for ftype, data in transport.av_frames(duration=args.duration):
                    if ftype == 'audio':
                        fa.write(data)
                        a_count += 1
            print(f"   ✅ {a_count} audio frames ({os.path.getsize(path)//1024} KB)")

        # ── Record AV (combined video + audio) ───────────────────
        if args.record_av:
            base = os.path.expanduser(args.record_av).rstrip('/')
            if os.path.isdir(base):
                print(f"❌ --record-av needs a base filename, not a directory.")
                print(f"   Example: --record-av /tmp/clip  (produces /tmp/clip.hevc + /tmp/clip.aac)")
            else:
                vpath = base + '.hevc'
                apath = base + '.aac'
                print(f"🎥 Recording {args.duration}s AV to {vpath} + {apath}...", flush=True)
                v_count = a_count = 0
                with open(vpath, 'wb') as fv, open(apath, 'wb') as fa:
                    for ftype, data in transport.av_frames(duration=args.duration):
                        if ftype == 'video':
                            fv.write(data); v_count += 1
                        else:
                            fa.write(data); a_count += 1
                print(f"   ✅ {v_count} video frames ({os.path.getsize(vpath)//1024} KB)")
                print(f"   ✅ {a_count} audio frames ({os.path.getsize(apath)//1024} KB)")

        # ── Stream audio ──────────────────────────────────────────
        if args.stream_audio:
            print("🎤 Streaming AAC-ADTS to stdout", flush=True)
            import sys as _sys
            for frame in transport.audio_frames():
                _sys.stdout.buffer.write(frame)
                _sys.stdout.buffer.flush()

        # ── Stream ───────────────────────────────────────────────
        if args.stream_video:
            print("🎥 Streaming HEVC video to stdout — pipe to: ffplay -f hevc -i -", flush=True)
            import sys as _sys
            for frame in transport.video_frames():
                _sys.stdout.buffer.write(frame)
                _sys.stdout.buffer.flush()

        # ── Record (muxed audio+video .mp4) ──────────────────────
        if args.record:
            path = os.path.expanduser(args.record)
            print(f"🎥 Recording {args.duration}s muxed audio+video to {path}...", flush=True)
            try:
                transport.record_video(path, duration_sec=args.duration)
                print(f"   ✅ Saved: {path} ({os.path.getsize(path)//1024} KB)")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Night light ──────────────────────────────────────────
        if args.night_light:
            on = args.night_light == 'on'
            print(f"\n💡 Night light → {'ON' if on else 'OFF'}...", flush=True)
            try:
                transport.ioctl(*build_set_night_light(on))
                print("   ✅ Done")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Brightness ───────────────────────────────────────────
        if args.brightness is not None:
            print(f"\n💡 Brightness → {args.brightness}%...", flush=True)
            try:
                transport.ioctl(*build_set_light_style_brightness(args.brightness))
                print("   ✅ Done")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Volume / timer ───────────────────────────────────────
        if args.volume is not None or args.timer is not None:
            print(f"\n🔊 Updating lullaby settings...", flush=True)
            try:
                tc, data = transport.ioctl(2440, b'\x00' * 132)
                cur_vol, cur_timer, got_cur = 50, LULLABY_TIMER_REPEAT, False
                if tc == IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP and len(data) >= 16:
                    sched = LullabySchedule.parse(data)
                    cur_vol, cur_timer, got_cur = sched.volume, sched.timer_mode, True
                new_vol = args.volume if args.volume is not None else cur_vol
                timer_map = {'repeat': LULLABY_TIMER_REPEAT,
                             '30min':  LULLABY_TIMER_30MIN,
                             '60min':  LULLABY_TIMER_60MIN}
                new_timer = timer_map.get(args.timer, cur_timer) if args.timer else cur_timer
                t_name = {LULLABY_TIMER_REPEAT:'repeat',
                          LULLABY_TIMER_30MIN:'30min',
                          LULLABY_TIMER_60MIN:'60min'}.get(new_timer, '?')
                # --volume-ramp: step the volume from the current level to the target over the
                # given duration, instead of one jump. Ramp only when we actually read the
                # current level and it differs from the target; otherwise fall back to a direct set.
                ramp = bool(args.volume_ramp) and got_cur and new_vol != cur_vol
                if args.volume_ramp and not ramp:
                    why = "current volume unknown (GET failed)" if not got_cur else f"already at {new_vol}%"
                    print(f"   (no ramp — {why}; setting directly)", flush=True)
                if ramp:
                    step_s = max(0.05, args.volume_ramp_step)
                    dur    = args.volume_ramp
                    delta  = new_vol - cur_vol
                    # Wall-clock ramp: the level is a function of ELAPSED time, and each SET is a
                    # blocking request/response — so we absorb the round-trip into the schedule
                    # instead of adding it on top. The ramp finishes in ~dur no matter how slow the
                    # camera replies (a slow RTT just lowers resolution). step_s is the MIN spacing
                    # between SETs (flood cap): we wake on an absolute grid (never drifts with RTT)
                    # and only SET when the time-scheduled level actually changes.
                    print(f"   ↗ Ramping {cur_vol}% → {new_vol}% over ~{dur:g}s "
                          f"(≥{step_s:g}s between SETs, timer={t_name})", flush=True)
                    t0 = time.monotonic()
                    tick, last_sent, n_sets = 0, cur_vol, 0
                    while True:
                        elapsed = time.monotonic() - t0
                        frac = 1.0 if elapsed >= dur else elapsed / dur
                        lvl = int(round(cur_vol + delta * frac))
                        if lvl != last_sent:
                            transport.ioctl(*build_set_lullaby_vol_duration(lvl, new_timer))
                            last_sent, n_sets = lvl, n_sets + 1
                            print(f"     · {lvl}%  (t+{elapsed:.2f}s)", flush=True)
                        if frac >= 1.0:
                            break
                        tick += 1
                        nap = min(t0 + tick * step_s, t0 + dur) - time.monotonic()
                        if nap > 0:
                            time.sleep(nap)
                    print(f"   ✅ Volume={new_vol}%  Timer={t_name} "
                          f"(ramped in {time.monotonic() - t0:.1f}s, {n_sets} SETs)")
                else:
                    transport.ioctl(*build_set_lullaby_vol_duration(new_vol, new_timer))
                    print(f"   ✅ Volume={new_vol}%  Timer={t_name}")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Play ─────────────────────────────────────────────────
        if args.play:
            result = find_song(args.play)
            if not result:
                print(f"\n❌ No song matching '{args.play}' — use --list-songs")
            else:
                uuid, name = result
                print(f"\n🎵 Playing: {name}...", flush=True)
                try:
                    transport.ioctl(*build_set_lullaby_play(uuid))
                    print(f"   ✅ Now playing: {name}")
                except Exception as e:
                    print(f"   ❌ Failed: {e}")

        # ── Stop ─────────────────────────────────────────────────
        if args.stop:
            print(f"\n⏹  Stopping lullaby...", flush=True)
            try:
                tc, data = transport.ioctl(*build_get_lullaby_vol_duration())
                uuid = ""
                if tc == IOTYPE_USER_GET_LULLABY_VOL_DURATION_RESP and len(data) >= 20:
                    lv = LullabyVolDuration.parse(data)
                    uuid = lv.current_song_uuid
                transport.ioctl(*build_set_lullaby_stop(uuid))
                print("   ✅ Stopped")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Sleep mode ───────────────────────────────────────────
        if args.sleep_mode:
            on = args.sleep_mode == 'on'
            print(f"\n😴 Sleep mode → {'ON' if on else 'OFF'}...", flush=True)
            try:
                transport.ioctl(*build_set_sleep_mode(on))
                print("   ✅ Done")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Night vision / status light / video flip / volumes (SET_HW_CONTROL) ──
        if args.night_vision:
            print(f"\n🌙 Night vision → {args.night_vision}...", flush=True)
            try:    transport.set_night_vision(args.night_vision); print("   ✅ Sent (firmware-managed; may not change)")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.status_light:
            on = args.status_light == 'on'
            print(f"\n🔆 Status LED → {'ON' if on else 'OFF'}...", flush=True)
            try:    transport.set_status_light(on); print("   ✅ Sent (firmware-managed; may not change)")
            except Exception as e: print(f"   ❌ Failed: {e}")
        _flip = args.flip_screen or args.video_flip
        if _flip:
            on = _flip == 'on'
            print(f"\n🔄 Flip screen → {'ON' if on else 'OFF'}...", flush=True)
            try:    transport.set_video_flip(on); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.mic_volume is not None:
            print(f"\n🎙  Mic level → {args.mic_volume}...", flush=True)
            try:    transport.set_mic_volume(args.mic_volume); print("   ✅ Sent (firmware-managed; may not change)")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.speaker_volume is not None:
            print(f"\n📢 Speaker level → {args.speaker_volume}...", flush=True)
            try:    transport.set_speaker_volume(args.speaker_volume); print("   ✅ Sent (firmware-managed; may not change)")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Cry / cough detection ────────────────────────────────
        # sensitivity labels map INVERTED to the wire: low=3, medium=2, high=1 (S28).
        _SENS = {'low': 3, 'medium': 2, 'high': 1}
        if args.cry_detection or args.cry_sensitivity is not None:
            cur = transport.get_cry_detection()
            on  = (args.cry_detection == 'on') if args.cry_detection else cur.get('enabled', True)
            sens = _SENS[args.cry_sensitivity] if args.cry_sensitivity else cur.get('sensitivity', 2)
            slab = {1:'High',2:'Medium',3:'Low'}.get(sens, sens)
            print(f"\n👶 Cry detection → {'ON' if on else 'OFF'} sensitivity={slab}...", flush=True)
            try:    transport.set_cry_detection(enabled=on, sensitivity=sens); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.cough_detection or args.cough_mode or args.cough_sensitivity:
            on = (args.cough_detection == 'on') if args.cough_detection else None
            in_crib = {'always': False, 'in-crib': True}.get(args.cough_mode) if args.cough_mode else None
            sens = _SENS[args.cough_sensitivity] if args.cough_sensitivity else None
            mode_txt = f" mode={args.cough_mode}" if args.cough_mode else ""
            print(f"\n🤧 Cough detection → {args.cough_detection or 'unchanged'}{mode_txt}...", flush=True)
            try:    transport.set_cough_detection(enabled=on, in_crib=in_crib, sensitivity=sens); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Sleep-safety mode (high-level: mutually-exclusive radio) ──
        # safety_alert=1,cover=0 → "Covered Face + Rollover"; cover=1,safety=0 →
        # "Covered Face Only"; both 0 → off  (S28, APK switchSleepSafetyDetectionType).
        if args.sleep_alerts:
            sa, ca = {'covered-and-rollover': (1, 0),
                      'covered-only':         (0, 1),
                      'off':                  (0, 0)}[args.sleep_alerts]
            print(f"\n🛡  Sleep alerts → {args.sleep_alerts} (safety={sa} cover={ca})...", flush=True)
            try:    transport.set_sleep_safety_setting(safety_alert=sa, cover_alert=ca); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Sleep-safety alerts (low-level individual flags, read-modify-write) ──
        if args.safety_alert or args.cover_alert or args.baby_presence_alert:
            cur = transport.get_sleep_safety_setting()
            def _b(flag, key): return (flag == 'on') if flag else bool(cur.get(key))
            sa = _b(args.safety_alert, 'safety_alert')
            ca = _b(args.cover_alert, 'cover_alert')
            bp = _b(args.baby_presence_alert, 'baby_presence_alert')
            se = int(cur.get('sensitivity') or 0)
            print(f"\n🛡  Sleep-safety → safety={sa} cover={ca} baby_presence={bp}...", flush=True)
            try:    transport.set_sleep_safety(int(sa), int(ca), se, int(bp)); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Danger-zone alert on/off (toggles roi.enable, the app's switch path) ──
        if args.danger_zone_alert:
            on = args.danger_zone_alert == 'on'
            print(f"\n⛔ Danger-zone alert → {'ON' if on else 'OFF'}...", flush=True)
            try:    transport.set_danger_zone(enable=1 if on else 0); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Auto event-snapshot mode ─────────────────────────────
        if args.auto_capture:
            mode = {'off': 0, 'motion': 1, 'schedule': 2, 'both': 3}[args.auto_capture]
            print(f"\n📸 Auto-capture → {args.auto_capture} (mode {mode})...", flush=True)
            try:    transport.set_auto_capture(mode); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Lullaby schedule volume ──────────────────────────────
        if args.schedule_volume is not None:
            print(f"\n🔊 Lullaby schedule volume → {args.schedule_volume}...", flush=True)
            try:    transport.set_lullaby_schedule(volume=args.schedule_volume); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Lullaby schedule TABLE add/delete (gated; SET_LULLABY_SCHEDULE 0x0990) ──
        if args.delete_lullaby_schedule:
            print(f"\n🗑  Delete lullaby schedule '{args.delete_lullaby_schedule}'...", flush=True)
            try:    transport.delete_lullaby_schedule(args.delete_lullaby_schedule); print("   ✅ Sent (live-confirmed; verify via read-back + app if desired)")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.add_lullaby_schedule:
            song = args.add_lullaby_schedule
            sname = args.schedule_name or song
            # parse start HH:MM
            sh, sm = 0, 0
            if args.schedule_start:
                try:
                    hh, mm = args.schedule_start.split(':')
                    sh, sm = int(hh), int(mm)
                except Exception:
                    parser.error("--schedule-start must be HH:MM")
            # parse day mask: 'all'/'everyday' → 0x7f, else decimal or 0xNN
            dspec = (args.schedule_days or 'all').strip().lower()
            if dspec in ('all', 'everyday', 'mon-sun', 'daily'):
                dmask = 0x7f
            else:
                try:    dmask = int(dspec, 0) & 0x7f
                except Exception: parser.error("--schedule-days must be 'all' or a mask (e.g. 0x7f)")
            print(f"\n🗓  Add lullaby schedule '{sname}' → {song} @ "
                  f"{sh:02d}:{sm:02d} days=0x{dmask:02x} "
                  f"dur={args.schedule_duration or 0}min "
                  f"ai={args.schedule_ai} "
                  f"{'(local-time)' if args.schedule_local_time else ''}"
                  f"{' [disabled]' if args.schedule_disable else ''}...", flush=True)
            try:
                transport.add_lullaby_schedule(
                    sname, song=song, days_mask=dmask, start_hour=sh, start_minute=sm,
                    duration_min=args.schedule_duration, enable=not args.schedule_disable,
                    ai=(args.schedule_ai == 'on'), use_local_time=args.schedule_local_time)
                print("   ✅ Sent (live-confirmed; verify via read-back + app if desired)")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Environment / comfort-range thresholds (read-modify-write) ──
        # --comfort-* are the friendly aliases of --temp-*/--humi-*.
        _t_lo = args.comfort_temp_low  if args.comfort_temp_low  is not None else args.temp_low
        _t_hi = args.comfort_temp_high if args.comfort_temp_high is not None else args.temp_high
        _h_lo = args.comfort_humi_low  if args.comfort_humi_low  is not None else args.humi_low
        _h_hi = args.comfort_humi_high if args.comfort_humi_high is not None else args.humi_high
        if any(v is not None for v in (args.temp_alert, _t_lo, _t_hi,
                                       args.humi_alert, _h_lo, _h_hi)):
            kw = {}
            if args.temp_alert: kw['temp_alert'] = 1 if args.temp_alert == 'on' else 0
            if args.humi_alert: kw['humi_alert'] = 1 if args.humi_alert == 'on' else 0
            if _t_lo is not None: kw['temp_low']  = _t_lo
            if _t_hi is not None: kw['temp_high'] = _t_hi
            if _h_lo is not None: kw['humi_low']  = _h_lo
            if _h_hi is not None: kw['humi_high'] = _h_hi
            print(f"\n🌡  Comfort range → {kw}...", flush=True)
            try:    transport.set_environment_alert(**kw); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

    finally:
        transport.disconnect()
        print("\nDisconnected.")


if __name__ == '__main__':
    main()
