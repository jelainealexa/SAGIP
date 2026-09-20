# SAGIP

Search-and-Rescue Aerial Guidance and Intelligent Prioritization System for
Post-Earthquake Emergency Response.

Undergraduate thesis prototype, BS Computer Engineering, Polytechnic
University of the Philippines.

Members: Dayrit, Maigue, Malagiona, Villahermosa
Adviser: Dr. Ado

---

## What is in this repository

```
dashboard/          Flask command dashboard (Maigue)
  app.py            backend, API, response lifecycle, priority scoring
  templates/        base, operations, records, network pages
  static/css/       style.css plus the bundled Leaflet stylesheet
  static/js/        common.js and one script per page, plus Leaflet
  static/tiles/     offline map tiles for the pilot test area
tools/
  download_tiles.py fetches the offline tile pack
requirements.txt    Python dependencies
```

Firmware, the mobile app, and hardware files are not in here yet. Add them as
sibling folders when they exist.

---

## Running the dashboard

Every member sets this up once on their own machine. The virtual environment
is deliberately not in the repository, because it is large and specific to one
operating system.

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

**macOS or Linux**

```
git clone https://github.com/<owner>/<repo>.git
cd <repo>
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd dashboard
python3 app.py
```

Then open http://127.0.0.1:5000 in a browser.

Run `python app.py` from inside `dashboard/`, not from the repository root.
The tile path is relative to that folder.

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

**Stay in your own area where you can.** The dashboard is Maigue's. Firmware,
mobile and hardware get their own folders. Two people editing `app.py` on the
same afternoon is where conflicts come from.

**If a push is rejected**, somebody else pushed first. Run `git pull`, fix any
conflict markers, then push again. Do not force push.

**Never commit the `venv` folder.** `.gitignore` already excludes it. If it
somehow appears in `git status`, stop and ask before committing.

---

## The API schema is a contract

`dashboard/app.py` defines the fields the dashboard expects: `emergency_id`,
`device_id`, `reported_status`, `requested_assistance`, `position_source`,
`communication_path`, `hop_count`, and the relay heartbeat fields.

The phone app and the relay firmware have to produce exactly these. Write them
down in `docs/api-schema.md` and have everyone confirm each field is one they
can actually measure. A field the firmware cannot produce is a field that has
to come out of the dashboard, and finding that out during integration week is
expensive.