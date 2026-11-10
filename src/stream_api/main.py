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
from fastapi import FastAPI, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, JSONResponse

from .scale import parse_scale_line, resolve_scale_port, split_frames

logger = logging.getLogger("stream_api")

app = FastAPI(title="Stream API")

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

# Output sizes a client can ask for. The camera runs at one native size and
# every request is scaled from that frame, so clients can ask for different
# presets from the same stream. A preset is a bounding box: the frame is
# scaled down to fit inside it with its aspect ratio kept, never stretched and
# never enlarged. The AR2020 is a 4:3 sensor, so ?preset=fhd of a full-sensor
# frame is 1440x1080, not 1920x1080. (On the 16:9 ZED these were exact sizes;
# the 2k preset went with it.)
RESOLUTIONS: dict[str, tuple[int, int]] = {
    "sd": (640, 480),
    "hd": (1280, 720),
    "fhd": (1920, 1080),
}
Preset = Literal["sd", "hd", "fhd"]


def _fit(image: np.ndarray, preset: Optional[str]) -> np.ndarray:
    """Scale `image` down to fit inside the preset's box, aspect kept."""
    if preset is None:
        return image
    bw, bh = RESOLUTIONS[preset]
    h, w = image.shape[:2]
    s = min(bw / w, bh / h)
    if s >= 1.0:
        return image
    return cv2.resize(
        image, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA
    )


# ---------------------------------------------------------------------------
# Camera configuration — Vadzo Falcon-2020CRS (onsemi AR2020, 20 MP, USB 3)
# ---------------------------------------------------------------------------
#
# Nothing in this service processes the image. Since the move from the ZED 2i
# (2026-11-10) /capture serves what the camera produces: the crop, de-glare,
# enhance, keep-chroma, fish-crop, stereo-fill and mm-per-pixel stages of the
# ZED branches are gone, and the only levers on the picture are the camera's
# own ISP controls (POST /camera/controls). The ZED code lives on
# feature/fish-crop.
#
# Every setting below is optional. Unset means "whatever the camera and the
# SDK choose", which is the right default until the camera has been probed on
# the Jetson.


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


# Which camera to open, by the name the SDK reports (get_devices()). Empty =
# the first one found. The name is stored in the camera itself
# (set_device_name), so a commissioned name survives a change of USB port.
CAMERA_NAME = _env("STREAM_API_CAMERA_NAME")

# Pixel formats tried in order at every open; the first one the camera
# streams and this file can decode wins, and is reported as camera.format in
# /health and X-Pixel-Format on /capture. MJPG first: a 20 MP frame
# uncompressed is ~39 MB in YUYV, which holds USB 3 to a few frames a second,
# whereas MJPEG is the camera's own encoder, and a bare /capture then returns
# the camera's JPEG byte for byte. STREAM_API_CAMERA_FORMAT pins a single one
# (e.g. UYVY for uncompressed frames).
_CAMERA_FORMATS_DEFAULT = ("MJPG", "UYVY", "YUYV", "BA81")
CAMERA_FORMATS = (
    (_env("STREAM_API_CAMERA_FORMAT").upper(),)
    if _env("STREAM_API_CAMERA_FORMAT")
    else _CAMERA_FORMATS_DEFAULT
)


def _parse_size(spec: str):
    """'WxH' -> (w, h), or None for empty/garbage (logged)."""
    if not spec:
        return None
    try:
        w, h = (int(v) for v in spec.lower().split("x"))
        if not (100 <= w <= 5120 and 100 <= h <= 3840):
            raise ValueError
    except ValueError:
        logger.warning("STREAM_API_CAMERA_SIZE=%r is not WxH within 5120x3840, ignored", spec)
        return None
    return (w, h)


# Output size from the camera, "WxH". Empty = the camera's default mode.
# Beware the SDK's "virtual resolution": a size smaller than an advertised
# mode is a WINDOW cut out of the larger mode, not a downscale, so 1920x1080
# is a narrower view of the tray, not the whole tray at fewer pixels. For a
# smaller image of the whole scene use ?preset= instead.
CAMERA_SIZE = _parse_size(_env("STREAM_API_CAMERA_SIZE"))


