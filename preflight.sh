#!/usr/bin/env bash
#
# preflight.sh — audit what is ALREADY set up on a Jetson before you install.
#
# Run this ON THE JETSON (over SSH) as the service user — NOT with sudo, since
# half of what it checks is what YOUR user is allowed to do. It reads state
# only; it installs nothing and changes nothing. Safe to run anytime.
#
#   chmod +x preflight.sh
#   ./preflight.sh
#
# It covers JETSON_SETUP.md steps 2 (ZED SDK), 3 (USB permissions) and
# 4 (Bluetooth) — the three the hardware team may already have done. Each
# section ends with a verdict telling you whether to skip or run that step.
#
# Override the battery MAC if a different pack is fitted:
#   BATTERY_MAC=AA:BB:CC:DD:EE:FF ./preflight.sh

set -u

BATTERY_MAC="${BATTERY_MAC:-A5:C2:37:6E:94:70}"
SVC_USER="${SVC_USER:-$(id -un)}"

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
pass()    { echo "  ${GREEN}PASS${NC} $*"; }
fail()    { echo "  ${RED}FAIL${NC} $*"; }
warn()    { echo "  ${YEL}WARN${NC} $*"; }
info()    { echo "       $*"; }
hdr()     { echo; echo "${BOLD}== $* ==${NC}"; }
verdict() { echo "  ${BOLD}-> $*${NC}"; }

if [ "$(id -u)" -eq 0 ]; then
    warn "Running as root. Group and D-Bus checks below will pass for root but"
    info "say nothing about the service user. Re-run as the normal login user."
fi

# ---------------------------------------------------------------------------
hdr "0. Box identity"
info "user     : $(id -un)"
info "hostname : $(hostname)"
info "IP       : $(hostname -I 2>/dev/null | awk '{print $1}')"
info "arch     : $(uname -m)"
info "kernel   : $(uname -r)"
if command -v lsb_release >/dev/null; then
    info "ubuntu   : $(lsb_release -ds) (codename: $(lsb_release -cs))"
fi
if [ -f /etc/nv_tegra_release ]; then
    info "jetpack  : $(head -1 /etc/nv_tegra_release)"
fi
info "python3  : $(/usr/bin/python3 --version 2>&1) at /usr/bin/python3"

# ---------------------------------------------------------------------------
hdr "STEP 2 — ZED SDK (CUDA + /usr/local/zed)"

STEP2_OK=1

