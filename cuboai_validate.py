#!/usr/bin/env python3
"""
cuboai_validate.py — CuboAI camera validation and control tool.

Usage:
    python3 cuboai_validate.py --lib /path/to/libIOTCAPIs_ALL.so \\
                               --uid YOUR_DEVICE_UID \\
                               --account admin@YOUR_DEVICE_HEX \\
                               --password YOUR_PASSWORD \\
                               [--camera-ip 192.168.1.x]

Controls:
    --snapshot FILE          Save JPEG snapshot
    --record FILE            Record HEVC stream to file
    --duration SECS          Recording duration (default 10)
    --stream                 Stream HEVC to stdout (pipe to ffplay -f hevc -i -)
    --talk FILE              Send audio file to camera speaker
    --night-light on|off     Night light on/off
    --brightness 0-100       Night light brightness
    --volume 0-100           Lullaby volume
    --timer repeat|30min|60min  Sleep timer
    --play NAME              Play lullaby by name
    --stop                   Stop lullaby
    --sleep-mode on|off      Sleep/privacy mode
    --list-songs             List all songs
    --no-status              Skip status read

Environment: CUBOAI_LIB, CUBOAI_UID, CUBOAI_ACCOUNT, CUBOAI_PASSWORD, CUBOAI_CAMERA_IP
"""
import argparse
import os
import platform
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cuboai_tutk import TUTKSession
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
)


def find_song(query: str):
    q = query.lower().strip()
    for uuid, (key, name, category) in LULLABY_CATALOG.items():
        if q in name.lower() or q in key.lower():
            return uuid, name
    return None


