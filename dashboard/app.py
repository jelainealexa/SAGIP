"""SAGIP command dashboard backend.

Reports, priority scores and estimated locations are pulled live from the
hardware-facing backend (backend/app.py, port 5001) via the functions in the
"Hardware backend connection" section below. What still has no counterpart on
that backend is kept as local, in-memory state or mock data, and is flagged
where it appears:

  - The response lifecycle (RECEIVED -> ... -> RESOLVED/NO_ACTION) and the
    ground-responder flag are operator workflow state the backend has no
    concept of, so the dashboard tracks them itself, keyed by survivor id
    (see _lifecycle below).
  - Relay telemetry (battery, RSSI, SNR, packet counts) and the UAV's
    richer mission sub-states (ASSIGNED/EN_ROUTE/ON_STATION/RETURNING,
    payload contents, GNSS/link status) have no backend source yet and stay
    mock data. See backend/README.md, "Open items for the team".
  - Actually triggering the backend's real MAVLink dispatch (which arms and
    flies the aircraft) is intentionally not wired to the "Dispatch UAV"
    button yet; that is a separate, higher-stakes change.

Terminology note: records are "emergency reports", not "survivors". The system
receives a report from a device. It does not confirm that a person is present,
alive, or in the reported condition.
"""

import math
import os

from datetime import datetime, timedelta, timezone

import requests

from flask import Flask, jsonify, render_template, request


app = Flask(__name__)

PH_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Hardware backend connection
#
# backend/app.py runs as a separate process on port 5001 and is the only
# thing that talks to the ESP32 base station and the drone. This dashboard
# calls it over HTTP rather than importing it, so the two can be started,
# restarted and deployed independently, exactly as the READMEs describe.
# ---------------------------------------------------------------------------

BACKEND_URL = os.environ.get("SAGIP_BACKEND_URL", "http://127.0.0.1:5001")
BACKEND_TIMEOUT_S = 3


class BackendUnavailable(Exception):
    """Raised when backend/app.py (port 5001) cannot be reached."""


