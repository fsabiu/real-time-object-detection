#!/usr/bin/env python3
"""
Standalone test for WebRTC output writer.
Tests the signaling server and video/data channel without needing SRT/RTSP input.
"""

import sys
import time
import cv2
import numpy as np
import threading

sys.path.insert(0, '/home/ubuntu/drones/detector')



def main():
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    # Enable aiortc debug logging
    logging.getLogger('aiortc').setLevel(logging.WARNING)  # Too verbose otherwise
    logging.getLogger('aioice').setLevel(logging.WARNING)
    
    from src.outputs.webrtc import WebRTCWriter, WEBRTC_AVAILABLE
    
    if not WEBRTC_AVAILABLE:
        print("ERROR: WebRTC not available. Install aiortc and aiohttp.")
        sys.exit(1)
    
    print("=" * 60)
    print("WebRTC Standalone Test")
    print("=" * 60)
    
    # Create WebRTC writer
    try:
        writer = WebRTCWriter(port=8080, width=640, height=480, fps=30)
        print(f"✓ WebRTC signaling server started on port 8080")
    except Exception as e:
        print(f"✗ Failed to start WebRTC: {e}")
        sys.exit(1)
    
    # HTTP server is now handled by WebRTCWriter on port 8080
    print(f"✓ WebRTC server (signaling + http) started on port 8080")
    
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
            cv2.putText(frame, "WebRTC Test", (20, 80),
                       cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            
            # Send frame
            writer.write_frame(frame)
            
            # Create and send metadata
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
            writer.send_metadata(metadata)
            
            frame_count += 1
            
            # Status update every 30 frames
            if frame_count % 30 == 0:
                print(f"Frame {frame_count} | Connections: {len(writer.pcs)} | Data channels: {len(writer.data_channels)}")
            
            time.sleep(1/30)  # 30 FPS
            
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        writer.close()
        print("Done.")

if __name__ == "__main__":
    main()
