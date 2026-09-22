// Operations page: queue, map, UAV panel, dispatch confirmation.

let reports = [];
let selectedId = null;
let activeFilter = "OPEN";

// Closed reports stay on the map by default. An operator scanning a zone needs
// to see which addresses have already been dealt with, not just the ones still
// waiting. The toggle is there for when the map gets busy.
let showClosed = true;

let map;
let markers = {};
let accuracyCircles = [];
let uavMarker = null;
let targetMarker = null;
let targetLine = null;
let targetLineGlow = null;
let baseMarker = null;

// Latest aircraft state, kept so the case actions can describe what closing a
// case will do to a UAV that is committed to it.
let uavState = {};

let pendingDispatchId = null;


document.addEventListener("DOMContentLoaded", async () => {
    const config = await loadConfig();

    map = buildMap("map", config, "map-source");

    // Setting up the controls must never stop the data from loading. Showing
    // the operator the current cases is the job; the buttons are secondary to
    // it, so a failure here is reported and stepped over.
    try {
        bindControls();
    } catch (error) {
        console.error("SAGIP: control setup failed.", error);
        showNotification("Some controls did not load. Data is still live.");
    }

    await refreshAll();

    window.setInterval(refreshAll, 10000);

    // Relative timestamps are recomputed locally so the queue does not look
    // frozen between server refreshes.
    window.setInterval(renderQueue, 30000);
});


/*
   Bind a handler only if the element is actually on the page.

   The previous version called getElementById(...).addEventListener(...)
   directly. One control missing from the template threw a TypeError, which
   aborted the whole setup function, so refreshAll() never ran and the board
   came up with an empty queue and an empty map. A missing checkbox should
   cost you that checkbox, not every case on the screen.
*/
function on(elementId, eventName, handler) {
    const element = document.getElementById(elementId);

    if (!element) {
        console.warn(`SAGIP: control "${elementId}" is not on this page.`);
        return;
    }

    element.addEventListener(eventName, handler);
}


function bindControls() {
    document.querySelectorAll(".filter-button").forEach((button) => {
        button.addEventListener("click", () => {
            activeFilter = button.dataset.filter;

            document.querySelectorAll(".filter-button").forEach((item) => {
                item.classList.remove("active");
            });

            button.classList.add("active");
            renderQueue();
        });
    });

    on("export-button", "click", exportActivity);

    on("show-closed", "change", (event) => {
        showClosed = event.target.checked;
        renderMarkers();
    });

    on("confirm-cancel", "click", closeConfirm);
    on("confirm-accept", "click", acceptConfirm);

    on("confirm-modal", "click", (event) => {
        if (event.target.id === "confirm-modal") {
            closeConfirm();
        }
    });

    on("dispatch-cancel", "click", closeDispatchModal);
    on("dispatch-confirm", "click", confirmDispatch);

    on("dispatch-modal", "click", (event) => {
        if (event.target.id === "dispatch-modal") {
            closeDispatchModal();
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closeDispatchModal();
            closeConfirm();
        }
    });
}


/*
   A confirmation step for anything that closes a case.

   "Mark resolved" and "No action needed" sit next to the buttons an operator
   presses all the time, and both are terminal. A stray click on either used to
   close the case silently. The prompt states what is about to happen and what
   it will do to the aircraft, so the operator is refusing or accepting a
   described outcome rather than a verb.
*/
let pendingConfirm = null;


function askConfirm(title, subtitle, body, onAccept) {
    pendingConfirm = onAccept;

    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-subtitle").textContent = subtitle;
    document.getElementById("confirm-body").textContent = body;
    document.getElementById("confirm-modal").classList.remove("hidden");
    document.getElementById("confirm-accept").focus();
}


function closeConfirm() {
    document.getElementById("confirm-modal").classList.add("hidden");
    pendingConfirm = null;
}


function acceptConfirm() {
    const action = pendingConfirm;

    closeConfirm();

    if (action) {
        action();
    }
}


