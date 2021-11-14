#!/usr/bin/env python3
import argparse
import carla  # pylint: disable=import-error
import math
import numpy as np
import time
import threading
from cereal import log
import os
from typing import Any
import cv2
from multiprocessing import Process, Queue
from cereal.visionipc.visionipc_pyx import VisionIpcServer, VisionStreamType  # pylint: disable=no-name-in-module, import-error
from panda import Panda
from opendbc.can.packer import CANPacker

import cereal.messaging as messaging
from common.params import Params
from common.numpy_fast import clip
from common.realtime import DT_CTRL, Ratekeeper, DT_DMON
# from lib.can import can_function
from lib.can_hyundai import can_function
# from selfdrive.car.honda.values import CruiseButtons
from selfdrive.car.hyundai.values import Buttons
from selfdrive.test.helpers import set_params_enabled

packer = CANPacker("hyundai_kia_generic")
parser = argparse.ArgumentParser(description='Bridge between CARLA and openpilot.')
parser.add_argument('--joystick', action='store_true')
parser.add_argument('--low_quality', action='store_true')
parser.add_argument('--town', type=str, default='Town04_Opt')
parser.add_argument('--spawn_point', dest='num_selected_spawn_point', type=int, default=16)
parser.add_argument('--laneless', action='store_true')

os.environ["SIMULATION"] = "1"

steer_angle_from_wheel = None
torque_from_wheel = None
args = parser.parse_args()

# W, H = 1164, 874
W, H, FOCAL_LENGTH = 1928, 1208, 2648.0
REPEAT_COUNTER = 5
PRINT_DECIMATION = 100
# STEER_RATIO = 10.
STEER_RATIO = 10.

pm = messaging.PubMaster(['roadCameraState', 'sensorEvents', 'can', "gpsLocationExternal"])
sm = messaging.SubMaster(['carControl', 'controlsState', 'carState', 'sendcan'])


class VehicleState:
  def __init__(self):
    self.speed = 0
    self.angle = 0
    self.bearing_deg = 0.0
    self.vel = carla.Vector3D()
    self.cruise_button = 0
    self.is_engaged = False
    self.blinkers = {
      "left": False,
      "right": False
    }


def steer_rate_limit(old, new):
  # Rate limiting to 0.5 degrees per step
  limit = 0.5
  if new > old + limit:
    return old + limit
  elif new < old - limit:
    return old - limit
  else:
    return new


frame_id = 0
vipc_server = VisionIpcServer("camerad")
vipc_server.create_buffers(VisionStreamType.VISION_STREAM_RGB_BACK, 4, True, W, H)
vipc_server.create_buffers(VisionStreamType.VISION_STREAM_YUV_BACK, 40, False, W, H)
vipc_server.start_listener()


def publishRoadCameraState(img, frame):
  global frame_id
  yuv = cv2.cvtColor(img, cv2.COLOR_RGB2YUV_I420)
  eof = frame_id * 0.05
  vipc_server.send(VisionStreamType.VISION_STREAM_RGB_BACK, img.tobytes(), frame_id, eof, eof)
  vipc_server.send(VisionStreamType.VISION_STREAM_YUV_BACK, yuv.tobytes(), frame_id, eof, eof)

  dat = messaging.new_message('roadCameraState')
  dat.roadCameraState = {
    "frameId": frame,
    "transform": [1.0, 0.0, 0.0,
                  0.0, 1.0, 0.0,
                  0.0, 0.0, 1.0]
  }
  pm.send('roadCameraState', dat)
  frame_id += 1


def cam_callback(image):    
  img = np.frombuffer(image.raw_data, dtype=np.dtype("uint8"))
  img = np.reshape(img, (H, W, 4))[:, :, [0, 1, 2]]
  p = threading.Thread(target=publishRoadCameraState, args=(img, image.frame))
  p.start()
  p.join()


