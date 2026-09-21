# ZED Stream API

A FastAPI server that exposes a Stereolabs ZED 2i camera (RGB), a USB serial scale and the battery BMS over HTTP — enabling web and mobile clients on the same LAN to stream live video, capture images and read weight and battery state without needing GUI access.

## Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/stream` | Live MJPEG color stream |
| GET | `/capture` | Capture and return a single JPEG image (raw auto-exposed frame; `?enhance=true` for CLAHE + shadow lift) |
| GET | `/scale/stream` | Live weight stream (SSE) |
| GET | `/scale/capture` | Read current weight |
| GET | `/battery/capture` | Read current battery state from the BMS |
| GET | `/health` | Per-device status + last error |
| GET | `/docs` | Interactive API documentation (Swagger UI) |

See `/docs` for the full list, including the `POST` recovery and system-control endpoints.

---

## Setup on Jetson

> **Note:** The camera SDK (`pyzed`) is not on PyPI and needs CUDA, so it comes from the ZED SDK installer rather than `uv sync`. The Jetson must run JetPack natively (CUDA at `/usr/local/cuda`); the SDK will not start without a GPU, even for RGB-only capture.

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y git libssl-dev libusb-1.0-0-dev libgtk-3-dev python3-dev pkg-config
```

### 2. Install the ZED SDK (matches the Jetson's JetPack / L4T release)

```bash
# Confirm the L4T release and that CUDA is present — the SDK build must match L4T
cat /etc/nv_tegra_release
ls /usr/local/cuda

# Download the "ZED SDK for JetPack <your version>" installer from
# https://www.stereolabs.com/developers/release and run it
chmod +x ZED_SDK_Tegra_L4T*.run
./ZED_SDK_Tegra_L4T*.run
```

The installer lands in `/usr/local/zed/` (tools in `/usr/local/zed/tools/`, udev rule at `/etc/udev/rules.d/99-slabs.rules`). Accept the default when it asks about the Python API, or install it into the venv in step 7 — either works, but the venv route is what step 7 shows.

On first `open()` the SDK downloads the camera's factory calibration file into `/usr/local/zed/settings/SN<serial>.conf`. That needs internet **once**; on a LAN-only Jetson run `/usr/local/zed/tools/ZED_Diagnostic` while online, or copy the file in by hand.

### 3. Camera permissions

The SDK's udev rule opens the camera to all users, so no group is needed for the ZED itself. The scale's serial adapter still needs `plugdev` (or `dialout` — see `JETSON_SETUP.md`):

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev,video $USER
# Log out and back in, then replug the camera
```

### 4. Enable Bluetooth for the battery BMS

The battery is read over Bluetooth LE. BLE goes through D-Bus to `org.bluez` — there is no device node, so the `plugdev` group from step 3 does **not** grant access to it.

```bash
# The radio ships blocked on some JetPack images
rfkill unblock bluetooth

# An adapter must be present and powered
bluetoothctl show          # expect "Powered: yes"

sudo usermod -aG bluetooth $USER
# Log out and back in for the group change to take effect
```

Confirm D-Bus actually lets your user talk to BlueZ — run this as the service user, **not** as root:

```bash
busctl --system call org.bluez /org/bluez org.freedesktop.DBus.Introspectable Introspect
```

`Rejected send message` here means the D-Bus policy in `/etc/dbus-1/system.d/bluetooth.conf` is missing a rule for the `bluetooth` group. That is a system configuration problem, not an application one.

> **Do not pair the pack.** The JBD BMS needs no bonding, and a paired device makes `bluetoothd` auto-reconnect and hold the one connection the pack allows. If it is already paired: `bluetoothctl` → `disconnect <MAC>` → `remove <MAC>`.

### 5. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

### 6. Clone the repository

```bash
git clone https://github.com/abumuadhsalih/StreamAPI.git
cd StreamAPI
```

### 7. Create venv with system site-packages and sync

The `pyzed` wheel is built against the system CUDA and NumPy, so the venv uses the system interpreter and sees system packages:

```bash
uv venv --python /usr/bin/python3 --system-site-packages
uv sync
# Install the Python API into the venv (downloads the wheel matching CUDA/L4T/Python)
uv run python /usr/local/zed/get_python_api.py
```