async function refreshAll() {
    try {
        const [reportData, uav, activity] = await Promise.all([
            getJSON("/api/reports"),
            getJSON("/api/uav"),
            getJSON("/api/activity")
        ]);

        reports = reportData.reports;

        renderSummary(reportData.summary);
        renderQueue();
        renderMarkers();
        renderUav(uav);
        renderActivity(activity);

        if (selectedId) {
            const selected = reports.find(
                (item) => item.emergency_id === selectedId
            );

            if (selected) {
                showReport(selected, false);
            }
        }

        document.getElementById("last-sync").textContent =
            `Last sync: ${new Date().toLocaleTimeString()}`;
    } catch (error) {
        document.getElementById("last-sync").textContent = "Sync failed";
        showNotification(`Unable to refresh: ${error.message}`);
    }
}


function renderSummary(summary) {
    document.getElementById("count-active").textContent = summary.active;
    document.getElementById("count-critical").textContent = summary.critical;
    document.getElementById("count-unlocated").textContent = summary.unlocated;
    document.getElementById("count-responded").textContent = summary.responded;
}


function visibleReports() {
    return reports.filter((report) => {
        if (activeFilter === "ALL") {
            return true;
        }

        if (activeFilter === "OPEN") {
            return report.is_open;
        }

        if (activeFilter === "CLOSED") {
            return !report.is_open;
        }

        if (activeFilter === "CRITICAL") {
            return report.is_open && report.reported_status === "CRITICAL";
        }

        if (activeFilter === "UNLOCATED") {
            return report.is_open && !report.is_located;
        }

        return true;
    });
}


function renderQueue() {
    const container = document.getElementById("report-list");
    container.replaceChildren();

    const visible = visibleReports();

    visible.forEach((report) => {
        const button = document.createElement("button");
        const classes = ["report-item", statusClass(report.reported_status)];

        if (selectedId === report.emergency_id) {
            classes.push("active");
        }

        if (!report.is_open) {
            classes.push("closed");
        }

        button.type = "button";
        button.className = classes.join(" ");

        const locationNote = report.is_located
            ? escapeHTML(report.zone || "Zone not available")
            : '<span class="flag-unlocated">No location</span>';

        // The dot on an unacknowledged critical row raises the alarm, matching
        // its marker on the map. The priority number is deliberately absent
        // here: the ordering already carries it, and a figure on every row
        // invites the reader to compare scores instead of reading the
        // situation.
        const dotClasses = ["status-dot", statusClass(report.reported_status)];

        if (
            report.reported_status === "CRITICAL" &&
            report.response_status === "RECEIVED"
        ) {
            dotClasses.push("alert");
        }

        const dot = `<span class="${dotClasses.join(" ")}"></span>`;

        button.innerHTML = `
            <span class="report-title">
                ${dot}

                <span class="report-main">
                    <strong>${escapeHTML(report.emergency_id)}</strong>
                    <small>${escapeHTML(assistanceText(report.requested_assistance))}</small>
                </span>

                <span class="state-chip state-${report.response_status.toLowerCase()}">
                    ${escapeHTML(STATE_LABEL[report.response_status])}
                </span>
            </span>

            <span class="report-meta">
                <span>${locationNote}</span>

                <span>
                    ${report.device_battery_pct}%
                    &middot;
                    ${escapeHTML(relativeTime(report.received_at))}
                </span>
            </span>
        `;

        button.addEventListener("click", () => showReport(report));
        container.appendChild(button);
    });

    if (visible.length === 0) {
        const message = document.createElement("p");

        message.className = "empty-note";
        message.textContent = "No reports under this filter.";

        container.appendChild(message);
    }

    const open = reports.filter((item) => item.is_open).length;

    document.getElementById("queue-note").textContent =
        `${open} open, ordered by urgency and waiting time`;
}


