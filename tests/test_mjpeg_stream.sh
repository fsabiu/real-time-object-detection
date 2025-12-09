#!/bin/bash

# Test MJPEG output with cala_del_moral.ts

cleanup() {
    echo "Stopping processes..."
    kill $FFMPEG_PID 2>/dev/null
    kill $DETECTOR_PID 2>/dev/null
    exit
}

trap cleanup SIGINT SIGTERM

cd "$(dirname "$0")/.."
PROJECT_ROOT=$(pwd)

echo "=========================================="
echo "MJPEG + SSE Test with cala_del_moral.ts"
echo "=========================================="

# Step 1: Stream TS file via ffmpeg to RTSP
echo ""
echo "Step 1: Starting FFmpeg stream (RTSP)"
ffmpeg \
    -nostdin \
    -re -stream_loop -1 \
    -i ../cala_del_moral.ts \
    -c copy \
    -f rtsp rtsp://127.0.0.1:8554/mjpegtest \
    > logs/ffmpeg_mjpeg_test.log 2>&1 &
FFMPEG_PID=$!
echo "FFmpeg started (PID: $FFMPEG_PID)"

# Wait for stream
echo "Waiting for RTSP stream..."
sleep 5

# Step 2: Run detector with MJPEG output
echo ""
echo "Step 2: Starting Detector (MJPEG Mode)"
python3 -m src.main \
  --input-srt 'rtsp://127.0.0.1:8554/mjpegtest' \
  --output-mjpeg 8080 \
  --model models/yolov8n.pt \
  --skip-frames 0 \
  --conf 0.25 \
  --mode auto \
  > logs/detector_mjpeg_test.log 2>&1 &
DETECTOR_PID=$!
echo "Detector started (PID: $DETECTOR_PID)"

echo ""
echo "=========================================="
echo "Demo Running!"
echo "Open: http://localhost:8080/"
echo "Press Ctrl+C to stop"
echo "=========================================="

wait
