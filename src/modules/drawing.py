import cv2
import numpy as np

# Professional color palette for different object classes
CLASS_COLORS = {
    'person': (255, 150, 0),      # Orange
    'car': (0, 120, 255),          # Blue
    'truck': (255, 50, 50),        # Red
    'bus': (200, 0, 200),          # Purple
    'motorcycle': (255, 200, 0),   # Yellow
    'bicycle': (0, 200, 200),      # Cyan
    'airplane': (100, 200, 100),   # Light green
    'boat': (150, 150, 255),       # Light blue
    'default': (0, 255, 150)       # Teal (default)
}

def get_color_for_class(class_name: str) -> tuple:
    """Get appealing color for object class."""
    return CLASS_COLORS.get(class_name.lower(), CLASS_COLORS['default'])

def draw_detections_vectorized(img: np.ndarray, detections: list, thickness: int = 2) -> np.ndarray:
    """
    Ultra-fast vectorized detection drawing using NumPy + class-specific colors.
    """
    if not detections or len(detections) == 0:
        return img
    
    # Extract all bboxes into a single NumPy array (N, 4)
    bboxes: np.ndarray = np.array(
        [[int(d['bbox'][0]), int(d['bbox'][1]), int(d['bbox'][2]), int(d['bbox'][3])] 
         for d in detections],
        dtype=np.int32
    )
    
    # Validate and clip coordinates to image bounds
    h, w = img.shape[:2]
    bboxes[:, [0, 2]] = np.clip(bboxes[:, [0, 2]], 0, w - 1)
    bboxes[:, [1, 3]] = np.clip(bboxes[:, [1, 3]], 0, h - 1)
    
    # Create output image
    img_out: np.ndarray = img.copy()
    
    # Draw boxes with class-specific colors
    for idx, det in enumerate(detections):
        try:
            x1, y1, x2, y2 = bboxes[idx]
            
            # Get color for this class
            class_name = det.get('class_name', 'unknown')
            color = get_color_for_class(class_name)
            
            # Draw box with numpy slicing (fast)
            for i in range(thickness):
                # Top and bottom edges
                if y1 + i < h and x2 > x1:
                    img_out[y1 + i, x1:x2] = color
                if y2 - i >= 0 and x2 > x1:
                    img_out[y2 - i, x1:x2] = color
                # Left and right edges
                if x1 + i < w and y2 > y1:
                    img_out[y1:y2, x1 + i] = color
                if x2 - i >= 0 and y2 > y1:
                    img_out[y1:y2, x2 - i] = color
        except:
            pass
    
    return img_out

def overlay_metadata(frame, frame_count, klv_data, detections, fps):
    overlay = frame.copy()
    y_offset = 30
    line_height = 35
    cv2.rectangle(overlay, (5, 5), (400, 250), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.3, frame, 0.7, 0, frame)
    cv2.putText(frame, f'FPS: {fps:.1f}', (10, y_offset), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    y_offset += line_height
    cv2.putText(frame, f'Frame: {frame_count}', (10, y_offset), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    y_offset += line_height
    if klv_data:
        if 'latitude' in klv_data and 'longitude' in klv_data:
            cv2.putText(frame, f"GPS: {klv_data['latitude']:.6f}, {klv_data['longitude']:.6f}", (10, y_offset), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
            y_offset += line_height
        if 'altitude' in klv_data:
            cv2.putText(frame, f"Alt: {klv_data['altitude']:.1f}m", (10, y_offset), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
            y_offset += line_height
        if 'heading' in klv_data:
            cv2.putText(frame, f"Heading: {klv_data['heading']:.1f}°", (10, y_offset), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
            y_offset += line_height
    if detections:
        det_text = f"Detections: {len(detections)}"
        cv2.putText(frame, det_text, (10, y_offset), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        y_offset += line_height
        for det in detections[:2]:
            det_info = f"{det['class_name']}: {det['confidence']:.2f}"
            cv2.putText(frame, det_info, (20, y_offset), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            y_offset += line_height
    return frame
