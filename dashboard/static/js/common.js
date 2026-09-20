// Shared helpers, vocabulary labels, map construction, and the system health
// drawer. Loaded on every page before the page-specific script.

const OFFLINE_TILE_URL = "/static/tiles/{z}/{x}/{y}.png";
const ONLINE_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";

// A plain grey square drawn where a tile is missing from the offline pack.
// Without it the browser shows a broken-image icon on every uncovered tile,
// which reads as a failure rather than the edge of the surveyed area.
const MISSING_TILE =
    "data:image/svg+xml;charset=UTF-8," +
    encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">' +
        '<rect width="256" height="256" fill="#e4e9e4"/></svg>'
    );

const STATUS_LABEL = {
    CRITICAL: "Critical",
    ASSISTANCE: "Needs assistance",
    SAFE: "Safe"
};

const ASSISTANCE_LABEL = {
    TRAPPED: "Trapped",
    MEDICAL: "Medical",
    WATER: "Water",
    FOOD: "Food",
    EXTRACTION: "Extraction",
    SHELTER: "Shelter"
};

const PATH_LABEL = {
    CELLULAR: "Cellular",
    BLE_LORA_1HOP: "BLE to LoRa, 1 hop",
    BLE_LORA_2HOP: "BLE to LoRa, 2 hops",
    BLE_LORA_3HOP: "BLE to LoRa, 3 hops"
};

const POSITION_LABEL = {
    PHONE_GNSS: "Phone GNSS",
    RELAY_ESTIMATE: "Relay estimate",
    NONE: "Not available"
};

const STATE_LABEL = {
    RECEIVED: "Received",
    ACKNOWLEDGED: "Acknowledged",
    ASSIGNED: "Assigned",
    EN_ROUTE: "En route",
    RESPONDED: "Payload delivered",
    RESOLVED: "Resolved",
    NO_ACTION: "No action needed",
    RESPONDER_REQUESTED: "Ground responder requested"
};

// The ordered lifecycle, used to draw the progress track on a record.
const LIFECYCLE = [
    "RECEIVED",
    "ACKNOWLEDGED",
    "ASSIGNED",
    "EN_ROUTE",
    "RESPONDED",
    "RESOLVED"
];

const UAV_LABEL = {
    STANDBY: "Standby",
    ASSIGNED: "Assigned",
    EN_ROUTE: "En route",
    ON_STATION: "On station",
    RETURNING: "Returning"
};

const RESPONDER_LABEL = {
    NONE: "Not requested",
    REQUESTED: "Requested",
    ON_SCENE: "On scene"
};

const HEALTH_ROWS = [
    { key: "cellular_uplink", label: "Internet / cellular" },
    { key: "base_station", label: "Base station" },
    { key: "lora_network", label: "LoRa network" },
    { key: "ble_relays", label: "BLE relays" },
    { key: "offline_map", label: "Offline map" },
    { key: "uav_link", label: "UAV link" }
];

// Values that mean "this is fine" rather than "this needs attention". Anything
// not listed is treated as a problem, so a new state added on the server side
// shows as a warning rather than silently reading as healthy.
const GOOD_VALUES = [
    "UP", "OPERATIONAL", "AVAILABLE", "CONNECTED", "LOADED", "3D_FIX"
];


// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function escapeHTML(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


function statusClass(status) {
    if (status === "CRITICAL") {
        return "critical";
    }

    if (status === "ASSISTANCE") {
        return "assistance";
    }

    return "safe";
}


function assistanceText(codes) {
    if (!codes || codes.length === 0) {
        return "No specific request";
    }

    return codes.map((code) => ASSISTANCE_LABEL[code] || code).join(", ");
}


function relativeTime(isoTimestamp) {
    const then = new Date(isoTimestamp);
    const minutes = Math.floor((Date.now() - then.getTime()) / 60000);

    if (minutes < 1) {
        return "just now";
    }

    if (minutes === 1) {
        return "1 minute ago";
    }

    if (minutes < 60) {
        return `${minutes} minutes ago`;
    }

    const hours = Math.floor(minutes / 60);

    return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
}


function clockTime(isoTimestamp) {
    return new Date(isoTimestamp).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit"
    });
}


function formatDistance(metres) {
    if (metres === null || metres === undefined) {
        return "--";
    }

    if (metres < 1000) {
        return `${Math.round(metres)} m`;
    }

    return `${(metres / 1000).toFixed(2)} km`;
}


