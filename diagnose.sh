#!/usr/bin/env bash
#
# diagnose.sh — one-shot camera + scale health check for the Jetson.
#
# Run this ON THE JETSON (over SSH). It reads state only; it does NOT
# reboot, restart the service, or reseat anything. Safe to run anytime.
#
#   chmod +x diagnose.sh
#   ./diagnose.sh
#
# Override the scale port / API base if they differ from the defaults:
#   SCALE_PORT=/dev/ttyUSB0 API=http://localhost:8000 ./diagnose.sh

set -u

SCALE_PORT="${SCALE_PORT:-/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B0000CHM-if00-port0}"
API="${API:-http://localhost:8000}"

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
pass() { echo "  ${GREEN}PASS${NC} $*"; }
fail() { echo "  ${RED}FAIL${NC} $*"; }
warn() { echo "  ${YEL}WARN${NC} $*"; }
hdr()  { echo; echo "${BOLD}== $* ==${NC}"; }

# ---------------------------------------------------------------------------
hdr "1. Camera — RealSense on the USB bus"
if lsusb | grep -qi 'Intel'; then
    pass "Intel RealSense detected on USB:"
    lsusb | grep -i 'Intel' | sed 's/^/       /'
    # RealSense needs USB 3.0 (5000M). Flag if it enumerated at USB 2.0 speed.
    if command -v lsusb >/dev/null && lsusb -t 2>/dev/null | grep -qi '5000M'; then
        pass "A device is linked at USB 3.0 (5000M) speed"
    else
        warn "No 5000M (USB 3.0) link seen — camera may be on a USB 2.0 port; streaming can fail"
    fi
else
    fail "No Intel RealSense on the USB bus — camera is physically disconnected or off the bus"
    echo "       -> reseat the camera into a blue USB 3.0 port, or reboot to re-enumerate"
fi

# ---------------------------------------------------------------------------
hdr "2. Scale — serial adapter present"
if [ -e "$SCALE_PORT" ]; then
    pass "Scale port exists: $SCALE_PORT"
    ls -l "$SCALE_PORT" 2>/dev/null | sed 's/^/       /'
else
    fail "Scale port NOT found: $SCALE_PORT"
    echo "       Available FTDI/by-id links:"
    ls -l /dev/serial/by-id/ 2>/dev/null | sed 's/^/       /' || echo "       (none)"
    echo "       Available ttyUSB devices:"
    ls -l /dev/ttyUSB* 2>/dev/null | sed 's/^/       /' || echo "       (none)"
fi

# ---------------------------------------------------------------------------
hdr "3. Scale — is it actually transmitting?"
if [ -e "$SCALE_PORT" ]; then
    echo "  Listening for 5s (port opens even when the scale is asleep)..."
    DATA="$(timeout 5 cat "$SCALE_PORT" 2>/dev/null | tr -d '\0')"
    if [ -n "${DATA//[[:space:]]/}" ]; then
        pass "Scale IS sending data:"
        echo "$DATA" | head -3 | sed 's/^/       RAW: /'
    else
        fail "Port open but NO bytes in 5s — scale asleep, off, unwired, or wrong port"
        echo "       -> wake/power-cycle the scale, put weight on it, then re-run"
    fi
else
    warn "Skipped — scale port not present (see section 2)"
fi

# ---------------------------------------------------------------------------
hdr "4. Recent USB kernel messages (disconnects / resets)"
if command -v dmesg >/dev/null; then
    LINES="$(dmesg 2>/dev/null | grep -iE 'usb|ftdi|uvc|realsense|reset|disconnect' | tail -15)"
    if [ -n "$LINES" ]; then
        echo "$LINES" | sed 's/^/       /'
    else
        warn "No relevant USB lines (dmesg may need sudo: 'sudo dmesg | tail')"
    fi
else
    warn "dmesg not available"
fi

# ---------------------------------------------------------------------------
hdr "5. Is the stream-api service running?"
if command -v systemctl >/dev/null && systemctl is-active --quiet stream-api 2>/dev/null; then
    pass "stream-api service is active"
elif pgrep -f 'stream[-_]api|uvicorn' >/dev/null; then
    pass "A stream-api / uvicorn process is running"
else
    warn "stream-api does not appear to be running (someone may have stopped it)"
fi

# ---------------------------------------------------------------------------
hdr "6. API /health"
if command -v curl >/dev/null; then
    HEALTH="$(curl -s -m 5 "$API/health" 2>/dev/null)"
    if [ -n "$HEALTH" ]; then
        if command -v python3 >/dev/null; then
            echo "$HEALTH" | python3 -m json.tool 2>/dev/null | sed 's/^/       /' || echo "       $HEALTH"
        else
            echo "       $HEALTH"
        fi
    else
        fail "No response from $API/health — service down or wrong host/port"
    fi
else
    warn "curl not available — skip or 'sudo apt install curl'"
fi

echo
echo "${BOLD}== Done ==${NC}"
echo "If a device shows FAIL on the USB bus (sections 1/2), it is physically"
echo "disconnected — reseat the cable or reboot the Jetson to re-enumerate."
echo "If devices are present but /health is degraded, try the soft recovery:"
echo "  curl -X POST $API/camera/restart"
echo "  curl -X POST $API/scale/reconnect"
