import sys
import logging
import subprocess
import time
import json
import os

# Add system GStreamer plugins to plugin path for rtspclientsink
system_gst_plugins = '/usr/lib/x86_64-linux-gnu/gstreamer-1.0'
current_path = os.environ.get('GST_PLUGIN_PATH', '')
if system_gst_plugins not in current_path:
    os.environ['GST_PLUGIN_PATH'] = f"{system_gst_plugins}:{current_path}" if current_path else system_gst_plugins

logger = logging.getLogger("SRTYOLOUnified.RTSP")

def _try_import_gi():
    """Try to import GStreamer GI; return (available: bool, gi, Gst, GstApp)."""
    try:
        import gi  # type: ignore
        gi.require_version('Gst', '1.0')
        gi.require_version('GstApp', '1.0')
        from gi.repository import Gst, GstApp, GLib  # type: ignore
        Gst.init(None)
        return True, gi, Gst, GstApp
    except Exception as e:
        logger.debug(f"GStreamer GI not available: {e}")
        return False, None, None, None

class RTSPWriter:
    def write_frame(self, frame):
        raise NotImplementedError
    
    def inject_metadata(self, metadata):
        pass
    
    def close(self):
        raise NotImplementedError

class BasicRTSPWriter(RTSPWriter):
    def __init__(self, output_rtsp, width, height, fps):
        self.output_rtsp = output_rtsp
        self.width = width
        self.height = height
        self.fps = fps
        
        available, gi, Gst, GstApp = _try_import_gi()
        if not available:
            raise RuntimeError("GStreamer GI not available; cannot run Basic pipeline with direct GStreamer")
        self.Gst = Gst
        self.GstApp = GstApp
        
        logger.info("Creating direct GStreamer pipeline (Basic mode with native bindings)...")
        self._create_pipeline()
        
    def _create_pipeline(self):
        Gst = self.Gst
        pipeline = Gst.Pipeline.new("basic-rtsp-pipeline")

        # appsrc
        appsrc = Gst.ElementFactory.make("appsrc", "source")
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("do-timestamp", True)
        appsrc.set_property("block", True)
        caps = Gst.Caps.from_string(f"video/x-raw,format=BGR,width={self.width},height={self.height},framerate={int(self.fps)}/1")
        appsrc.set_property("caps", caps)

        # videoconvert
        videoconvert = Gst.ElementFactory.make("videoconvert", "convert")
        
        # Input queue
        input_queue = Gst.ElementFactory.make("queue", "input_queue")
        input_queue.set_property("max-size-time", 200000000)  # 200ms
        input_queue.set_property("leaky", "downstream")
        
        # x264enc with zero latency tuning
        x264enc = Gst.ElementFactory.make("x264enc", "encoder")
        x264enc.set_property("speed-preset", 1)  # "fast"
        x264enc.set_property("tune", 0x00000004)  # zerolatency
        x264enc.set_property("bitrate", 6000)
        x264enc.set_property("key-int-max", 60)
        x264enc.set_property("threads", 4)

        # h264parse
        h264parse = Gst.ElementFactory.make("h264parse", "parser")
        
        # Output queue
        output_queue = Gst.ElementFactory.make("queue", "output_queue")
        output_queue.set_property("max-size-time", 100000000)  # 100ms
        output_queue.set_property("leaky", "downstream")
        
        # rtspclientsink (from system plugins)
        rtspclientsink = Gst.ElementFactory.make("rtspclientsink", "sink")
        if not rtspclientsink:
            raise RuntimeError("rtspclientsink not available - check GST_PLUGIN_PATH")
        rtspclientsink.set_property("location", self.output_rtsp)
        rtspclientsink.set_property("protocols", "tcp")
        rtspclientsink.set_property("latency", 200)

        # Add all elements to pipeline
        for e in [appsrc, videoconvert, input_queue, x264enc, h264parse, output_queue, rtspclientsink]:
            if not e:
                raise RuntimeError(f"Failed to create GStreamer element: {e}")
            pipeline.add(e)

        # Link elements
        appsrc.link(videoconvert)
        videoconvert.link(input_queue)
        input_queue.link(x264enc)
        x264enc.link(h264parse)
        h264parse.link(output_queue)
        output_queue.link(rtspclientsink)

        # Start pipeline
        pipeline.set_state(Gst.State.PLAYING)

        self.pipeline = pipeline
        self.appsrc = appsrc
        self.frame_duration = int(Gst.SECOND / self.fps)
        self.gst_timestamp = 0
        logger.info("Pure GStreamer pipeline with rtspclientsink started successfully")

    def write_frame(self, frame):
        data = frame.tobytes()
        buf = self.Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts = self.gst_timestamp
        buf.duration = self.frame_duration
        self.gst_timestamp += self.frame_duration
        
        ret = self.appsrc.emit("push-buffer", buf)
        if ret != self.Gst.FlowReturn.OK:
            logger.warning(f"Error pushing buffer: {ret}")

    def close(self):
        if self.appsrc:
            try:
                self.appsrc.emit("end-of-stream")
            except Exception:
                pass
        if self.pipeline:
            try:
                time.sleep(0.3)
                self.pipeline.set_state(self.Gst.State.NULL)
            except Exception:
                pass

