#!/bin/bash
# Simple test script for WebRTC output
# Uses a video file directly instead of SRT/RTSP

set -e

cd /home/ubuntu/drones/detector

# Activate conda
source ~/miniforge/bin/activate drone_detector

echo "Testing WebRTC output with video file..."

# Start HTTP server for frontend in background
python3 -m http.server 8001 --directory . &
HTTP_PID=$!
echo "HTTP server started on port 8001 (PID: $HTTP_PID)"

# Use ffmpeg to convert video file to RTSP on MediaMTX
echo "Starting video stream to MediaMTX..."
ffmpeg -re -stream_loop 0 -i /home/ubuntu/drones/sample_video/DJI_0088.MP4 \
  -c:v libx264 -preset ultrafast -tune zerolatency -g 30 \
  -f rtsp -rtsp_transport tcp rtsp://localhost:8554/teststream > /tmp/ffmpeg_test.log 2>&1 &
FFMPEG_PID=$!
echo "FFmpeg started (PID: $FFMPEG_PID)"

sleep 5

# Check if stream is available
echo "Checking stream availability..."
if ffprobe -v error -timeout 3000000 rtsp://localhost:8554/teststream 2>/dev/null; then
    echo "Stream is available!"
else
    echo "Stream not available. Checking ffmpeg log:"
    cat /tmp/ffmpeg_test.log
    kill $HTTP_PID 2>/dev/null || true
    exit 1
fi

# Run detector with WebRTC output
echo ""
echo "Starting detector with WebRTC output on port 8080..."
echo "Open http://localhost:8001/tests/webrtc_player.html in browser to test"
echo ""

python3 -m src.main \
  --input-srt "rtsp://localhost:8554/teststream" \
  --output-webrtc 8080 \
  --model yolov8n.pt \
  --conf 0.4

# Cleanup
kill $FFMPEG_PID 2>/dev/null || true
kill $HTTP_PID 2>/dev/null || true