# 2a. CUDA. The ZED SDK is a CUDA library — it will not open the camera
#     without a GPU, even for RGB-only capture. A plain Ubuntu VM fails here.
if [ -d /usr/local/cuda ]; then
    pass "CUDA present at /usr/local/cuda"
    [ -f /usr/local/cuda/version.json ] \
        && info "cuda    : $(grep -o '"version" *: *"[^"]*"' /usr/local/cuda/version.json | head -1 | cut -d'"' -f4)"
else
    fail "No /usr/local/cuda — the ZED SDK cannot run here (needs native JetPack)"
    STEP2_OK=0
fi

# 2b. The SDK itself. The build must match the L4T release printed above —
#     a mismatch shows up later as NO_GPU_COMPATIBLE / CUDA errors on open().
if [ -d /usr/local/zed ]; then
    pass "ZED SDK installed at /usr/local/zed"
    [ -f /usr/local/zed/version ] && info "sdk     : $(head -1 /usr/local/zed/version)"
    [ -x /usr/local/zed/tools/ZED_Diagnostic ] \
        && pass "ZED_Diagnostic tool present" \
        || warn "ZED_Diagnostic missing — SDK installed without tools; re-run the installer if you want it"
    [ -f /usr/local/zed/get_python_api.py ] \
        && pass "get_python_api.py present (installs pyzed into the venv in step 8)" \
        || warn "get_python_api.py missing — the venv cannot get pyzed without it"
else
    fail "ZED SDK NOT installed (no /usr/local/zed)"
    STEP2_OK=0
fi

# 2c. Calibration file. The SDK fetches SN<serial>.conf from Stereolabs on the
#     first open() of a camera — the one moment this box needs internet. On a
#     LAN-only Jetson, run ZED_Diagnostic while online so it lands beforehand.
CAL="$(ls /usr/local/zed/settings/SN*.conf 2>/dev/null)"
if [ -n "$CAL" ]; then
    pass "Calibration file(s) already fetched:"
    echo "$CAL" | sed 's/^/       /'
else
    warn "No /usr/local/zed/settings/SN*.conf yet — the first camera open needs"
    info "internet to download it (CALIBRATION_FILE_NOT_AVAILABLE otherwise)"
fi

# 2d. pyzed is NOT expected under the system python — it goes into the venv
#     (step 8). Report it either way so nobody hunts for a missing apt package.
if /usr/bin/python3 -c "import pyzed.sl" 2>/dev/null; then
    info "pyzed is importable from /usr/bin/python3 (installer put it there; harmless)"
else
    info "pyzed not under /usr/bin/python3 — expected; step 8 installs it into the venv"
fi

if [ "$STEP2_OK" -eq 1 ]; then
    verdict "STEP 2 is DONE — skip it."
else
    verdict "STEP 2 is NOT done — run it."
fi

# ---------------------------------------------------------------------------
hdr "STEP 3 — USB permissions (camera + scale)"

STEP3_OK=1
GROUPS_NOW="$(id -nG)"

# 3a. plugdev membership — the systemd unit runs under Group=plugdev (scale).
if echo " $GROUPS_NOW " | grep -q ' plugdev '; then
    pass "$SVC_USER is in 'plugdev'"
else
    fail "$SVC_USER is NOT in 'plugdev'"
    info "-> sudo usermod -aG plugdev $SVC_USER   (then log out and back in)"
    STEP3_OK=0
fi
# video — the unit lists it in SupplementaryGroups for the /dev/video* nodes.
if echo " $GROUPS_NOW " | grep -q ' video '; then
    pass "$SVC_USER is in 'video'"
else
    warn "$SVC_USER is NOT in 'video' — add it: sudo usermod -aG video $SVC_USER"
fi
info "all groups: $GROUPS_NOW"

# 3b. udev rule from the ZED SDK installer. It opens the camera (and its IMU
#     HID node) to all users, which is why the camera needs no group of its own.
if [ -f /etc/udev/rules.d/99-slabs.rules ]; then
    pass "ZED udev rule installed: /etc/udev/rules.d/99-slabs.rules"
else
    warn "No /etc/udev/rules.d/99-slabs.rules — the camera may only be reachable as root."
    info "Re-run the SDK installer and accept the udev step, or copy the rule from"
    info "/usr/local/zed/ (then: sudo udevadm control --reload-rules && sudo udevadm trigger)"
fi

# 3c. Is the camera on the bus? Stereolabs' USB vendor id is 2b03. Opening it
#     is left to ZED_Diagnostic / the service — the SDK holds the camera while
#     open and a probe here would collide with a running service.
if lsusb 2>/dev/null | grep -qiE '2b03|stereolabs'; then
    pass "ZED is on the USB bus:"
    lsusb | grep -iE '2b03|stereolabs' | sed 's/^/       /'
else
    warn "No ZED on the USB bus at all — plug it into a blue USB 3.0 port"
    info "(cannot judge permissions until the device is present)"
fi

# 3d. USB 3.0 link speed. Not a permission, but it is checked here because the
#     symptom (open() refuses HD1080) reads like a driver bug.
if lsusb -t 2>/dev/null | grep -qi '5000M'; then
    pass "A device is linked at USB 3.0 (5000M)"
else
    warn "No 5000M link — if the camera is plugged in, it is on a USB 2.0 port"
fi

# 3e. Serial port for the scale. NOTE: on stock Ubuntu /dev/ttyUSB* is owned by
#     group 'dialout', NOT 'plugdev'. If that is the case here, Group=plugdev
#     alone will NOT open the scale and the unit needs dialout added.
SERIAL_DEV=""
for link in /dev/serial/by-id/*; do
    [ -e "$link" ] && { SERIAL_DEV="$(readlink -f "$link")"; break; }
done
[ -z "$SERIAL_DEV" ] && for d in /dev/ttyUSB* /dev/ttyACM*; do
    [ -e "$d" ] && { SERIAL_DEV="$d"; break; }
done

if [ -n "$SERIAL_DEV" ]; then
    DEV_GROUP="$(stat -c '%G' "$SERIAL_DEV" 2>/dev/null)"
    DEV_MODE="$(stat -c '%a' "$SERIAL_DEV" 2>/dev/null)"
    pass "Serial device present: $SERIAL_DEV (group=$DEV_GROUP mode=$DEV_MODE)"
    ls -l /dev/serial/by-id/ 2>/dev/null | sed 's/^/       /'

    if [ -r "$SERIAL_DEV" ] && [ -w "$SERIAL_DEV" ]; then
        pass "$SVC_USER can read and write it"
    else
        fail "$SVC_USER canNOT read/write $SERIAL_DEV"
        STEP3_OK=0
    fi

    # The check that actually catches the silent post-format failure:
    if [ "$DEV_GROUP" != "plugdev" ]; then
        warn "Device group is '$DEV_GROUP', not 'plugdev'."
        info "The systemd unit runs with Group=plugdev, so it will NOT be able to"
        info "open this port even though your login shell can (you are in $DEV_GROUP)."
        info "-> add to the unit:  SupplementaryGroups=bluetooth $DEV_GROUP"
        info "   or add a udev rule setting GROUP=\"plugdev\" on the FTDI adapter."
    fi
else
    warn "No serial device found — scale unplugged, or the FTDI adapter is dead"
    info "check: lsusb | grep -i 0403:6001"
fi

if [ "$STEP3_OK" -eq 1 ]; then
    verdict "STEP 3 is DONE — skip it (but read any WARN above)."
else
    verdict "STEP 3 is NOT done — run it."
fi

# ---------------------------------------------------------------------------
hdr "STEP 4 — Bluetooth for the battery BMS"

STEP4_OK=1

# 4a. Radio not soft/hard blocked.
if command -v rfkill >/dev/null; then
    RFK="$(rfkill list bluetooth 2>/dev/null)"
    if [ -z "$RFK" ]; then
        warn "rfkill reports no bluetooth device — no adapter, or driver not loaded"
    elif echo "$RFK" | grep -q 'yes'; then
        fail "Bluetooth is BLOCKED:"
        echo "$RFK" | sed 's/^/       /'
        info "-> rfkill unblock bluetooth"
        STEP4_OK=0
    else
        pass "Bluetooth radio is unblocked"
    fi
else
    warn "rfkill not installed — cannot check block state"
fi

# 4b. bluetoothd running.
if systemctl is-active --quiet bluetooth 2>/dev/null; then
    pass "bluetooth.service is active"
else
    fail "bluetooth.service is NOT active"
    info "-> sudo systemctl enable --now bluetooth"
    STEP4_OK=0
fi
command -v bluetoothctl >/dev/null && info "bluez: $(bluetoothctl --version 2>/dev/null)"

# 4c. A powered controller must exist.
if command -v bluetoothctl >/dev/null; then
    BTSHOW="$(timeout 10 bluetoothctl show 2>/dev/null)"
    if echo "$BTSHOW" | grep -q 'Powered: yes'; then
        pass "Bluetooth controller is powered:"
        echo "$BTSHOW" | grep -E 'Controller|Name:|Powered:|Discoverable:' | sed 's/^/       /'
    elif [ -n "$BTSHOW" ]; then
        fail "Controller present but NOT powered"
        info "-> bluetoothctl power on"
        STEP4_OK=0
    else
        fail "No Bluetooth controller found"
        STEP4_OK=0
    fi
fi

# 4d. bluetooth group — needed by the unit's SupplementaryGroups=bluetooth.
if echo " $GROUPS_NOW " | grep -q ' bluetooth '; then
    pass "$SVC_USER is in 'bluetooth'"
else
    fail "$SVC_USER is NOT in 'bluetooth'"
    info "-> sudo usermod -aG bluetooth $SVC_USER   (then log out and back in)"
    STEP4_OK=0
fi

# 4e. The check that matters: D-Bus access to org.bluez as THIS user.
#     Group membership without a matching dbus policy still gets rejected.
if command -v busctl >/dev/null; then
    if timeout 10 busctl --system call org.bluez /org/bluez \
         org.freedesktop.DBus.Introspectable Introspect >/dev/null 2>&1; then
        pass "$SVC_USER can talk to org.bluez over D-Bus"
    else
        fail "D-Bus call to org.bluez was rejected for $SVC_USER"
        info "-> /etc/dbus-1/system.d/bluetooth.conf needs a policy for the"
        info "   'bluetooth' group, or the group change has not taken effect yet"
        info "   (log out and back in)."
        STEP4_OK=0
    fi
fi

# 4f. The pack must NOT be paired — bonding makes bluetoothd hold the single
#     BLE slot on boot and the service can then never connect.
if command -v bluetoothctl >/dev/null; then
    PAIRED="$(timeout 10 bluetoothctl devices Paired 2>/dev/null)"
    if echo "$PAIRED" | grep -qi "$BATTERY_MAC"; then
        fail "Battery pack $BATTERY_MAC is PAIRED — remove it"
        info "-> bluetoothctl disconnect $BATTERY_MAC && bluetoothctl remove $BATTERY_MAC"
        STEP4_OK=0
    else
        pass "Battery pack is not paired (correct — JBD needs no bonding)"
    fi
    [ -n "$PAIRED" ] && { info "other paired devices:"; echo "$PAIRED" | sed 's/^/       /'; }
fi

# 4g. Is the pack advertising? It stops while any client holds the link, so a
#     miss here usually means someone's phone app is connected, not a fault.
if command -v bluetoothctl >/dev/null; then
    echo "  Scanning 12s for $BATTERY_MAC ..."
    SCAN="$(timeout 12 bluetoothctl --timeout 10 scan le 2>/dev/null)"
    if echo "$SCAN" | grep -qi "$BATTERY_MAC"; then
        pass "Battery pack is advertising and reachable"
    else
        warn "Pack not seen while scanning. Either it is powered off, out of"
        info "range, or something already holds its single BLE slot — close the"
        info "JBD phone app and stop stream-api, then re-run."
    fi
fi

if [ "$STEP4_OK" -eq 1 ]; then
    verdict "STEP 4 is DONE — skip it."
else
    verdict "STEP 4 is NOT done — run it."
fi

# ---------------------------------------------------------------------------
hdr "Bonus — is anything already installed?"
command -v uv >/dev/null \
    && pass "uv present: $(uv --version 2>/dev/null)" \
    || info "uv not installed (step 5)"
[ -d /opt/stream-api/.venv ] \
    && pass "/opt/stream-api/.venv already exists (step 8 was done before)" \
    || info "no /opt/stream-api venv (step 8)"
[ -f /etc/systemd/system/stream-api.service ] \
    && pass "systemd unit already installed: $(systemctl is-enabled stream-api 2>/dev/null), $(systemctl is-active stream-api 2>/dev/null)" \
    || info "no systemd unit (step 10)"
[ -f /etc/sudoers.d/stream-api ] \
    && pass "sudoers drop-in present" \
    || info "no sudoers drop-in (step 11)"

echo
echo "${BOLD}== Done ==${NC}"
echo "Every FAIL above means that step still has to be run. WARN means it will"
echo "probably work but read the note — most of these are the failures that do"
echo "not look like what they are."
