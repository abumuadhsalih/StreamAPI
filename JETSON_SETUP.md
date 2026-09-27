# Jetson Setup — From a Freshly Formatted Box to a Running Service

Follow this top to bottom on a Jetson that has just been reformatted. It assumes the box
already boots into Ubuntu, is on the LAN, and you can SSH into it. Everything after that —
camera drivers, USB and Bluetooth permissions, Python, the service, auto-start on boot — is
built here from nothing.

This document is deliberately self-contained. Do not go looking for `DEPLOYMENT.md` or `KT.md`
on the Jetson: they are gitignored and will not be in the clone.

**Time:** ~45 minutes, most of it `apt` downloading.
**You will reboot twice** — once for group membership, once to prove auto-start works.

---

## 0. Before you start

Check these first. Every one of them will waste your time later if it is wrong.

```bash
whoami                              # expect: jetson
sudo -v                             # expect: password prompt, then silence
hostname -I | awk '{print $1}'      # write this down — it is <jetson-ip> below
uname -m                            # expect: aarch64
lsb_release -cs                      # note the codename — step 2 needs it
```

Plug the hardware in now, so the verification steps in each section have something to find:

| Device | How it connects | Watch out for |
|---|---|---|
| Stereolabs ZED 2i | USB — **must be a blue USB 3.0 port** | On USB 2.0 it enumerates but the SDK refuses to open HD1080. Needs JetPack + CUDA; one SDK client at a time |
| DX11 scale indicator | USB via the FTDI FT232 adapter | Indicator must be set to **continuous** output, 9600 8N1 |
| JBD battery pack | Bluetooth LE, MAC `A5:C2:37:6E:94:70` | The pack accepts **one** BLE client. Close the JBD phone app and make sure nobody else is connected |

> The pack stops advertising while something is connected to it. If a phone app holds the
> link, this service will report "device not found" and you will chase a ghost.

---

## 0.5 Check what the hardware team already did

Steps 2, 3 and 4 are often already done when the box comes back. Re-running them is harmless
— apt will say "already the newest version" and `usermod` on an existing group is a no-op —
but it is worth 5 minutes to know, because a **half**-done step is the expensive case: the
service starts, looks healthy, and one device silently never works.

Run these as your normal login user, **not** with `sudo` — half of what you are testing is
what *your* user is allowed to do, and root can do it all regardless.

### Quick answer per step

**Step 2 — is the ZED SDK installed?** Two things must be true: the SDK is on the box, and it
matches the JetPack release.

```bash
cat /etc/nv_tegra_release                          # L4T release — the SDK build must match it
ls /usr/local/cuda                                 # CUDA present (the SDK will not start without it)
cat /usr/local/zed/version 2>/dev/null || ls /usr/local/zed   # SDK installed?
/usr/local/zed/tools/ZED_Diagnostic --help >/dev/null 2>&1 && echo tools-ok
```
`/usr/local/zed/` exists and CUDA is there → **done, skip step 2.** The Python API is installed
into the venv in step 8, so do not look for `pyzed` under the system interpreter.

Supporting detail:
```bash
ls /usr/local/zed/settings/                        # SN<serial>.conf = calibration already fetched
ls /etc/udev/rules.d/99-slabs.rules                # the SDK's udev rule
```

**Step 3 — are USB permissions set?**

```bash
id -nG | tr ' ' '\n' | grep -x plugdev            # expect: plugdev
lsusb | grep -i 2b03                              # Stereolabs vendor id — camera on the bus
ls -l /dev/serial/by-id/                          # scale adapter present?
stat -c '%U %G %a' $(readlink -f /dev/serial/by-id/*)   # who owns the serial port?
```
`plugdev` present **and** the camera shows in `lsusb` → **done, skip step 3.** (The ZED's udev
rule opens it to every user, so the camera itself needs no group.)

> Read the `stat` output carefully. On stock Ubuntu `/dev/ttyUSB*` belongs to group
> **`dialout`**, not `plugdev`. Your login shell can open it because you are in `dialout`, but
> the systemd unit in step 10 runs under `Group=plugdev` and will get permission denied. If
> that is what you see, either add `dialout` to the unit's `SupplementaryGroups=` line or add
> a udev rule setting `GROUP="plugdev"` on the FTDI adapter. This is a classic
> works-by-hand-fails-as-a-service trap, and reformatting is exactly when it reappears.

**Step 4 — is Bluetooth ready?**

