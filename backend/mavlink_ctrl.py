# SAGIP drone dispatch over pymavlink.
#
# The APM 2.8 is an 8-bit board that only speaks MAVLink 1.0. Keep these lines
# at the very top of this file, before pymavlink is imported.
#
# Do NOT write os.environ["MAVLINK20"] = "0". pymavlink only checks whether
# MAVLINK20 exists, not its value, so "0" switches MAVLink 2.0 ON. Removing the
# variable is what selects MAVLink 1.0.
import os
os.environ.pop("MAVLINK20", None)

import logging
import threading
import time

from datetime import datetime, timezone

from pymavlink import mavutil

if mavutil.mavlink.WIRE_PROTOCOL_VERSION != "1.0":
    raise ImportError(
        "pymavlink loaded MAVLink " + mavutil.mavlink.WIRE_PROTOCOL_VERSION
        + ". Import mavlink_ctrl before anything else that imports pymavlink."
    )


log = logging.getLogger(__name__)

# Mission Planner stays connected to the drone over telemetry radio and mirrors
# the MAVLink stream to this UDP port. We listen here. In Mission Planner's
# MAVLink Mirror, tick "Write access" or our commands are dropped.
CONNECTION_STRING = "udpin:127.0.0.1:14550"

TAKEOFF_ALTITUDE_M = 20

HEARTBEAT_TIMEOUT_S = 15
COMMAND_ACK_TIMEOUT_S = 5
MODE_TIMEOUT_S = 10
ARM_TIMEOUT_S = 15
TAKEOFF_TIMEOUT_S = 60

# Takeoff counts as complete at 95% of the target, as ArduCopter overshoots
# and settles rather than landing exactly on it.
TAKEOFF_REACHED_RATIO = 0.95

# Above this relative altitude with motors armed, the drone is treated as
# already flying and a new dispatch skips arming and takeoff.
AIRBORNE_ALTITUDE_M = 1.0

# SET_POSITION_TARGET_GLOBAL_INT type_mask: use lat, lon, alt only. Bits 3-11
# ignore velocity, acceleration, yaw and yaw rate.
POSITION_ONLY_MASK = 0b0000_1111_1111_1000

COPTER_TYPES = {
    mavutil.mavlink.MAV_TYPE_QUADROTOR,
    mavutil.mavlink.MAV_TYPE_HEXAROTOR,
    mavutil.mavlink.MAV_TYPE_OCTOROTOR,
    mavutil.mavlink.MAV_TYPE_TRICOPTER,
    mavutil.mavlink.MAV_TYPE_COAXIAL,
    mavutil.mavlink.MAV_TYPE_HELICOPTER
}


class DroneError(Exception):
    """Raised when the drone rejects or does not complete a step."""


class DroneBusy(Exception):
    """Raised when a dispatch is requested while another is still running."""


_master = None
_link_lock = threading.RLock()
_last_statustext = None

_state_lock = threading.Lock()
_state = {
    "state": "IDLE",
    "target": None,
    "message": "No dispatch yet.",
    "updated_at": None
}


def _set_state(state, message, target=None):
    with _state_lock:
        _state["state"] = state
        _state["message"] = message
        _state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if target is not None:
            _state["target"] = target

    log.info("Drone %s: %s", state, message)


