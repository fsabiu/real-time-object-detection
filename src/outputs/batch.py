"""
Batch video processing output - saves annotated video + JSON metadata.
"""

import cv2
import json
import logging
import os
import subprocess
from typing import Dict, Any, Optional
import numpy as np

logger = logging.getLogger("SRTYOLOUnified.Batch")


import time

class BatchVideoWriter:
    """
    Batch processing writer that saves:
    - Annotated video file (MP4) - named after input file
    - Complete JSON metadata for all frames - named after input file
    """

    def __init__(self, output_dir: str, width: int, height: int, fps: float, input_filename: Optional[str] = None):
        self.start_time = time.time()
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
        
        video_path = os.path.join(output_dir, self.video_filename)
        
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
        logger.info(f"  Video: {video_path} ({width}x{height} @ {fps} fps)")
        logger.info(f"  JSON:  {os.path.join(output_dir, self.json_filename)}")
        
        # Initialize FFmpeg process for on-the-fly compression
        # This replaces cv2.VideoWriter which produces large files
        self.ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-f', 'rawvideo',
            '-vcodec', 'rawvideo',
            '-s', f'{width}x{height}',
            '-pix_fmt', 'bgr24',
            '-r', str(fps),
            '-i', '-',
            '-c:v', 'libx264',
            '-crf', '28',        # High compression
            '-preset', 'fast',
            '-movflags', '+faststart',
            video_path
        ]
        
        logger.info("Starting FFmpeg compression process...")
        try:
            self.process = subprocess.Popen(
                self.ffmpeg_cmd, 
                stdin=subprocess.PIPE, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            logger.error(f"Failed to start FFmpeg: {e}")
            raise RuntimeError(f"FFmpeg start failed: {e}")

    def write_frame(self, frame: np.ndarray):
        """Write annotated frame to video file via FFmpeg pipe."""
        if frame.shape[0] != self.height or frame.shape[1] != self.width:
            frame = cv2.resize(frame, (self.width, self.height))
        
        try:
            self.process.stdin.write(frame.tobytes())
        except Exception as e:
            logger.error(f"Error writing frame to FFmpeg: {e}")

    def inject_metadata(self, metadata: Dict[str, Any]):
        """Store metadata for this frame."""
        self.metadata["frames"].append(metadata)

    def close(self):
        """Finalize video and write JSON metadata."""
        # Close ffmpeg stdin to signal EOF and wait for process to finish
        if self.process:
            self.process.stdin.close()
            self.process.wait()
            logger.info(f"Video saved: {self.output_dir}/{self.video_filename} ({len(self.metadata['frames'])} frames)")
        
        # Write JSON metadata
        json_path = os.path.join(self.output_dir, self.json_filename)
        with open(json_path, 'w') as f:
            json.dump(self.metadata, f, indent=2, default=str)
        
        elapsed = time.time() - self.start_time
        logger.info(f"Metadata saved: {json_path}")
        logger.info(f"Batch processing complete! Total time: {elapsed:.2f}s ({len(self.metadata['frames']) / elapsed:.1f} fps)")