function positionText(report) {
    if (report.position_source === "NONE") {
        return POSITION_LABEL.NONE;
    }

    return (
        `${POSITION_LABEL[report.position_source]}, ` +
        `+/- ${report.position_accuracy_m} m`
    );
}


function showNotification(message) {
    const notification = document.getElementById("notification");

    notification.textContent = message;
    notification.classList.add("show");

    window.setTimeout(() => {
        notification.classList.remove("show");
    }, 3600);
}


async function getJSON(url) {
    const response = await fetch(url);

    if (!response.ok) {
        const body = await response.json().catch(() => ({}));

        throw new Error(body.message || "Request failed.");
    }

    return response.json();
}


async function postJSON(url, body) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {})
    });

    const result = await response.json().catch(() => ({}));

    if (!response.ok) {
        throw new Error(result.message || "Action failed.");
    }

    return result;
}


function downloadCSV(filename, rows) {
    const csv = rows
        .map((row) => row.map(csvValue).join(","))
        .join("\r\n");

    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");

    link.href = url;
    link.download = filename;
    link.click();

    URL.revokeObjectURL(url);
}


function csvValue(value) {
    const escaped = String(value === null || value === undefined ? "" : value)
        .replaceAll('"', '""');

    return `"${escaped}"`;
}


// ---------------------------------------------------------------------------
// Map construction
//
// The server reports whether an offline tile pack exists. If it does the map
// is built from local files and never touches the internet. If it does not,
// the map falls back to the public tile server and says so on screen, because
// a map quietly depending on internet access would undermine the whole point
// of the fallback demonstration.
// ---------------------------------------------------------------------------

let appConfig = null;


async function loadConfig() {
    if (appConfig) {
        return appConfig;
    }

    try {
        appConfig = await getJSON("/api/config");
    } catch (error) {
        appConfig = {
            map_center: [14.4579, 120.9848],
            default_zoom: 15,
            tiles: { available: false },
            area: null,
            poor_accuracy_m: 30
        };
    }

    return appConfig;
}


function buildMap(elementId, config, sourceLabelId) {
    const map = L.map(elementId, {
        zoomControl: true,

        // Wheel zoom is off by default. The map sits inside a scrolling page,
        // and a wheel over the map would otherwise zoom instead of scrolling,
        // which loses the operator's place in the queue. Ctrl or Cmd with the
        // wheel zooms, and the hint below appears when someone scrolls without
        // the modifier so the behaviour is discoverable rather than silent.
        scrollWheelZoom: false
    }).setView(config.map_center, config.default_zoom);

    bindWheelZoom(map);

    const pack = config.tiles;
    const label = sourceLabelId
        ? document.getElementById(sourceLabelId)
        : null;

    if (pack.available) {
        L.tileLayer(OFFLINE_TILE_URL, {
            minZoom: pack.min_zoom,

            // maxZoom is how far the map allows zooming. maxNativeZoom is the
            // deepest level saved on disk. Leaflet upscales the deepest saved
            // tiles to fill the levels beyond it, so the map stays usable
            // without downloading those extra levels.
            maxZoom: pack.max_zoom + 2,
            maxNativeZoom: pack.max_zoom,

            errorTileUrl: MISSING_TILE,
            attribution: "&copy; OpenStreetMap contributors (offline pack)"
        }).addTo(map);

        map.setMinZoom(pack.min_zoom);

        if (label) {
            label.textContent =
                `Offline tiles, zoom ${pack.min_zoom} to ${pack.max_zoom}`;
            label.className = "map-source offline";
        }
    } else {
        L.tileLayer(ONLINE_TILE_URL, {
            maxZoom: 19,
            errorTileUrl: MISSING_TILE,
            attribution: "&copy; OpenStreetMap contributors"
        }).addTo(map);

        if (label) {
            label.textContent = "Online tiles, internet required";
            label.className = "map-source online";
        }
    }

    drawPilotArea(map, config.area);

    return map;
}


function drawPilotArea(map, area) {
    if (!area || !area.boundary) {
        return;
    }

    // Drawn as an outline with almost no fill. A solid shaded blob would read
    // as "covered", which is a claim the prototype cannot support.
    //
    // No tooltip. A label that follows the pointer across the whole boundary
    // covers the markers underneath it, and the boundary is already explained
    // in the panel text. The polygon does not intercept pointer events either,
    // so clicking inside it reaches the markers rather than the shape.
    L.polygon(area.boundary, {
        color: "#7c8d96",
        weight: 1.5,
        dashArray: "6 5",
        fillOpacity: 0.03,
        interactive: false
    }).addTo(map);
}


