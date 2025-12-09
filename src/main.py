import argparse
import logging
import sys
from pathlib import Path

from .core.pipeline import ThreadedPipeline
from .modules.tak import TAKCoTSender
from .modules.sse import SSEBroadcaster, start_sse_server

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SRTYOLOUnified")

# Suppress noisy logs
logging.getLogger('libav').setLevel(logging.CRITICAL)
logging.getLogger('libav.h264').setLevel(logging.CRITICAL)
import av
av.logging.set_level(av.logging.ERROR)

def main():
    parser = argparse.ArgumentParser(description='SRT → YOLO → RTSP/HLS with optional ID3 and SSE metadata', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--input-srt', type=str, required=True, help='Input SRT URL (e.g., srt://host:port)')
    parser.add_argument('--output-rtsp', type=str, default='rtsp://localhost:8554/detected_stream', help='Output RTSP URL (MediaMTX will convert to HLS)')
    parser.add_argument('--output-format', type=str, default='rtsp', choices=['rtsp', 'hls'], help='Output format: rtsp (stream) or hls (files)')
    parser.add_argument('--model', type=str, default='models/yolov8n.pt', help='Path to YOLO model')
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
    parser.add_argument('--output-webrtc', type=int, default=None, help='Start WebRTC signaling server on this port (e.g., 8080)')
    parser.add_argument('--output-mjpeg', type=int, default=None, help='Start MJPEG+SSE server on this port (e.g., 8080)')
    
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
        # sys.exit(1) # Allow to continue if model will be downloaded or is just a name

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

    # Initialize SSE Broadcaster if enabled
    sse_broadcaster = None
    if args.sse_port:
        import threading
        sse_broadcaster = SSEBroadcaster()
        stop_event = threading.Event()
        # Note: We need to handle stop_event properly, maybe pass it to pipeline
        start_sse_server(args.sse_port, sse_broadcaster, stop_event)

    try:
        pipeline = ThreadedPipeline(
            input_srt=args.input_srt,
            output_rtsp=args.output_rtsp, # Used as output_dir for HLS
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
            sse_broadcaster=sse_broadcaster,
            id3_interval=args.id3_interval,
            detections_dir=args.detections_dir,
            detection_log_interval=args.detection_log_interval,
            save_detection_images=args.save_detection_images,
            tak_sender=tak_sender,
            mode=args.mode,
            output_format=args.output_format,
            output_webrtc=args.output_webrtc,
            output_mjpeg=args.output_mjpeg
        )
        pipeline.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
