#!/usr/bin/env python3
"""
Standalone test for WebSocket output writer.
Tests frame + metadata streaming without needing SRT/RTSP input.
"""

import sys
import time
import cv2
import numpy as np
import logging

sys.path.insert(0, '/home/ubuntu/drones/detector')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def main():
    from src.outputs.websocket import WebSocketWriter, WEBSOCKET_AVAILABLE
    
    if not WEBSOCKET_AVAILABLE:
        print("ERROR: WebSocket not available. Install aiohttp.")
        sys.exit(1)
    
    print("=" * 60)
    print("WebSocket Standalone Test")
    print("=" * 60)
    
    # Create WebSocket writer
    try:
        writer = WebSocketWriter(port=8080, width=640, height=480, fps=30, quality=85)
        print(f"✓ WebSocket server started on port 8080")
    except Exception as e:
        print(f"✗ Failed to start WebSocket: {e}")
        sys.exit(1)
    
    print()
    print("Open in browser: http://localhost:8080/")
    print()
    print("Sending test frames with metadata...")
    print("Press Ctrl+C to stop")
    print()
    
    frame_count = 0
    try:
        while True:
            # Create test frame with moving circle
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            
            # Draw gradient background
            for y in range(480):
                frame[y, :, 0] = int(50 + (y / 480) * 50)  # Blue gradient
                frame[y, :, 1] = int(30 + (y / 480) * 30)  # Green gradient
            
            # Draw moving circle
            x = int(320 + 200 * np.sin(frame_count * 0.05))
            y = int(240 + 100 * np.cos(frame_count * 0.03))
            cv2.circle(frame, (x, y), 40, (0, 255, 0), -1)
            cv2.circle(frame, (x, y), 40, (255, 255, 255), 2)
            
            # Draw frame counter
            cv2.putText(frame, f"Frame: {frame_count}", (20, 40), 
                       cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.putText(frame, "WebSocket Test", (20, 80),
                       cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            
            # Create metadata
            metadata = {
                "frame": frame_count,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "telemetry": {
                    "lat": 37.7749 + 0.001 * np.sin(frame_count * 0.01),
                    "lon": -122.4194 + 0.001 * np.cos(frame_count * 0.01),
                    "alt": 100 + 10 * np.sin(frame_count * 0.02)
                },
                "detections": [
                    {
                        "class_name": "test_object",
                        "confidence": 0.95,
                        "track_id": 1,
                        "bbox": [x-40, y-40, x+40, y+40],
                        "geo_coordinates": {
                            "lat": 37.7749,
                            "lon": -122.4194
                        }
                    }
                ],
                "detection_count": 1
            }
            
            # Send frame (which internally sends metadata too if available)
            writer.write_frame(frame)
            writer.send_metadata(metadata)
            
            frame_count += 1
            
            # Status update every 30 frames
            if frame_count % 30 == 0:
                print(f"Frame {frame_count} | Clients: {len(writer.clients)}")
            
            time.sleep(1/30)  # 30 FPS
            
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        writer.close()
        print("Done.")

if __name__ == "__main__":
    main()
