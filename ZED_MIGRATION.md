# ZED 2i migration — what to do on the Jetson

Checklist for the day you get remote access to a Jetson that the hardware team has
already fitted with the ZED 2i and installed the ZED SDK on. It takes an existing
D435 deployment (`/opt/stream-api` + the `stream-api` systemd unit, per
[DEPLOYMENT.md](./DEPLOYMENT.md)) to the `feature/zed-2i-camera` build.

Steps 1–2 change nothing — run them first, and stop if either fails. If the Jetson
has never been set up at all, use [JETSON_SETUP.md](./JETSON_SETUP.md) instead; this
document assumes a working service is already there.

Before you start, from the dev machine:

```bash
git push -u origin feature/zed-2i-camera
scp preflight.sh jetson@<jetson-ip>:~/
```

---

## 1. Confirm what the hardware team left you (read-only)

```bash
chmod +x ~/preflight.sh
~/preflight.sh
```

STEP 2 and STEP 3 must show:

- `/usr/local/cuda` present — the SDK is a CUDA library and will not open the camera without it
- `/usr/local/zed` present, `ZED_Diagnostic` present
- `/etc/udev/rules.d/99-slabs.rules` present
- `lsusb` shows vendor `2b03` **and** a `5000M` link — HD1080 refuses on a USB 2.0 port
- `/usr/local/zed/settings/SN*.conf` — the calibration file. Missing is fine for now; step 2 fetches it

Also compare the two version strings. A mismatch surfaces later as `NO_GPU_COMPATIBLE`:

```bash
cat /etc/nv_tegra_release      # L4T release
cat /usr/local/zed/version     # SDK build — must be the one for that L4T
```

## 2. Prove the SDK opens the camera — with Stereolabs' tool, not ours

```bash
/usr/local/zed/tools/ZED_Diagnostic
```

This checks CUDA + SDK + USB together, downloads the calibration file if it is missing
(**the one moment this box needs internet**), and shows serial and firmware.

**Close it when done.** A ZED accepts one client at a time; a diagnostic left running
makes the service report `held by another process`.

If this fails, stop here and send the hardware team the exact error — nothing in our
code can work until it passes.

## 3. Stop the old service and fetch the new code

```bash
sudo systemctl stop stream-api      # the D435 build would crash-loop on import otherwise
cd ~/stream-api
git fetch
git checkout feature/zed-2i-camera
uv build
```

## 4. Install the wheel and put `pyzed` into the venv

`pyzed` is not in the wheel or in `uv.lock` — it is not on PyPI and needs CUDA — so
this step is **required**; nothing else installs it.

First check whether the existing venv is on the system interpreter:

```bash
/opt/stream-api/.venv/bin/python --version
/usr/bin/python3 --version
```

**Versions match** — keep the venv:

```bash
uv pip install --reinstall ~/stream-api/dist/stream_api-0.1.0-py3-none-any.whl \
  --python /opt/stream-api/.venv/bin/python
/opt/stream-api/.venv/bin/python /usr/local/zed/get_python_api.py
```

**Versions differ** — the `pyzed` wheel is built per Python/CUDA against the system
NumPy, so rebuild the venv on the system interpreter (JETSON_SETUP.md §8, verbatim):

```bash
rm -rf /opt/stream-api/.venv
cd /opt/stream-api
uv venv --python /usr/bin/python3 --system-site-packages
uv pip install ~/stream-api/dist/stream_api-0.1.0-py3-none-any.whl
/opt/stream-api/.venv/bin/python /usr/local/zed/get_python_api.py
```

Either way, verify from the venv:

```bash
/opt/stream-api/.venv/bin/python -c "import pyzed.sl as sl; print(sl.Camera.get_device_list())"
```

Expected: one device with `camera_state: AVAILABLE`. `NOT_AVAILABLE` means something
still holds the camera — `ZED_Diagnostic` from step 2, or the old service still running.

## 5. Smoke test in the foreground before systemd

```bash
cd /opt/stream-api && .venv/bin/stream-api
```

Look for `camera opened: ZED 2i serial=… fw=…` in the output. In a second shell:

```bash
curl -s localhost:8000/health | python3 -m json.tool          # camera.ok true, device filled in
curl -s -o /tmp/raw.jpg localhost:8000/capture                 # scp to the dev machine and LOOK at it
curl -s -D - -o /tmp/enh.jpg "localhost:8000/capture?enhance=true" | grep -i x-enhance
```

Then open `http://<jetson-ip>:8000/stream` in a browser on the dev machine. `Ctrl-C` the
service when satisfied.