def get_dispatch_state():
    with _state_lock:
        return dict(_state)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect():
    """Open the UDP link and wait until the drone's heartbeat is seen."""

    global _master

    with _link_lock:
        if _master is not None:
            return _master

        master = mavutil.mavlink_connection(CONNECTION_STRING, source_system=255)

        # pymavlink upgrades itself to MAVLink 2.0 for good if the first packet
        # it hears is 2.0 (Mission Planner's own heartbeat can be). Mark the
        # first byte as already seen so the link stays on 1.0.
        master.first_byte = False

        # Mission Planner's own heartbeat can arrive on the mirror too. Wait
        # for one from the vehicle so commands are addressed to the drone.
        deadline = time.monotonic() + HEARTBEAT_TIMEOUT_S
        heartbeat = None

        while time.monotonic() < deadline:
            msg = master.recv_match(type="HEARTBEAT", blocking=True, timeout=1)

            if msg is not None and msg.type != mavutil.mavlink.MAV_TYPE_GCS:
                heartbeat = msg
                break

        if heartbeat is None:
            master.close()
            raise DroneError(
                f"No drone heartbeat on {CONNECTION_STRING} within "
                f"{HEARTBEAT_TIMEOUT_S}s. Is Mission Planner connected and "
                f"mirroring MAVLink to UDP 14550?"
            )

        if heartbeat.type not in COPTER_TYPES:
            master.close()
            raise DroneError(
                f"Vehicle type {heartbeat.type} is not a multicopter. "
                f"This dispatch sequence is written for ArduCopter."
            )

        master.target_system = heartbeat.get_srcSystem()
        master.target_component = heartbeat.get_srcComponent()

        log.info("Drone found: system %s component %s.",
                 master.target_system, master.target_component)

        _master = master
        return master


def _disconnect():
    global _master

    with _link_lock:
        if _master is not None:
            _master.close()
            _master = None


def _wait_for(master, types, condition, timeout):
    """Return the first message from the drone of these types that satisfies
    condition, or None on timeout. STATUSTEXT is logged along the way so
    pre-arm failures show up in the console and in error messages."""

    global _last_statustext

    types = list(types) + ["STATUSTEXT"]
    deadline = time.monotonic() + timeout

    while (remaining := deadline - time.monotonic()) > 0:
        msg = master.recv_match(type=types, blocking=True, timeout=min(remaining, 1))

        if msg is None or msg.get_srcSystem() != master.target_system:
            continue

        if msg.get_type() == "STATUSTEXT":
            _last_statustext = msg.text
            log.info("Drone says: %s", msg.text)
            continue

        if condition(msg):
            return msg

    return None


def _hint():
    return f" Last drone message: {_last_statustext}" if _last_statustext else ""


def _command(master, command, *params):
    """Send COMMAND_LONG and check the ACK. Old APM firmware sometimes does not
    ACK, so a missing ACK is logged and the caller verifies the effect."""

    params = list(params) + [0] * (7 - len(params))

    master.mav.command_long_send(
        master.target_system, master.target_component,
        command, 0, *params
    )

    ack = _wait_for(
        master, ["COMMAND_ACK"],
        lambda m: m.command == command,
        COMMAND_ACK_TIMEOUT_S
    )

    if ack is None:
        log.warning("No COMMAND_ACK for command %s.", command)
        return

    if ack.result != mavutil.mavlink.MAV_RESULT_ACCEPTED:
        raise DroneError(f"Drone rejected command {command} "
                         f"(result {ack.result}).{_hint()}")


# ---------------------------------------------------------------------------
# Flight steps
# ---------------------------------------------------------------------------

def _is_armed(heartbeat):
    return bool(heartbeat.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)


def _current_status(master):
    heartbeat = _wait_for(master, ["HEARTBEAT"], lambda m: True, 5)
    position = _wait_for(master, ["GLOBAL_POSITION_INT"], lambda m: True, 5)

    if heartbeat is None or position is None:
        raise DroneError("Drone stopped sending heartbeat or position.")

    return _is_armed(heartbeat), position.relative_alt / 1000


def set_mode_guided(master):
    # mode_mapping_acm maps ArduCopter mode numbers to names. GUIDED is 4.
    mode_id = {name: number for number, name in mavutil.mode_mapping_acm.items()}["GUIDED"]

    master.mav.set_mode_send(
        master.target_system,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        mode_id
    )

    confirmed = _wait_for(
        master, ["HEARTBEAT"],
        lambda m: m.custom_mode == mode_id,
        MODE_TIMEOUT_S
    )

    if confirmed is None:
        raise DroneError(f"Drone did not enter GUIDED mode.{_hint()}")