def imu_callback(imu, vehicle_state):
  vehicle_state.bearing_deg = math.degrees(imu.compass)
  dat = messaging.new_message('sensorEvents', 2)
  dat.sensorEvents[0].sensor = 4
  dat.sensorEvents[0].type = 0x10
  dat.sensorEvents[0].init('acceleration')
  dat.sensorEvents[0].acceleration.v = [imu.accelerometer.x, imu.accelerometer.y, imu.accelerometer.z]
  # copied these numbers from locationd
  dat.sensorEvents[1].sensor = 5
  dat.sensorEvents[1].type = 0x10
  dat.sensorEvents[1].init('gyroUncalibrated')
  dat.sensorEvents[1].gyroUncalibrated.v = [imu.gyroscope.x, imu.gyroscope.y, imu.gyroscope.z]
  pm.send('sensorEvents', dat)


def panda_state_function(exit_event: threading.Event):
  pm = messaging.PubMaster(['pandaStates'])
  while not exit_event.is_set():
    dat = messaging.new_message('pandaStates', 1)
    dat.valid = True
    dat.pandaStates[0] = {
      'ignitionLine': True,
      'pandaType': "blackPanda",
      'controlsAllowed': True,
      # 'safetyModel': 'hondaNidec'
      'safetyModel': 'hyundai',
      'safetyParam': Panda.FLAG_HYUNDAI_LONG
    }
    pm.send('pandaStates', dat)
    time.sleep(0.5)


def peripheral_state_function(exit_event: threading.Event):
  pm = messaging.PubMaster(['peripheralState'])
  while not exit_event.is_set():
    dat = messaging.new_message('peripheralState')
    dat.valid = True
    # fake peripheral state data
    dat.peripheralState = {
      'pandaType': log.PandaState.PandaType.blackPanda,
      'voltage': 12000,
      'current': 5678,
      'fanSpeedRpm': 1000
    }
    pm.send('peripheralState', dat)
    time.sleep(0.5)


def gps_callback(gps, vehicle_state):
  dat = messaging.new_message('gpsLocationExternal')

  # transform vel from carla to NED
  # north is -Y in CARLA
  velNED = [
    -vehicle_state.vel.y,  # north/south component of NED is negative when moving south
    vehicle_state.vel.x,  # positive when moving east, which is x in carla
    vehicle_state.vel.z,
  ]

  dat.gpsLocationExternal = {
    "timestamp": int(time.time() * 1000),
    "flags": 1,  # valid fix
    "accuracy": 1.0,
    "verticalAccuracy": 1.0,
    "speedAccuracy": 0.1,
    "bearingAccuracyDeg": 0.1,
    "vNED": velNED,
    "bearingDeg": vehicle_state.bearing_deg,
    "latitude": gps.latitude,
    "longitude": gps.longitude,
    "altitude": gps.altitude,
    "speed": vehicle_state.speed,
    "source": log.GpsLocationData.SensorSource.ublox,
  }

  pm.send('gpsLocationExternal', dat)


def fake_driver_monitoring(exit_event: threading.Event):
  pm = messaging.PubMaster(['driverState', 'driverMonitoringState'])
  while not exit_event.is_set():
    # dmonitoringmodeld output
    dat = messaging.new_message('driverState')
    dat.driverState.faceProb = 1.0
    pm.send('driverState', dat)

    # dmonitoringd output
    dat = messaging.new_message('driverMonitoringState')
    dat.driverMonitoringState = {
      "faceDetected": True,
      "isDistracted": False,
      "awarenessStatus": 1.,
    }
    pm.send('driverMonitoringState', dat)

    time.sleep(DT_DMON)


def can_function_runner(vs: VehicleState, exit_event: threading.Event):
  i = 0
  while not exit_event.is_set():
    can_function(pm, vs.speed, vs.angle, i, vs.cruise_button, vs.is_engaged, vs.blinkers, steer_angle_from_wheel, torque_from_wheel)
    time.sleep(0.01)
    i += 1

def create_eps_ems16(packer, counter, engine_status):
  # send @ 100Hz
  values = {
    "ENG_STAT": engine_status,  # 3 == running?
    "AliveCounter": counter % 4,
  }
  dat = packer.make_can_msg("EMS16", 0, values)[2]
  h_sum = sum(dat[i] & 0xF0 for i in range(8))
  l_sum = sum(dat[i] & 0x0F for i in range(8))
  csum = 0x10 - ((h_sum >> 4) + l_sum & 0xF) & 0xF
  values["Checksum"] = csum
  return packer.make_can_msg("EMS16", 0, values)

