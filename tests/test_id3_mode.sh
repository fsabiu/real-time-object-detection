#!/bin/bash
# Test ID3 Mode End-to-End
# This script runs the detector in ID3 mode and verifies metadata is embedded

set -e

# Ensure we are in the project root
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "========================================"
echo "ID3 Mode Test"
echo "========================================"

# Cleanup function
cleanup() {
    echo "Stopping processes..."
    kill $FFMPEG_PID 2>/dev/null || true
    kill $DETECTOR_PID 2>/dev/null || true
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
/home/ubuntu/miniforge/envs/drone_detector/bin/ffmpeg -ss 00:01:00 -stream_loop -1 -re -i cala_del_moral.ts -c copy -f rtsp rtsp://localhost:8554/klvstream > /dev/null 2>&1 &
FFMPEG_PID=$!
echo "FFmpeg started (PID: $FFMPEG_PID)"

# Wait for stream to be available
echo "Waiting for stream to be available..."
for i in {1..15}; do
    if /home/ubuntu/miniforge/envs/drone_detector/bin/ffprobe -v error -show_entries stream=codec_type rtsp://localhost:8554/klvstream > /dev/null 2>&1; then
        echo "✅ Stream ready!"
        break
    fi
    echo "  Attempt $i/15..."
    sleep 1
done

echo ""
echo "Step 2: Starting detector in ID3 mode (background)"
conda run -n drone_detector python3 -m src.main \
  --input-srt 'rtsp://localhost:8554/klvstream' \
  --output-rtsp 'rtsp://localhost:8554/detected_stream' \
  --model yolov8n.pt \
  --mode id3 \
  --id3-interval 10 \
  > detector_id3.log 2>&1 &
DETECTOR_PID=$!
echo "Detector started (PID: $DETECTOR_PID)"
echo "Waiting 15 seconds for pipeline to stabilize..."
sleep 15

echo ""
echo "Step 3: Reading ID3 metadata from stream (30 seconds)"
echo "----------------------------------------"
conda run -n drone_detector python3 test_id3_reader.py rtsp://localhost:8554/detected_stream 30

# Cleanup
echo ""
echo "Step 4: Cleanup"
kill $FFMPEG_PID 2>/dev/null || true
kill $DETECTOR_PID 2>/dev/null || true
wait $FFMPEG_PID 2>/dev/null || true
wait $DETECTOR_PID 2>/dev/null || true

echo ""
echo "✅ Test complete!"
echo "Check detector_id3.log for detector output"