// ---------------------------------------------------------------------------
// Wheel zoom with a modifier
// ---------------------------------------------------------------------------

function bindWheelZoom(map) {
    const container = map.getContainer();

    // Cmd, not Ctrl, is the zoom modifier on a Mac keyboard. The hint should
    // name the key that is actually under the operator's thumb.
    const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
    const modifierLabel = isMac ? "⌘ Cmd" : "Ctrl";

    const hint = document.createElement("div");
    hint.className = "map-hint";
    hint.innerHTML =
        '<svg class="map-hint-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" ' +
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<rect x="6" y="3" width="12" height="18" rx="6"></rect>' +
        '<line x1="12" y1="7" x2="12" y2="11"></line>' +
        '</svg>' +
        `<span><kbd class="map-hint-key">${modifierLabel}</kbd> + scroll to zoom</span>`;
    container.appendChild(hint);

    let hintTimer = null;

    container.addEventListener(
        "wheel",
        (event) => {
            if (event.ctrlKey || event.metaKey) {
                // The browser would otherwise zoom the whole page.
                event.preventDefault();

                const point = map.mouseEventToContainerPoint(event);
                const latlng = map.containerPointToLatLng(point);
                const step = event.deltaY > 0 ? -1 : 1;

                map.setZoomAround(latlng, map.getZoom() + step);
                return;
            }

            // Without the modifier the page scrolls as normal. Show the hint
            // so the operator knows zooming is still available.
            hint.classList.add("show");

            window.clearTimeout(hintTimer);
            hintTimer = window.setTimeout(() => {
                hint.classList.remove("show");
            }, 800);
        },
        { passive: false }
    );
}


// ---------------------------------------------------------------------------
// System health drawer
// ---------------------------------------------------------------------------

function healthValueText(key, value, health) {
    if (key === "ble_relays") {
        return value;
    }

    return String(value)
        .replace("_", " ")
        .toLowerCase()
        .replace(/^\w/, (c) => c.toUpperCase());
}


function healthIsGood(key, value, health) {
    if (key === "cellular_uplink") {
        // Cellular being down is the scenario SAGIP exists for. It is reported
        // as a state, not as a fault, so it is shown neutral rather than red.
        return null;
    }

    if (key === "ble_relays") {
        return health.relay_detail.reachable === health.relay_detail.total;
    }

    return GOOD_VALUES.includes(String(value));
}


function renderHealth(health) {
    const grid = document.getElementById("health-grid");

    if (!grid) {
        return;
    }

    grid.replaceChildren();

    HEALTH_ROWS.forEach((row) => {
        const value = health[row.key];
        const good = healthIsGood(row.key, value, health);

        const cell = document.createElement("div");
        cell.className = "health-cell";

        const state =
            good === null ? "neutral" : good ? "ok" : "warn";

        cell.innerHTML = `
            <span class="health-label">${escapeHTML(row.label)}</span>

            <span class="health-value ${state}">
                <span class="health-dot"></span>
                ${escapeHTML(healthValueText(row.key, value, health))}
            </span>
        `;

        grid.appendChild(cell);
    });

    document.getElementById("health-checked").textContent =
        `Last checked ${clockTime(health.checked_at)}`;

    const dot = document.getElementById("mode-dot");
    const label = document.getElementById("mode-label");
    const chip = document.getElementById("health-toggle");

    if (health.mode === "OFFLINE") {
        label.textContent = "Offline mode";
        chip.className = "mode-chip offline";
        dot.className = "mode-dot offline";
    } else {
        label.textContent = "Online";
        chip.className = "mode-chip online";
        dot.className = "mode-dot online";
    }
}


async function refreshHealth() {
    try {
        renderHealth(await getJSON("/api/system"));
    } catch (error) {
        const label = document.getElementById("mode-label");

        if (label) {
            label.textContent = "Base station unreachable";
            document.getElementById("health-toggle").className =
                "mode-chip warn";
        }
    }
}


function bindHealthDrawer() {
    const toggle = document.getElementById("health-toggle");
    const drawer = document.getElementById("health-drawer");

    if (!toggle || !drawer) {
        return;
    }

    toggle.addEventListener("click", () => {
        const open = !drawer.hidden;

        drawer.hidden = open;
        toggle.setAttribute("aria-expanded", String(!open));
    });
}


document.addEventListener("DOMContentLoaded", () => {
    bindHealthDrawer();
    refreshHealth();

    window.setInterval(refreshHealth, 10000);
});