def backend_get(path, params=None):
    try:
        response = requests.get(
            f"{BACKEND_URL}{path}", params=params, timeout=BACKEND_TIMEOUT_S
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        raise BackendUnavailable(
            f"Could not reach the hardware backend at {BACKEND_URL}{path}: {error}"
        ) from error


@app.errorhandler(BackendUnavailable)
def handle_backend_unavailable(error):
    return jsonify({"message": str(error)}), 502


# The backend's status vocabulary (backend/priority.py) is six values, aimed
# at keeping the LoRa payload small. The dashboard's is three. This mapping is
# a provisional bridge, not a firmware decision, and it should be revisited
# with whoever owns the phone app and relay firmware: see backend/README.md,
# "Open items for the team: Status vocabulary".
BACKEND_STATUS_TO_REPORTED = {
    "CRITICAL SOS": "CRITICAL",
    "MEDICAL": "CRITICAL",
    "UNCONFIRMED": "ASSISTANCE",
    "NEED ASSISTANCE": "ASSISTANCE",
    "EVACUATING": "ASSISTANCE",
    "SAFE": "SAFE"
}


# ---------------------------------------------------------------------------
# Offline map tiles
#
# The dashboard demonstrates operation when cellular service is unavailable, so
# the map must not depend on an internet tile server. Tiles for the pilot test
# area are saved under static/tiles/{z}/{x}/{y}.png by tools/download_tiles.py.
# ---------------------------------------------------------------------------

TILE_DIR = os.path.join(app.static_folder, "tiles")


def tile_pack_status():
    """Report whether an offline tile pack exists and which zooms it covers."""

    status = {
        "available": False,
        "min_zoom": None,
        "max_zoom": None,
        "tile_count": 0
    }

    if not os.path.isdir(TILE_DIR):
        return status

    zooms = sorted(
        int(name) for name in os.listdir(TILE_DIR)
        if name.isdigit() and os.path.isdir(os.path.join(TILE_DIR, name))
    )

    if not zooms:
        return status

    count = 0
    for _, _, files in os.walk(TILE_DIR):
        count += sum(1 for name in files if name.endswith(".png"))

    if count == 0:
        return status

    status["available"] = True
    status["min_zoom"] = zooms[0]
    status["max_zoom"] = zooms[-1]
    status["tile_count"] = count

    return status


# ---------------------------------------------------------------------------
# Pilot test area
#
# This polygon is the area the prototype is deployed and tested in. It is
# deliberately called the pilot test area and not a coverage area. Drawing a
# boundary does not demonstrate that communication succeeds everywhere inside
# it. Whether a report from a given point reaches the base station is an
# experimental result, not a property of this polygon.
# ---------------------------------------------------------------------------

PILOT_AREA = {
    "name": "SAGIP pilot test area",
    "note": (
        "Deployment and testing boundary. Communication success inside this "
        "boundary is measured, not assumed."
    ),
    "boundary": [
        [14.46150, 120.98080],
        [14.46140, 120.98920],
        [14.45420, 120.98960],
        [14.45260, 120.98620],
        [14.45300, 120.98150],
        [14.45720, 120.97990]
    ]
}


# ---------------------------------------------------------------------------
# Controlled vocabularies
#
# These are the only accepted values. The fallback path sends the numeric index
# of the enum, not the text, so that the BLE advertisement and LoRa payload stay
# small. The dashboard expands the code into readable text.
# ---------------------------------------------------------------------------

REPORTED_STATUS = ["CRITICAL", "ASSISTANCE", "SAFE"]

ASSISTANCE_CODES = [
    "TRAPPED",
    "MEDICAL",
    "WATER",
    "FOOD",
    "EXTRACTION",
    "SHELTER"
]

COMMUNICATION_PATHS = [
    "CELLULAR",
    "BLE_LORA_1HOP",
    "BLE_LORA_2HOP",
    "BLE_LORA_3HOP"
]

POSITION_SOURCES = [
    "PHONE_GNSS",      # coordinate came from the phone's own GNSS receiver
    "RELAY_ESTIMATE",  # coordinate estimated from relay measurements
    "NONE"             # no usable coordinate was received
]


# ---------------------------------------------------------------------------
# Response lifecycle
#
# Each transition is recorded with who or what caused it, because the
# distinction matters when the results are written up. Only EN_ROUTE and
# RESPONDED could in principle be confirmed by UAV telemetry. Everything else
# is an operator action. Until telemetry is wired in, every transition is
# operator-entered and the dashboard labels it that way.
#
# RECEIVED      the base station has the report, nobody has looked at it
# ACKNOWLEDGED  an operator has seen it and accepted it into the queue
# ASSIGNED      a UAV has been committed to it and given the coordinate
# EN_ROUTE      the UAV has launched and is travelling to the coordinate
# RESPONDED     the UAV reached the coordinate and performed its function
# RESOLVED      a ground responder closed the case
# NO_ACTION     terminal state for a report that needs no response
#
# RESPONDED and RESOLVED are separate on purpose. The UAV delivering a payload
# is not the same as a person being helped, and SAGIP must not claim that it
# rescues anyone. RESOLVED is entered by an operator on information from the
# ground, never by the system on its own.
# ---------------------------------------------------------------------------

RESPONSE_STATES = [
    "RECEIVED",
    "ACKNOWLEDGED",
    "ASSIGNED",
    "EN_ROUTE",
    "RESPONDED",
    "RESOLVED",
    "NO_ACTION"
]

# Which state each action may move a report from, and to.
TRANSITIONS = {
    "acknowledge": (["RECEIVED"], "ACKNOWLEDGED"),
    "dispatch": (["ACKNOWLEDGED"], "ASSIGNED"),
    "launch": (["ASSIGNED"], "EN_ROUTE"),
    "arrived": (["EN_ROUTE"], "RESPONDED"),
    # Resolvable from any open state. An operator can learn that a case was
    # handled on the ground at any moment, including while an aircraft is in
    # the air toward it, and the interface must not argue with that.
    "resolve": (
        ["RESPONDED", "EN_ROUTE", "ASSIGNED", "ACKNOWLEDGED", "RECEIVED"],
        "RESOLVED"
    ),
    "no_action": (["RECEIVED", "ACKNOWLEDGED"], "NO_ACTION")
}

# RESPONDED is an OPEN state. The UAV reaching a coordinate and releasing a
# payload is not the end of the case. Somebody still has to confirm what
# happened on the ground, and a case that still needs a responder must not
# disappear from the operator's queue because an aircraft flew over it.
OPEN_STATES = ["RECEIVED", "ACKNOWLEDGED", "ASSIGNED", "EN_ROUTE", "RESPONDED"]
CLOSED_STATES = ["RESOLVED", "NO_ACTION"]


# ---------------------------------------------------------------------------
# Who goes: UAV, ground responder, or both
#
# The UAV carries a small payload. That helps with a supply request and does
# nothing for a person who is trapped or injured, so the requested assistance
# decides the route rather than the reported urgency alone.
#
# Splitting the request codes this way also answers a question a panel will
# certainly ask: why send a drone to someone under rubble? Under these rules it
# does not. It goes where a delivery is the useful thing to do.
# ---------------------------------------------------------------------------

# Requests a payload drop can actually satisfy.
UAV_DELIVERABLE = ["WATER", "FOOD", "SHELTER"]

# Requests that need a person on scene. No payload substitutes for these.
NEEDS_RESPONDER = ["TRAPPED", "MEDICAL", "EXTRACTION"]

RESPONDER_STATES = ["NONE", "REQUESTED", "ON_SCENE"]


def routing(report):
    """Decide which resources this report calls for.

    Returns the UAV-deliverable requests, the requests that need a person, and
    whether a ground responder is required regardless of what was requested.
    """

    requested = report.get("requested_assistance", [])

    deliverable = [code for code in requested if code in UAV_DELIVERABLE]
    responder_items = [code for code in requested if code in NEEDS_RESPONDER]

    # A report marked critical always calls for a responder, even when the
    # person asked for nothing. "Critical" with no request is the case where a
    # payload drop is plainly not the answer.
    responder_required = (
        bool(responder_items) or report["reported_status"] == "CRITICAL"
    )

    if report["reported_status"] == "SAFE":
        responder_required = False

    reason = None

    if not deliverable:
        if responder_items:
            labels = ", ".join(code.title() for code in responder_items)
            reason = (
                f"Requested assistance ({labels}) needs a person on scene. "
                "A payload drop would not help."
            )
        elif requested:
            reason = "Nothing requested here can be delivered by the UAV."
        else:
            reason = (
                "No assistance was requested, so there is nothing for the UAV "
                "to deliver."
            )

    return {
        "uav_deliverable": deliverable,
        "responder_items": responder_items,
        "uav_eligible": bool(deliverable),
        "responder_required": responder_required,
        "uav_blocked_reason": reason
    }


# ---------------------------------------------------------------------------
# Priority scoring
#
# The score itself is now computed by the hardware backend (backend/priority.py)
# and arrives on every survivor from GET /survivors, breakdown included. The
# dashboard used to keep its own, differently-weighted copy of this logic; that
# was dropped rather than kept as a second, disagreeing scoring engine. The
# backend's weights are provisional in the same sense the old ones were, and
# any change to them is backend/priority.py's concern, not this file's.
#
# The score orders the queue. It is deliberately not shown as a number on the
# main dashboard, because a precise-looking figure invites the reader to treat
# it as a measurement. It is available in full, with its breakdown, on the
# record page and through the API, which is what the evaluation for RQ2 needs.
# ---------------------------------------------------------------------------

# Position accuracy worse than this is called out before a UAV is dispatched.
POOR_ACCURACY_M = 30


def report_age_minutes(report, now):
    received_at = datetime.fromisoformat(report["received_at"])

    return max(int((now - received_at).total_seconds() // 60), 0)


def distance_m(lat_a, lon_a, lat_b, lon_b):
    """Great-circle distance in metres.

    This is straight-line distance, not flight distance. The UAV does not fly
    a straight line in practice, so this figure is a lower bound on the travel
    distance and must be described that way.
    """

    radius = 6371000.0

    phi_a = math.radians(lat_a)
    phi_b = math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lon_b - lon_a)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    )

    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


def dispatch_block_reason(report):
    """Why this report cannot be given to the UAV, or None if it can."""

    if report["reported_status"] == "SAFE":
        return "Report is marked safe. No response required."

    if report["response_status"] in CLOSED_STATES:
        return "Report is already closed."

    if report["response_status"] != "ACKNOWLEDGED":
        if report["response_status"] == "RECEIVED":
            return "Acknowledge the report before assigning a UAV."

        return "A UAV is already assigned to this report."

    # Nothing to deliver means there is no job for the aircraft, whatever the
    # reported urgency. This is the rule that keeps the UAV from being sent to
    # a trapped person with a water pouch.
    route = routing(report)

    if not route["uav_eligible"]:
        return route["uav_blocked_reason"]

    if report["latitude"] is None or report["longitude"] is None:
        return "No usable coordinate. A UAV destination cannot be set."

    if uav["payload_state"] != "LOADED":
        return "UAV payload has been released. Reload before dispatching."

    if uav["mission_state"] != "STANDBY":
        return f"UAV is already committed to {uav['assigned_report']}."

    return None


def dispatch_warnings(report):
    """Conditions an operator should see before committing the UAV."""

    warnings = []

    if report["position_source"] == "RELAY_ESTIMATE":
        warnings.append(
            "Position is estimated from relay measurements, not from the "
            "phone's own GNSS receiver."
        )

    accuracy = report.get("position_accuracy_m")

    if accuracy is not None and accuracy > POOR_ACCURACY_M:
        warnings.append(
            f"Stated position uncertainty is {accuracy} m. The UAV will arrive "
            f"at the centre of an area roughly {accuracy * 2} m across."
        )

    if report["latitude"] is not None:
        travel = distance_m(
            uav["current_latitude"], uav["current_longitude"],
            report["latitude"], report["longitude"]
        )

        if travel > 800:
            warnings.append(
                f"Straight-line distance is {travel:.0f} m. Check endurance "
                "against the measured flight time for the loaded aircraft."
            )

    if uav["battery_pct"] < 40:
        warnings.append(
            f"UAV battery is {uav['battery_pct']}%."
        )

    if not uav["payload"]:
        warnings.append("No payload is loaded.")

    return warnings


# ---------------------------------------------------------------------------
# Reports: live from the backend, with locally-tracked workflow state
#
# A "report" record is a merge of two things that come from different places:
#
#   - What was reported (status, requested assistance, battery, estimated
#     location, priority) comes fresh from the backend's GET /survivors on
#     every request. It is never stored here.
#   - What the operator has done about it (the response lifecycle, the
#     ground-responder flag) has no backend counterpart, so it is kept here,
#     keyed by survivor id, and survives across requests for as long as this
#     process runs (see "State is in memory" in the top-level README).
# ---------------------------------------------------------------------------

_lifecycle = {}


def _lifecycle_for(survivor_id, reported_status):
    """The operator-workflow state for one survivor id, created on first sight.

    A SAFE check-in never enters the response lifecycle (see the
    RESPONSE_STATES documentation above); everything else starts at RECEIVED.
    """

    state = _lifecycle.get(survivor_id)

    if state is None:
        initial = "NO_ACTION" if reported_status == "SAFE" else "RECEIVED"

        state = {
            "response_status": initial,
            "responder_status": "NONE",
            "responder_requested_at": None,
            "history": [{
                "at": datetime.now(PH_TZ).isoformat(),
                "state": initial,
                "by": "system"
            }]
        }

        _lifecycle[survivor_id] = state

    return state


def fetch_reports():
    """Every survivor from the backend, reshaped into a report record and
    merged with its locally-tracked lifecycle state.

    Raises BackendUnavailable if backend/app.py cannot be reached.
    """

    survivors = backend_get("/survivors")

    records = []

    for survivor in survivors:
        reported_status = BACKEND_STATUS_TO_REPORTED.get(
            survivor.get("status"), "ASSISTANCE"
        )

        state = _lifecycle_for(survivor["id"], reported_status)
        location = survivor.get("estimated_location")

        records.append({
            "emergency_id": survivor["id"],
            # The backend has no separate device identifier from the
            # survivor id, so the two are the same value here.
            "device_id": survivor["id"],
            "reported_status": reported_status,
            "requested_assistance": survivor.get("assistance") or [],
            "note": None,
            "address": survivor.get("address"),
            "received_at": survivor.get("last_seen") or survivor.get("timestamp"),
            "latitude": location["lat"] if location else None,
            "longitude": location["lon"] if location else None,
            "position_source": "RELAY_ESTIMATE" if location else "NONE",
            # Not available: the backend reports a reading count, not a
            # position uncertainty in metres.
            "position_accuracy_m": None,
            # Not available: the backend does not record the communication
            # path, hop count or a named zone per report.
            "communication_path": None,
            "hop_count": None,
            "zone": None,
            "via_relay": survivor.get("relay_id"),
            "device_battery_pct": survivor.get("battery"),
            "priority_score": survivor.get("priority", 0),
            "priority_breakdown": survivor.get("priority_breakdown", []),
            "response_status": state["response_status"],
            "responder_status": state["responder_status"],
            "responder_requested_at": state["responder_requested_at"],
            "history": state["history"]
        })

    return records


# ---------------------------------------------------------------------------
# Relay sites
#
# Where relay hardware is sited is a placement fact the team decides, like
# BASE_STATION and PILOT_AREA below, not sensor telemetry, so it can be shown
# without any hardware attached. These four coordinates were carried over
# from this file's earlier mock data and have NOT been confirmed as a real
# survey; replace them with the team's actual (or planned) site coordinates.
#
# Per-relay telemetry (battery, RSSI, SNR, packets forwarded, online/offline)
# is a different thing: the backend only ever sees a relay_id string on a
# packet, never a heartbeat from the relay itself, so there is nowhere to get
# it from yet. Every relay below carries "UNKNOWN"/None for those fields
# rather than an invented number; see backend/README.md, "No relay telemetry".
# ---------------------------------------------------------------------------

RELAY_SITES = [
    {"relay_id": "R-01", "label": "Relay 01", "site": "Covered court roof",
     "latitude": 14.45905, "longitude": 120.98310},
    {"relay_id": "R-02", "label": "Relay 02", "site": "Barangay hall mast",
     "latitude": 14.45640, "longitude": 120.98130},
    {"relay_id": "R-03", "label": "Relay 03", "site": "School water tank",
     "latitude": 14.45455, "longitude": 120.98720},
    {"relay_id": "R-04", "label": "Relay 04", "site": "Perimeter pole, east",
     "latitude": 14.46020, "longitude": 120.98780}
]

relays = [
    {
        **site,
        "status": "UNKNOWN",
        "battery_pct": None,
        "solar_charging": None,
        "last_heartbeat": None,
        "uplink_rssi_dbm": None,
        "uplink_snr_db": None,
        "hops_to_base": None,
        "packets_forwarded": None,
        "duplicates_suppressed": None
    }
    for site in RELAY_SITES
]

activity_log = []


# Payload is what the aircraft is physically carrying right now. Section 13 of
# the project brief is explicit that the final payload must follow from measured
# lift capacity, so the masses here are placeholders until the aircraft has been
# weighed and flown. The dashboard shows the total so an operator can see when
# the load has changed.
# Where the aircraft launches from and returns to. Replace these coordinates
# with the actual launch point for your pilot test area before the field tests.
BASE_STATION = {
    "name": "Base station",
    "latitude": 14.45870,
    "longitude": 120.98240
}


# The aircraft's own state machine, separate from the case lifecycle.
#
# STANDBY     on the ground at base, available
# ASSIGNED    committed to a report, not yet launched
# EN_ROUTE    flying out to the coordinate
# ON_STATION  at the coordinate, performing its response
# RETURNING   flying back to base
#
# The return leg is a state of its own because it is the part of the sortie
# where the aircraft is still airborne, still consuming battery, and still
# unavailable for the next case. Dropping straight from arrival back to standby
# would hide a leg of flight that is exactly as long as the outbound one.
UAV_STATES = ["STANDBY", "ASSIGNED", "EN_ROUTE", "ON_STATION", "RETURNING"]

uav = {
    "mission_state": "STANDBY",
    "battery_pct": 78,
    "gnss_fix": "3D_FIX",
    "link": "CONNECTED",
    "payload": [
        {"code": "WATER", "label": "Water pouch, 500 mL", "mass_g": 520},
        {"code": "FIRST_AID", "label": "Compact first-aid kit", "mass_g": 180},
        {"code": "LIGHT", "label": "Marker light and whistle", "mass_g": 60}
    ],
    "payload_state": "LOADED",

    # Return-to-launch handled by the flight controller. Switch off to record
    # a sortie where it failed or was not armed.
    "auto_rtl": True,

    "assigned_report": None,
    "target_latitude": None,
    "target_longitude": None,
    "current_latitude": BASE_STATION["latitude"],
    "current_longitude": BASE_STATION["longitude"],
    "home_latitude": BASE_STATION["latitude"],
    "home_longitude": BASE_STATION["longitude"]
}


comms = {
    "cellular_uplink": "DOWN"
}


ACTIVITY_LOG_LIMIT = 500


def log_event(event, zone=None):
    del activity_log[ACTIVITY_LOG_LIMIT - 1:]

    activity_log.insert(0, {
        "timestamp": datetime.now(PH_TZ).isoformat(),
        "event": event,
        "zone": zone or "-"
    })


log_event("Base station started", "-")


def payload_mass_g():
    return sum(item["mass_g"] for item in uav["payload"])


def serialize_uav():
    record = dict(uav)
    record["payload_mass_g"] = payload_mass_g()
    record["base"] = BASE_STATION
    record["target_distance_m"] = None

    if uav["target_latitude"] is not None:
        record["target_distance_m"] = distance_m(
            uav["current_latitude"], uav["current_longitude"],
            uav["target_latitude"], uav["target_longitude"]
        )

    # Distance home is reported on every leg, not only while returning. An
    # operator deciding whether the aircraft can take one more task needs to
    # know how far it is from base at that moment.
    record["home_distance_m"] = distance_m(
        uav["current_latitude"], uav["current_longitude"],
        uav["home_latitude"], uav["home_longitude"]
    )

    record["is_airborne"] = uav["mission_state"] in (
        "EN_ROUTE", "ON_STATION", "RETURNING"
    )

    return record


def serialize_reports():
    now = datetime.now(PH_TZ)
    output = []

    for report in fetch_reports():
        # priority_score and priority_breakdown are already on the report,
        # computed by the backend.
        record = dict(report)
        record["age_minutes"] = report_age_minutes(report, now)
        record["is_open"] = report["response_status"] in OPEN_STATES
        record["is_located"] = report["latitude"] is not None
        record["blocked_reason"] = dispatch_block_reason(report)
        record["can_dispatch"] = record["blocked_reason"] is None
        record["routing"] = routing(report)

        if record["is_located"]:
            record["uav_distance_m"] = distance_m(
                uav["current_latitude"], uav["current_longitude"],
                report["latitude"], report["longitude"]
            )
        else:
            record["uav_distance_m"] = None

        output.append(record)

    # Open reports first, then by score, then oldest first. A closed report
    # never outranks an open one however urgent it was.
    output.sort(
        key=lambda item: (
            not item["is_open"],
            -item["priority_score"],
            -item["age_minutes"]
        )
    )

    rank = 0
    for record in output:
        if record["is_open"] and record["reported_status"] != "SAFE":
            rank += 1
            record["priority_rank"] = rank
        else:
            record["priority_rank"] = None

    return output


def summary_counts(records):
    """The four figures on the main dashboard.

    Active and unlocated overlap on purpose. An unlocated report is still an
    active one, and hiding it inside the active total is exactly the fact the
    operator most needs to see.
    """

    return {
        "active": sum(1 for r in records if r["is_open"]),
        "critical": sum(
            1 for r in records
            if r["is_open"] and r["reported_status"] == "CRITICAL"
        ),
        "unlocated": sum(
            1 for r in records if r["is_open"] and not r["is_located"]
        ),
        "responded": sum(
            1 for r in records
            if r["response_status"] in ("RESPONDED", "RESOLVED")
        )
    }


def relay_health():
    # Sites can be known (see RELAY_SITES above) while their status is still
    # UNKNOWN, since no relay reports a heartbeat to the backend today. That
    # is "not available", not "every relay is reachable" or "every relay is
    # down" -- neither of which this dashboard can actually claim yet.
    known = [relay for relay in relays if relay["status"] != "UNKNOWN"]

    if not known:
        return {"state": "NOT_AVAILABLE", "reachable": 0, "degraded": 0, "total": len(relays)}

    reachable = sum(1 for relay in known if relay["status"] == "ONLINE")
    degraded = sum(1 for relay in known if relay["status"] == "DEGRADED")

    if reachable == len(known):
        state = "OPERATIONAL"
    elif reachable + degraded == 0:
        state = "DOWN"
    else:
        state = "DEGRADED"

    return {
        "state": state,
        "reachable": reachable,
        "degraded": degraded,
        "total": len(relays)
    }


def system_health():
    health = relay_health()
    tiles = tile_pack_status()

    try:
        base_station_up = bool(backend_get("/health").get("serial_connected"))
    except BackendUnavailable:
        base_station_up = False

    return {
        "cellular_uplink": comms["cellular_uplink"],
        "base_station": "OPERATIONAL" if base_station_up else "DOWN",
        "lora_network": health["state"],
        "ble_relays": (
            "Not available" if health["state"] == "NOT_AVAILABLE"
            else f"{health['reachable']}/{health['total']} reachable"
        ),
        "relay_detail": health,
        "offline_map": "AVAILABLE" if tiles["available"] else "NOT AVAILABLE",
        "uav_link": uav["link"],
        "mode": (
            "ONLINE" if comms["cellular_uplink"] == "UP" else "OFFLINE"
        ),
        "checked_at": datetime.now(PH_TZ).isoformat()
    }


def find_report(emergency_id):
    return next(
        (item for item in fetch_reports() if item["emergency_id"] == emergency_id),
        None
    )


def apply_transition(report, action, by="operator"):
    """Move a report to the next state if the action is allowed from here.

    Writes go to _lifecycle, the persistent store, not to `report` itself:
    `report` is a fresh dict built by fetch_reports() on every call and is
    discarded as soon as this request finishes.
    """

    state = _lifecycle[report["emergency_id"]]
    allowed_from, target = TRANSITIONS[action]

    if state["response_status"] not in allowed_from:
        return False, (
            f"Cannot {action.replace('_', ' ')} a report that is "
            f"{state['response_status'].replace('_', ' ').lower()}."
        )

    state["response_status"] = target
    state["history"].append({
        "at": datetime.now(PH_TZ).isoformat(),
        "state": target,
        "by": by
    })

    report["response_status"] = target

    return True, None


def clear_assignment():
    """Detach the aircraft from a case without touching where it is.

    This used to be called release_uav() and also set the state to STANDBY.
    That was wrong: standby means parked at base, and an aircraft sitting at a
    survivor's coordinate is not parked. Resolving a case while the UAV was out
    therefore marked it home, which removed every return control and left no
    way to fly it back.

    Only land_uav() may put the aircraft in STANDBY.
    """

    uav["assigned_report"] = None
    uav["target_latitude"] = None
    uav["target_longitude"] = None


def send_home():
    """Put an airborne aircraft on the return leg."""

    uav["mission_state"] = "RETURNING"
    uav["assigned_report"] = None
    uav["target_latitude"] = uav["home_latitude"]
    uav["target_longitude"] = uav["home_longitude"]


def land_uav():
    uav["mission_state"] = "STANDBY"
    uav["current_latitude"] = uav["home_latitude"]
    uav["current_longitude"] = uav["home_longitude"]
    clear_assignment()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard_page():
    return render_template("dashboard.html", active_page="dashboard")


@app.route("/records")
def records_page():
    return render_template("records.html", active_page="records")


@app.route("/network")
def network_page():
    return render_template("network.html", active_page="network")


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "map_center": [14.4579, 120.9848],
        "default_zoom": 15,
        "tiles": tile_pack_status(),
        "area": PILOT_AREA,
        "base": BASE_STATION,
        "poor_accuracy_m": POOR_ACCURACY_M
    })


