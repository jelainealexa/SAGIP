"""Background reader for the base station ESP32 on a USB serial port.

The ESP32 forwards every packet it hears as one line:

    SOS|[ID]|[STATUS_CODE]|[BATTERY]|[RELAY_ID]|[NODE_LAT]|[NODE_LON]|[NODE_ALT]|[RSSI]\\n

NODE_LAT, NODE_LON and NODE_ALT are the surveyed position of the relay named
in RELAY_ID, and RSSI is the BLE signal strength that relay measured from the
phone. They are written once by the relay that heard the device and are never
altered by a forwarding hop: a relay that rebroadcasts a packet changes only
the hop counter.

This matters more than it looks. If a forwarding relay overwrote RSSI with the
LoRa strength it just measured, the value would describe the previous relay
rather than the phone, and the position the backend produces would be silently
wrong rather than obviously broken.

Each line becomes one reading in drone_readings.json, and the survivor's
latest status, battery and last_seen are updated in survivors.json.

Windows lets only one program open a COM port at a time, so the payload
release command is written through this same connection (send_line) instead
of opening the port a second time.
"""

import logging
import threading
import time

from datetime import datetime, timezone

import serial

import storage

from priority import status_from_code


log = logging.getLogger(__name__)

PACKET_FIELD_COUNT = 9

RECONNECT_DELAY_S = 5


class SerialUnavailable(Exception):
    """Raised when a command is sent while the base station is not connected."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_packet(line):
    """Parse one SOS line into a reading dict. Raises ValueError if malformed."""

    parts = [part.strip() for part in line.strip().split("|")]

    if len(parts) != PACKET_FIELD_COUNT or parts[0] != "SOS":
        raise ValueError(f"Not an SOS packet: {line.strip()!r}")

    _, survivor_id, status_code, battery, relay_id, lat, lon, alt, rssi = parts

    if not survivor_id:
        raise ValueError("SOS packet has an empty ID.")

    return {
        "survivor_id": survivor_id,
        "status": status_from_code(status_code),
        "battery": float(battery),
        "relay_id": relay_id,
        "node_lat": float(lat),
        "node_lon": float(lon),
        "node_alt": float(alt),
        "rssi": float(rssi),
        "received_at": now_iso()
    }


def record_reading(reading):
    storage.append_reading(reading)

    storage.upsert_survivor(reading["survivor_id"], {
        "status": reading["status"],
        "battery": reading["battery"],
        "relay_id": reading["relay_id"],
        "last_seen": reading["received_at"],
        "source": "relay"
    })


class SerialReader(threading.Thread):
    """Reads the base station continuously and reconnects if it is unplugged."""

    def __init__(self, port, baudrate=115200):
        super().__init__(name="serial-reader", daemon=True)

        self.port = port
        self.baudrate = baudrate

        self._serial = None
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()

    @property
    def connected(self):
        return self._serial is not None and self._serial.is_open

    def stop(self):
        self._stop_event.set()

    def send_line(self, text):
        """Write one command line to the base station."""

        with self._write_lock:
            if not self.connected:
                raise SerialUnavailable(
                    f"Base station is not connected on {self.port}."
                )

            self._serial.write((text + "\n").encode("ascii"))
            self._serial.flush()

    def run(self):
        warned = False

        while not self._stop_event.is_set():
            try:
                self._serial = serial.Serial(self.port, self.baudrate, timeout=1)
            except serial.SerialException as error:
                # Log once, not every retry, so an unplugged ESP32 does not
                # flood the console.
                if not warned:
                    log.warning("Cannot open %s (%s). Retrying every %ss.",
                                self.port, error, RECONNECT_DELAY_S)
                    warned = True

                self._stop_event.wait(RECONNECT_DELAY_S)
                continue

            warned = False
            log.info("Base station connected on %s.", self.port)

            try:
                self._read_loop()
            except serial.SerialException as error:
                log.warning("Base station on %s disconnected: %s", self.port, error)
            finally:
                with self._write_lock:
                    self._serial.close()
                    self._serial = None

    def _read_loop(self):
        # Buffer bytes and split on newlines ourselves. readline() returns a
        # partial line when its timeout expires mid-packet.
        buffer = b""

        while not self._stop_event.is_set():
            chunk = self._serial.read(self._serial.in_waiting or 1)

            if not chunk:
                continue

            buffer += chunk

            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self._handle_line(raw.decode("ascii", errors="replace"))

    def _handle_line(self, line):
        line = line.strip()

        if not line:
            return

        # The ESP32 also prints boot and debug messages on this port.
        if not line.startswith("SOS|"):
            log.debug("Base station: %s", line)
            return

        try:
            reading = parse_packet(line)
        except ValueError as error:
            log.warning("Dropped packet: %s", error)
            return

        try:
            record_reading(reading)
        except (OSError, ValueError) as error:
            log.error("Could not store reading from %s: %s",
                      reading["survivor_id"], error)
            return

        log.info("SOS %s heard by %s rssi=%s at (%s, %s)",
                 reading["survivor_id"], reading["relay_id"],
                 reading["rssi"], reading["node_lat"], reading["node_lon"])
