import sys
import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, Response

app = FastAPI(title="RealSense Stream API")

# pyrealsense2 is Linux-only; use a stub on other platforms for local dev
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
    # --- Stub for local development (non-Linux) ---
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


# --- Frame generator ---

def generate_mjpeg():
    """Yield MJPEG frames for the streaming endpoint."""
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


# --- Endpoints ---

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