@app.route("/api/reports", methods=["GET"])
def get_reports():
    records = serialize_reports()

    return jsonify({
        "reports": records,
        "summary": summary_counts(records)
    })


@app.route("/api/reports/<emergency_id>", methods=["GET"])
def get_report(emergency_id):
    record = next(
        (
            item for item in serialize_reports()
            if item["emergency_id"] == emergency_id
        ),
        None
    )

    if record is None:
        return jsonify({"message": "Emergency report not found."}), 404

    return jsonify(record)


@app.route("/api/uav", methods=["GET"])
def get_uav():
    return jsonify(serialize_uav())


@app.route("/api/relays", methods=["GET"])
def get_relays():
    response = {"relays": relays, "health": relay_health()}

    if relay_health()["state"] == "NOT_AVAILABLE":
        response["note"] = (
            "Sites shown are where relay hardware is planned to be "
            "installed. Live telemetry (battery, RSSI, SNR, packet counts, "
            "online/offline) is not reported by the backend yet, so those "
            "columns read \"--\" until that data is available."
        )

    return jsonify(response)


@app.route("/api/system", methods=["GET"])
def get_system():
    return jsonify(system_health())


@app.route("/api/activity", methods=["GET"])
def get_activity():
    return jsonify(activity_log)


@app.route("/api/dispatch/preview/<emergency_id>", methods=["GET"])
def dispatch_preview(emergency_id):
    """What the operator is shown before committing the UAV.

    The confirmation step exists because the coordinate may be an estimate. An
    operator who sees a pin on a map naturally reads it as a known position,
    and this screen is where that assumption gets corrected.
    """

    report = find_report(emergency_id)

    if report is None:
        return jsonify({"message": "Emergency report not found."}), 404

    blocked = dispatch_block_reason(report)

    payload = {
        "emergency_id": report["emergency_id"],
        "reported_status": report["reported_status"],
        "requested_assistance": report["requested_assistance"],
        "position_source": report["position_source"],
        "position_accuracy_m": report["position_accuracy_m"],
        "latitude": report["latitude"],
        "longitude": report["longitude"],
        "blocked_reason": blocked,
        "can_dispatch": blocked is None,
        "warnings": [] if blocked else dispatch_warnings(report),
        "uav_battery_pct": uav["battery_pct"],
        "payload": uav["payload"],
        "payload_mass_g": payload_mass_g()
    }

    if report["latitude"] is not None:
        payload["straight_line_distance_m"] = distance_m(
            uav["current_latitude"], uav["current_longitude"],
            report["latitude"], report["longitude"]
        )
    else:
        payload["straight_line_distance_m"] = None

    return jsonify(payload)


