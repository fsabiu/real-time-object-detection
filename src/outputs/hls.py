import sys
import logging
import time
import json
import os
import struct
from .rtsp import RTSPWriter, _try_import_gi

logger = logging.getLogger("SRTYOLOUnified.HLS")

def create_id3v2_frame(json_data: str) -> bytes:
    """
    Create an ID3v2.4 tag with a TXXX frame containing the JSON data.
    This format is recognized by hls.js and other HLS players.
    """
    # TXXX frame:
    # - Frame ID: "TXXX" (4 bytes)
    # - Size: syncsafe integer (4 bytes)
    # - Flags: 0x0000 (2 bytes)
    # - Encoding: 0x03 = UTF-8 (1 byte)
    # - Description: null-terminated string
    # - Value: the JSON data
    
    description = b"detector_metadata\x00"
    value = json_data.encode('utf-8')
    frame_content = b'\x03' + description + value  # 0x03 = UTF-8 encoding
    
    frame_size = len(frame_content)
    # Syncsafe integer for frame size (ID3v2.4)
    syncsafe_frame_size = (
        ((frame_size >> 21) & 0x7F) << 24 |
        ((frame_size >> 14) & 0x7F) << 16 |
        ((frame_size >> 7) & 0x7F) << 8 |
        (frame_size & 0x7F)
    )
    
    frame = b'TXXX' + struct.pack('>I', syncsafe_frame_size) + b'\x00\x00' + frame_content
    
    # ID3v2.4 header:
    # - "ID3" (3 bytes)
    # - Version: 0x04 0x00 (2 bytes) = ID3v2.4
    # - Flags: 0x00 (1 byte)
    # - Size: syncsafe integer (4 bytes)
    
    tag_size = len(frame)
    syncsafe_tag_size = (
        ((tag_size >> 21) & 0x7F) << 24 |
        ((tag_size >> 14) & 0x7F) << 16 |
        ((tag_size >> 7) & 0x7F) << 8 |
        (tag_size & 0x7F)
    )
    
    header = b'ID3\x04\x00\x00' + struct.pack('>I', syncsafe_tag_size)
    
    return header + frame


