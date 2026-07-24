import os
import sys
import json
import time
import glob
import hmac
import asyncio
import logging
import threading
import subprocess
from typing import Literal, Optional
import cv2
import numpy as np
import serial
import psutil
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, JSONResponse

from .scale import parse_scale_line, resolve_scale_port, split_frames

logger = logging.getLogger("stream_api")

app = FastAPI(title="RealSense Stream API")

# Open CORS for POC — LAN browser/mobile clients on arbitrary origins.
# Lock down `allow_origins` to specific domains before any production use.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Admin token — the one guard on destructive actions (reboot / restart).
# The rest of the API is unauthenticated (LAN-only POC). Hardcoded on purpose
# for the POC; note it ships in the wheel and lives in git, so it is NOT a real
# secret — change this value per-deployment and treat the API as LAN-only.
# ---------------------------------------------------------------------------

ADMIN_TOKEN = "jetson-reboot"


def require_admin_token(token: str = "") -> None:
    """FastAPI dependency guarding destructive endpoints via `?token=`."""
    if not ADMIN_TOKEN:
        raise _AdminError(503, "admin token not configured")
    if not hmac.compare_digest(token, ADMIN_TOKEN):
        raise _AdminError(403, "invalid or missing admin token")


def require_shutdown_confirm(confirm: str = "") -> None:
    """Second guard on shutdown only. Unlike reboot, a powered-off Jetson cannot
    be brought back over the network — someone has to press the button."""
    if confirm != "yes":
        raise _AdminError(
            400,
            "shutdown requires confirm=yes",
            "The device cannot be powered on remotely. Physical access is "
            "required to bring it back. Re-send with &confirm=yes if you are sure.",
        )


class _AdminError(Exception):
    def __init__(self, status_code: int, message: str, detail: str = "") -> None:
        self.status_code = status_code
        self.message = message
        self.detail = detail


@app.exception_handler(_AdminError)
def _admin_error_handler(request, exc: _AdminError):
    body = {"error": exc.message}
    if exc.detail:
        body["detail"] = exc.detail
    return JSONResponse(content=body, status_code=exc.status_code)

# ---------------------------------------------------------------------------
# Resolution presets
# ---------------------------------------------------------------------------

# Capture runs at the largest preset; smaller presets are downscaled per-request
# so multiple clients can request different resolutions concurrently.
RESOLUTIONS: dict[str, tuple[int, int]] = {
    "sd": (640, 480),
    "hd": (1280, 720),
    "fhd": (1920, 1080),
}
CAPTURE_WIDTH, CAPTURE_HEIGHT = RESOLUTIONS["fhd"]
Preset = Literal["sd", "hd", "fhd"]


# ---------------------------------------------------------------------------
# Hardware status — surfaced via /health and used to short-circuit endpoints
# ---------------------------------------------------------------------------

camera_status: dict = {"ok": False, "error": "not yet started", "mode": "real"}
# `port` is the device actually opened (discovery can pick it, so the configured
# value is not always the answer) and `format` is the parser layer the last
# frame matched — both are diagnostics for /health, neither drives the 503.
scale_status: dict = {
    "ok": False,
    "error": "not yet opened",
    "mode": "real",
    "port": None,
    "format": None,
}
# `ok` means "a fresh-enough reading is available to serve" and drives the 503;
# `connected` is the live BLE link state and is informational. They differ on
# purpose — a brief link drop keeps serving the cached reading (see
# _BATTERY_STALE_AFTER). Keys are fixed at definition so only values ever
# change, which is what makes reading these dicts lock-free under the GIL.
battery_status: dict = {
    "ok": False,
    "error": "not yet connected",
    "mode": "real",
    "connected": False,
}


def _camera_failed(error: str) -> None:
    if camera_status["ok"] or camera_status.get("error") != error:
        logger.warning("camera failure: %s", error)
    camera_status["ok"] = False
    camera_status["error"] = error


def _scale_failed(error: str) -> None:
    if scale_status["ok"] or scale_status.get("error") != error:
        logger.warning("scale failure: %s", error)
    scale_status["ok"] = False
    scale_status["error"] = error


def _battery_failed(error: str) -> None:
    if battery_status["ok"] or battery_status.get("error") != error:
        logger.warning("battery failure: %s", error)
    battery_status["ok"] = False
    battery_status["error"] = error


# ---------------------------------------------------------------------------
# Camera — pyrealsense2 is Linux-only; stub on other platforms for local dev
# ---------------------------------------------------------------------------