# ---------------------------------------------------------------------------
# Action endpoints
#
# The server enforces the lifecycle. A disabled button in the browser is not a
# safeguard, since the endpoint can still be called directly.
# ---------------------------------------------------------------------------

def _report_from_request():
    data = request.get_json(silent=True) or {}

    return find_report(data.get("emergency_id"))


@app.route("/api/acknowledge", methods=["POST"])
def acknowledge():
    report = _report_from_request()

    if report is None:
        return jsonify({"message": "Emergency report not found."}), 404

    ok, error = apply_transition(report, "acknowledge")

    if not ok:
        return jsonify({"message": error}), 409

    log_event(f"{report['emergency_id']} acknowledged", report["zone"])

    return jsonify({"message": f"{report['emergency_id']} acknowledged."})


@app.route("/api/no-action", methods=["POST"])
def no_action():
    report = _report_from_request()

    if report is None:
        return jsonify({"message": "Emergency report not found."}), 404

    ok, error = apply_transition(report, "no_action")

    if not ok:
        return jsonify({"message": error}), 409

    log_event(
        f"{report['emergency_id']} closed, no action required",
        report["zone"]
    )

    return jsonify({"message": "Report closed with no action required."})


@app.route("/api/dispatch", methods=["POST"])
def dispatch_uav():
    report = _report_from_request()

    if report is None:
        return jsonify({"message": "Emergency report not found."}), 404

    blocked = dispatch_block_reason(report)

    if blocked:
        return jsonify({"message": blocked}), 409

    ok, error = apply_transition(report, "dispatch")

    if not ok:
        return jsonify({"message": error}), 409

    uav["mission_state"] = "ASSIGNED"
    uav["assigned_report"] = report["emergency_id"]
    uav["target_latitude"] = report["latitude"]
    uav["target_longitude"] = report["longitude"]

    log_event(
        f"UAV assigned to {report['emergency_id']}",
        report["zone"]
    )

    return jsonify({
        "message": f"UAV assigned to {report['emergency_id']}."
    })


