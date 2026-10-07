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

ISP output brightness is 0-8, default 4 (the SDK default). Auto-exposure keeps
running; the rendered frame just sits lower. Sweep it **live over the API** — no
restart, no rebuild, service stays up:

```bash
for b in 4 3 2 1; do
  curl -s -X POST "http://<jetson-ip>:8000/camera/brightness?value=$b" > /dev/null
  curl -s -o b$b.jpg "http://<jetson-ip>:8000/capture"
done
```

Run that from the dev machine and the four files land next to you, already named.
`/health` echoes the value in force as `camera.brightness`.

The value survives a self-heal reopen (a USB hiccup will not silently revert it) but
**not** a service restart, which returns to `STREAM_API_CAMERA_BRIGHTNESS`. That is
deliberate: nothing you poked in while testing leaks into production. To keep the value
you settled on, put it in the unit:

```bash
sudo systemctl edit --full stream-api      # add: Environment=STREAM_API_CAMERA_BRIGHTNESS=3
sudo systemctl daemon-reload && sudo systemctl restart stream-api
curl -s http://<jetson-ip>:8000/health | python3 -m json.tool   # confirm camera.brightness
```

**It will not clear the glare bands.** Measured on the first on-site capture
(2026-09-22): the fish were already well exposed (L median 128, 0.3 % clipped) while
the two vertical bands — the overhead fixtures mirrored off the steel — were 12-17 %
clipped. Those pixels saturate at the sensor, so dimming the ISP output maps them down
without bringing detail back, and takes the fish down with them. The bands are fixed by
moving or diffusing the light fixture, or by changing the angle between lamp, tray and
lens (a polarizer works best at roughly 50-60° off the surface; the camera currently
looks almost straight down). Use this setting for the look of the frame, nothing more.

## 7c. Check the tray crop box

`/capture` returns **only the tray** by default. The box is in full-frame coordinates,
measured from a real capture on 2026-09-23, and it is **mount-specific** — if the camera
has been nudged or re-hung since, the crop is wrong and nothing will shout about it.

```bash
curl -s -D - -o cropped.jpg "http://<jetson-ip>:8000/capture" | grep -i x-crop
curl -s -o full.jpg "http://<jetson-ip>:8000/capture?crop=false"
curl -s http://<jetson-ip>:8000/health | python3 -m json.tool   # camera.crop
```

Open both. `cropped.jpg` should be the tray and nothing else — no floor, no baskets, no
feet — with no fish touching an edge. If it is off, re-measure against `full.jpg` and set
the box in the unit (empty value disables cropping entirely):

```bash
sudo systemctl edit --full stream-api   # add: Environment=STREAM_API_CROP=248,118,1830,986
sudo systemctl daemon-reload && sudo systemctl restart stream-api
```

`/stream` is never cropped, so use it in a browser while re-aiming the camera.

The box can be set live now — no unit edit while you are still moving things:

```bash
curl -s -o full.jpg "http://<jetson-ip>:8000/capture?crop=false"          # read corners off this
curl -X POST "http://<jetson-ip>:8000/camera/crop?value=true&box=350,185,1760,925"
curl -X POST "http://<jetson-ip>:8000/camera/crop?value=false"             # full frame again
```

Once it settles, keep it with `Environment=STREAM_API_CROP=`.

## 7d. Tune the de-glare pass

`/capture` flattens the light-strip reflections by default — **on by default because
the mobile app and the AI pipeline send no query params**, so the default is the only
image they can get. Tune it live from the Jetson, no restart:

```bash
# A/B against the raw frame
curl -s -o raw.jpg     "http://<jetson-ip>:8000/capture?deglare=false"
curl -s -o deglared.jpg "http://<jetson-ip>:8000/capture"

# gentler correction, or a wider radius
curl -s -X POST "http://<jetson-ip>:8000/camera/deglare?strength=60"
curl -s -X POST "http://<jetson-ip>:8000/camera/deglare?strength=100&sigma=80"
curl -s -X POST "http://<jetson-ip>:8000/camera/deglare?strength=0"     # off for everyone
```

`strength` 0-100 blends against the original; `sigma` is the illumination radius in
full-frame pixels — larger is gentler, and **below about 40 the blur starts to see the
fish themselves and leaves halos**. The `X-Deglare` header on every capture reports
`strength,sigma`, and `/health` shows `camera.deglare`.

To keep a setting, put it in the unit (values reset on service restart):

```bash
sudo systemctl edit --full stream-api
#   Environment=STREAM_API_DEGLARE_STRENGTH=100
#   Environment=STREAM_API_DEGLARE_SIGMA=60
sudo systemctl daemon-reload && sudo systemctl restart stream-api
```

It cannot restore the small hard specular spots — those pixels are genuinely clipped at
the sensor. Fixing those is still a lighting job: move or diffuse the fixture, or tilt
the camera off straight-down so the polarizer actually works (~50-60° off the surface).

## 7e. The enhance default

De-glare evens out the illumination, and that is also what removes contrast — the
corrected frame is right but flat (measured: L std 30-34, only 65-71% of the tonal
range). `enhance_for_text()` puts the contrast back and is **on by default** for the
same reason as crop and de-glare: the mobile app and the AI pipeline send no query
params, so the default is the only image they get.

```bash
curl -s -D - -o enh.jpg  "http://<jetson-ip>:8000/capture" | grep -i x-enhance
curl -s -o flat.jpg      "http://<jetson-ip>:8000/capture?enhance=false"

curl -X POST "http://<jetson-ip>:8000/camera/enhance?value=false"   # off for everyone
```