if sys.platform == "linux":
    import pyrealsense2 as rs

    # Camera exposure tuning — edit these and redeploy to change the baseline.
    # For on-site iteration without a redeploy, use ?exposure_us=<us> on /capture
    # (persists across requests until changed again; ?exposure_us=0 re-enables AE).
    # Typical manual values under bright glare: 1000-8000 microseconds.
    # On-site tuned values for the Dubai smart-table (bright overhead lights +
    # polished steel tray). Verified on the Jetson to give a clean, readable
    # capture without highlight blow-out or colour cast. Override per-request
    # via ?exposure_us= / ?gain= / ?white_balance= on /capture if the scene
    # changes; set any to 0 to hand that channel back to auto.
    CAMERA_EXPOSURE_US = 80     # 0 = auto-exposure; >0 = manual microseconds
    CAMERA_GAIN = 60            # 0 = don't override; D435 range 16-248
    CAMERA_WHITE_BALANCE = 3600 # 0 = auto-WB; >0 = locked Kelvin (2800-6500)

    pipeline = rs.pipeline()
    config = rs.config()
    config.enable_stream(
        rs.stream.color, CAPTURE_WIDTH, CAPTURE_HEIGHT, rs.format.bgr8, 30
    )

    # Serialize all pipeline access — pyrealsense2's pipeline is not thread-safe,
    # and lazy restart from multiple concurrent requests must be single-flighted.
    _camera_lock = threading.Lock()
    _camera_last_retry = 0.0
    _CAMERA_RETRY_COOLDOWN = 5.0  # seconds — avoid hammering USB after a disconnect

    def _apply_exposure(color_sensor, exposure_us: int, gain: int) -> None:
        """Switch to manual exposure if exposure_us > 0; otherwise leave AE on."""
        if exposure_us <= 0:
            color_sensor.set_option(rs.option.enable_auto_exposure, 1)
            return
        color_sensor.set_option(rs.option.enable_auto_exposure, 0)
        color_sensor.set_option(rs.option.exposure, exposure_us)
        if gain > 0:
            color_sensor.set_option(rs.option.gain, gain)

    def _apply_white_balance(color_sensor, kelvin: int) -> None:
        """Lock WB to a manual Kelvin value if kelvin > 0; otherwise re-enable auto-WB."""
        if kelvin <= 0:
            color_sensor.set_option(rs.option.enable_auto_white_balance, 1)
            return
        color_sensor.set_option(rs.option.enable_auto_white_balance, 0)
        color_sensor.set_option(rs.option.white_balance, kelvin)

    def _tune_color_sensor() -> None:
        color_sensor = pipeline.get_active_profile().get_device().first_color_sensor()
        color_sensor.set_option(rs.option.sharpness, 100)
        color_sensor.set_option(rs.option.contrast, 60)
        color_sensor.set_option(rs.option.gamma, 300)
        color_sensor.set_option(rs.option.backlight_compensation, 1)
        color_sensor.set_option(rs.option.power_line_frequency, 1)
        color_sensor.set_option(rs.option.auto_exposure_priority, 0)
        _apply_exposure(color_sensor, CAMERA_EXPOSURE_US, CAMERA_GAIN)
        _apply_white_balance(color_sensor, CAMERA_WHITE_BALANCE)

    def _start_pipeline() -> bool:
        """(Re)start the RealSense pipeline. Caller must hold _camera_lock."""
        global _camera_last_retry
        _camera_last_retry = time.monotonic()
        try:
            pipeline.stop()
        except Exception:
            pass
        # Enumerate first so we can distinguish "no camera on USB" (hardware
        # unplugged / bad cable / wrong port) from "camera present but pipeline
        # refused" (another process holds it, firmware wedged, USB 2.0 port).
        try:
            devices = rs.context().query_devices()
        except Exception as e:
            _camera_failed(f"failed to enumerate USB devices: {e}")
            return False
        if len(devices) == 0:
            _camera_failed(
                "no RealSense device detected on USB — check the cable, "
                "try a different USB 3.0 port, or unplug/replug the camera"
            )
            return False
        camera_status["device"] = {
            "name": devices[0].get_info(rs.camera_info.name),
            "serial": devices[0].get_info(rs.camera_info.serial_number),
            "firmware": devices[0].get_info(rs.camera_info.firmware_version),
        }
        try:
            pipeline.start(config)
            _tune_color_sensor()
            camera_status["ok"] = True
            camera_status["error"] = None
            logger.info(
                "camera pipeline started: %s serial=%s fw=%s",
                camera_status["device"]["name"],
                camera_status["device"]["serial"],
                camera_status["device"]["firmware"],
            )
            return True
        except Exception as e:
            _camera_failed(
                f"pipeline start refused by device (camera detected but "
                f"stream config was rejected — likely another process holds "
                f"it, or the camera is on a USB 2.0 port): {e}"
            )
            return False

    def _grab_frame():
        with _camera_lock:
            if not camera_status["ok"]:
                if time.monotonic() - _camera_last_retry < _CAMERA_RETRY_COOLDOWN:
                    return None
                if not _start_pipeline():
                    return None
            # Two attempts per request — masks transient USB hiccups (idle
            # autosuspend, brief re-enumeration, single stalled frame). On the
            # first timeout we restart the pipeline in-line and try once more
            # so the client sees a valid frame instead of a 503.
            for attempt in (1, 2):
                try:
                    frames = pipeline.wait_for_frames(timeout_ms=3000)
                    frame = frames.get_color_frame()
                    if frame:
                        return np.asanyarray(frame.get_data())
                except Exception as e:
                    logger.info("frame grab attempt %d failed: %s", attempt, e)
                    try:
                        pipeline.stop()
                    except Exception:
                        pass
                    if attempt == 2:
                        _camera_failed(str(e))
                        return None
                    if not _start_pipeline():
                        return None
            return None

    def restart_camera() -> dict:
        """Force a pipeline stop+restart on demand, bypassing the cooldown."""
        global _camera_last_retry
        with _camera_lock:
            _camera_last_retry = 0.0
            _start_pipeline()
        return camera_status

    @app.on_event("startup")
    def _camera_startup():
        with _camera_lock:
            _start_pipeline()

    @app.on_event("shutdown")
    def _camera_shutdown():
        try:
            pipeline.stop()
        except Exception:
            pass

