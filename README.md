# RealSense Stream API

A FastAPI server that exposes an Intel RealSense camera over HTTP — enabling web and mobile clients on the same LAN to stream live video and capture images without needing GUI access.

## Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/stream` | Live MJPEG color stream |
| GET | `/capture` | Capture and return a single JPEG image |
| GET | `/docs` | Interactive API documentation (Swagger UI) |

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

### 4. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

### 5. Clone the repository

```bash
git clone https://github.com/abumuadhsalih/StreamAPI.git
cd StreamAPI
```

### 6. Create venv with system site-packages and sync

Because `pyrealsense2` was installed system-wide (step 2), the venv needs access to system packages:

```bash
uv venv --system-site-packages
uv sync
```

### 7. Verify RealSense camera is detected

```bash
uv run python -c "import pyrealsense2 as rs; ctx = rs.context(); print(f'Devices found: {len(ctx.devices)}')"
```

You should see `Devices found: 1` (or more). If it shows `0`, check the USB connection and udev rules.

### 8. Run the server

```bash
uv run stream-api
```

The API will be accessible from any device on the same LAN:

- Stream: `http://<jetson-ip>:8000/stream`
- Capture: `http://<jetson-ip>:8000/capture`
- Docs: `http://<jetson-ip>:8000/docs`

> To find the Jetson's IP: `hostname -I | awk '{print $1}'`

---

## Run as a systemd service (auto-start on boot)

Once the venv works, host the API as a managed daemon so you never SSH in to start it manually.

### 1. Build the wheel (on your dev machine)

```bash
uv build
```

This produces `dist/stream_api-0.1.0-py3-none-any.whl`.

### 2. Copy the wheel to the Jetson

```bash
scp dist/stream_api-0.1.0-py3-none-any.whl <user>@<jetson-ip>:/tmp/
```

(Or skip this and just `git pull && uv build` on the Jetson itself.)

### 3. Install into a dedicated venv on the Jetson

```bash
sudo mkdir -p /opt/stream-api
sudo chown $USER:$USER /opt/stream-api
cd /opt/stream-api

uv venv --system-site-packages          # needed for system pyrealsense2
uv pip install /tmp/stream_api-0.1.0-py3-none-any.whl
```

Verify:

```bash
.venv/bin/stream-api    # Ctrl+C after it logs "Uvicorn running on http://0.0.0.0:8000"
```

### 4. Create the systemd unit

```bash
sudo tee /etc/systemd/system/stream-api.service > /dev/null <<'EOF'
[Unit]
Description=RealSense Stream API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<your-user>
Group=plugdev
WorkingDirectory=/opt/stream-api
ExecStart=/opt/stream-api/.venv/bin/stream-api
Environment=STREAM_API_HOST=0.0.0.0
Environment=STREAM_API_PORT=8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Replace `<your-user>` with the Linux user that owns the RealSense udev rule and the serial device (`plugdev` is the group from the udev step above).

### 5. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stream-api    # start now + on every boot
sudo systemctl status stream-api          # confirm "active (running)"
sudo journalctl -u stream-api -f          # tail live logs
```

### 6. Updating after a code change

```bash
# on dev machine
uv build
scp dist/stream_api-0.1.0-py3-none-any.whl <user>@<jetson-ip>:/tmp/

# on Jetson
uv pip install --reinstall /tmp/stream_api-0.1.0-py3-none-any.whl --python /opt/stream-api/.venv/bin/python
sudo systemctl restart stream-api
```

### Notes

- Keep `--workers 1` (the default). The RealSense pipeline and `/dev/ttyUSB0` are single-owner — a second worker will crash on startup trying to grab the same hardware.
- `Restart=on-failure` brings the service back if the camera/scale throws. If it crash-loops, `journalctl -u stream-api -n 200` will show why.

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
