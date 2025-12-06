# ID3 Mode Testing Guide

## Quick Test (Automated)

Run the automated test script:
```bash
./test_id3_mode.sh
```

This will:
1. Start FFmpeg streaming `cala_del_moral.ts`
2. Start detector in ID3 mode
3. Read and display ID3 metadata for 30 seconds
4. Clean up

## Manual Testing

### Step 1: Start MediaMTX
```bash
docker run --rm -d --network=host bluenviron/mediamtx:latest
```

### Step 2: Start Source Stream
```bash
ffmpeg -re -stream_loop -1 \
  -i /home/ubuntu/drones/cala_del_moral.ts \
  -c copy \
  -f rtsp \
  rtsp://localhost:8554/klvstream
```

### Step 3: Start Detector (ID3 Mode)
```bash
conda activate drone_detector
python3 -m src.main \
  --input-srt 'rtsp://localhost:8554/klvstream' \
  --output-rtsp 'rtsp://localhost:8554/detected_stream' \
  --model yolov8n.pt \
  --mode id3 \
  --id3-interval 10
```

**Parameters**:
- `--mode id3`: Enable ID3 metadata injection
- `--id3-interval 10`: Inject metadata every 10 frames (~3x per second at 30 FPS)

### Step 4: Read ID3 Metadata
```bash
python3 test_id3_reader.py rtsp://localhost:8554/detected_stream 30
```

## What to Expect

**Successful Output:**
```
🎯 ID3 Tag #1 at 3.45s
================================================================================
{
  "frame": 105,
  "timestamp": "2025-12-03T22:15:30.123456",
  "telemetry": {
    "latitude": 38.123,
    "longitude": -122.456,
    ...
  },
  "detections": [
    {
      "bbox": [120, 340, 180, 420],
      "class_name": "person",
      "confidence": 0.87,
      "track_id": 5,
      "geo_coordinates": {
        "latitude": 38.124,
        "longitude": -122.457
      }
    }
  ],
  "detection_count": 1
}
================================================================================
```

**Failed Output:**
```
❌ No ID3 metadata found!
```

Possible reasons:
1. Detector not in ID3 mode
2. No objects detected (metadata only sent when detections occur)
3. ID3 interval too long

## Comparing Basic vs ID3 Mode

### Basic Mode
- Lower latency (~1ms less)
- No metadata in stream
- Metadata available via UDP/SSE only (unsynchronized)

### ID3 Mode  
- Metadata **embedded in video stream** (perfect sync)
- Slightly higher latency (~1ms overhead)
- Standard MPEG-TS format
- Works with any MPEG-TS client

## Troubleshooting

### "Server returned 404 Not Found"
- Wait longer for stream to start (15+ seconds)
- Check if FFmpeg is running: `ps aux | grep ffmpeg`
- Verify MediaMTX: `docker ps | grep mediamtx`

### "No ID3 metadata found"
- Ensure detector is in ID3 mode (`--mode id3`)
- Check if detections are occurring (need objects in frame)
- Try shorter interval: `--id3-interval 1`

### Stream works but no detections
- Check if objects are visible in source video
- Lower confidence threshold: `--conf-threshold 0.1`
- Verify YOLO model loaded: Check logs for "Loading YOLO model"
