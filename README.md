# SAGIP

Search-and-Rescue Aerial Guidance and Intelligent Prioritization System for
Post-Earthquake Emergency Response.

Undergraduate thesis prototype, BS Computer Engineering, Polytechnic
University of the Philippines.

---

## What is in this repository

```
dashboard/          Flask command dashboard
  app.py            backend, API, response lifecycle, priority scoring
  templates/        base, operations, records, network pages
  static/css/       style.css plus the bundled Leaflet stylesheet
  static/js/        common.js and one script per page, plus Leaflet
  static/tiles/     offline map tiles for the pilot test area
backend/            hardware-facing API (port 5001)
  app.py            REST endpoints, wires the modules below together
  serial_reader.py  background thread reading the base station ESP32
  algorithm.py      RSSI weighted centroid localisation
  priority.py       priority scoring engine and status vocabulary
  routing.py        greedy drone route planner
  mavlink_ctrl.py   drone dispatch over pymavlink (MAVLink 1.0, APM 2.8)
  storage.py        JSON file storage shared by the API and serial thread
tools/
  download_tiles.py fetches the offline tile pack
```

Firmware, the mobile app, and hardware files are not in here yet. Add them as
sibling folders when they exist.

---

## Running the dashboard

**Windows**

```
git clone https://github.com/<owner>/<repo>.git
cd <repo>
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
cd dashboard
python app.py
```

Then open http://127.0.0.1:5000 in a browser.

Run `python app.py` from inside `dashboard/`, not from the repository root.
The tile path is relative to that folder.

---

## Running the backend

The backend talks to the hardware and runs next to the dashboard on port 5001.
CORS is enabled so dashboard pages can call it. Full documentation of every
feature, the API, the algorithms and testing is in
[backend/README.md](backend/README.md).

```
venv\Scripts\activate
pip install -r backend\requirements.txt
cd backend
set SAGIP_SERIAL_PORT=COM3
python app.py
```

It starts without the ESP32 or the drone connected. The serial reader retries
the COM port every 5 seconds, and the drone link opens on the first dispatch.

| Method | Path                   | Purpose                                            |
|--------|------------------------|----------------------------------------------------|
| POST   | `/sos`                 | SOS from the phone app                             |
| GET    | `/survivors`           | all survivors with priority and estimated location |
| GET    | `/estimated-locations` | weighted centroid estimate per survivor            |
| GET    | `/route`               | greedy route from the drone's position             |
| POST   | `/dispatch`            | `{lat, lon}`, `{survivor_id}`, or `{}` for route   |
| GET    | `/dispatch`            | dispatch progress                                  |
| POST   | `/release`             | writes `RELEASE\|<id>` to the base station         |
| GET    | `/health`              | serial and drone link status                       |

Data is kept in `backend/survivors.json` and `backend/drone_readings.json`.
Delete both to start from empty.

**Base station packet.** One line per packet, `STATUS_CODE` is the index into
`STATUS_NAMES` in `priority.py` (0 = CRITICAL SOS ... 5 = SAFE):

```
SOS|[ID]|[STATUS_CODE]|[BATTERY]|[RELAY_ID]|[NODE_LAT]|[NODE_LON]|[NODE_ALT]|[RSSI]
```

`NODE_LAT`, `NODE_LON` and `NODE_ALT` are the surveyed position of the relay
named in `RELAY_ID`, and `RSSI` is the BLE strength that relay measured from
the phone. They are set once by the relay that heard the device and are never
changed by a forwarding hop.

**Drone link.** Connect Mission Planner to the drone, then open the MAVLink
Mirror (Ctrl+F, "Mavlink"), choose UDP Client, port 14550, and tick
**Write access**. Without write access the drone never receives commands.

`mavlink_ctrl.py` removes `MAVLINK20` from the environment instead of setting
it to `"0"`. pymavlink only checks whether the variable exists, so `"0"` would
switch MAVLink 2.0 on, and the APM 2.8 cannot read that.

---

## Offline map tiles

The dashboard is meant to demonstrate operation when cellular service is
unavailable, so the map must not fetch tiles from the internet. The tile pack
for the pilot area is committed, so a fresh clone runs offline straight away.

To rebuild it for a different area, edit `TEST_AREA` and `CONTACT` in
`tools/download_tiles.py`, run it once with `ESTIMATE_ONLY = True` to see the
tile count, then set that to `False`.

The OpenStreetMap tile usage policy restricts bulk downloading. Keep the area
small and leave the request delay in place.

---

## State is in memory

The backend keeps reports, relays and aircraft state in Python variables.
Restarting `app.py` resets everything to the mock data. That is fine for
development and it is worth remembering before a demonstration: do not restart
the server halfway through.

---

## Working together without breaking each other

Four people editing the same files will collide. A few habits avoid most of it.

**Pull before you start.** Every session, before touching anything:

```
git pull
```

**Commit small and often.** One change, one commit, with a message that says
what changed:

```
git add .
git commit -m "Add relay heartbeat fields to network page"
git push
```

**Stay in your own area where you can.** Firmware, mobile and hardware get
their own folders.

**If a push is rejected**, somebody else pushed first. Run `git pull`, fix any
conflict markers, then push again. Do not force push.

**Never commit the `venv` folder.** `.gitignore` already excludes it. If it
somehow appears in `git status`, stop and ask before committing.