function renderMarkers() {
    Object.values(markers).forEach((marker) => marker.remove());
    markers = {};

    accuracyCircles.forEach((circle) => circle.remove());
    accuracyCircles = [];

    reports.forEach((report) => {
        // A report with no coordinate gets no pin. It stays in the queue and
        // is counted as unlocated, which is the honest representation of a
        // report received without a usable position.
        if (!report.is_located) {
            return;
        }

        if (!report.is_open && !showClosed) {
            return;
        }

        const critical =
            report.reported_status === "CRITICAL" && report.is_open;

        // Only a critical report nobody has looked at yet raises the alarm.
        // Acknowledging is the operator saying "I have seen this", so that is
        // the moment the animation stops. If every open critical kept flashing
        // the alarm would still be running on cases already being worked, and
        // an alarm that never stops is one operators learn to ignore.
        const unacknowledged =
            critical && report.response_status === "RECEIVED";

        /*
           A marker carries two independent facts, and each gets its own
           channel so neither overwrites the other.

           Colour is what was reported. That is a fact about the report and it
           never changes, so a critical case stays red after it is handled.

           Fill is whether it still needs attention. Solid means open, a ring
           means closed.

           The previous version turned closed cases green, which threw away the
           reported status and left green meaning two different things, "safe"
           and "resolved". That is what made the hover text look wrong: the
           marker said resolved while the tooltip said assistance needed.

           A safe report is the exception. It never entered the response
           lifecycle at all, so it is not a closed case; it is a check-in. It
           stays a small solid green dot.
        */
        const classes = ["map-marker", statusClass(report.reported_status)];

        if (report.reported_status !== "SAFE" && !report.is_open) {
            classes.push("done");
        }

        if (unacknowledged) {
            classes.push("alert");
        }

        const marker = L.marker([report.latitude, report.longitude], {
            icon: L.divIcon({
                className: "",
                html: `<div class="${classes.join(" ")}"></div>`,
                iconSize: [22, 22],
                iconAnchor: [11, 11]
            }),
            zIndexOffset: unacknowledged
                ? 1200
                : critical
                    ? 1000
                    : report.is_open ? 200 : 0
        });

        marker.addTo(map);

        // The uncertainty radius is drawn so an estimated position is not
        // presented as an exact point. It is drawn only while the report is
        // open, because the uncertainty matters when an operator is deciding
        // where to send an aircraft. Once the case is closed the ring is
        // clutter sitting on top of the reports that still need attention.
        if (report.is_open && report.position_accuracy_m) {
            const circle = L.circle([report.latitude, report.longitude], {
                radius: report.position_accuracy_m,
                color: critical ? "#e8392b" : "#7a919d",
                weight: 1,
                opacity: 0.45,
                fillOpacity: 0.06
            });

            circle.addTo(map);
            accuracyCircles.push(circle);
        }

        // The tooltip names both facts in the same order the marker draws
        // them: what was reported, then where the response has got to. Naming
        // only the reported status is what made a handled case read as if it
        // still needed attention.
        marker.bindTooltip(
            `<strong>${escapeHTML(report.emergency_id)}</strong>` +
            `<span class="tt-sep">&middot;</span>` +
            `${escapeHTML(STATUS_LABEL[report.reported_status])}` +
            `<span class="tt-sep">&middot;</span>` +
            `<span class="tt-state">` +
            `${escapeHTML(STATE_LABEL[report.response_status])}</span>`
        );

        marker.on("click", () => showReport(report, false));
        markers[report.emergency_id] = marker;
    });
}


function renderLifecycle(report) {
    const container = document.getElementById("selected-lifecycle");
    container.replaceChildren();

    if (report.response_status === "NO_ACTION") {
        const note = document.createElement("p");

        note.className = "lifecycle-note";
        note.textContent = "Closed with no action required.";

        container.appendChild(note);
        return;
    }

    const current = LIFECYCLE.indexOf(report.response_status);

    // Six labels do not fit side by side in this column without truncating to
    // "ACKNOW...", so the track is drawn as plain segments and only the step
    // the report is actually on is named underneath.
    const track = document.createElement("div");
    track.className = "lifecycle-track";

    LIFECYCLE.forEach((state, index) => {
        const segment = document.createElement("span");

        segment.className =
            index <= current ? "lifecycle-seg done" : "lifecycle-seg";
        segment.title = STATE_LABEL[state];

        track.appendChild(segment);
    });

    const caption = document.createElement("p");

    caption.className = "lifecycle-caption";
    caption.innerHTML =
        `<strong>${escapeHTML(STATE_LABEL[report.response_status])}</strong>` +
        `<span>step ${current + 1} of ${LIFECYCLE.length}</span>`;

    container.appendChild(track);
    container.appendChild(caption);
}


function actionButton(label, kind, handler, title) {
    const button = document.createElement("button");

    button.type = "button";
    button.className = kind;
    button.textContent = label;

    if (title) {
        button.title = title;
    }

    button.addEventListener("click", handler);

    return button;
}