```bash
rfkill list bluetooth                             # expect: Soft blocked: no
systemctl is-active bluetooth                     # expect: active
bluetoothctl show | grep Powered                  # expect: Powered: yes
id -nG | tr ' ' '\n' | grep -x bluetooth          # expect: bluetooth
busctl --system call org.bluez /org/bluez org.freedesktop.DBus.Introspectable Introspect >/dev/null && echo "D-Bus OK"
bluetoothctl devices Paired | grep -i A5:C2:37:6E:94:70   # expect: NO output
```
All six behave as noted → **done, skip step 4.**

The `busctl` line is the one that counts. Being in the `bluetooth` group is not the same as
being allowed to talk to BlueZ — if `/etc/dbus-1/system.d/bluetooth.conf` has no policy for
the group, membership passes and the call is still rejected. And the paired-device check must
come back **empty**: a bonded pack makes `bluetoothd` grab the BMS's single BLE slot at boot,
after which this service can never connect.

### Or run all of it at once

`preflight.sh` in this repo performs every check above, plus the box identity and a scan for
the pack, and prints `DONE — skip it` or `NOT done — run it` per step. Since the clone does
not happen until step 6, copy it over from a dev machine first:

```bash
# from the dev machine
scp preflight.sh jetson@<jetson-ip>:~/

# on the Jetson, as the login user — NOT sudo
chmod +x ~/preflight.sh
~/preflight.sh
```

It reads state only: it installs nothing, changes nothing, and is safe to re-run at any point
during or after setup.

---

## 1. Install OS packages

```bash
sudo apt update
sudo apt install -y git curl libssl-dev libusb-1.0-0-dev libgtk-3-dev python3-dev pkg-config
```

`curl` matters — step 2, step 5 and every verification step below use it, and a fresh
Jetson image does not always ship it.

---

## 2. Install the ZED SDK

> **Already done?** `/usr/local/zed/` exists and `ls /usr/local/cuda` works — skip to step 3.
> See §0.5.

The camera is driven by Stereolabs' ZED SDK, which is a CUDA library: it needs JetPack running
natively on the Jetson (no VM without GPU passthrough), and the SDK build must match the
installed L4T release. There is no apt repo and nothing on PyPI — it is a `.run` installer.
(`pyproject.toml` deliberately does not list `pyzed`; the venv gets it in step 8.)

```bash
cat /etc/nv_tegra_release        # note the L4T release, e.g. R36 (release), REVISION: 4.0
ls /usr/local/cuda               # must exist
```

Download the **ZED SDK for JetPack** build matching that release from
<https://www.stereolabs.com/developers/release> (on the Jetson, or `scp` it over), then:

```bash
chmod +x ZED_SDK_Tegra_L4T*.run
./ZED_SDK_Tegra_L4T*.run        # NOT sudo — it asks for the password itself
```

Answer the prompts: accept the licence, install the tools, install the udev rule. When it
offers to install the Python API you can say no — step 8 installs it into the venv, which is
the copy the service actually uses. The SDK lands in `/usr/local/zed/`.

Two things the installer does not tell you:

- **Calibration file.** On the first `open()` of a given camera the SDK downloads its factory
  calibration to `/usr/local/zed/settings/SN<serial>.conf`. That needs internet **once**. If
  this Jetson will live on a LAN with no route out, run `/usr/local/zed/tools/ZED_Diagnostic`
  with the camera plugged in while it is still online, and confirm the file exists.
- **One client at a time.** The ZED behaves like the battery pack: whoever opens it owns it.
  `ZED_Explorer` / `ZED_Diagnostic` left running means the service reports the camera as
  "held by another process" until they are closed.

Verify with the SDK's own tool, then **close it**:

```bash
/usr/local/zed/tools/ZED_Diagnostic      # expect the camera, serial and firmware listed
```

---

## 3. USB permissions

> **Already done?** `id -nG` contains `plugdev` **and** `lsusb | grep -i 2b03` shows the
> camera — then skip to step 4, but read the `dialout` warning in §0.5 first.