@app.route("/api/uav/launch", methods=["POST"])
def launch():
    if uav["mission_state"] != "ASSIGNED":
        return jsonify({"message": "No assigned mission to launch."}), 409

    report = find_report(uav["assigned_report"])

    if report is None:
        return jsonify({
            "message": "The assigned report no longer exists. Cancel the mission."
        }), 409

    ok, error = apply_transition(report, "launch")

    if not ok:
        return jsonify({"message": error}), 409

    uav["mission_state"] = "EN_ROUTE"

    log_event(
        f"UAV launched toward {report['emergency_id']}",
        report["zone"]
    )

    return jsonify({"message": "UAV en route."})


@app.route("/api/uav/arrived", methods=["POST"])
def arrived():
    """The UAV reached the coordinate and released its payload.

    This does not close the case and it does not free the aircraft. The case
    stays open until somebody confirms what happened on the ground, and the
    aircraft is still at the coordinate and still has to fly home.
    """

    if uav["mission_state"] != "EN_ROUTE":
        return jsonify({"message": "UAV is not en route."}), 409

    report = find_report(uav["assigned_report"])

    if report is None:
        return jsonify({
            "message": "The assigned report no longer exists. Cancel the mission."
        }), 409

    ok, error = apply_transition(report, "arrived")

    if not ok:
        return jsonify({"message": error}), 409

    uav["mission_state"] = "ON_STATION"
    uav["payload_state"] = "RELEASED"
    uav["current_latitude"] = uav["target_latitude"]
    uav["current_longitude"] = uav["target_longitude"]

    log_event(
        f"Payload released at {report['emergency_id']}",
        report["zone"]
    )

    route = routing(report)

    if route["responder_required"] and report["responder_status"] == "NONE":
        message = (
            "Payload released. This case still needs a ground responder, and "
            "stays open until one confirms the outcome."
        )
    else:
        message = (
            "Payload released. The case stays open until the outcome is "
            "confirmed on the ground."
        )

    # Return to base is a flight-controller function, not an operator task. The
    # delivery is the last thing the mission asks of the operator; the flight
    # home is commanded automatically the moment the payload is away.
    #
    # It is a flag rather than an assumption because return-to-launch can fail,
    # and the field tests have to be able to record a sortie where it did. With
    # it off, the aircraft holds at the coordinate and waits to be told.
    if uav["auto_rtl"]:
        send_home()
        log_event("Auto return-to-base engaged")

        distance = distance_m(
            uav["current_latitude"], uav["current_longitude"],
            uav["home_latitude"], uav["home_longitude"]
        )

        message += (
            f" Auto return-to-base engaged, {distance:.0f} m to run."
        )

    return jsonify({"message": message})


