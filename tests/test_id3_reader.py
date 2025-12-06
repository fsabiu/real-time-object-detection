import av
import sys
import json
import os
from PIL import Image
import numpy as np
from datetime import datetime

def read_id3_from_stream(rtsp_url, duration_seconds=30, output_dir="captured_data"):
    """
    Read RTSP stream and extract ID3 metadata.
    Saves frame + metadata JSON when detections are found.
    
    Args:
        rtsp_url: RTSP URL to read from
        duration_seconds: How long to listen (seconds)
        output_dir: Directory to save captured frames and metadata
    """
    print(f"Opening RTSP stream: {rtsp_url}")
    print(f"Listening for {duration_seconds} seconds...")
    print(f"Saving samples with detections to: {output_dir}")
    print("-" * 80)
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    try:
        container = av.open(rtsp_url, options={'rtsp_transport': 'tcp'})
        
        start_time = None
        id3_count = 0
        frame_count = 0
        saved_count = 0
        last_frame = None
        
        for packet in container.demux():
            if start_time is None:
                start_time = datetime.now()
            
            # Check if we've exceeded duration
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed > duration_seconds:
                print(f"\n✅ Test complete after {duration_seconds} seconds")
                break
            
            # Handle Video Packets
            if packet.stream.type == 'video':
                for frame in packet.decode():
                    # Convert to numpy array (RGB for PIL)
                    img = frame.to_ndarray(format='rgb24')
                    last_frame = img
                    frame_count += 1
                    if frame_count % 30 == 0:
                        print(f"[{elapsed:.1f}s] Processed {frame_count} video frames, {id3_count} ID3 tags")
            
            # Handle Data Packets (ID3 Metadata)
            elif packet.stream.type == 'data':
                id3_count += 1
                data = bytes(packet)
                
                try:
                    # Try to decode as text/JSON
                    text = data.decode('utf-8', errors='ignore')
                    
                    # Look for JSON patterns
                    if '{' in text:
                        start = text.find('{')
                        end = text.rfind('}') + 1
                        if start >= 0 and end > start:
                            json_str = text[start:end]
                            try:
                                metadata = json.loads(json_str)
                                detection_count = metadata.get('detection_count', 0)
                                
                                # Only save if we have detections and a valid frame
                                if detection_count > 0 and last_frame is not None:
                                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                                    base_name = f"{output_dir}/detection_{timestamp}"
                                    
                                    # Save Image using PIL
                                    try:
                                        im = Image.fromarray(last_frame)
                                        im.save(f"{base_name}.jpg")
                                        
                                        # Save Metadata
                                        with open(f"{base_name}.json", 'w') as f:
                                            json.dump(metadata, f, indent=2)
                                            
                                        print(f"\n📸 SAVED detection sample: {base_name}")
                                        print(f"   - Frame: {metadata.get('frame')}")
                                        print(f"   - Detections: {detection_count}")
                                        saved_count += 1
                                    except Exception as e:
                                        print(f"Error saving image: {e}")
                                    
                            except json.JSONDecodeError:
                                pass
                except Exception as e:
                    pass
        
        print("\n" + "="*80)
        print("SUMMARY")
        print("="*80)
        print(f"Video frames: {frame_count}")
        print(f"ID3 tags found: {id3_count}")
        print(f"Saved samples: {saved_count}")
        
        if id3_count == 0:
            print("\n❌ No ID3 metadata found!")
        else:
            print(f"\n✅ ID3 metadata is working! ({id3_count} tags received)")
            if saved_count > 0:
                print(f"✅ Saved {saved_count} samples with detections to '{output_dir}'")
        
    except Exception as e:
        print(f"\n❌ Error reading stream: {e}")
        return False
    
    return id3_count > 0

if __name__ == '__main__':
    rtsp_url = 'rtsp://localhost:8554/detected_stream'
    if len(sys.argv) > 1:
        rtsp_url = sys.argv[1]
    
    duration = 30
    if len(sys.argv) > 2:
        duration = int(sys.argv[2])
    
    success = read_id3_from_stream(rtsp_url, duration)
    sys.exit(0 if success else 1)
