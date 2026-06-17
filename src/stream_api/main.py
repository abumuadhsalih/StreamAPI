import sys
import json
import time
import logging
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

    def _grab_frame():
        if not camera_status["ok"]:
            return None
        try:
            frames = pipeline.wait_for_frames()
            frame = frames.get_color_frame()
            return np.asanyarray(frame.get_data()) if frame else None
        except Exception as e:
            _camera_failed(str(e))
            return None

    @app.on_event("startup")
    def _camera_startup():
        try:
            pipeline.start(config)
            camera_status["ok"] = True
            camera_status["error"] = None
            logger.info("camera pipeline started")
        except Exception as e:
            _camera_failed(f"failed to start pipeline: {e}")

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

SCALE_PORT = "/dev/ttyUSB0"
SCALE_BAUD = 9600

if sys.platform == "linux":
    scale_serial: Optional[serial.Serial] = None

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

@app.get("/stream", summary="Live MJPEG color stream")
def stream(preset: Preset = "fhd"):
    if not camera_status["ok"]:
        return JSONResponse(
            content={"error": "camera not available", "detail": camera_status["error"]},
            status_code=503,
        )
    width, height = RESOLUTIONS[preset]
    return StreamingResponse(
        generate_mjpeg(width, height),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/capture", summary="Capture a single JPEG image")
def capture(preset: Preset = "fhd"):
    if not camera_status["ok"]:
        return JSONResponse(
            content={"error": "camera not available", "detail": camera_status["error"]},
            status_code=503,
        )
    width, height = RESOLUTIONS[preset]
    image = get_color_frame(width, height)
    if image is None:
        return JSONResponse(
            content={"error": "camera not available", "detail": camera_status["error"]},
            status_code=503,
        )
    _, jpeg = cv2.imencode(".jpg", image)
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
    import os
    import uvicorn

    uvicorn.run(
        "stream_api.main:app",
        host=os.environ.get("STREAM_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("STREAM_API_PORT", "8000")),
    )
