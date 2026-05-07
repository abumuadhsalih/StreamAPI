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
uv run fastapi run main.py --host 0.0.0.0 --port 8000
```

The API will be accessible from any device on the same LAN:

- Stream: `http://<jetson-ip>:8000/stream`
- Capture: `http://<jetson-ip>:8000/capture`
- Docs: `http://<jetson-ip>:8000/docs`

> To find the Jetson's IP: `hostname -I | awk '{print $1}'`

---

## Expose the API externally (Cloudflare Tunnel)

Used during POC so the mobile app developer can hit the API from outside the LAN. **Temporary** — remove once the mobile app integrates with the production setup.

> ⚠️ The API has no authentication. Only share the tunnel URL with trusted developers and tear it down when not actively in use.

### 1. Install cloudflared on Jetson (ARM64)

```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb
```

### 2. Start the API server (in one terminal)

```bash
uv run fastapi run main.py --host 0.0.0.0 --port 8000
```

### 3. Start the tunnel (in another terminal)

```bash
cloudflared tunnel --url http://localhost:8000
```

Cloudflare prints a public HTTPS URL like:
```
https://abc-random-words-1234.trycloudflare.com
```

Share that URL with the mobile app developer. Endpoints become:
- `https://<tunnel-url>/scale/stream`
- `https://<tunnel-url>/scale/capture`
- `https://<tunnel-url>/stream?preset=hd`
- `https://<tunnel-url>/capture?preset=fhd`
- `https://<tunnel-url>/docs`

### Notes

- The URL changes every time you restart the tunnel. For a stable URL, set up a named tunnel with a Cloudflare account + domain.
- For 1080p streaming over the public internet, prefer `?preset=hd` or `?preset=sd` to avoid bandwidth issues.
- To run the tunnel persistently in the background, use a systemd service or `nohup cloudflared tunnel --url http://localhost:8000 &`.
- To tear down: `Ctrl+C` the cloudflared process — the public URL stops resolving immediately.

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
uv run fastapi run main.py --host 0.0.0.0 --port 8080
```