def _parse_fps(spec: str) -> Optional[int]:
    if not spec:
        return None
    try:
        v = int(spec)
        if not 1 <= v <= 120:
            raise ValueError
    except ValueError:
        logger.warning("STREAM_API_CAMERA_FPS=%r is not 1-120, ignored", spec)
        return None
    return v


# Frame rate requested from the camera. Empty = the camera's default for the
# mode. Long exposures cap it regardless.
CAMERA_FPS = _parse_fps(_env("STREAM_API_CAMERA_FPS"))

# Only used if the camera streams raw Bayer (BA81). Vadzo's example converts
# BA81 with COLOR_BayerRG2BGR; if red and blue come out swapped, try BG, GB or
# GR. This is decoding, not processing: a Bayer frame is not an image until
# it has been demosaiced.
_BAYER_CODES = {
    "RG": cv2.COLOR_BayerRG2BGR,
    "BG": cv2.COLOR_BayerBG2BGR,
    "GB": cv2.COLOR_BayerGB2BGR,
    "GR": cv2.COLOR_BayerGR2BGR,
}
CAMERA_BAYER = _env("STREAM_API_CAMERA_BAYER").upper() or "RG"
if CAMERA_BAYER not in _BAYER_CODES:
    logger.warning("STREAM_API_CAMERA_BAYER=%r is not RG/BG/GB/GR, using RG", CAMERA_BAYER)
    CAMERA_BAYER = "RG"


