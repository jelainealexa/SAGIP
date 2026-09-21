"""JSON file storage for the SAGIP backend.

The API threads and the serial reader thread both write to these files, so
every read-modify-write happens under one lock. Writes go to a temporary file
first and are then swapped in, so a crash mid-write cannot leave a half-written
JSON file behind.
"""

import json
import os
import threading


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SURVIVORS_FILE = os.path.join(BASE_DIR, "survivors.json")
READINGS_FILE = os.path.join(BASE_DIR, "drone_readings.json")

_lock = threading.RLock()


def _read(path):
    if not os.path.exists(path):
        return []

    with open(path, encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as error:
            # Refuse to continue rather than overwrite the file and lose
            # whatever is in it. This only happens after a manual edit.
            raise ValueError(f"{path} is not valid JSON: {error}") from error

    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list.")

    return data


def _write(path, data):
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)

    os.replace(temp_path, path)


# ---------------------------------------------------------------------------
# Survivors
# ---------------------------------------------------------------------------

def get_survivors():
    with _lock:
        return _read(SURVIVORS_FILE)


def upsert_survivor(survivor_id, fields):
    """Merge fields into the survivor with this id, creating it if needed.

    Fields not mentioned are kept, so a relay packet that only carries status
    and battery does not wipe the address sent earlier by the phone app.
    """

    with _lock:
        survivors = _read(SURVIVORS_FILE)

        for survivor in survivors:
            if survivor["id"] == survivor_id:
                survivor.update(fields)
                break
        else:
            survivor = {"id": survivor_id, **fields}
            survivors.append(survivor)

        _write(SURVIVORS_FILE, survivors)

        return dict(survivor)


# ---------------------------------------------------------------------------
# Drone readings
# ---------------------------------------------------------------------------

def get_readings(survivor_id=None):
    with _lock:
        readings = _read(READINGS_FILE)

    if survivor_id is None:
        return readings

    return [r for r in readings if r["survivor_id"] == survivor_id]


def append_reading(reading):
    with _lock:
        readings = _read(READINGS_FILE)
        readings.append(reading)
        _write(READINGS_FILE, readings)
