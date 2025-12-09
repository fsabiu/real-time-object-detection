#!/usr/bin/env python3
"""
Standalone test for MJPEG + SSE output.
"""
import sys
import time
import cv2
import numpy as np
import logging

sys.path.insert(0, '/home/ubuntu/drones/detector')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(message)s')

def main():
    from src.outputs.mjpeg import MJPEGWriter, MJPEG_AVAILABLE
    
    if not MJPEG_AVAILABLE:
        print("ERROR: aiohttp not available")
        sys.exit(1)
    
    print("=" * 60)
    print("MJPEG + SSE Test")
    print("=" * 60)
    
    writer = MJPEGWriter(port=8080, width=640, height=480, fps=30, quality=85)
    print("✓ Server started on port 8080")
    print("\nOpen: http://localhost:8080/\n")
    
    frame_count = 0
    try:
        while True:
            # Create test frame
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            for y in range(480):
                frame[y, :, 0] = int(50 + (y / 480) * 50)
                frame[y, :, 1] = int(30 + (y / 480) * 30)
            
            x = int(320 + 200 * np.sin(frame_count * 0.05))
            y = int(240 + 100 * np.cos(frame_count * 0.03))
            cv2.circle(frame, (x, y), 40, (0, 255, 0), -1)
            cv2.putText(frame, f"Frame: {frame_count}", (20, 40), 
                       cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            
            # Send frame
            writer.write_frame(frame)
            
            # Send metadata
            metadata = {
                "frame": frame_count,
                "detections": [{
                    "class_name": "test",
                    "confidence": 0.95,
                    "bbox": [x-40, y-40, x+40, y+40]
                }],
                "detection_count": 1
            }
            writer.send_metadata(metadata)
            
            frame_count += 1
            if frame_count % 30 == 0:
                print(f"Frame {frame_count}")
            
            time.sleep(1/30)
            
    except KeyboardInterrupt:
        print("\nStopping...")
        writer.close()

if __name__ == "__main__":
    main()