else:
    camera_status.update({"ok": True, "error": None, "mode": "stub"})

    def restart_camera() -> dict:
        """No-op restart for local dev — returns the stub status."""
        return camera_status

    def _grab_frame():
        """Return a placeholder frame when no RealSense is available."""
        frame = np.zeros((CAPTURE_HEIGHT, CAPTURE_WIDTH, 3), dtype=np.uint8)
        cv2.putText(
            frame,
            "RealSense not available (stub)",
            (60, CAPTURE_HEIGHT // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.5,
            (255, 255, 255),
            3,
        )
        return frame


def get_color_frame(width: int, height: int):
    """Grab a frame and resize if smaller than capture resolution."""
    image = _grab_frame()
    if image is None:
        return None
    if (width, height) != (CAPTURE_WIDTH, CAPTURE_HEIGHT):
        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    return image


# ---------------------------------------------------------------------------
# Scale — serial port on Linux; stub on other platforms for local dev
# ---------------------------------------------------------------------------

# Unset by default: resolve_scale_port() discovers the adapter, preferring the
# kernel's stable /dev/serial/by-id/ symlink over ttyUSBN (whose number shifts
# after USB re-enumeration). Set STREAM_API_SCALE_PORT to pin one device when
# more than one serial adapter is attached, e.g.
#   /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B0000CHM-if00-port0
SCALE_PORT = os.environ.get("STREAM_API_SCALE_PORT")
SCALE_BAUD = int(os.environ.get("STREAM_API_SCALE_BAUD", "9600"))
# SSE pacing. read_scale() is a cache lookup, so the generator needs its own
# throttle — see generate_scale_sse().
SCALE_STREAM_INTERVAL = float(
    os.environ.get("STREAM_API_SCALE_STREAM_INTERVAL", "0.1")
)
# Emit an unchanged reading this often anyway, so a client can tell "the pan is
# steady" from "the stream is wedged".
SCALE_STREAM_HEARTBEAT = 1.0

if sys.platform == "linux":
    # A background thread owns the serial handle and caches the newest decoded
    # frame; endpoints only read that cache. This is the same shape as the
    # battery block below, for the same reason, and it is not merely an
    # optimisation:
    #
    # The DX11 indicator streams continuously. A readline() per request returns
    # the *oldest unread* line sitting in the kernel's buffer, not the current
    # weight — so a /scale/capture after a quiet minute answers with a
    # minute-old weight that looks completely valid. Draining continuously and
    # keeping only the newest frame is the only way the endpoint can be
    # truthful about "now".
    _scale_lock = threading.Lock()      # guards the cache, never the handle
    _scale_last: Optional[dict] = None
    _scale_last_mono: Optional[float] = None
    # Set by /scale/reconnect; the reader picks it up and re-opens the port.
    # The request thread must never close the handle out from under a read.
    _scale_reopen = threading.Event()

    _SCALE_READ_TIMEOUT = 0.2       # also the reader's tick for staleness checks
    _SCALE_RETRY_COOLDOWN = 2.0
    # Short on purpose: with a continuous stream, a reading a few seconds old
    # already means the wire went quiet. Contrast the battery's 60 s — state of
    # charge moves over minutes, a weight moves the instant someone touches it.
    _SCALE_STALE_AFTER = 3.0
    _SCALE_RECONNECT_WAIT = 3.0

    # Constant failure strings: _scale_failed() dedups its logging on message
    # equality, so text carrying varying detail (timings, byte counts) would
    # defeat the dedup and fill the journal while the indicator is merely off.
    _SCALE_ERR_NO_PORT = (
        "no USB serial adapter found — check the cable and that the indicator "
        "is powered, or pin the device with STREAM_API_SCALE_PORT"
    )
    _SCALE_ERR_SILENT = "the serial port is open but the indicator is sending nothing"
    _SCALE_ERR_STALE = "no fresh reading from the indicator"

    def _scale_age() -> Optional[float]:
        """Seconds since the last decoded frame, or None if there has never
        been one. Monotonic — a Jetson with no RTC steps its wall clock by
        years when NTP first syncs."""
        with _scale_lock:
            if _scale_last_mono is None:
                return None
            return time.monotonic() - _scale_last_mono

    def _scale_store(reading: dict) -> None:
        global _scale_last, _scale_last_mono
        with _scale_lock:
            _scale_last = reading
            _scale_last_mono = time.monotonic()
        if not scale_status["ok"]:
            logger.info("scale reading recovered")
        scale_status["ok"] = True
        scale_status["error"] = None
        scale_status["format"] = reading["format"]

    def _scale_silent(reason: str) -> None:
        """The port is open but nothing is arriving. Keep serving the last good
        reading until it goes stale — a single dropped frame is not worth
        blanking an otherwise-good weight over."""
        age = _scale_age()
        if age is None or age > _SCALE_STALE_AFTER:
            _scale_failed(reason)

    def _open_scale() -> Optional[serial.Serial]:
        """Open the resolved port, or None (having recorded why)."""
        port = resolve_scale_port(SCALE_PORT)
        if port is None:
            scale_status["port"] = None
            _scale_failed(_SCALE_ERR_NO_PORT)
            return None
        try:
            handle = serial.Serial(
                port=port,
                baudrate=SCALE_BAUD,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=_SCALE_READ_TIMEOUT,
            )
        except Exception as e:
            scale_status["port"] = port
            _scale_failed(f"failed to open {port}: {e}")
            return None
        # Anything buffered from before we opened is by definition stale.
        handle.reset_input_buffer()
        scale_status["port"] = port
        logger.info("scale opened on %s at %d baud", port, SCALE_BAUD)
        return handle

    def _scale_reader() -> None:
        """Own the serial port, drain it forever, cache the newest frame."""
        buffer = bytearray()
        while True:
            _scale_reopen.clear()
            handle = None
            try:
                handle = _open_scale()
                if handle is not None:
                    buffer.clear()
                    while not _scale_reopen.is_set():
                        # read(1) blocks in the kernel until a byte lands or the
                        # timeout expires, then in_waiting drains the rest of
                        # the burst in one call. The shared script polls
                        # in_waiting with a 1 ms sleep instead, which costs
                        # ~1000 wakeups a second to do the same job.
                        chunk = handle.read(1)
                        if handle.in_waiting:
                            chunk += handle.read(handle.in_waiting)
                        if not chunk:
                            _scale_silent(_SCALE_ERR_SILENT)
                            continue
                        buffer.extend(chunk)
                        newest = None
                        for text in split_frames(buffer):
                            reading = parse_scale_line(text)
                            if reading is not None:
                                reading["raw"] = text
                                newest = reading
                        # Only the last frame of the burst is kept. Queueing the
                        # intermediates is exactly what produced the staleness
                        # this thread exists to fix.
                        if newest is not None:
                            newest["timestamp"] = time.time()
                            _scale_store(newest)
            except Exception as e:
                _scale_failed(str(e))
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
            # A deliberate /scale/reconnect shouldn't have to wait out the
            # failure cooldown.
            if not _scale_reopen.is_set():
                time.sleep(_SCALE_RETRY_COOLDOWN)

    @app.on_event("startup")
    def _scale_startup():
        # Started here rather than at import, for the same reason as the
        # battery thread: lifespan runs post-fork.
        threading.Thread(
            target=_scale_reader,
            name="scale-serial",
            daemon=True,
        ).start()

    def reconnect_scale() -> dict:
        """Ask the reader to drop and reopen the port, then wait briefly so the
        response reflects the outcome rather than the state we started in."""
        _scale_reopen.set()
        deadline = time.monotonic() + _SCALE_RECONNECT_WAIT
        # First for the reader to acknowledge (it clears the flag as it loops),
        # then for a frame to arrive off the fresh handle.
        while time.monotonic() < deadline and _scale_reopen.is_set():
            time.sleep(0.05)
        while time.monotonic() < deadline and not scale_status["ok"]:
            time.sleep(0.05)
        return scale_status

    def read_scale() -> Optional[dict]:
        """Return the newest usable reading, or None if there isn't one.

        Never touches the serial handle — the reader thread owns it.
        """
        with _scale_lock:
            if _scale_last is None:
                return None
            reading = dict(_scale_last)
            age = time.monotonic() - _scale_last_mono
        if age > _SCALE_STALE_AFTER:
            # Backstop: the reader normally trips this within a tick, but if the
            # thread died outright nothing else ever would.
            _scale_failed(_SCALE_ERR_STALE)
            return None
        reading["age_seconds"] = round(age, 3)
        return reading

else:
    import random
    scale_status.update(
        {"ok": True, "error": None, "mode": "stub", "port": "stub", "format": "dx11"}
    )

    def reconnect_scale() -> dict:
        """No-op reconnect for local dev — returns the stub status."""
        return scale_status

    def read_scale() -> Optional[dict]:
        """Simulate the DX11: a load settles onto the pan over ~4 s, then sits
        stable for ~4 s before the next one. The simulated frame is fed through
        the real parser rather than hand-built, so dev exercises the production
        decode path and cannot drift from it."""
        now = time.time()
        phase = now % 8.0
        target = 2.0 + (int(now / 8.0) % 5)     # 2.0 - 6.0 kg
        if phase < 4.0:
            stable = False
            weight = target * (phase / 4.0) + random.uniform(-0.05, 0.05)
        else:
            stable = True
            weight = target
        raw = f"{'S' if stable else 'U'}{weight:+09.2f}"
        reading = parse_scale_line(raw)
        reading["raw"] = raw
        reading["timestamp"] = now
        reading["age_seconds"] = 0.0
        return reading


# ---------------------------------------------------------------------------
# Battery — JBD BMS protocol (pure byte handling, no transport)
# ---------------------------------------------------------------------------

# Frame layout: DD <register> <status> <length> <payload...> <checksum:2> 77
_JBD_START = 0xDD
_JBD_END = 0x77
_JBD_BASIC_INFO_REGISTER = 0x03

# Read the "basic information" register. The trailing FF FD is this frame's own
# checksum (0x10000 - (0x03 + 0x00)); 77 terminates it.
_JBD_BASIC_INFO_COMMAND = bytes.fromhex("DD A5 03 00 FF FD 77")

BATTERY_NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
BATTERY_WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"


def _jbd_checksum_ok(frame: bytes) -> bool:
    """Verify a JBD frame's checksum: 0x10000 minus the sum of every byte
    between the register and the checksum itself, truncated to 16 bits and
    stored big-endian."""
    length = frame[3]
    expected = (0x10000 - sum(frame[2:4 + length])) & 0xFFFF
    return int.from_bytes(frame[4 + length:6 + length], "big") == expected


def _extract_jbd_frame(buffer: bytearray) -> Optional[bytes]:
    """Pull the first complete, checksum-valid frame off the front of `buffer`,
    consuming the bytes it uses. Returns None while the frame is still arriving.

    BLE delivers notifications in ~20-byte fragments, so frames arrive split.
    Reassembling on the length byte alone is not safe: 0xDD occurs inside
    payload data too, so a buffer that starts mid-frame reads a length from
    arbitrary bytes and then decodes into plausible-but-wrong numbers. Nothing
    here is trusted until the checksum agrees; on disagreement we drop a single
    byte and rescan rather than stalling on a length we invented.
    """
    while True:
        start = buffer.find(_JBD_START)
        if start < 0:
            buffer.clear()
            return None
        del buffer[:start]

        if len(buffer) < 2:
            return None
        # We only ever ask for register 0x03, so anything else is a false
        # header — reject it before trusting the length byte behind it.
        if buffer[1] != _JBD_BASIC_INFO_REGISTER:
            del buffer[:1]
            continue

        if len(buffer) < 4:
            return None
        frame_length = 4 + buffer[3] + 3
        if len(buffer) < frame_length:
            return None

        frame = bytes(buffer[:frame_length])
        if frame[-1] == _JBD_END and _jbd_checksum_ok(frame):
            del buffer[:frame_length]
            return frame

        del buffer[:1]


def decode_basic_info(frame: bytes) -> dict:
    """Decode a JBD basic-information (register 0x03) response frame."""
    if len(frame) < 7:
        raise ValueError("frame is too short")
    if frame[0] != _JBD_START or frame[1] != _JBD_BASIC_INFO_REGISTER:
        raise ValueError("not a JBD basic-information frame")

    status = frame[2]
    if status != 0:
        raise ValueError(f"BMS returned status 0x{status:02X}")

    payload = frame[4:4 + frame[3]]
    if len(payload) < 23:
        raise ValueError("incomplete basic-information payload")

    # Voltage is in 10 mV units, current in 10 mA (signed — negative is
    # discharge), capacities in 10 mAh.
    voltage = int.from_bytes(payload[0:2], "big") / 100.0
    current = int.from_bytes(payload[2:4], "big", signed=True) / 100.0

    temperatures = []
    offset = 23
    for _ in range(payload[22]):
        if offset + 2 > len(payload):
            break
        # JBD reports temperature in 0.1 Kelvin.
        raw = int.from_bytes(payload[offset:offset + 2], "big")
        temperatures.append(round(raw / 10.0 - 273.15, 1))
        offset += 2

    # The shunt never settles at exactly zero, so treat a trickle as idle.
    if abs(current) < 0.05:
        state = "idle"
    else:
        state = "discharging" if current < 0 else "charging"

    mosfet = payload[20]
    return {
        "soc": payload[19],
        "voltage": voltage,
        "current": current,
        "state": state,
        "remaining_capacity": int.from_bytes(payload[4:6], "big") / 100.0,
        "full_capacity": int.from_bytes(payload[6:8], "big") / 100.0,
        "cycles": int.from_bytes(payload[8:10], "big"),
        "cell_count": payload[21],
        "temperatures": temperatures,
        "charge_mosfet": bool(mosfet & 0x01),
        "discharge_mosfet": bool(mosfet & 0x02),
        # Raw bitfield; 0 means no alarms. Bit-level decoding is out of scope.
        "protection": int.from_bytes(payload[16:18], "big"),
        # JBD packs the version as hex nibbles: 0x10 is version 1.0.
        "software_version": f"{payload[18] >> 4}.{payload[18] & 0x0F}",
    }


# ---------------------------------------------------------------------------
# Battery — BLE link on Linux; stub on other platforms for local dev
# ---------------------------------------------------------------------------

# The pack's BLE MAC. Override via STREAM_API_BATTERY_ADDRESS if the hardware is
# swapped. BlueZ addresses by MAC; macOS/CoreBluetooth uses an opaque per-host
# UUID instead, which is one more reason the non-Linux path is a stub.
BATTERY_ADDRESS = os.environ.get(
    "STREAM_API_BATTERY_ADDRESS",
    "A5:C2:37:6E:94:70",
)
BATTERY_POLL_INTERVAL = float(os.environ.get("STREAM_API_BATTERY_POLL_INTERVAL", "2.0"))

if sys.platform == "linux":
    from bleak import BleakClient, BleakScanner

    _battery_lock = threading.Lock()
    _battery_last: Optional[dict] = None
    _battery_last_mono: Optional[float] = None

    _BATTERY_SCAN_TIMEOUT = 15.0
    _BATTERY_CONNECT_TIMEOUT = 20.0
    _BATTERY_READ_TIMEOUT = 5.0
    _BATTERY_RETRY_COOLDOWN = 5.0
    # A dropped notification is not worth tearing the link down — reconnecting
    # costs seconds. Only give up once the pack has missed several in a row.
    _BATTERY_MAX_MISSES = 3
    # Keep serving the last good reading across a brief link drop rather than
    # 503ing it. State of charge moves over minutes, and the Jetson's combo
    # Wi-Fi/BT radio shares an antenna with the MJPEG stream this box exists to
    # serve — so short BLE flaps are expected and are not worth blanking a
    # perfectly good number over. Callers see the real age via `age_seconds`.
    _BATTERY_STALE_AFTER = 60.0

    # Failure reasons are constants on purpose: _battery_failed() dedups its
    # logging on message equality, so a reason carrying varying text (D-Bus
    # serials, addresses, elapsed times) would defeat the dedup and fill the
    # journal every retry while the pack is merely switched off.
    _BATTERY_ERR_NOT_FOUND = (
        f"no BLE advertisement from {BATTERY_ADDRESS} — a JBD pack accepts one "
        "client at a time and stops advertising while connected, so close any "
        "phone app, run `bluetoothctl disconnect`, and check the pack is on"
    )
    _BATTERY_ERR_NO_REPLY = "the BMS did not answer the basic-info request"
    _BATTERY_ERR_DROPPED = "the BLE link to the BMS dropped"
    _BATTERY_ERR_STALE = "no fresh reading from the BMS"

    def _battery_reason(e: Exception) -> str:
        """Map an exception onto a stable message — see the constants above."""
        logger.debug("battery BLE error", exc_info=e)
        if isinstance(e, asyncio.TimeoutError):
            return _BATTERY_ERR_NO_REPLY
        return f"BLE error ({type(e).__name__})"

    def _battery_age() -> Optional[float]:
        """Seconds since the last good frame, or None if there has never been
        one. Monotonic: a Jetson with no RTC steps its wall clock by years when
        NTP first syncs, which is exactly when this would be consulted."""
        with _battery_lock:
            if _battery_last_mono is None:
                return None
            return time.monotonic() - _battery_last_mono

    def _battery_store(reading: dict) -> None:
        global _battery_last, _battery_last_mono
        with _battery_lock:
            _battery_last = reading
            _battery_last_mono = time.monotonic()
        if not battery_status["ok"]:
            logger.info("battery reading recovered")
        battery_status["ok"] = True
        battery_status["error"] = None

    def _battery_link_down(reason: str) -> None:
        """Record that the BLE link is down. The last good reading keeps serving
        until it goes stale, so `ok` only flips once we genuinely have nothing
        worth returning."""
        battery_status["connected"] = False
        age = _battery_age()
        if age is None or age > _BATTERY_STALE_AFTER:
            _battery_failed(reason)

    async def _battery_session() -> None:
        """Resolve the pack, hold one connection, and poll until it breaks."""
        # bleak advises resolving a BLEDevice rather than handing BleakClient a
        # bare address (which makes it scan implicitly anyway). Doing it here
        # also separates "pack isn't advertising" — normally a phone app holding
        # the one connection it allows — from "connect refused".
        device = await BleakScanner.find_device_by_address(
            BATTERY_ADDRESS, timeout=_BATTERY_SCAN_TIMEOUT
        )
        if device is None:
            _battery_link_down(_BATTERY_ERR_NOT_FOUND)
            return

        buffer = bytearray()
        frames: list[bytes] = []
        frame_ready = asyncio.Event()
        dropped = asyncio.Event()

        def _on_notify(_, data: bytearray) -> None:
            # bleak runs this on the event-loop thread, so the buffer needs no
            # lock and can never be observed half-updated.
            buffer.extend(data)
            frame = _extract_jbd_frame(buffer)
            if frame is not None:
                frames.append(frame)
                frame_ready.set()

        def _on_disconnect(_) -> None:
            dropped.set()

        async with BleakClient(
            device,
            timeout=_BATTERY_CONNECT_TIMEOUT,
            disconnected_callback=_on_disconnect,
        ) as client:
            # Deliberately no stop_notify: bleak stops notifications on
            # disconnect anyway, and calling it on an already-dropped link
            # raises during the unwind and masks the real error.
            await client.start_notify(BATTERY_NOTIFY_UUID, _on_notify)
            battery_status["connected"] = True
            logger.info("battery connected at %s", device.address)

            misses = 0
            while not dropped.is_set():
                # Start each poll clean so a late frame from the previous
                # exchange can't be mistaken for this request's answer.
                buffer.clear()
                frames.clear()
                frame_ready.clear()
                await client.write_gatt_char(
                    BATTERY_WRITE_UUID,
                    _JBD_BASIC_INFO_COMMAND,
                    response=False,
                )
                try:
                    await asyncio.wait_for(
                        frame_ready.wait(), timeout=_BATTERY_READ_TIMEOUT
                    )
                    reading = decode_basic_info(frames[0])
                except (asyncio.TimeoutError, ValueError) as e:
                    misses += 1
                    if misses >= _BATTERY_MAX_MISSES:
                        raise
                    logger.debug(
                        "battery poll failed (%d/%d): %s",
                        misses, _BATTERY_MAX_MISSES, e,
                    )
                else:
                    misses = 0
                    reading["timestamp"] = time.time()
                    _battery_store(reading)
                await asyncio.sleep(BATTERY_POLL_INTERVAL)

        _battery_link_down(_BATTERY_ERR_DROPPED)

    async def _battery_supervisor() -> None:
        """Reconnect forever. Owns the only BLE connection in the process."""
        while True:
            try:
                await _battery_session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _battery_link_down(_battery_reason(e))
            await asyncio.sleep(_BATTERY_RETRY_COOLDOWN)

    def _battery_thread() -> None:
        # bleak needs one long-lived event loop for all of its operations and
        # asyncio.run() must only be called once, so the whole BLE lifecycle
        # lives on this thread instead of being driven per-request.
        #
        # Two layers on purpose: the supervisor above handles the expected (pack
        # out of range, link dropped, poll timeout) and keeps the same loop
        # alive, which is what bleak requires. This outer loop is a backstop for
        # asyncio.run() itself dying — without it the thread would exit silently,
        # leaving the process up and healthy-looking with a permanently dead
        # battery reader and nothing for systemd's Restart= to notice.
        while True:
            try:
                asyncio.run(_battery_supervisor())
            except Exception:
                logger.exception("battery BLE loop crashed, restarting")
                _battery_failed("battery BLE loop restarting after an error")
            # Getting here at all is a bug — sleep so a permanent failure (no
            # adapter, D-Bus refused) can't spin this thread at 100% CPU.
            time.sleep(_BATTERY_RETRY_COOLDOWN)

    @app.on_event("startup")
    def _battery_startup():
        # Started here rather than at import: lifespan runs post-fork, so a
        # forked worker can't inherit a duplicated D-Bus socket and a dead loop.
        threading.Thread(
            target=_battery_thread,
            name="battery-ble",
            daemon=True,
        ).start()

    def read_battery() -> Optional[dict]:
        """Return the newest usable BMS snapshot, or None if there isn't one.

        Unlike the scale, this never touches the hardware: a BLE round trip
        costs ~100-500 ms and a connect costs seconds, so the background poller
        owns the link and this just reads its cache.
        """
        with _battery_lock:
            if _battery_last is None:
                return None
            reading = dict(_battery_last)
            age = time.monotonic() - _battery_last_mono
        if age > _BATTERY_STALE_AFTER:
            # Backstop: the supervisor normally trips this within a retry, but
            # if the BLE thread died outright nothing else ever would.
            _battery_failed(_BATTERY_ERR_STALE)
            return None
        reading["age_seconds"] = round(age, 2)
        return reading

else:
    # `random` comes from the scale's stub branch above — same platform guard.
    battery_status.update(
        {"ok": True, "error": None, "mode": "stub", "connected": True}
    )

    def read_battery() -> Optional[dict]:
        """Return a simulated pack for local dev. Drifts so that repeated calls
        visibly move, the way a real one would."""
        now = time.time()
        # Sawtooth from 100% down to 41% over ~6 minutes.
        soc = 100 - int(now / 6) % 60
        current = -round(random.uniform(1.0, 3.0), 2)
        return {
            "soc": soc,
            "voltage": round(48.0 + soc * 0.06, 2),
            "current": current,
            "state": "discharging",
            "remaining_capacity": round(52.0 * soc / 100.0, 2),
            "full_capacity": 52.0,
            "cycles": 12,
            "cell_count": 16,
            "temperatures": [24.1, 25.3],
            "charge_mosfet": True,
            "discharge_mosfet": True,
            "protection": 0,
            "software_version": "1.0",
            "timestamp": now,
            "age_seconds": 0.0,
        }


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def generate_mjpeg(width: int, height: int):
    """Yield MJPEG frames; ends cleanly if the camera goes down."""
    while True:
        image = get_color_frame(width, height)
        if image is None:
            if not camera_status["ok"]:
                return
            time.sleep(0.05)
            continue
        _, jpeg = cv2.imencode(".jpg", image)
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + jpeg.tobytes()
            + b"\r\n"
        )