class HLSWriter(RTSPWriter):
    def __init__(self, output_dir, width, height, fps, id3_interval=30):
        self.output_dir = output_dir
        self.width = width
        self.height = height
        self.fps = fps
        self.id3_interval = id3_interval
        self.frame_count = 0
        
        # Ensure output directory exists
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
            
        available, gi, Gst, GstApp = _try_import_gi()
        if not available:
            raise RuntimeError("GStreamer GI not available; cannot run HLS pipeline")
        self.Gst = Gst
        
        self._create_pipeline()
        
    def _create_pipeline(self):
        Gst = self.Gst
        pipeline = Gst.Pipeline.new("hls-pipeline")

        appsrc = Gst.ElementFactory.make("appsrc", "source")
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("do-timestamp", True)
        appsrc.set_property("block", True)
        caps = Gst.Caps.from_string(f"video/x-raw,format=BGR,width={self.width},height={self.height},framerate={int(self.fps)}/1")
        appsrc.set_property("caps", caps)

        videoconvert = Gst.ElementFactory.make("videoconvert", "convert")
        
        # Force YUV420P for compatibility
        capsfilter = Gst.ElementFactory.make("capsfilter", "capsfilter")
        caps = Gst.Caps.from_string("video/x-raw,format=I420")
        capsfilter.set_property("caps", caps)
        
        videoscale = Gst.ElementFactory.make("videoscale", "scale")
        
        input_queue = Gst.ElementFactory.make("queue", "input_queue")
        input_queue.set_property("max-size-time", 200000000)  # 200ms
        input_queue.set_property("leaky", "downstream")
        
        encoder_queue = Gst.ElementFactory.make("queue", "encoder_queue")
        encoder_queue.set_property("max-size-time", 1000000000)  # 1 second buffer
        encoder_queue.set_property("leaky", "downstream")
        
        x264enc = Gst.ElementFactory.make("x264enc", "encoder")
        x264enc.set_property("speed-preset", "fast")
        x264enc.set_property("bitrate", 4000) # Lower bitrate for HLS
        x264enc.set_property("key-int-max", int(self.fps * 2)) # 2 second GOP for HLS
        x264enc.set_property("threads", 4)
        x264enc.set_property("tune", 0x00000004)  # zerolatency

        h264parse = Gst.ElementFactory.make("h264parse", "parser")
        
        output_queue = Gst.ElementFactory.make("queue", "output_queue")
        output_queue.set_property("max-size-time", 100000000)  # 100ms
        output_queue.set_property("leaky", "downstream")
        
        mpegtsmux = Gst.ElementFactory.make("mpegtsmux", "mux")
        mpegtsmux.set_property("alignment", 7)
        
        # hlssink
        hlssink = Gst.ElementFactory.make("hlssink", "sink")
        if not hlssink:
            raise RuntimeError("hlssink not available - check GST_PLUGIN_PATH")
            
        # Configure hlssink
        hlssink.set_property("location", os.path.join(self.output_dir, "segment%05d.ts"))
        hlssink.set_property("playlist-location", os.path.join(self.output_dir, "index.m3u8"))
        hlssink.set_property("target-duration", 2) # 2 second segments for low latency
        hlssink.set_property("max-files", 5) # Keep last 5 segments
        hlssink.set_property("playlist-length", 3)
        
        # KLV Metadata Source - using meta/x-klv caps
        # Note: mpegtsmux doesn't support ID3 caps directly, but KLV works
        meta_appsrc = Gst.ElementFactory.make("appsrc", "meta_source")
        meta_appsrc.set_property("format", Gst.Format.TIME)
        meta_appsrc.set_property("is-live", True)
        meta_appsrc.set_property("do-timestamp", True)
        # meta/x-klv creates a data stream in MPEG-TS that can be parsed by the player
        meta_caps = Gst.Caps.from_string("meta/x-klv, parsed=(boolean)true") 
        meta_appsrc.set_property("caps", meta_caps)

        for e in [appsrc, videoconvert, capsfilter, videoscale, input_queue, encoder_queue, x264enc, h264parse, output_queue, mpegtsmux, hlssink, meta_appsrc]:
            if not e:
                raise RuntimeError(f"Failed to create GStreamer element: {e}")
            pipeline.add(e)

        appsrc.link(videoconvert)
        videoconvert.link(capsfilter)
        capsfilter.link(videoscale)
        videoscale.link(input_queue)
        input_queue.link(encoder_queue)
        encoder_queue.link(x264enc)
        x264enc.link(h264parse)
        h264parse.link(output_queue)
        output_queue.link(mpegtsmux)
        
        # Link metadata source to mpegtsmux
        meta_appsrc.link(mpegtsmux)
        
        mpegtsmux.link(hlssink)

        pipeline.set_state(Gst.State.PLAYING)

        self.pipeline = pipeline
        self.appsrc = appsrc
        self.meta_appsrc = meta_appsrc
        self.mpegtsmux = mpegtsmux
        self.frame_duration = int(self.Gst.SECOND / self.fps)
        self.gst_timestamp = 0
        logger.info(f"HLS pipeline started with ID3 metadata. Output: {self.output_dir}")

    def write_frame(self, frame):
        # Check for bus messages
        bus = self.pipeline.get_bus()
        while True:
            msg = bus.pop()
            if not msg:
                break
            t = msg.type
            if t == self.Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                logger.error(f"GStreamer Pipeline Error: {err}: {debug}")
            elif t == self.Gst.MessageType.WARNING:
                err, debug = msg.parse_warning()
                logger.warning(f"GStreamer Pipeline Warning: {err}: {debug}")

        self.frame_count += 1
        data = frame.tobytes()
        buf = self.Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts = self.gst_timestamp
        buf.duration = self.frame_duration
        self.gst_timestamp += self.frame_duration
        
        ret = self.appsrc.emit("push-buffer", buf)
        if ret != self.Gst.FlowReturn.OK:
            logger.warning(f"Error pushing buffer: {ret}")

    def inject_metadata(self, metadata):
        # Log every call to trace the issue
        should_inject = (self.frame_count % max(1, self.id3_interval) == 0)
        
        if not should_inject:
            return
        
        try:
            # Create JSON payload - push raw JSON to KLV stream
            json_str = json.dumps(metadata, separators=(',', ':'))
            data = json_str.encode('utf-8')
            
            # Create buffer
            buf = self.Gst.Buffer.new_allocate(None, len(data), None)
            buf.fill(0, data)
            
            # Sync timestamp with video  
            buf.pts = self.gst_timestamp
            buf.duration = self.frame_duration
            
            # Push to metadata appsrc
            ret = self.meta_appsrc.emit("push-buffer", buf)
            if ret != self.Gst.FlowReturn.OK:
                logger.warning(f"Error pushing KLV metadata buffer: {ret}")
            else:
                logger.info(f"KLV Metadata injected: frame={metadata.get('frame', '?')}, size={len(data)}B, pts={buf.pts}")
                
        except Exception as e:
            logger.error(f"Error injecting KLV metadata: {e}")

    def close(self):
        if self.appsrc:
            try:
                self.appsrc.emit("end-of-stream")
            except Exception:
                pass
        if self.meta_appsrc:
            try:
                self.meta_appsrc.emit("end-of-stream")
            except Exception:
                pass
        if self.pipeline:
            try:
                time.sleep(0.3)
                self.pipeline.set_state(self.Gst.State.NULL)
            except Exception:
                pass
