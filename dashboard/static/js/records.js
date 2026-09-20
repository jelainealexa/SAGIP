// Records page: every report received, with its full response history.

let allRecords = [];
let selectedRecordId = null;


document.addEventListener("DOMContentLoaded", async () => {
    document
        .getElementById("records-search")
        .addEventListener("input", renderTable);

    document
        .getElementById("records-state")
        .addEventListener("change", renderTable);

    document
        .getElementById("export-records")
        .addEventListener("click", exportRecords);

    await refreshRecords();

    // Open the record named in the query string, so the operations page can
    // link straight to a specific record.
    const requested = new URLSearchParams(window.location.search).get("id");

    if (requested) {
        showDetail(requested);
    }

    window.setInterval(refreshRecords, 15000);
});


async function refreshRecords() {
    try {
        const data = await getJSON("/api/reports");

        allRecords = data.reports;
        renderTable();

        if (selectedRecordId) {
            showDetail(selectedRecordId);
        }
    } catch (error) {
        showNotification(`Unable to load records: ${error.message}`);
    }
}


function filteredRecords() {
    const term = document
        .getElementById("records-search")
        .value.trim()
        .toLowerCase();

    const state = document.getElementById("records-state").value;

    return allRecords.filter((record) => {
        if (state === "OPEN" && !record.is_open) {
            return false;
        }

        if (state !== "ALL" && state !== "OPEN") {
            if (record.response_status !== state) {
                return false;
            }
        }

        if (!term) {
            return true;
        }

        return (
            record.emergency_id.toLowerCase().includes(term) ||
            record.device_id.toLowerCase().includes(term)
        );
    });
}


function renderTable() {
    const body = document.getElementById("records-body");
    body.replaceChildren();

    const rows = filteredRecords();

    if (rows.length === 0) {
        body.innerHTML =
            '<tr><td colspan="8" class="empty-note">No matching records.</td></tr>';

        return;
    }

    rows.forEach((record) => {
        const row = document.createElement("tr");

        row.className =
            record.reported_status === "CRITICAL" && record.is_open
                ? "row-critical"
                : "";

        if (record.emergency_id === selectedRecordId) {
            row.classList.add("row-selected");
        }

        const position = record.is_located
            ? escapeHTML(positionText(record))
            : '<span class="flag-unlocated">No location</span>';

        row.innerHTML = `
            <td><strong>${escapeHTML(record.emergency_id)}</strong></td>
            <td class="mono">${escapeHTML(record.device_id)}</td>

            <td>
                <span class="tag ${statusClass(record.reported_status)}">
                    ${escapeHTML(STATUS_LABEL[record.reported_status])}
                </span>
            </td>

            <td>${escapeHTML(assistanceText(record.requested_assistance))}</td>
            <td>${position}</td>
            <td>${escapeHTML(PATH_LABEL[record.communication_path] || record.communication_path)}</td>

            <td>
                <span class="state-chip state-${record.response_status.toLowerCase()}">
                    ${escapeHTML(STATE_LABEL[record.response_status])}
                </span>
            </td>

            <td>${escapeHTML(clockTime(record.received_at))}</td>
        `;

        row.addEventListener("click", () => showDetail(record.emergency_id));
        body.appendChild(row);
    });
}


function showDetail(emergencyId) {
    const record = allRecords.find(
        (item) => item.emergency_id === emergencyId
    );

    if (!record) {
        return;
    }

    selectedRecordId = emergencyId;

    const panel = document.getElementById("record-detail");
    panel.classList.remove("hidden");

    document.getElementById("detail-id").textContent =
        `${record.emergency_id} | device ${record.device_id}`;

    document.getElementById("detail-heading").textContent =
        assistanceText(record.requested_assistance);

    const status = document.getElementById("detail-status");

    status.textContent = STATE_LABEL[record.response_status];
    status.className = `tag state-${record.response_status.toLowerCase()}`;

    const history = document.getElementById("detail-history");

    history.innerHTML = record.history
        .map(
            (entry) =>
                "<li>" +
                `<strong>${escapeHTML(STATE_LABEL[entry.state] || entry.state)}</strong>` +
                `<span>${escapeHTML(clockTime(entry.at))} &middot; ${escapeHTML(entry.by)}</span>` +
                "</li>"
        )
        .join("");

    const priority = document.getElementById("detail-priority");

    if (record.reported_status === "SAFE") {
        priority.innerHTML =
            '<p class="priority-total">Not ranked. Report marked safe.</p>';
    } else {
        priority.innerHTML =
            `<p class="priority-total">Score ${record.priority_score}` +
            (record.priority_rank
                ? `, currently rank ${record.priority_rank} in the open queue`
                : ", not in the open queue") +
            "</p>" +
            record.priority_breakdown
                .map(
                    (item) =>
                        `<p class="priority-row">${escapeHTML(item.factor)}: ` +
                        `${escapeHTML(item.detail)} (+${item.points})</p>`
                )
                .join("");
    }

    renderTable();
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}


function exportRecords() {
    const rows = [
        [
            "Emergency ID", "Device ID", "Reported status",
            "Requested assistance", "Latitude", "Longitude",
            "Position source", "Accuracy m", "Communication path",
            "Hop count", "Via relay", "Device battery %",
            "Zone", "Response state", "Received at", "Priority score"
        ],
        ...filteredRecords().map((record) => [
            record.emergency_id,
            record.device_id,
            record.reported_status,
            record.requested_assistance.join(" "),
            record.latitude,
            record.longitude,
            record.position_source,
            record.position_accuracy_m,
            record.communication_path,
            record.hop_count,
            record.via_relay,
            record.device_battery_pct,
            record.zone,
            record.response_status,
            record.received_at,
            record.priority_score
        ])
    ];

    downloadCSV("sagip_records.csv", rows);
}