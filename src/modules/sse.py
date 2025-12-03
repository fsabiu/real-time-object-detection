import threading
import logging
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("SRTYOLOUnified.SSE")

class SSEBroadcaster:
    def __init__(self):
        self._subscribers = []  # list[queue.Queue]
        self._lock = threading.Lock()

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, data: str):
        dead = []
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                if q in self._subscribers:
                    self._subscribers.remove(q)


def start_sse_server(port: int, broadcaster: SSEBroadcaster, stop_event: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.debug("SSE: " + fmt % args)
        
        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

        def do_GET(self):
            if self.path != '/events':
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

            q = broadcaster.subscribe()
            try:
                self.wfile.write(b":\n\n")
                self.wfile.flush()
                while not stop_event.is_set():
                    try:
                        data = q.get(timeout=0.5)
                    except Exception:
                        continue
                    payload = f"data: {data}\n\n".encode('utf-8')
                    self.wfile.write(payload)
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                broadcaster.unsubscribe(q)

    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)

    def serve():
        logger.info(f"SSE server listening on :{port} at /events")
        while not stop_event.is_set():
            server.handle_request()

    t = threading.Thread(target=serve, name="sse-server", daemon=True)
    t.start()
    return server
