# Detector Module

This module provides a unified SRT → YOLO → RTSP/HLS pipeline with optional ID3v2 injection and SSE/UDP metadata. It is designed for high-performance real-time object detection on drone video streams.

## Features

-   **High Performance**: Multi-threaded architecture (Capture, Inference, Output) to ensure low latency (<30ms processing time).
-   **Modular Design**: Clean separation of concerns (KLV decoding, TAK integration, Geolocation, etc.).
-   **YOLO Integration**: Uses Ultralytics YOLOv8 for object detection and tracking.
-   **Metadata**:
    -   Decodes MISB 0601 KLV metadata from SRT stream.
    -   Calculates geographic coordinates of detected objects using photogrammetry.
    -   Injects metadata into RTSP output (ID3 tags).
    -   Broadcasts metadata via UDP and SSE.
-   **TAK Server**: Optional integration to send Cursor on Target (CoT) messages to TAK Server.

## Installation

### 1. System Dependencies

Install required system packages:
```bash
sudo apt-get update
sudo apt-get install -y \
    libcairo2-dev \
    pkg-config \
    python3-dev \
### 1. Install System Dependencies

The detector uses a **hybrid architecture**:
- **Main App**: Runs in Conda (Python 3.10) with `opencv-python` for image processing
- **GStreamer**: Uses native `pygobject` bindings + system GStreamer plugins for ultra-low-latency RTSP output

Install required system packages:
```bash
sudo apt-get update
sudo apt-get install -y \
    python3-opencv \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-rtsp \
    libva-dev \
    libva-drm2
```

**Why these packages?**
- `python3-opencv`: Provides system OpenCV (not used directly, but validates GStreamer setup)
- `gstreamer1.0-plugins-*`: Codec support (x264, parsers, filters)
- `gstreamer1.0-rtsp`: Provides `rtspclientsink` element for RTSP output
- `libva-*`: Video acceleration libraries

### 2. Create Conda Environment

```bash
conda create -n drone_detector python=3.10 -y
conda activate drone_detector
```

### 3. Install Python Dependencies

**Install GStreamer bindings (critical for performance):**
```bash
conda install -c conda-forge pygobject -y
```

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**What's in requirements.txt?**
- `ultralytics`: YOLOv8 object detection
- `opencv-python`: Image processing and frame manipulation
- `av`: PyAV for SRT/RTSP stream input
- `numpy`: Array operations
- `lapx`: Required for YOLO tracking (ByteTrack)
- `tensorrt`, `onnx`, `onnxsim`: (Optional) For TensorRT optimization

**Note**: The detector automatically sets `GST_PLUGIN_PATH` to use system GStreamer plugins, specifically `rtspclientsink` from `/usr/lib/x86_64-linux-gnu/gstreamer-1.0`.

### 4. Download YOLO Model

```bash
chmod +x setup_yolo.sh
./setup_yolo.sh
```

This downloads `yolov8n.pt` (11MB, ~10ms inference on GPU).

### 5. Start MediaMTX (RTSP Server)

```bash
docker run --rm -d --network=host bluenviron/mediamtx:latest
```

MediaMTX listens on:
- RTSP: `rtsp://localhost:8554/`
- WebRTC: `http://localhost:8889/`

## Usage

The detector follows a simple pipeline: `Input → Detector → Output(s)`.

### 1. Inputs (`--input-srt`)

The `--input-srt` argument accepts two types of sources:

| Input Type | Description | Example |
| :--- | :--- | :--- |
| **Video File** | Use a local file path. The detector reads it directly (fastest). | `--input-srt ../video.ts` |
| **Network Stream** | Any URL supported by PyAV/FFmpeg (RTSP, SRT, UDP, etc.). | `--input-srt srt://127.0.0.1:9000`<br>`--input-srt rtsp://camera:554/live` |

### 2. Output Modes

You can enable one or multiple outputs simultaneously.

#### A. RTSP Streaming (Default)
Streams the annotated video to an external RTSP server.
- **Backend (Run)**:
  ```bash
  python3 -m src.main --input-srt ../cala_del_moral.ts --output-rtsp rtsp://localhost:8554/detected --mode basic
  ```
- **Frontend (View)**:
  ```bash
  ffplay rtsp://localhost:8554/detected
  # or use the test script:
  ./tests/test_rtsp_streaming.sh
  ```

