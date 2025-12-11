#!/bin/bash
# Test script for detector with recorded stream

echo "Starting test with recorded stream..."
echo ""
echo "Step 1: Starting FFmpeg stream (Terminal 1)"
echo "This will stream cala_del_moral.ts to rtsp://localhost:8554/klvstream"
echo ""

# Start ffmpeg # Ensure we are in the project root
cd "$(dirname "$0")/.."

# Start ffmpeg in background (use conda environment ffmpeg)
/home/ubuntu/miniforge/envs/drone_detector/bin/ffmpeg -stream_loop -1 -re -i /home/ubuntu/drones/cala_del_moral.ts -c copy -f rtsp rtsp://localhost:8554/klvstream &
FFMPEG_PID=$!

echo "FFmpeg started (PID: $FFMPEG_PID)"
echo "Waiting 5 seconds for stream to stabilize..."
sleep 5

echo ""
echo "Step 2: Starting detector (Terminal 2)"
echo "This will run the detector on the stream"
echo ""

# Activate conda and run detector
cd /home/ubuntu/drones/detector
/home/ubuntu/miniforge/envs/drone_detector/bin/python -m src.main \
  --input-srt 'rtsp://localhost:8554/klvstream' \
  --output-rtsp 'rtsp://localhost:8554/detected_stream' \
  --model yolov8l.pt \
  --id3-interval 10 \
  --mode id3

# Cleanup on exit
echo ""
echo "Stopping FFmpeg..."
kill $FFMPEG_PID 2>/dev/null || true