def create_eps_366ems(packer, speed_kph):
  # send @ 100Hz
  values = {
    "VS": speed_kph,
  }
  return packer.make_can_msg("EMS_366", 0, values)

def create_eps_clu11(packer, counter, speed_kph):
  # send @ 50Hz
  values = {
    "CF_Clu_Vanz": speed_kph,
    "CF_Clu_AliveCnt1": counter % 0x10,
  }
  return packer.make_can_msg("CLU11", 0, values)

def create_eps_psts(packer, counter):
  # send @ 50Hz
  values = {
    "Counter": counter,
    # more fields are in the checksum, but doesn't matter when zero
    "Checksum": ((counter & 0b11 == 0b11) + (counter & 0b1100 == 0b1100)) & 3,
  }
  return packer.make_can_msg("P_STS", 0, values)

def sendcan_function_runner(vs: VehicleState, exit_event: threading.Event):
  global steer_angle_from_wheel
  global torque_from_wheel
  p = Panda()
  p.set_safety_mode(Panda.SAFETY_ALLOUTPUT)

  i = 0
  while not exit_event.is_set():
    if not len(sm['sendcan']):
      time.sleep(0.01)
      continue

    msgs = []
    speed_kph = vs.speed * 3.6
    msgs.append(create_eps_ems16(packer, i, 3))
    msgs.append(create_eps_366ems(packer, speed_kph))
    if i % 2 == 0:
      msgs.append(create_eps_clu11(packer, int(i / 2), speed_kph))
      msgs.append(create_eps_psts(packer, int(i / 2)))

    # append from sendcan
    for m in sm['sendcan']:
      msgs.append([m.address, 0, m.dat, m.src])
      # print(msgs[-1])

    # print(p.health())

    # send over the panda
    p.can_send_many(msgs)
    while True:
      received = p.can_recv()
      if not received:
        break
      for can_msg in received:
        if can_msg[3] == 0 and can_msg[0] == 688:
          steer_angle_from_wheel = can_msg
        elif can_msg[3] == 0 and can_msg[0] == 593:
          torque_from_wheel = can_msg

    time.sleep(DT_CTRL)
    i += 1


