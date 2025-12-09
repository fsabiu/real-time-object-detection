#!/bin/bash
# Test RTSP output streaming with file input

cleanup() {
    echo "Stopping processes..."
    kill $FFMPEG_PID 2>/dev/null
    kill $DETECTOR_PID 2>/dev/null
    exit
}

trap cleanup SIGINT SIGTERM

cd "$(dirname "$0")/.."

echo "=========================================="
echo "RTSP Streaming Test"
echo "=========================================="

# Step 1: Stream TS file to RTSP
echo ""
echo "Step 1: Streaming cala_del_moral.ts to RTSP"
ffmpeg \
    -nostdin \
    -re -stream_loop -1 \
    -i ../cala_del_moral.ts \
    -c copy \
    -f rtsp rtsp://127.0.0.1:8554/source \
    > logs/ffmpeg_rtsp_test.log 2>&1 &
FFMPEG_PID=$!
echo "FFmpeg started (PID: $FFMPEG_PID)"

sleep 5

# Step 2: Run detector with RTSP output
echo ""
echo "Step 2: Running detector (RTSP → RTSP)"
python3 -m src.main \
  --input-srt rtsp://127.0.0.1:8554/source \
  --output-rtsp rtsp://127.0.0.1:8554/detected \
  --model models/yolov8n.pt \
  --conf 0.25 \
  > logs/detector_rtsp_test.log 2>&1 &
DETECTOR_PID=$!
echo "Detector started (PID: $DETECTOR_PID)"

echo ""
echo "=========================================="
echo "RTSP Streaming Running!"
echo "Input:  rtsp://127.0.0.1:8554/source"
echo "Output: rtsp://127.0.0.1:8554/detected"
echo ""
echo "View with: ffplay rtsp://127.0.0.1:8554/detected"
echo "Press Ctrl+C to stop"
echo "=========================================="

wait