def generate_scale_sse():
    """Yield SSE events; emits one error event and stops if the scale fails.

    Paced deliberately. read_scale() used to block on readline(), which was the
    only thing throttling this loop; now that it is a cache lookup, an
    unthrottled `while True` would spin a core flat out. Events go out when the
    reading changes, plus a heartbeat so a steady pan is distinguishable from a
    wedged stream.
    """
    last_raw = None
    last_emit = 0.0
    while True:
        data = read_scale()
        if not scale_status["ok"]:
            yield f"data: {json.dumps({'error': scale_status['error'], 'timestamp': time.time()})}\n\n"
            return
        now = time.monotonic()
        if data is not None and (
            data["raw"] != last_raw or now - last_emit >= SCALE_STREAM_HEARTBEAT
        ):
            last_raw = data["raw"]
            last_emit = now
            yield f"data: {json.dumps(data)}\n\n"
        time.sleep(SCALE_STREAM_INTERVAL)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", summary="Hardware status for camera, scale and battery")
def health():
    # The battery is reported but deliberately left out of `status`: BLE is
    # markedly flakier than USB, and clients already alert on status != "ok".
    # A dropped pack link should not paint the box red while the camera and
    # scale are fine — read health.battery.ok if you care about it.
    overall = "ok" if (camera_status["ok"] and scale_status["ok"]) else "degraded"
    return {
        "status": overall,
        "camera": camera_status,
        "scale": scale_status,
        "battery": battery_status,
    }


