const dashboardData = window.IAM_DASHBOARD_DATA || null;

const summaryGrid = document.getElementById("summary-grid");
const targetsContainer = document.getElementById("targets");
const notesList = document.getElementById("notes-list");
const titleNode = document.getElementById("dashboard-title");
const generatedAtNode = document.getElementById("generated-at");

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function statusClass(status) {
    switch (status) {
    case "healthy":
        return "is-healthy";
    case "warning":
        return "is-warning";
    default:
        return "is-down";
    }
}

function createSummaryCard(label, value, caption, tone = "is-neutral") {
    return `
        <article class="summary-card ${tone}">
            <p class="chip">${escapeHtml(label)}</p>
            <p class="summary-value">${escapeHtml(value)}</p>
            <p class="summary-caption">${escapeHtml(caption)}</p>
        </article>
    `;
}

function createMeter(percent) {
    const safePercent = Math.max(0, Math.min(Number(percent) || 0, 100));
    return `
        <div class="meter" aria-hidden="true">
            <span style="width: ${safePercent}%"></span>
        </div>
    `;
}

function renderSummary(data) {
    const { summary } = data;
    summaryGrid.innerHTML = [
        createSummaryCard("Targets", summary.totalTargets, `${summary.healthyTargets} healthy, ${summary.warningTargets} warning, ${summary.downTargets} down`, "is-neutral"),
        createSummaryCard("Applications", summary.totalApps, `${summary.healthyApps} healthy, ${summary.warningApps} warning, ${summary.downApps} down`, "is-healthy"),
        createSummaryCard("Last Refresh", data.generatedAtLocal, "Collector snapshot timestamp", "is-warning"),
        createSummaryCard("Monitoring Style", "SSH + HTTP", "Remote Linux metrics with internal app checks", "is-neutral")
    ].join("");
}

function renderNotes(data) {
    const notes = Array.isArray(data.notes) ? data.notes : [];
    notesList.innerHTML = notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("");
}

function createDetailRows(rows) {
    return rows.map((row) => `
        <div class="detail-row">
            <span class="detail-key">${escapeHtml(row.key)}</span>
            <span class="detail-value">${escapeHtml(row.value)}</span>
        </div>
    `).join("");
}

function createProcessList(processes) {
    if (!Array.isArray(processes) || processes.length === 0) {
        return `<p class="empty-state">No matching processes were captured for the current pattern set.</p>`;
    }

    const shorten = (value, max = 180) => {
        const text = String(value ?? "");
        return text.length > max ? `${text.slice(0, max - 1)}...` : text;
    };

    return `
        <ul class="stack-list">
            ${processes.map((process) => `<li><code title="${escapeHtml(process)}">${escapeHtml(shorten(process))}</code></li>`).join("")}
        </ul>
    `;
}

function createScriptsList(server) {
    if (!Array.isArray(server.scripts) || server.scripts.length === 0) {
        return `<p class="empty-state">No control scripts were discovered in the configured directory.</p>`;
    }

    return `
        <ul class="stack-list">
            ${server.scripts.map((script) => `<li><code>${escapeHtml(server.scriptDirectory)}\/${escapeHtml(script)}</code></li>`).join("")}
        </ul>
    `;
}

