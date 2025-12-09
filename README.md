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

### Basic Mode (Recommended)

Uses pure GStreamer pipeline (appsrc -> x264enc -> rtspclientsink):

```bash
python3 -m src.main \
  --input-srt 'srt://127.0.0.1:8890?mode=caller' \
  --output-rtsp 'rtsp://localhost:8554/detected_stream' \
  --model yolov8n.pt \
  --mode basic
```

### Testing with Recorded Stream

```bash
chmod +x test_detector.sh
./test_detector.sh
```

This script:
1. Streams `cala_del_moral.ts` to MediaMTX via ffmpeg
2. Runs the detector
3. Outputs detected stream to `rtsp://localhost:8554/detected_stream`

### Viewing the Output

**VLC (low latency):**
```bash
vlc --network-caching=100 rtsp://localhost:8554/detected_stream
```

**Browser (WebRTC):**
```
http://localhost:8889/detected_stream
```

## Performance

**Architecture**: 3-thread pipeline (Capture -> Inference -> Output)

**Typical Latency** (measured end-to-end):
- **Light load** (0-5 detections): 11-14ms
- **Medium load** (10-15 detections): 15-18ms  
- **Heavy load** (20-30 detections): 20-24ms
- **Max observed**: 33.4ms (rare spikes with 30+ objects)

**Breakdown**:
- Inference (YOLO): 7-17ms (scales with detection count)
- Drawing/Overlay: 1-3ms
- GStreamer Write: **1.4ms** (pure GStreamer, no IPC overhead)

**Target**: <33ms for 30 FPS (achieved ✅)

## Architecture

```
Input (SRT/RTSP)
    ↓
[Capture Thread] → Queue(2) → [Inference Thread] → Queue(2) → [Output Thread]
                                   (YOLO)                         (Draw + GStreamer)
                                                                         ↓
                                                                   RTSP Output
```

**Key Optimizations**:
1. **Leaky queues** (maxsize=2): Drop old frames instead of buffering
2. **Native GStreamer**: No subprocess/pickle overhead
3. **x264enc tune=zerolatency**: Minimal encoding delay
4. **Vectorized drawing**: NumPy operations for bounding boxes

## Troubleshooting

### "rtspclientsink not available"

If you see this error, the GStreamer plugin path isn't set correctly:

```bash
# Verify rtspclientsink exists
ls /usr/lib/x86_64-linux-gnu/gstreamer-1.0/libgstrtspclientsink.so

# Test it manually
conda run -n drone_detector python -c "
import os
os.environ['GST_PLUGIN_PATH'] = '/usr/lib/x86_64-linux-gnu/gstreamer-1.0'
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
print('rtspclientsink:', Gst.ElementFactory.make('rtspclientsink'))
"
```

### High Latency / Buffering

1. **Check inference time**: Look for log lines like `Frame X: Total=Yms | Inf=Zms`
2. **Reduce detection load**: Increase `--conf-threshold` (default 0.25)
3. **Lower VLC buffer**: Use `--network-caching=50` (50ms)

### Arguments
-   `--input-srt`: Input SRT URL or RTSP URL.
-   `--output-rtsp`: Output RTSP URL (for `rtsp` format) or Output Directory (for `hls` format).
-   `--output-format`: Output format: `rtsp` (default) or `hls`.
-   `--model`: Path to YOLO model.
-   `--conf`: Confidence threshold (default: 0.25).
-   `--device`: Device to run inference on (auto, cpu, 0, 1...).
-   `--mode`: Pipeline mode (`auto`, `id3`, `basic`). Use `basic` if pygobject is not installed.

### HLS Output Mode
To generate HLS segments directly (bypassing MediaMTX):
```bash
python3 -m src.main \
  --input-srt 'rtsp://localhost:8554/klvstream' \
  --output-rtsp '/path/to/hls_output_dir' \
  --output-format hls \
  --mode id3
```
This will create `index.m3u8` and `.ts` segments in the specified directory, with ID3 tags injected.

### WebRTC Output Mode (Recommended for Metadata)
WebRTC provides frame-synchronized metadata via data channels. This is the **recommended method** for real-time telemetry display:

```bash
python3 -m src.main \
  --input-srt 'srt://localhost:5001?mode=listener' \
  --output-webrtc 8080 \
  --model yolov8n.pt
```

This starts a WebRTC signaling server on port 8080. Open `tests/webrtc_player.html` in a browser to connect.

**Features:**
- Per-frame metadata delivery via data channel
- Low latency video streaming
- No external RTSP/HLS server required
- Built-in telemetry display in test page


## Architecture

The application is structured as a Python package:
-   `src/main.py`: Entry point.
-   `src/core/pipeline.py`: Threaded pipeline logic.
-   `src/modules/`: Helper modules (KLV, TAK, Geo, Drawing, SSE).
-   `src/outputs/`: Output writers (RTSP/GStreamer).

## Performance

The pipeline uses three dedicated threads:
1.  **Capture Thread**: Decodes video and KLV data.
2.  **Inference Thread**: Runs YOLO tracking.
3.  **Output Thread**: Draws overlays and writes to RTSP.

Queues between threads are size-limited to prevent latency buildup. If the pipeline cannot keep up, it will drop frames (preserving the most recent ones) rather than introducing lag.

## Troubleshooting

### pygobject Installation Issues
If you encounter issues installing `pygobject`, the ID3 mode won't be available. Use `--mode basic` instead, which works without pygobject.

### MediaMTX Not Running
Ensure MediaMTX is running to accept RTSP output:
```bash
mediamtx
```

---

## HLS ID3 Metadata Testing Summary

Multiple approaches were tested to embed per-frame metadata in HLS streams for synchronized playback in the browser. **All approaches failed** due to GStreamer `mpegtsmux` limitations.

### Approach 1: ID3v2 Tags with `application/x-id3` caps
- **Attempt**: Created `create_id3v2_frame()` to generate valid ID3v2.4 TXXX frames containing JSON metadata
- **Result**: ❌ `mpegtsmux` rejected the caps with "not-negotiated" errors
- **Reason**: GStreamer's `mpegtsmux` does not support `application/x-id3` or `private/x-timed_id3` sink pad capabilities

### Approach 2: KLV Metadata Stream with `meta/x-klv` caps
- **Attempt**: Changed `meta_appsrc` to use `meta/x-klv, parsed=(boolean)true` caps and pushed raw JSON
- **Result**: ⚠️ Partial. Stream created with `KLVA` header visible in segments, but JSON content not found by frontend parser
- **Reason**: The KLV stream appeared in `ffprobe` output, but `hls.js` cannot natively parse KLV data

### Approach 3: Custom Segment Fetcher (Frontend)
- **Attempt**: JavaScript polling of HLS playlist + fetching `.ts` segments directly + parsing embedded JSON
- **Result**: ❌ Segments fetched successfully but `parseMetadataFromSegment()` found no JSON
- **Diagnosis**: Even `strings` command on segments showed `KLVA` header but no JSON payload

### Conclusion
GStreamer's `mpegtsmux` (v1.20+) does not provide a reliable mechanism for injecting arbitrary metadata (ID3 or KLV) that can be extracted by browser-based players like `hls.js`. 

**Recommended Alternative**: WebRTC with data channels (see `--output-webrtc` option).