class ID3RTSPWriter(RTSPWriter):
    def __init__(self, output_rtsp, width, height, fps, id3_interval=30):
        self.output_rtsp = output_rtsp
        self.width = width
        self.height = height
        self.fps = fps
        self.id3_interval = id3_interval
        self.frame_count = 0
        
        available, gi, Gst, GstApp = _try_import_gi()
        if not available:
            raise RuntimeError("GStreamer GI not available; cannot run ID3 pipeline")
        self.Gst = Gst
        
        self._create_pipeline()
        
    def _create_pipeline(self):
        Gst = self.Gst
        pipeline = Gst.Pipeline.new("id3-pipeline")

        appsrc = Gst.ElementFactory.make("appsrc", "source")
        appsrc.set_property("format", self.Gst.Format.TIME)
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
        x264enc.set_property("bitrate", 6000)
        x264enc.set_property("key-int-max", 60)
        x264enc.set_property("threads", 4)
        # Tune for low latency but ensure compatibility with mpegtsmux
        x264enc.set_property("tune", 0x00000004)  # zerolatency

        h264parse = Gst.ElementFactory.make("h264parse", "parser")
        
        output_queue = Gst.ElementFactory.make("queue", "output_queue")
        output_queue.set_property("max-size-time", 100000000)  # 100ms
        output_queue.set_property("leaky", "downstream")
        
        mpegtsmux = Gst.ElementFactory.make("mpegtsmux", "mux")
        mpegtsmux.set_property("alignment", 7)
        
        # Use rtspclientsink instead of fdsink+ffmpeg
        rtspclientsink = Gst.ElementFactory.make("rtspclientsink", "sink")
        if not rtspclientsink:
            raise RuntimeError("rtspclientsink not available - check GST_PLUGIN_PATH")
        rtspclientsink.set_property("location", self.output_rtsp)
        rtspclientsink.set_property("protocols", "tcp")
        rtspclientsink.set_property("latency", 200)
        # mpegtsmux produces a stream that rtspclientsink can handle (video/mp2t)

        for e in [appsrc, videoconvert, capsfilter, videoscale, input_queue, encoder_queue, x264enc, h264parse, output_queue, mpegtsmux, rtspclientsink]:
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
        mpegtsmux.link(rtspclientsink)

        pipeline.set_state(self.Gst.State.PLAYING)

        self.pipeline = pipeline
        self.appsrc = appsrc
        self.mpegtsmux = mpegtsmux
        self.frame_duration = int(self.Gst.SECOND / self.fps)
        self.gst_timestamp = 0
        logger.info("ID3 pipeline started")

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
            # Send tag event to mpegtsmux sink pad (where video comes in)
            # This ensures tags are associated with the stream
            sink_pad = self.mpegtsmux.get_static_pad("sink_0")
            if not sink_pad:
                 # Try to find the pad linked to output_queue
                 sink_pad = self.mpegtsmux.iterate_sink_pads().next()[1]
            
            if sink_pad:
                ret = sink_pad.send_event(event)
                if not ret:
                    logger.warning("Failed to send ID3 tag event to sink pad")
            else:
                ret = self.mpegtsmux.send_event(event)
                if not ret:
                    logger.warning("Failed to send ID3 tag event to mpegtsmux")
        except Exception as e:
            logger.error(f"Error injecting MPEG-TS metadata: {e}")

    def close(self):
        if self.appsrc:
            try:
                self.appsrc.emit("end-of-stream")
            except Exception:
                pass
        if self.pipeline:
            try:
                time.sleep(0.3)
                self.pipeline.set_state(self.Gst.State.NULL)
            except Exception:
                pass
