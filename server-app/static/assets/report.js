let reportPayload = null;

function escapeHtml(value) {
    return String(value == null ? "" : value)
        .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function label(value) {
    return String(value || "Details").replace(/([a-z0-9])([A-Z])/g, "$1 $2").replaceAll("_", " ")
        .replace(/\b\w/g, function(character) { return character.toUpperCase(); });
}

function isPrimitive(value) { return value == null || ["string", "number", "boolean"].includes(typeof value); }
function display(value) {
    if (value == null || value === "") return "-";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    return String(value);
}

function itemTitle(item, index) {
    if (!item || typeof item !== "object") return `Item ${index + 1}`;
    return item.name || item.label || item.id || item.server || item.host || item.key || `Item ${index + 1}`;
}

function scalarTable(entries) {
    if (!entries.length) return "";
    return `<div class="table-wrap"><table class="key-value"><tbody>${entries.map(function(entry) {
        return `<tr><th>${escapeHtml(label(entry[0]))}</th><td>${escapeHtml(display(entry[1]))}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

function renderValue(value, depth) {
    if (depth > 7) return `<pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
    if (isPrimitive(value)) return `<span>${escapeHtml(display(value))}</span>`;
    if (Array.isArray(value)) {
        if (!value.length) return `<p class="empty">No rows</p>`;
        if (value.every(isPrimitive)) return `<ul class="value-list">${value.map(function(item) { return `<li>${escapeHtml(display(item))}</li>`; }).join("")}</ul>`;
        const scalarObjects = value.every(function(item) {
            return item && typeof item === "object" && !Array.isArray(item) && Object.values(item).every(isPrimitive);
        });
        if (scalarObjects) {
            const columns = Array.from(new Set(value.flatMap(function(item) { return Object.keys(item); }))).slice(0, 12);
            return `<div class="table-wrap"><table><thead><tr>${columns.map(function(column) { return `<th>${escapeHtml(label(column))}</th>`; }).join("")}</tr></thead><tbody>${value.map(function(item) { return `<tr>${columns.map(function(column) { return `<td>${escapeHtml(display(item[column]))}</td>`; }).join("")}</tr>`; }).join("")}</tbody></table></div>`;
        }
        return value.map(function(item, index) {
            return `<details class="nested-block"><summary><span>${escapeHtml(itemTitle(item, index))}</span><span class="count">Item ${index + 1}</span></summary><div class="nested-content">${renderValue(item, depth + 1)}</div></details>`;
        }).join("");
    }
    const entries = Object.entries(value);
    if (!entries.length) return `<p class="empty">No details</p>`;
    const scalars = entries.filter(function(entry) { return isPrimitive(entry[1]); });
    const nested = entries.filter(function(entry) { return !isPrimitive(entry[1]); });
    return `${scalarTable(scalars)}${nested.map(function(entry) {
        const count = Array.isArray(entry[1]) ? `${entry[1].length} rows` : `${Object.keys(entry[1] || {}).length} fields`;
        return `<details class="nested-block"><summary><span>${escapeHtml(label(entry[0]))}</span><span class="count">${escapeHtml(count)}</span></summary><div class="nested-content">${renderValue(entry[1], depth + 1)}</div></details>`;
    }).join("")}`;
}

function summaryCard(name, value) { return `<article class="summary-card"><span>${escapeHtml(name)}</span><strong>${escapeHtml(display(value))}</strong></article>`; }

function renderReport(report) {
    reportPayload = report;
    const environment = report.environment || {};
    const server = report.server || {};
    const summary = report.summary || {};
    const collection = report.collection || {};
    const status = summary.status || report.status || (server.health || {}).status || "unknown";
    const products = Object.entries(environment.products || {}).filter(function(entry) { return entry[1]; }).map(function(entry) { return entry[0].toUpperCase(); }).join(", ");
    document.title = `${environment.name || environment.id || "Historical Report"} - FMW Report`;
    document.getElementById("report-hero").innerHTML = `<p class="eyebrow">Historical collector snapshot</p><h1>${escapeHtml(environment.name || environment.id || "Environment Report")}</h1><p>Collected ${escapeHtml(report.generatedAtLocal || report.generatedAt || "-")} · Read-only point-in-time evidence</p><span class="status ${escapeHtml(String(status).toLowerCase())}">${escapeHtml(status)}</span>`;
    document.getElementById("report-summary").innerHTML = [
        summaryCard("Environment", environment.name || environment.id), summaryCard("Host", server.host || environment.host),
        summaryCard("Products", products || "-"), summaryCard("Applications", summary.totalApps),
        summaryCard("Healthy", summary.healthyApps), summaryCard("Warnings", summary.warningApps),
        summaryCard("Down", summary.downApps), summaryCard("Duration", collection.durationMs == null ? "-" : `${collection.durationMs} ms`)
    ].join("");
    const preferredOrder = ["summary", "environment", "server", "systemMetrics", "appChecks", "applications", "productMetrics", "collection", "notifications"];
    const keys = Object.keys(report).filter(function(key) { return !["generatedAt", "generatedAtLocal", "generatedAtEpoch", "status"].includes(key); });
    keys.sort(function(a, b) { const ai = preferredOrder.indexOf(a), bi = preferredOrder.indexOf(b); return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi) || a.localeCompare(b); });
    document.getElementById("report-content").innerHTML = keys.map(function(key, index) {
        const value = report[key];
        const count = Array.isArray(value) ? `${value.length} rows` : (value && typeof value === "object" ? `${Object.keys(value).length} fields` : "");
        return `<details class="report-section" ${index < 2 ? "open" : ""}><summary><span>${escapeHtml(label(key))}</span><span class="count">${escapeHtml(count)}</span></summary><div class="section-body">${renderValue(value, 0)}</div></details>`;
    }).join("");
    document.getElementById("download-json").disabled = false;
}

function showError(message) {
    const target = document.getElementById("report-message");
    target.classList.remove("hidden");
    target.textContent = message;
}

async function loadReport() {
    const params = new URLSearchParams(location.search);
    const environment = params.get("environment");
    const report = params.get("report");
    if (!environment || !report) return showError("The report link is missing its environment or report identifier.");
    try {
        const response = await fetch(`/api/environments/${encodeURIComponent(environment)}/reports/${encodeURIComponent(report)}`, {cache: "no-store"});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `Report request failed with status ${response.status}.`);
        renderReport(payload);
    } catch (error) { showError(error.message); }
}

document.getElementById("print-report").addEventListener("click", function() { window.print(); });
document.getElementById("download-json").addEventListener("click", function() {
    if (!reportPayload) return;
    const params = new URLSearchParams(location.search);
    const name = `fmw-report-${params.get("environment") || "environment"}-${params.get("report") || "snapshot"}.json`;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([JSON.stringify(reportPayload, null, 2) + "\n"], {type: "application/json"}));
    link.download = name;
    link.click();
    URL.revokeObjectURL(link.href);
});

loadReport();
