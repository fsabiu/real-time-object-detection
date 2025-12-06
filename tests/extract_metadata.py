import json
import base64
import sys
import binascii

def extract_metadata():
    try:
        with open('captured_data/packets.json', 'r') as f:
            data = json.load(f)
        
        packets = data.get('packets', [])
        print(f"Found {len(packets)} packets")
        
        found_metadata = []
        
        for p in packets:
            # Check for data in 'data' field (hex encoded) or tags
            if 'data' in p:
                try:
                    # Decode hex
                    raw_data = binascii.unhexlify(p['data'])
                    # Try to decode as utf-8
                    text = raw_data.decode('utf-8', errors='ignore')
                    
                    # Look for JSON
                    if '{' in text and '}' in text:
                        start = text.find('{')
                        end = text.rfind('}') + 1
                        json_str = text[start:end]
                        try:
                            meta = json.loads(json_str)
                            if 'detection_count' in meta:
                                found_metadata.append(meta)
                        except:
                            pass
                except:
                    pass
            
            # Also check side_data or tags if present
            if 'side_data_list' in p:
                for sd in p['side_data_list']:
                    if 'data' in sd:
                         # similar decoding...
                         pass

        print(f"Extracted {len(found_metadata)} metadata objects")
        
        if found_metadata:
            # Save the first one with detections
            for meta in found_metadata:
                if meta.get('detection_count', 0) > 0:
                    with open('captured_data/metadata.json', 'w') as f:
                        json.dump(meta, f, indent=2)
                    print("Saved captured_data/metadata.json")
                    return
            
            # If no detections, save the first one
            with open('captured_data/metadata.json', 'w') as f:
                json.dump(found_metadata[0], f, indent=2)
            print("Saved captured_data/metadata.json (no detections)")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    extract_metadata()