def arm(master):
    _command(master, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1)

    if _wait_for(master, ["HEARTBEAT"], _is_armed, ARM_TIMEOUT_S) is None:
        raise DroneError(f"Drone did not arm.{_hint()}")


def takeoff(master, altitude_m=TAKEOFF_ALTITUDE_M):
    # param7 is the takeoff altitude in metres above home.
    _command(master, mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
             0, 0, 0, 0, 0, 0, altitude_m)

    reached = _wait_for(
        master, ["GLOBAL_POSITION_INT"],
        lambda m: m.relative_alt / 1000 >= altitude_m * TAKEOFF_REACHED_RATIO,
        TAKEOFF_TIMEOUT_S
    )

    if reached is None:
        raise DroneError(f"Drone did not reach {altitude_m} m within "
                         f"{TAKEOFF_TIMEOUT_S}s.{_hint()}")


def goto(master, target_lat, target_lon, altitude_m=TAKEOFF_ALTITUDE_M):
    master.mav.set_position_target_global_int_send(
        0,
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        POSITION_ONLY_MASK,
        int(target_lat * 1e7), int(target_lon * 1e7), altitude_m,
        0, 0, 0,
        0, 0, 0,
        0, 0
    )


def dispatch_drone(target_lat, target_lon):
    """Fly the drone to a target: GUIDED, arm, take off to 20 m, then go.

    Blocks until the position target has been sent. If the drone is already
    flying, arming and takeoff are skipped and it is simply redirected.
    """

    with _link_lock:
        master = connect()

        try:
            set_mode_guided(master)

            armed, altitude = _current_status(master)

            if armed and altitude > AIRBORNE_ALTITUDE_M:
                log.info("Already airborne at %.1f m, redirecting.", altitude)
            else:
                if not armed:
                    arm(master)
                takeoff(master)

            goto(master, target_lat, target_lon)
        except (OSError, ConnectionError) as error:
            # The socket is unusable. Reconnect cleanly on the next dispatch.
            _disconnect()
            raise DroneError(f"MAVLink link failed: {error}") from error


def start_dispatch(target_lat, target_lon):
    """Run dispatch_drone on a background thread so the HTTP request returns
    straight away. Progress is read back with get_dispatch_state()."""

    target = {"lat": target_lat, "lon": target_lon}

    # Check and claim in one step so two clicks cannot start two dispatches.
    with _state_lock:
        if _state["state"] in ("CONNECTING", "LAUNCHING"):
            raise DroneBusy("A dispatch is already in progress.")

        _state["state"] = "CONNECTING"

    _set_state("CONNECTING", "Connecting to the drone.", target)

    def run():
        try:
            _set_state("LAUNCHING", "Setting GUIDED, arming and taking off.")
            dispatch_drone(target_lat, target_lon)
            _set_state("EN_ROUTE", f"Flying to {target_lat}, {target_lon} "
                                   f"at {TAKEOFF_ALTITUDE_M} m.")
        except DroneError as error:
            _set_state("FAILED", str(error))
        except Exception as error:  # Never leave the state stuck at LAUNCHING.
            log.exception("Dispatch crashed.")
            _set_state("FAILED", f"Unexpected error: {error}")

    threading.Thread(target=run, name="drone-dispatch", daemon=True).start()


def get_drone_position():
    """Latest drone position as (lat, lon), or None if unknown.

    Never blocks: if a dispatch is using the link, the last cached position
    is returned instead.
    """

    if _master is None:
        return None

    if _link_lock.acquire(blocking=False):
        try:
            # Drain what arrived while idle so the cache holds the newest fix.
            while _master is not None and _master.recv_match(blocking=False):
                pass
        finally:
            _link_lock.release()

    master = _master
    position = master.messages.get("GLOBAL_POSITION_INT") if master else None

    if position is None or (position.lat == 0 and position.lon == 0):
        return None

    return position.lat / 1e7, position.lon / 1e7
