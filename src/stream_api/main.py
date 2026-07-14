import os
import sys
import json
import time
import logging
import threading
from typing import Literal, Optional
import cv2
import numpy as np
import serial
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, JSONResponse

logger = logging.getLogger("stream_api")

app = FastAPI(title="RealSense Stream API")

# Open CORS for POC — LAN browser/mobile clients on arbitrary origins.
# Lock down `allow_origins` to specific domains before any production use.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

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
scale_status: dict = {"ok": False, "error": "not yet opened", "mode": "real"}


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


# ---------------------------------------------------------------------------
# Camera — pyrealsense2 is Linux-only; stub on other platforms for local dev
# ---------------------------------------------------------------------------

if sys.platform == "linux":
    import pyrealsense2 as rs

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

    # Optional fixed-exposure / fixed-white-balance overrides. On auto, the
    # camera reacts to specular glare in-frame (darkening the whole capture)
    # and to fluorescent light spikes (green cast). For a fixed measurement
    # rig, locking both to a value tuned to the room gives consistent captures.
    # Leave unset to keep auto behavior.
    _CAMERA_EXPOSURE = os.environ.get("STREAM_API_CAMERA_EXPOSURE")  # microseconds, e.g. "15000"
    _CAMERA_WB = os.environ.get("STREAM_API_CAMERA_WB")              # Kelvin, e.g. "4600"

    def _tune_color_sensor() -> None:
        color_sensor = pipeline.get_active_profile().get_device().first_color_sensor()
        color_sensor.set_option(rs.option.sharpness, 100)
        color_sensor.set_option(rs.option.contrast, 60)
        color_sensor.set_option(rs.option.gamma, 300)
        color_sensor.set_option(rs.option.backlight_compensation, 1)
        color_sensor.set_option(rs.option.power_line_frequency, 1)
        color_sensor.set_option(rs.option.auto_exposure_priority, 0)
        if _CAMERA_EXPOSURE:
            color_sensor.set_option(rs.option.enable_auto_exposure, 0)
            color_sensor.set_option(rs.option.exposure, float(_CAMERA_EXPOSURE))
            logger.info("camera exposure locked at %s us", _CAMERA_EXPOSURE)
        if _CAMERA_WB:
            color_sensor.set_option(rs.option.enable_auto_white_balance, 0)
            color_sensor.set_option(rs.option.white_balance, float(_CAMERA_WB))
            logger.info("camera white balance locked at %s K", _CAMERA_WB)

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
            try:
                frames = pipeline.wait_for_frames()
                frame = frames.get_color_frame()
                return np.asanyarray(frame.get_data()) if frame else None
            except Exception as e:
                _camera_failed(str(e))
                try:
                    pipeline.stop()
                except Exception:
                    pass
                return None

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

# Prefer the kernel's /dev/serial/by-id/ symlink over /dev/ttyUSB0 — the
# ttyUSBN number can shift after USB re-enumeration, but the by-id path
# is stable across reboots and reconnects. Override via STREAM_API_SCALE_PORT
# if the scale hardware changes.
SCALE_PORT = os.environ.get(
    "STREAM_API_SCALE_PORT",
    "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B0000CHM-if00-port0",
)
SCALE_BAUD = int(os.environ.get("STREAM_API_SCALE_BAUD", "9600"))

if sys.platform == "linux":
    scale_serial: Optional[serial.Serial] = None
    # Serialize access to the shared serial handle so concurrent endpoints
    # (e.g. /scale/stream loop + /scale/capture click) don't both call
    # readline() on the same port and trip pyserial's
    # "device reports readiness to read but returned no data" race.
    _scale_lock = threading.Lock()

    def _open_scale() -> None:
        global scale_serial
        try:
            scale_serial = serial.Serial(
                port=SCALE_PORT,
                baudrate=SCALE_BAUD,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=1,
            )
            scale_status["ok"] = True
            scale_status["error"] = None
            logger.info("scale opened on %s", SCALE_PORT)
        except Exception as e:
            scale_serial = None
            _scale_failed(f"failed to open {SCALE_PORT}: {e}")

    @app.on_event("startup")
    def _scale_startup():
        _open_scale()

    def read_scale() -> dict:
        """Read one line from the scale; reopens the port if it was lost."""
        global scale_serial
        with _scale_lock:
            if scale_serial is None:
                _open_scale()
            if scale_serial is None:
                return {"raw": "", "timestamp": time.time()}
            try:
                line = scale_serial.readline().decode("utf-8", errors="ignore").strip()
                scale_status["ok"] = True
                scale_status["error"] = None
                return {"raw": line, "timestamp": time.time()}
            except Exception as e:
                _scale_failed(str(e))
                try:
                    scale_serial.close()
                except Exception:
                    pass
                scale_serial = None
                return {"raw": "", "timestamp": time.time()}

else:
    import random
    scale_status.update({"ok": True, "error": None, "mode": "stub"})

    def read_scale() -> dict:
        """Return simulated weight data for local dev."""
        time.sleep(0.5)
        weight = round(random.uniform(0.0, 10.0), 3)
        return {"raw": f"{weight} kg", "timestamp": time.time()}


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
    """Yield SSE events; emits one error event and stops if the scale fails."""
    while True:
        data = read_scale()
        if not scale_status["ok"]:
            yield f"data: {json.dumps({'error': scale_status['error'], 'timestamp': time.time()})}\n\n"
            return
        if data["raw"]:
            yield f"data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", summary="Hardware status for camera and scale")
def health():
    overall = "ok" if (camera_status["ok"] and scale_status["ok"]) else "degraded"
    return {"status": overall, "camera": camera_status, "scale": scale_status}


# ---------------------------------------------------------------------------
# Camera endpoints
# ---------------------------------------------------------------------------

def enhance_for_text(image: np.ndarray) -> np.ndarray:
    """CLAHE on LAB-L + unsharp mask — lifts small printed text on glared surfaces."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
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
def capture(preset: Preset = "fhd", enhance: bool = False):
    width, height = RESOLUTIONS[preset]
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
    data = read_scale()
    if not scale_status["ok"]:
        return JSONResponse(
            content={"error": "scale not available", "detail": scale_status["error"]},
            status_code=503,
        )
    if not data["raw"]:
        return JSONResponse(content={"error": "no data from scale"}, status_code=503)
    return data


def run():
    """Entry point for the `stream-api` console script."""
    import uvicorn

    uvicorn.run(
        "stream_api.main:app",
        host=os.environ.get("STREAM_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("STREAM_API_PORT", "8000")),
    )
