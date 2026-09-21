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
