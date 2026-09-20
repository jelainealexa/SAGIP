// Network page: relay health table and relay placement map.

const RELAY_STATE_CLASS = {
    ONLINE: "ok",
    DEGRADED: "warn",
    OFFLINE: "down"
};

let relayMap;
let relayLayers = [];


document.addEventListener("DOMContentLoaded", async () => {
    const config = await loadConfig();

    relayMap = buildMap("relay-map", config, null);

    await refreshRelays();
    window.setInterval(refreshRelays, 10000);
});


async function refreshRelays() {
    try {
        const data = await getJSON("/api/relays");

        renderSummary(data);
        renderTable(data.relays);
        renderRelayMap(data.relays);
    } catch (error) {
        showNotification(`Unable to load relay status: ${error.message}`);
    }
}


function renderSummary(data) {
    const relays = data.relays;

    document.getElementById("relay-online").textContent =
        relays.filter((r) => r.status === "ONLINE").length;

    document.getElementById("relay-degraded").textContent =
        relays.filter((r) => r.status === "DEGRADED").length;

    document.getElementById("relay-offline").textContent =
        relays.filter((r) => r.status === "OFFLINE").length;

    document.getElementById("relay-packets").textContent =
        relays.reduce((total, r) => total + r.packets_forwarded, 0);
}


function orDash(value, suffix) {
    if (value === null || value === undefined) {
        return "--";
    }

    return suffix ? `${value}${suffix}` : String(value);
}


function renderTable(relays) {
    const body = document.getElementById("relay-body");

    body.innerHTML = relays
        .map((relay) => {
            const charging = relay.solar_charging ? " (charging)" : "";

            return `
                <tr class="${relay.status === "OFFLINE" ? "row-offline" : ""}">
                    <td><strong>${escapeHTML(relay.label)}</strong></td>
                    <td>${escapeHTML(relay.site)}</td>

                    <td>
                        <span class="state-chip relay-${RELAY_STATE_CLASS[relay.status]}">
                            ${escapeHTML(relay.status.toLowerCase())}
                        </span>
                    </td>

                    <td>${escapeHTML(orDash(relay.battery_pct, "%") + charging)}</td>
                    <td class="mono">${escapeHTML(orDash(relay.uplink_rssi_dbm, " dBm"))}</td>
                    <td class="mono">${escapeHTML(orDash(relay.uplink_snr_db, " dB"))}</td>
                    <td>${escapeHTML(orDash(relay.hops_to_base))}</td>
                    <td>${relay.packets_forwarded}</td>
                    <td>${relay.duplicates_suppressed}</td>
                    <td>${escapeHTML(relativeTime(relay.last_heartbeat))}</td>
                </tr>
            `;
        })
        .join("");
}


function renderRelayMap(relays) {
    relayLayers.forEach((layer) => layer.remove());
    relayLayers = [];

    relays.forEach((relay) => {
        const marker = L.marker([relay.latitude, relay.longitude], {
            icon: L.divIcon({
                className: "",
                html:
                    `<div class="relay-marker relay-${RELAY_STATE_CLASS[relay.status]}">` +
                    `${escapeHTML(relay.relay_id)}</div>`,
                iconSize: [40, 20],
                iconAnchor: [20, 10]
            })
        });

        marker.addTo(relayMap);

        marker.bindTooltip(
            `${escapeHTML(relay.label)}: ${escapeHTML(relay.status.toLowerCase())}`
        );

        relayLayers.push(marker);
    });
}