@app.route("/api/uav/return", methods=["POST"])
def return_to_base():
    if uav["mission_state"] not in ("ON_STATION", "EN_ROUTE"):
        return jsonify({
            "message": "UAV is not airborne on a mission."
        }), 409

    uav["mission_state"] = "RETURNING"
    uav["target_latitude"] = uav["home_latitude"]
    uav["target_longitude"] = uav["home_longitude"]

    log_event("UAV returning to base")

    distance = distance_m(
        uav["current_latitude"], uav["current_longitude"],
        uav["home_latitude"], uav["home_longitude"]
    )

    return jsonify({
        "message": f"UAV returning to base, {distance:.0f} m straight-line."
    })


@app.route("/api/uav/landed", methods=["POST"])
def landed():
    if uav["mission_state"] != "RETURNING":
        return jsonify({"message": "UAV is not returning to base."}), 409

    land_uav()

    log_event("UAV landed at base")

    if uav["payload_state"] != "LOADED":
        return jsonify({
            "message": "UAV landed. Reload the payload before the next sortie."
        })

    return jsonify({"message": "UAV landed and available."})


@app.route("/api/uav/auto-rtl", methods=["POST"])
def set_auto_rtl():
    """Arm or disarm automatic return-to-launch.

    Return-to-launch is a flight-controller behaviour and it can fail or be
    left unarmed. The flag exists so a field test can record a sortie flown
    without it, rather than the dashboard assuming it always works.
    """

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))

    uav["auto_rtl"] = enabled
    log_event(
        "Auto return-to-base armed" if enabled
        else "Auto return-to-base disarmed"
    )

    return jsonify({
        "message": (
            "Auto return-to-base armed." if enabled
            else "Auto return-to-base disarmed. The aircraft will hold at the "
                 "coordinate until told to return."
        )
    })


