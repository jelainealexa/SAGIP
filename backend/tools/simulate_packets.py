"""Feed simulated base station packets into the backend, no ESP32 needed.

Four relays sit at surveyed positions. Each one repeatedly hears three hidden
survivors over BLE, and the RSSI it measures is computed with a log-distance
path loss model plus random noise. Every packet goes through the same
parse_packet() and record_reading() the serial reader uses, so the running API
sees them exactly as if they came from the base station.

Run from the repository root while backend\\app.py is running (or not):

    python backend\\tools\\simulate_packets.py --reset

--reset deletes survivors.json and drone_readings.json first.

This used to fly a virtual drone over a grid and weight the drone's own
positions. The relays are the sensors now: a drone measuring a LoRa packet is
measuring the relay that sent it, not the phone, so drone-side readings carry
no information about where a survivor is.

Read the error column with the geometry in mind. A weighted centroid can never
land outside the convex hull of the relays that heard the device, so a
survivor well outside the relay square will show a large error no matter how
clean the RSSI is. That is the method's limit, not a bug, and SIM-003 is
placed outside the square on purpose so the effect is visible.
"""

import argparse
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import algorithm
import routing
import serial_reader
import storage


# Surveyed relay positions. In deployment these are measured once with a tape
# from a fixed benchmark and typed into the node table. They are not GPS
# readings, so they do not drift and the relays need no GPS.
# (relay id, lat, lon, mounting height in metres)
RELAYS = [
    ("R-01", 14.45700, 120.98500, 3.0),
    ("R-02", 14.45700, 120.98600, 3.0),
    ("R-03", 14.45800, 120.98500, 3.0),
    ("R-04", 14.45800, 120.98600, 3.0),
]

# (id, status code, battery, true lat, true lon)
# Status codes index backend/priority.py STATUS_NAMES.
# SIM-001 sits inside the relay square, SIM-002 near its edge, SIM-003 well
# outside it.
SURVIVORS = [
    ("SIM-001", 0, 12, 14.45750, 120.98550),
    ("SIM-002", 3, 45, 14.45720, 120.98590),
    ("SIM-003", 5, 80, 14.45880, 120.98420),
]

# How many times each relay reports each survivor it can hear. A real phone
# advertises repeatedly, so several readings accumulate per relay and the
# noise partly averages out.
REPORTS_PER_RELAY = 8

RSSI_AT_1M = -40.0              # dBm at 1 m, tune to your radio
PATH_LOSS_EXPONENT = 2.7        # 2 = free space, higher = rubble and buildings
NOISE_DB = 3.0

# BLE through debris is short range. This is the number that decides how many
# relays hear a given survivor, and therefore whether the result is a
# coordinate, a narrowed zone, or proximity to one relay. Measure it for real
# before trusting any error figure this script prints.
SENSITIVITY_DBM = -95.0


def simulated_rssi(relay, survivor):
    """BLE RSSI the relay would measure from the survivor's phone."""

    relay_lat, relay_lon, height = relay
    horizontal = routing.distance_m((relay_lat, relay_lon), survivor)
    distance = max(1.0, math.hypot(horizontal, height))

    return (RSSI_AT_1M - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
            + random.gauss(0, NOISE_DB))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="clear stored data first")
    parser.add_argument("--seed", type=int, default=1, help="random seed for repeatable runs")
    args = parser.parse_args()

    random.seed(args.seed)

    if args.reset:
        for path in (storage.SURVIVORS_FILE, storage.READINGS_FILE):
            if os.path.exists(path):
                os.remove(path)

    sent = 0
    for _ in range(REPORTS_PER_RELAY):
        for relay_id, relay_lat, relay_lon, height in RELAYS:
            for survivor_id, code, battery, lat, lon in SURVIVORS:
                rssi = simulated_rssi((relay_lat, relay_lon, height), (lat, lon))

                if rssi < SENSITIVITY_DBM:
                    continue

                line = (f"SOS|{survivor_id}|{code}|{battery}|{relay_id}|"
                        f"{relay_lat:.6f}|{relay_lon:.6f}|{height}|{rssi:.0f}")

                serial_reader.record_reading(serial_reader.parse_packet(line))
                sent += 1

    print(f"Sent {sent} packets from {len(RELAYS)} relays.\n")
    print(f"{'Survivor':<10} {'Readings':>8} {'Relays':>7} {'Error (m)':>10}  Usable as")

    estimates = algorithm.estimate_all()
    for survivor_id, _, _, lat, lon in SURVIVORS:
        estimate = estimates.get(survivor_id)

        if estimate is None:
            print(f"{survivor_id:<10} {'0':>8} {'0':>7} {'-':>10}  not heard")
            continue

        error = routing.distance_m((lat, lon), (estimate["lat"], estimate["lon"]))
        nodes = estimate["node_count"]

        if nodes >= 3:
            usable = "coordinate"
        elif nodes == 2:
            usable = "narrowed to a line"
        else:
            usable = f"proximity only"

        print(f"{survivor_id:<10} {estimate['reading_count']:>8} {nodes:>7} "
              f"{error:>10.1f}  {usable}")


if __name__ == "__main__":
    main()
