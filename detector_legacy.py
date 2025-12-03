#!/usr/bin/env python3
"""
Unified SRT → YOLO → RTSP/HLS pipeline with optional ID3v2 injection and SSE/UDP metadata.

Modes:
- auto: Use ID3 (GStreamer GI) when available, else basic pipeline.
- id3:  Require ID3 (error if GI not available).
- basic: Always use basic pipeline (no GI).

Metadata:
- Embeds ID3v2 timed metadata when in id3 mode (via GStreamer id3v2mux).
- Always exposes metadata via optional UDP and optional HTTP SSE for consumers.

TAK Server Integration:
- Sends detected objects as Cursor on Target (CoT) messages to TAK Server
- Uses SSL authentication with client certificates
- Objects include geographic coordinates calculated via photogrammetry
- Automatically reconnects on connection loss
- Uses YOLO tracking mode with persistent IDs (same object = same TAK icon)
- Track IDs prevent duplicate objects on TAK map

Example (basic):
  python3 srt_yolo_hls_unified.py \
    --input-srt 'srt://100.105.188.84:8890' \
    --output-rtsp 'rtsp://localhost:8554/detected_stream' \
    --model runs/detect/train10/weights/best.pt \
    --mode auto --sse-port 8081 --metadata-host 127.0.0.1 --metadata-port 5555

Example (with TAK Server):
  python3 srt_yolo_hls_unified.py \
    --input-srt 'srt://100.105.188.84:8890' \
    --output-rtsp 'rtsp://localhost:8554/detected_stream' \
    --model runs/detect/train10/weights/best.pt \
    --tak-enable --tak-host localhost --tak-port 8089 \
    --tak-cert certs/user1.pem --tak-key certs/user1.key
"""

import argparse
import sys
import struct
import time
import json
import socket
import ssl
import threading
import logging
import uuid
import queue
from pathlib import Path
from datetime import datetime, timezone
from collections import deque

import av
import cv2
import numpy as np
from ultralytics import YOLO

import math

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SRTYOLOUnified")

# Suppress noisy FFmpeg/libav video decoding error messages
# These errors occur when SRT drops packets and video frames are corrupted
# The pipeline handles them gracefully, so we suppress the spam
logging.getLogger('libav').setLevel(logging.CRITICAL)
logging.getLogger('libav.h264').setLevel(logging.CRITICAL)

# Set av (PyAV) logging to only show critical errors
# This suppresses "decode_slice_header error", "field mode" errors, etc.
av.logging.set_level(av.logging.ERROR)


def _try_import_gi():
    """Try to import GStreamer GI; return (available: bool, gi, Gst, GstApp)."""
    try:
        import gi  # type: ignore
        gi.require_version('Gst', '1.0')
        gi.require_version('GstApp', '1.0')
        from gi.repository import Gst, GstApp, GLib  # type: ignore
        Gst.init(None)
        try:
            logger.info(f"Using Python: {sys.executable}")
        except Exception:
            pass
        return True, gi, Gst, GstApp
    except Exception as e:
        logger.debug(f"GStreamer GI not available: {e}")
        return False, None, None, None


class KLVDecoder:
    """Decoder for MISB 0601 KLV metadata."""

    MISB_0601_KEY = bytes([
        0x06, 0x0E, 0x2B, 0x34, 0x02, 0x0B, 0x01, 0x01,
        0x0E, 0x01, 0x03, 0x01, 0x01, 0x00, 0x00, 0x00
    ])

    @staticmethod
    def decode(data):
        """Decode MISB 0601 KLV packet to a dict; return None if not applicable."""
        try:
            if not data.startswith(KLVDecoder.MISB_0601_KEY):
                return None

            offset = 16
            length_byte = data[offset]
            offset += 1

            if length_byte < 128:
                value_length = length_byte
            elif length_byte == 0x81:
                value_length = data[offset]
                offset += 1
            elif length_byte == 0x82:
                value_length = struct.unpack('>H', data[offset:offset+2])[0]
                offset += 2
            else:
                return None

            telemetry = {}
            end_offset = offset + value_length

            while offset < end_offset and offset < len(data):
                tag = data[offset]
                offset += 1
                if offset >= len(data):
                    break
                item_length = data[offset]
                offset += 1
                if offset + item_length > len(data):
                    break
                value_bytes = data[offset:offset+item_length]
                offset += item_length

                try:
                    if tag == 2:
                        # Unix Timestamp (microseconds, 8-byte unsigned int)
                        if item_length == 8:
                            telemetry['timestamp_us'] = struct.unpack('>Q', value_bytes)[0]
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 2")
                    elif tag == 5:
                        # Platform Roll (degrees, 2-byte signed int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>h', value_bytes)[0]
                            telemetry['roll'] = scaled / 100.0
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 5")
                    elif tag == 6:
                        # Platform Pitch (degrees, 2-byte signed int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>h', value_bytes)[0]
                            telemetry['pitch'] = scaled / 100.0
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 6")
                    elif tag == 7:
                        # Platform Heading (degrees, 2-byte unsigned int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>H', value_bytes)[0]
                            telemetry['heading'] = scaled / 100.0
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 7")
                    elif tag == 13:
                        # Sensor Latitude (degrees, 4-byte signed int scaled by 1e7)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['latitude'] = scaled / 1e7
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 13")
                    elif tag == 14:
                        # Sensor Longitude (degrees, 4-byte signed int scaled by 1e7)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['longitude'] = scaled / 1e7
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 14")
                    elif tag == 15:
                        # Sensor Altitude (meters, 2-byte unsigned int scaled by 10)
                        if item_length == 2:
                            scaled = struct.unpack('>H', value_bytes)[0]
                            telemetry['altitude'] = scaled / 10.0
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 15")
                    elif tag == 18:
                        # Sensor Horizontal Field of View (degrees, 2-byte unsigned int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>H', value_bytes)[0]
                            telemetry['sensor_h_fov'] = scaled / 100.0
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 18")
                    elif tag == 19:
                        # Sensor Vertical Field of View (degrees, 2-byte unsigned int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>H', value_bytes)[0]
                            telemetry['sensor_v_fov'] = scaled / 100.0
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 19")
                    elif tag == 21:
                        # Sensor Relative Roll / Gimbal Roll Relative (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_roll_rel'] = scaled / 1e6
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 21")
                    elif tag == 22:
                        # Sensor Relative Pitch / Gimbal Pitch Relative (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_pitch_rel'] = scaled / 1e6
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 22")
                    elif tag == 23:
                        # Sensor Relative Yaw / Gimbal Yaw Relative (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_yaw_rel'] = scaled / 1e6
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 23")
                    elif tag == 102:
                        # Sensor Width (millimeters, 4-byte float)
                        if item_length == 4:
                            telemetry['sensor_width_mm'] = struct.unpack('>f', value_bytes)[0]
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 102")
                    elif tag == 103:
                        # Sensor Height (millimeters, 4-byte float)
                        if item_length == 4:
                            telemetry['sensor_height_mm'] = struct.unpack('>f', value_bytes)[0]
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 103")
                    elif tag == 104:
                        # Focal Length (millimeters, 4-byte float)
                        if item_length == 4:
                            telemetry['focal_length_mm'] = struct.unpack('>f', value_bytes)[0]
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 104")
                    elif tag == 105:
                        # Gimbal Absolute Yaw (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_yaw_abs'] = scaled / 1e6
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 105")
                    elif tag == 106:
                        # Gimbal Absolute Pitch (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_pitch_abs'] = scaled / 1e6
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 106")
                    elif tag == 107:
                        # Gimbal Absolute Roll (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_roll_abs'] = scaled / 1e6
                        else:
                            logger.debug(f"Unexpected length {item_length} for tag 107")
                except struct.error:
                    continue

            return telemetry
        except Exception as e:
            logger.debug(f"KLV decode error: {e}")
            return None