function renderActions(report) {
    const container = document.getElementById("selected-actions");
    container.replaceChildren();

    const state = report.response_status;

    if (state === "RECEIVED") {
        container.appendChild(
            actionButton("Acknowledge", "primary-button", () =>
                runAction("/api/acknowledge", report.emergency_id)
            )
        );

        container.appendChild(
            actionButton("No action needed", "secondary-button", () =>
                askConfirm(
                    "Close with no action?",
                    `${report.emergency_id}, ${STATUS_LABEL[report.reported_status]}`,
                    "This closes the report without any response being sent. " +
                    "It can be reopened afterwards if this was a mistake.",
                    () => runAction("/api/no-action", report.emergency_id)
                )
            )
        );
    }

    // A case can need the UAV, a ground responder, or both, so both actions
    // are offered side by side rather than one being the only route forward.
    const route = report.routing || {};

    if (state === "ACKNOWLEDGED" && route.uav_eligible) {
        const dispatch = actionButton(
            "Dispatch UAV",
            "primary-button",
            () => openDispatchModal(report.emergency_id),
            report.blocked_reason || ""
        );

        dispatch.disabled = !report.can_dispatch;
        container.appendChild(dispatch);
    }

    if (
        route.responder_required &&
        report.responder_status === "NONE" &&
        report.response_status !== "RESOLVED" &&
        report.response_status !== "NO_ACTION"
    ) {
        container.appendChild(
            actionButton(
                "Record responder sent",
                route.uav_eligible ? "secondary-button" : "primary-button",
                () => runAction("/api/responder", report.emergency_id),
                "The dashboard cannot dispatch a person. This records that one "
                + "was sent."
            )
        );
    }

    if (state === "ASSIGNED" || state === "EN_ROUTE") {
        const note = document.createElement("p");

        note.className = "action-note";
        note.textContent =
            "Mission controls are in the UAV Operations panel below.";

        container.appendChild(note);
    }

    if (
        state === "ACKNOWLEDGED" ||
        state === "RESPONDED" ||
        state === "ASSIGNED"
    ) {
        // The prompt spells out what closing does to the aircraft, because
        // that consequence is not obvious from the button.
        const uavNote =
            uavState.assigned_report === report.emergency_id
                ? uavState.mission_state === "ASSIGNED"
                    ? " The UAV assigned to it will stand down at base."
                    : " The UAV assigned to it will return to base."
                : "";

        container.appendChild(
            actionButton(
                "Mark resolved",
                state === "RESPONDED" ? "primary-button" : "secondary-button",
                () =>
                    askConfirm(
                        "Mark this case resolved?",
                        `${report.emergency_id}, ${STATUS_LABEL[report.reported_status]}`,
                        "Resolving records that the situation was dealt with " +
                        "on the ground. It closes the case and removes it " +
                        "from the active queue." + uavNote +
                        " It can be reopened afterwards if this was a mistake.",
                        () => runAction("/api/resolve", report.emergency_id)
                    )
            )
        );
    }

    if (state === "RESOLVED" || state === "NO_ACTION") {
        container.appendChild(
            actionButton(
                "Reopen case",
                "secondary-button",
                () => runAction("/api/reopen", report.emergency_id),
                "Return this report to the active queue."
            )
        );
    }

    // Say why the UAV is not an option, so a disabled or absent button is
    // never just a dead end the operator has to guess at.
    const reason =
        state === "ACKNOWLEDGED"
            ? report.blocked_reason || route.uav_blocked_reason
            : null;

    if (reason) {
        const note = document.createElement("p");

        note.className = "action-note";
        note.textContent = reason;

        container.appendChild(note);
    }
}


