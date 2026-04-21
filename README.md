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

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y git libssl-dev libusb-1.0-0-dev libgtk-3-dev python3-dev pkg-config
```

### 2. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

### 3. Configure RealSense udev rules

This allows the RealSense device to be accessed without `sudo`.

```bash
sudo apt install -y librealsense2-utils
sudo cp /etc/udev/rules.d/99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

> If `librealsense2-utils` is not available via apt, download the udev rules manually:
> ```bash
> curl -LO https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules
> sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/
> sudo udevadm control --reload-rules && sudo udevadm trigger
> ```

### 4. Clone the repository

```bash
git clone https://github.com/abumuadhsalih/StreamAPI.git
cd StreamAPI
```

### 5. Install Python dependencies

```bash
uv sync
```

This will install `fastapi[standard]`, `opencv-python-headless`, and `pyrealsense2` automatically.

### 6. Verify RealSense camera is detected

```bash
uv run python -c "import pyrealsense2 as rs; ctx = rs.context(); print(f'Devices found: {len(ctx.devices)}')"
```

You should see `Devices found: 1` (or more). If it shows `0`, check the USB connection and udev rules.

### 7. Run the server

```bash
uv run fastapi run main.py --host 0.0.0.0 --port 8000
```

The API will be accessible from any device on the same LAN:

- Stream: `http://<jetson-ip>:8000/stream`
- Capture: `http://<jetson-ip>:8000/capture`
- Docs: `http://<jetson-ip>:8000/docs`

> To find the Jetson's IP: `hostname -I | awk '{print $1}'`

---

## Troubleshooting

**`No module named 'pyrealsense2'`**
Run `uv sync` again. If it still fails, check that you're on Linux — pyrealsense2 is Linux-only.

**`Devices found: 0`**
- Check USB cable is connected and RealSense is powered
- Re-run the udev rules steps above and replug the device
- Try `rs-enumerate-devices` if `librealsense2-utils` is installed

**Permission denied on USB device**
```bash
sudo usermod -aG plugdev $USER
# Log out and back in for the group change to take effect
```

**Port 8000 already in use**
```bash
uv run fastapi run main.py --host 0.0.0.0 --port 8080
```
