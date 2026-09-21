"""RSSI weighted centroid localisation.

Each reading in drone_readings.json is a point where the drone heard a
survivor's device, with the signal strength it heard. The survivor is placed at
the average of those drone positions, weighted so that stronger (less
negative) readings pull the estimate harder:

    weight        = 1 / (abs(RSSI) + 1)
    estimated_lat = sum(lat * weight) / sum(weight)
    estimated_lon = sum(lon * weight) / sum(weight)
"""

from collections import defaultdict

import storage


def _usable(reading):
    """Skip readings taken before the drone had a GPS fix (reported as 0, 0)."""

    return not (reading["drone_lat"] == 0 and reading["drone_lon"] == 0)


def weighted_centroid(readings):
    """Estimate a position from readings, or return None if there are none."""

    readings = [r for r in readings if _usable(r)]

    if not readings:
        return None

    weights = [1 / (abs(r["rssi"]) + 1) for r in readings]
    total = sum(weights)

    lat = sum(r["drone_lat"] * w for r, w in zip(readings, weights)) / total
    lon = sum(r["drone_lon"] * w for r, w in zip(readings, weights)) / total

    return {
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "reading_count": len(readings)
    }


def estimate_location(survivor_id):
    """Estimate one survivor's position from its readings on file."""

    return weighted_centroid(storage.get_readings(survivor_id))


def estimate_all(readings=None):
    """Estimate every survivor that has readings. Returns {survivor_id: estimate}."""

    if readings is None:
        readings = storage.get_readings()

    grouped = defaultdict(list)
    for reading in readings:
        grouped[reading["survivor_id"]].append(reading)

    estimates = {}
    for survivor_id, group in grouped.items():
        estimate = weighted_centroid(group)

        if estimate is not None:
            estimates[survivor_id] = estimate

    return estimates
