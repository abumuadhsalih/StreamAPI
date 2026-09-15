# RealSense Stream API

A FastAPI server that exposes an Intel RealSense camera, a USB serial scale and the battery BMS over HTTP — enabling web and mobile clients on the same LAN to stream live video, capture images and read weight and battery state without needing GUI access.

## Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/stream` | Live MJPEG color stream |
| GET | `/capture` | Capture and return a single JPEG image (enhanced by default; `?enhance=false` for the raw sensor frame) |
| GET | `/scale/stream` | Live weight stream (SSE) |
| GET | `/scale/capture` | Read current weight |
| GET | `/battery/capture` | Read current battery state from the BMS |
| GET | `/health` | Per-device status + last error |
| GET | `/docs` | Interactive API documentation (Swagger UI) |

See `/docs` for the full list, including the `POST` recovery and system-control endpoints.

---

## Setup on Jetson Ubuntu VM

> **Note:** Jetson uses ARM64 (`aarch64`). `pyrealsense2` is not available on PyPI for ARM64, so it must be installed separately via Intel's APT repository before running `uv sync`.

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y git libssl-dev libusb-1.0-0-dev libgtk-3-dev python3-dev pkg-config
```

### 2. Install pyrealsense2 via Intel APT repository (ARM64)

```bash
# Register Intel's key and repository
sudo mkdir -p /etc/apt/keyrings
curl -sSf https://librealsense.intel.com/Debian/librealsense.pgp \
  | sudo tee /etc/apt/keyrings/librealsense.pgp > /dev/null

echo "deb [signed-by=/etc/apt/keyrings/librealsense.pgp] \
  https://librealsense.intel.com/Debian/apt-repo $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/librealsense.list

sudo apt update
sudo apt install -y librealsense2-utils librealsense2-python3
```

This installs pyrealsense2 as a system package at `/usr/lib/python3/dist-packages/pyrealsense2`.

### 3. Configure RealSense udev rules

This allows the RealSense device to be accessed without `sudo`.

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev $USER
# Log out and back in, then replug the RealSense device
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

Because `pyrealsense2` was installed system-wide (step 2), the venv needs access to system packages:

```bash
uv venv --system-site-packages
uv sync
```

### 8. Verify RealSense camera is detected

```bash
uv run python -c "import pyrealsense2 as rs; ctx = rs.context(); print(f'Devices found: {len(ctx.devices)}')"
```

You should see `Devices found: 1` (or more). If it shows `0`, check the USB connection and udev rules.

### 9. Run the server

```bash
uv run stream-api
```

The API will be accessible from any device on the same LAN:

- Stream: `http://<jetson-ip>:8000/stream`
- Capture: `http://<jetson-ip>:8000/capture` (raw sensor frame: `http://<jetson-ip>:8000/capture?enhance=false`)
- Docs: `http://<jetson-ip>:8000/docs`

> To find the Jetson's IP: `hostname -I | awk '{print $1}'`

---

## Run as a systemd service (auto-start on boot)

For production hosting — service starts on Jetson boot, restarts on failure, logs to the system journal — see **[DEPLOYMENT.md](./DEPLOYMENT.md)**.

---

## Troubleshooting

**`uv sync` fails with pyrealsense2 wheel error**
This means you skipped step 2. `pyrealsense2` has no ARM64 PyPI wheel — it must be installed via Intel's APT repo first, then `uv venv --system-site-packages` used.

**`No module named 'pyrealsense2'` after uv sync**
The venv was created without `--system-site-packages`. Recreate it:
```bash
rm -rf .venv
uv venv --system-site-packages
uv sync
```

**`Devices found: 0`**
- Check USB cable is connected and RealSense is powered
- Re-run `sudo udevadm control --reload-rules && sudo udevadm trigger` and replug the device
- Try `rs-enumerate-devices` to confirm the device is visible at the system level

**Permission denied on USB device**
```bash
sudo usermod -aG plugdev $USER
# Log out and back in for the group change to take effect
```

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