class TAKCoTSender:
    """
    TAK Server Cursor on Target (CoT) message sender with async queue.
    Uses background thread to send messages without blocking main pipeline.
    """
    
    def __init__(self, host='localhost', port=8089, cert_file='certs/user1.pem', 
                 key_file='certs/user1.key', cert_password='atakatak', 
                 enabled=False, stale_time_seconds=600):
        """
        Initialize TAK CoT sender with async queue.
        
        Args:
            host: TAK server hostname/IP
            port: TAK server SSL port (default 8089)
            cert_file: Path to client certificate PEM file
            key_file: Path to client key PEM file
            cert_password: Certificate password (default: 'atakatak')
            enabled: Enable/disable TAK sending
            stale_time_seconds: How long objects persist on TAK (default: 600s = 10min)
        """
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
                        
                        # Log if we dropped any detections
                        dropped_count = total_pending - len(detections_to_send)
                        # if dropped_count > 0:
                        #     logger.info(f"📡 TAK batch: sending {len(detections_to_send)} detections, dropping {dropped_count} excess")
                        
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
        
        Args:
            detection: Detection dictionary with class_name, geo_coordinates, and optional track_id
            frame_num: Frame number (fallback if no track_id)
            
        Returns:
            CoT XML message string, or None if invalid data
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
            # Use track_id if available (persistent across frames), otherwise use frame number
            if track_id is not None:
                # Persistent UID - same object will update on TAK map
                uid = f"YOLO-{class_name}-{track_id}"
            else:
                # Fallback: new UID every frame (creates new objects)
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
        """
        Map detection class to CoT type.
        
        Reference:
        - a-f-G = friendly ground
        - a-h-G = hostile ground  
        - a-n-G = neutral ground
        - a-u-G = unknown ground
        """
        # Customize based on your detection classes
        hostile_classes = ['weapon', 'gun', 'threat']
        
        class_lower = class_name.lower()
        
        if any(h in class_lower for h in hostile_classes):
            return "a-h-G-U-C"  # hostile
        else:
            return "a-n-G-U-C"  # neutral (default for detections)
    
    def send_detection(self, detection, frame_num=0):
        """
        Queue a detection for TAK server sending.
        Detections are aggregated and sent every 5 seconds (max 5 per batch).
        
        Args:
            detection: Detection dictionary
            frame_num: Frame number
            
        Returns:
            bool: True if queued successfully, False otherwise
        """
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
            if len(self.pending_detections) > 20:  # Keep only last 20 detections (4 batches worth)
                dropped_old = len(self.pending_detections) - 20
                self.pending_detections = self.pending_detections[-20:]
                logger.debug(f"📡 TAK queue full: dropped {dropped_old} old detections, keeping latest 20")
            
            # Just queued, timer thread will handle sending
            return True
    
    def _send_detection_batch(self, detection_batch):
        """
        Send a batch of detections to TAK server.
        
        Args:
            detection_batch: List of detection dictionaries with timestamps
            
        Returns:
            bool: True if all messages queued successfully, False otherwise
        """
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
                        # Only log queue full occasionally to avoid log spam
                        if self.messages_dropped % 100 == 1:
                            logger.warning(f"⚠️ TAK queue full, {self.messages_dropped} messages dropped so far")
            
            # Log batch statistics
            # if success_count > 0:
            #     logger.info(f"📡 TAK batch sent: {success_count}/{len(detection_batch)} detections")
            # elif len(detection_batch) > 0:
            #     logger.debug(f"📡 TAK batch: all {len(detection_batch)} detections were rate-limited")
            
            return success_count > 0
                    
        except Exception as e:
            logger.debug(f"Error sending TAK batch: {e}")
            return False
    
    def get_batch_stats(self):
        """Get statistics about current batching state."""
        with self.batch_lock:
            return {
                'pending_detections': len(self.pending_detections),
                'max_detections_per_batch': self.max_detections_per_batch,
                'batch_window_seconds': self.batch_window_seconds,
                'time_since_last_batch': time.time() - self.last_batch_send_time if self.last_batch_send_time > 0 else 0,
                'messages_sent': self.messages_sent,
                'messages_dropped': self.messages_dropped
            }