#### B. Batch Processing (File Output)
Processes the video and saves results to a folder.
- **Backend (Run)**:
  ```bash
  python3 -m src.main --input-srt ../cala_del_moral.ts --batch-output ./batch_output
  ```
- **Frontend (View)**:
  ```bash
  # Results will be in the output folder
  ls -lh ./batch_output/
  # Or run verification script:
  ./tests/test_batch_processing.sh
  ```

#### C. WebRTC (Browser Streaming)
Streams video + metadata directly to a browser.
- **Backend (Run)**:
  ```bash
  python3 -m src.main --input-srt ../cala_del_moral.ts --output-webrtc 8080
  ```
- **Frontend (View)**:
  Open [tests/webrtc_player.html](file://home/ubuntu/drones/detector/tests/webrtc_player.html) in your browser.

#### D. MJPEG (Browser Fallback)
Simple HTTP multi-part stream.
- **Backend (Run)**:
  ```bash
  python3 -m src.main --input-srt ../cala_del_moral.ts --output-mjpeg 8081
  ```
- **Frontend (View)**:
  Open [tests/mjpeg_player.html](file://home/ubuntu/drones/detector/tests/mjpeg_player.html) in your browser.

#### E. HLS (HTTP Live Streaming)
Generates static HLS segments.
- **Backend (Run)**:
  ```bash
  python3 -m src.main --input-srt ../cala_del_moral.ts --output-rtsp ./hls_output --output-format hls
  ```
- **Frontend (View)**:
  Open [tests/hls_player.html](file://home/ubuntu/drones/detector/tests/hls_player.html) in your browser.

---

### 3. Remote Access & Port Forwarding

If you are running the detector on a **remote server** (e.g., via SSH) and want to view the results on your local machine:

#### A. Built-in Web Servers (WebRTC & MJPEG)
These modes already serve their player pages on the specified port.
1. **Run Backend**: `python3 -m src.main ... --output-webrtc 8080`
2. **Access**: Navigate to `http://<server-ip>:8080/`

#### B. Serving Static Files (HLS & General Tests)
For HLS or to browse the `tests/` folder, use Python's built-in HTTP server:
```bash
# To serve the tests folder on port 9000
python3 -m http.server 9000 --directory tests/
```
Then navigate to `http://<server-ip>:9000/`.
---

## Quick Reference Table

| Mode | Argument | Sample Player / Tool |
| :--- | :--- | :--- |
| **RTSP** | `--output-rtsp <url>` | `ffplay` / VLC / `tests/test_rtsp_streaming.sh` |
| **Batch** | `--batch-output <dir>` | System Video Player / `tests/test_batch_processing.sh` |
| **WebRTC** | `--output-webrtc <port>` | `tests/webrtc_player.html` |
| **MJPEG** | `--output-mjpeg <port>` | `tests/mjpeg_player.html` |
| **HLS** | `--output-format hls` | `tests/hls_player.html` |

---

## Architecture details

The application is structured as a Python package:
-   `src/main.py`: Entry point.
-   `src/core/pipeline.py`: Threaded pipeline logic.
-   `src/modules/`: Helper modules (KLV, TAK, Geo, Drawing, SSE).
-   `src/outputs/`: Output writers (RTSP/GStreamer, Batch, WebRTC, MJPEG).

## Performance

The pipeline uses three dedicated threads:
1.  **Capture Thread**: Decodes video and KLV data.
2.  **Inference Thread**: Runs YOLO tracking.
3.  **Output Thread**: Draws overlays and writes to selected output(s).

## Troubleshooting

### "rtspclientsink not available"
If you see this error, the GStreamer plugin path isn't set correctly. Ensure you installed the system dependencies.

### Arguments
full list:
-   `--input-srt`: Input source (File path or URL).
-   `--output-rtsp`: RTSP Destination URL.
-   `--batch-output`: Directory for batch file output.
-   `--output-webrtc`: Port for WebRTC server.
-   `--output-mjpeg`: Port for MJPEG server.
-   `--model`: Path to YOLO model.
-   `--conf`: Confidence threshold (default: 0.25).
-   `--mode`: `basic` (recommended for RTSP), `id3` (experimental).