def print_status(sess: TUTKSession) -> None:
    """Read and print all current camera state."""
    s = struct

    print("\n" + "═" * 60)
    print("  CuboAI Camera — Current State")
    print("═" * 60)

    # ── Hardware ─────────────────────────────────────────────────
    try:
        tc, data = sess.ioctl(*build_get_hw_control())
        if tc == IOTYPE_USER_GET_HW_CONTROL_RESP and len(data) >= 16:
            hw = HWControl.parse(data)
            print(f"\n🌡️  Temperature:    {hw.temperature:.1f} °C")
            print(f"💧 Humidity:       {hw.humidity:.1f} %")
            print(f"💡 Night light:    {'ON' if hw.night_light_on else 'OFF'}")
            print(f"📶 WiFi quality:   {hw.wifi_quality}%")
            print(f"⚙️  Firmware:       {hw.fw_version}")
            print(f"📡 SSID:           {hw.ssid}")
    except Exception as e:
        print(f"  ❌ Hardware status failed: {e}")

    # ── Night light brightness ───────────────────────────────────
    try:
        tc, data = sess.ioctl(*build_get_light_style())
        if tc == IOTYPE_USER_GET_LIGHT_STYLE_RESP and len(data) >= 28:
            ls = LightStyle.parse(data)
            print(f"💡 Brightness:     {ls.brightness}%")
    except Exception as e:
        print(f"  ❌ Brightness failed: {e}")

    # ── Status light (untested — may not exist on all models) ────
    try:
        tc, data = sess.ioctl(IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_REQ,
                              s.pack('<i', 0) + b'\x00' * 4)
        if tc == IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_RESP and len(data) >= 8:
            on = s.unpack_from('<I', data, 4)[0]
            print(f"🔴 Status LED:     {'ON' if on else 'OFF'}  [untested]")
    except Exception:
        pass  # silently skip — not present on all models

    # ── Firmware update info ─────────────────────────────────────
    try:
        tc, data = sess.ioctl(IOTYPE_USER_GET_UPDATE_INFO_REQ,
                              s.pack('<i', 0) + b'\x00' * 4)
        if tc == IOTYPE_USER_GET_UPDATE_INFO_RESP and len(data) >= 8:
            has_update = s.unpack_from('<I', data, 4)[0]
            if has_update:
                new_ver = data[8:20].split(b'\x00')[0].decode('ascii', 'replace')
                print(f"🔄 Update:         Available → {new_ver}")
            else:
                print(f"🔄 Firmware:       Up to date")
    except Exception as e:
        print(f"  ❌ Update info failed: {e}")

    # ── Lullaby ───────────────────────────────────────────────────
    print()
    try:
        tc, data = sess.ioctl(*build_get_lullaby_vol_duration())
        if tc == IOTYPE_USER_GET_LULLABY_VOL_DURATION_RESP and len(data) >= 20:
            lv = LullabyVolDuration.parse(data)
            print(f"🎵 Lullaby:        {'▶  Playing' if lv.is_playing else '⏹  Stopped'}")
            print(f"   Track:          {get_song_name(lv.current_song_uuid)}")
    except Exception as e:
        print(f"  ❌ Lullaby state failed: {e}")

    try:
        tc, data = sess.ioctl(2440, b'\x00' * 132)
        if tc == IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP and len(data) >= 16:
            sched = LullabySchedule.parse(data)
            print(f"   Volume:         {sched.volume}%")
            print(f"   Timer:          {sched.timer_name}")
    except Exception as e:
        print(f"  ❌ Volume/timer failed: {e}")

    # ── Safety & detection ────────────────────────────────────────
    print()

    try:
        tc, data = sess.ioctl(*build_get_sleep_mode())
        if len(data) >= 8:
            on = s.unpack_from('<I', data, 4)[0]
            print(f"😴 Sleep mode:     {'ON (camera suspended)' if on else 'OFF'}")
    except Exception as e:
        print(f"  ❌ Sleep mode failed: {e}")

    try:
        tc, data = sess.ioctl(*build_get_cry_detect())
        if len(data) >= 8:
            enabled = s.unpack_from('<I', data, 4)[0]
            print(f"👶 Cry detection:  {'Enabled' if enabled else 'Disabled'}")
    except Exception as e:
        print(f"  ❌ Cry detect failed: {e}")

    try:
        tc, data = sess.ioctl(*build_get_sleep_safety_setting())
        if len(data) >= 12:
            safety = s.unpack_from('<I', data, 4)[0]
            cover  = s.unpack_from('<I', data, 8)[0]
            print(f"🛡️  Sleep safety:   position={'ON' if safety else 'OFF'}  cover={'ON' if cover else 'OFF'}")
    except Exception as e:
        print(f"  ❌ Sleep safety failed: {e}")

    try:
        tc, data = sess.ioctl(*build_get_cough_setting())
        if len(data) >= 8:
            enabled = s.unpack_from('<I', data, 4)[0]
            print(f"🤧 Cough detect:   {'Enabled' if enabled else 'Disabled'}")
    except Exception as e:
        print(f"  ❌ Cough detect failed: {e}")

    # ── Sessions ──────────────────────────────────────────────────
    print()
    try:
        import datetime as _dt
        tc, data = sess.ioctl(*build_get_connected_users())
        if len(data) >= 248:
            RECORD_START = 128
            RECORD_SIZE  = 120
            CONN_TYPES   = {0: 'P2P', 1: 'Relay', 2: 'LAN'}
            sessions = []
            for i in range(3):
                off = RECORD_START + i * RECORD_SIZE
                if off + RECORD_SIZE > len(data):
                    break
                email = data[off:off+64].split(b'\x00')[0].decode('ascii', 'replace')
                if not email:
                    break
                ctype = s.unpack_from('<I', data, off+64)[0]
                ts    = s.unpack_from('<I', data, off+68)[0]
                t_str = _dt.datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts else '?'
                sessions.append(f"{email} ({CONN_TYPES.get(ctype, str(ctype))}) @ {t_str}")
            print(f"👥 Sessions ({len(sessions)}):")
            for sv in sessions:
                print(f"   {sv}")
    except Exception as e:
        print(f"  ❌ Sessions failed: {e}")

    print("\n" + "═" * 60)


