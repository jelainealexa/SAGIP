"""RSSI weighted centroid localisation.

Each reading is a node that heard a survivor's device, recorded with that
node's own position and the signal strength it measured. The survivor is
placed at the average of those node positions, weighted so that stronger
readings pull the estimate harder:

    weight        = 10 ** (RSSI / 10)
    estimated_lat = sum(lat * weight) / sum(weight)
    estimated_lon = sum(lon * weight) / sum(weight)

The nodes are the relays, not the drone. A relay measures the BLE signal
arriving from the phone, so its reading carries information about where the
phone is. A drone measuring a LoRa packet is measuring the relay that sent it,
which says nothing about the phone, so drone-side RSSI cannot localise a
survivor no matter how the numbers are combined.

Relay positions are surveyed at installation and stored in the node table.
They are not GPS readings, so they do not drift and the relays need no GPS.

On the weighting
----------------

The earlier formula was weight = 1 / (abs(RSSI) + 1). It produces 0.0164 at
-60 dBm and 0.0090 at -110 dBm, under a 2x spread across the whole usable
range, so every node ends up weighted almost equally and the estimate collapses
to the plain geometric centre of whichever relays happened to hear the device.
Signal strength stops affecting the answer.

10 ** (RSSI / 10) converts dBm back to linear power, which is the quantity that
actually falls off with distance. It spans about five orders of magnitude over
the same range, so a close relay dominates a distant one, which is the whole
point of weighting.

This is still a centroid, not trilateration. The estimate can never fall
outside the convex hull of the relays that heard the device, so it degrades
toward "somewhere among these relays" as the geometry gets worse. That is a
limitation to report, not a bug to hide: with four relays and a device heard
by only one or two, a zone is an honest answer and a coordinate is not.
"""

from collections import defaultdict

import storage


def _usable(reading):
    """Skip readings with no usable node position (recorded as 0, 0)."""

    return not (reading["node_lat"] == 0 and reading["node_lon"] == 0)


def weighted_centroid(readings):
    """Estimate a position from readings, or return None if there are none."""

    readings = [r for r in readings if _usable(r)]

    if not readings:
        return None

    weights = [10 ** (r["rssi"] / 10) for r in readings]
    total = sum(weights)

    lat = sum(r["node_lat"] * w for r, w in zip(readings, weights)) / total
    lon = sum(r["node_lon"] * w for r, w in zip(readings, weights)) / total

    return {
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "reading_count": len(readings),
        # How many distinct relays contributed. One relay is proximity only,
        # two narrows to a line, three or more is a usable coordinate. The
        # dashboard should say which of those it is rather than showing every
        # estimate with the same confidence.
        "node_count": len({r["relay_id"] for r in readings if r.get("relay_id")})
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