def decode_frame(raw: np.ndarray, fourcc: str) -> np.ndarray:
    """Turn a frame as the SDK delivers it into a plain BGR image.

    Shapes per the VISPA guide (section 7.2): MJPEG is one-dimensional (its
    size varies per frame), YUYV/UYVY are (h, w*2), RGB24/BGR24 (h, w*3),
    GREY and Bayer (h, w). Raises ValueError on anything it cannot decode.
    """
    if fourcc == "BGR":  # the macOS stub, already an image
        return raw
    if fourcc == "MJPG":
        image = cv2.imdecode(raw.reshape(-1), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("MJPEG frame did not decode")
        return image
    if raw.ndim != 2:
        raise ValueError(f"unexpected {fourcc} frame shape {raw.shape}")
    h, row = raw.shape
    if fourcc in ("YUYV", "UYVY"):
        code = cv2.COLOR_YUV2BGR_YUYV if fourcc == "YUYV" else cv2.COLOR_YUV2BGR_UYVY
        return cv2.cvtColor(raw.reshape(h, row // 2, 2), code)
    if fourcc in ("RGB3", "BGR3"):
        image = raw.reshape(h, row // 3, 3)
        return cv2.cvtColor(image, cv2.COLOR_RGB2BGR) if fourcc == "RGB3" else image
    if fourcc == "GREY":
        return cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
    if fourcc == "BA81":
        return cv2.cvtColor(raw, _BAYER_CODES[CAMERA_BAYER])
    raise ValueError(f"pixel format {fourcc} is not supported")


# The camera's own ISP controls, each a VISPA set_<name>_op(device, int).
# Applied after every open in THIS order, so an auto switch lands before the
# manual value it gates. Ranges are the camera's: the SDK rejects an
# out-of-range value with a message naming the range, and POST
# /camera/controls hands that message straight back, so nothing here
# hardcodes a range that could drift from the firmware.
# roi_exposure is the one control with four values: set_roi_exposure_op(
# device, mode, x, y, size), meter auto-exposure on a window instead of the
# whole frame (mode 0 = full frame; x and y on a 0-255 grid).
CAMERA_CONTROLS = (
    "auto_exposure",
    "exposure",
    "gain",
    "roi_exposure",
    "brightness",
    "auto_white_balance",
    "white_balance",
    "contrast",
    "saturation",
    "hue",
    "gamma",
    "sharpness",
    "black_level",
    "denoise",
    "hdr",
    "tone_control",
    "color_mode",
    "scene_mode",
    "flicker",
    "flip",
)

# Auto exposure and auto white balance are asserted on every open, as the ZED
# path did with AEC_AGC and WHITEBALANCE_AUTO: settings can persist in the
# camera between opens, and a manual value left behind by a vendor tool would
# otherwise survive silently. Everything else stays at the camera's default
# unless STREAM_API_CAMERA_<NAME> sets it, e.g. STREAM_API_CAMERA_FLICKER=1
# for 50 Hz mains or STREAM_API_CAMERA_ROI_EXPOSURE=1,128,128,4.
_CONTROL_DEFAULTS = {"auto_exposure": 1, "auto_white_balance": 1}


def _parse_roi_exposure(spec: str):
    """'mode,x,y,size' -> [mode, x, y, size], or None if malformed."""
    try:
        vals = [int(v) for v in spec.split(",")]
    except ValueError:
        return None
    return vals if len(vals) == 4 else None


def _controls_from_env() -> dict:
    out: dict = {}
    for name in CAMERA_CONTROLS:
        var = f"STREAM_API_CAMERA_{name.upper()}"
        raw = _env(var)
        if not raw:
            if name in _CONTROL_DEFAULTS:
                out[name] = _CONTROL_DEFAULTS[name]
            continue
        if name == "roi_exposure":
            value = _parse_roi_exposure(raw)
            if value is None:
                logger.warning("%s=%r is not mode,x,y,size, ignored", var, raw)
                continue
        else:
            try:
                value = int(raw)
            except ValueError:
                logger.warning("%s=%r is not an integer, ignored", var, raw)
                continue
        out[name] = value
    return out


# The control values in force. POST /camera/controls moves them and every
# open re-applies them, so a self-heal reopen keeps what was set; a service
# restart returns to the environment, so nothing poked in while testing leaks
# into production.
_controls = _controls_from_env()


def _ordered_controls(values: dict) -> list:
    return sorted(values.items(), key=lambda kv: CAMERA_CONTROLS.index(kv[0]))

# ---------------------------------------------------------------------------
# Hardware status — surfaced via /health and used to short-circuit endpoints
# ---------------------------------------------------------------------------

camera_status: dict = {
    "ok": False,
    "error": "not yet started",
    "mode": "real",
    # Camera name as the SDK reports it, plus get_device_info() (firmware).
    "device": None,
    # vispa.version() of the installed SDK.
    "sdk": None,
    # Pixel format in force (one of CAMERA_FORMATS) and the size of the
    # frames it delivers — the native size a bare /capture returns.
    "format": None,
    "frame": None,
    # Frame rate requested (STREAM_API_CAMERA_FPS), null = camera default.
    "fps": CAMERA_FPS,
    # ISP control values in force, re-applied on every open.
    "controls": _controls,
}
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
# Camera — Vadzo VISPA SDK on Linux (aarch64 wheel); stub elsewhere for dev
# ---------------------------------------------------------------------------

# Seconds to wait for a frame. The guide's default for continuous reads; the
# first frame after a start gets longer while the camera settles.
_FRAME_TIMEOUT = 2.0
_FIRST_FRAME_TIMEOUT = 5.0

_CAMERA_ERR_NO_SDK = (
    "the VISPA camera SDK is not installed in this venv — install the "
    "vispa-*-cp313-*_aarch64.whl from the Falcon-2020CRS pack (see DEPLOYMENT.md)"
)
_CAMERA_ERR_NOT_FOUND = (
    "no Falcon camera found — check the USB 3 cable and port, and that "
    "vadzo_env.sh has been run (without its udev rule the SDK cannot see "
    "the camera); unplug/replug after running it"
)
_CAMERA_ERR_BUSY = (
    "Falcon camera detected but busy — another process holds it (a vendor "
    "viewer, a probe script, or a second copy of this service); close it and retry"
)

if sys.platform == "linux":
    try:
        import vispa
        from VISPA_Falcon import FalconCamera
        _sdk_error = None
        camera_status["sdk"] = str(vispa.version())
    except ImportError as e:
        logger.error("VISPA SDK import failed: %s", e)
        FalconCamera = None
        _sdk_error = _CAMERA_ERR_NO_SDK

    # The SDK addresses everything through one controller and the camera's
    # name. Created lazily on the first open (post-fork, in the startup hook)
    # and kept for the life of the process; only the device is closed and
    # reopened on a self-heal.
    _cam = None
    _dev: Optional[str] = None

    # Serialize all camera access: the SDK hands frames over a
    # single-consumer ring, and a lazy reopen from concurrent requests must be
    # single-flighted.
    _camera_lock = threading.Lock()
    _camera_last_retry = 0.0
    _CAMERA_RETRY_COOLDOWN = 5.0  # seconds, so a dead USB bus is not hammered by /stream

    def _check(status, what: str) -> None:
        # The SDK reports failure as a descriptive string instead of raising;
        # equality with "success" is the only reliable test (guide, section 9).
        if status != "success":
            raise RuntimeError(f"{what}: {status}")

    def _close_camera() -> None:
        """Stop and release the device. Caller holds _camera_lock."""
        global _dev
        if _cam is not None and _dev is not None:
            for call in (_cam.stop_acquisition, _cam.close_device):
                try:
                    call(_dev)
                except Exception:
                    pass
        _dev = None

    def _apply_control(name: str, value) -> str:
        if name == "roi_exposure":
            return _cam.set_roi_exposure_op(_dev, *value)
        return getattr(_cam, f"set_{name}_op")(_dev, value)

    def _apply_controls() -> None:
        """Push every control in force to the camera. Must run AFTER
        start_acquisition(): starting re-applies the camera's standard control
        set and overwrites any _op value set before it (guide, section 8)."""
        for name, value in _ordered_controls(_controls):
            try:
                status = _apply_control(name, value)
            except Exception as e:
                status = str(e)
            if status != "success":
                logger.warning("camera control %s=%s not applied: %s", name, value, status)

    def _start(fmt: str) -> np.ndarray:
        """Configure, start and read one frame in `fmt`. Returns it decoded;
        raises with the SDK's message if any step fails."""
        _check(_cam.set_pixel_format(_dev, fmt), "set_pixel_format")
        if CAMERA_SIZE:
            _check(_cam.set_roi(_dev, width=CAMERA_SIZE[0], height=CAMERA_SIZE[1]), "set_roi")
        # Newest frame first: a request wants "now", not the oldest frame
        # queued since the last one (the same reasoning as the scale reader).
        _check(_cam.set_acquisition_mode(_dev, "continuous", order="lifo"), "set_acquisition_mode")
        _check(_cam.start_acquisition(_dev), "start_acquisition")
        if CAMERA_FPS:
            status = _cam.set_frame_rate_op(_dev, CAMERA_FPS)
            if status != "success":
                logger.warning("camera frame rate %d not applied: %s", CAMERA_FPS, status)
        # blocking=True: with False the SDK resets the camera on its own when a
        # frame is late, which repeats and hides the original problem while the
        # camera is coming up (guide, section 7.3). Recovery is ours.
        frame = _cam.get_frame(_dev, timeout=_FIRST_FRAME_TIMEOUT, blocking=True)
        if isinstance(frame, str):
            raise RuntimeError(f"first frame: {frame}")
        return decode_frame(np.array(frame, copy=True), fmt)

    def _open_camera() -> bool:
        """(Re)open the camera. Caller must hold _camera_lock."""
        global _cam, _dev, _camera_last_retry
        _camera_last_retry = time.monotonic()
        if _sdk_error:
            _camera_failed(_sdk_error)
            return False
        _close_camera()
        try:
            if _cam is None:
                _cam = FalconCamera().falcon_controller()
            devices = list(_cam.get_devices() or [])
        except Exception as e:
            _camera_failed(f"failed to enumerate cameras: {e}")
            return False
        # Re-listed on every open: after a disconnect the camera may come
        # back under a different identity (guide, section 9).
        if not devices:
            _camera_failed(_CAMERA_ERR_NOT_FOUND)
            return False
        name = CAMERA_NAME or devices[0]
        if name not in devices:
            _camera_failed(f"camera {name!r} (STREAM_API_CAMERA_NAME) not found; connected: {devices}")
            return False
        status = _cam.open_device(name)
        if status != "success":
            _camera_failed(_CAMERA_ERR_BUSY if "busy" in str(status).lower() else f"camera open refused: {status}")
            return False
        _dev = name
        try:
            info = _cam.get_device_info(name)
            info = list(info) if isinstance(info, tuple) else info
        except Exception as e:
            info = f"unreadable: {e}"
        camera_status["device"] = {"name": name, "info": info}

        errors = []
        for fmt in CAMERA_FORMATS:
            try:
                first = _start(fmt)
                break
            except Exception as e:
                errors.append(f"{fmt}: {e}")
                try:
                    _cam.stop_acquisition(_dev)
                except Exception:
                    pass
        else:
            _close_camera()
            _camera_failed("camera opened but would not stream — " + "; ".join(errors))
            return False
        if errors:
            logger.info("camera formats skipped: %s", "; ".join(errors))
        camera_status["format"] = fmt
        camera_status["frame"] = {"width": int(first.shape[1]), "height": int(first.shape[0])}
        _apply_controls()
        camera_status["ok"] = True
        camera_status["error"] = None
        logger.info(
            "camera opened: %s %s %dx%d fps=%s info=%s",
            name, fmt, first.shape[1], first.shape[0], CAMERA_FPS or "default", info,
        )
        return True

    def _grab_raw():
        """One frame as the SDK delivered it, copied out of the SDK's frame
        pool, with its pixel format. (None, None) if no frame."""
        with _camera_lock:
            if not camera_status["ok"]:
                if time.monotonic() - _camera_last_retry < _CAMERA_RETRY_COOLDOWN:
                    return None, None
                if not _open_camera():
                    return None, None
            # Two attempts per request: on the first failure the camera is
            # reopened in-line and read once more, so a USB hiccup costs the
            # client a slow frame rather than a 503.
            for attempt in (1, 2):
                try:
                    frame = _cam.get_frame(_dev, timeout=_FRAME_TIMEOUT, blocking=True)
                    if isinstance(frame, str):
                        raise RuntimeError(frame)
                    # Copied: the SDK delivers frames zero-copy from a pool it
                    # reuses, so the array must not outlive this read.
                    return np.array(frame, copy=True), camera_status["format"]
                except Exception as e:
                    logger.info("frame read attempt %d failed: %s", attempt, e)
                    _close_camera()
                    if attempt == 2:
                        _camera_failed(str(e))
                        return None, None
                    if not _open_camera():
                        return None, None
            return None, None

    def restart_camera() -> dict:
        """Force a camera close+reopen on demand, bypassing the cooldown."""
        global _camera_last_retry
        with _camera_lock:
            _camera_last_retry = 0.0
            _open_camera()
        return camera_status

    def set_camera_controls(values: dict) -> dict:
        """Apply controls now and keep them across reopens. Returns the
        SDK's status string per control. A value the camera rejects is not
        kept; with the camera down, values are kept and applied on the next
        open (where a rejection is logged)."""
        results = {}
        with _camera_lock:
            for name, value in _ordered_controls(values):
                if camera_status["ok"]:
                    try:
                        status = _apply_control(name, value)
                    except Exception as e:
                        status = str(e)
                    if status == "success":
                        _controls[name] = value
                else:
                    status = "pending: camera not open, applied on the next open"
                    _controls[name] = value
                results[name] = status
        logger.info("camera controls %s -> %s", values, results)
        return results

    @app.on_event("startup")
    def _camera_startup():
        with _camera_lock:
            _open_camera()

    @app.on_event("shutdown")
    def _camera_shutdown():
        with _camera_lock:
            _close_camera()

else:
    def _stub_frame() -> np.ndarray:
        """A placeholder at the AR2020's full 4:3 size, or a real capture from
        STREAM_API_STUB_FRAME so a laptop can serve a genuine frame."""
        path = os.environ.get("STREAM_API_STUB_FRAME")
        if path:
            image = cv2.imread(path)
            if image is not None:
                return image
            logger.warning("STREAM_API_STUB_FRAME=%r could not be read", path)
        w, h = CAMERA_SIZE or (5120, 3840)
        image = np.zeros((h, w, 3), dtype=np.uint8)
        cv2.putText(
            image,
            "Falcon-2020CRS not available (stub)",
            (w // 10, h // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            w / 1000,
            (255, 255, 255),
            max(2, w // 500),
        )
        return image

    _STUB = _stub_frame()  # built once; read-only from here on
    camera_status.update({
        "ok": True,
        "error": None,
        "mode": "stub",
        "device": {"name": "stub", "info": None},
        "format": "BGR",
        "frame": {"width": int(_STUB.shape[1]), "height": int(_STUB.shape[0])},
    })

    def _grab_raw():
        return _STUB, "BGR"

    def restart_camera() -> dict:
        """No-op restart for local dev — returns the stub status."""
        return camera_status

    def set_camera_controls(values: dict) -> dict:
        """Record the values so the stub answers like the real thing."""
        _controls.update(values)
        return {name: "stub" for name in values}


def _grab_frame() -> Optional[np.ndarray]:
    """The newest frame as a BGR image, or None."""
    raw, fourcc = _grab_raw()
    if raw is None:
        return None
    try:
        return decode_frame(raw, fourcc)
    except ValueError as e:
        logger.warning("camera frame dropped: %s", e)
        return None


def get_color_frame(preset: Optional[str] = None):
    """Grab a frame, scaled down to fit the preset if one is given."""
    image = _grab_frame()
    if image is None:
        return None
    return _fit(image, preset)

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

    # Talking to bluetoothd directly, below the level bleak exposes — see
    # _battery_drop_stale_link(). dbus-fast is bleak's own transport, so this
    # adds no new wheel to the Jetson, but it is declared explicitly in
    # pyproject.toml rather than leaned on as a transitive dependency.
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType, MessageType
    from dbus_fast.message import Message

    _battery_lock = threading.Lock()
    _battery_last: Optional[dict] = None
    _battery_last_mono: Optional[float] = None

    _BATTERY_SCAN_TIMEOUT = 15.0
    _BATTERY_CONNECT_TIMEOUT = 20.0
    _BATTERY_READ_TIMEOUT = 5.0
    _BATTERY_RETRY_COOLDOWN = 5.0
    # Bounds the whole D-Bus exchange in _battery_drop_stale_link(). It runs on
    # the same event loop as the poller, so a wedged bluetoothd must not be able
    # to park the battery thread indefinitely.
    _BATTERY_DBUS_TIMEOUT = 5.0
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
        "phone app holding it, and check the pack is powered on and in range"
    )
    # Distinct from _BATTERY_ERR_NOT_FOUND on purpose: this one is transient and
    # self-clearing, so /health should not send anyone hunting for a phone app.
    _BATTERY_ERR_STALE_LINK = (
        "bluetoothd was holding a leftover connection to the pack — dropped it "
        "so the pack advertises again, reconnecting"
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

    async def _battery_drop_stale_link() -> bool:
        """Drop a connection bluetoothd is holding to the pack. True if it did.

        This is the escape hatch from a trap the service sets for itself. The
        BLE work runs on a daemon thread, so killing the process (`systemctl
        restart`, a crash, SIGKILL) tears down bleak's D-Bus client without ever
        disconnecting — and bluetoothd keeps the ACL open on its own behalf. The
        pack, still connected, stops advertising. The scan above then cannot
        find it *ever again*: `find_device_by_address()` matches on
        advertisement callbacks, and so does `BleakClient(<address string>)`,
        which scans internally whenever it wasn't handed a `BLEDevice`. Nothing
        in bleak's API can attach to a link bluetoothd already owns, and
        restarting the service does not clear a connection it does not hold, so
        without this the box needs a human with `bluetoothctl disconnect`.

        Dropping the link is what breaks the cycle: the pack resumes
        advertising, and the supervisor's next retry connects the normal way.

        Talks to org.bluez over raw D-Bus messages rather than proxy objects —
        the same idiom bleak uses internally, and it skips an introspection
        round trip. Every failure is swallowed: this runs only when we are
        already reporting a fault, and a bluetoothd that will not answer is
        indistinguishable in outcome from a pack that is simply switched off.
        """

        async def _run() -> bool:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                reply = await bus.call(
                    Message(
                        destination="org.bluez",
                        path="/",
                        interface="org.freedesktop.DBus.ObjectManager",
                        member="GetManagedObjects",
                    )
                )
                if reply is None or reply.message_type != MessageType.METHOD_RETURN:
                    return False

                wanted = BATTERY_ADDRESS.upper()
                for path, interfaces in reply.body[0].items():
                    props = interfaces.get("org.bluez.Device1")
                    if props is None:
                        continue
                    # Properties arrive as D-Bus variants; .value unwraps them.
                    address = props.get("Address")
                    if address is None or address.value.upper() != wanted:
                        continue
                    connected = props.get("Connected")
                    if connected is None or not connected.value:
                        # The pack is known to BlueZ but not connected, so the
                        # scan miss is a genuine one — leave it to the caller.
                        return False
                    reply = await bus.call(
                        Message(
                            destination="org.bluez",
                            path=path,
                            interface="org.bluez.Device1",
                            member="Disconnect",
                        )
                    )
                    return (
                        reply is not None
                        and reply.message_type == MessageType.METHOD_RETURN
                    )
                return False
            finally:
                bus.disconnect()

        try:
            return await asyncio.wait_for(_run(), _BATTERY_DBUS_TIMEOUT)
        except Exception as e:
            logger.debug("could not clear a stale BlueZ link", exc_info=e)
            return False

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
            # A miss is usually just a pack that is off or held by a phone, but
            # it is also how a leftover bluetoothd connection presents — and
            # that one never clears itself. Try to drop it before blaming the
            # pack; the supervisor retries in _BATTERY_RETRY_COOLDOWN either
            # way, and a disconnect the pack never saw costs one D-Bus call.
            if await _battery_drop_stale_link():
                logger.warning(
                    "dropped a stale bluetoothd connection to %s — the pack "
                    "should advertise again on the next retry",
                    BATTERY_ADDRESS,
                )
                _battery_link_down(_BATTERY_ERR_STALE_LINK)
            else:
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

def generate_mjpeg(preset: Optional[str]):
    """Yield MJPEG frames; ends cleanly if the camera goes down."""
    while True:
        image = get_color_frame(preset)
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

@app.get("/stream", summary="Live MJPEG color stream")
def stream(preset: Preset = "fhd"):
    """
    The full camera view, scaled down to fit the preset (aspect kept). A 20 MP
    frame is decoded and re-encoded per frame here, so the frame rate depends
    on the camera format and the Jetson's CPU; /capture is the full-quality path.
    """
    # Probe once to trigger a lazy camera reopen if the camera has dropped.
    if get_color_frame(preset) is None:
        return JSONResponse(
            content={"error": "camera not available", "detail": camera_status["error"]},
            status_code=503,
        )
    return StreamingResponse(
        generate_mjpeg(preset),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


def _is_whole_jpeg(raw: np.ndarray) -> bool:
    """SOI at the start and EOI at the end — a frame cut short by USB
    bandwidth fails this, and is retried as a 503 rather than served torn."""
    b = raw.reshape(-1)
    return b.size > 4 and b[0] == 0xFF and b[1] == 0xD8 and b[-2] == 0xFF and b[-1] == 0xD9


@app.get("/capture", summary="Capture a single JPEG image")
def capture(preset: Optional[Preset] = None):
    """
    The camera's frame, unprocessed: no crop, no de-glare, no enhance. The
    picture is whatever the camera's own ISP produces under the controls in
    force (POST /camera/controls).

    preset: unset, the frame comes back at the camera's native size. When the
            camera streams MJPEG this is the camera's own JPEG, byte for byte,
            neither decoded nor re-encoded. Set, the frame is scaled down to
            fit the preset's box with its aspect kept (the AR2020 is 4:3, so
            fhd is 1440x1080).

    Headers: X-Resolution (the size returned), X-Pixel-Format (what the
    camera streamed).

    Query params from the ZED branches (crop, deglare, enhance, fish, chroma)
    are no longer read; a client still sending them gets the plain frame.
    """
    raw, fourcc = _grab_raw()
    if raw is None:
        return JSONResponse(
            content={"error": "camera not available", "detail": camera_status["error"]},
            status_code=503,
        )
    headers = {"X-Pixel-Format": fourcc}
    if preset is None and fourcc == "MJPG":
        if not _is_whole_jpeg(raw):
            return JSONResponse(
                content={"error": "camera not available", "detail": "the camera delivered a truncated JPEG frame"},
                status_code=503,
            )
        frame = camera_status["frame"] or {}
        headers["X-Resolution"] = f"{frame.get('width')}x{frame.get('height')}"
        return Response(content=raw.tobytes(), media_type="image/jpeg", headers=headers)
    try:
        image = _fit(decode_frame(raw, fourcc), preset)
    except ValueError as e:
        return JSONResponse(
            content={"error": "camera not available", "detail": str(e)},
            status_code=503,
        )
    headers["X-Resolution"] = f"{image.shape[1]}x{image.shape[0]}"
    _, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 100])
    return Response(content=jpeg.tobytes(), media_type="image/jpeg", headers=headers)


@app.post("/camera/restart", summary="Force a camera close+reopen")
def camera_restart():
    status = restart_camera()
    return JSONResponse(
        content={"camera": status},
        status_code=200 if status["ok"] else 503,
    )


@app.post("/camera/controls", summary="Set the camera's own ISP controls (exposure, white balance, colour, HDR, ...)")
def camera_controls(
    auto_exposure: Optional[int] = Query(None, description="1 auto (the default), 0 manual"),
    exposure: Optional[int] = Query(None, description="Manual exposure in ms; needs auto_exposure=0"),
    gain: Optional[int] = Query(None, description="Analogue gain"),
    roi_exposure: Optional[str] = Query(
        None,
        description="mode,x,y,size: meter auto-exposure on a window instead of the whole frame "
        "(mode 0 = full frame; x, y on a 0-255 grid)",
    ),
    brightness: Optional[int] = Query(None),
    auto_white_balance: Optional[int] = Query(None, description="1 auto (the default), 0 manual"),
    white_balance: Optional[int] = Query(None, description="Manual white balance; needs auto_white_balance=0"),
    contrast: Optional[int] = Query(None),
    saturation: Optional[int] = Query(None),
    hue: Optional[int] = Query(None),
    gamma: Optional[int] = Query(None),
    sharpness: Optional[int] = Query(None),
    black_level: Optional[int] = Query(None),
    denoise: Optional[int] = Query(None, description="0-15"),
    hdr: Optional[int] = Query(None, description="1 off, 2 auto"),
    tone_control: Optional[int] = Query(None),
    color_mode: Optional[int] = Query(None),
    scene_mode: Optional[int] = Query(None),
    flicker: Optional[int] = Query(None, description="0 auto, 1 50 Hz, 2 60 Hz"),
    flip: Optional[int] = Query(None, description="0 none, 1 horizontal, 2 vertical, 3 both"),
):
    """
    The camera's built-in controls, applied in the camera. Send any subset;
    omitted controls are left alone. The camera validates each value, and a
    rejection comes back in `results` with the SDK's message, which names the
    valid range (400 if any was rejected).

    Values survive a self-heal reopen but NOT a service restart, which
    returns to the STREAM_API_CAMERA_<NAME> environment (auto exposure and
    auto white balance on, everything else at the camera's default). Set the
    env var in the unit to make a value permanent. They change the sensor, so
    /stream moves with them, not only /capture.
    """
    values = {k: v for k, v in locals().items() if v is not None}
    if not values:
        return JSONResponse(
            content={"error": "no control given", "detail": "pass one or more of: " + ", ".join(CAMERA_CONTROLS)},
            status_code=400,
        )
    if "roi_exposure" in values:
        roi = _parse_roi_exposure(values["roi_exposure"])
        if roi is None:
            return JSONResponse(
                content={"error": "bad roi_exposure", "detail": "roi_exposure must be mode,x,y,size"},
                status_code=400,
            )
        values["roi_exposure"] = roi
    results = set_camera_controls(values)
    failed = [k for k, s in results.items() if s not in ("success", "stub") and not s.startswith("pending")]
    code = 200 if not failed else 400
    if not camera_status["ok"]:
        code = 503
    return JSONResponse(content={"results": results, "camera": camera_status}, status_code=code)


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
