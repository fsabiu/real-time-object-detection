import math
import logging

logger = logging.getLogger("SRTYOLOUnified.Geo")

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
    try:
        # --- Validate required KLV fields ---
        required_fields = ['latitude', 'longitude', 'altitude']
        missing_fields = [f for f in required_fields if f not in klv_data or klv_data[f] is None]

        if missing_fields:
            # logger.debug(f"Missing required fields for coordinate calculation: {missing_fields}")
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
        elif has_relative:
            gimbal_yaw_rel = klv_data.get('gimbal_yaw_rel', 0.0)
            gimbal_pitch_rel = klv_data.get('gimbal_pitch_rel', -90.0)
            gimbal_roll_rel = klv_data.get('gimbal_roll_rel', 0.0)

            # Approximate world transformation
            gimbal_yaw_world = platform_heading + gimbal_yaw_rel
            gimbal_pitch_world = gimbal_pitch_rel + platform_pitch
            gimbal_roll_world = gimbal_roll_rel + platform_roll
            gimbal_method = "relative_approx_transform"
        else:
            gimbal_yaw_world = platform_heading
            gimbal_pitch_world = -90.0
            gimbal_roll_world = 0.0
            gimbal_method = "fallback_nadir"

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

        # --- Compute ground intersection ---
        if camera_elevation >= 0:
            # Looking above the horizon, no ground intersection
            return None

        # Convert to downward angle (from horizontal)
        look_down_angle = abs(camera_elevation)

        # Prevent extreme values for near-horizontal shots
        if look_down_angle < 5:
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
