"""Priority scoring engine.

The score expresses reported urgency, device battery, how long the report has
waited, and how weak the signal is. It is a triage aid for the operator, not a
medical assessment.
"""

from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Status vocabulary
#
# The relay packet sends STATUS_CODE as the index into STATUS_NAMES to keep the
# LoRa payload small. The phone app firmware and relay firmware must use this
# same order.
# ---------------------------------------------------------------------------

STATUS_NAMES = [
    "CRITICAL SOS",
    "MEDICAL",
    "UNCONFIRMED",
    "NEED ASSISTANCE",
    "EVACUATING",
    "SAFE"
]

STATUS_POINTS = {
    "CRITICAL SOS": 100,
    "MEDICAL": 80,
    "UNCONFIRMED": 60,
    "NEED ASSISTANCE": 50,
    "EVACUATING": 20,
    "SAFE": 0
}

# Short forms accepted from the phone app and the existing dashboard.
STATUS_ALIASES = {
    "CRITICAL": "CRITICAL SOS",
    "SOS": "CRITICAL SOS",
    "ASSISTANCE": "NEED ASSISTANCE"
}


def normalise_status(value):
    """Return the canonical status name, or None if it is not recognised."""

    text = str(value).strip().upper().replace("_", " ")
    text = STATUS_ALIASES.get(text, text)

    return text if text in STATUS_POINTS else None


def status_from_code(code):
    """Turn a packet STATUS_CODE (index or name) into a status name."""

    code = str(code).strip()

    if code.isdigit():
        index = int(code)

        if index >= len(STATUS_NAMES):
            raise ValueError(f"Unknown status code {code}.")

        return STATUS_NAMES[index]

    status = normalise_status(code)

    if status is None:
        raise ValueError(f"Unknown status {code!r}.")

    return status


# ---------------------------------------------------------------------------
# Factors
# ---------------------------------------------------------------------------

def battery_points(battery):
    if battery is None:
        return 0
    if battery < 15:
        return 40
    if battery < 30:
        return 25
    if battery <= 50:
        return 15
    return 0


def signal_age_points(age_minutes):
    if age_minutes is None:
        return 0
    if age_minutes > 30:
        return 30
    if age_minutes >= 10:
        return 15
    return 0


def rssi_points(rssi):
    if rssi is None:
        return 0
    if rssi < -90:
        return 20
    if rssi <= -70:
        return 10
    return 0


def minutes_since(iso_time, now=None):
    if not iso_time:
        return None

    now = now or datetime.now(timezone.utc)
    then = datetime.fromisoformat(iso_time)

    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)

    # A clock that runs ahead must not produce a negative age.
    return max(0.0, (now - then).total_seconds() / 60)


def compute_priority(survivor, latest_rssi=None, now=None):
    """Score one survivor and explain where each point came from.

    Signal age is measured from last_seen, the time the backend last heard
    from this device, so a wrong clock on the phone cannot distort it.
    """

    status = normalise_status(survivor.get("status", ""))
    age = minutes_since(survivor.get("last_seen"), now)

    breakdown = [
        {
            "factor": "Status",
            "detail": status or "UNKNOWN",
            "points": STATUS_POINTS.get(status, 0)
        },
        {
            "factor": "Battery",
            "detail": survivor.get("battery"),
            "points": battery_points(survivor.get("battery"))
        },
        {
            "factor": "Signal age (min)",
            "detail": None if age is None else round(age, 1),
            "points": signal_age_points(age)
        },
        {
            "factor": "RSSI depth (dBm)",
            "detail": latest_rssi,
            "points": rssi_points(latest_rssi)
        }
    ]

    return {
        "score": sum(item["points"] for item in breakdown),
        "breakdown": breakdown
    }
