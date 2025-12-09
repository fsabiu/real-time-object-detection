"""
WebSocket output writer for frame-perfect video + detection synchronization.
Works over port forwarding and remote connections (unlike WebRTC).
"""

import asyncio
import json
import logging
import threading
import time
import base64
from typing import Optional, Dict, Any
import cv2
import numpy as np

logger = logging.getLogger("SRTYOLOUnified.WebSocket")

try:
    from aiohttp import web
    import aiohttp
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger.warning("aiohttp not available - WebSocket output disabled")


class WebSocketWriter:
    """
    WebSocket output writer that sends JPEG frames + metadata for canvas rendering.
    """

    def __init__(self, port: int, width: int, height: int, fps: float, quality: int = 85):
        if not WEBSOCKET_AVAILABLE:
            raise RuntimeError("aiohttp not available - install with: pip install aiohttp")

        self.port = port
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality  # JPEG quality (0-100)

        self.clients: set = set()  # Active WebSocket connections
        self._lock = threading.Lock()
        
        self._loop = None
        self._server_thread = None
        self._app = None
        self._runner = None

        self._start_server()
        logger.info(f"WebSocket server started on port {port}")

    def _start_server(self):
        """Start the WebSocket server in a background thread."""
        self._server_thread = threading.Thread(target=self._run_server, daemon=True)
        self._server_thread.start()
        time.sleep(1)  # Wait for server to be ready

    def _run_server(self):
        """Run the asyncio event loop for the WebSocket server."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/health", self._handle_health)
        
        # Add CORS middleware
        async def cors_middleware(app, handler):
            async def middleware_handler(request):
                try:
                    response = await handler(request)
                except Exception as e:
                    logger.error(f"Request handler error: {e}")
                    response = web.json_response({"error": str(e)}, status=500)
                    
                response.headers["Access-Control-Allow-Origin"] = "*"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type"
                return response
            return middleware_handler
        
        self._app.middlewares.append(cors_middleware)
        
        self._runner = web.AppRunner(self._app)
        self._loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        self._loop.run_until_complete(site.start())
        
        logger.info(f"WebSocket server running on http://0.0.0.0:{self.port}")
        self._loop.run_forever()

    async def _handle_health(self, request):
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "clients": len(self.clients),
            "protocol": "websocket"
        })

    async def _handle_index(self, request):
        """Serve the frontend HTML."""
        try:
            import os
            paths = [
                "tests/websocket_player.html",
                "../tests/websocket_player.html",
                "/home/ubuntu/drones/detector/tests/websocket_player.html"
            ]
            
            content = None
            for path in paths:
                if os.path.exists(path):
                    with open(path, "r") as f:
                        content = f.read()
                    break
            
            if content:
                return web.Response(text=content, content_type="text/html")
            else:
                return web.Response(text="frontend file not found", status=404)
        except Exception as e:
            logger.error(f"Error serving index: {e}")
            return web.Response(text=str(e), status=500)

    async def _handle_websocket(self, request):
        """Handle WebSocket connections."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        with self._lock:
            self.clients.add(ws)
        
        logger.info(f"Client connected. Total clients: {len(self.clients)}")
        
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    # Handle client messages if needed
                    pass
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
        finally:
            with self._lock:
                self.clients.discard(ws)
            logger.info(f"Client disconnected. Total clients: {len(self.clients)}")
        
        return ws

    def write_frame(self, frame: np.ndarray):
        """Encode and send frame to all connected clients."""
        if not self.clients:
            return
        
        try:
            # Resize if needed
            if frame.shape[0] != self.height or frame.shape[1] != self.width:
                frame = cv2.resize(frame, (self.width, self.height))
            
            # Encode as JPEG
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
            _, buffer = cv2.imencode('.jpg', frame, encode_param)
            
            # Convert to base64
            frame_b64 = base64.b64encode(buffer).decode('utf-8')
            
            # Send to all clients
            asyncio.run_coroutine_threadsafe(
                self._broadcast({"type": "frame", "data": frame_b64}),
                self._loop
            )
        except Exception as e:
            logger.error(f"Error encoding frame: {e}")

    def send_metadata(self, metadata: Dict[str, Any]):
        """Send metadata to all connected clients."""
        if not self.clients:
            return
        
        try:
            asyncio.run_coroutine_threadsafe(
                self._broadcast({"type": "metadata", "data": metadata}),
                self._loop
            )
        except Exception as e:
            logger.error(f"Error sending metadata: {e}")

    def inject_metadata(self, metadata: Dict[str, Any]):
        """Alias for send_metadata to match HLS/WebRTC writer interface."""
        self.send_metadata(metadata)

    async def _broadcast(self, message: dict):
        """Broadcast message to all connected clients."""
        if not self.clients:
            return
        
        json_str = json.dumps(message, default=str)
        
        with self._lock:
            dead_clients = set()
            for ws in self.clients:
                try:
                    if not ws.closed:
                        await ws.send_str(json_str)
                    else:
                        dead_clients.add(ws)
                except Exception as e:
                    logger.warning(f"Error broadcasting to client: {e}")
                    dead_clients.add(ws)
            
            # Remove dead clients
            self.clients -= dead_clients

    def close(self):
        """Close all connections and stop the server."""
        logger.info("Closing WebSocket writer...")
        
        # Close all client connections
        if self._loop and self.clients:
            for ws in list(self.clients):
                asyncio.run_coroutine_threadsafe(ws.close(), self._loop)
        
        # Stop the event loop
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        
        if self._server_thread:
            self._server_thread.join(timeout=2)
