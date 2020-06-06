#!/usr/bin/env python3

#####################################################
##          librealsense D4xx to MAVLink           ##
#####################################################
# This script assumes pyrealsense2.[].so file is found under the same directory as this script
# Install required packages: 
#   pip install pyrealsense2
#   pip install transformations
#   pip3 install dronekit
#   pip3 install apscheduler

# Set the path for IDLE
import sys
sys.path.append("/usr/local/lib/")

# Set MAVLink protocol to 2.
import os
os.environ["MAVLINK20"] = "1"

# Import the libraries
import pyrealsense2 as rs
import numpy as np
import transformations as tf
import math as m
import time
import argparse
import threading
from time import sleep
from apscheduler.schedulers.background import BackgroundScheduler
from dronekit import connect, VehicleMode
from pymavlink import mavutil

from numba import njit              # pip install numba, or use anaconda to install

######################################################
##        Depth parameters - reconfigurable         ##
######################################################
STREAM_TYPE  = rs.stream.depth  # rs2_stream is a types of data provided by RealSense device
FORMAT       = rs.format.z16    # rs2_format is identifies how binary data is encoded within a frame
WIDTH        = 640              # Defines the number of columns for each frame or zero for auto resolve
HEIGHT       = 480              # Defines the number of lines for each frame or zero for auto resolve
FPS          = 30               # Defines the rate of frames per second
HEIGHT_RATIO = 20               # Defines the height ratio between the original frame to the new frame
WIDTH_RATIO  = 10               # Defines the width ratio between the original frame to the new frame
MAX_TXT_IMG_DEPTH = 1           # Approximate the coverage of pixels within this range (meter)
ROW_LENGTH   = int(WIDTH / WIDTH_RATIO)
pixels       = " .:nhBXWW"      # The text-based representation of depth

# Sensor-specific parameter, for D435: https://www.intelrealsense.com/depth-camera-d435/
HFOV        = 87
DEPTH_RANGE = [0.1, 8.0]        # Replace with your sensor's specifics, in meter
MIN_DEPTH_CM = int(DEPTH_RANGE[0] * 100)  # In cm
MAX_DEPTH_CM = int(DEPTH_RANGE[1] * 100)  # In cm, being a little conservative

# Obstacle distances in front of the sensor, starting from the left in increment degrees to the right
# See here: https://mavlink.io/en/messages/common.html#OBSTACLE_DISTANCE
DISTANCES_ARRAY_LEN = 72
angle_offset = -(HFOV / 2)  # In the case of a forward facing camera with a horizontal wide view:
increment_f  = HFOV / DISTANCES_ARRAY_LEN
distances = np.ones((DISTANCES_ARRAY_LEN,), dtype=np.uint16) * (MAX_DEPTH_CM + 1)

######################################################
##                    Parameters                    ##
######################################################

# Default configurations for connection to the FCU
connection_string_default = '/dev/ttyUSB0'
connection_baudrate_default = 921600
connection_timeout_sec_default = 5

camera_orientation_default = 0

# Enable/disable each message/function individually
enable_msg_obstacle_distance = True
obstacle_distance_msg_hz_default = 15

# Default global position for EKF home/ origin
enable_auto_set_ekf_home = False
home_lat = 151269321    # Somewhere random
home_lon = 16624301     # Somewhere random
home_alt = 163000       # Somewhere random

# TODO: Taken care of by ArduPilot, so can be removed (once the handling on AP side is confirmed stable)
# In NED frame, offset from the IMU or the center of gravity to the camera's origin point
body_offset_enabled = 0
body_offset_x = 0  # In meters (m)
body_offset_y = 0  # In meters (m)
body_offset_z = 0  # In meters (m)

# lock for thread synchronization
lock = threading.Lock()

debug_enable = True

#######################################
# Global variables
#######################################

# FCU connection variables
vehicle = None
is_vehicle_connected = False

# Camera-related variables
pipe = None
depth_scale = 0

# Data variables
data = None
current_time_us = 0

#######################################
# Parsing user' inputs
#######################################

parser = argparse.ArgumentParser(description='Reboots vehicle')
parser.add_argument('--connect',
                    help="Vehicle connection target string. If not specified, a default string will be used.")
parser.add_argument('--baudrate', type=float,
                    help="Vehicle connection baudrate. If not specified, a default value will be used.")
parser.add_argument('--obstacle_distance_msg_hz', type=float,
                    help="Update frequency for OBSTACLE_DISTANCE message. If not specified, a default value will be used.")

args = parser.parse_args()

connection_string = args.connect
connection_baudrate = args.baudrate
obstacle_distance_msg_hz = args.obstacle_distance_msg_hz

