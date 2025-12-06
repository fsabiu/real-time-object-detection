#!/bin/bash
# Run HLS Frontend Test
# Starts Stream, Detector, MediaMTX, and HTTP Server

set -e

# Ensure we are in the project root
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "========================================"
echo "HLS Frontend Test"
echo "========================================"

# Cleanup function
cleanup() {
    echo ""
    echo "Stopping processes..."
    kill $FFMPEG_PID 2>/dev/null || true
    kill $DETECTOR_PID 2>/dev/null || true
    kill $HTTP_PID 2>/dev/null || true
    exit
}
trap cleanup SIGINT SIGTERM

# Check if MediaMTX is running
if ! docker ps | grep -q mediamtx; then
    echo "Starting MediaMTX..."
    docker run --rm -d --network=host bluenviron/mediamtx:latest
    sleep 3
fi

echo ""
echo "Step 1: Starting FFmpeg stream (background)"
/home/ubuntu/miniforge/envs/drone_detector/bin/ffmpeg -ss 00:01:00 -stream_loop -1 -re -i ../cala_del_moral.ts -c copy -f rtsp rtsp://localhost:8554/klvstream > ffmpeg.log 2>&1 &
FFMPEG_PID=$!
echo "FFmpeg started (PID: $FFMPEG_PID)"

# Wait for stream to be available
echo "Waiting for stream to be available..."
for i in {1..30}; do
    if /home/ubuntu/miniforge/envs/drone_detector/bin/ffprobe -v error -show_entries stream=codec_type rtsp://localhost:8554/klvstream > /dev/null 2>&1; then
        echo "✅ Stream ready!"
        break
    fi
    echo "  Attempt $i/30..."
    sleep 1
done

# Clean up previous HLS output
rm -rf tests/hls_output

echo ""
echo "Step 2: Starting detector in HLS mode (background)"
conda run -n drone_detector python3 -m src.main \
  --input-srt 'rtsp://localhost:8554/klvstream' \
  --output-rtsp 'tests/hls_output' \
  --output-format hls \
  --model yolov8n.pt \
  --mode id3 \
  --id3-interval 10 \
  > detector_hls.log 2>&1 &
DETECTOR_PID=$!
echo "Detector started (PID: $DETECTOR_PID)"

echo ""
echo "Step 3: Starting HTTP Server"
cd tests
python3 -m http.server 8001 > /dev/null 2>&1 &
HTTP_PID=$!
echo "HTTP Server started (PID: $HTTP_PID)"

echo ""
echo "========================================"
echo "✅ Test Environment Ready!"
echo "Open the following URL in your browser:"
echo "http://localhost:8001/hls_player.html"
echo "========================================"
echo "Press Ctrl+C to stop"

wait
