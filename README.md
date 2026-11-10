# Stream API

A FastAPI server that exposes a Vadzo Falcon-2020CRS camera (onsemi AR2020, 20 MP colour, USB 3), a USB serial scale and the battery BMS over HTTP — enabling web and mobile clients on the same LAN to stream live video, capture images and read weight and battery state without needing GUI access.

## Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/stream` | Live MJPEG color stream (full view, scaled to `?preset=sd\|hd\|fhd`, default `fhd`) |
| GET | `/capture` | A single JPEG, **unprocessed** — the camera's own picture at its native size (`?preset=` to scale it down). No crop, de-glare or enhance |
| POST | `/camera/controls` | The camera's built-in ISP controls: exposure, gain, white balance, colour, HDR, denoise, flicker, flip, ... |
| GET | `/scale/stream` | Live weight stream (SSE) — see `SCALE_API.md` |
| GET | `/scale/capture` | Read current weight — see `SCALE_API.md` |
| GET | `/battery/capture` | Read current battery state from the BMS |
| GET | `/health` | Per-device status + last error |
| GET | `/docs` | Interactive API documentation (Swagger UI) |

See `/docs` for the full list, including the `POST` recovery and system-control endpoints.

---

## Setup on Jetson

> **Note:** The camera SDK (`vispa`, Vadzo's VISPA Falcon SDK) is not on PyPI. Vadzo ships it as one wheel per Python version and CPU architecture; the pack in `Falcon-2020CRS/` holds the **Python 3.13 / aarch64** wheel, which is why this project runs on Python 3.13. The Jetson must be on **JetPack 6 or newer** (Ubuntu 22.04+): the wheel needs glibc ≥ 2.28 and Vadzo's firmware tool ≥ 2.34. No CUDA or GPU is needed for the camera.

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y git libssl-dev libusb-1.0-0-dev libgtk-3-dev python3-dev pkg-config
```

### 2. Check the platform

```bash
cat /etc/nv_tegra_release   # JetPack 6 = L4T R36
ldd --version | head -1     # glibc 2.35 on JetPack 6; must be >= 2.28
uname -m                    # aarch64
```

### 3. Camera permissions

The `Falcon-2020CRS/` vendor pack is git-ignored (Vadzo marks it confidential), so a fresh clone does not have it: copy it into the checkout first (`scp -r Falcon-2020CRS <jetson-user>@<jetson-ip>:~/StreamAPI/`).


Vadzo's `vadzo_env.sh` installs a udev rule (`/etc/udev/rules.d/99-vadzo.rules`) that opens the camera's USB and HID control interface (vendor `eba1`, product `3e00`) to all users. Without it the SDK cannot see or control the camera. The `/dev/video*` nodes need `video`, and the scale's serial adapter still needs `plugdev` (or `dialout`, see `JETSON_SETUP.md`):

```bash
sudo bash Falcon-2020CRS/vadzo_env.sh
sudo usermod -aG plugdev,video $USER
# Log out and back in, then replug the camera
```

Vadzo's firmware tool (`Falcon-2020CRS/vadzo-dfu_1.2.0_arm64.deb`) is **not** needed to run the service. The firmware image bundled in it is named `FALCON-2020BRS.img`, not CRS, so do not flash it without confirming with Vadzo first.

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

### 7. Create the venv on Python 3.13 and install the camera SDK

`uv` installs its own Python 3.13 (from `.python-version`); the system Python is not used and no system site-packages are needed:

```bash
uv venv --python 3.13
uv sync
# The camera SDK is not on PyPI: install Vadzo's wheel into the venv.
# --inexact on later syncs keeps uv from removing it again.
uv pip install Falcon-2020CRS/vispa-1.11-cp313-cp313-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl
```

After this, use `uv sync --inexact` (plain `uv sync` removes packages not listed in `pyproject.toml`, and `vispa` deliberately is not).

### 8. Verify the camera is detected

```bash
uv run python -c "import vispa; print('SDK', vispa.version())"
uv run python -c "from VISPA_Falcon import FalconCamera; print(FalconCamera().falcon_controller().get_devices())"
```

You should see the camera's name in the list. An empty list means USB or permissions (step 3). Stop the service first if it is running: the camera accepts one client at a time.

### 9. Run the server

```bash
uv run stream-api
```

The API will be accessible from any device on the same LAN:

- Stream: `http://<jetson-ip>:8000/stream`
- Capture: `http://<jetson-ip>:8000/capture` (native size; `?preset=fhd` for a smaller image)
- Docs: `http://<jetson-ip>:8000/docs`

> To find the Jetson's IP: `hostname -I | awk '{print $1}'`

---

## Run as a systemd service (auto-start on boot)

For production hosting — service starts on Jetson boot, restarts on failure, logs to the system journal — see **[DEPLOYMENT.md](./DEPLOYMENT.md)**.

---

## Troubleshooting

**`/health` says "the VISPA camera SDK is not installed in this venv"**
The `vispa` wheel is missing from the venv, usually because a plain `uv sync` removed it. Reinstall it (step 7) and use `uv sync --inexact` from then on. `is not a supported wheel on this platform` means the venv is not Python 3.13 or the machine is not aarch64: `uv run python --version`, `uname -m`.

**`/health` says "no Falcon camera found"**
- Check the USB cable and that the camera is on a USB 3 port (`lsusb -t` shows `5000M`; `lsusb` lists vendor `eba1`)
- Run `sudo bash Falcon-2020CRS/vadzo_env.sh` if it was never run, then replug the camera
- Confirm the service user is in `video` (`id`)

**`/health` says the camera is busy**
Another process holds it (a vendor viewer, a probe script, a second copy of the service); the camera accepts one client at a time. Close it, and the service reconnects on the next request.

**`/health` says "camera opened but would not stream"**
None of the pixel formats tried (MJPG, UYVY, YUYV, BA81) produced a frame; the error lists the SDK's message for each. Usually USB bandwidth (use a USB 3 port and a short certified cable), or a size or frame rate the camera cannot do: unset `STREAM_API_CAMERA_SIZE` / `STREAM_API_CAMERA_FPS` and retry.

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