@app.route("/api/uav/reload", methods=["POST"])
def reload_payload():
    if uav["mission_state"] != "STANDBY":
        return jsonify({
            "message": "The aircraft has to be on the ground to be loaded."
        }), 409

    if uav["payload_state"] == "LOADED":
        return jsonify({"message": "Payload is already loaded."}), 409

    uav["payload_state"] = "LOADED"
    log_event("UAV payload reloaded")

    return jsonify({"message": "Payload reloaded. UAV available."})


@app.route("/api/responder", methods=["POST"])
def request_responder():
    """Record that a ground responder has been sent to a report.

    The dashboard cannot dispatch a person. This records an instruction that
    was passed on by radio or phone, which is why the state is called
    REQUESTED rather than DISPATCHED.
    """

    data = request.get_json(silent=True) or {}
    report = find_report(data.get("emergency_id"))

    if report is None:
        return jsonify({"message": "Emergency report not found."}), 404

    if report["response_status"] in CLOSED_STATES:
        return jsonify({"message": "Report is already closed."}), 409

    if report["responder_status"] != "NONE":
        return jsonify({
            "message": "A responder has already been requested for this report."
        }), 409

    state = _lifecycle[report["emergency_id"]]
    state["responder_status"] = "REQUESTED"
    state["responder_requested_at"] = datetime.now(PH_TZ).isoformat()

    state["history"].append({
        "at": state["responder_requested_at"],
        "state": "RESPONDER_REQUESTED",
        "by": "operator"
    })

    log_event(
        f"Ground responder requested for {report['emergency_id']}",
        report["zone"]
    )

    return jsonify({"message": "Ground responder recorded as requested."})