def take_snapshot(sess: TUTKSession, path: str) -> None:
    print("📸 Taking snapshot...", flush=True)
    try:
        jpeg = sess.snapshot(timeout_sec=20.0)
        path = os.path.expanduser(path)
        with open(path, 'wb') as f:
            f.write(jpeg)
        print(f"   ✅ Saved: {path} ({len(jpeg)//1024} KB)")
    except ImportError:
        print("❌ Snapshot requires PyAV: pip install av")
    except TimeoutError as e:
        print(f"   ❌ {e}")
    except Exception as e:
        print(f"   ❌ Snapshot failed: {e}")


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

    parser.add_argument('--snapshot',    metavar='FILE',              help='Save JPEG snapshot to FILE')
    parser.add_argument('--record',      metavar='FILE',              help='Record HEVC stream to file')
    parser.add_argument('--duration',    type=float, default=10.0,    metavar='SECS')
    parser.add_argument('--stream-video', action='store_true',        help='Stream HEVC video to stdout (pipe to ffplay -f hevc -i -)')
    parser.add_argument('--talk',          metavar='FILE',              help='Send audio file to camera speaker')
    parser.add_argument('--record-audio',  metavar='FILE',              help='Record AAC audio to file (e.g. audio.aac)')
    parser.add_argument('--record-av',     metavar='BASE',              help='Record both streams: BASE.hevc + BASE.aac')
    parser.add_argument('--stream-audio',  action='store_true',         help='Stream raw AAC-ADTS to stdout')
    parser.add_argument('--night-light', choices=['on','off'],        help='Night light on/off')
    parser.add_argument('--brightness',  type=int,   metavar='0-100', help='Night light brightness %%')
    parser.add_argument('--volume',      type=int,   metavar='0-100', help='Lullaby volume %%')
    parser.add_argument('--timer',       choices=['repeat','30min','60min'])
    parser.add_argument('--play',        metavar='NAME',              help='Play lullaby by name')
    parser.add_argument('--stop',        action='store_true',         help='Stop lullaby')
    parser.add_argument('--sleep-mode',  choices=['on','off'],        help='Sleep/privacy mode')
    parser.add_argument('--list-songs',  action='store_true',         help='List all songs')
    parser.add_argument('--no-status',   action='store_true',         help='Skip status read')

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
    if camera_ip and platform.system() == 'Linux':
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

    missing = [k for k,v in [('--uid',uid),('--account',account),('--password',password)] if not v]
    if missing:
        print(f"❌ Missing: {', '.join(missing)}")
        print("   Set via args or env: CUBOAI_UID, CUBOAI_ACCOUNT, CUBOAI_PASSWORD")
        sys.exit(1)

    if args.brightness is not None and not 0 <= args.brightness <= 100:
        parser.error("--brightness must be 0-100")
    if args.volume is not None and not 0 <= args.volume <= 100:
        parser.error("--volume must be 0-100")

    # ── Connect ──────────────────────────────────────────────────
    print(f"\nConnecting to {uid}...", flush=True)
    transport = TUTKSession(uid, account, password, lib_path=lib_path, camera_ip=camera_ip)
    try:
        transport.connect()
        print("✅ Connected\n", flush=True)
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    try:
        if not args.no_status:
            print_status(transport)

        # ── Snapshot ─────────────────────────────────────────────
        if args.snapshot:
            take_snapshot(transport, args.snapshot)

        # ── Talk ─────────────────────────────────────────────────
        if args.talk:
            print(f"\n🎤 Sending audio to camera: {args.talk}", flush=True)
            try:
                transport.send_audio_file(args.talk)
                print("   ✅ Audio sent")
            except FileNotFoundError as e:
                print(f"   ❌ {e}")
            except RuntimeError as e:
                print(f"   ❌ {e}")
            except ImportError:
                print("   ❌ Requires PyAV: pip install av")

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

        # ── Record ───────────────────────────────────────────────
        if args.record:
            path = os.path.expanduser(args.record)
            print(f"🎥 Recording {args.duration}s to {path}...", flush=True)
            count = 0
            with open(path, 'wb') as f:
                for ftype, data in transport.av_frames(duration=args.duration):
                    if ftype == 'video':
                        f.write(data)
                        count += 1
            print(f"   ✅ {count} frames ({os.path.getsize(path)//1024} KB)")

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
                cur_vol, cur_timer = 50, LULLABY_TIMER_REPEAT
                if tc == IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP and len(data) >= 16:
                    sched = LullabySchedule.parse(data)
                    cur_vol, cur_timer = sched.volume, sched.timer_mode
                new_vol = args.volume if args.volume is not None else cur_vol
                timer_map = {'repeat': LULLABY_TIMER_REPEAT,
                             '30min':  LULLABY_TIMER_30MIN,
                             '60min':  LULLABY_TIMER_60MIN}
                new_timer = timer_map.get(args.timer, cur_timer) if args.timer else cur_timer
                transport.ioctl(*build_set_lullaby_vol_duration(new_vol, new_timer))
                t_name = {LULLABY_TIMER_REPEAT:'repeat',
                          LULLABY_TIMER_30MIN:'30min',
                          LULLABY_TIMER_60MIN:'60min'}.get(new_timer, '?')
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

    finally:
        transport.disconnect()
        print("\nDisconnected.")


if __name__ == '__main__':
    main()