The SDK installer drops `/etc/udev/rules.d/99-slabs.rules`, which opens the ZED (and its IMU
HID node) to every user — the camera needs no group. `plugdev` is still needed for the scale's
`/dev/ttyUSB*`, and `video` is added for the `/dev/video*` nodes the SDK reads through.

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev,video jetson
```

**Log out and back in now** (or `sudo reboot`) — group changes do not apply to an existing
shell. Then unplug and replug the camera.

Verify:

```bash
id                              # expect "plugdev" and "video" in the group list
lsusb | grep -i 2b03            # expect the Stereolabs device
lsusb -t | grep 5000M           # it must sit on a USB 3.0 (5000M) bus
ls -l /dev/serial/by-id/        # expect a usb-FTDI_FT232R_... entry for the scale
```

The systemd unit in step 10 runs the service under `Group=plugdev` with `video` as a
supplementary group for exactly this split.

If `lsusb` shows nothing: try another cable (the ZED is picky about long or thin USB 3 cables)
and another blue port.

---

## 4. Bluetooth for the battery BMS

> **Already done?** Radio unblocked, controller powered, `id -nG` contains `bluetooth`, the
> `busctl` call to `org.bluez` succeeds, and the pack is **not** paired — then skip to step 5.
> See §0.5.

```bash
rfkill unblock bluetooth        # the radio ships soft-blocked on some JetPack images
bluetoothctl show               # expect "Powered: yes" and a controller MAC
sudo usermod -aG bluetooth jetson
```

**Log out and back in again** for the `bluetooth` group. Then confirm your user — not root —
can actually talk to BlueZ over D-Bus:

```bash
busctl --system call org.bluez /org/bluez org.freedesktop.DBus.Introspectable Introspect
```

Expect a wall of XML. `Rejected send message` means `/etc/dbus-1/system.d/bluetooth.conf`
has no policy granting the `bluetooth` group access — add one, or BLE will fail later with a
`BleakDBusError` that looks nothing like a permissions problem.

> **Do not pair or trust the pack.** The JBD BMS needs no bonding, and pairing makes
> `bluetoothd` auto-reconnect on boot and hold the pack's single BLE slot — the service will
> then never get in. If it is already paired:
> `bluetoothctl` → `disconnect A5:C2:37:6E:94:70` → `remove A5:C2:37:6E:94:70`.

If `bluetoothctl show` reports no controller at all, the Jetson's radio is not up:
`sudo systemctl status bluetooth` and check `dmesg | grep -i blue`.

---

## 5. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
echo 'source $HOME/.local/bin/env' >> ~/.bashrc      # so it survives a new SSH session
uv --version
```

---

## 6. Clone the repository — on the right branch

> **This is the step most likely to go wrong.** `main` is 25 commits behind and has **no
> battery support, no background scale reader, and no `/system/reboot` or `/system/shutdown`
> endpoints.** A default `git clone` will hand you that older build and everything will look
> fine until a client calls an endpoint that does not exist.

```bash
git clone -b batter_api https://github.com/abumuadhsalih/StreamAPI.git ~/stream-api
cd ~/stream-api
git log --oneline -1              # expect: cefd04d scale file also updated (or newer)
git branch --show-current         # expect: batter_api
```

---

## 7. Build the wheel

`dist/` is gitignored, so no wheel arrives with the clone — build it on the box.

```bash
cd ~/stream-api
uv build
ls dist/stream_api-0.1.0-py3-none-any.whl
```

---

## 8. Create the production venv at `/opt/stream-api`

The service runs from `/opt/stream-api`, separate from the source checkout, so a `git pull`
can never half-break a running service.

```bash
sudo mkdir -p /opt/stream-api
sudo chown jetson:jetson /opt/stream-api
cd /opt/stream-api

uv venv --python /usr/bin/python3 --system-site-packages
uv pip install ~/stream-api/dist/stream_api-0.1.0-py3-none-any.whl
```

Both flags are load-bearing:

- `--python /usr/bin/python3` pins the venv to the **system** interpreter. The `pyzed` wheel
  is built per CUDA/L4T/Python combination against the system NumPy; a uv-downloaded Python
  gets a wheel that either does not exist or imports with "numpy binary incompatibility".
- `--system-site-packages` lets the venv see the system NumPy that wheel was built against
  (and anything else JetPack ships).

**If you see `Building wheel for dbus-fast` during the install, stop.** Prebuilt aarch64
wheels exist only for CPython 3.10–3.13; a build-from-source means you are on the wrong
Python. Delete and redo:

```bash
rm -rf /opt/stream-api/.venv
uv venv --python /usr/bin/python3 --system-site-packages
uv pip install ~/stream-api/dist/stream_api-0.1.0-py3-none-any.whl
```

Now install the ZED Python API **into this venv** — the script installs into whichever
interpreter runs it, and it downloads the wheel that matches the SDK, CUDA and Python found:

