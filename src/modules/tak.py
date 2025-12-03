import socket
import ssl
import threading
import queue
import time
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger("SRTYOLOUnified.TAK")

class TAKCoTSender:
    """
    TAK Server Cursor on Target (CoT) message sender with async queue.
    Uses background thread to send messages without blocking main pipeline.
    """
    
    def __init__(self, host='localhost', port=8089, cert_file='certs/user1.pem', 
                 key_file='certs/user1.key', cert_password='atakatak', 
                 enabled=False, stale_time_seconds=600):
        self.host = host
        self.port = port
        self.cert_file = cert_file
        self.key_file = key_file
        self.cert_password = cert_password
        self.enabled = enabled
        self.stale_time_seconds = stale_time_seconds
        self.ssl_context = None
        self.connection = None
        self.ssl_socket = None
        self.connected = False
        self.lock = threading.Lock()
        
        # Async queue for non-blocking sends
        self.message_queue = queue.Queue(maxsize=1000)
        self.sender_thread = None
        self.batch_timer_thread = None
        self.stop_event = threading.Event()
        self.messages_sent = 0
        self.messages_dropped = 0
        self.ready = False  # Flag to indicate TAK is connected and ready
        
        # Rate limiting: Track last send time per track_id
        self.last_send_time = {}  # {track_id: timestamp}
        self.update_interval = 3.0  # Send updates every 3 seconds per track
        self.rate_limit_lock = threading.Lock()
        
        # Aggregation settings
        self.max_detections_per_batch = 5  # Maximum detections to send per batch
        self.batch_window_seconds = 5.0  # Time window for batching detections (send every 5 seconds)
        self.pending_detections = []  # Queue for pending detections
        self.last_batch_send_time = 0  # Last time we sent a batch
        self.batch_lock = threading.Lock()
        
        if self.enabled:
            self._setup_ssl_context()
            self._start_sender_thread()
            self._start_batch_timer_thread()
    
    def _setup_ssl_context(self):
        """Setup SSL context with certificates."""
        try:
            self.ssl_context = ssl.create_default_context()
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
            
            # Try to load certificates
            try:
                self.ssl_context.load_cert_chain(
                    certfile=self.cert_file,
                    keyfile=self.key_file,
                    password=self.cert_password
                )
                logger.info(f"✅ TAK certificates loaded: {self.cert_file}")
            except Exception as cert_error:
                logger.warning(f"⚠️ TAK certificate loading failed: {cert_error}")
                try:
                    # Try without password
                    self.ssl_context.load_cert_chain(
                        certfile=self.cert_file,
                        keyfile=self.key_file
                    )
                    logger.info(f"✅ TAK certificates loaded (no password): {self.cert_file}")
                except Exception as e:
                    logger.error(f"❌ Failed to load TAK certificates: {e}")
                    self.enabled = False
        except Exception as e:
            logger.error(f"❌ Failed to setup SSL context: {e}")
            self.enabled = False
    
    def connect(self):
        """Establish connection to TAK server."""
        if not self.enabled:
            return False
        
        with self.lock:
            try:
                if self.connected:
                    return True
                
                logger.info(f"🔌 Connecting to TAK server: {self.host}:{self.port}")
                self.connection = socket.create_connection((self.host, self.port), timeout=15)
                self.ssl_socket = self.ssl_context.wrap_socket(
                    self.connection, 
                    server_hostname=self.host
                )
                self.connected = True
                logger.info(f"✅ TAK server connected: {self.host}:{self.port}")
                return True
            except Exception as e:
                logger.error(f"❌ TAK connection failed: {e}")
                self.connected = False
                return False
    
    def _start_sender_thread(self):
        """Start background thread for sending TAK messages."""
        self.sender_thread = threading.Thread(target=self._sender_worker, daemon=True)
        self.sender_thread.start()
        logger.info("✅ TAK sender thread started")
    
    def _start_batch_timer_thread(self):
        """Start background thread for periodic batch sending."""
        self.batch_timer_thread = threading.Thread(target=self._batch_timer_worker, daemon=True)
        self.batch_timer_thread.start()
        logger.info("✅ TAK batch timer thread started")
    
    def _sender_worker(self):
        """Background worker thread that processes the message queue."""
        # Wait for initial connection before marking as ready
        if not self.connect():
            logger.error("❌ TAK initial connection failed, sender thread will retry")
        else:
            self.ready = True  # Mark as ready once connected
        
        while not self.stop_event.is_set():
            try:
                # Wait for message with timeout to allow checking stop_event
                try:
                    message = self.message_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Ensure connected
                if not self.connected:
                    if not self.connect():
                        self.messages_dropped += 1
                        continue
                    else:
                        self.ready = True  # Mark as ready after reconnection
                
                # Send message
                try:
                    with self.lock:
                        self.ssl_socket.send(message.encode('utf-8'))
                        self.messages_sent += 1
                except Exception as send_error:
                    logger.warning(f"⚠️ TAK send failed: {send_error}")
                    self.connected = False
                    self.ready = False  # Mark as not ready when connection fails
                    self.messages_dropped += 1
                    
            except Exception as e:
                logger.error(f"Error in TAK sender thread: {e}")
                time.sleep(1)  # Avoid tight loop on errors
    
    def _batch_timer_worker(self):
        """Background worker thread that sends batches every 5 seconds."""
        while not self.stop_event.is_set():
            try:
                # Wait for 5 seconds
                if self.stop_event.wait(self.batch_window_seconds):
                    break  # Stop event was set
                
                # Check if we have pending detections and it's time to send
                with self.batch_lock:
                    current_time = time.time()
                    time_since_last = current_time - self.last_batch_send_time
                    
                    if (len(self.pending_detections) > 0 and 
                        time_since_last >= self.batch_window_seconds):
                        
                        total_pending = len(self.pending_detections)
                        
                        # Select up to max_detections_per_batch detections (drop excess)
                        detections_to_send = self.pending_detections[:self.max_detections_per_batch]
                        
                        # Remove sent detections from pending queue
                        self.pending_detections = self.pending_detections[self.max_detections_per_batch:]
                        
                        # Update last batch send time
                        self.last_batch_send_time = current_time
                        
                        # Send the batch
                        if detections_to_send:
                            self._send_detection_batch(detections_to_send)
                            
            except Exception as e:
                logger.error(f"Error in TAK batch timer thread: {e}")
                time.sleep(1)  # Avoid tight loop on errors
    
    def disconnect(self):
        """Close connection to TAK server and stop sender thread."""
        # Send any remaining pending detections before disconnecting
        with self.batch_lock:
            if self.pending_detections:
                logger.info(f"📡 Sending {len(self.pending_detections)} remaining detections before disconnect")
                self._send_detection_batch(self.pending_detections)
                self.pending_detections = []
        
        self.stop_event.set()
        if self.sender_thread and self.sender_thread.is_alive():
            self.sender_thread.join(timeout=2.0)
        if self.batch_timer_thread and self.batch_timer_thread.is_alive():
            self.batch_timer_thread.join(timeout=2.0)
        
        with self.lock:
            try:
                if self.ssl_socket:
                    self.ssl_socket.close()
                if self.connection:
                    self.connection.close()
                self.connected = False
                logger.info(f"🔌 TAK server disconnected (sent: {self.messages_sent}, dropped: {self.messages_dropped})")
            except Exception as e:
                logger.debug(f"Error disconnecting from TAK: {e}")
    
    def build_cot_message(self, detection, frame_num=0):
        """
        Build CoT XML message from detection data.
        """
        try:
            # Extract required data
            class_name = detection.get('class_name', 'Unknown')
            geo_coords = detection.get('geo_coordinates')
            track_id = detection.get('track_id')
            
            if not geo_coords:
                return None
            
            latitude = geo_coords.get('latitude')
            longitude = geo_coords.get('longitude')
            
            if latitude is None or longitude is None:
                return None
            
            # Generate UID for this detection
            if track_id is not None:
                uid = f"YOLO-{class_name}-{track_id}"
            else:
                uid = f"YOLO-{class_name}-{frame_num}-{str(uuid.uuid4())[:8]}"
            
            # Build callsign
            confidence = detection.get('confidence', 0.0)
            if track_id is not None:
                callsign = f"{class_name}_ID{track_id}_{confidence:.0%}"
            else:
                callsign = f"{class_name}_{confidence:.0%}"
            
            # Time information
            now = datetime.now(timezone.utc)
            time_str = now.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            stale_time = datetime.fromtimestamp(
                now.timestamp() + self.stale_time_seconds, 
                timezone.utc
            ).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            
            # Altitude (default to ground level if not available)
            altitude = geo_coords.get('altitude', 0.0)
            
            # Distance and camera info for remarks
            distance = geo_coords.get('estimated_ground_distance_m', 0)
            camera_az = geo_coords.get('camera_azimuth_deg', 0)
            camera_el = geo_coords.get('camera_elevation_deg', 0)
            
            # CoT type: a-f-G = friendly ground (can customize based on class_name)
            cot_type = self._get_cot_type(class_name)
            
            # Build CoT XML
            cot_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{uid}" type="{cot_type}" time="{time_str}" start="{time_str}" stale="{stale_time}" how="m-g">
