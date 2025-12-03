import struct
import logging

logger = logging.getLogger("SRTYOLOUnified.KLV")

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
                    elif tag == 5:
                        # Platform Roll (degrees, 2-byte signed int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>h', value_bytes)[0]
                            telemetry['roll'] = scaled / 100.0
                    elif tag == 6:
                        # Platform Pitch (degrees, 2-byte signed int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>h', value_bytes)[0]
                            telemetry['pitch'] = scaled / 100.0
                    elif tag == 7:
                        # Platform Heading (degrees, 2-byte unsigned int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>H', value_bytes)[0]
                            telemetry['heading'] = scaled / 100.0
                    elif tag == 13:
                        # Sensor Latitude (degrees, 4-byte signed int scaled by 1e7)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['latitude'] = scaled / 1e7
                    elif tag == 14:
                        # Sensor Longitude (degrees, 4-byte signed int scaled by 1e7)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['longitude'] = scaled / 1e7
                    elif tag == 15:
                        # Sensor Altitude (meters, 2-byte unsigned int scaled by 10)
                        if item_length == 2:
                            scaled = struct.unpack('>H', value_bytes)[0]
                            telemetry['altitude'] = scaled / 10.0
                    elif tag == 18:
                        # Sensor Horizontal Field of View (degrees, 2-byte unsigned int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>H', value_bytes)[0]
                            telemetry['sensor_h_fov'] = scaled / 100.0
                    elif tag == 19:
                        # Sensor Vertical Field of View (degrees, 2-byte unsigned int scaled by 100)
                        if item_length == 2:
                            scaled = struct.unpack('>H', value_bytes)[0]
                            telemetry['sensor_v_fov'] = scaled / 100.0
                    elif tag == 21:
                        # Sensor Relative Roll / Gimbal Roll Relative (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_roll_rel'] = scaled / 1e6
                    elif tag == 22:
                        # Sensor Relative Pitch / Gimbal Pitch Relative (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_pitch_rel'] = scaled / 1e6
                    elif tag == 23:
                        # Sensor Relative Yaw / Gimbal Yaw Relative (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_yaw_rel'] = scaled / 1e6
                    elif tag == 102:
                        # Sensor Width (millimeters, 4-byte float)
                        if item_length == 4:
                            telemetry['sensor_width_mm'] = struct.unpack('>f', value_bytes)[0]
                    elif tag == 103:
                        # Sensor Height (millimeters, 4-byte float)
                        if item_length == 4:
                            telemetry['sensor_height_mm'] = struct.unpack('>f', value_bytes)[0]
                    elif tag == 104:
                        # Focal Length (millimeters, 4-byte float)
                        if item_length == 4:
                            telemetry['focal_length_mm'] = struct.unpack('>f', value_bytes)[0]
                    elif tag == 105:
                        # Gimbal Absolute Yaw (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_yaw_abs'] = scaled / 1e6
                    elif tag == 106:
                        # Gimbal Absolute Pitch (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_pitch_abs'] = scaled / 1e6
                    elif tag == 107:
                        # Gimbal Absolute Roll (degrees, 4-byte signed int scaled by 1e6)
                        if item_length == 4:
                            scaled = struct.unpack('>i', value_bytes)[0]
                            telemetry['gimbal_roll_abs'] = scaled / 1e6
                except struct.error:
                    continue

            return telemetry
        except Exception as e:
            logger.debug(f"KLV decode error: {e}")
            return None