```bash
/opt/stream-api/.venv/bin/python /usr/local/zed/get_python_api.py
```

Confirm the venv can see everything:

```bash
/opt/stream-api/.venv/bin/python -c "import pyzed.sl, serial, bleak, cv2; print('OK')"
/opt/stream-api/.venv/bin/python -c "import pyzed.sl as sl; print(sl.Camera.get_device_list())"
```

---

## 9. Smoke test before wiring up systemd

Run it in the foreground first. Debugging is far easier here than through `journalctl`.

```bash
/opt/stream-api/.venv/bin/stream-api
```

In the startup log look for:

- the scale port, e.g. `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_... at 9600 baud`
- roughly 15 seconds later, `battery connected at A5:C2:37:6E:94:70`

Then, from a second SSH session, test each device separately — a failure on one device does
not take the service down, so "the process is running" tells you nothing on its own:

```bash
curl -s localhost:8000/health

curl -s -o /tmp/test.jpg -w '%{http_code} %{size_download} bytes\n' localhost:8000/capture
curl -s localhost:8000/scale/capture
curl -s localhost:8000/battery/capture
```

Expected: `/health` reports each device with its own status, `/capture` returns `200` and a
JPEG of non-trivial size, `/scale/capture` returns a weight, `/battery/capture` returns a
state of charge.

A device that is not working returns **503** with `{"error": ..., "detail": ...}` rather than
crashing the service. That is by design — the box stays reachable so you can diagnose it
remotely. It also means a green `/health` roll-up is not a substitute for testing all three.

Stop it with `Ctrl-C` before continuing.

---

## 10. Install the systemd unit

```bash
sudo tee /etc/systemd/system/stream-api.service > /dev/null <<'EOF'
[Unit]
Description=ZED Stream API
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
Type=simple
User=jetson
Group=plugdev
SupplementaryGroups=bluetooth video
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

Three lines are not obvious:

- `Group=plugdev` — grants `/dev/ttyUSB*` (the scale). `video` in `SupplementaryGroups`
  covers the `/dev/video*` nodes behind the ZED; the SDK's udev rule already opens the
  camera's USB device to everyone.
- `SupplementaryGroups=bluetooth` — required **separately** for BLE. Bluetooth goes through
  D-Bus to `org.bluez`, not through a device node, so `plugdev` grants nothing for it. Omit
  this and camera and scale work perfectly while the battery silently never connects.
- `After=/Wants=bluetooth.target` — otherwise the service can start before BlueZ is up and
  spend its first reconnect cycles failing for no reason.

There is no `workers=` anywhere, and none should ever be added: all three integrations own
exclusive hardware (one ZED camera, one serial handle, one BLE link). A second worker
process would fight the first for the devices.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stream-api
sudo systemctl status stream-api            # expect: active (running)
ss -tlnp | grep 8000                        # expect: LISTEN on 0.0.0.0:8000
curl -s localhost:8000/health
```

And check it from another machine on the LAN — this is what the clients actually do:

```bash
curl -s http://<jetson-ip>:8000/health
```

---

## 11. Grant sudo rights for the `/system/*` endpoints

`/system/reboot`, `/system/shutdown` and `/system/restart-service` shell out to
`sudo systemctl …`. Without this drop-in they return 200 and nothing happens.

```bash
command -v systemctl        # confirm the path — expect /usr/bin/systemctl
sudo visudo -f /etc/sudoers.d/stream-api
```

Put exactly this in the file, on one line:

```
jetson ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl restart stream-api, /usr/bin/systemctl poweroff
```

sudo matches the **whole command line**, so a rule permitting `reboot` does not permit
`poweroff`. Leaving `poweroff` out is exactly the "`/system/shutdown` returns 200 but the box
stays on" symptom.

If `command -v systemctl` printed something other than `/usr/bin/systemctl`, you must change
both the sudoers line above **and** add `Environment=STREAM_API_SYSTEMCTL=<real path>` to the
unit from step 10.

Verify — this should run silently, with no password prompt:

```bash
sudo -n /usr/bin/systemctl --version
```

Then test the endpoint end to end (this genuinely restarts the service):

```bash
curl -s -X POST "http://localhost:8000/system/restart-service?token=jetson-reboot"
sleep 10 && systemctl is-active stream-api      # expect: active
```

---

## 12. Reboot test

Nothing is finished until it survives a power cycle — that is the only state that matters in
the field.

