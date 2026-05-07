import sys
import json
import time
from typing import Literal
import cv2
import numpy as np
import serial
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, Response

app = FastAPI(title="RealSense Stream API")

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
        frames = pipeline.wait_for_frames()
        frame = frames.get_color_frame()
        return np.asanyarray(frame.get_data()) if frame else None

    @app.on_event("startup")
    def on_startup():
        pipeline.start(config)

    @app.on_event("shutdown")
    def on_shutdown():
        pipeline.stop()

else:
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
    scale_serial = serial.Serial(
        port=SCALE_PORT,
        baudrate=SCALE_BAUD,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=1,
    )

    def read_scale() -> dict:
        """Read one line from the scale and return parsed data."""
        line = scale_serial.readline().decode("utf-8", errors="ignore").strip()
        return {"raw": line, "timestamp": time.time()}

else:
    import random

    def read_scale() -> dict:
        """Return simulated weight data for local dev."""
        time.sleep(0.5)
        weight = round(random.uniform(0.0, 10.0), 3)
        return {"raw": f"{weight} kg", "timestamp": time.time()}


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def generate_mjpeg(width: int, height: int):
    """Yield MJPEG frames at the requested resolution."""
    while True:
        image = get_color_frame(width, height)
        if image is None:
            continue
        _, jpeg = cv2.imencode(".jpg", image)
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + jpeg.tobytes()
            + b"\r\n"
        )


def generate_scale_sse():
    """Yield SSE events with weight readings."""
    while True:
        data = read_scale()
        if data["raw"]:
            yield f"data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Camera endpoints
# ---------------------------------------------------------------------------

@app.get("/stream", summary="Live MJPEG color stream")
def stream(preset: Preset = "fhd"):
    width, height = RESOLUTIONS[preset]
    return StreamingResponse(
        generate_mjpeg(width, height),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/capture", summary="Capture a single JPEG image")
def capture(preset: Preset = "fhd"):
    width, height = RESOLUTIONS[preset]
    image = get_color_frame(width, height)
    if image is None:
        return Response(content="No frame available", status_code=503)
    _, jpeg = cv2.imencode(".jpg", image)
    return Response(content=jpeg.tobytes(), media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Scale endpoints
# ---------------------------------------------------------------------------

@app.get("/scale/stream", summary="Live weight scale stream (SSE)")
def scale_stream():
    return StreamingResponse(
        generate_scale_sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/scale/capture", summary="Read current weight from scale")
def scale_capture():
    data = read_scale()
    if not data["raw"]:
        return Response(content="No data from scale", status_code=503)
    return data
