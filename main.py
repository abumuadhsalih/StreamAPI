import sys
import json
import time
import cv2
import numpy as np
import serial
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, Response

app = FastAPI(title="RealSense Stream API")

# ---------------------------------------------------------------------------
# Camera — pyrealsense2 is Linux-only; stub on other platforms for local dev
# ---------------------------------------------------------------------------

if sys.platform == "linux":
    import pyrealsense2 as rs

    pipeline = rs.pipeline()
    config = rs.config()
    config.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)

    def get_color_frame():
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
    def get_color_frame():
        """Return a placeholder frame when no RealSense is available."""
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(
            frame,
            "RealSense not available (stub)",
            (60, 240),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 255, 255),
            2,
        )
        return frame


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
# Camera generators
# ---------------------------------------------------------------------------

def generate_mjpeg():
    """Yield MJPEG frames for the camera stream endpoint."""
    while True:
        image = get_color_frame()
        if image is None:
            continue
        _, jpeg = cv2.imencode(".jpg", image)
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + jpeg.tobytes()
            + b"\r\n"
        )


# ---------------------------------------------------------------------------
# Scale generator
# ---------------------------------------------------------------------------

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
def stream():
    return StreamingResponse(
        generate_mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/capture", summary="Capture a single JPEG image")
def capture():
    image = get_color_frame()
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