<point lat="{latitude:.6f}" lon="{longitude:.6f}" hae="{altitude:.1f}" ce="10.0" le="10.0"/>
<detail>
<contact callsign="{callsign}" endpoint="*:-1:stcp"/>
<uid Droid="{callsign}"/>
<__group name="Yellow" role="Team Member"/>
<status battery="100"/>
<takv device="YOLO Detection" platform="Python Pipeline" os="Linux" version="1.0"/>
<track speed="0.0" course="{camera_az:.1f}"/>
<remarks>{"Tracked" if track_id else "Detected"}: {class_name}{f" (ID:{track_id})" if track_id else ""} | Distance: {distance:.0f}m | Camera: Az={camera_az:.1f}° El={camera_el:.1f}° | Conf={confidence:.1%}</remarks>
<precisionlocation altsrc="DTED0" geopointsrc="Photogrammetry"/>
</detail>
</event>
'''
            return cot_xml
            
        except Exception as e:
            logger.debug(f"Error building CoT message: {e}")
            return None
    
    def _get_cot_type(self, class_name):
        """Map detection class to CoT type."""
        hostile_classes = ['weapon', 'gun', 'threat']
        class_lower = class_name.lower()
        if any(h in class_lower for h in hostile_classes):
            return "a-h-G-U-C"  # hostile
        else:
            return "a-n-G-U-C"  # neutral (default for detections)
    
    def send_detection(self, detection, frame_num=0):
        """Queue a detection for TAK server sending."""
        if not self.enabled:
            return False
        
        # Don't queue messages until TAK is connected and ready
        if not self.ready:
            return False

        current_time = time.time()
        
        # Add detection to pending queue with timestamp
        with self.batch_lock:
            self.pending_detections.append({
                'detection': detection,
                'frame_num': frame_num,
                'timestamp': current_time
            })
            
            # Limit queue size to prevent memory issues - keep only recent detections
            if len(self.pending_detections) > 20:
                self.pending_detections = self.pending_detections[-20:]
            
            return True
    
    def _send_detection_batch(self, detection_batch):
        """Send a batch of detections to TAK server."""
        try:
            success_count = 0
            
            for item in detection_batch:
                detection = item['detection']
                frame_num = item['frame_num']
                
                # Rate limiting: Check if we should send this track_id
                track_id = detection.get('track_id')
                if track_id is not None:
                    with self.rate_limit_lock:
                        last_time = self.last_send_time.get(track_id, 0)
                        time_since_last = time.time() - last_time
                        
                        # Skip if we sent this track_id too recently
                        if time_since_last < self.update_interval:
                            continue  # Skip this detection
                        
                        # Update last send time
                        self.last_send_time[track_id] = time.time()
                        
                        # Clean up old track_ids (older than 60 seconds)
                        if len(self.last_send_time) > 1000:
                            cutoff_time = time.time() - 60.0
                            self.last_send_time = {
                                tid: t for tid, t in self.last_send_time.items() 
                                if t > cutoff_time
                            }
                
                # Build and queue CoT message
                cot_message = self.build_cot_message(detection, frame_num)
                if cot_message:
                    try:
                        self.message_queue.put_nowait(cot_message)
                        success_count += 1
                    except queue.Full:
                        self.messages_dropped += 1
                        if self.messages_dropped % 100 == 1:
                            logger.warning(f"⚠️ TAK queue full, {self.messages_dropped} messages dropped so far")
            
            return success_count > 0
                    
        except Exception as e:
            logger.debug(f"Error sending TAK batch: {e}")
            return False
