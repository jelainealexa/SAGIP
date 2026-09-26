"""SAGIP backend API: survivor reports, localisation, priority and dispatch.

Runs separately from the dashboard (dashboard/app.py on port 5000) on port
5001. CORS is enabled so the dashboard pages can call it from the browser.

Data lives in survivors.json and drone_readings.json next to this file. The
serial reader thread fills drone_readings.json from the base station ESP32.

Configuration (environment variables):
    SAGIP_SERIAL_PORT   base station COM port        default COM3
    SAGIP_SERIAL_BAUD   base station baud rate       default 115200
    SAGIP_API_PORT      port this API listens on     default 5001
"""

import logging
import os

from flask import Flask, jsonify, request
from flask_cors import CORS

import algorithm
import mavlink_ctrl
import priority
import routing
import storage

from serial_reader import SerialReader, SerialUnavailable, now_iso


app = Flask(__name__)
CORS(app)

SERIAL_PORT = os.environ.get("SAGIP_SERIAL_PORT", "COM3")
SERIAL_BAUD = int(os.environ.get("SAGIP_SERIAL_BAUD", "115200"))
API_PORT = int(os.environ.get("SAGIP_API_PORT", "5001"))

serial_reader = SerialReader(SERIAL_PORT, SERIAL_BAUD)


def error(message, status):
    return jsonify({"message": message}), status


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def latest_rssi_by_survivor(readings):
    """Most recent RSSI per survivor. Readings are stored in arrival order."""

    latest = {}
    for reading in readings:
        latest[reading["survivor_id"]] = reading["rssi"]

    return latest


def scored_survivors():
    """Every survivor with its priority and estimated location attached,
    highest priority first."""

    survivors = storage.get_survivors()
    readings = storage.get_readings()

    estimates = algorithm.estimate_all(readings)
    rssi = latest_rssi_by_survivor(readings)

    result = []
    for survivor in survivors:
        scored = priority.compute_priority(survivor, rssi.get(survivor["id"]))

        result.append({
            **survivor,
            "priority": scored["score"],
            "priority_breakdown": scored["breakdown"],
            "latest_rssi": rssi.get(survivor["id"]),
            "estimated_location": estimates.get(survivor["id"])
        })

    result.sort(key=lambda s: s["priority"], reverse=True)
    return result


def drone_start_position(body):
    """Where the drone is now: live MAVLink position, else the position given
    in the request.

    There used to be a third fallback here that took the position from the most
    recent reading. That was correct while readings carried the drone's own
    position. They now carry the position of the relay that heard the device,
    so the fallback would have answered "where is the drone" with the location
    of a relay on a wall, and route planning would have started from the wrong
    point without reporting any error.

    Returning None is the honest answer. The caller turns it into a 409 telling
    the operator to supply a position.
    """

    position = mavlink_ctrl.get_drone_position()
    if position is not None:
        return position

    if "drone_lat" in body and "drone_lon" in body:
        return parse_coordinates(body["drone_lat"], body["drone_lon"])

    return None


def plan_current_route(start):
    candidates = [
        {
            "survivor_id": s["id"],
            "lat": s["estimated_location"]["lat"],
            "lon": s["estimated_location"]["lon"],
            "priority": s["priority"]
        }
        for s in scored_survivors()
        if s["estimated_location"] is not None
    ]

    return routing.plan_route(start, candidates)