function showReport(report, moveMap = true) {
    selectedId = report.emergency_id;

    document.getElementById("selected-case").classList.remove("hidden");
    document.getElementById("detail-empty").classList.add("hidden");
    document.getElementById("selected-id").textContent = report.emergency_id;

    document.getElementById("selected-heading").textContent =
        assistanceText(report.requested_assistance);

    const statusElement = document.getElementById("selected-status");

    statusElement.textContent = STATUS_LABEL[report.reported_status];
    statusElement.className = "tag " + statusClass(report.reported_status);

    document.getElementById("selected-device").textContent = report.device_id;
    document.getElementById("selected-zone").textContent =
        report.zone || "Not available";

    document.getElementById("selected-path").textContent =
        PATH_LABEL[report.communication_path] ||
        report.communication_path ||
        "Not available";

    document.getElementById("selected-position").textContent =
        positionText(report);

    document.getElementById("selected-battery").textContent =
        `${report.device_battery_pct}%`;

    const responderCell = document.getElementById("selected-responder");

    if (responderCell) {
        responderCell.textContent =
            RESPONDER_LABEL[report.responder_status] || report.responder_status;
    }

    // One line saying who this case calls for, so the operator is not
    // inferring the routing rule from which buttons happen to be enabled.
    const routingNote = document.getElementById("routing-note");
    const route = report.routing || {};

    if (routingNote) {
        const needs = [];

        if (route.uav_eligible) {
            needs.push(
                "UAV delivery (" +
                route.uav_deliverable
                    .map((code) => ASSISTANCE_LABEL[code] || code)
                    .join(", ") +
                ")"
            );
        }

        if (route.responder_required) {
            needs.push("ground responder");
        }

        routingNote.textContent = needs.length
            ? `This case calls for: ${needs.join(" and ")}.`
            : "No response resource is called for by this report.";

        routingNote.classList.toggle("hidden", report.reported_status === "SAFE");
    }

    document.getElementById("selected-received").textContent =
        `${relativeTime(report.received_at)} (${clockTime(report.received_at)})`;

    document.getElementById("selected-note").textContent =
        report.note || "No free-text note on this report.";

    document.getElementById("selected-record-link").href =
        `/records?id=${encodeURIComponent(report.emergency_id)}`;

    const warning = document.getElementById("location-warning");

    if (report.is_located) {
        warning.classList.add("hidden");
    } else {
        warning.classList.remove("hidden");
    }

    renderLifecycle(report);
    renderActions(report);
    renderQueue();

    if (moveMap && report.is_located) {
        map.flyTo([report.latitude, report.longitude], 17);
    }
}


async function runAction(url, emergencyId) {
    try {
        const result = await postJSON(url, { emergency_id: emergencyId });

        showNotification(result.message);
        await refreshAll();
    } catch (error) {
        showNotification(error.message);
    }
}


// ---------------------------------------------------------------------------
// Dispatch confirmation
// ---------------------------------------------------------------------------

async function openDispatchModal(emergencyId) {
    try {
        const preview = await getJSON(
            `/api/dispatch/preview/${encodeURIComponent(emergencyId)}`
        );

        if (!preview.can_dispatch) {
            showNotification(preview.blocked_reason);
            return;
        }

        pendingDispatchId = emergencyId;

        document.getElementById("dispatch-modal-subtitle").textContent =
            `${preview.emergency_id}, ${assistanceText(preview.requested_assistance)}`;

        const body = document.getElementById("dispatch-modal-body");
        body.replaceChildren();

        const warningBlock = document.createElement("div");

        if (preview.warnings.length) {
            warningBlock.className = "modal-warnings";

            warningBlock.innerHTML =
                '<span class="block-label">Before you confirm</span><ul>' +
                preview.warnings
                    .map((text) => `<li>${escapeHTML(text)}</li>`)
                    .join("") +
                "</ul>";

            body.appendChild(warningBlock);
        }

        const facts = document.createElement("dl");
        facts.className = "case-details";

        const rows = [
            ["Position source", positionText(preview)],
            [
                "Straight-line distance",
                formatDistance(preview.straight_line_distance_m)
            ],
            ["UAV battery", `${preview.uav_battery_pct}%`],
            ["Payload mass", `${preview.payload_mass_g} g`]
        ];

        facts.innerHTML = rows
            .map(
                ([term, value]) =>
                    `<div><dt>${escapeHTML(term)}</dt>` +
                    `<dd>${escapeHTML(value)}</dd></div>`
            )
            .join("");

        body.appendChild(facts);

        const payload = document.createElement("div");

        payload.innerHTML =
            '<span class="block-label">Payload carried</span><ul class="payload-list">' +
            preview.payload
                .map(
                    (item) =>
                        `<li>${escapeHTML(item.label)} ` +
                        `<em>${item.mass_g} g</em></li>`
                )
                .join("") +
            "</ul>";

        body.appendChild(payload);

        const caveat = document.createElement("p");

        caveat.className = "payload-note";
        caveat.textContent =
            "Distance is straight-line, not flight distance. The aircraft " +
            "travels further than this figure.";

        body.appendChild(caveat);

        document.getElementById("dispatch-modal").classList.remove("hidden");
        document.getElementById("dispatch-confirm").focus();
    } catch (error) {
        showNotification(error.message);
    }
}