@app.route("/api/resolve", methods=["POST"])
def resolve():
    report = _report_from_request()

    if report is None:
        return jsonify({"message": "Emergency report not found."}), 404

    ok, error = apply_transition(report, "resolve")

    if not ok:
        return jsonify({"message": error}), 409

    log_event(f"{report['emergency_id']} resolved", report["zone"])

    note = ""

    # Closing a case must never strand the aircraft. What happens to the UAV
    # depends only on where it physically is.
    if uav["assigned_report"] == report["emergency_id"]:
        if uav["mission_state"] == "ASSIGNED":
            # Never left the ground, so it simply stands down.
            uav["mission_state"] = "STANDBY"
            clear_assignment()
            note = " UAV stood down at base."
        elif uav["mission_state"] in ("EN_ROUTE", "ON_STATION"):
            send_home()
            note = " UAV mission ended, returning to base."
            log_event("UAV returning to base, case resolved")
        else:
            clear_assignment()

    return jsonify({"message": f"Report resolved.{note}"})


@app.route("/api/reopen", methods=["POST"])
def reopen():
    """Undo a close that was made in error.

    Terminal states are the easiest thing to reach by mistake and, without
    this, the only way back was restarting the server. The reopened case
    returns to ACKNOWLEDGED, and the history keeps the mistake rather than
    hiding it, because an audit trail that quietly erases actions is worth
    less than one that shows them.
    """

    report = _report_from_request()

    if report is None:
        return jsonify({"message": "Emergency report not found."}), 404

    if report["response_status"] not in ("RESOLVED", "NO_ACTION"):
        return jsonify({"message": "That report is not closed."}), 409

    state = _lifecycle[report["emergency_id"]]
    state["response_status"] = "ACKNOWLEDGED"
    state["history"].append({
        "at": datetime.now(PH_TZ).isoformat(),
        "state": "ACKNOWLEDGED",
        "by": "operator (reopened)"
    })

    log_event(f"{report['emergency_id']} reopened", report["zone"])

    return jsonify({"message": f"{report['emergency_id']} reopened."})


@app.route("/api/uav/cancel", methods=["POST"])
def cancel_mission():
    """Abort a mission.

    An aircraft that has not left the ground can simply stand down. One that is
    already flying has to come back, so cancelling in the air puts it on the
    return leg rather than pretending it is instantly home.
    """

    if uav["mission_state"] == "STANDBY":
        return jsonify({"message": "No active UAV mission."}), 409

    if uav["mission_state"] == "RETURNING":
        return jsonify({"message": "UAV is already on its way back."}), 409

    report = find_report(uav["assigned_report"])

    if report is not None and report["response_status"] in (
        "ASSIGNED", "EN_ROUTE"
    ):
        state = _lifecycle[report["emergency_id"]]
        state["response_status"] = "ACKNOWLEDGED"
        state["history"].append({
            "at": datetime.now(PH_TZ).isoformat(),
            "state": "ACKNOWLEDGED",
            "by": "operator (mission cancelled)"
        })

    cancelled = uav["assigned_report"]

    if uav["mission_state"] == "ASSIGNED":
        log_event(f"UAV mission to {cancelled} cancelled before launch")
        uav["mission_state"] = "STANDBY"
        clear_assignment()

        return jsonify({
            "message": "Mission cancelled. Aircraft stood down at base."
        })

    uav["mission_state"] = "RETURNING"
    uav["assigned_report"] = None
    uav["target_latitude"] = uav["home_latitude"]
    uav["target_longitude"] = uav["home_longitude"]

    log_event(f"UAV mission to {cancelled} cancelled, returning to base")

    return jsonify({
        "message": "Mission cancelled. Aircraft is returning to base."
    })


if __name__ == "__main__":
    app.run(
        debug=True,
        host="127.0.0.1",
        port=5000
    )