def bridge(q):

  # setup CARLA
  client = carla.Client("127.0.0.1", 2000)
  client.set_timeout(10.0)
  world = client.load_world(args.town)

  if args.low_quality:
    world.unload_map_layer(carla.MapLayer.Foliage)
    world.unload_map_layer(carla.MapLayer.Buildings)
    world.unload_map_layer(carla.MapLayer.ParkedVehicles)
    world.unload_map_layer(carla.MapLayer.Particles)
    world.unload_map_layer(carla.MapLayer.Props)
    world.unload_map_layer(carla.MapLayer.StreetLights)

  blueprint_library = world.get_blueprint_library()

  world_map = world.get_map()

  vehicle_bp = blueprint_library.filter('vehicle.tesla.*')[1]
  spawn_points = world_map.get_spawn_points()
  assert len(spawn_points) > args.num_selected_spawn_point, \
    f'''No spawn point {args.num_selected_spawn_point}, try a value between 0 and {len(spawn_points)} for this town.'''
  spawn_point = spawn_points[args.num_selected_spawn_point]
  vehicle = world.spawn_actor(vehicle_bp, spawn_point)

  max_steer_angle = vehicle.get_physics_control().wheels[0].max_steer_angle

  # make tires less slippery
  # wheel_control = carla.WheelPhysicsControl(tire_friction=5)
  physics_control = vehicle.get_physics_control()
  physics_control.mass = 2326
  # physics_control.wheels = [wheel_control]*4
  physics_control.torque_curve = [[20.0, 500.0], [5000.0, 500.0]]
  physics_control.gear_switch_time = 0.0
  vehicle.apply_physics_control(physics_control)

  blueprint = blueprint_library.find('sensor.camera.rgb')
  blueprint.set_attribute('image_size_x', str(W))
  blueprint.set_attribute('image_size_y', str(H))
  # blueprint.set_attribute('fov', '70')
  blueprint.set_attribute('focal_distance', str(FOCAL_LENGTH))
  blueprint.set_attribute('sensor_tick', '0.05')
  transform = carla.Transform(carla.Location(x=0.8, z=1.13))
  camera = world.spawn_actor(blueprint, transform, attach_to=vehicle)
  camera.listen(cam_callback)

  vehicle_state = VehicleState()

  # reenable IMU
  imu_bp = blueprint_library.find('sensor.other.imu')
  imu = world.spawn_actor(imu_bp, transform, attach_to=vehicle)
  imu.listen(lambda imu: imu_callback(imu, vehicle_state))

  gps_bp = blueprint_library.find('sensor.other.gnss')
  gps = world.spawn_actor(gps_bp, transform, attach_to=vehicle)
  gps.listen(lambda gps: gps_callback(gps, vehicle_state))

  # launch fake car threads
  threads = []
  exit_event = threading.Event()
  threads.append(threading.Thread(target=panda_state_function, args=(exit_event,)))
  threads.append(threading.Thread(target=peripheral_state_function, args=(exit_event,)))
  threads.append(threading.Thread(target=fake_driver_monitoring, args=(exit_event,)))
  threads.append(threading.Thread(target=can_function_runner, args=(vehicle_state, exit_event,)))
  threads.append(threading.Thread(target=sendcan_function_runner, args=(vehicle_state, exit_event,)))
  for t in threads:
    t.start()

  # can loop
  rk = Ratekeeper(100, print_delay_threshold=0.05)

  # init
  throttle_ease_out_counter = REPEAT_COUNTER
  brake_ease_out_counter = REPEAT_COUNTER
  steer_ease_out_counter = REPEAT_COUNTER

  vc = carla.VehicleControl(throttle=0, steer=0, brake=0, reverse=False)

  is_openpilot_engaged = False
  throttle_out = steer_out = brake_out = 0
  throttle_op = steer_op = brake_op = 0
  throttle_manual = steer_manual = brake_manual = 0

  old_steer = old_brake = old_throttle = 0
  throttle_manual_multiplier = 0.7  # keyboard signal is always 1
  brake_manual_multiplier = 0.7  # keyboard signal is always 1
  steer_manual_multiplier = 45 * STEER_RATIO  # keyboard signal is always 1

  while 1:
    # 1. Read the throttle, steer and brake from op or manual controls
    # 2. Set instructions in Carla
    # 3. Send current carstate to op via can

    cruise_button = 0
    throttle_out = steer_out = brake_out = 0.0
    throttle_op = steer_op = brake_op = 0
    throttle_manual = steer_manual = brake_manual = 0.0

    # --------------Step 1-------------------------------
    if not q.empty():
      message = q.get()
      m = message.split('_')
      if m[0] == "steer":
        steer_manual = float(m[1])
      elif m[0] == "throttle":
        throttle_manual = float(m[1])
        is_openpilot_engaged = False
      elif m[0] == "brake":
        brake_manual = float(m[1])
        is_openpilot_engaged = False
      elif m[0] == "reverse":
        # in_reverse = not in_reverse
        # cruise_button = CruiseButtons.CANCEL
        cruise_button = Buttons.CANCEL
        is_openpilot_engaged = False
      elif m[0] == "cruise":
        if m[1] == "down":
          # cruise_button = CruiseButtons.DECEL_SET
          cruise_button = Buttons.SET_DECEL
          is_openpilot_engaged = True
        elif m[1] == "up":
          # cruise_button = CruiseButtons.RES_ACCEL
          cruise_button = Buttons.RES_ACCEL
          is_openpilot_engaged = True
        elif m[1] == "cancel":
          # cruise_button = CruiseButtons.CANCEL
          cruise_button = Buttons.CANCEL
          is_openpilot_engaged = False
      elif m[0] == "blinker":
        vehicle_state.blinkers[m[1]] = not vehicle_state.blinkers[m[1]]
      elif m[0] == "quit":
        break

      throttle_out = throttle_manual * throttle_manual_multiplier
      steer_out = steer_manual * steer_manual_multiplier
      brake_out = brake_manual * brake_manual_multiplier

      # steer_out = steer_out
      # steer_out = steer_rate_limit(old_steer, steer_out)
      old_steer = steer_out
      old_throttle = throttle_out
      old_brake = brake_out

      # print('message',old_throttle, old_steer, old_brake)
    sm.update(0)
    actual_steering_angle = sm['carState'].steeringAngleDeg

    if is_openpilot_engaged:
      # TODO gas and brake is deprecated
      throttle_op = clip(sm['carControl'].actuators.accel/1.6, 0.0, 1.0)
      brake_op = clip(-sm['carControl'].actuators.accel/4.0, 0.0, 1.0)
      steer_op = sm['carControl'].actuators.steeringAngleDeg

      throttle_out = throttle_op
      steer_out = steer_op
      brake_out = brake_op

      steer_out = steer_rate_limit(old_steer, steer_out)
      old_steer = steer_out

    else:
      if throttle_out == 0 and old_throttle > 0:
        if throttle_ease_out_counter > 0:
          throttle_out = old_throttle
          throttle_ease_out_counter += -1
        else:
          throttle_ease_out_counter = REPEAT_COUNTER
          old_throttle = 0

      if brake_out == 0 and old_brake > 0:
        if brake_ease_out_counter > 0:
          brake_out = old_brake
          brake_ease_out_counter += -1
        else:
          brake_ease_out_counter = REPEAT_COUNTER
          old_brake = 0

      if steer_out == 0 and old_steer != 0:
        if steer_ease_out_counter > 0:
          steer_out = old_steer
          steer_ease_out_counter += -1
        else:
          steer_ease_out_counter = REPEAT_COUNTER
          old_steer = 0

    # --------------Step 2-------------------------------

    # steer_carla = steer_out / (max_steer_angle * STEER_RATIO * -1)
    steer_carla = actual_steering_angle / (max_steer_angle * STEER_RATIO * -1)

    steer_carla = np.clip(steer_carla, -1, 1)
    print('ACTUAL STEERING wheel angle', steer_carla * max_steer_angle)

    steer_out = steer_carla * (max_steer_angle * STEER_RATIO * -1)
    old_steer = steer_carla * (max_steer_angle * STEER_RATIO * -1)

    vc.throttle = throttle_out / 0.6
    vc.steer = steer_carla
    vc.brake = brake_out
    vehicle.apply_control(vc)

    # --------------Step 3-------------------------------
    vel = vehicle.get_velocity()
    speed = math.sqrt(vel.x**2 + vel.y**2 + vel.z**2)  # in m/s
    vehicle_state.speed = speed
    vehicle_state.vel = vel
    vehicle_state.angle = steer_out
    vehicle_state.cruise_button = cruise_button
    vehicle_state.is_engaged = is_openpilot_engaged

    if rk.frame % PRINT_DECIMATION == 0:
      print("frame: ", "engaged:", is_openpilot_engaged,
        "; throttle: ", round(vc.throttle, 3),
        "; steer(c/deg): ", round(vc.steer, 3), round(steer_out, 3),
        "; brake: ", round(vc.brake, 3),
        "; blinkers: ", vehicle_state.blinkers)
    rk.keep_time()

  # Clean up resources in the opposite order they were created.
  exit_event.set()
  for t in reversed(threads):
    t.join()
  gps.destroy()
  imu.destroy()
  camera.destroy()
  vehicle.destroy()


def bridge_keep_alive(q: Any):
  while 1:
    try:
      bridge(q)
      break
    except RuntimeError:
      print("Restarting bridge...")


if __name__ == "__main__":
  # make sure params are in a good state
  set_params_enabled()

  msg = messaging.new_message('liveCalibration')
  msg.liveCalibration.validBlocks = 20
  msg.liveCalibration.rpyCalib = [0.0, 0.0, 0.0]
  Params().put("CalibrationParams", msg.to_bytes())
  Params().put_bool("DisableRadar", True)
  Params().put_bool("DisableRadar_Allow", True)
  if args.laneless:
    Params().put_bool("EndToEndToggle", True)
  else:
    Params().put_bool("EndToEndToggle", False)

  q: Any = Queue()
  p = Process(target=bridge_keep_alive, args=(q,), daemon=True)
  p.start()

  if args.joystick:
    # start input poll for joystick
    from lib.manual_ctrl import wheel_poll_thread
    wheel_poll_thread(q)
    p.join()
  else:
    # start input poll for keyboard
    from lib.keyboard_ctrl import keyboard_poll_thread
    keyboard_poll_thread(q)