# ---------------------------------------------------------------------------
# System control — reboot / service restart / stats
# ---------------------------------------------------------------------------

# Absolute path so the invocation matches the scoped sudoers NOPASSWD rule
# (see README). Override if `command -v systemctl` differs on your Jetson.
SYSTEMCTL = os.environ.get("STREAM_API_SYSTEMCTL", "/usr/bin/systemctl")


def _run_detached_after_delay(cmd: list[str], delay: float = 1.0) -> None:
    """Fire a privileged command from a daemon thread so the HTTP response is
    flushed first. start_new_session detaches the child so restarting our own
    unit doesn't kill the command mid-flight."""
    def _worker() -> None:
        time.sleep(delay)
        try:
            subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            logger.error("failed to run %s: %s", cmd, e)

    threading.Thread(target=_worker, daemon=True).start()


@app.post(
    "/system/reboot",
    summary="Reboot the Jetson (requires admin token)",
    dependencies=[Depends(require_admin_token)],
)
def system_reboot():
    if sys.platform != "linux":
        return {
            "status": "stub",
            "message": "Reboot is a no-op on this non-Linux dev machine.",
            "would_run": "systemctl reboot",
        }
    logger.warning("reboot requested via API")
    _run_detached_after_delay(["sudo", SYSTEMCTL, "reboot"])
    return {
        "status": "rebooting",
        "message": "Reboot started. The device will be offline for about 30-60 "
        "seconds, then come back online automatically. Poll /health until it "
        "responds again.",
        "estimated_downtime_seconds": 60,
    }


