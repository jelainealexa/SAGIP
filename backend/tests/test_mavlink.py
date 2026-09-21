"""Checks mavlink_ctrl against a fake drone on UDP 14550.

Run from the repository root:  python backend\\tests\\test_mavlink.py
Stop backend\\app.py first if it has dispatched, since it holds port 14550.

The fake drone behaves like ArduCopter in the steps we use: it changes mode on
SET_MODE, arms only in GUIDED, climbs after NAV_TAKEOFF, and records the
position target. It also checks every packet it receives is MAVLink 1.0.
"""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mavlink_ctrl

from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as v2

M = mavutil.mavlink

drone = {"mode": 0, "armed": False, "alt": 0.0, "climbing": False,
         "target": None, "non_v1": [], "stop": False}


def fake_drone():
    link = mavutil.mavlink_connection("udpout:127.0.0.1:14550", source_system=1, source_component=1)

    # A MAVLink 2.0 heartbeat first, like Mission Planner can send. The
    # backend must stay on MAVLink 1.0 anyway.
    v2_packer = v2.MAVLink(None, srcSystem=255, srcComponent=190)
    raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw.sendto(v2_packer.heartbeat_encode(6, 8, 0, 0, 0).pack(v2_packer), ("127.0.0.1", 14550))

    last = 0
    while not drone["stop"]:
        if time.time() - last > 0.2:
            last = time.time()
            base = M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            if drone["armed"]:
                base |= M.MAV_MODE_FLAG_SAFETY_ARMED
            link.mav.heartbeat_send(M.MAV_TYPE_QUADROTOR, M.MAV_AUTOPILOT_ARDUPILOTMEGA, base, drone["mode"], 4)
            if drone["climbing"]:
                drone["alt"] = min(20.5, drone["alt"] + 3)
            link.mav.global_position_int_send(0, int(14.4570e7), int(120.9850e7), 0,
                                              int(drone["alt"] * 1000), 0, 0, 0, 0)

        msg = link.recv_match(blocking=True, timeout=0.05)
        if msg is None or msg.get_type() == "BAD_DATA":
            continue
        if msg.get_msgbuf()[0] != 0xFE:
            drone["non_v1"].append(msg.get_type())

        kind = msg.get_type()
        if kind == "SET_MODE":
            drone["mode"] = msg.custom_mode
        elif kind == "COMMAND_LONG" and msg.command == M.MAV_CMD_COMPONENT_ARM_DISARM:
            drone["armed"] = drone["mode"] == 4
            link.mav.command_ack_send(msg.command, M.MAV_RESULT_ACCEPTED if drone["armed"] else M.MAV_RESULT_FAILED)
        elif kind == "COMMAND_LONG" and msg.command == M.MAV_CMD_NAV_TAKEOFF:
            drone["climbing"] = drone["armed"]
            link.mav.command_ack_send(msg.command, M.MAV_RESULT_ACCEPTED if drone["armed"] else M.MAV_RESULT_FAILED)
        elif kind == "SET_POSITION_TARGET_GLOBAL_INT":
            drone["target"] = (msg.coordinate_frame, msg.type_mask, msg.lat_int, msg.lon_int, msg.alt)


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name + ("" if condition else f"  {detail}"))
    if not condition:
        check.failed += 1

check.failed = 0

threading.Thread(target=fake_drone, daemon=True).start()

mavlink_ctrl.dispatch_drone(14.4585, 120.9871)
time.sleep(0.5)

check("mode set to GUIDED", drone["mode"] == 4, drone)
check("armed", drone["armed"])
check("took off to 20 m", drone["alt"] >= 19, drone["alt"])
check("position target sent", drone["target"] is not None)
if drone["target"]:
    frame, mask, lat, lon, alt = drone["target"]
    check("frame is GLOBAL_RELATIVE_ALT_INT", frame == M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT)
    check("position-only type mask", mask == 0b0000_1111_1111_1000, bin(mask))
    check("target coordinates and altitude", (lat, lon, alt) == (144585000, 1209871000, 20), drone["target"])

drone["climbing"] = False
mavlink_ctrl.dispatch_drone(14.4590, 120.9880)
time.sleep(0.5)
check("redirect while airborne", drone["target"][2:4] == (144590000, 1209880000), drone["target"])

check("every packet was MAVLink 1.0", not drone["non_v1"], drone["non_v1"])

drone["stop"] = True
print()
print("ALL PASSED" if check.failed == 0 else f"{check.failed} FAILED")
sys.exit(1 if check.failed else 0)
