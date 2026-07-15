# Project Scope: RealSense Stream API

## Overview

An API server running on a Jetson Ubuntu VM to expose an Intel RealSense camera over HTTP — enabling web and mobile clients on the same LAN to stream live video and capture images without needing GUI access.

## Architecture

- **API server:** Runs directly on the Jetson Ubuntu VM (direct RealSense hardware access)
- **Stack:** Python + FastAPI
- **SDK:** pyrealsense2
- **Network:** Local network (LAN) only
- **Clients:** Web browser frontend, mobile app

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/stream` | GET | Live MJPEG stream of the RealSense RGB color feed |
| `/capture` | GET | Trigger a snapshot and return the image in the response body |
| `/scale/stream` | GET | Live weight stream (SSE) |
| `/scale/capture` | GET | Read current weight |
| `/health` | GET | Per-device status + last error |
| `/system/stats` | GET | CPU / memory / disk / temperature / uptime |
| `/camera/restart` | POST | Force a RealSense pipeline restart |
| `/scale/reconnect` | POST | Force the scale serial port to reopen |
| `/system/reboot` | POST | Reboot the Jetson (admin-token guarded) |
| `/system/restart-service` | POST | Restart the stream-api service (admin-token guarded) |

## Stream Details

- **Data:** RGB color only
- **Protocol:** MJPEG over HTTP (renders natively in browser `<img>` tags and mobile)
- **Capture format:** JPEG, returned directly in the HTTP response

## Future Considerations

Items intentionally deferred — revisit if requirements evolve:

- **Authentication** — token or API key to restrict stream/capture access
- **Depth stream** — expose RealSense depth or IR data as an additional endpoint
- **Frame rate / resolution** — configurable via query params (e.g. `?fps=30&width=1280`)
- **Concurrency** — explicit multi-client stream management
- **Image format** — PNG option for lossless captures
- **Cloud storage** — option to persist captured images to S3/GCS and return a URL
- **WebRTC / WebSocket** — lower-latency streaming protocol if MJPEG proves insufficient