@app.post(
    "/system/shutdown",
    summary="Power off the Jetson (requires admin token + confirm=yes)",
    dependencies=[Depends(require_admin_token), Depends(require_shutdown_confirm)],
)
def system_shutdown():
    if sys.platform != "linux":
        return {
            "status": "stub",
            "message": "Shutdown is a no-op on this non-Linux dev machine.",
            "would_run": "systemctl poweroff",
        }
    logger.warning("shutdown requested via API — device will need a physical power-on")
    _run_detached_after_delay(["sudo", SYSTEMCTL, "poweroff"])
    return {
        "status": "shutting down",
        "message": "Shutdown started. The device powers off in a few seconds and "
        "will NOT come back on its own — it needs a physical power-on.",
        "recoverable_remotely": False,
    }


@app.post(
    "/system/restart-service",
    summary="Restart the stream-api service (requires admin token)",
    dependencies=[Depends(require_admin_token)],
)
def system_restart_service():
    if sys.platform != "linux":
        return {
            "status": "stub",
            "message": "Service restart is a no-op on this non-Linux dev machine.",
            "would_run": "systemctl restart stream-api",
        }
    logger.warning("service restart requested via API")
    _run_detached_after_delay(["sudo", SYSTEMCTL, "restart", "stream-api"])
    return {
        "status": "restarting",
        "message": "Service restart started. The API will be reachable again in "
        "a few seconds. Poll /health until it responds.",
        "estimated_downtime_seconds": 10,
    }