def parse_coordinates(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("lat and lon must be numbers.")

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat or lon is out of range.")

    return lat, lon


def validate_sos(body):
    """Check an SOS payload and return the fields to store."""

    survivor_id = str(body.get("id", "")).strip()
    if not survivor_id:
        raise ValueError("id is required.")

    status = priority.normalise_status(body.get("status", ""))
    if status is None:
        raise ValueError(
            "status must be one of: " + ", ".join(priority.STATUS_NAMES) + "."
        )

    fields = {"status": status}

    if body.get("battery") is not None:
        try:
            battery = float(body["battery"])
        except (TypeError, ValueError):
            raise ValueError("battery must be a number.")

        if not 0 <= battery <= 100:
            raise ValueError("battery must be between 0 and 100.")

        fields["battery"] = battery

    if body.get("household_size") is not None:
        try:
            size = int(body["household_size"])
        except (TypeError, ValueError):
            raise ValueError("household_size must be a whole number.")

        if size < 1:
            raise ValueError("household_size must be at least 1.")

        fields["household_size"] = size

    assistance = body.get("assistance")
    if assistance is not None:
        if isinstance(assistance, str):
            assistance = [assistance]

        if not isinstance(assistance, list):
            raise ValueError("assistance must be a list of strings.")

        fields["assistance"] = [str(item).strip() for item in assistance]

    if body.get("address") is not None:
        fields["address"] = str(body["address"]).strip()

    # The device clock may be wrong after an outage, so its timestamp is kept
    # for the record but priority uses last_seen, the time we received it.
    if body.get("timestamp") is not None:
        fields["timestamp"] = body["timestamp"]

    return survivor_id, fields


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/sos", methods=["POST"])
def receive_sos():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error("Request body must be a JSON object.", 400)

    try:
        survivor_id, fields = validate_sos(body)
    except ValueError as exc:
        return error(str(exc), 400)

    fields["last_seen"] = now_iso()
    fields["source"] = "app"

    survivor = storage.upsert_survivor(survivor_id, fields)

    return jsonify({"message": "SOS received.", "survivor": survivor}), 201


@app.route("/survivors", methods=["GET"])
def list_survivors():
    return jsonify(scored_survivors())


@app.route("/estimated-locations", methods=["GET"])
def estimated_locations():
    estimates = algorithm.estimate_all()

    return jsonify([
        {"survivor_id": survivor_id, **estimate}
        for survivor_id, estimate in estimates.items()
    ])


@app.route("/route", methods=["GET"])
def current_route():
    start = drone_start_position(request.args)
    if start is None:
        return error("Drone position is unknown. Pass drone_lat and drone_lon.", 409)

    return jsonify({
        "start": {"lat": start[0], "lon": start[1]},
        "waypoints": plan_current_route(start)
    })


@app.route("/dispatch", methods=["POST"])
def dispatch():
    """Send the drone to a target.

    Body options, checked in this order:
        {"lat": ..., "lon": ...}    fly to these coordinates
        {"survivor_id": "..."}      fly to that survivor's estimated location
        {}                          fly to the first waypoint of the route
    """

    body = request.get_json(silent=True) or {}
    route = None

    try:
        if "lat" in body and "lon" in body:
            lat, lon = parse_coordinates(body["lat"], body["lon"])

        elif body.get("survivor_id"):
            estimate = algorithm.estimate_location(str(body["survivor_id"]))
            if estimate is None:
                return error("No drone readings yet for that survivor, so "
                             "there is no estimated location.", 409)

            lat, lon = estimate["lat"], estimate["lon"]

        else:
            start = drone_start_position(body)
            if start is None:
                return error("Drone position is unknown. Pass drone_lat and "
                             "drone_lon, or a target lat and lon.", 409)

            route = plan_current_route(start)
            if not route:
                return error("No survivor has an estimated location yet.", 409)

            lat, lon = route[0]["lat"], route[0]["lon"]

    except ValueError as exc:
        return error(str(exc), 400)

    try:
        mavlink_ctrl.start_dispatch(lat, lon)
    except mavlink_ctrl.DroneBusy as exc:
        return error(str(exc), 409)

    return jsonify({
        "message": "Dispatch started.",
        "target": {"lat": lat, "lon": lon},
        "route": route,
        "drone": mavlink_ctrl.get_dispatch_state()
    }), 202


@app.route("/dispatch", methods=["GET"])
def dispatch_status():
    return jsonify(mavlink_ctrl.get_dispatch_state())


@app.route("/release", methods=["POST"])
def release_payload():
    body = request.get_json(silent=True) or {}
    survivor_id = str(body.get("survivor_id", "")).strip()

    # The base station firmware must recognise this line and trigger the
    # payload servo. The optional ID is only for its log.
    command = f"RELEASE|{survivor_id}" if survivor_id else "RELEASE"

    try:
        serial_reader.send_line(command)
    except SerialUnavailable as exc:
        return error(str(exc), 503)

    return jsonify({"message": "Release command sent.", "command": command})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "serial_port": SERIAL_PORT,
        "serial_connected": serial_reader.connected,
        "drone": mavlink_ctrl.get_dispatch_state()
    })


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    serial_reader.start()

    # No reloader: it would start a second process that fights over the COM
    # port and the UDP socket.
    app.run(host="127.0.0.1", port=API_PORT, debug=False, use_reloader=False)
