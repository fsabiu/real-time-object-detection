"""
MJPEG + SSE output writer for frame-perfect video + detection synchronization.
Uses proven technologies that work over any network.
"""

import asyncio
import json
import logging
import threading
import time
import queue
from typing import Optional, Dict, Any
import cv2
import numpy as np

logger = logging.getLogger("SRTYOLOUnified.MJPEG")

try:
    from aiohttp import web
    import aiohttp
    MJPEG_AVAILABLE = True
except ImportError:
    MJPEG_AVAILABLE = False
    logger.warning("aiohttp not available - MJPEG output disabled")


class MJPEGWriter:
    """
    MJPEG stream + SSE metadata writer for frame-perfect synchronization.
    """

    def __init__(self, port: int, width: int, height: int, fps: float, quality: int = 85):
        if not MJPEG_AVAILABLE:
            raise RuntimeError("aiohttp not available - install with: pip install aiohttp")

        self.port = port
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality

        self.frame_queue = queue.Queue(maxsize=2)  # Latest frame
        self.metadata_queue = queue.Queue(maxsize=100)  # Metadata buffer
        self.current_frame_number = 0
        
        self._loop = None
        self._server_thread = None
        self._app = None
        self._runner = None

        self._start_server()
        logger.info(f"MJPEG server started on port {port}")

    def _start_server(self):
        """Start the MJPEG server in a background thread."""
        self._server_thread = threading.Thread(target=self._run_server, daemon=True)
        self._server_thread.start()
        time.sleep(1)

    def _run_server(self):
        """Run the asyncio event loop for the MJPEG server."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        self._app = web.Application()
        self._app.router.add_get("/stream", self._handle_mjpeg)
        self._app.router.add_get("/metadata", self._handle_sse)
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/health", self._handle_health)
        
        self._runner = web.AppRunner(self._app)
        self._loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        self._loop.run_until_complete(site.start())
        
        logger.info(f"MJPEG server running on http://0.0.0.0:{self.port}")
        self._loop.run_forever()

    async def _handle_health(self, request):
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "protocol": "mjpeg+sse",
            "frame_queue_size": self.frame_queue.qsize()
        })

    async def _handle_index(self, request):
        """Serve the frontend HTML."""
        try:
            import os
            paths = [
                "tests/mjpeg_player.html",
                "/home/ubuntu/drones/detector/tests/mjpeg_player.html"
            ]
            
            for path in paths:
                if os.path.exists(path):
                    with open(path, "r") as f:
                        return web.Response(text=f.read(), content_type="text/html")
            
            return web.Response(text="frontend file not found", status=404)
        except Exception as e:
            logger.error(f"Error serving index: {e}")
            return web.Response(text=str(e), status=500)

    async def _handle_mjpeg(self, request):
        """Stream MJPEG video."""
        response = web.StreamResponse()
        response.content_type = 'multipart/x-mixed-replace; boundary=frame'
        await response.prepare(request)
        
        logger.info("MJPEG client connected")
        
        try:
            while True:
                # Get latest frame (non-blocking)
                try:
                    frame_data = self.frame_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(1/self.fps)
                    continue
                
                # Send frame
                await response.write(
                    b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n\r\n' + frame_data + b'\r\n'
                )
                
        except Exception as e:
            logger.info(f"MJPEG client disconnected: {e}")
        
        return response

    async def _handle_sse(self, request):
        """Stream metadata via Server-Sent Events."""
        response = web.StreamResponse()
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Connection'] = 'keep-alive'
        await response.prepare(request)
        
        logger.info("SSE client connected")
        
        try:
            while True:
                # Get metadata (blocking with timeout)
                try:
                    metadata = self.metadata_queue.get(timeout=1)
                    
                    # Send as SSE
                    data = json.dumps(metadata, default=str)
                    await response.write(f"data: {data}\n\n".encode('utf-8'))
                    
                except queue.Empty:
                    # Send keepalive
                    await response.write(b": keepalive\n\n")
                    
        except Exception as e:
            logger.info(f"SSE client disconnected: {e}")
        
        return response

    def write_frame(self, frame: np.ndarray):
        """Encode and queue frame for MJPEG stream."""
        try:
            # Resize if needed
            if frame.shape[0] != self.height or frame.shape[1] != self.width:
                frame = cv2.resize(frame, (self.width, self.height))
            
            # Encode as JPEG
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
            _, buffer = cv2.imencode('.jpg', frame, encode_param)
            frame_data = buffer.tobytes()
            
            # Update queue (drop old frames if full)
            if self.frame_queue.full():
                try:
                    self.frame_queue.get_nowait()
                except queue.Empty:
                    pass
            
            self.frame_queue.put(frame_data)
            self.current_frame_number += 1
            
        except Exception as e:
            logger.error(f"Error encoding frame: {e}")

    def send_metadata(self, metadata: Dict[str, Any]):
        """Queue metadata for SSE stream."""
        try:
            # Add frame number for synchronization
            metadata['frame_number'] = self.current_frame_number
            metadata['timestamp_ms'] = int(time.time() * 1000)
            
            # Queue metadata
            if self.metadata_queue.full():
                try:
                    self.metadata_queue.get_nowait()
                except queue.Empty:
                    pass
            
            self.metadata_queue.put(metadata)
            
        except Exception as e:
            logger.error(f"Error queuing metadata: {e}")

    def inject_metadata(self, metadata: Dict[str, Any]):
        """Alias for send_metadata to match HLS/WebRTC writer interface."""
        self.send_metadata(metadata)

    def close(self):
        """Close the server."""
        logger.info("Closing MJPEG writer...")
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._server_thread:
            self._server_thread.join(timeout=2)