# Using default values if no specified inputs
if not connection_string:
    connection_string = connection_string_default
    print("INFO: Using default connection_string", connection_string)
else:
    print("INFO: Using connection_string", connection_string)

if not connection_baudrate:
    connection_baudrate = connection_baudrate_default
    print("INFO: Using default connection_baudrate", connection_baudrate)
else:
    print("INFO: Using connection_baudrate", connection_baudrate)
    
if not obstacle_distance_msg_hz:
    obstacle_distance_msg_hz = obstacle_distance_msg_hz_default
    print("INFO: Using default obstacle_distance_msg_hz", obstacle_distance_msg_hz)
else:
    print("INFO: Using obstacle_distance_msg_hz", obstacle_distance_msg_hz)

if not debug_enable:
    debug_enable = 0
else:
    debug_enable = 1
    np.set_printoptions(precision=4, suppress=True) # Format output on terminal 
    print("INFO: Debug messages enabled.")


#######################################
# Functions
#######################################

# https://mavlink.io/en/messages/common.html#OBSTACLE_DISTANCE
def send_obstacle_distance_message():
    global current_time_us, distances, MIN_DEPTH_CM, MAX_DEPTH_CM, increment_f, angle_offset
    msg = vehicle.message_factory.obstacle_distance_encode(
        current_time_us,    # us Timestamp (UNIX time or time since system boot)
        0,                  # sensor_type, defined here: https://mavlink.io/en/messages/common.html#MAV_DISTANCE_SENSOR
        distances,          # distances,    uint16_t[72],   cm
        0,                  # increment,    uint8_t,        deg
        MIN_DEPTH_CM,	    # min_distance, uint16_t,       cm
        MAX_DEPTH_CM,       # max_distance, uint16_t,       cm
        increment_f,	    # increment_f,  float,          deg
        angle_offset,       # angle_offset, float,          deg
        12                  # MAV_FRAME, vehicle-front aligned: https://mavlink.io/en/messages/common.html#MAV_FRAME_BODY_FRD    
    )

    vehicle.send_mavlink(msg)
    vehicle.flush()

# https://mavlink.io/en/messages/common.html#DISTANCE_SENSOR
# To-do: Possible extension for individual object detection
def send_distance_sensor_message():
    global current_time, distances

    # Average out a portion of the centermost part
    curr_dist = int(np.mean(distances[33:38]))

    msg = vehicle.message_factory.distance_sensor_encode(
        0,              # us Timestamp (UNIX time or time since system boot) (ignored)
        10,             # min_distance, uint16_t, cm
        65,             # min_distance, uint16_t, cm
        curr_dist,      # current_distance,	uint16_t, cm	
        0,	            # type : 0 (ignored)
        0,              # id : 0 (ignored)
        0,              # forward
        0               # covariance : 0 (ignored)
    )

    vehicle.send_mavlink(msg)
    vehicle.flush()

def send_msg_to_gcs(text_to_be_sent):
    # MAV_SEVERITY: 0=EMERGENCY 1=ALERT 2=CRITICAL 3=ERROR, 4=WARNING, 5=NOTICE, 6=INFO, 7=DEBUG, 8=ENUM_END
    # Defined here: https://mavlink.io/en/messages/common.html#MAV_SEVERITY
    # MAV_SEVERITY = 3 will let the message be displayed on Mission Planner HUD, but 6 is ok for QGroundControl
    if is_vehicle_connected == True:
        text_msg = 'D4xx: ' + text_to_be_sent
        status_msg = vehicle.message_factory.statustext_encode(
            6,                      # MAV_SEVERITY
            text_msg.encode()	    # max size is char[50]       
        )
        vehicle.send_mavlink(status_msg)
        vehicle.flush()
        print("INFO: " + text_to_be_sent)
    else:
        print("INFO: Vehicle not connected. Cannot send text message to Ground Control Station (GCS)")

# Send a mavlink SET_GPS_GLOBAL_ORIGIN message (http://mavlink.org/messages/common#SET_GPS_GLOBAL_ORIGIN), which allows us to use local position information without a GPS.
def set_default_global_origin():
    if is_vehicle_connected == True:
        msg = vehicle.message_factory.set_gps_global_origin_encode(
            int(vehicle._master.source_system),
            home_lat, 
            home_lon,
            home_alt
        )

        vehicle.send_mavlink(msg)
        vehicle.flush()

