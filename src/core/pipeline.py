import threading
import queue
import time
import logging
import av
import cv2
import numpy as np
from datetime import datetime
from collections import deque
from ultralytics import YOLO

from ..modules.klv import KLVDecoder
from ..modules.geo import calculate_object_coordinates
from ..modules.drawing import draw_detections_vectorized, overlay_metadata
from ..outputs.rtsp import BasicRTSPWriter, ID3RTSPWriter, _try_import_gi
from ..outputs.hls import HLSWriter

logger = logging.getLogger("SRTYOLOUnified.Pipeline")

class FrameData:
    def __init__(self, frame, timestamp, klv_data, frame_count):
        self.frame = frame
        self.timestamp = timestamp
        self.klv_data = klv_data
        self.frame_count = frame_count
        self.detections = []
        self.metadata = {}
        self.annotated_frame = None
        self.timings = {
            'capture_start': time.time(),
            'inference_ms': 0,
            'drawing_ms': 0,
            'write_ms': 0,
            'total_ms': 0
        }

class ThreadedPipeline:
    def __init__(self, input_srt, output_rtsp, model_path, conf_threshold=0.25,
                 device='auto', classes=None, show_overlay=True,
                 metadata_file=None, skip_frames=0, srt_latency=500,
                 metadata_host=None, metadata_port=5555,
                 sse_broadcaster=None, id3_interval=30,
                 detections_dir=None, detection_log_interval=5.0, save_detection_images=True,
                 tak_sender=None, mode='auto', output_format='rtsp'):
        
        self.input_srt = input_srt
        self.output_rtsp = output_rtsp
        self.output_format = output_format
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
        self.sse_broadcaster = sse_broadcaster
        self.id3_interval = id3_interval
        self.detections_dir = detections_dir
        self.detection_log_interval = detection_log_interval
        self.save_detection_images = save_detection_images
        self.tak_sender = tak_sender
        self.mode = mode

        self.running = False
        self.stop_event = threading.Event()
        
        # Queues
        self.inference_queue = queue.Queue(maxsize=2)
        self.output_queue = queue.Queue(maxsize=2)
        
        # State
        self.latest_klv = {}
        self.frame_count = 0
        self.processed_count = 0
        self.klv_count = 0
        self.detection_count = 0
        self.start_time = None
        
        # Components
        self.container = None
        self.model = None
        self.writer = None
        self.klv_decoder = KLVDecoder()
        
        # UDP Socket
        self.metadata_socket = None
        if self.metadata_host:
            import socket
            self.metadata_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _load_model(self):
        logger.info(f"Loading YOLO model: {self.model_path}")
        self.model = YOLO(self.model_path)
        # Warmup
        # self.model(np.zeros((640, 640, 3), dtype=np.uint8), verbose=False)

    def _open_srt(self):
        logger.info(f"Opening SRT stream: {self.input_srt}")
        options = {
            'latency': str(self.srt_latency * 1000),
            'recv_buffer_size': '8388608',
        }
        self.container = av.open(self.input_srt, options=options)

    def _capture_thread(self):
        logger.info("Starting capture thread")
        try:
            video_stream = None
            for stream in self.container.streams:
                if stream.type == 'video':
                    video_stream = stream
                    break
            
            if not video_stream:
                raise RuntimeError("No video stream found")

            # Initialize writer in the capture thread (or main thread) once we know dims
            width = video_stream.width
            height = video_stream.height
            fps = float(video_stream.average_rate) if video_stream.average_rate else 30.0
            
            # Signal that we are ready to initialize writer
            self._init_writer(width, height, fps)

            for packet in self.container.demux():
                if self.stop_event.is_set():
                    break
                
                if packet.stream.type == 'data':
                    self.klv_count += 1
                    data = bytes(packet)
                    decoded = self.klv_decoder.decode(data)
                    if decoded:
                        self.latest_klv = decoded
                
                elif packet.stream.type == 'video':
                    try:
                        frames = packet.decode()
                        for frame in frames:
                            self.frame_count += 1
                            
                            # Skip frames logic if needed at capture level
                            if self.skip_frames > 0 and self.frame_count % (self.skip_frames + 1) != 1:
                                continue

                            img = frame.to_ndarray(format='bgr24')
                            frame_data = FrameData(img, time.time(), self.latest_klv.copy(), self.frame_count)
                            
                            # Leaky put
                            try:
                                self.inference_queue.put_nowait(frame_data)
                            except queue.Full:
                                try:
                                    self.inference_queue.get_nowait() # Drop old
                                    self.inference_queue.put_nowait(frame_data)
                                except:
                                    pass
                    except Exception as e:
                        # logger.warning(f"Decode error: {e}")
                        pass
        except Exception as e:
            logger.error(f"Capture thread error: {e}")
            self.stop_event.set()

    def _inference_thread(self):
        logger.info("Starting inference thread")
        while not self.stop_event.is_set():
            try:
                frame_data = self.inference_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            
            try:
                t0 = time.time()
                # Run Inference
                # Use track mode for persistence
                results = self.model.track(frame_data.frame, conf=self.conf_threshold, 
                                         persist=True, verbose=False, tracker="bytetrack.yaml")
                frame_data.timings['inference_ms'] = (time.time() - t0) * 1000
                
                detections = []
                if results and len(results) > 0:
                    r = results[0]
                    if r.boxes:
                        for box in r.boxes:
                            cls_id = int(box.cls[0].item())
                            conf = float(box.conf[0].item())
                            x1, y1, x2, y2 = box.xyxy[0].tolist()
                            class_name = r.names.get(cls_id, f"class_{cls_id}")
                            
                            det = {
                                'bbox': [x1, y1, x2, y2],
                                'class_name': class_name,
                                'confidence': conf,
                                'class_id': cls_id
                            }
                            if box.id is not None:
                                det['track_id'] = int(box.id.item())
                            
                            detections.append(det)
                
                frame_data.detections = detections
                self.detection_count += len(detections)
                
                # Leaky put to output
                try:
                    self.output_queue.put_nowait(frame_data)
                except queue.Full:
                    try:
                        self.output_queue.get_nowait()
                        self.output_queue.put_nowait(frame_data)
                    except:
                        pass
                        
            except Exception as e:
                logger.error(f"Inference error: {e}")

    def _output_thread(self):
        logger.info("Starting output thread")
        while not self.stop_event.is_set():
            try:
                frame_data = self.output_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            
            try:
                t_draw_start = time.time()
                # 1. Calculate Coordinates
                h, w = frame_data.frame.shape[:2]
                enriched_detections = []
                for det in frame_data.detections:
                    enriched = det.copy()
                    if frame_data.klv_data:
                        coords = calculate_object_coordinates(det['bbox'], frame_data.klv_data, w, h)
                        if coords:
                            enriched['geo_coordinates'] = coords
                            
                            # Send to TAK
                            if self.tak_sender:
                                self.tak_sender.send_detection(enriched, frame_data.frame_count)
                    enriched_detections.append(enriched)
                
                # 2. Prepare Metadata
                metadata = {
                    'frame': frame_data.frame_count,
                    'timestamp': datetime.fromtimestamp(frame_data.timestamp).isoformat(),
                    'telemetry': frame_data.klv_data,
                    'detections': enriched_detections,
                    'detection_count': len(enriched_detections)
                }
                
                # 3. Draw Overlay
                if self.show_overlay:
                    frame_data.annotated_frame = draw_detections_vectorized(frame_data.frame, enriched_detections)
                    frame_data.annotated_frame = overlay_metadata(frame_data.annotated_frame, frame_data.frame_count, 
                                                                frame_data.klv_data, enriched_detections, 0.0) # FPS TODO
                else:
                    frame_data.annotated_frame = frame_data.frame
                
                frame_data.timings['drawing_ms'] = (time.time() - t_draw_start) * 1000
                
                # 4. Write Output
                t_write_start = time.time()
                if self.writer:
                    self.writer.inject_metadata(metadata)
                    self.writer.write_frame(frame_data.annotated_frame)
                frame_data.timings['write_ms'] = (time.time() - t_write_start) * 1000
                
                # 5. Broadcast Metadata
                if self.metadata_socket:
                    import json
                    try:
                        self.metadata_socket.sendto(json.dumps(metadata, default=str).encode('utf-8'), 
                                                  (self.metadata_host, self.metadata_port))
                    except:
                        pass
                
                if self.sse_broadcaster:
                    import json
                    try:
                        self.sse_broadcaster.publish(json.dumps(metadata, default=str, separators=(',', ':')))
                    except:
                        pass
                
                # Log performance
                self.processed_count += 1
                total_ms = (time.time() - frame_data.timings['capture_start']) * 1000
                if self.processed_count % 30 == 0:
                    logger.info(f"Frame {frame_data.frame_count}: Total={total_ms:.1f}ms | Inf={frame_data.timings['inference_ms']:.1f}ms | Draw={frame_data.timings['drawing_ms']:.1f}ms | Write={frame_data.timings['write_ms']:.1f}ms | Detections={len(enriched_detections)}")
                        
            except Exception as e:
                logger.error(f"Output error: {e}")

    def _init_writer(self, width, height, fps):
        if self.writer:
            return
        
        logger.info(f"Initializing writer: {width}x{height} @ {fps}fps (Format: {self.output_format})")
        
        if self.output_format == 'hls':
            self.writer = HLSWriter(self.output_rtsp, width, height, fps, self.id3_interval)
            return

        if self.mode == 'id3':
            self.writer = ID3RTSPWriter(self.output_rtsp, width, height, fps, self.id3_interval)
        elif self.mode == 'basic':
            self.writer = BasicRTSPWriter(self.output_rtsp, width, height, fps)
        elif self.mode == 'auto':
            available, _, _, _ = _try_import_gi()
            if available:
                logger.info("Auto mode: GI available, using ID3 pipeline")
                self.writer = ID3RTSPWriter(self.output_rtsp, width, height, fps, self.id3_interval)
            else:
                logger.info("Auto mode: GI not available, using Basic pipeline")
                self.writer = BasicRTSPWriter(self.output_rtsp, width, height, fps)

    def run(self):
        self._load_model()
        self._open_srt()
        
        self.running = True
        
        t_cap = threading.Thread(target=self._capture_thread, daemon=True)
        t_inf = threading.Thread(target=self._inference_thread, daemon=True)
        t_out = threading.Thread(target=self._output_thread, daemon=True)
        
        t_cap.start()
        # Wait for writer init (simple synchronization)
        time.sleep(2) 
        t_inf.start()
        t_out.start()
        
        try:
            while self.running and not self.stop_event.is_set():
                time.sleep(1)
                if not t_cap.is_alive():
                    logger.error("Capture thread died")
                    break
        except KeyboardInterrupt:
            logger.info("Stopping...")
        finally:
            self.stop_event.set()
            if self.writer:
                self.writer.close()
            if self.container:
                self.container.close()
            if self.metadata_socket:
                self.metadata_socket.close()