```bash
sudo reboot
```

Wait about 30 seconds, then from another machine:

```bash
curl -s http://<jetson-ip>:8000/health
```

If that returns healthy without you logging in, the setup is complete.

---

## Reference

### Environment variables

Set these as `Environment=` lines in `/etc/systemd/system/stream-api.service` (via
`sudo systemctl edit --full stream-api`), never by editing source.

| Variable | Default | Notes |
|---|---|---|
| `STREAM_API_HOST` | `0.0.0.0` | Set in the unit already |
| `STREAM_API_PORT` | `8000` | Set in the unit already |
| `STREAM_API_SCALE_PORT` | *auto-discovered* | Tries `/dev/serial/by-id/*`, then `/dev/ttyUSB*`, then `/dev/ttyACM*`. Pin it only if a second serial adapter is attached |
| `STREAM_API_SCALE_BAUD` | `9600` | 8-N-1 |
| `STREAM_API_SCALE_UNIT` | `kg` | The DX11 transmits **no unit**; this is the assumed one |
| `STREAM_API_SCALE_STREAM_INTERVAL` | `0.1` | SSE pacing, 10 Hz |
| `STREAM_API_ENHANCE` | `1` | Whether a param-less `/capture` gets CLAHE + shadow lift. `0` turns it off for every client |
| `STREAM_API_CAMERA_CONTRAST` | `4` | ZED ISP contrast, 0-8. Raise for punch in-camera, before de-glare |
| `STREAM_API_DEGLARE_STRENGTH` | `100` | Software de-glare, 0-100. `0` disables it for every client. Tune live with `POST /camera/deglare` |
| `STREAM_API_DEGLARE_SIGMA` | `60` | De-glare radius in full-frame px. Larger = gentler; below ~40 it haloes the fish |
| `STREAM_API_CROP` | `248,118,1830,986` | `/capture` tray crop box in full-frame coords. Empty disables it. Re-measure after any camera move — see ZED_MIGRATION.md |
| `STREAM_API_AEC_ROI` | *(empty = off)* | Auto-exposure metering box. `tray` follows `STREAM_API_CROP`; or `x1,y1,x2,y2`. Off = the camera meters the whole frame. A/B it live with `POST /camera/aec-roi` before setting it here — see ZED_MIGRATION.md §7f |
| `STREAM_API_CAMERA_BRIGHTNESS` | `4` | ZED ISP brightness, 0-8. Lower = darker frame with auto-exposure still running. Does **not** recover blown glare — see ZED_MIGRATION.md |
| `STREAM_API_BATTERY_ADDRESS` | `A5:C2:37:6E:94:70` | Change here when a pack is swapped |
| `STREAM_API_BATTERY_POLL_INTERVAL` | `2.0` | Seconds |
| `STREAM_API_SYSTEMCTL` | `/usr/bin/systemctl` | Only if systemctl lives elsewhere |

### Admin token

Not an environment variable — it is hardcoded as `ADMIN_TOKEN = "jetson-reboot"` in
`src/stream_api/main.py`. It ships in the wheel and lives in git, so it is a guard against
accidents, not a secret.

```bash
curl -X POST "http://<jetson-ip>:8000/system/restart-service?token=jetson-reboot"
curl -X POST "http://<jetson-ip>:8000/system/reboot?token=jetson-reboot"
curl -X POST "http://<jetson-ip>:8000/system/shutdown?token=jetson-reboot&confirm=yes"
```

Shutdown needs `&confirm=yes` on top of the token because it is **one-way** — nothing can
power the box back on over the network. It takes someone physically pressing the button.

### Day-to-day operations

```bash
sudo systemctl status stream-api
sudo systemctl restart stream-api
sudo systemctl stop stream-api            # frees the BLE link for the JBD phone app
sudo journalctl -u stream-api -f          # live log
sudo journalctl -u stream-api -b          # everything since last boot
sudo journalctl -u stream-api -n 200 --no-pager
```

### Updating to a new version

```bash
cd ~/stream-api
git pull
uv build
uv pip install --reinstall ~/stream-api/dist/stream_api-0.1.0-py3-none-any.whl \
  --python /opt/stream-api/.venv/bin/python
sudo systemctl restart stream-api
curl -s localhost:8000/health
```

### Endpoints