# Send a mavlink SET_HOME_POSITION message (http://mavlink.org/messages/common#SET_HOME_POSITION), which allows us to use local position information without a GPS.
def set_default_home_position():
    if is_vehicle_connected == True:
        x = 0
        y = 0
        z = 0
        q = [1, 0, 0, 0]   # w x y z

        approach_x = 0
        approach_y = 0
        approach_z = 1

        msg = vehicle.message_factory.set_home_position_encode(
            int(vehicle._master.source_system),
            home_lat, 
            home_lon,
            home_alt,
            x,
            y,
            z,
            q,
            approach_x,
            approach_y,
            approach_z
        )

        vehicle.send_mavlink(msg)
        vehicle.flush()

# Request a timesync update from the flight controller, for future work.
# TODO: Inspect the usage of timesync_update 
def update_timesync(ts=0, tc=0):
    if ts == 0:
        ts = int(round(time.time() * 1000))
    msg = vehicle.message_factory.timesync_encode(
        tc,     # tc1
        ts      # ts1
    )
    vehicle.send_mavlink(msg)
    vehicle.flush()

# Listen to attitude data to acquire heading when compass data is enabled
def att_msg_callback(self, attr_name, value):
    global heading_north_yaw
    if heading_north_yaw is None:
        heading_north_yaw = value.yaw
        print("INFO: Received first ATTITUDE message with heading yaw", heading_north_yaw * 180 / m.pi, "degrees")
    else:
        heading_north_yaw = value.yaw
        print("INFO: Received ATTITUDE message with heading yaw", heading_north_yaw * 180 / m.pi, "degrees")

def vehicle_connect():
    global vehicle, is_vehicle_connected
    
    try:
        vehicle = connect(connection_string, wait_ready = True, baud = connection_baudrate, source_system = 1)
    except:
        print('Connection error! Retrying...')
        sleep(1)

    if vehicle == None:
        is_vehicle_connected = False
        return False
    else:
        is_vehicle_connected = True
        return True

def realsense_connect():
    global pipe, depth_scale
    # Declare RealSense pipe, encapsulating the actual device and sensors
    pipe = rs.pipeline()

    # Build config object before requesting data
    cfg = rs.config()

    # Enable the stream we are interested in
    cfg.enable_stream(STREAM_TYPE, WIDTH, HEIGHT, FORMAT, FPS)

    # Start streaming with requested config
    profile = pipe.start(cfg)

    # Getting the depth sensor's depth scale (see rs-align example for explanation)
    depth_sensor = profile.get_device().first_depth_sensor()
    depth_scale = depth_sensor.get_depth_scale()
    print("INFO: Depth scale is: ", depth_scale)

# Monitor user input from the terminal and perform action accordingly
def user_input_monitor():
    global scale_factor
    while True:
        if enable_auto_set_ekf_home:
            send_msg_to_gcs('Set EKF home with default GPS location')
            set_default_global_origin()
            set_default_home_position()
            time.sleep(1) # Wait a short while for FCU to start working

        # Add new action here according to the key pressed.
        # Enter: Set EKF home when user press enter
        try:
            c = input()
            if c == "":
                send_msg_to_gcs('Set EKF home with default GPS location')
                set_default_global_origin()
                set_default_home_position()
            else:
                print("Got keyboard input", c)
        except IOError: pass