## 6. Update the unit and hand back to systemd

Two lines change in `/etc/systemd/system/stream-api.service`:

```ini
Description=ZED Stream API
SupplementaryGroups=bluetooth video
```

`video` covers the `/dev/video*` nodes behind the ZED; the camera's USB device itself is
opened to everyone by the SDK's udev rule. Then:

```bash
sudo usermod -aG video jetson          # once; matches the unit
sudo systemctl daemon-reload
sudo systemctl restart stream-api
sudo systemctl status stream-api
curl -s localhost:8000/health
```

## 7. Recovery drills (do these while you still have the box)

| Drill | Expected |
|---|---|
| Unplug the USB, `curl /capture` | 503 `{"error": "camera not available", "detail": "no ZED device detected on USB …"}` |
| Replug, `curl /capture` again | Recovers within ~5 s, no service restart |
| Start `ZED_Explorer` with the service running, check `/health` | `ZED detected but held by another process …`; close it → recovers on the next request |
| `curl -X POST localhost:8000/camera/restart` | 200 |
| `sudo reboot` | Service is back on its own and `/health` is green |

## 7b. Optional: A/B the image brightness

`STREAM_API_CAMERA_BRIGHTNESS` (0-8, default 4 = the SDK default) is the ZED's ISP
output brightness. Auto-exposure keeps running; the rendered frame just sits lower.
No rebuild needed — run the service by hand with the value you want:

```bash
sudo systemctl stop stream-api
cd /opt/stream-api
STREAM_API_CAMERA_BRIGHTNESS=3 .venv/bin/stream-api
# in another shell
curl -s -o /tmp/b3.jpg localhost:8000/capture
curl -s localhost:8000/health | python3 -m json.tool   # camera.brightness echoes the value
```

Repeat for 4 (baseline) and 2, `scp` the three files out and compare them side by side.
To keep a value, add it to the unit and restart:

```bash
sudo systemctl edit --full stream-api      # add: Environment=STREAM_API_CAMERA_BRIGHTNESS=3
sudo systemctl daemon-reload && sudo systemctl restart stream-api
```

**It will not clear the glare bands.** Measured on the first on-site capture
(2026-09-22): the fish were already well exposed (L median 128, 0.3 % clipped) while
the two vertical bands — the overhead fixtures mirrored off the steel — were 12-17 %
clipped. Those pixels saturate at the sensor, so dimming the ISP output maps them down
without bringing detail back, and takes the fish down with them. The bands are fixed by
moving or diffusing the light fixture, or by changing the angle between lamp, tray and
lens (a polarizer works best at roughly 50-60° off the surface; the camera currently
looks almost straight down). Use this setting for the look of the frame, nothing more.

## 8. What to bring back

- A raw `/capture` and an `?enhance=true` capture under the **real** lights, ideally one
  at night. Look at the bright bands on the tray: still clipping to white, or textured
  now? That is the evidence for whether the polarizer solved the glare on its own.
- If the fish come out dark: do **not** reach for manual exposure — note it. The next
  built-in lever is `AEC_AGC_ROI` (meter only on the tray region).
- Whichever `STREAM_API_CAMERA_BRIGHTNESS` you settle on, and the captures that decided it.
- The `nv_tegra_release` and `/usr/local/zed/version` strings, for JETSON_SETUP.md.

## 9. Tell the client

Bare `/capture` — what their AI pipeline fetches — now returns the **raw** auto-exposed
frame instead of the enhanced one. They should hear that before they see it in production.

---

## If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `ModuleNotFoundError: No module named 'pyzed'` | Step 4 was skipped, or `get_python_api.py` ran under a different python. Re-run it with `/opt/stream-api/.venv/bin/python` |
| `camera open refused … NO_GPU_COMPATIBLE` / CUDA errors | SDK build does not match the L4T release (step 1). Hardware team reinstalls the matching build |
| `camera open refused … CALIBRATION_FILE_NOT_AVAILABLE` | No internet on first open. Run `ZED_Diagnostic` while online, or copy `SN<serial>.conf` into `/usr/local/zed/settings/` |
| `no ZED device detected on USB` | Cable or port — needs a blue USB 3.0 port; `lsusb -t \| grep 5000M`. The ZED is picky about long/thin USB 3 cables |
| `ZED detected but held by another process` | Close `ZED_Explorer` / `ZED_Diagnostic`; make sure the old service is stopped |
| `/health` fine, image looks fisheye / tray only fills the middle | Expected — the ZED's FOV is 110° vs the D435's 69°. Mount height or lens choice, not code |
