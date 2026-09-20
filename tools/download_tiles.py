"""Download a small offline map tile pack for the SAGIP pilot test area.

Why this exists: the dashboard is meant to demonstrate operation when cellular
service is unavailable. A map that fetches tiles from an internet tile server
contradicts that. This script saves the tiles for one small area to disk so the
dashboard can serve them locally.

Usage:
    python tools/download_tiles.py

Before running, set TEST_AREA and ZOOM_LEVELS below to your own pilot area, and
set CONTACT to a real email address. Run ESTIMATE_ONLY first to see how many
tiles the area needs.

Important: the OpenStreetMap Foundation tile servers are donated infrastructure
and their usage policy restricts bulk downloading. Keep the area small, keep the
zoom range narrow, and leave the delay in place. A pilot-sized area at three
zoom levels is a few hundred tiles, which is acceptable. A whole city is not.
Read https://operations.osmfoundation.org/policies/tiles/ before running this.
"""

import math
import os
import time
import urllib.request


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Bounding box of the pilot test area: (south, west, north, east).
# Replace these with the corners of your actual test area. The values below are
# a small box around the coordinates currently used in the mock data.
TEST_AREA = (14.4520, 120.9800, 14.4620, 120.9900)

# Zoom levels to save. 15 shows the surrounding streets, 17 shows individual
# buildings. The dashboard upscales 17 to fill zoom 18 and 19, so there is no
# need to download those.
ZOOM_LEVELS = [15, 16, 17]

# Where the tiles are written. This must match the path the dashboard requests.
OUTPUT_DIR = os.path.join("dashboard", "static", "tiles")

TILE_SERVER = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# The tile server requires an identifying User-Agent. A generic one is refused.
CONTACT = "sagip-thesis/1.0 (your.email@example.com)"

# Seconds to wait between requests. Do not lower this.
REQUEST_DELAY = 1.0

# Set to True to print the tile count without downloading anything.
ESTIMATE_ONLY = True


# ---------------------------------------------------------------------------
# Tile arithmetic
#
# Web map tiles use the Web Mercator scheme. At zoom level z the world is cut
# into 2^z columns and 2^z rows. These two functions convert a latitude and
# longitude into the column (x) and row (y) that contain it.
# ---------------------------------------------------------------------------

def lon_to_x(longitude, zoom):
    return int((longitude + 180.0) / 360.0 * (2 ** zoom))


def lat_to_y(latitude, zoom):
    radians = math.radians(latitude)

    fraction = (
        1.0 - math.log(math.tan(radians) + 1.0 / math.cos(radians)) / math.pi
    ) / 2.0

    return int(fraction * (2 ** zoom))


def tiles_for_area(area, zoom):
    """Return every (x, y) tile index covering the bounding box at one zoom."""

    south, west, north, east = area

    x_start = lon_to_x(west, zoom)
    x_end = lon_to_x(east, zoom)

    # Row numbers increase southward, so the northern edge gives the smaller y.
    y_start = lat_to_y(north, zoom)
    y_end = lat_to_y(south, zoom)

    for x in range(min(x_start, x_end), max(x_start, x_end) + 1):
        for y in range(min(y_start, y_end), max(y_start, y_end) + 1):
            yield x, y


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_tile(zoom, x, y):
    """Save one tile. Returns 'saved', 'skipped', or 'failed'."""

    folder = os.path.join(OUTPUT_DIR, str(zoom), str(x))
    path = os.path.join(folder, f"{y}.png")

    if os.path.exists(path) and os.path.getsize(path) > 0:
        return "skipped"

    os.makedirs(folder, exist_ok=True)

    url = TILE_SERVER.format(z=zoom, x=x, y=y)
    request = urllib.request.Request(url, headers={"User-Agent": CONTACT})

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except Exception as error:
        print(f"  failed {zoom}/{x}/{y}: {error}")
        return "failed"

    with open(path, "wb") as output_file:
        output_file.write(data)

    return "saved"


def main():
    planned = []

    for zoom in ZOOM_LEVELS:
        tiles = list(tiles_for_area(TEST_AREA, zoom))
        planned.append((zoom, tiles))

        print(f"Zoom {zoom}: {len(tiles)} tiles")

    total = sum(len(tiles) for _, tiles in planned)

    # Tiles average roughly 15 KB each in built-up areas.
    print(f"\nTotal: {total} tiles, roughly {total * 15 / 1024:.1f} MB")
    print(f"Estimated time: {total * REQUEST_DELAY / 60:.1f} minutes\n")

    if ESTIMATE_ONLY:
        print("ESTIMATE_ONLY is True. Nothing was downloaded.")
        print("Set ESTIMATE_ONLY = False to download.")
        return

    if CONTACT.endswith("example.com)"):
        print("Set CONTACT to a real email address before downloading.")
        return

    counts = {"saved": 0, "skipped": 0, "failed": 0}

    for zoom, tiles in planned:
        print(f"Downloading zoom {zoom} ({len(tiles)} tiles)")

        for index, (x, y) in enumerate(tiles, start=1):
            result = download_tile(zoom, x, y)
            counts[result] += 1

            if result == "saved":
                time.sleep(REQUEST_DELAY)

            if index % 25 == 0:
                print(f"  {index}/{len(tiles)}")

    print(
        f"\nDone. {counts['saved']} saved, "
        f"{counts['skipped']} already present, "
        f"{counts['failed']} failed."
    )

    if counts["failed"]:
        print("Run the script again to retry the failed tiles.")


if __name__ == "__main__":
    main()