function createAppTable(appChecks) {
    if (!Array.isArray(appChecks) || appChecks.length === 0) {
        return `<p class="empty-state">No application endpoints are configured yet for this target.</p>`;
    }

    const rows = appChecks.map((check) => `
        <tr>
            <td>
                <strong>${escapeHtml(check.name)}</strong><br>
                <span class="table-label">${escapeHtml(check.url)}</span>
            </td>
            <td><span class="small-pill ${statusClass(check.status)}">${escapeHtml(check.status)}</span></td>
            <td>${escapeHtml(check.httpCode ?? "-")}</td>
            <td>${escapeHtml(check.responseTimeMs ? `${check.responseTimeMs} ms` : "-")}</td>
            <td>${escapeHtml(check.statusText)}</td>
        </tr>
    `).join("");

    return `
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Application</th>
                        <th>Status</th>
                        <th>HTTP</th>
                        <th>Latency</th>
                        <th>Message</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
}

function renderTarget(target) {
    const server = target.server || {};
    const memory = server.memory || {};
    const rootDisk = server.rootDisk || {};
    const refreshDisk = server.refreshDisk || null;
    const uptime = server.uptime || {};

    if (!server.reachable) {
        return `
            <article class="target-card">
                <div class="target-header">
                    <div>
                        <div class="target-title">
                            <h2>${escapeHtml(target.name)}</h2>
                            <span class="status-pill ${statusClass(target.status)}">${escapeHtml(target.status)}</span>
                        </div>
                        <p class="target-meta">${escapeHtml(target.role)} | ${escapeHtml(target.host)}</p>
                    </div>
                </div>
                <p class="empty-state">${escapeHtml(server.error || "Connection failed.")}</p>
            </article>
        `;
    }

    const detailRows = [
        { key: "Configured host", value: target.host },
        { key: "Reported hostname", value: server.actualHostname || "-" },
        { key: "Operating system", value: server.os || "-" },
        { key: "Kernel", value: server.kernel || "-" },
        { key: "Uptime summary", value: uptime.raw || "-" }
    ];

    if (refreshDisk) {
        detailRows.push({
            key: "/refresh usage",
            value: `${refreshDisk.used || "-"} / ${refreshDisk.size || "-"} (${refreshDisk.usedPercent || 0}%)`
        });
    }

    return `
        <article class="target-card">
            <div class="target-header">
                <div>
                    <div class="target-title">
                        <h2>${escapeHtml(target.name)}</h2>
                        <span class="status-pill ${statusClass(target.status)}">${escapeHtml(target.status)}</span>
                    </div>
                    <p class="target-meta">${escapeHtml(target.role)} | ${escapeHtml(target.host)}</p>
                </div>
            </div>

            <div class="metric-grid">
                <section class="metric-card">
                    <p class="metric-label">CPU Load Pressure</p>
                    <p class="metric-value">${escapeHtml(`${uptime.cpuPressure || 0}%`)}</p>
                    ${createMeter(uptime.cpuPressure)}
                </section>
                <section class="metric-card">
                    <p class="metric-label">Memory Used</p>
                    <p class="metric-value">${escapeHtml(`${memory.usedMb || 0} MB`)}</p>
                    ${createMeter(memory.usedPercent)}
                </section>
                <section class="metric-card">
                    <p class="metric-label">Root Disk Used</p>
                    <p class="metric-value">${escapeHtml(`${rootDisk.used || "-"} / ${rootDisk.size || "-"}`)}</p>
                    ${createMeter(rootDisk.usedPercent)}
                </section>
                <section class="metric-card">
                    <p class="metric-label">Load Average</p>
                    <p class="metric-value">${escapeHtml(`${uptime.load1 || 0} / ${uptime.load5 || 0} / ${uptime.load15 || 0}`)}</p>
                    <p class="target-meta">1m / 5m / 15m</p>
                </section>
            </div>

            <div class="detail-grid">
                <section class="detail-card">
                    <h3>Server Details</h3>
                    <div class="detail-list">${createDetailRows(detailRows)}</div>
                </section>
                <section class="detail-card">
                    <h3>Control Scripts</h3>
                    ${createScriptsList(server)}
                </section>
            </div>

            <div class="detail-grid">
                <section class="detail-card">
                    <h3>Matched Processes</h3>
                    ${createProcessList(server.processes)}
                </section>
                <section class="detail-card">
                    <h3>Application Reachability</h3>
                    ${createAppTable(target.appChecks)}
                </section>
            </div>
        </article>
    `;
}

function renderTargets(data) {
    const targets = Array.isArray(data.targets) ? data.targets : [];
    targetsContainer.innerHTML = targets.map(renderTarget).join("");
}

function renderEmptyState() {
    summaryGrid.innerHTML = createSummaryCard("Status", "No data", "Run the refresh script to generate the first snapshot.");
    notesList.innerHTML = "<li>No collector output has been generated yet.</li>";
    targetsContainer.innerHTML = `
        <article class="target-card">
            <h2>Waiting for the first refresh</h2>
            <p class="empty-state">Run <code>powershell -ExecutionPolicy Bypass -File .\\scripts\\Refresh-IamDashboard.ps1</code> and reopen this page.</p>
        </article>
    `;
}

function boot() {
    if (!dashboardData) {
        renderEmptyState();
        return;
    }

    titleNode.textContent = dashboardData.title || titleNode.textContent;
    generatedAtNode.textContent = dashboardData.generatedAtLocal || "Unknown";
    renderSummary(dashboardData);
    renderNotes(dashboardData);
    renderTargets(dashboardData);
}

boot();
