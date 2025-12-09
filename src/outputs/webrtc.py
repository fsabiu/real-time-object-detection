"""
WebRTC output writer with data channel for frame-synchronized metadata.

Uses aiortc for WebRTC and aiohttp for signaling server.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Optional, Dict, Any
import cv2
import numpy as np

logger = logging.getLogger("SRTYOLOUnified.WebRTC")

try:
    from aiohttp import web
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCConfiguration, RTCIceServer
    from aiortc.contrib.media import MediaRelay
    from av import VideoFrame
    WEBRTC_AVAILABLE = True
except ImportError:
    WEBRTC_AVAILABLE = False
    logger.warning("aiortc/aiohttp not available - WebRTC output disabled")


class FrameVideoTrack(VideoStreamTrack):
    """
    A video track that serves frames pushed from the detector pipeline.
    Properly encodes frames for browser consumption.
    """
    kind = "video"

    def __init__(self, width=640, height=480, fps=30):
        super().__init__()
        self._frame = None
        self._frame_time = 0
        self._lock = threading.Lock()
        self._start_time = time.time()
        self.width = width
        self.height = height
        self.fps = fps

    def push_frame(self, frame: np.ndarray, timestamp: float):
        """Push a new frame from the detector pipeline."""
        with self._lock:
            self._frame = frame.copy()
            self._frame_time = timestamp

    async def recv(self):
        """Receive the next frame for WebRTC transmission."""
        # Calculate PTS based on elapsed time and FPS
        pts, time_base = await self.next_timestamp()

        # Get current frame
        with self._lock:
            if self._frame is None:
                # Return black frame if no frame available yet
                frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            else:
                frame = self._frame.copy()

        # Ensure frame is correct size
        if frame.shape[0] != self.height or frame.shape[1] != self.width:
            frame = cv2.resize(frame, (self.width, self.height))

        # Convert BGR to RGB for WebRTC
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Create VideoFrame with proper format
        video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base

        return video_frame



class WebRTCWriter:
    """
    WebRTC output writer with signaling server and data channel for metadata.
    """

    def __init__(self, port: int, width: int, height: int, fps: float):
        if not WEBRTC_AVAILABLE:
            raise RuntimeError("aiortc/aiohttp not available - install with: pip install aiortc aiohttp")

        self.port = port
        self.width = width
        self.height = height
        self.fps = fps

        self.video_track = FrameVideoTrack(width, height, fps)
        self.pcs: set = set()  # Active peer connections
        self.data_channels: list = []  # Active data channels
        
        self._loop = None
        self._server_thread = None
        self._app = None
        self._runner = None
        self._lock = threading.Lock()

        self._start_server()
        logger.info(f"WebRTC signaling server started on port {port}")

    def _start_server(self):
        """Start the signaling server in a background thread."""
        self._server_thread = threading.Thread(target=self._run_server, daemon=True)
        self._server_thread.start()
        # Wait for server to be ready
        time.sleep(1)

    def _run_server(self):
        """Run the asyncio event loop for the signaling server."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        self._app = web.Application()
        self._app.router.add_post("/offer", self._handle_offer)
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/health", self._handle_health)  # Separate health check
        self._app.router.add_options("/offer", self._handle_options)
        
        # Add CORS middleware
        async def cors_middleware(app, handler):
            async def middleware_handler(request):
                if request.method == "OPTIONS":
                    response = web.Response()
                else:
                    try:
                        response = await handler(request)
                    except Exception as e:
                        logger.error(f"Request handler error: {e}")
                        response = web.json_response({"error": str(e)}, status=500)
                        
                response.headers["Access-Control-Allow-Origin"] = "*"
                response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type"
                return response
            return middleware_handler
        
        self._app.middlewares.append(cors_middleware)
        
        self._runner = web.AppRunner(self._app)
        self._loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        self._loop.run_until_complete(site.start())
        
        logger.info(f"WebRTC server running on http://0.0.0.0:{self.port}")
        self._loop.run_forever()

    async def _handle_options(self, request):
        """Handle CORS preflight requests."""
        return web.Response(status=200)

    async def _handle_health(self, request):
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "connections": len(self.pcs),
            "data_channels": len(self.data_channels)
        })

    async def _handle_index(self, request):
        """Serve the frontend HTML."""
        try:
            # Look for the player file relative to the module or current dir
            import os
            
            # Try specific paths
            paths = [
                "tests/webrtc_player.html",
                "../tests/webrtc_player.html",
                "/home/ubuntu/drones/detector/tests/webrtc_player.html"
            ]
            
            content = None
            for path in paths:
                if os.path.exists(path):
                    with open(path, "r") as f:
                        content = f.read()
                    break
            
            if content:
                # Inject the correct signaling URL (same origin)
                content = content.replace("http://localhost:8080/offer", "/offer")
                return web.Response(text=content, content_type="text/html")
            else:
                return web.Response(text="frontend file not found", status=404)
        except Exception as e:
            logger.error(f"Error serving index: {e}")
            return web.Response(text=str(e), status=500)

    async def _handle_offer(self, request):
        """Handle WebRTC offer from client."""
        try:
            params = await request.json()
            offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

            # Configure for localhost connections - no STUN needed
            # Using empty iceServers forces host candidates only
            config = RTCConfiguration(
                iceServers=[]  # Empty = host candidates only (localhost)
            )
            pc = RTCPeerConnection(configuration=config)
            self.pcs.add(pc)

            @pc.on("connectionstatechange")
            async def on_connectionstatechange():
                logger.info(f"Connection state: {pc.connectionState}")
                if pc.connectionState == "failed" or pc.connectionState == "closed":
                    await pc.close()
                    self.pcs.discard(pc)

            @pc.on("datachannel")
            async def on_datachannel(channel):
                logger.info(f"Data channel established: {channel.label}")
                with self._lock:
                    self.data_channels.append(channel)
                
                @channel.on("close")
                def on_close():
                    with self._lock:
                        if channel in self.data_channels:
                            self.data_channels.remove(channel)

            # Add video track
            pc.addTrack(self.video_track)

            # Create data channel for metadata (server-initiated)
            data_channel = pc.createDataChannel("metadata", ordered=True)
            with self._lock:
                self.data_channels.append(data_channel)
            
            @data_channel.on("open")
            def on_open():
                logger.info("Metadata data channel opened")

            @data_channel.on("close")  
            def on_close():
                with self._lock:
                    if data_channel in self.data_channels:
                        self.data_channels.remove(data_channel)

            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            # Wait for ICE gathering to complete
            # This ensures all ICE candidates are included in the SDP
            while pc.iceGatheringState != "complete":
                await asyncio.sleep(0.1)

            return web.json_response({
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type
            })

        except Exception as e:
            logger.error(f"Error handling offer: {e}")
            return web.json_response({"error": str(e)}, status=500)

    def write_frame(self, frame: np.ndarray):
        """Push a video frame to all connected clients."""
        self.video_track.push_frame(frame, time.time())

    def send_metadata(self, metadata: Dict[str, Any]):
        """Send metadata to all connected clients via data channel."""
        if not self.data_channels:
            return

        try:
            json_str = json.dumps(metadata, default=str, separators=(',', ':'))
            
            with self._lock:
                channels_to_remove = []
                for channel in self.data_channels:
                    try:
                        if channel.readyState == "open":
                            channel.send(json_str)
                        elif channel.readyState == "closed":
                            channels_to_remove.append(channel)
                    except Exception as e:
                        logger.warning(f"Error sending metadata: {e}")
                        channels_to_remove.append(channel)
                
                for channel in channels_to_remove:
                    if channel in self.data_channels:
                        self.data_channels.remove(channel)

        except Exception as e:
            logger.error(f"Error serializing metadata: {e}")

    def inject_metadata(self, metadata: Dict[str, Any]):
        """Alias for send_metadata to match HLS writer interface."""
        self.send_metadata(metadata)

    def close(self):
        """Close all connections and stop the server."""
        logger.info("Closing WebRTC writer...")
        
        # Close all peer connections
        if self._loop and self.pcs:
            for pc in list(self.pcs):
                asyncio.run_coroutine_threadsafe(pc.close(), self._loop)
        
        # Stop the event loop
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        
        if self._server_thread:
            self._server_thread.join(timeout=2)