def _jetson_temperature_c() -> Optional[float]:
    """Hottest thermal zone in °C, or None. Tegra exposes millidegrees at
    /sys/class/thermal/thermal_zone*/temp; psutil.sensors_temperatures() is
    unreliable there."""
    if sys.platform != "linux":
        return None
    temps = []
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(path) as f:
                temps.append(int(f.read().strip()) / 1000.0)
        except Exception:
            continue
    return round(max(temps), 1) if temps else None


@app.get("/system/stats", summary="CPU / memory / disk / temperature / uptime")
def system_stats():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = None
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory": {
            "total": mem.total,
            "available": mem.available,
            "percent": mem.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "percent": disk.percent,
        },
        "temperature_c": _jetson_temperature_c(),
        "uptime_seconds": round(time.time() - psutil.boot_time()),
        "load_average": load,
    }


# ---------------------------------------------------------------------------
# Camera endpoints
# ---------------------------------------------------------------------------

# Gamma 0.6 lift table — computed once, applied per capture via cv2.LUT (~0.1ms).
# Pulls the deliberately-underexposed midtones back up to a viewable brightness.
_GAMMA_LUT_060 = np.array(
    [((i / 255.0) ** 0.6) * 255.0 for i in range(256)], dtype=np.uint8
)


def enhance_for_text(image: np.ndarray) -> np.ndarray:
    """LAB CLAHE + shadow gamma lift + unsharp — recovers text under glare/shadow."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    # Stronger local contrast — recovers detail in near-saturated bright bands
    # (where a low-exposure capture leaves headroom to work with).
    l = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(l)
    # Shadow lift — turns a dark low-exposure capture into a viewable image
    # while the glare bands (now well below 255) reveal their hidden detail.
    l = cv2.LUT(l, _GAMMA_LUT_060)
    out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    blurred = cv2.GaussianBlur(out, (0, 0), sigmaX=1.5)
    return cv2.addWeighted(out, 1.5, blurred, -0.5, 0)


@app.get("/stream", summary="Live MJPEG color stream")
def stream(preset: Preset = "fhd"):
    width, height = RESOLUTIONS[preset]
    # Probe once to trigger a lazy pipeline restart if the camera has dropped.
    if get_color_frame(width, height) is None:
        return JSONResponse(
            content={"error": "camera not available", "detail": camera_status["error"]},
            status_code=503,
        )
    return StreamingResponse(
        generate_mjpeg(width, height),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/capture", summary="Capture a single JPEG image")
def capture(
    preset: Preset = "fhd",
    enhance: bool = False,
    exposure_us: int = -1,
    gain: int = -1,
    white_balance: int = -1,
):
    """
    exposure_us:    -1 = leave as-is, 0 = re-enable AE, >0 = manual microseconds
    gain:           -1 = leave as-is, 0 = don't override, >0 = manual (16-248)
    white_balance:  -1 = leave as-is, 0 = re-enable auto-WB, >0 = manual Kelvin (2800-6500)
    All overrides persist across requests until changed again.
    """
    width, height = RESOLUTIONS[preset]
    if sys.platform == "linux" and camera_status["ok"] and (
        exposure_us >= 0 or gain >= 0 or white_balance >= 0
    ):
        with _camera_lock:
            try:
                color_sensor = pipeline.get_active_profile().get_device().first_color_sensor()
                if exposure_us >= 0:
                    _apply_exposure(
                        color_sensor, exposure_us,
                        gain if gain >= 0 else CAMERA_GAIN,
                    )
                elif gain > 0:
                    color_sensor.set_option(rs.option.gain, gain)
                if white_balance >= 0:
                    _apply_white_balance(color_sensor, white_balance)
            except Exception as e:
                logger.warning(
                    "failed to apply overrides (exposure_us=%s gain=%s white_balance=%s): %s",
                    exposure_us, gain, white_balance, e,
                )
    image = get_color_frame(width, height)
    if image is None:
        return JSONResponse(
            content={"error": "camera not available", "detail": camera_status["error"]},
            status_code=503,
        )
    if enhance:
        image = enhance_for_text(image)
    _, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 100])
    return Response(content=jpeg.tobytes(), media_type="image/jpeg")


@app.post("/camera/restart", summary="Force a RealSense pipeline restart")
def camera_restart():
    status = restart_camera()
    return JSONResponse(
        content={"camera": status},
        status_code=200 if status["ok"] else 503,
    )


# ---------------------------------------------------------------------------
# Scale endpoints
# ---------------------------------------------------------------------------

@app.get("/scale/stream", summary="Live weight scale stream (SSE)")
def scale_stream():
    if not scale_status["ok"]:
        return JSONResponse(
            content={"error": "scale not available", "detail": scale_status["error"]},
            status_code=503,
        )
    return StreamingResponse(
        generate_scale_sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/scale/capture", summary="Read current weight from scale")
def scale_capture():
    # read_scale() first: it is what trips the staleness check that flips `ok`.
    data = read_scale()
    if not scale_status["ok"]:
        return JSONResponse(
            content={"error": "scale not available", "detail": scale_status["error"]},
            status_code=503,
        )
    if data is None:
        return JSONResponse(content={"error": "no data from scale"}, status_code=503)
    return data


@app.post("/scale/reconnect", summary="Force the scale serial port to reopen")
def scale_reconnect():
    status = reconnect_scale()
    return JSONResponse(
        content={"scale": status},
        status_code=200 if status["ok"] else 503,
    )


# ---------------------------------------------------------------------------
# Battery endpoints
# ---------------------------------------------------------------------------

@app.get("/battery/capture", summary="Read current battery state from the BMS")
def battery_capture():
    data = read_battery()
    if not battery_status["ok"]:
        return JSONResponse(
            content={
                "error": "battery not available",
                "detail": battery_status["error"],
            },
            status_code=503,
        )
    if data is None:
        return JSONResponse(content={"error": "no data from battery"}, status_code=503)
    return data


def run():
    """Entry point for the `stream-api` console script."""
    import uvicorn

    uvicorn.run(
        "stream_api.main:app",
        host=os.environ.get("STREAM_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("STREAM_API_PORT", "8000")),
    )