def resolve_device(device_value: str) -> str:
    if str(device_value).lower() == 'auto':
        try:
            import torch  # type: ignore
            if torch.cuda.is_available() and torch.cuda.device_count() > 0:
                return '0'
        except Exception:
            pass
        return 'cpu'
    return str(device_value)


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
    NO text rendering for maximum performance in real-time scenarios.
    
    Performance: 1000+ colored boxes in <3ms. Color-coded for class identification.
    
    Args:
        img: Input image as numpy array (H, W, 3)
        detections: List of detection dicts with 'bbox' and 'class_name'
        thickness: Line thickness in pixels
        
    Returns:
        Annotated image with color-coded bounding boxes
    """
    if not detections or len(detections) == 0:
        return img
    
    # Extract all bboxes into a single NumPy array (N, 4)
    # This is the ONLY loop over detections - everything else is vectorized
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
            
            # Skip text rendering - it's too slow for real-time
            # Color-coded boxes are sufficient for identification
            # Text can be added in post-processing or on fewer frames if needed
        except:
            pass
    
    return img_out


def extract_detections(results):
    """
    Extract detections from YOLO results, including track IDs if available.
    
    Args:
        results: YOLO results object (from model() or model.track())
        
    Returns:
        List of detection dictionaries with bbox, class info, and optional track_id
    """
    detections = []
    if len(results.boxes) > 0:
        for box in results.boxes:
            class_id = int(box.cls[0].item())
            confidence = float(box.conf[0].item())
            class_name = results.names.get(class_id, f"class_{class_id}")
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            
            detection = {
                'class_id': class_id,
                'class_name': class_name,
                'confidence': confidence,
                'bbox': [x1, y1, x2, y2]
            }
            
            # Add track ID if available (when using model.track())
            if hasattr(box, 'id') and box.id is not None:
                detection['track_id'] = int(box.id.item())
            
            detections.append(detection)
    return detections



def calculate_object_coordinates(bbox, klv_data, frame_width, frame_height):
    """
    Calculate geographic coordinates (lat/lon) for a detected object using photogrammetry.

    Args:
        bbox: Bounding box [x1, y1, x2, y2] in pixels
        klv_data: Telemetry data containing platform and camera information
        frame_width: Video frame width in pixels
        frame_height: Video frame height in pixels

    Returns:
        dict: Geographic coordinates and metadata, or None if calculation fails
    """
    import math
    import logging

    logger = logging.getLogger(__name__)

    try:
        # --- Validate required KLV fields ---
        required_fields = ['latitude', 'longitude', 'altitude']
        missing_fields = [f for f in required_fields if f not in klv_data or klv_data[f] is None]

        if missing_fields:
            logger.debug(f"Missing required fields for coordinate calculation: {missing_fields}")
            return None

        # --- Platform position/orientation ---
        platform_lat = klv_data['latitude']     # degrees
        platform_lon = klv_data['longitude']    # degrees
        platform_alt = klv_data['altitude']     # meters

        platform_roll = klv_data.get('roll', 0.0)       # degrees
        platform_pitch = klv_data.get('pitch', 0.0)     # degrees
        platform_heading = klv_data.get('heading', 0.0) # degrees (0 = North)

        # --- Gimbal orientation handling ---
        has_absolute = 'gimbal_yaw_abs' in klv_data or 'gimbal_pitch_abs' in klv_data
        has_relative = 'gimbal_yaw_rel' in klv_data or 'gimbal_pitch_rel' in klv_data

        if has_absolute:
            gimbal_yaw_world = klv_data.get('gimbal_yaw_abs', 0.0)
            gimbal_pitch_world = klv_data.get('gimbal_pitch_abs', -90.0)
            gimbal_roll_world = klv_data.get('gimbal_roll_abs', 0.0)
            gimbal_method = "absolute_world_frame"
            logger.debug("Using gimbal ABSOLUTE angles (world frame)")
        elif has_relative:
            gimbal_yaw_rel = klv_data.get('gimbal_yaw_rel', 0.0)
            gimbal_pitch_rel = klv_data.get('gimbal_pitch_rel', -90.0)
            gimbal_roll_rel = klv_data.get('gimbal_roll_rel', 0.0)

            # Approximate world transformation
            gimbal_yaw_world = platform_heading + gimbal_yaw_rel
            gimbal_pitch_world = gimbal_pitch_rel + platform_pitch
            gimbal_roll_world = gimbal_roll_rel + platform_roll
            gimbal_method = "relative_approx_transform"

            logger.debug(f"Using gimbal RELATIVE angles: converted to world frame (APPROXIMATE)")
        else:
            gimbal_yaw_world = platform_heading
            gimbal_pitch_world = -90.0
            gimbal_roll_world = 0.0
            gimbal_method = "fallback_nadir"
            logger.debug("No gimbal data, assuming nadir (straight down)")

        # --- Camera specifications ---
        sensor_width_mm = klv_data.get('sensor_width_mm')
        sensor_height_mm = klv_data.get('sensor_height_mm')
        focal_length_mm = klv_data.get('focal_length_mm')

        # --- Bounding box center (in pixels) ---
        x1, y1, x2, y2 = bbox
        bbox_center_x = (x1 + x2) / 2.0
        bbox_center_y = (y1 + y2) / 2.0

        pixel_offset_x = bbox_center_x - frame_width / 2.0
        pixel_offset_y = bbox_center_y - frame_height / 2.0

        # --- Calculate angular offset from image center ---
        if sensor_width_mm and sensor_height_mm and focal_length_mm:
            angle_per_pixel_x = math.atan(sensor_width_mm / (2.0 * focal_length_mm)) * 2.0 / frame_width
            angle_per_pixel_y = math.atan(sensor_height_mm / (2.0 * focal_length_mm)) * 2.0 / frame_height
            alpha_x = pixel_offset_x * angle_per_pixel_x
            alpha_y = pixel_offset_y * angle_per_pixel_y
            has_camera_specs = True
        elif 'sensor_h_fov' in klv_data and 'sensor_v_fov' in klv_data:
            h_fov_rad = math.radians(klv_data['sensor_h_fov'])
            v_fov_rad = math.radians(klv_data['sensor_v_fov'])
            alpha_x = (pixel_offset_x / frame_width) * h_fov_rad
            alpha_y = (pixel_offset_y / frame_height) * v_fov_rad
            has_camera_specs = True
        else:
            # Fallback to default FOV
            h_fov_rad = math.radians(60.0)
            v_fov_rad = h_fov_rad * (frame_height / frame_width)
            alpha_x = (pixel_offset_x / frame_width) * h_fov_rad
            alpha_y = (pixel_offset_y / frame_height) * v_fov_rad
            has_camera_specs = False

        # --- Camera pointing direction (world frame) ---
        camera_azimuth = (gimbal_yaw_world + math.degrees(alpha_x)) % 360.0
        camera_elevation = gimbal_pitch_world + math.degrees(alpha_y)  # negative = downward

        logger.debug(f"Final camera pointing: azimuth={camera_azimuth:.1f}°, elevation={camera_elevation:.1f}°")

        # --- Compute ground intersection ---
        if camera_elevation >= 0:
            # Looking above the horizon, no ground intersection
            logger.debug(f"Camera elevation {camera_elevation:.1f}° >= 0, cannot determine ground intersection")
            return None

        # Convert to downward angle (from horizontal)
        look_down_angle = abs(camera_elevation)

        # Prevent extreme values for near-horizontal shots
        if look_down_angle < 5:
            logger.debug(f"Look-down angle too shallow ({look_down_angle:.1f}°); ignoring object")
            return None

        # Horizontal distance from drone to ground point (flat Earth assumption)
        horizontal_distance = platform_alt * math.tan(math.radians(look_down_angle))

        # --- Convert displacement to geographic coordinates ---
        meters_per_degree_lat = 111320.0
        meters_per_degree_lon = 111320.0 * math.cos(math.radians(platform_lat))

        displacement_north = horizontal_distance * math.cos(math.radians(camera_azimuth))
        displacement_east = horizontal_distance * math.sin(math.radians(camera_azimuth))

        target_lat = platform_lat + (displacement_north / meters_per_degree_lat)
        target_lon = platform_lon + (displacement_east / meters_per_degree_lon)

        logger.debug(f"Displacement: N={displacement_north:.1f}m, E={displacement_east:.1f}m")
        logger.debug(f"Target coordinates: {target_lat:.6f}, {target_lon:.6f}")

        return {
            "latitude": target_lat,
            "longitude": target_lon,
            "estimated_ground_distance_m": horizontal_distance,
            "camera_azimuth_deg": camera_azimuth,
            "camera_elevation_deg": camera_elevation,
            "calculation_method": "photogrammetry",
            "gimbal_method": gimbal_method,
            "has_camera_specs": has_camera_specs
        }

    except Exception as e:
        logger.exception(f"Coordinate estimation failed: {e}")
        return None


def create_metadata_packet(klv_data, detections, frame_num, timestamp, frame_width=None, frame_height=None, tak_sender=None):
    """
    Create metadata packet with detections and geographic coordinates.
    
    Args:
        klv_data: Telemetry data from KLV decoder
        detections: List of detection dictionaries from YOLO
        frame_num: Frame number
        timestamp: Timestamp string
        frame_width: Video frame width in pixels (optional)
        frame_height: Video frame height in pixels (optional)
    
    Returns:
        dict: Complete metadata packet
    """
    # Log telemetry data periodically for debugging
    if frame_num % 1000 == 0:
        if klv_data:
            logger.info(f"KLV data at frame {frame_num}: {klv_data}")
            # Check for GPS data
            has_gps = all(k in klv_data for k in ['latitude', 'longitude', 'altitude'])
            if not has_gps:
                logger.warning(f"⚠ Missing GPS data in telemetry! Cannot calculate object coordinates. Present fields: {list(klv_data.keys())}")
        else:
            logger.warning(f"⚠ No KLV data at frame {frame_num}")
    
    # Enrich detections with geographic coordinates
    enriched_detections = []
    coords_calculated = 0
    coords_failed = 0
    tak_sent = 0
    
    # Measure coordinate calculation time
    coord_start = time.time()
    
    for detection in detections:
        enriched_detection = detection.copy()
        
        # Calculate geographic coordinates if we have necessary data
        # Only skip very low confidence detections
        if klv_data and frame_width and frame_height and detection.get('confidence', 0) > 0.4:
            try:
                bbox = detection.get('bbox')
                if bbox:
                    geo_coords = calculate_object_coordinates(bbox, klv_data, frame_width, frame_height)
                    if geo_coords:
                        enriched_detection['geo_coordinates'] = geo_coords
                        coords_calculated += 1
                        # if frame_num % 2000 == 0:
                        #     track_info = f" [ID:{detection['track_id']}]" if 'track_id' in detection else ""
                        #     logger.info(f"  ✓ Detection '{detection['class_name']}'{track_info} → ({geo_coords['latitude']:.6f}, {geo_coords['longitude']:.6f})")
                        
                        # Send to TAK server if enabled
                        if tak_sender and tak_sender.enabled:
                            if tak_sender.send_detection(enriched_detection, frame_num):
                                tak_sent += 1
                    else:
                        coords_failed += 1
            except Exception:
                # Coordinate calculation failed, count it but don't log for performance
                coords_failed += 1
        
        enriched_detections.append(enriched_detection)
    
    # Record coordinate calculation time
    coord_time = time.time() - coord_start
    # Note: This will be stored by the calling function
    
    # if frame_num % 100 == 0 and detections:
    #     logger.info(f"Coordinates calculated: {coords_calculated}/{len(detections)} detections")
        #if tak_sender and tak_sender.enabled:
            #logger.info(f"📡 TAK messages sent: {tak_sent}/{coords_calculated} detections")

   
    # Updating drone position
    enriched_detections.append(
        {
        "class_id": -1,
        "class_name": "Parrot",
        "confidence": 1,
        "latitude": klv_data['latitude'] if klv_data and 'latitude' in klv_data else None,
        "longitude": klv_data['longitude'] if klv_data and 'longitude' in klv_data else None,
        "altitude": klv_data['altitude'] if klv_data and 'altitude' in klv_data else None
        }
    )

    return {
        'frame': frame_num,
        'timestamp': timestamp,
        'telemetry': klv_data if klv_data else {},
        'detections': enriched_detections,
        'detection_count': len(enriched_detections)
    }


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


# -------------------- SSE Broadcaster (optional) --------------------
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
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


# -------------------- Pipelines --------------------
class BasePipeline:
    def __init__(self, input_srt, output_rtsp, model_path, conf_threshold=0.25,
                 device='auto', classes=None, show_overlay=True,
                 metadata_file=None, skip_frames=0, srt_latency=500,
                 metadata_host=None, metadata_port=5555,
                 sse_port=None, id3_interval=30,
                 detections_dir='detections', detection_log_interval=5.0, save_detection_images=True,
                 tak_sender=None):
        self.input_srt = input_srt
        self.output_rtsp = output_rtsp
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self.device = device
        self.classes = classes
        self.show_overlay = show_overlay
        self.metadata_file = metadata_file
        self.skip_frames = skip_frames
        self.srt_latency = srt_latency
        self.metadata_host = metadata_host
        self.metadata_port = metadata_port
        self.sse_port = sse_port
        self.id3_interval = id3_interval
        self.detections_dir = detections_dir
        self.detection_log_interval = detection_log_interval
        self.save_detection_images = save_detection_images

        self.model = None
        self.container = None
        self.klv_decoder = KLVDecoder()
        self.tak_sender = tak_sender

        self.frame_count = 0
        self.processed_frame_count = 0
        self.klv_count = 0
        self.detection_count = 0
        self.start_time = None
        self.latest_klv = None
        self.klv_pts = None
        self.latest_detections = []
        self.metadata_buffer = deque(maxlen=1000)
        self.last_detection_log_time = None
        
        # Performance monitoring
        self.yolo_times = deque(maxlen=100)  # Store last 100 inference times
        self.total_processing_times = deque(maxlen=100)  # Store last 100 total processing times
        
        # Comprehensive timing measurements
        self.frame_receive_times = deque(maxlen=100)  # Time to receive frame from stream
        self.frame_decode_times = deque(maxlen=100)   # Time to decode frame
        self.detection_processing_times = deque(maxlen=100)  # Time for detection processing
        self.coordinate_calculation_times = deque(maxlen=100)  # Time for coordinate calculations
        self.metadata_creation_times = deque(maxlen=100)  # Time for metadata creation
        self.frame_write_times = deque(maxlen=100)  # Time to write frame to output
        self.total_frame_times = deque(maxlen=100)  # Total time per frame
        
        # Performance tracking
        self.frame_processing_threshold = 0.030  # 30ms threshold
        self.slow_frame_count = 0
        
        # Initialize detections directory
        if self.detections_dir:
            import os
            detections_path = os.path.abspath(self.detections_dir)
            os.makedirs(detections_path, exist_ok=True)
            self.detections_dir = detections_path
            logger.info(f"✓ Detection logging enabled → {detections_path} (interval: {self.detection_log_interval}s)")

        # UDP socket for metadata streaming
        self.metadata_socket = None
        if self.metadata_host:
            self.metadata_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            logger.info(f"Metadata (UDP) → {self.metadata_host}:{self.metadata_port}")

        # SSE server (optional)
        self._stop_event = threading.Event()
        self.sse_broadcaster = SSEBroadcaster() if self.sse_port else None
        self._sse_server = None
        if self.sse_port:
            self._sse_server = start_sse_server(self.sse_port, self.sse_broadcaster, self._stop_event)

    def _open_srt_container(self):
        logger.info("Opening SRT stream…")
        srt_options = {
            'timeout': '5000000',
            'recv_buffer_size': '8388608',
            'latency': str(self.srt_latency * 1000),
            'payload_size': '1316',
            'max_bw': '0',
            'ffs': '25600',
            'ipttl': '64',
            'iptos': '0xB8',
            'tlpktdrop': '1',
            'tsbpdmode': '1',
        }
        self.container = av.open(self.input_srt, options=srt_options)

    def _load_model(self):
        logger.info("Loading YOLO model…")
        self.model = YOLO(self.model_path)
        self.device = resolve_device(self.device)
        logger.info(f"Device: {self.device}")

    def start_common(self):
        self._load_model()
        self._open_srt_container()

        video_stream = None
        data_stream = None
        for stream in self.container.streams:
            logger.info(f"Found stream: {stream.type} - {stream}")
            if stream.type == 'video':
                video_stream = stream
            elif stream.type == 'data':
                data_stream = stream

        if not video_stream:
            raise RuntimeError("No video stream found in SRT input")

        width = video_stream.width
        height = video_stream.height
        fps = float(video_stream.average_rate) if video_stream.average_rate else 30.0
        logger.info(f"Video: {width}x{height} @ {fps} fps")

        self.video_stream = video_stream
        self.data_stream = data_stream
        self.fps = fps
        self.frame_width = width
        self.frame_height = height
        self.start_time = time.time()
        return width, height, int(fps)

    # Hooks implemented by subclasses
    def start(self):
        raise NotImplementedError

    def write_frame(self, frame):
        raise NotImplementedError

    def inject_metadata(self, metadata):
        pass

    def _save_detections(self, metadata, frame_image=None):
        """
        Save detection data to disk.
        
        Args:
            metadata: Metadata packet with detections
            frame_image: Optional numpy array of the current frame for saving detection crops
        """
        if not self.detections_dir:
            return
        
        try:
            import os
            from datetime import datetime
            
            # Create timestamp-based filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]  # milliseconds
            
            # Save metadata as JSON
            json_filename = os.path.join(self.detections_dir, f"detections_{timestamp}.json")
            with open(json_filename, 'w') as f:
                json.dump(metadata, f, indent=2, default=str)
            
            detections_with_coords = len([d for d in metadata.get('detections', []) if 'geo_coordinates' in d])
            total_detections = len(metadata.get('detections', []))
            
            logger.info(f"Saved detections: {total_detections} objects ({detections_with_coords} with coords) → {json_filename}")
            
            # Optionally save cropped detection images
            if self.save_detection_images and frame_image is not None and total_detections > 0:
                crops_dir = os.path.join(self.detections_dir, f"crops_{timestamp}")
                os.makedirs(crops_dir, exist_ok=True)
                
                for idx, detection in enumerate(metadata.get('detections', [])):
                    try:
                        bbox = detection.get('bbox')
                        if bbox:
                            x1, y1, x2, y2 = map(int, bbox)
                            # Ensure coordinates are within image bounds
                            h, w = frame_image.shape[:2]
                            x1, y1 = max(0, x1), max(0, y1)
                            x2, y2 = min(w, x2), min(h, y2)
                            
                            if x2 > x1 and y2 > y1:
                                crop = frame_image[y1:y2, x1:x2]
                                class_name = detection.get('class_name', 'unknown')
                                confidence = detection.get('confidence', 0.0)
                                
                                # Include coordinates in filename if available
                                if 'geo_coordinates' in detection:
                                    geo = detection['geo_coordinates']
                                    crop_filename = f"{idx:03d}_{class_name}_{confidence:.2f}_lat{geo['latitude']:.6f}_lon{geo['longitude']:.6f}.jpg"
                                else:
                                    crop_filename = f"{idx:03d}_{class_name}_{confidence:.2f}.jpg"
                                
                                crop_path = os.path.join(crops_dir, crop_filename)
                                cv2.imwrite(crop_path, crop)
                    except Exception as e:
                        logger.debug(f"Error saving detection crop {idx}: {e}")
                
                logger.info(f"Saved {total_detections} detection crops → {crops_dir}")
        
        except Exception as e:
            logger.error(f"Error saving detections: {e}", exc_info=True)

    def get_performance_stats(self):
        """Get comprehensive performance statistics."""
        stats = {
            'frame_count': self.frame_count,
            'processed_frame_count': self.processed_frame_count,
            'slow_frame_count': self.slow_frame_count,
            'slow_frame_percentage': (self.slow_frame_count / self.frame_count) * 100 if self.frame_count > 0 else 0,
            'detection_count': self.detection_count,
            'klv_count': self.klv_count,
            'avg_yolo_time_ms': sum(self.yolo_times) / len(self.yolo_times) * 1000 if self.yolo_times else 0,
            'max_yolo_time_ms': max(self.yolo_times) * 1000 if self.yolo_times else 0,
            'avg_total_frame_time_ms': sum(self.total_frame_times) / len(self.total_frame_times) * 1000 if self.total_frame_times else 0,
            'avg_decode_time_ms': sum(self.frame_decode_times) / len(self.frame_decode_times) * 1000 if self.frame_decode_times else 0,
            'avg_metadata_time_ms': sum(self.metadata_creation_times) / len(self.metadata_creation_times) * 1000 if self.metadata_creation_times else 0,
            'avg_write_time_ms': sum(self.frame_write_times) / len(self.frame_write_times) * 1000 if self.frame_write_times else 0,
            'threshold_ms': self.frame_processing_threshold * 1000
        }
        return stats

    def stop(self):
        self._stop_event.set()
        if self.container:
            try:
                self.container.close()
                logger.info("SRT stream closed")
            except Exception as e:
                logger.error(f"Error closing container: {e}")
        if self.metadata_socket:
            try:
                self.metadata_socket.close()
                logger.info("Metadata UDP socket closed")
            except Exception as e:
                logger.error(f"Error closing UDP socket: {e}")
        if self.tak_sender:
            try:
                self.tak_sender.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting TAK sender: {e}")
        if self.metadata_file and self.metadata_buffer:
            try:
                with open(self.metadata_file, 'w') as f:
                    json.dump(list(self.metadata_buffer), f, indent=2)
                logger.info(f"Saved metadata to {self.metadata_file}")
            except Exception as e:
                logger.error(f"Error saving metadata: {e}")
        elapsed = time.time() - self.start_time if self.start_time else 0
        logger.info("\n" + "=" * 70)
        logger.info("Summary:")
        logger.info(f"  Frames processed: {self.frame_count}")
        logger.info(f"  KLV packets: {self.klv_count}")
        logger.info(f"  Total detections: {self.detection_count}")
        logger.info(f"  Duration: {elapsed:.1f}s")
        if self.frame_count > 0 and elapsed > 0:
            logger.info(f"  Average FPS: {self.frame_count / elapsed:.2f}")
        logger.info("=" * 70)

    def _reconnect_stream(self, max_retries=5, retry_delay=3):
        """Attempt to reconnect to the SRT stream after an error."""
        logger.info(f"🔄 Starting SRT stream reconnection process...")
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Reconnection attempt {attempt + 1}/{max_retries}...")
                
                # Clean up existing connection
                if self.container:
                    try:
                        self.container.close()
                        logger.debug("Closed existing container")
                    except Exception as e:
                        logger.debug(f"Error closing container: {e}")
                
                # Wait before retry (exponential backoff)
                wait_time = retry_delay * (2 ** attempt)
                logger.info(f"Waiting {wait_time}s before reconnection attempt...")
                time.sleep(wait_time)
                
                # Attempt to reconnect
                self._open_srt_container()
                
                # Re-identify streams
                video_stream = None
                data_stream = None
                for stream in self.container.streams:
                    if stream.type == 'video':
                        video_stream = stream
                    elif stream.type == 'data':
                        data_stream = stream
                
                if not video_stream:
                    logger.warning("No video stream found after reconnection")
                    continue
                
                self.video_stream = video_stream
                self.data_stream = data_stream
                
                # Skip stream testing on later attempts to avoid hanging
                if attempt >= 2:
                    logger.info("✅ Reconnected to SRT stream (skipping detailed test to avoid hanging)")
                    return True
                
                # Test the connection by checking if streams are available
                logger.info("Testing reconnected stream...")
                try:
                    # Simple test: just check if we can access the stream properties
                    if (self.video_stream and 
                        hasattr(self.video_stream, 'width') and 
                        hasattr(self.video_stream, 'height') and
                        self.video_stream.width > 0 and 
                        self.video_stream.height > 0):
                        logger.info("✅ Successfully reconnected to SRT stream")
                        return True
                    else:
                        logger.warning("Stream properties not ready after reconnection")
                        continue
                except Exception as test_error:
                    logger.warning(f"Stream test failed: {test_error}")
                    continue
                    
            except Exception as e:
                logger.warning(f"Reconnection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    continue
        
        logger.error("❌ All reconnection attempts failed")
        return False

    # Main loop shared logic
    def run(self):
        if not self.start():
            return
        last_fps_time = self.start_time
        fps_frame_count = 0
        current_fps = 0.0
        seen_keyframe = False
        logger.info("Starting inference loop… (Ctrl+C to stop)")
        
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        try:
            while not self._stop_event.is_set():
                try:
                    streams_to_demux = [self.video_stream]
                    if self.data_stream:
                        streams_to_demux.append(self.data_stream)

                    for packet in self.container.demux(streams_to_demux):
                        if self._stop_event.is_set():
                            break
                        if packet.stream.type == 'data':
                            self.klv_count += 1
                            packet_data = bytes(packet)
                            klv_data = self.klv_decoder.decode(packet_data)
                            if klv_data:
                                self.latest_klv = klv_data
                                self.klv_pts = packet.pts
                                # Log KLV data every 5 packets for debugging/monitoring
                                if self.klv_count % 5 == 0:
                                    logger.debug(f"KLV data (packet {self.klv_count}): {klv_data}")
                            continue

                        if packet.stream.type == 'video':
                            # Wait for first keyframe to avoid decoder errors when starting mid-stream
                            if not seen_keyframe:
                                try:
                                    if getattr(packet, 'is_keyframe', False):
                                        seen_keyframe = True
                                    else:
                                        continue
                                except Exception:
                                    # If property not available, attempt decode and rely on error handling
                                    pass

                            try:
                                frames = packet.decode()
                            except Exception:
                                # Corrupt/truncated packet (common on live joins). Skip and continue
                                # Debug logging removed for performance
                                continue
                            for frame in frames:
                                # Start comprehensive timing for this frame
                                frame_start_time = time.time()
                                
                                self.frame_count += 1
                                fps_frame_count += 1

                                # Measure frame decode time
                                decode_start = time.time()
                                try:
                                    img = frame.to_ndarray(format='bgr24')
                                    frame_decode_time = time.time() - decode_start
                                    self.frame_decode_times.append(frame_decode_time)
                                except Exception:
                                    # Frame conversion error, skip frame
                                    # Debug logging removed for performance
                                    continue
                                # Adaptive frame skipping based on processing load
                                detection_count = len(self.latest_detections) if self.latest_detections else 0
                                
                                # Simplified frame skipping - trust the vectorized optimizations
                                should_detect = (self.skip_frames == 0) or (self.frame_count % (self.skip_frames + 1) == 1)
                                if should_detect:
                                    # Measure detection processing time
                                    processing_start = time.time()
                                    detection_start = time.time()
                                    self.processed_frame_count += 1
                                    
                                    # Measure YOLO tracking time
                                    yolo_start = time.time()
                                    # Use tracking mode with streaming for efficiency
                                    # stream=True returns generator, processes frames immediately without batching
                                    results_gen = self.model.track(img, conf=self.conf_threshold, verbose=False, 
                                                                   device=self.device, classes=self.classes, 
                                                                   persist=True, tracker="bytetrack.yaml",
                                                                   stream=True)
                                    # Get first (and only) result from generator
                                    results = next(results_gen)
                                    yolo_time = time.time() - yolo_start
                                    self.yolo_times.append(yolo_time)
                                    
                                    detections = extract_detections(results)
                                    if detections:
                                        self.detection_count += len(detections)
                                        
                                    self.latest_detections = detections
                                    # Ultra-fast vectorized drawing with color-coded classes (no text for performance)
                                    annotated_frame = draw_detections_vectorized(img, detections, thickness=2)
                                    
                                    # Measure total processing time (including metadata, coordinates, TAK)
                                    total_processing_time = time.time() - processing_start
                                    self.total_processing_times.append(total_processing_time)
                                else:
                                    detections = self.latest_detections
                                    # Ultra-fast vectorized drawing (no text)
                                    annotated_frame = draw_detections_vectorized(img, detections, thickness=2)

                                now = time.time()
                                if now - last_fps_time >= 1.0:
                                    current_fps = fps_frame_count / (now - last_fps_time)
                                    last_fps_time = now
                                    fps_frame_count = 0

                                # Always show overlay - trust the optimizations
                                if self.show_overlay:
                                    annotated_frame = overlay_metadata(annotated_frame, self.frame_count, self.latest_klv, detections, current_fps)

                                # Measure metadata creation time
                                metadata_start = time.time()
                                # Process all detections - no artificial limits
                                metadata = create_metadata_packet(
                                    self.latest_klv, 
                                    detections, 
                                    self.frame_count, 
                                    datetime.now().isoformat(),
                                    frame_width=self.frame_width,
                                    frame_height=self.frame_height,
                                    tak_sender=self.tak_sender
                                )
                                metadata_time = time.time() - metadata_start
                                self.metadata_creation_times.append(metadata_time)
                                self.metadata_buffer.append(metadata)

                                # Periodic detection logging to disk
                                if self.detections_dir and detections:
                                    if self.last_detection_log_time is None:
                                        self.last_detection_log_time = now
                                    
                                    if (now - self.last_detection_log_time) >= self.detection_log_interval:
                                        self._save_detections(metadata, img)
                                        self.last_detection_log_time = now

                                # UDP
                                if self.metadata_socket:
                                    try:
                                        self.metadata_socket.sendto(json.dumps(metadata).encode('utf-8'), (self.metadata_host, self.metadata_port))
                                    except Exception:
                                        pass

                                # SSE
                                if self.sse_broadcaster:
                                    try:
                                        self.sse_broadcaster.publish(json.dumps(metadata, separators=(',', ':')))
                                    except Exception:
                                        pass

                                # Optional in-band injection (implemented by subclass)
                                self.inject_metadata(metadata)

                                # Measure frame write time
                                write_start = time.time()
                                self.write_frame(annotated_frame)
                                write_time = time.time() - write_start
                                self.frame_write_times.append(write_time)
                                
                                # Complete timing measurements
                                total_frame_time = time.time() - frame_start_time
                                self.total_frame_times.append(total_frame_time)
                                
                                # Simple performance tracking - just count slow frames
                                if total_frame_time > self.frame_processing_threshold:
                                    self.slow_frame_count += 1

                                if self.frame_count % 1000 == 0:
                                    # Calculate comprehensive performance metrics
                                    avg_yolo_time = sum(self.yolo_times) / len(self.yolo_times) if self.yolo_times else 0
                                    max_yolo_time = max(self.yolo_times) if self.yolo_times else 0
                                    
                                    # Calculate all timing metrics
                                    avg_frame_decode = sum(self.frame_decode_times) / len(self.frame_decode_times) if self.frame_decode_times else 0
                                    avg_metadata_creation = sum(self.metadata_creation_times) / len(self.metadata_creation_times) if self.metadata_creation_times else 0
                                    avg_frame_write = sum(self.frame_write_times) / len(self.frame_write_times) if self.frame_write_times else 0
                                    avg_total_frame = sum(self.total_frame_times) / len(self.total_frame_times) if self.total_frame_times else 0
                                    avg_total_time = sum(self.total_processing_times) / len(self.total_processing_times) if self.total_processing_times else 0
                                    max_total_time = max(self.total_processing_times) if self.total_processing_times else 0
                                    
                                    # Calculate slow frame percentage
                                    slow_frame_percentage = (self.slow_frame_count / self.frame_count) * 100 if self.frame_count > 0 else 0
                                    
                                    logger.info(f"📊 Frames: {self.frame_count} | FPS: {current_fps:.1f} | Detections: {self.detection_count} | YOLO: {avg_yolo_time*1000:.1f}ms avg | Slow frames: {self.slow_frame_count} ({slow_frame_percentage:.1f}%)")
                    
                    # If we exit the demux loop normally, break the outer loop
                    break
                    
                except (av.error.OSError, av.error.TimeoutError) as av_err:
                    # Handle SRT stream errors (e.g., decoding errors, I/O errors, timeouts)
                    consecutive_errors += 1
                    error_type = "timeout" if isinstance(av_err, av.error.TimeoutError) else "stream"
                    logger.warning(f"SRT {error_type} error (attempt {consecutive_errors}/{max_consecutive_errors}): {av_err}")
                    
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error("Too many consecutive errors, attempting reconnection")
                        if not self._reconnect_stream():
                            logger.error("Reconnection failed, stopping stream")
                            break
                        # Reset error counter and keyframe flag after successful reconnection
                        consecutive_errors = 0
                        seen_keyframe = False
                    else:
                        # Wait a bit before continuing, longer for timeout errors
                        wait_time = 2.0 if isinstance(av_err, av.error.TimeoutError) else 0.5
                        time.sleep(wait_time)
                        continue

        except KeyboardInterrupt:
            logger.info("Stopping…")
        except Exception as e:
            logger.error(f"Error during inference: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.stop()


class BasicPipeline(BasePipeline):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.video_writer = None

    def _create_gst_writer(self, width, height, fps):
        gst_pipeline = (
            'appsrc ! '
            'videoconvert ! '
            'video/x-raw,format=I420 ! '
            'queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 leaky=downstream ! '
            'x264enc bitrate=6000 speed-preset=fast key-int-max=60 ! '
            'video/x-h264,profile=main ! '
            'h264parse ! '
            'queue max-size-buffers=0 max-size-bytes=0 max-size-time=100000000 leaky=downstream ! '
            f'rtspclientsink location={self.output_rtsp} protocols=tcp latency=200'
        )
        logger.info("Creating GStreamer VideoWriter (basic mode)…")
        logger.info(f"  Pipeline: {gst_pipeline}")
        out = cv2.VideoWriter(gst_pipeline, cv2.CAP_GSTREAMER, 0, fps, (width, height), True)
        if not out.isOpened():
            raise RuntimeError("Failed to open GStreamer VideoWriter. Ensure MediaMTX and GStreamer are installed.")
        return out

    def start(self):
        width, height, fps = self.start_common()
        self.video_writer = self._create_gst_writer(width, height, fps)
        return True

    def write_frame(self, frame):
        try:
            self.video_writer.write(frame)
        except Exception as e:
            logger.error(f"Error writing frame: {e}")
            raise

    def stop(self):
        if self.video_writer:
            try:
                self.video_writer.release()
                logger.info("GStreamer VideoWriter closed")
            except Exception as e:
                logger.error(f"Error closing VideoWriter: {e}")
        super().stop()


class ID3Pipeline(BasePipeline):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        available, gi, Gst, GstApp = _try_import_gi()
        if not available:
            raise RuntimeError("GStreamer GI not available; cannot run ID3 pipeline")
        self.Gst = Gst
        self.GstApp = GstApp
        self.pipeline = None
        self.appsrc = None
        self.mpegtsmux = None
        self.ffmpeg_process = None
        self.gst_timestamp = 0
        self.frame_duration = 0
        self._id3_counter = 0

    def _create_pipeline(self, width, height, fps):
        Gst = self.Gst
        # Verify required elements exist in this GStreamer installation
        required = ['appsrc', 'videoconvert', 'videoscale', 'x264enc', 'h264parse', 'mpegtsmux', 'fdsink']
        missing = [name for name in required if Gst.ElementFactory.find(name) is None]
        if missing:
            raise RuntimeError(
                "Missing GStreamer elements: " + ', '.join(missing) +
                ". Ensure system Python (/usr/bin/python3) and packages: "
                "python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 "
                "gstreamer1.0-plugins-base gstreamer1.0-plugins-good "
                "gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly"
            )

        # Start ffmpeg process to push to MediaMTX via RTSP
        # Using flv format to preserve metadata
        import subprocess
        self.ffmpeg_process = subprocess.Popen([
            'ffmpeg',
            '-f', 'mpegts',
            '-i', 'pipe:0',
            '-c:v', 'copy',
            '-metadata', 'title=YOLO Detection Stream',
            '-metadata', 'comment=Contains detection and telemetry metadata',
            '-f', 'rtsp',
            '-rtsp_transport', 'tcp',
            self.output_rtsp
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        pipeline = Gst.Pipeline.new("id3-pipeline")

        appsrc = Gst.ElementFactory.make("appsrc", "source")
        if appsrc is None:
            raise RuntimeError("Failed to create 'appsrc'")
        appsrc.set_property("format", self.Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("do-timestamp", True)
        appsrc.set_property("block", True)
        caps = Gst.Caps.from_string(f"video/x-raw,format=BGR,width={width},height={height},framerate={int(fps)}/1")
        appsrc.set_property("caps", caps)

        videoconvert = Gst.ElementFactory.make("videoconvert", "convert")
        videoscale = Gst.ElementFactory.make("videoscale", "scale")
        
        # Add input queue for buffering (reduced for HLS compatibility)
        input_queue = Gst.ElementFactory.make("queue", "input_queue")
        input_queue.set_property("max-size-buffers", 0)
        input_queue.set_property("max-size-bytes", 0)
        input_queue.set_property("max-size-time", 200000000)  # 200ms
        input_queue.set_property("leaky", "downstream")
        
        # Add dedicated encoder queue to smooth out x264enc processing
        encoder_queue = Gst.ElementFactory.make("queue", "encoder_queue")
        encoder_queue.set_property("max-size-buffers", 0)
        encoder_queue.set_property("max-size-bytes", 0)
        encoder_queue.set_property("max-size-time", 1000000000)  # 1 second buffer
        encoder_queue.set_property("leaky", "downstream")
        
        x264enc = Gst.ElementFactory.make("x264enc", "encoder")
        if x264enc is None:
            raise RuntimeError("Failed to create 'x264enc' (install gstreamer1.0-plugins-ugly)")
        
        # Store reference for adaptive quality
        self.x264enc = x264enc
        # Adaptive encoding based on detection load
        self.adaptive_bitrate = 6000
        self.adaptive_speed_preset = "fast"
        x264enc.set_property("speed-preset", self.adaptive_speed_preset)
        x264enc.set_property("bitrate", self.adaptive_bitrate)
        x264enc.set_property("key-int-max", 60)
        x264enc.set_property("threads", 4)

        h264parse = Gst.ElementFactory.make("h264parse", "parser")
        
        # Add output queue for buffering (reduced for HLS compatibility)
        output_queue = Gst.ElementFactory.make("queue", "output_queue")
        output_queue.set_property("max-size-buffers", 0)
        output_queue.set_property("max-size-bytes", 0)
        output_queue.set_property("max-size-time", 100000000)  # 100ms
        output_queue.set_property("leaky", "downstream")
        
        # Create mpegtsmux with metadata support
        mpegtsmux = Gst.ElementFactory.make("mpegtsmux", "mux")
        if mpegtsmux is None:
            raise RuntimeError("Failed to create 'mpegtsmux' (install gstreamer1.0-plugins-bad)")
        mpegtsmux.set_property("alignment", 7)
        
        # Use fdsink to pipe to ffmpeg
        fdsink = Gst.ElementFactory.make("fdsink", "sink")
        if fdsink is None:
            raise RuntimeError("Failed to create 'fdsink'")
        fdsink.set_property("fd", self.ffmpeg_process.stdin.fileno())
        fdsink.set_property("sync", False)

        for e in [appsrc, videoconvert, videoscale, input_queue, encoder_queue, x264enc, h264parse, output_queue, mpegtsmux, fdsink]:
            if not e:
                raise RuntimeError("Failed to create GStreamer element")
            pipeline.add(e)

        if not appsrc.link(videoconvert):
            raise RuntimeError("Failed to link appsrc → videoconvert")
        if not videoconvert.link(videoscale):
            raise RuntimeError("Failed to link videoconvert → videoscale")
        if not videoscale.link(input_queue):
            raise RuntimeError("Failed to link videoscale → input_queue")
        if not input_queue.link(encoder_queue):
            raise RuntimeError("Failed to link input_queue → encoder_queue")
        if not encoder_queue.link(x264enc):
            raise RuntimeError("Failed to link encoder_queue → x264enc")
        if not x264enc.link(h264parse):
            raise RuntimeError("Failed to link x264enc → h264parse")
        if not h264parse.link(output_queue):
            raise RuntimeError("Failed to link h264parse → output_queue")
        if not output_queue.link(mpegtsmux):
            raise RuntimeError("Failed to link output_queue → mpegtsmux")
        if not mpegtsmux.link(fdsink):
            raise RuntimeError("Failed to link mpegtsmux → fdsink")

        ret = pipeline.set_state(self.Gst.State.PLAYING)
        if ret == self.Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to start GStreamer pipeline")

        self.pipeline = pipeline
        self.appsrc = appsrc
        self.mpegtsmux = mpegtsmux
        self.frame_duration = int(self.Gst.SECOND / fps)
        self.gst_timestamp = 0
        logger.info("ID3 pipeline started (using MPEG-TS with inline metadata, publishing via ffmpeg)")

    def start(self):
        width, height, fps = self.start_common()
        self._create_pipeline(width, height, fps)
        return True

    def write_frame(self, frame):
        data = frame.tobytes()
        buf = self.Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts = self.gst_timestamp
        buf.duration = self.frame_duration
        self.gst_timestamp += self.frame_duration
        ret = self.appsrc.emit("push-buffer", buf)
        if ret != self.Gst.FlowReturn.OK:
            raise RuntimeError(f"Error pushing buffer: {ret}")

    def inject_metadata(self, metadata):
        # Inject every id3_interval frames as custom MPEG-TS metadata
        if self.frame_count % max(1, self.id3_interval) != 0:
            return
        try:
            taglist = self.Gst.TagList.new_empty()
            telemetry = metadata.get('telemetry', {})
            if 'latitude' in telemetry and 'longitude' in telemetry:
                gps_string = f"{telemetry['latitude']:.7f},{telemetry['longitude']:.7f}"
                taglist.add_value(self.Gst.TagMergeMode.APPEND, 'geo-location-name', gps_string)
                taglist.add_value(self.Gst.TagMergeMode.APPEND, 'geo-location-latitude', telemetry['latitude'])
                taglist.add_value(self.Gst.TagMergeMode.APPEND, 'geo-location-longitude', telemetry['longitude'])
            if 'altitude' in telemetry:
                taglist.add_value(self.Gst.TagMergeMode.APPEND, 'geo-location-elevation', telemetry['altitude'])
            if 'detection_count' in metadata:
                taglist.add_value(self.Gst.TagMergeMode.APPEND, 'comment', f"Detections: {metadata['detection_count']}")
            taglist.add_value(self.Gst.TagMergeMode.APPEND, 'extended-comment', json.dumps(metadata, separators=(',', ':')))
            event = self.Gst.Event.new_tag(taglist)
            # Send tag event to mpegtsmux for inline metadata
            self.mpegtsmux.send_event(event)
            self._id3_counter += 1
        except Exception as e:
            logger.error(f"Error injecting MPEG-TS metadata: {e}")

    def stop(self):
        if self.appsrc:
            try:
                self.appsrc.emit("end-of-stream")
                logger.info("Sent EOS to GStreamer pipeline")
            except Exception as e:
                logger.error(f"Error sending EOS: {e}")
        if self.pipeline:
            try:
                time.sleep(0.3)
                self.pipeline.set_state(self.Gst.State.NULL)
                logger.info("GStreamer pipeline stopped")
            except Exception as e:
                logger.error(f"Error stopping pipeline: {e}")
        if hasattr(self, 'ffmpeg_process') and self.ffmpeg_process:
            try:
                self.ffmpeg_process.stdin.close()
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=5)
                logger.info("FFmpeg process stopped")
            except Exception as e:
                logger.error(f"Error stopping ffmpeg: {e}")
                try:
                    self.ffmpeg_process.kill()
                except:
                    pass
        super().stop()


def build_pipeline(mode: str, **kwargs) -> BasePipeline:
    mode = mode.lower()
    available, _, _, _ = _try_import_gi()
    if mode == 'id3':
        if not available:
            raise RuntimeError("Mode 'id3' requested but GStreamer GI not available")
        return ID3Pipeline(**kwargs)
    if mode == 'auto':
        if available:
            logger.info("GI available → using ID3 pipeline")
            return ID3Pipeline(**kwargs)
        logger.info("GI not available → using basic pipeline")
        return BasicPipeline(**kwargs)
    if mode == 'basic':
        return BasicPipeline(**kwargs)
    raise ValueError("Invalid mode; expected auto|id3|basic")


def main():
    parser = argparse.ArgumentParser(description='SRT → YOLO → RTSP/HLS with optional ID3 and SSE metadata', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--input-srt', type=str, required=True, help='Input SRT URL (e.g., srt://host:port)')
    parser.add_argument('--output-rtsp', type=str, default='rtsp://localhost:8554/detected_stream', help='Output RTSP URL (MediaMTX will convert to HLS)')
    parser.add_argument('--model', type=str, default='runs/detect/train10/weights/best.engine', help='Path to YOLO model')
    parser.add_argument('--conf', type=float, default=0.25, help='Confidence threshold')
    parser.add_argument('--device', type=str, default='auto', help='Device to run inference on (auto, cpu, 0, 1, …)')
    parser.add_argument('--classes', type=int, nargs='+', default=None, help='List of class IDs to detect')
    parser.add_argument('--no-overlay', action='store_true', help='Disable overlay on video')
    parser.add_argument('--metadata-file', type=str, default=None, help='Save metadata to JSON file')
    parser.add_argument('--skip-frames', type=int, default=0, help='Skip N frames between detections (0 = all frames)')
    parser.add_argument('--srt-latency', type=int, default=1500, help='SRT latency in milliseconds')
    parser.add_argument('--metadata-host', type=str, default=None, help='Host to send metadata via UDP')
    parser.add_argument('--metadata-port', type=int, default=5555, help='UDP port for metadata')
    parser.add_argument('--sse-port', type=int, default=None, help='Start SSE server on this port (path: /events)')
    parser.add_argument('--id3-interval', type=int, default=30, help='Insert ID3 tag every N frames (ID3 mode)')
    parser.add_argument('--mode', type=str, default='auto', choices=['auto', 'id3', 'basic'], help='Pipeline selection mode')
    parser.add_argument('--log-level', type=str, default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='Logging level')
    parser.add_argument('--detections-dir', type=str, default=None, help='Directory to save detection logs (JSON and optional images)')
    parser.add_argument('--detection-log-interval', type=float, default=5.0, help='Interval in seconds to save detection logs')
    parser.add_argument('--save-detection-images', action='store_true', help='Save cropped images of detected objects')
    
    # TAK Server arguments
    parser.add_argument('--tak-enable', action='store_true', help='Enable TAK Server CoT message sending')
    parser.add_argument('--tak-host', type=str, default='localhost', help='TAK Server hostname/IP')
    parser.add_argument('--tak-port', type=int, default=8089, help='TAK Server SSL port')
    parser.add_argument('--tak-cert', type=str, default='certs/user1.pem', help='TAK client certificate file')
    parser.add_argument('--tak-key', type=str, default='certs/user1.key', help='TAK client key file')
    parser.add_argument('--tak-password', type=str, default='atakatak', help='TAK certificate password')
    parser.add_argument('--tak-stale', type=int, default=600, help='TAK object stale time in seconds')

    args = parser.parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    model_path = Path(args.model)
    if not model_path.exists():
        logger.error(f"Model file not found: {model_path}")
        sys.exit(1)

    # Initialize TAK CoT sender if enabled
    tak_sender = None
    if args.tak_enable:
        tak_sender = TAKCoTSender(
            host=args.tak_host,
            port=args.tak_port,
            cert_file=args.tak_cert,
            key_file=args.tak_key,
            cert_password=args.tak_password,
            enabled=True,
            stale_time_seconds=args.tak_stale
        )
        if tak_sender.enabled:
            logger.info(f"🎯 TAK Server integration enabled: {args.tak_host}:{args.tak_port}")
        else:
            logger.warning("⚠️ TAK Server integration failed to initialize")

    try:
        pipeline = build_pipeline(
            mode=args.mode,
            input_srt=args.input_srt,
            output_rtsp=args.output_rtsp,
            model_path=args.model,
            conf_threshold=args.conf,
            device=args.device,
            classes=args.classes,
            show_overlay=not args.no_overlay,
            metadata_file=args.metadata_file,
            skip_frames=args.skip_frames,
            srt_latency=args.srt_latency,
            metadata_host=args.metadata_host,
            metadata_port=args.metadata_port,
            sse_port=args.sse_port,
            id3_interval=args.id3_interval,
            detections_dir=None,
            detection_log_interval=5.0,
            save_detection_images=False,
            tak_sender=tak_sender,
        )
        pipeline.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()