function closeDispatchModal() {
    document.getElementById("dispatch-modal").classList.add("hidden");
    pendingDispatchId = null;
}


async function confirmDispatch() {
    const emergencyId = pendingDispatchId;

    closeDispatchModal();

    if (emergencyId) {
        await runAction("/api/dispatch", emergencyId);
    }
}


// ---------------------------------------------------------------------------
// UAV panel and map layers
// ---------------------------------------------------------------------------

function renderUav(uav) {
    uavState = uav;

    const state = document.getElementById("uav-state");

    state.textContent = UAV_LABEL[uav.mission_state] || uav.mission_state;
    state.className =
        "tag " + (uav.mission_state === "STANDBY" ? "neutral" : "active");

    document.getElementById("uav-battery").textContent = `${uav.battery_pct}%`;

    document.getElementById("uav-gnss").textContent =
        uav.gnss_fix === "3D_FIX" ? "3D fix" : uav.gnss_fix;

    document.getElementById("uav-link").textContent =
        uav.link === "CONNECTED" ? "Connected" : uav.link;

    document.getElementById("payload-mass").textContent =
        `${uav.payload_mass_g} g total`;

    const payloadList = document.getElementById("payload-list");
    payloadList.replaceChildren();

    if (uav.payload.length === 0) {
        payloadList.innerHTML = "<li>No payload loaded</li>";
    } else {
        payloadList.innerHTML = uav.payload
            .map(
                (item) =>
                    `<li>${escapeHTML(item.label)} ` +
                    `<em>${item.mass_g} g</em></li>`
            )
            .join("");
    }

    const assignment = document.getElementById("uav-assignment");

    if (uav.assigned_report) {
        assignment.textContent =
            `Assigned to ${uav.assigned_report}, ` +
            `${formatDistance(uav.target_distance_m)} straight-line`;
    } else {
        assignment.textContent = "No active assignment";
    }

    renderUavActions(uav);
    drawUavLayers(uav);
}


function renderUavActions(uav) {
    const container = document.getElementById("uav-actions");
    container.replaceChildren();

    if (uav.mission_state === "ASSIGNED") {
        container.appendChild(
            actionButton("Confirm launch", "primary-button small", async () => {
                try {
                    const result = await postJSON("/api/uav/launch");

                    showNotification(result.message);
                    await refreshAll();
                } catch (error) {
                    showNotification(error.message);
                }
            })
        );
    }

    if (uav.mission_state === "EN_ROUTE") {
        container.appendChild(
            actionButton("Record arrival", "primary-button small", () =>
                uavAction("/api/uav/arrived")
            )
        );
    }

    // The aircraft is at the coordinate with an empty bay. The only thing left
    // in the sortie is the flight home, which is a leg the operator has to
    // watch, not a state change that happens by itself.
    if (uav.mission_state === "ON_STATION") {
        container.appendChild(
            actionButton("Return to base", "primary-button small", () =>
                uavAction("/api/uav/return")
            )
        );
    }

    if (uav.mission_state === "RETURNING") {
        container.appendChild(
            actionButton("Confirm landed", "primary-button small", () =>
                uavAction("/api/uav/landed")
            )
        );
    }

    if (
        uav.mission_state === "STANDBY" &&
        uav.payload_state !== "LOADED"
    ) {
        container.appendChild(
            actionButton("Reload payload", "primary-button small", () =>
                uavAction("/api/uav/reload")
            )
        );
    }

    if (
        uav.mission_state !== "STANDBY" &&
        uav.mission_state !== "RETURNING"
    ) {
        container.appendChild(
            actionButton("Cancel mission", "text-button", () =>
                uavAction("/api/uav/cancel")
            )
        );
    }
}


async function uavAction(url) {
    try {
        const result = await postJSON(url);

        showNotification(result.message);
        await refreshAll();
    } catch (error) {
        showNotification(error.message);
    }
}


