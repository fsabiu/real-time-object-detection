#!/bin/bash

# Cleanup function
cleanup() {
    echo "Stopping processes..."
    kill $FFMPEG_PID 2>/dev/null
    kill $DETECTOR_PID 2>/dev/null
    kill $HTTP_PID 2>/dev/null
    exit
}

trap cleanup SIGINT SIGTERM

# Change to project root
cd "$(dirname "$0")/.."
PROJECT_ROOT=$(pwd)

echo "Starting Drone Detector HLS Demo"


echo "================================"

# Clean up previous HLS output
rm -rf tests/hls_output

echo "Step 1: Starting FFmpeg stream loop (background)"
# Stream cala_del_moral.ts in a loop to localhost:8554/klvstream
/home/ubuntu/miniforge/envs/drone_detector/bin/ffmpeg \
    -nostdin \
    -re -stream_loop -1 \
    -i ../cala_del_moral.ts \
    -c copy \
    -f rtsp rtsp://127.0.0.1:8554/klvstream \
    > ffmpeg.log 2>&1 &
FFMPEG_PID=$!
echo "FFmpeg started (PID: $FFMPEG_PID)"

# Wait for stream to be ready
echo "Waiting for stream..."
until /home/ubuntu/miniforge/envs/drone_detector/bin/ffprobe -v quiet -show_streams rtsp://127.0.0.1:8554/klvstream > /dev/null; do
    sleep 1
done

echo ""
echo "Step 2: Starting Detector (HLS Mode, No Overlay)"
# Run detector with:
# - HLS output format
# - No video overlay (clean video)
# - ID3 metadata injection
conda run -n drone_detector python3 -m src.main \
  --input-srt 'rtsp://127.0.0.1:8554/klvstream' \
  --output-rtsp 'tests/hls_output' \
  --output-format hls \
  --model yolov8n.pt \
  --mode id3 \
  --id3-interval 10 \
  --no-overlay \
  > detector_hls.log 2>&1 &
DETECTOR_PID=$!
echo "Detector started (PID: $DETECTOR_PID)"

echo ""
echo "Step 3: Starting HTTP Server"
cd tests
python3 -m http.server 8001 &
HTTP_PID=$!
echo "HTTP Server started at http://localhost:8001"

echo ""
echo "=================================================="
echo "Demo Running!"
echo "Open http://localhost:8001/hls_player.html"
echo "Press Ctrl+C to stop"
echo "=================================================="

wait
