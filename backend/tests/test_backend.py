"""Checks the parser, algorithm, priority, routing and API without hardware.

Run from the repository root:  python backend\\tests\\test_backend.py
Uses a temporary folder, so your real survivors.json is not touched.
"""

import os
import sys
import tempfile

from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage

temp_dir = tempfile.mkdtemp()
storage.SURVIVORS_FILE = os.path.join(temp_dir, "survivors.json")
storage.READINGS_FILE = os.path.join(temp_dir, "drone_readings.json")

import algorithm
import priority
import routing
import serial_reader

from app import app


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name + ("" if condition else f"  {detail}"))
    if not condition:
        check.failed += 1

check.failed = 0


# Packet parsing
r = serial_reader.parse_packet("SOS|SGP-001|0|12|R2|14.4570|120.9850|20.5|-95\n")
check("parse valid packet", r["status"] == "CRITICAL SOS" and r["rssi"] == -95, r)

for bad in ["SOS|x|0|12", "HELLO|a|0|1|R|1|1|1|1", "SOS||0|1|R|1|1|1|1", "SOS|a|9|1|R|1|1|1|1"]:
    try:
        serial_reader.parse_packet(bad)
        check(f"reject {bad!r}", False)
    except ValueError:
        check(f"reject {bad!r}", True)

# Weighted centroid.
# Weight is linear power, 10 ** (rssi / 10), so a -59 dBm reading outweighs a
# -119 dBm one by a factor of a million and the estimate sits essentially on
# the strong node. Under the old 1 / (abs(rssi) + 1) weighting the same pair
# differed by less than 2x and the estimate landed near their midpoint.
est = algorithm.weighted_centroid([
    {"relay_id": "R-01", "node_lat": 10.0, "node_lon": 20.0, "rssi": -59},
    {"relay_id": "R-02", "node_lat": 11.0, "node_lon": 21.0, "rssi": -119},
    {"relay_id": "R-03", "node_lat": 0, "node_lon": 0, "rssi": -10},
])
w1, w2 = 10 ** (-59 / 10), 10 ** (-119 / 10)
expected = (10.0 * w1 + 11.0 * w2) / (w1 + w2)
check("weighted centroid formula", abs(est["lat"] - expected) < 1e-6, est)
check("skips readings with no node position", est["reading_count"] == 2, est)
check("counts distinct relays", est["node_count"] == 2, est)

# The strong reading must dominate. This is the property the old formula did
# not have, and the reason the estimate used to collapse to the geometric
# centre of whichever relays happened to hear the device.
check("strong reading dominates", abs(est["lat"] - 10.0) < 0.001, est)

# Priority thresholds
check("battery points", [priority.battery_points(b) for b in (14.9, 15, 29.9, 30, 50, 50.1)] == [40, 25, 25, 15, 15, 0])
check("signal age points", [priority.signal_age_points(a) for a in (9.9, 10, 30, 30.1)] == [0, 15, 15, 30])
check("rssi points", [priority.rssi_points(x) for x in (-91, -90, -70, -69)] == [20, 10, 10, 0])

now = datetime.now(timezone.utc)
survivor = {"status": "MEDICAL", "battery": 20, "last_seen": (now - timedelta(minutes=45)).isoformat()}
check("total score", priority.compute_priority(survivor, -80, now)["score"] == 80 + 25 + 30 + 10)

# Routing
candidates = [
    {"survivor_id": "A", "lat": 14.46, "lon": 120.98, "priority": 200},
    {"survivor_id": "B", "lat": 14.4601, "lon": 120.9801, "priority": 41},
    {"survivor_id": "C", "lat": 14.47, "lon": 120.99, "priority": 60},
    {"survivor_id": "D", "lat": 14.46005, "lon": 120.98005, "priority": 40},
]
order = [w["survivor_id"] for w in routing.plan_route((14.45, 120.97), candidates)]
check("route: highest first, nearest next, score 40 skipped", order == ["A", "B", "C"], order)

# API
client = app.test_client()
check("POST /sos rejects missing id", client.post("/sos", json={"status": "SAFE"}).status_code == 400)
check("POST /sos rejects bad status", client.post("/sos", json={"id": "X", "status": "BAD"}).status_code == 400)

resp = client.post("/sos", json={
    "id": "SGP-001", "status": "MEDICAL", "battery": 60, "address": "Blk 1",
    "household_size": 3, "assistance": ["WATER"], "timestamp": "2026-09-22T08:00:00+08:00"
})
check("POST /sos accepts valid SOS", resp.status_code == 201, resp.json)
check("CORS header present", resp.headers.get("Access-Control-Allow-Origin") == "*")

serial_reader.record_reading(serial_reader.parse_packet("SOS|SGP-001|0|12|R2|14.4570|120.9850|20|-95"))
serial_reader.record_reading(serial_reader.parse_packet("SOS|SGP-001|0|12|R2|14.4572|120.9852|20|-75"))
serial_reader.record_reading(serial_reader.parse_packet("SOS|SGP-003|3|40|R1|14.4580|120.9860|20|-85"))

survivors = client.get("/survivors").json
check("GET /survivors sorted by priority", survivors[0]["id"] == "SGP-001", survivors)
check("relay packet keeps phone address", survivors[0].get("address") == "Blk 1")
check("GET /estimated-locations", len(client.get("/estimated-locations").json) == 2)

# /route needs to know where the drone is starting from. With no MAVLink
# connection in a test, that has to be supplied. It used to be inferred from
# the last reading, which is no longer valid: readings now carry the position
# of the relay that heard the device, and a relay bolted to a wall is not the
# drone.
check("GET /route without a drone position", client.get("/route").status_code == 409)
route = client.get("/route", query_string={"drone_lat": 14.4570, "drone_lon": 120.9850}).json
check("GET /route", len(route["waypoints"]) == 2, route)
check("POST /dispatch needs a location", client.post("/dispatch", json={"survivor_id": "NONE"}).status_code == 409)
check("POST /dispatch rejects bad coordinates", client.post("/dispatch", json={"lat": 95, "lon": 0}).status_code == 400)
check("POST /release without ESP32 returns 503", client.post("/release", json={}).status_code == 503)

print()
print("ALL PASSED" if check.failed == 0 else f"{check.failed} FAILED")
sys.exit(1 if check.failed else 0)