`GET` — `/stream` (MJPEG), `/capture` (JPEG, `?preset=sd|hd|fhd`, `?crop=true|false` — default `true`, tray only, `?deglare=true|false` — default `true`, `?enhance=` defaults to the server setting (ships on); exposure/gain/WB are the camera's own auto modes),
`/scale/stream` (SSE), `/scale/capture`, `/battery/capture`, `/health`, `/system/stats`,
`/docs` (Swagger).
`POST` — `/camera/restart`, `/camera/brightness?value=&contrast=`, `/camera/deglare?strength=&sigma=`, `/camera/enhance?value=`, `/camera/aec-roi?value=&box=`, `/scale/reconnect`, `/system/reboot`, `/system/shutdown`,
`/system/restart-service`.

---

## Troubleshooting

Start here whenever something is off — it checks USB, the serial port, kernel messages and
the service in one pass:

```bash
cd ~/stream-api
chmod +x diagnose.sh        # the execute bit does not always survive a clone
./diagnose.sh
```

| Symptom | Cause and fix |
|---|---|
| `No module named 'pyzed'` | `get_python_api.py` was not run with the venv's python, or the venv is on the wrong interpreter. Redo step 8 exactly |
| `Building wheel for dbus-fast` during install | Wrong Python — aarch64 wheels only exist for 3.10–3.13. Redo step 8 with `--python /usr/bin/python3` |
| `/health`: `no ZED device detected on USB` | Reload udev (`sudo udevadm control --reload-rules && sudo udevadm trigger`), replug, try another cable, and confirm a **blue USB 3.0** port: `lsusb -t \| grep 5000M` |
| `/health`: `ZED detected but held by another process` | Close `ZED_Explorer` / `ZED_Diagnostic` (one SDK client at a time). The service reconnects on the next request |
| `/health`: `camera open refused ... CALIBRATION_FILE_NOT_AVAILABLE` | First open needs internet once to fetch `SN<serial>.conf`. Connect, `POST /camera/restart`, or copy the file into `/usr/local/zed/settings/` |
| `/health`: `camera open refused ... NO_GPU_COMPATIBLE` or CUDA errors | SDK build does not match the JetPack release, or CUDA is missing. Compare `cat /etc/nv_tegra_release` with the installer you used |
| Camera stops after an unplug and does not come back | It retries on the next request after a 5 s cooldown; `POST /camera/restart` forces it. If the SDK itself is wedged, `sudo systemctl restart stream-api` |
| No scale readings | `ls -l /dev/serial/by-id/`; prove bytes are flowing with `timeout 5 cat /dev/ttyUSB0`; confirm the indicator is in **continuous** output mode at 9600 8N1. Also check the RS-232 vs TTL cable — a wrong cable gives a live port and silence |
| Scale reads a stale weight | Should not happen — the reader thread keeps only the newest frame and readings older than 3 s return 503. If you see it, the indicator is likely in on-demand mode, not continuous |
| Battery: `device not found` | Something else holds the pack's single BLE slot — usually the JBD phone app. Close it. Also confirm the pack is not paired (step 4) |
| Battery: `Rejected send message` / `BleakDBusError` | `SupplementaryGroups=bluetooth` missing from the unit, or `/etc/dbus-1/system.d/bluetooth.conf` has no rule for the group |
| Battery drops out while streaming video | Known: the Jetson's combo Wi-Fi/BT radio shares an antenna and heavy MJPEG traffic flaps BLE. A USB Bluetooth dongle fixes it. The service serves the cached reading for 60 s and reconnects on its own |
| `/system/shutdown` returns 200, box stays on | The sudoers rule is missing `poweroff`. See step 11 |
| Port 8000 already in use | `ss -tlnp \| grep 8000` to find the holder, or change `STREAM_API_PORT` in the unit |
| Service is up but a client sees 503 | Expected for a failed device. `curl localhost:8000/health` names which one and why |

### Field tools that are not in the repo

`dx11.py`, `dx11_raw.py` and `battery_state.py` — deeper protocol-level diagnostics for the
scale and battery — are not committed, so they will **not** be on a freshly cloned Jetson.
If you need to debug the DX11 wire format itself, copy them over from a dev machine:

```bash
# run from the dev machine, not the Jetson
scp dx11.py dx11_raw.py battery_state.py jetson@<jetson-ip>:~/stream-api/
```

`dx11.py` imports `stream_api.scale`, so run it from a shell where the package is importable,
and stop the service first — it needs exclusive access to the serial port:

```bash
sudo systemctl stop stream-api
cd ~/stream-api && uv run python dx11.py
sudo systemctl start stream-api
```
