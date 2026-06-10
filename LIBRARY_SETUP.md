# Obtaining the TUTK Library (only required for users wanting to use the native library instead of pure python)

## Why you need this

The CuboAI camera uses the **ThroughTek (TUTK) P2P SDK** to handle the
encrypted peer-to-peer connection between clients and the camera. The core of
this integration — `cuboai_tutk.py` — wraps this native library via Python
`ctypes`.

**The library cannot be redistributed.** It is proprietary software owned by
ThroughTek Co. Ltd. You must obtain it yourself.

---

## Architecture summary

| Architecture | Hardware | Library source |
|---|---|---|
| x86-64 | Intel/AMD Linux, most HA VMs | wyze-bridge Docker image OR TUTK SDK |
| aarch64 | Raspberry Pi 4/5 | CuboAI APK (arm64-v8a) |

---

## x86-64 (Intel/AMD Linux)

The CuboAI APK only ships ARM libraries. For x86-64 you have two options:

### Option A: Extract from wyze-bridge (easiest)

The [wyze-bridge](https://github.com/mrlt8/docker-wyze-bridge) project
bundles a compatible x86-64 `libIOTCAPIs_ALL.so`. Pull the Docker image
and copy the library out:

```bash
# Pull the image (no account needed)
docker pull mrlt8/wyze-bridge:latest

# Extract the library
docker create --name tmp mrlt8/wyze-bridge:latest
docker cp tmp:/app/wyzecam/tutk/libIOTCAPIs_ALL.so ./libs/x86_64/
docker rm tmp
```

### Verify it loaded correctly

```bash
python3 -c "
import ctypes
lib = ctypes.CDLL('./libs/x86_64/libIOTCAPIs_ALL.so')
lib.IOTC_Get_Version_String.restype = ctypes.c_char_p
print('Version:', lib.IOTC_Get_Version_String().decode())
"
# Expected: Version: 4.x.x.x-H
```

---

## aarch64 (Raspberry Pi 4/5)

> ⚠️ **Untested on real hardware.** The library loading code is implemented
> but has not been verified on an actual Raspberry Pi. In particular, the
> `_AVClientStartInConfig` struct field offsets may differ on ARM64 due to
> stricter pointer alignment requirements. If you test this on a Pi, please
> report your findings.

The arm64 build ships as **three separate libraries** (not combined).
Extract all three from the CuboAI APK. The arm64 libraries are in the
**architecture split APK** (`split_config.arm64_v8a.apk`), not the base APK:

```bash
# Get all APKs for the app from your Android device
adb shell pm path com.getcubo.app
# This lists multiple paths — find the one containing 'arm64_v8a'
# e.g. /data/app/.../split_config.arm64_v8a.apk

adb pull /data/app/.../split_config.arm64_v8a.apk cuboai_arm64.apk

# Extract the arm64 libraries
unzip cuboai_arm64.apk 'lib/arm64-v8a/*' -d extracted/
mkdir -p libs/aarch64
cp extracted/lib/arm64-v8a/libTUTKGlobalAPIs.so libs/aarch64/
cp extracted/lib/arm64-v8a/libIOTCAPIs.so       libs/aarch64/
cp extracted/lib/arm64-v8a/libAVAPIs.so         libs/aarch64/
```

They must be loaded in this order (handled automatically by `cuboai_tutk.py`):
1. `libTUTKGlobalAPIs.so` — core P2P engine
2. `libIOTCAPIs.so` — IOTC session management
3. `libAVAPIs.so` — AV channel (this is the one we call directly)

---

## Place the library

Put the files alongside the Python scripts:

```
cuboai/
├── cuboai_tutk.py
├── cuboai_messages.py
├── cuboai_validate.py
└── libs/
    ├── x86_64/
    │   └── libIOTCAPIs_ALL.so
    └── aarch64/
        ├── libTUTKGlobalAPIs.so
        ├── libIOTCAPIs.so
        └── libAVAPIs.so
```

Or pass the path explicitly:
```bash
python3 cuboai_validate.py --lib /path/to/libIOTCAPIs_ALL.so ...
```

---

## Copyright notice

`libIOTCAPIs_ALL.so`, `libIOTCAPIs.so`, `libAVAPIs.so`, and
`libTUTKGlobalAPIs.so` are proprietary software owned by
**ThroughTek Co. Ltd.** They are embedded in the CuboAI app and the
wyze-bridge project under commercial licences. You may use them for
personal use on hardware you own, but you may **not** redistribute them,
sell them, or use them in commercial products without a licence from
ThroughTek.

See: https://www.throughtek.com/


---

## What you need

- A computer with Python 3.9+ and `adb` installed
- An Android device or emulator with the CuboAI app installed
- OR: just the CuboAI APK file (no device needed)