### 8. Verify the ZED camera is detected

```bash
uv run python -c "import pyzed.sl as sl; d = sl.Camera.get_device_list(); print(f'Devices found: {len(d)}', [str(x.camera_state) for x in d])"
```

You should see `Devices found: 1 ['AVAILABLE']`. `0` means USB/udev; `NOT_AVAILABLE` means another process (e.g. `ZED_Explorer`) holds the camera — a ZED accepts one client at a time.

### 9. Run the server

```bash
uv run stream-api
```

The API will be accessible from any device on the same LAN:

- Stream: `http://<jetson-ip>:8000/stream`
- Capture: `http://<jetson-ip>:8000/capture` (enhanced: `http://<jetson-ip>:8000/capture?enhance=true`)
- Docs: `http://<jetson-ip>:8000/docs`

> To find the Jetson's IP: `hostname -I | awk '{print $1}'`

---

## Run as a systemd service (auto-start on boot)

For production hosting — service starts on Jetson boot, restarts on failure, logs to the system journal — see **[DEPLOYMENT.md](./DEPLOYMENT.md)**.

---

## Troubleshooting

**`No module named 'pyzed'`**
The Python API was not installed into this venv. Run `uv run python /usr/local/zed/get_python_api.py` (step 7). If the venv was created without `--system-site-packages` / the system interpreter, recreate it:
```bash
rm -rf .venv
uv venv --python /usr/bin/python3 --system-site-packages
uv sync
uv run python /usr/local/zed/get_python_api.py
```

**`Devices found: 0`**
- Check the USB cable and that the camera is on a USB 3.0 port (`lsusb -t` shows `5000M`; the vendor id is `2b03`)
- Re-run `sudo udevadm control --reload-rules && sudo udevadm trigger` and replug the device
- Run `/usr/local/zed/tools/ZED_Diagnostic` to confirm the SDK sees the camera — then close it before starting the service

**`/health` says the camera is held by another process**
Close `ZED_Explorer` / `ZED_Diagnostic` (or any other SDK client); the ZED accepts one at a time. The service reconnects on its own.

**`camera open refused ... CALIBRATION_FILE_NOT_AVAILABLE`**
The Jetson had no internet the first time the camera was opened. Connect it once and `POST /camera/restart`, or copy `SN<serial>.conf` into `/usr/local/zed/settings/`.

**`camera open refused ... NO_GPU_COMPATIBLE` / CUDA errors**
The SDK build does not match the installed JetPack, or CUDA is missing. Compare `cat /etc/nv_tegra_release` with the SDK version you downloaded.

**Port 8000 already in use**
```bash
STREAM_API_PORT=8080 uv run stream-api
```

**The JBD phone app cannot connect to the battery**
Expected, and it is not a fault. A JBD pack accepts **one** BLE client at a time, and this service holds the link continuously while it runs. To use the phone app, stop the service first:
```bash
sudo systemctl stop stream-api
# ...use the app, then:
sudo systemctl start stream-api
```

**`/battery/capture` returns 503 "no BLE advertisement from ..."**
The pack stops advertising while another client holds it, so "not found" almost always means "something else is connected", not "out of range". In order of likelihood:
- A phone app is connected — close it.
- `bluetoothd` auto-reconnected a paired device — `bluetoothctl` → `disconnect <MAC>`, then `remove <MAC>`.
- The pack is switched off, or `rfkill unblock bluetooth` was never run.

Confirm the pack is visible at the system level with `bluetoothctl scan on` and look for the MAC in `/health`'s `battery.error`.

**`/battery/capture` flaps between 200 and 503 while streaming video**
The Jetson's combo radio shares an antenna between Wi-Fi and Bluetooth, and this box streams FHD MJPEG over Wi-Fi. Heavy streaming can drop the BLE link. The reader already tolerates this — it keeps serving the last reading for 60s and reconnects on its own, so brief flaps should be invisible. If they are not, move Bluetooth to a separate radio with a USB dongle.

**The battery MAC is different on this unit**
Every BMS has its own address. Override it rather than editing the source:
```bash
STREAM_API_BATTERY_ADDRESS=AA:BB:CC:DD:EE:FF uv run stream-api
```