`X-Enhance-Gamma` / `X-Enhance-Median` report the lift chosen. A median far from 140
with gamma pinned at 0.6 or 1.0 means the enhancer has run out of range — that is a
signal to look at the exposure or the de-glare strength, not to retune the enhancer.

**The in-camera alternative**, worth trying before leaning on software: raise the ZED's
own contrast, which adds punch in the ISP *before* the de-glare pass.

```bash
curl -X POST "http://<jetson-ip>:8000/camera/brightness?contrast=6"
curl -s -o c6.jpg "http://<jetson-ip>:8000/capture?enhance=false"
```

Both reset on restart; keep one with `Environment=STREAM_API_ENHANCE=` /
`STREAM_API_CAMERA_CONTRAST=` in the unit.

## 7f. Auto-exposure metering on the tray (AEC ROI) — do this LAST

Ships **off**, so nothing above is affected by it. The glare bands outside the tray
drag the auto-exposure down for the whole frame (measured: clean tray L median 86
while the worst band sat at 222). `AEC_AGC_ROI` is the ZED's built-in answer: meter
only on the tray box and the bands stop counting. Not manual exposure — AEC/AGC keeps
running, it just looks at less of the frame.

It is a trade, which is why it is a switch and not a default: brighter fish also means
more of the bands hard-clip, and clipped pixels are the one thing de-glare cannot
recover. Decide it against the same lights, in the same minute:

```bash
curl -s -o roi_off.jpg "http://<jetson-ip>:8000/capture"
curl -X POST "http://<jetson-ip>:8000/camera/aec-roi?value=true"     # meter on the crop box
sleep 2                                                              # let AE settle
curl -s -D - -o roi_on.jpg "http://<jetson-ip>:8000/capture" | grep -i x-aec-roi

# meter on a smaller region (just where the fish sit), full-frame px
curl -X POST "http://<jetson-ip>:8000/camera/aec-roi?value=true&box=400,250,1650,900"

curl -X POST "http://<jetson-ip>:8000/camera/aec-roi?value=false"    # back to full frame
```

Compare `roi_off.jpg` and `roi_on.jpg` on the fish first, then on the bands. Keep it
on only if the fish gained more than the bands lost. `/stream` shifts too — it is the
sensor, not a filter — so the live view is a fair way to watch AE settle.

It survives a self-heal reopen but not a restart; keep it with
`Environment=STREAM_API_AEC_ROI=tray` (follows the crop box, so re-measuring one
re-measures both) or an explicit `x1,y1,x2,y2`. `/health` reports it as `camera.aec_roi`.

## 7g. 2K sensor mode

Same 110-deg field of view, 1.15x the pixels per fish, half the frame rate (15 fps).
Resolution was not the bottleneck in the mobile-vs-ours comparison, so this is a
"prove it" switch, not a fix. Bare `/capture` returns the native size of the mode in
force, so the AI pipeline gets 2208x1242 without the mobile app changing anything.

```bash
curl -X POST "http://<jetson-ip>:8000/camera/resolution?value=2k"     # ~2 s reopen
curl -s -D - -o cap_2k.jpg  "http://<jetson-ip>:8000/capture" | grep -i x-resolution
curl -X POST "http://<jetson-ip>:8000/camera/resolution?value=fhd"
curl -s -D - -o cap_fhd.jpg "http://<jetson-ip>:8000/capture" | grep -i x-resolution
```

Crop and AEC boxes stay in 1080p coordinates and are scaled to the live mode, so
nothing needs re-entering. Check `?crop=false` at both modes shows the same scene
edge to edge — that is the same-FOV assumption the scaling relies on. Keep it with
`Environment=STREAM_API_CAMERA_MODE=2k`; `/health` reports it as `camera.resolution`.

## 8. What to bring back

- A raw `/capture` and an `?enhance=true` capture under the **real** lights, ideally one
  at night. Look at the bright bands on the tray: still clipping to white, or textured
  now? That is the evidence for whether the polarizer solved the glare on its own.
- If the fish come out dark: do **not** reach for manual exposure — try §7f (meter
  auto-exposure on the tray) and bring back the on/off pair of captures.
- Whether `STREAM_API_AEC_ROI` should be `tray` or stay off, and the pair that decided it.
- Whichever `STREAM_API_CAMERA_BRIGHTNESS` you settle on, and the captures that decided it.
- The `STREAM_API_CROP` box if you had to re-measure it, plus a `?crop=false` full frame so the next person can re-derive it.
- The `deglare` strength/sigma you settled on, with a `?deglare=false` frame alongside a default one.
- The `nv_tegra_release` and `/usr/local/zed/version` strings, for JETSON_SETUP.md.

## 9. Tell the client

Bare `/capture` — what their AI pipeline fetches — changed twice:

- it returns the **raw** auto-exposed frame instead of the enhanced one, and
- it is now **cropped to the tray** (1582×868 for the current box) instead of the full
  1920×1080 frame, and
- the light-strip reflections are **flattened** by a de-glare pass, and
- it is **enhanced** again (CLAHE + shadow lift), which had been switched off earlier in
  the ZED migration and is back on because de-glare flattens the frame.

All three are improvements for the model, but they change the image it receives.
`?crop=false`, `?deglare=false` and `?enhance=true` restore the old behaviour piece by
piece if they need to stage it.

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
