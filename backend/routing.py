"""Greedy drone route planner.

Starting from the drone's current position, the route visits the highest
priority survivor first. From there it repeatedly flies to the nearest
unvisited survivor whose score is above FOLLOW_UP_MIN_SCORE. This is a greedy
nearest-neighbour heuristic, not an optimal tour.
"""

import math


FOLLOW_UP_MIN_SCORE = 40

EARTH_RADIUS_M = 6371000


def distance_m(a, b):
    """Great-circle distance in metres between two (lat, lon) pairs."""

    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)

    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )

    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def plan_route(start, candidates):
    """Order survivors into a flight route.

    start       (lat, lon) of the drone.
    candidates  dicts with survivor_id, lat, lon and priority.

    Returns the ordered waypoints, each with the length of the leg flown to
    reach it.
    """

    if not candidates:
        return []

    def point(c):
        return (c["lat"], c["lon"])

    # Highest score first. On a tie, the closer survivor wins.
    first = max(
        candidates,
        key=lambda c: (c["priority"], -distance_m(start, point(c)))
    )

    remaining = [
        c for c in candidates
        if c is not first and c["priority"] > FOLLOW_UP_MIN_SCORE
    ]

    route = []
    here = start
    target = first

    while target is not None:
        route.append({
            "survivor_id": target["survivor_id"],
            "lat": target["lat"],
            "lon": target["lon"],
            "priority": target["priority"],
            "leg_distance_m": round(distance_m(here, point(target)), 1)
        })

        here = point(target)

        if not remaining:
            break

        target = min(remaining, key=lambda c: distance_m(here, point(c)))
        remaining.remove(target)

    return route
