"""Feed simulated base station packets into the backend, no ESP32 needed.

A virtual drone flies a grid at 20 m over three hidden survivors. At each
grid point, the RSSI each survivor's device would produce is computed with a
log-distance path loss model plus random noise. Every packet goes through the
same parse_packet() and record_reading() the serial reader uses, so the
running API sees them exactly as if they came from the base station.

Run from the repository root while backend\\app.py is running (or not):

    python backend\\tools\\simulate_packets.py --reset

--reset deletes survivors.json and drone_readings.json first.
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


# (id, status code, battery, true lat, true lon)
SURVIVORS = [
    ("SIM-001", 0, 12, 14.45750, 120.98550),
    ("SIM-002", 3, 45, 14.45620, 120.98690),
    ("SIM-003", 5, 80, 14.45880, 120.98420),
]

GRID_CENTRE = (14.45750, 120.98550)
GRID_HALF_SPAN_DEG = 0.0020     # about 220 m each way
GRID_STEP_DEG = 0.0005          # about 55 m between passes

ALTITUDE_M = 20.0
RSSI_AT_1M = -40.0              # dBm at 1 m, tune to your radio
PATH_LOSS_EXPONENT = 2.7        # 2 = free space, higher = rubble and buildings
NOISE_DB = 3.0
SENSITIVITY_DBM = -115.0        # weaker than this is not heard


def simulated_rssi(drone, survivor):
    horizontal = routing.distance_m(drone, survivor)
    distance = max(1.0, math.hypot(horizontal, ALTITUDE_M))

    return (RSSI_AT_1M - 10 * PATH_LOSS_EXPONENT * math.log10(distance)
            + random.gauss(0, NOISE_DB))


def grid_points():
    steps = int(round(2 * GRID_HALF_SPAN_DEG / GRID_STEP_DEG))

    for row in range(steps + 1):
        lat = GRID_CENTRE[0] - GRID_HALF_SPAN_DEG + row * GRID_STEP_DEG
        cols = range(steps + 1) if row % 2 == 0 else range(steps, -1, -1)

        for col in cols:
            yield lat, GRID_CENTRE[1] - GRID_HALF_SPAN_DEG + col * GRID_STEP_DEG


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
    for drone in grid_points():
        for survivor_id, code, battery, lat, lon in SURVIVORS:
            rssi = simulated_rssi(drone, (lat, lon))

            if rssi < SENSITIVITY_DBM:
                continue

            line = (f"SOS|{survivor_id}|{code}|{battery}|R1|"
                    f"{drone[0]:.6f}|{drone[1]:.6f}|{ALTITUDE_M}|{rssi:.0f}")

            serial_reader.record_reading(serial_reader.parse_packet(line))
            sent += 1

    print(f"Sent {sent} packets.\n")
    print(f"{'Survivor':<10} {'Readings':>8} {'Error (m)':>10}")

    estimates = algorithm.estimate_all()
    for survivor_id, _, _, lat, lon in SURVIVORS:
        estimate = estimates.get(survivor_id)

        if estimate is None:
            print(f"{survivor_id:<10} {'0':>8} {'no estimate':>10}")
            continue

        error = routing.distance_m((lat, lon), (estimate["lat"], estimate["lon"]))
        print(f"{survivor_id:<10} {estimate['reading_count']:>8} {error:>10.1f}")


if __name__ == "__main__":
    main()