function drawUavLayers(uav) {
    [uavMarker, targetMarker, targetLine, targetLineGlow, baseMarker].forEach((layer) => {
        if (layer) {
            layer.remove();
        }
    });

    uavMarker = null;
    targetMarker = null;
    targetLine = null;
    targetLineGlow = null;
    baseMarker = null;

    // Base is always drawn, whether the aircraft is out or not. It is the
    // fixed point every sortie is measured against.
    if (uav.base) {
        baseMarker = L.marker([uav.base.latitude, uav.base.longitude], {
            icon: L.divIcon({
                className: "",
                html: '<div class="base-marker">BASE</div>',
                iconSize: [40, 20],
                iconAnchor: [20, 10]
            }),
            zIndexOffset: 1100
        }).addTo(map);

        baseMarker.bindTooltip(uav.base.name);
    }

    if (uav.current_latitude !== null) {
        const airborne = uav.is_airborne ? " airborne" : "";

        uavMarker = L.marker(
            [uav.current_latitude, uav.current_longitude],
            {
                icon: L.divIcon({
                    className: "",
                    html: `<div class="uav-marker${airborne}">UAV</div>`,
                    iconSize: [34, 20],
                    iconAnchor: [17, 10]
                }),
                zIndexOffset: 1200
            }
        ).addTo(map);

        uavMarker.bindTooltip(
            `UAV &middot; ${escapeHTML(UAV_LABEL[uav.mission_state] || uav.mission_state)}` +
            `<span class="tt-sep">&middot;</span>` +
            `<span class="tt-state">${formatDistance(uav.home_distance_m)} from base</span>`
        );
    }

    if (uav.target_latitude === null) {
        return;
    }

    const returning = uav.mission_state === "RETURNING";

    // The return leg is drawn in the settled colour, not the action colour, so
    // an outbound task and a flight home are never mistaken for each other at
    // a glance.
    const legColour = returning ? "#48c58e" : "#4f8cf7";

    if (!returning) {
        targetMarker = L.circleMarker(
            [uav.target_latitude, uav.target_longitude],
            { radius: 11, color: legColour, weight: 2.5, fillColor: legColour, fillOpacity: 0.08 }
        ).addTo(map);
    }

    // The line is the straight-line bearing from aircraft to destination, not
    // a flight path. It is dashed so it does not read as a planned route. A
    // wider, low-opacity line underneath gives it a soft glow without a CSS
    // filter, which Leaflet's SVG renderer handles more predictably.
    targetLineGlow = L.polyline(
        [
            [uav.current_latitude, uav.current_longitude],
            [uav.target_latitude, uav.target_longitude]
        ],
        { color: legColour, weight: 7, opacity: 0.14, interactive: false }
    ).addTo(map);

    targetLine = L.polyline(
        [
            [uav.current_latitude, uav.current_longitude],
            [uav.target_latitude, uav.target_longitude]
        ],
        { color: legColour, weight: 2.5, dashArray: "6 6", lineCap: "round", opacity: 0.95 }
    ).addTo(map);

    targetLine.bindTooltip(
        `${returning ? "Returning, " : ""}` +
        `${formatDistance(uav.target_distance_m)} straight-line`,
        {
            permanent: true,
            direction: "center",
            className: returning
                ? "distance-tooltip returning"
                : "distance-tooltip"
        }
    );
}


function renderActivity(activity) {
    const body = document.getElementById("activity-body");
    body.replaceChildren();

    if (activity.length === 0) {
        body.innerHTML = '<tr><td colspan="3">No recorded activity.</td></tr>';
        return;
    }

    body.innerHTML = activity
        .slice(0, 6)
        .map(
            (entry) =>
                "<tr>" +
                `<td>${escapeHTML(clockTime(entry.timestamp))}</td>` +
                `<td>${escapeHTML(entry.event)}</td>` +
                `<td>${escapeHTML(entry.zone)}</td>` +
                "</tr>"
        )
        .join("");
}


async function exportActivity() {
    try {
        const activity = await getJSON("/api/activity");

        downloadCSV("sagip_activity_log.csv", [
            ["Timestamp", "Event", "Zone"],
            ...activity.map((item) => [item.timestamp, item.event, item.zone])
        ]);
    } catch (error) {
        showNotification("Unable to export the activity log.");
    }
}