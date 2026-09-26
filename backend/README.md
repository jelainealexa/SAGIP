# SAGIP Backend: Algorithm Integration

Hardware-facing backend for SAGIP (Offline UAV-Assisted Earthquake Survivor
Localization). It receives SOS reports, reads drone telemetry from the base
station, estimates survivor positions from RSSI, ranks survivors by priority,
plans a drone route, and dispatches the drone.

It runs next to the command dashboard (`dashboard/app.py`, port 5000) as a
separate service on **port 5001**, with CORS enabled so dashboard pages can
call it from the browser.

---

## Contents

1. [Features](#features)
2. [How the parts fit together](#how-the-parts-fit-together)
3. [Files](#files)
4. [Setup and running](#setup-and-running)
5. [REST API](#rest-api)
6. [Base station serial protocol](#base-station-serial-protocol)
7. [RSSI weighted centroid localisation](#rssi-weighted-centroid-localisation)
8. [Priority scoring](#priority-scoring)
9. [Route planning](#route-planning)
10. [Drone dispatch (pymavlink, MAVLink 1.0)](#drone-dispatch-pymavlink-mavlink-10)
11. [Data storage](#data-storage)
12. [Testing](#testing)
13. [Simulation results](#simulation-results)
14. [Open items for the team](#open-items-for-the-team)

---

## Features

| Feature | Where | Summary |
|---|---|---|
| REST API | `app.py` | SOS intake, survivor list, location estimates, route, dispatch, payload release, health |
| Serial telemetry reader | `serial_reader.py` | Background thread that reads the ESP32 base station, reconnects if unplugged |
| RSSI weighted centroid | `algorithm.py` | Estimates each survivor's position from the surveyed relay positions where it was heard |
| Priority scoring engine | `priority.py` | Scores status, battery, signal age and RSSI depth, with a per-factor breakdown |
| Greedy route planner | `routing.py` | Highest priority first, then nearest unvisited survivor scoring above 40 |
| Drone dispatch | `mavlink_ctrl.py` | pymavlink over MAVLink 1.0 for the APM 2.8: GUIDED, arm, take off to 20 m, fly to target |
| JSON file storage | `storage.py` | Thread-safe, crash-safe `survivors.json` and `drone_readings.json` |
| Automated tests | `tests/` | API/algorithm tests and a fake-drone MAVLink test |
| Packet simulator | `tools/simulate_packets.py` | Fake relay traffic for demos without hardware |

---

## How the parts fit together

```
 Phone app ──POST /sos──────────────────────────┐
                                                ▼
 ESP32 base station ──USB serial──► serial_reader.py ──► storage.py ◄── app.py (REST API, port 5001)
        ▲                                        survivors.json          │
        │                                        drone_readings.json     ├─ algorithm.py  (where)
        └──────── RELEASE|<id> ◄── POST /release                         ├─ priority.py   (how urgent)
                                                                         ├─ routing.py    (in what order)
 Drone (APM 2.8) ◄─radio─ Mission Planner ◄─UDP 14550─ mavlink_ctrl.py ◄─┘  POST /dispatch
```

1. A survivor's phone sends an SOS. It arrives either directly through
   `POST /sos` or through the relay network and the drone, ending up as a
   serial line from the base station.
2. Every serial packet carries the drone's position and the RSSI it heard,
   and is saved as one reading.
3. `algorithm.py` turns a survivor's readings into an estimated position.
4. `priority.py` scores each survivor, and `routing.py` orders them into a route.
5. `POST /dispatch` sends the drone to a coordinate, a survivor, or the first
   route waypoint.
6. `POST /release` tells the base station to drop the payload.

---

## Files

```
backend/
  app.py              Flask API. Wires every module below together.
  serial_reader.py    Base station reader thread and packet parser.
  algorithm.py        RSSI weighted centroid localisation.
  priority.py         Priority scoring and the status vocabulary.
  routing.py          Greedy route planner and distance helper.
  mavlink_ctrl.py     Drone dispatch over pymavlink (MAVLink 1.0).
  storage.py          JSON file storage shared by the API and serial thread.
  requirements.txt    Flask, flask-cors, pymavlink, pyserial.
  tests/
    test_backend.py   Parser, algorithm, priority, routing and API checks.
    test_mavlink.py   Dispatch sequence against a fake drone on UDP 14550.
  tools/
    simulate_packets.py   Simulated base station traffic.
```

`survivors.json` and `drone_readings.json` are created at runtime and are
excluded from git.

---

## Setup and running

From the repository root, on Windows:

```
python -m venv venv
venv\Scripts\activate
pip install -r backend\requirements.txt
cd backend
set SAGIP_SERIAL_PORT=COM3
python app.py
```

The API listens on http://127.0.0.1:5001. It starts without the ESP32 or the
drone attached: the serial reader retries the COM port every 5 seconds, and
the drone link only opens on the first dispatch.

| Environment variable | Default | Meaning |
|---|---|---|
| `SAGIP_SERIAL_PORT` | `COM3` | Base station COM port (Device Manager, Ports) |
| `SAGIP_SERIAL_BAUD` | `115200` | Base station baud rate |
| `SAGIP_API_PORT` | `5001` | Port this API listens on |

The server binds to `127.0.0.1` only, because `/dispatch` can arm the drone.
Do not expose it on a network.

---

## REST API

All bodies are JSON. Errors return `{"message": "..."}` with a 4xx/5xx status.

| Method | Path | Purpose |
|---|---|---|
| POST | `/sos` | Receive an SOS from the phone app |
| GET | `/survivors` | All survivors with priority and estimated location, highest priority first |
| GET | `/estimated-locations` | Weighted centroid estimate for every survivor with readings |
| GET | `/route` | Greedy route from the drone's current position |
| POST | `/dispatch` | Send the drone to a target (runs in the background) |
| GET | `/dispatch` | Progress of the current dispatch |
| POST | `/release` | Send the payload release command to the base station |
| GET | `/health` | Serial and drone link status |

### POST /sos

```json
{
  "id": "SGP-001",
  "status": "MEDICAL",
  "battery": 20,
  "address": "Blk 5 Lot 2",
  "household_size": 3,
  "assistance": ["WATER", "MEDICAL"],
  "timestamp": "2026-09-22T09:00:00+08:00"
}
```

- `id` and `status` are required. `status` accepts the names in
  [Priority scoring](#priority-scoring), plus `CRITICAL`, `SOS` and
  `ASSISTANCE` as short forms.
- `battery` must be 0–100, `household_size` at least 1.
- Returns `201` with the stored survivor. Sending the same `id` again updates it.

### GET /survivors

```json
[
  {
    "id": "SGP-001",
    "status": "CRITICAL SOS",
    "battery": 12.0,
    "address": "Blk 5 Lot 2",
    "last_seen": "2026-09-22T01:12:04+00:00",
    "priority": 150,
    "priority_breakdown": [
      {"factor": "Status", "detail": "CRITICAL SOS", "points": 100},
      {"factor": "Battery", "detail": 12.0, "points": 40},
      {"factor": "Signal age (min)", "detail": 2.1, "points": 0},
      {"factor": "RSSI depth (dBm)", "detail": -75.0, "points": 10}
    ],
    "latest_rssi": -75.0,
    "estimated_location": {"lat": 14.4571116, "lon": 120.9851116, "reading_count": 2}
  }
]
```

### GET /estimated-locations

```json
[{"survivor_id": "SGP-001", "lat": 14.4571116, "lon": 120.9851116, "reading_count": 2}]
```

### GET /route

Query `?drone_lat=..&drone_lon=..` is required when the live drone position is not
known from MAVLink or recent readings.

```json
{
  "start": {"lat": 14.458, "lon": 120.986},
  "waypoints": [
    {"survivor_id": "SGP-001", "lat": 14.4571116, "lon": 120.9851116, "priority": 150, "leg_distance_m": 137.5},
    {"survivor_id": "SGP-003", "lat": 14.458, "lon": 120.986, "priority": 75, "leg_distance_m": 137.5}
  ]
}
```

### POST /dispatch

One of, checked in this order:

| Body | Target |
|---|---|
| `{"lat": 14.4585, "lon": 120.9871}` | These coordinates |
| `{"survivor_id": "SGP-001"}` | That survivor's estimated location |
| `{}` | The first waypoint of the current route |

Returns `202` immediately. The flight sequence runs in the background; poll
`GET /dispatch` for its state: `IDLE`, `CONNECTING`, `LAUNCHING`, `EN_ROUTE`
or `FAILED` (with the reason in `message`). A second dispatch while one is
launching returns `409`.

### POST /release

`{"survivor_id": "SGP-001"}` (optional). Writes `RELEASE|SGP-001` (or
`RELEASE`) to the base station. Returns `503` if the ESP32 is not connected.

---

## Base station serial protocol

The ESP32 sends one line per packet:

```
SOS|[ID]|[STATUS_CODE]|[BATTERY]|[RELAY_ID]|[NODE_LAT]|[NODE_LON]|[NODE_ALT]|[RSSI]\n
```

Example: `SOS|SGP-001|0|12|R-02|14.457000|120.985000|3.0|-72`

`NODE_LAT`, `NODE_LON` and `NODE_ALT` are the surveyed position of the relay
named in `RELAY_ID`, and `RSSI` is the BLE signal strength that relay measured
from the phone.

Those four fields are written once, by the relay that heard the device, and
are never altered afterwards. A relay forwarding someone else's packet changes
only the hop counter. If a forwarding relay overwrote `RSSI` with the LoRa
strength it just measured, the value would describe the previous relay instead
of the phone, and every position the backend produced would be quietly wrong
rather than visibly broken.

The relays are the sensors here, not the drone. A drone measuring a LoRa
packet is measuring the relay that transmitted it, so drone-side RSSI carries
no information about where a survivor is.

`STATUS_CODE` is the index into `STATUS_NAMES` in `priority.py`:

| Code | Status |
|---|---|
| 0 | CRITICAL SOS |
| 1 | MEDICAL |
| 2 | UNCONFIRMED |
| 3 | NEED ASSISTANCE |
| 4 | EVACUATING |
| 5 | SAFE |

Handling rules:

- Lines not starting with `SOS|` (boot messages, debug prints) are ignored.
- Malformed packets are logged and dropped; they never stop the reader.
- Bytes are buffered and split on newlines, so a packet split across two
  reads is not lost.
- Each packet is appended to `drone_readings.json`, and the survivor's
  `status`, `battery`, `relay_id` and `last_seen` are updated in
  `survivors.json`. Fields from the phone app, such as the address, are kept.

Windows lets only one program open a COM port, so `/release` writes through
the reader's open connection. Close the Arduino Serial Monitor before starting
the backend.

---

## RSSI weighted centroid localisation

`algorithm.py` places a survivor at the weighted average of the surveyed relay
positions where its signal was heard:

```
weight        = 10 ** (RSSI / 10)
estimated_lat = sum(lat * weight) / sum(weight)
estimated_lon = sum(lon * weight) / sum(weight)
```

Readings with no usable node position, recorded as (0, 0), are skipped.
Survivors with no usable readings have no estimate.

Relay positions are surveyed once at installation with a tape from a fixed
benchmark. They are accurate to well under a metre and they do not drift, so
the relays need no GPS. A GPS module on each relay would report 2 to 3 m of
error forever and cost roughly ₱1,000 per node to make the reference worse.

`node_count` in the estimate is how many distinct relays contributed. It
decides what the estimate is worth, and the dashboard should say so rather
than presenting every estimate with equal confidence:

| Relays heard | Honest reading |
|---|---|
| 3 or more | a coordinate, with an error figure |
| 2 | narrowed to a line between two zones |
| 1 | proximity only, "near relay 3" |
| 0 | no detection |

A weighted centroid can never place a survivor outside the convex hull of the
relays that heard them, so accuracy degrades as the geometry worsens and a
device outside the relay square cannot be located well however clean the RSSI
is. That is a property of the method and belongs in the limitations, not
something to be tuned away.

See [Simulation results](#simulation-results) for measured accuracy.

---

## Priority scoring

`priority.py` adds four factors. The response includes a breakdown so the
operator can see why a survivor ranks where it does.

| Factor | Condition | Points |
|---|---|---|
| Status | CRITICAL SOS | +100 |
| | MEDICAL | +80 |
| | UNCONFIRMED | +60 |
| | NEED ASSISTANCE | +50 |
| | EVACUATING | +20 |
| | SAFE | 0 |
| Battery | below 15% | +40 |
| | 15% to below 30% | +25 |
| | 30% to 50% | +15 |
| | above 50% | 0 |
| Signal age | over 30 min | +30 |
| | 10 to 30 min | +15 |
| | under 10 min | 0 |
| RSSI depth (latest reading) | below −90 dBm | +20 |
| | −90 to −70 dBm | +10 |
| | above −70 dBm | 0 |

Signal age is measured from `last_seen`, the time the backend last received
anything from the device, not the phone's own timestamp, because phone clocks
can be wrong after an outage. Missing battery or RSSI scores 0 for that factor.

---

## Route planning

`routing.py` builds a greedy nearest-neighbour route:

1. Start at the drone's current position.
2. Go to the highest-priority survivor that has an estimated location
   (ties go to the closer one).
3. From there, repeatedly go to the nearest unvisited survivor with a score
   **above 40**.

Distances are great-circle (haversine) metres. The drone's start position
comes from, in order: live MAVLink position, then `drone_lat`/`drone_lon` in
the request. If neither is available the endpoint returns 409 rather than
guessing.

There used to be a third fallback that took the position from the most recent
reading. That was correct while readings carried the drone's own position.
They now carry the position of a relay, so the fallback would have answered
"where is the drone" with the location of a box bolted to a wall, and planned
a route from there without reporting anything wrong.

---

## Drone dispatch (pymavlink, MAVLink 1.0)

The APM 2.8 is an 8-bit flight controller that only understands MAVLink 1.0,
and DroneKit is not used. `mavlink_ctrl.py` uses pymavlink directly.

**Connection.** `udpin:127.0.0.1:14550`: the backend listens, and Mission
Planner (connected to the drone over the telemetry radio) mirrors the MAVLink
stream to it. Mission Planner setup:

1. Connect Mission Planner to the drone as normal.
2. Press Ctrl+F and click **Mavlink** (MAVLink Mirror).
3. Choose **UDP Client**, host `127.0.0.1`, port `14550`.
4. Tick **Write access**. Without it the drone never receives our commands.

**`dispatch_drone(target_lat, target_lon)`:**

1. Connect and wait for the vehicle heartbeat. Mission Planner's own
   heartbeats are ignored so commands are addressed to the drone.
2. Set mode `GUIDED` and wait for the heartbeat to confirm it.
3. If not already flying: arm, then `MAV_CMD_NAV_TAKEOFF` to 20 m, and wait
   until 95% of that altitude is reached.
4. Send `SET_POSITION_TARGET_GLOBAL_INT` in `MAV_FRAME_GLOBAL_RELATIVE_ALT_INT`
   with a position-only type mask, at 20 m.

If the drone is already airborne, step 3 is skipped and the drone is simply
redirected. Every step has a timeout, and failures include the drone's last
STATUSTEXT (for example a `PreArm:` message) in the error.

**Forcing MAVLink 1.0: important.** The original plan was to put
`os.environ['MAVLINK20'] = '0'` at the top of the script. That does the
opposite of what is intended: pymavlink only checks whether `MAVLINK20`
*exists*, not its value, so `'0'` switches MAVLink 2.0 **on**. This was
confirmed by test, where the commands went out with the MAVLink 2.0 start
byte `0xFD`. `mavlink_ctrl.py` instead:

- removes `MAVLINK20` from the environment before pymavlink is imported,
- refuses to load if pymavlink still picked MAVLink 2.0,
- disables pymavlink's automatic upgrade to MAVLink 2.0, which would otherwise
  happen if the first packet heard were MAVLink 2.0 (Mission Planner's own
  heartbeat can be).

`tests/test_mavlink.py` checks that every packet sent is MAVLink 1.0.

---

## Data storage

| File | Contents |
|---|---|
| `survivors.json` | One record per survivor: `id`, `status`, `battery`, `address`, `household_size`, `assistance`, `timestamp`, `last_seen`, `relay_id`, `source` |
| `drone_readings.json` | One record per base station packet: `survivor_id`, `status`, `battery`, `relay_id`, `node_lat`, `node_lon`, `node_alt`, `rssi`, `received_at` |

- All access goes through one lock, since the API and serial thread both write.
- Writes go to a `.tmp` file which is then swapped in, so a crash cannot leave
  half-written JSON.
- If a file is edited by hand into invalid JSON, the backend refuses to
  overwrite it rather than lose its contents.
- Delete both files to start from empty. They are in `.gitignore`.

---

## Testing

Run from the repository root. Test in stages so a failure points at one part.

**1. Automated tests, no hardware**

```
venv\Scripts\python.exe backend\tests\test_backend.py
venv\Scripts\python.exe backend\tests\test_mavlink.py
```

`test_backend.py` covers the parser, formula, every priority threshold, route
order and all endpoints, using a temporary folder. `test_mavlink.py` runs a
fake drone on UDP 14550 and checks the GUIDED, arm, takeoff and position
target sequence, and that every packet is MAVLink 1.0. Stop `app.py` first if
it has dispatched, since it holds port 14550.

**2. Server with simulated data**

```
cd backend
..\venv\Scripts\python.exe app.py
```

In a second terminal, from the repository root:

```
venv\Scripts\python.exe backend\tools\simulate_packets.py --reset
```

Then, in PowerShell:

```
Invoke-RestMethod http://127.0.0.1:5001/survivors | Format-Table id, status, battery, priority, latest_rssi
Invoke-RestMethod http://127.0.0.1:5001/route | ConvertTo-Json -Depth 5
```

**3. Real ESP32.** Set `SAGIP_SERIAL_PORT`, start the backend, and check
`/health` shows `"serial_connected": true`. Trigger a phone SOS and watch
for `SOS <id> rssi=...` in the console.

**4. Drone in simulation.** Use Mission Planner's Simulation tab (ArduCopter
SITL), set up the MAVLink Mirror as above, and send:

```
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/dispatch -ContentType "application/json" -Body '{"lat":14.4585,"lon":120.9871}'
```

**5. Real APM 2.8, propellers removed.** Confirm GUIDED, arming and takeoff
acceptance on the bench before any outdoor flight, and fly outdoors only
with a safety pilot on the RC transmitter.

---

## Simulation results

`tools/simulate_packets.py` places four relays in a square about 110 m on a
side and has each hear three survivors eight times over BLE. RSSI comes from a
log-distance path loss model (−40 dBm at 1 m, exponent 2.7, 3 dB noise,
−95 dBm sensitivity). Result with `--seed 1`:

| Survivor | Position | Relays heard | Error | Usable as |
|---|---|---|---|---|
| SIM-001 | inside the square | 4 | 15.8 m | coordinate |
| SIM-002 | near the edge | 3 | 20.5 m | coordinate |
| SIM-003 | well outside | 1 | 123.8 m | proximity only |

SIM-003 is placed outside the relay square deliberately. A centroid cannot
report a position outside the hull of the nodes that heard the device, so the
large error is the method being honest about geometry it cannot resolve. The
right output there is "near relay 3", not a coordinate, which is what
`node_count` is for.

Treat these figures as a check that the maths behaves, not as a predicted
field accuracy. Everything depends on the path loss exponent and the BLE
sensitivity, and both have to be measured in the real environment before any
error figure means anything.

### Why the weighting changed

The original weight was `1 / (abs(RSSI) + 1)`. It produces 0.0164 at −60 dBm
and 0.0090 at −110 dBm, under a 2x spread across the entire usable range, so
every node ends up weighted almost equally and the estimate collapses to the
plain geometric centre of whichever nodes heard the device. Signal strength
stops affecting the answer.

An earlier drone-grid simulation measured the effect directly: off-centre
survivors landed 192 to 198 m out under that formula against 12 to 21 m under
linear power, and the one survivor that looked accurate only did so because it
happened to sit at the centre of the grid.

`10 ** (RSSI / 10)` converts dBm back to linear power, the quantity that
actually falls off with distance, spanning about five orders of magnitude over
the same range.

---

## Open items for the team

- **Localisation weighting.** Settled: linear power, `10 ** (RSSI / 10)`.
  The comparison against the original formula is worth reporting as a finding
  rather than discarding, since it is a measured result.
- **Relay survey.** Every relay position in the node table must be measured
  with a tape from a fixed benchmark, not read off a phone GPS. Ground truth
  at ±3 m makes a 15 m error figure meaningless.
- **Path loss calibration.** Measure the exponent in the real environment,
  open air and through concrete separately, before quoting any accuracy
  number. The simulator's 2.7 is a placeholder.
- **Status code order.** The relay and phone firmware must send
  `STATUS_CODE` in the order in the table above.
- **Release command.** The ESP32 firmware must act on `RELEASE|<id>` lines.
- **APM firmware support.** Bench-test that your ArduCopter version on the
  APM 2.8 accepts `SET_POSITION_TARGET_GLOBAL_INT` in GUIDED mode.
- **Dashboard wiring.** `dashboard/app.py`'s `/api/reports` now calls this
  backend's `GET /survivors` live (see `fetch_reports()`), so reported
  status, priority and estimated location are real. Still not wired: the
  dashboard's "Dispatch UAV" button does not call this backend's
  `POST /dispatch`, so it does not arm or fly the aircraft yet, and the
  relay-telemetry and UAV-mission-substate panels stay on mock data because
  this backend has no data source for them (see below).
- **Status vocabulary.** The dashboard uses CRITICAL / ASSISTANCE / SAFE; this
  backend uses the six statuses above. `BACKEND_STATUS_TO_REPORTED` in
  `dashboard/app.py` is a provisional many-to-one mapping bridging the two;
  the two vocabularies themselves still need to be aligned with whoever owns
  the phone app and relay firmware.
- **No relay telemetry.** `GET /survivors` carries a `relay_id` per reading
  but nothing else about a relay (battery, RSSI, SNR, uptime). The
  dashboard's Network page has nowhere to get this from until either the
  relay firmware reports its own heartbeat or the base station protocol is
  extended to carry it.
- **No UAV mission sub-states.** `mavlink_ctrl.py` and `GET /dispatch` only
  expose `IDLE/CONNECTING/LAUNCHING/EN_ROUTE/FAILED`. The dashboard's mock
  UAV panel has additional states (`ASSIGNED`, `ON_STATION`, `RETURNING`),
  a payload list, battery, GNSS and link status with no equivalent here;
  wiring the panel for real means either simplifying it to match what this
  backend can report, or extending the backend to track more of the flight.