# Calculate a simple text-based representation of the image, by breaking it into WIDTH_RATIO x HEIGHT_RATIO pixel regions and approximating the coverage of pixels within MAX_TXT_IMG_DEPTH
@njit
def calculate_depth_txt_img(depth_mat):
    img_txt = ""
    coverage = [0] * ROW_LENGTH
    for y in range(HEIGHT):
        # Create a depth histogram for each row
        for x in range(WIDTH):
            dist = depth_mat[y,x] * depth_scale
            if 0 < dist and dist < MAX_TXT_IMG_DEPTH:
                coverage[x // WIDTH_RATIO] += 1

        if y % HEIGHT_RATIO is (HEIGHT_RATIO - 1):
            line = ""
            for c in coverage:
                pixel_index = c // int(HEIGHT_RATIO * WIDTH_RATIO / (len(pixels) - 1))  # Magic number: c // 25
                line += pixels[pixel_index]
            coverage = [0] * ROW_LENGTH
            img_txt += line + "\n"
            
    return img_txt

@njit
def calculate_distances_from_depth(depth_mat, distances, min_depth_m, max_depth_m):
    step = WIDTH / DISTANCES_ARRAY_LEN
    radius = step / 2
    for i in range(DISTANCES_ARRAY_LEN):

        x_pixel_range_lower_bound = i * step - step / 2
        if x_pixel_range_lower_bound < 0:
            x_pixel_range_lower_bound = 0
        
        x_pixel_range_upper_bound = i * step + step / 2
        if x_pixel_range_upper_bound > WIDTH - 1:
            x_pixel_range_upper_bound = WIDTH - 1
        
        y_pixel_range_lower_bound = HEIGHT / 2 - step / 2
        y_pixel_range_upper_bound = HEIGHT / 2 + step / 2

        # dist_m = np.mean(depth_mat[int(y_pixel_range_lower_bound):int(y_pixel_range_upper_bound), int(x_pixel_range_lower_bound):int(x_pixel_range_upper_bound)]) * depth_scale
        dist_m = np.mean(depth_mat[int(HEIGHT/2), int(x_pixel_range_lower_bound):int(x_pixel_range_upper_bound)]) * depth_scale
        # dist_m = depth_mat[int(HEIGHT/2), int(i * step)] * depth_scale

        if dist_m < min_depth_m or dist_m > max_depth_m:
            distances[i] = max_depth_m * 100 + 1
        else:
            distances[i] = dist_m * 100

#######################################
# Main code starts here
#######################################

print("INFO: Connecting to vehicle.")
while (not vehicle_connect()):
    pass
print("INFO: Vehicle connected.")

send_msg_to_gcs('Connecting to camera...')
realsense_connect()
send_msg_to_gcs('Camera connected.')

# Send MAVlink messages in the background at pre-determined frequencies
sched = BackgroundScheduler()

if enable_msg_obstacle_distance:
    sched.add_job(send_obstacle_distance_message, 'interval', seconds = 1/obstacle_distance_msg_hz)

# A separate thread to monitor user input
user_keyboard_input_thread = threading.Thread(target=user_input_monitor)
user_keyboard_input_thread.daemon = True
user_keyboard_input_thread.start()

sched.start()

if enable_msg_obstacle_distance:
    send_msg_to_gcs('Sending obstacle distance messages to FCU')
else:
    send_msg_to_gcs('Nothing to do. Check params to enable something')
    pipe.stop()
    vehicle.close()
    print("INFO: Realsense pipe and vehicle object closed.")
    sys.exit()

print("INFO: Press Enter to set EKF home at default location")

# Filters to be applied
decimation = rs.decimation_filter()
decimation.set_option(rs.option.filter_magnitude, 4)

spatial = rs.spatial_filter()
# spatial.set_option(rs.option.filter_magnitude, 5)
# spatial.set_option(rs.option.filter_smooth_alpha, 1)
# spatial.set_option(rs.option.filter_smooth_delta, 50)
# spatial.set_option(rs.option.holes_fill, 3)

hole_filling = rs.hole_filling_filter()

depth_to_disparity = rs.disparity_transform(True)
disparity_to_depth = rs.disparity_transform(False)

# Begin of the main loop
last_time = time.time()
try:
    while True:
        # This call waits until a new coherent set of frames is available on a device
        # Calls to get_frame_data(...) and get_frame_timestamp(...) on a device will return stable values until wait_for_frames(...) is called
        frames = pipe.wait_for_frames()
        depth_frame = frames.get_depth_frame()

        if not depth_frame:
            continue

        # Store the timestamp for MAVLink messages
        current_time_us = int(round(time.time() * 1000000))

        filtered_depth = depth_frame
        # Apply the filter(s) according to this recommendation: https://github.com/IntelRealSense/librealsense/blob/master/doc/post-processing-filters.md#using-filters-in-application-code
        # filtered_depth = decimation.process(filtered_depth)
        # filtered_depth = depth_to_disparity.process(filtered_depth)
        # filtered_depth = spatial.process(filtered_depth)
        # filtered_depth = disparity_to_depth.process(filtered_depth)
        filtered_depth = hole_filling.process(filtered_depth)

        # Extract depth in matrix form
        depth_data = filtered_depth.as_frame().get_data()
        depth_mat = np.asanyarray(depth_data)

        # Create obstacle distance data from depth image
        calculate_distances_from_depth(depth_mat, distances, DEPTH_RANGE[0], DEPTH_RANGE[1])

        if debug_enable:
            img_txt = calculate_depth_txt_img(depth_mat)
            print(img_txt)
            
            # Print some debugging messages
            processing_time = time.time() - last_time
            print("Text-based depth img within %.3f meter" % MAX_TXT_IMG_DEPTH)
            print("Processing time per image: %.3f sec" % processing_time)
            if processing_time > 0:
                print("Processing freq per image: %.3f Hz" % (1/processing_time))
            last_time = time.time()

            # Print all the distances in a line
            # print(*distances)

except KeyboardInterrupt:
    send_msg_to_gcs('Closing the script...')  

except Exception as e:
    print(e)
    pass

except:
    send_msg_to_gcs('ERROR: Depth camera disconnected')  

finally:
    pipe.stop()
    vehicle.close()
    print("INFO: Realsense pipe and vehicle object closed.")
    sys.exit()
