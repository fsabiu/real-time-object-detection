"""
Batch video processing output - saves annotated video + JSON metadata.
"""

import cv2
import json
import logging
import os
from typing import Dict, Any, Optional
import numpy as np

logger = logging.getLogger("SRTYOLOUnified.Batch")


class BatchVideoWriter:
    """
    Batch processing writer that saves:
    - Annotated video file (MP4) - named after input file
    - Complete JSON metadata for all frames - named after input file
    """

    def __init__(self, output_dir: str, width: int, height: int, fps: float, input_filename: Optional[str] = None):
        self.output_dir = output_dir
        self.width = width
        self.height = height
        self.fps = fps
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
        
        # Derive output filenames from input filename
        if input_filename:
            base_name = os.path.splitext(os.path.basename(input_filename))[0]
        else:
            base_name = "output"
        
        self.video_filename = f"{base_name}.mp4"
        self.json_filename = f"{base_name}.json"
        
        # Video writer
        video_path = os.path.join(output_dir, self.video_filename)
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        
        # IMPORTANT: Use exact FPS from input to prevent speed-up/slow-down
        self.video_writer = cv2.VideoWriter(video_path, fourcc, fps, (width, height))
        
        if not self.video_writer.isOpened():
            raise RuntimeError(f"Failed to create video writer: {video_path}")
        
        # Metadata storage
        self.metadata = {
            "video_info": {
                "width": width,
                "height": height,
                "fps": fps,
                "output_file": self.video_filename,
                "input_file": input_filename or "unknown"
            },
            "frames": []
        }
        
        logger.info(f"Batch writer initialized:")
        logger.info(f"  Video: {output_dir}/{self.video_filename} ({width}x{height} @ {fps} fps)")
        logger.info(f"  JSON:  {output_dir}/{self.json_filename}")

    def write_frame(self, frame: np.ndarray):
        """Write annotated frame to video file."""
        if frame.shape[0] != self.height or frame.shape[1] != self.width:
            frame = cv2.resize(frame, (self.width, self.height))
        
        self.video_writer.write(frame)

    def inject_metadata(self, metadata: Dict[str, Any]):
        """Store metadata for this frame."""
        self.metadata["frames"].append(metadata)

    def close(self):
        """Finalize video and write JSON metadata."""
        # Release video writer
        self.video_writer.release()
        logger.info(f"Video saved: {self.output_dir}/{self.video_filename} ({len(self.metadata['frames'])} frames)")
        
        # Write JSON metadata
        json_path = os.path.join(self.output_dir, self.json_filename)
        with open(json_path, 'w') as f:
            json.dump(self.metadata, f, indent=2, default=str)
        
        logger.info(f"Metadata saved: {json_path}")
        logger.info(f"Batch processing complete!")
