"use strict";

const STORAGE_KEYS = {
  theme: "patchscope.appTheme.v2"
};

const DEFAULT_OPATCH_HEAP_OPTIONS = "-Xmx3072m";

const standardSteps = [
  { id: "connect", label: "Connect", title: "Connect to the customer server", hint: "Capture SSH details and the patch location." },
  { id: "discover", label: "Discover", title: "Discover Oracle homes", hint: "Find Oracle homes, domain homes, instance homes, OPatch, BSU, and running services." },
  { id: "readme", label: "README", title: "Read patch README", hint: "Extract prerequisites, backup scope, OPatch minimums, postinstall, and startup notes." },
  { id: "shutdown", label: "Shutdown", title: "Confirm shutdown", hint: "Confirm graceful shutdown, then verify no target-home processes remain." },
  { id: "backup", label: "Backup", title: "Take required backups", hint: "Back up ORACLE_HOME, DOMAIN_HOME, and OUD INSTANCE_HOME when required." },
  { id: "opatch", label: "OPatch", title: "Validate OPatch", hint: "Compare current OPatch with README minimum and upgrade when needed." },
  { id: "apply", label: "Apply", title: "Apply the patch", hint: "Run conflict checks, apply the selected patch, and stream progress." },
  { id: "postinstall", label: "Postinstall", title: "Complete postinstall", hint: "Run or confirm postinstall steps from the README." },
  { id: "restart", label: "Start", title: "Start services", hint: "Ask the user to start services with their runbook or scripts, then record startup confirmation." },
  { id: "report", label: "Report", title: "Generate final report", hint: "Summarize decisions, checks, commands, results, and rollback options." }
];

const spbSteps = [
  { id: "connect", label: "Connect", title: "Connect to the customer server", hint: "Capture SSH details and the SPB download location." },
  { id: "discover", label: "Discover", title: "Discover Oracle homes", hint: "Find target IDM homes, domains, OPatch, and currently running services." },
  { id: "readme", label: "README", title: "Read SPB README", hint: "Load SPB README and extract SPBAT prerequisites, phases, OPatch minimums, and post steps." },
  { id: "spbBackupShutdown", label: "Stop for Backup", title: "Stop services for backup", hint: "Verify selected-home services are down before creating ORACLE_HOME, DOMAIN_HOME, and INSTANCE_HOME tar backups." },
  { id: "backup", label: "Backup", title: "Take file and database backups", hint: "Confirm ORACLE_HOME, DOMAIN_HOME, INSTANCE_HOME, and database backups before cleanup or SPBAT phases." },
  { id: "spbInactive", label: "Inactive Patches", title: "Optional inactive patch review", hint: "Optionally review or remove inactive patches after backups, or skip this step with the decision recorded." },
  { id: "spbPrepare", label: "SPB Setup", title: "Prepare SPBAT run", hint: "Choose OAM/OIG/OUD/OID, create SPBAT log directory, and verify SPBAT bundle layout." },
  { id: "opatch", label: "OPatch", title: "Validate OPatch", hint: "SPB requires OPatch 13.9.4.2.17 or higher; use the OPatch bundle from the SPB download if needed." },
  { id: "spbUp", label: "Services Up", title: "Confirm services are up", hint: "For existing domains, verify services are up before PreStop. For fresh installs before domain creation, skip only the services-up check." },
  { id: "spbPrestop", label: "PreStop", title: "Run SPBAT PreStop", hint: "Run PreStop before Downtime. SPBAT requires this phase even when services are not present yet." },
  { id: "shutdown", label: "Shutdown", title: "Stop services after PreStop", hint: "Stop IDM/WebLogic services, then verify no selected-home processes remain." },
  { id: "spbDowntime", label: "Downtime", title: "Run SPBAT Downtime", hint: "Run SPBAT Downtime with services stopped and the same log directory used for PreStop." },
  { id: "postinstall", label: "Cleanup", title: "Pre-start cleanup", hint: "Clear selected WebLogic tmp/cache directory contents before services are restarted." },
  { id: "restart", label: "Start", title: "Start services", hint: "Start IDM/WebLogic services after SPBAT Downtime/Cleanup, then confirm before PostStart." },
  { id: "spbPoststart", label: "PostStart", title: "Run SPBAT PostStart", hint: "Run SPBAT PostStart and perform product-specific post-start actions such as OIG profile/script steps." },
  { id: "report", label: "Report", title: "Generate final report", hint: "Summarize SPBAT phases, log directory, backup, startup, and post-start actions." }
];

let discoveredHomes = [];

const state = {
  integrationContext: null,
  activeStep: 0,
  completed: new Set(),
  failed: new Set(),
  connected: false,
  connectionResult: null,
  discovered: false,
  discoveryError: "",
  selectedHomeId: "",
  selectedHomePath: "",
  readmeText: "",
  readmePath: "",
  readmeAnalysis: null,
  servicesUpConfirmed: false,
  servicesUpVerified: false,
  servicesUpManualAccepted: false,
  servicesUpCheck: null,
  servicesUpStatus: "idle",
  servicesUpError: "",
  spbFreshInstallNoDomain: false,
  spbPrepared: false,
  spbPrepareResult: null,
  spbPrepareStatus: "idle",
  spbPrepareError: "",
  spbInactiveCheck: null,
  spbInactiveResult: null,
  spbInactiveRetainLevel: "1",
  spbInactiveRemoveConfirmed: false,
  spbInactiveSkipConfirmed: false,
  spbInactiveReviewed: false,
  spbInactiveError: "",
  spbInactiveJobId: "",
  spbInactiveJobOutput: "",
  spbInactiveJobStatus: "idle",
  spbInactiveJobKind: "",
  spbInactiveStopRequested: false,
  spbInactiveSkipAfterCancel: false,
  spbInactiveJobStartedAt: null,
  spbInactiveJobFinishedAt: null,
  spbPrestopDone: false,
  spbPrestopExternalApproved: false,
  spbPrestopExternalNote: "",
  spbDowntimeDone: false,
  spbPoststartDone: false,
  spbPhaseStatus: { prestop: "idle", downtime: "idle", poststart: "idle" },
  spbPhaseLogs: { prestop: "", downtime: "", poststart: "" },
  spbPhaseErrors: { prestop: "", downtime: "", poststart: "" },
  spbPhaseOutput: { prestop: "", downtime: "", poststart: "" },
  spbPhaseJobs: { prestop: null, downtime: null, poststart: null },
  spbCleanupApproved: false,
  spbCleanupTargetsText: "",
  spbCleanupResult: null,
  spbCleanupAttempts: [],
  spbCleanupOutput: "",
  spbCleanupError: "",
  spbCleanupStatus: "idle",
  oigProfileReady: false,
  oigProfile: null,
  oigProfileEdits: {},
  oigProfileSaved: false,
  oigPasswordsConfirmed: false,
  oigManualAccepted: false,
  oigScriptStatus: "idle",
  oigScriptDone: false,
  oigScriptLogPath: "",
  oigScriptOutput: "",
  oigScriptResult: null,
  oigScriptError: "",
  customerStopped: false,
  shutdownVerified: false,
  shutdownOverrideApproved: false,
  shutdownCheck: null,
  shutdownSelections: new Set(),
  backupShutdownCustomerStopped: false,
  backupShutdownVerified: false,
  backupShutdownOverrideApproved: false,
  backupShutdownCheck: null,
  backupShutdownSelections: new Set(),
  backupsDone: false,
  backupDirOverride: "",
  backupDestinationDirty: false,
  backupPreflight: null,
  backupPreflightConfirmed: false,
  backupExternalApproved: false,
  backupExternalNote: "",
  databaseBackupConfirmed: false,
  databaseBackupNote: "",
  backupResult: null,
  backupOutput: "",
  opatchReady: false,
  opatchUpgradeJobId: "",
  opatchUpgradeOutput: "",
  patchApplied: false,
  patchResult: null,
  patchApplyOutput: "",
  patchProgressPhase: "idle",
  patchStartedAt: 0,
  opatchDebugUsed: false,
  postinstallConfirmed: false,
  postinstallDone: false,
  startupConfirmed: false,
  startupNote: "",
  restartDone: false,
  rollbackInProgress: false,
  rollbackDone: false,
  rollbackResult: null,
  rollbackOutput: "",
  running: false,
  progress: 0,
  report: null,
  form: {
    host: "",
    port: "22",
    user: "oracle",
    runAsUser: "oracle",
    password: "",
    sudoAuth: "ssh-password",
    sudoPassword: "",
    patchType: "Stack Patch Bundle",
    applyMode: "single",
    batchSelection: "all",
    batchPatchIds: "",
    patchListFile: "",
    patchPath: "",
    opatchBundlePath: "",
    opatchHeapOptions: DEFAULT_OPATCH_HEAP_OPTIONS,
    spbInstallType: "auto",
    spbExtraArgs: "",
    spbLogDir: "",
    customer: "",
    changeRef: ""
  }
};

const dom = {
  appThemeSelect: document.getElementById("appThemeSelect"),
  resetRunButton: document.getElementById("resetRunButton"),
  downloadReportButton: document.getElementById("downloadReportButton"),
  wizardLayout: document.getElementById("wizardLayout"),
  monitorPanel: document.getElementById("monitorPanel"),
  runStatusPill: document.getElementById("runStatusPill"),
  stepTitle: document.getElementById("stepTitle"),
  stepHint: document.getElementById("stepHint"),
  wizardSteps: document.getElementById("wizardSteps"),
  stepContent: document.getElementById("stepContent"),
  backButton: document.getElementById("backButton"),
  nextButton: document.getElementById("nextButton"),
  secondaryActionButton: document.getElementById("secondaryActionButton"),
  dryRunToggle: document.getElementById("dryRunToggle"),
  opatchDebugToggle: document.getElementById("opatchDebugToggle"),
  autoRollbackToggle: document.getElementById("autoRollbackToggle"),
  simulateConflictToggle: document.getElementById("simulateConflictToggle"),
  connectionStatus: document.getElementById("connectionStatus"),
  monitorState: document.getElementById("monitorState"),
  rollbackButton: document.getElementById("rollbackButton"),
  clearLogButton: document.getElementById("clearLogButton"),
  activityLog: document.getElementById("activityLog")
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function nowStamp() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isSpbPatchTypeLabel(value) {
  return /stack\s+patch\s+bundle|\bspb\b|\bcspu\b/i.test(value || "");
}

function textIndicatesSpbat(value) {
  const text = String(value || "");
  return /\bcspu\b|critical\s+stack\s+patch\s+update|stack\s+patch\s+bundle|stack\s+patch\s+bundle\s+application\s+tool|\bspbat\b|spbat\.sh|spb_download_dir|-phase\s+(?:prestop|downtime|poststart)|\bprestop\b[\s\S]{0,80}\bdowntime\b|\bdowntime\b[\s\S]{0,80}\bpoststart\b/i.test(text);
}

function pathIndicatesSpbat(value) {
  return /(?:^|[\/\\])tools[\/\\]spbat(?:[\/\\]|$)|\bcspu\b|\bidm[_-]?spb\b|\bspb[_-]?\d|\bstack[_-]?patch/i.test(value || "");
}

function textIndicatesOpatchApply(value) {
  const text = String(value || "");
  return /opatch\s+(?:prereq\s+checkconflictagainstohwithdetail|apply|rollback)|checkconflictagainstohwithdetail|oracle\s+interim\s+patch|one[-\s]?off/i.test(text);
}

function detectedReadmePatchMethod(text = state.readmeText) {
  if (!String(text || "").trim()) return "";
  if (textIndicatesSpbat(text)) return "spbat";
  if (textIndicatesOpatchApply(text)) return "opatch";
  return "";
}

function isStackPatchBundle() {
  const readmeMethod = detectedReadmePatchMethod();
  if (readmeMethod === "spbat") return true;
  if (readmeMethod === "opatch" && !pathIndicatesSpbat(state.form.patchPath)) return false;
  return isSpbPatchTypeLabel(state.form.patchType) || pathIndicatesSpbat(state.form.patchPath);
}

function isBatchNapply() {
  return !isStackPatchBundle() && state.form.patchType === "One-off" && state.form.applyMode === "batch";
}

function patchApplyCommandLabel() {
  return isBatchNapply() ? "OPatch napply" : "OPatch apply";
}

function batchNapplyCommand(home = selectedHome(), debugArgs = "") {
  const opatch = `${home.oracleHome}/OPatch/opatch`;
  if (state.form.batchSelection === "file") {
    return `${opatch} napply -oh ${home.oracleHome} -phBaseFile ${state.form.patchListFile} -silent${debugArgs}`;
  }
  if (state.form.batchSelection === "ids") {
    const ids = String(state.form.batchPatchIds || "").trim().replace(/\s+/g, ",");
    return `${opatch} napply ${state.form.patchPath} -id ${ids} -oh ${home.oracleHome} -silent${debugArgs}`;
  }
  return `${opatch} napply ${state.form.patchPath} -oh ${home.oracleHome} -silent${debugArgs}`;
}

function workflowSteps() {
  return isStackPatchBundle() ? spbSteps : standardSteps;
}

function currentStep() {
  const list = workflowSteps();
  if (state.activeStep >= list.length) state.activeStep = list.length - 1;
  return list[state.activeStep] || list[0];
}

function emptySelectedHome() {
  return {
    id: "",
    label: "No Oracle home selected",
    host: state.form.host,
    product: "",
    oracleHome: "",
    discoveredHome: "",
    canonicalHome: "",
    domainHome: "",
    instanceHome: "",
    opatchVersion: "not checked",
    services: []
  };
}

function homeMatchesPath(home, path) {
  const key = homePathKey(path);
  if (!key) return false;
  return [home.oracleHome, home.canonicalHome, home.discoveredHome].some((candidate) => homePathKey(candidate) === key);
}

function rememberSelectedHome(home) {
  const target = home || {};
  state.selectedHomePath = homePathKey(target.oracleHome || target.canonicalHome || target.discoveredHome || "");
}

function selectedHome() {
  const byId = discoveredHomes.find((home) => home.id === state.selectedHomeId);
  if (byId) {
    rememberSelectedHome(byId);
    return byId;
  }
  const byPath = discoveredHomes.find((home) => homeMatchesPath(home, state.selectedHomePath));
  if (byPath) {
    state.selectedHomeId = byPath.id;
    rememberSelectedHome(byPath);
    return byPath;
  }
  if (discoveredHomes[0]) {
    state.selectedHomeId = discoveredHomes[0].id;
    rememberSelectedHome(discoveredHomes[0]);
    return discoveredHomes[0];
  }
  return emptySelectedHome();
}

function reportSelectedHome() {
  const home = selectedHome();
  if (!isStackPatchBundle()) return { ...home };
  return {
    ...home,
    detectedProduct: home.product || home.label || "",
    product: spbInstallType().toUpperCase()
  };
}

function homePathKey(path) {
  return String(path || "")
    .replace(/\\/g, "/")
    .replace(/\/+$/g, "")
    .trim();
}

function normalizeDiscoveredHome(rawHome) {
  const sourceHome = homePathKey(rawHome && rawHome.oracleHome);
  const canonicalHome = homePathKey(rawHome && rawHome.canonicalHome);
  const oracleHome = canonicalHome || sourceHome;
  return {
    ...(rawHome || {}),
    oracleHome,
    canonicalHome,
    discoveredHome: sourceHome && sourceHome !== oracleHome
      ? sourceHome
      : homePathKey(rawHome && rawHome.discoveredHome)
  };
}

function mergeCsvValues(left, right) {
  const values = [];
  const seen = new Set();
  for (const value of `${left || ""},${right || ""}`.split(",")) {
    const clean = value.trim();
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    values.push(clean);
  }
  return values.join(", ");
}

function dedupeDiscoveredHomes(homes) {
  const byPath = new Map();
  for (const rawHome of Array.isArray(homes) ? homes : []) {
    const normalizedHome = normalizeDiscoveredHome(rawHome);
    const key = homePathKey(normalizedHome.oracleHome || normalizedHome.canonicalHome);
    if (!key) continue;
    const existing = byPath.get(key);
    if (!existing) {
      byPath.set(key, {
        ...normalizedHome,
        services: Array.isArray(normalizedHome.services) ? [...normalizedHome.services] : []
      });
      continue;
    }
    existing.product = mergeCsvValues(existing.product, normalizedHome.product);
    existing.domainHome = existing.domainHome || normalizedHome.domainHome || "";
    existing.instanceHome = existing.instanceHome || normalizedHome.instanceHome || "";
    existing.discoveredHome = existing.discoveredHome || normalizedHome.discoveredHome || "";
    existing.opatchVersion = /^(not found|unavailable|not checked)/i.test(existing.opatchVersion || "")
      ? (normalizedHome.opatchVersion || existing.opatchVersion)
      : existing.opatchVersion;
    existing.services = Array.from(new Set([
      ...(Array.isArray(existing.services) ? existing.services : []),
      ...(Array.isArray(normalizedHome.services) ? normalizedHome.services : [])
    ]));
  }
  return Array.from(byPath.values()).map((home, index) => ({
    ...home,
    id: `home-${index + 1}`
  }));
}

function log(message, level = "info") {
  const row = document.createElement("div");
  row.className = level === "warn" ? "log-warn" : level === "error" ? "log-error" : level === "pass" ? "log-pass" : "";
  row.textContent = `[${nowStamp()}] ${message}`;
  dom.activityLog.appendChild(row);
  dom.activityLog.scrollTop = dom.activityLog.scrollHeight;
}

function logCommandTail(text, label = "OPatch log") {
  if (!text) return;
  const lines = text
    .replace(/\r/g, "\n")
    .split("\n")
    .map((line) => line.trimEnd())
    .filter(Boolean);
  for (const line of lines) {
    if (line.includes("__PATCHSCOPE_JSON_START__") || line.includes("__PATCHSCOPE_JSON_END__")) continue;
    if (/^\{.*"(oracleHome|phase|status)"/.test(line)) continue;
    const clean = line.replace(/\x1b\[[0-9;]*m/g, "");
    log(`${label}: ${clean.length > 500 ? `${clean.slice(0, 500)}...` : clean}`);
  }
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) {
    throw new Error(data.error || `Request failed with HTTP ${response.status}`);
  }
  return data;
}

function connectionPayload() {
  return {
    host: state.form.host,
    port: state.form.port,
    user: state.form.user,
    runAsUser: state.form.runAsUser,
    password: state.form.password,
    sudoAuth: state.form.sudoAuth,
    sudoPassword: state.form.sudoAuth === "separate" ? state.form.sudoPassword : "",
    patchPath: state.form.patchPath
  };
}

function setStatus(text, tone = "neutral") {
  if (!dom.runStatusPill) return;
  dom.runStatusPill.textContent = text;
  dom.runStatusPill.classList.toggle("is-good", tone === "good");
  dom.runStatusPill.classList.toggle("is-danger", tone === "danger");
}

function setBusy(active, text = "Running") {
  state.running = active;
  dom.monitorState.textContent = active ? text : "Idle";
  renderControls();
}

function shutdownGateMeta(stepId = currentStep().id) {
  const backupGate = stepId === "spbBackupShutdown";
  return {
    stepId: backupGate ? "spbBackupShutdown" : "shutdown",
    customerStoppedKey: backupGate ? "backupShutdownCustomerStopped" : "customerStopped",
    verifiedKey: backupGate ? "backupShutdownVerified" : "shutdownVerified",
    overrideKey: backupGate ? "backupShutdownOverrideApproved" : "shutdownOverrideApproved",
    checkKey: backupGate ? "backupShutdownCheck" : "shutdownCheck",
    selectionsKey: backupGate ? "backupShutdownSelections" : "shutdownSelections",
    breadcrumb: backupGate ? "Backup Safety" : "Shutdown Gate",
    heading: backupGate ? "Stop Services for Backup" : "Graceful Shutdown",
    confirmText: backupGate
      ? "Confirm that selected-home services are stopped before PatchPilot creates tar backups."
      : "Confirm that the selected-home servers were gracefully stopped",
    verifiedTitle: backupGate ? "Backup shutdown verified" : "Shutdown verified",
    overrideTitle: backupGate ? "Backup shutdown manually accepted" : "Shutdown manually accepted",
    checkTitle: backupGate ? "Services still running before backup" : "Services still running",
    verifiedStatus: backupGate ? "Services down for backup" : "Shutdown verified",
    verifyLog: backupGate
      ? "No active process was found. File-system tar backups can be created from a stopped home."
      : "No active process was found for the selected ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME.",
    runningLog: backupGate
      ? "Backup shutdown verification found {count} service group(s) still running. Stop them before creating tar backups."
      : "Shutdown verification found {count} service group(s) still running for the selected home.",
    overrideLog: backupGate
      ? "Backup shutdown override accepted. Remaining processes were marked handled manually or safe to ignore before backup."
      : "Shutdown override accepted. Remaining processes were marked handled manually or safe to ignore for this run.",
    overrideStatus: backupGate ? "Backup shutdown accepted" : "Shutdown accepted with override"
  };
}

function shutdownGateSatisfied(stepId = "shutdown") {
  const meta = shutdownGateMeta(stepId);
  return Boolean(state[meta.verifiedKey] || state[meta.overrideKey]);
}

function resetShutdownGate(stepId = "shutdown") {
  const meta = shutdownGateMeta(stepId);
  state[meta.customerStoppedKey] = false;
  state[meta.verifiedKey] = false;
  state[meta.overrideKey] = false;
  state[meta.checkKey] = null;
  state[meta.selectionsKey].clear();
  state.completed.delete(meta.stepId);
  state.failed.delete(meta.stepId);
}

function resetAllShutdownGates() {
  resetShutdownGate("shutdown");
  resetShutdownGate("spbBackupShutdown");
}

function resetOigPostinstallState() {
  state.oigProfileReady = false;
  state.oigProfile = null;
  state.oigProfileEdits = {};
  state.oigProfileSaved = false;
  state.oigPasswordsConfirmed = false;
  state.oigManualAccepted = false;
  state.oigScriptStatus = "idle";
  state.oigScriptDone = false;
  state.oigScriptLogPath = "";
  state.oigScriptOutput = "";
  state.oigScriptResult = null;
  state.oigScriptError = "";
}

function resetSpbInactiveState() {
  state.spbInactiveCheck = null;
  state.spbInactiveResult = null;
  state.spbInactiveRetainLevel = "1";
  state.spbInactiveRemoveConfirmed = false;
  state.spbInactiveSkipConfirmed = false;
  state.spbInactiveReviewed = false;
  state.spbInactiveError = "";
  state.spbInactiveJobId = "";
  state.spbInactiveJobOutput = "";
  state.spbInactiveJobStatus = "idle";
  state.spbInactiveJobKind = "";
  state.spbInactiveStopRequested = false;
  state.spbInactiveSkipAfterCancel = false;
  state.spbInactiveJobStartedAt = null;
  state.spbInactiveJobFinishedAt = null;
  state.completed.delete("spbInactive");
  state.failed.delete("spbInactive");
}

function resetSpbPhaseState() {
  state.servicesUpConfirmed = false;
  state.servicesUpVerified = false;
  state.servicesUpManualAccepted = false;
  state.servicesUpCheck = null;
  state.servicesUpStatus = "idle";
  state.servicesUpError = "";
  state.spbFreshInstallNoDomain = false;
  resetShutdownGate("spbBackupShutdown");
  state.spbPrepared = false;
  state.spbPrepareResult = null;
  state.spbPrepareStatus = "idle";
  state.spbPrepareError = "";
  state.failed.delete("spbPrepare");
  state.spbPrestopDone = false;
  state.spbPrestopExternalApproved = false;
  state.spbPrestopExternalNote = "";
  state.spbDowntimeDone = false;
  state.spbPoststartDone = false;
  state.spbPhaseStatus = { prestop: "idle", downtime: "idle", poststart: "idle" };
  state.spbPhaseLogs = { prestop: "", downtime: "", poststart: "" };
  state.spbPhaseErrors = { prestop: "", downtime: "", poststart: "" };
  state.spbPhaseOutput = { prestop: "", downtime: "", poststart: "" };
  state.spbPhaseJobs = { prestop: null, downtime: null, poststart: null };
  resetSpbCleanupState();
  resetSpbInactiveState();
  resetOigPostinstallState();
  ["spbBackupShutdown", "spbUp", "spbPrepare", "spbPrestop", "spbDowntime", "spbPoststart"].forEach((step) => state.completed.delete(step));
}

function versionParts(version) {
  return String(version || "0").split(".").map((part) => Number.parseInt(part, 10) || 0);
}

function compareVersions(left, right) {
  const a = versionParts(left);
  const b = versionParts(right);
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    const delta = (a[index] || 0) - (b[index] || 0);
    if (delta !== 0) return delta;
  }
  return 0;
}

function opatchMeetsReadmeMinimum() {
  const home = selectedHome();
  const analysis = state.readmeAnalysis || analyzeReadme(state.readmeText);
  return Boolean(home.oracleHome && home.opatchVersion && analysis.minimumOpatch && compareVersions(home.opatchVersion, analysis.minimumOpatch) >= 0);
}

function syncOpatchReadyFromDiscoveredVersion() {
  if (!opatchMeetsReadmeMinimum()) return false;
  state.opatchReady = true;
  state.failed.delete("opatch");
  return true;
}

function extractLineMatches(text, patterns) {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && patterns.some((pattern) => pattern.test(line)))
    .slice(0, 8);
}

function isSectionHeading(line) {
  return /^\s*Section\s+\d+(?:\.\d+)?\.?\s+/i.test(line);
}

function extractReadmeSection(text, headingPatterns) {
  const lines = String(text || "").split(/\r?\n/);
  const starts = lines
    .map((line, index) => headingPatterns.some((pattern) => pattern.test(line)) ? index : -1)
    .filter((index) => index >= 0);
  if (!starts.length) return "";
  let firstCandidate = "";

  for (const start of starts) {
    let end = lines.length;
    for (let index = start + 1; index < lines.length; index += 1) {
      const line = lines[index];
      if (isSectionHeading(line) || /^\s*(?:De[-\s]?installation|Rollback)\s+Instructions/i.test(line)) {
        end = index;
        break;
      }
    }
    const candidate = lines.slice(start, end).join("\n").trim();
    if (!firstCandidate) firstCandidate = candidate;
    const body = lines.slice(start + 1, end)
      .filter((line) => line.trim() && !/^\s*[-=]{3,}\s*$/.test(line))
      .join("\n")
      .trim();
    if (body) {
      return candidate;
    }
  }

  return firstCandidate;
}

function cleanSectionBody(sectionText, headingPattern) {
  return String(sectionText || "")
    .split(/\r?\n/)
    .filter((line, index) => {
      if (index === 0 && headingPattern.test(line)) return false;
      if (/^\s*[-=]{3,}\s*$/.test(line)) return false;
      return true;
    })
    .join("\n")
    .trim();
}

function splitInstructionBlocks(sectionText, headingPattern) {
  const body = cleanSectionBody(sectionText, headingPattern);
  if (!body) return [];
  const blocks = [];
  let current = [];
  for (const line of body.split(/\r?\n/)) {
    const startsInstruction = /^\s*(?:\d+\.|[-*]\s+)/.test(line);
    const startsNote = /^\s*(?:NOTE|IMPORTANT|WARNING|MANUAL ACTION)\s*:?/i.test(line);
    if ((startsInstruction || startsNote) && current.some((item) => item.trim())) {
      blocks.push(current.join("\n").trim());
      current = [];
    }
    current.push(line);
  }
  if (current.some((item) => item.trim())) blocks.push(current.join("\n").trim());
  return blocks.length ? blocks : [body];
}

function postinstallNeedsManualAction(step) {
  return /sqlplus|<passwd>|password|schema|ods|tns_admin|database|db\b|wlst|patch_oim_wls|manual|perform\s+the\s+steps/i.test(step);
}

function isOigOrOimHome(home = selectedHome()) {
  const text = `${home.product || ""} ${home.label || ""} ${home.oracleHome || ""} ${state.form.spbInstallType || ""}`.toLowerCase();
  return /\boig\b|\boim\b|identity\s+governance|oracle\s+identity\s+manager/.test(text);
}

function oigPostinstallSteps(home = selectedHome()) {
  const oracleHome = home.oracleHome || "$ORACLE_HOME";
  return [
    `For OIG/OIM, fill ${oracleHome}/idm/server/bin/patch_oim_wls.profile with the environment values required by the SPB README before running the OIG post-start script.`,
    `After SPBAT PostStart and service startup validation, run ${oracleHome}/idm/server/bin/patch_oim_wls.sh, then review the script output and product logs before closing the patch.`
  ];
}

function analyzeReadme(text) {
  const detectedMethod = detectedReadmePatchMethod(text) || (isStackPatchBundle() ? "spbat" : "opatch");
  const opatchMatch =
    text.match(/minimum\s+opatch\s+version\s*:?\s*([0-9]+(?:\.[0-9]+)+)/i) ||
    text.match(/opatch\s+(?:version\s+)?(?:must\s+be|should\s+be|requires|required|required version|min(?:imum)?)\s*:?\s*([0-9]+(?:\.[0-9]+)+)/i) ||
    text.match(/opatch[^0-9]{0,40}([0-9]+(?:\.[0-9]+){2,})/i);

  const backupTargets = [];
  if (/ORACLE_HOME/i.test(text)) backupTargets.push("ORACLE_HOME");
  if (/DOMAIN_HOME/i.test(text)) backupTargets.push("DOMAIN_HOME");
  if (/INSTANCE_HOME|OUD.*instance|instance.*OUD/i.test(text)) backupTargets.push("INSTANCE_HOME");

  const stopSteps = extractLineMatches(text, [/stop/i, /shut\s*down/i, /shutdown/i]);
  const postinstallHeadingPattern = /^\s*(?:(?:Section\s+)?\d+(?:\.\d+)*\.?\s+)?Post\s*[- ]?\s*Installation\s+Instructions/i;
  const postinstallSection = extractReadmeSection(text, [postinstallHeadingPattern]);
  const postinstallSteps = splitInstructionBlocks(postinstallSection, postinstallHeadingPattern);
  const startSteps = extractLineMatches(text, [/start/i, /restart/i, /validate/i, /health/i]);
  const spbatSteps = extractLineMatches(text, [/spbat/i, /spabt/i, /stack\s+patch/i, /downtime/i, /prestop/i, /inactive\s+patch/i, /deleteinactivepatches/i, /RETAIN_INACTIVE_PATCHES/i]);
  const inactivePatchCleanupRecommended = /Removing\s+Inactive\s+Patches|deleteinactivepatches|RETAIN_INACTIVE_PATCHES/i.test(text);
  const javaPrereqRecommended = /minimum\s+java|minimum\s+.*JDK|JDK\/JRE|Java\s+SE/i.test(text);
  const opatchHeapRecommended = /OPATCH_JRE_MEMORY_OPTIONS|Setting\s+Heap\s+size\s+for\s+OPatch|default\s+Heap\s+size/i.test(text);
  const oigSteps = detectedMethod === "spbat" && isOigOrOimHome() ? oigPostinstallSteps() : [];
  const effectivePostinstallSteps = postinstallSteps.length || oigSteps.length
    ? uniqueValues([...postinstallSteps, ...oigSteps])
    : ["No explicit postinstall section found. Manual README review is required before production execution."];

  return {
    patchMethod: detectedMethod,
    detectedPatchFamily: detectedMethod === "spbat" ? (/\bcspu\b|critical\s+stack\s+patch\s+update/i.test(text) ? "CSPU / SPBAT" : "SPBAT / Stack Patch Bundle") : "OPatch",
    minimumOpatch: opatchMatch ? opatchMatch[1] : detectedMethod === "spbat" ? "13.9.4.2.17" : "13.9.4.2.15",
    backupTargets: backupTargets.length ? backupTargets : ["ORACLE_HOME", "DOMAIN_HOME"],
    stopSteps: stopSteps.length ? stopSteps : ["Stop all WebLogic servers and Node Manager before applying this patch."],
    postinstallSection,
    postinstallSteps: effectivePostinstallSteps,
    postinstallManualSteps: effectivePostinstallSteps.map(postinstallNeedsManualAction),
    postinstallRequiresManual: effectivePostinstallSteps.some(postinstallNeedsManualAction),
    inactivePatchCleanupRecommended,
    javaPrereqRecommended,
    opatchHeapRecommended,
    startSteps: startSteps.length ? startSteps : ["Start services using the approved customer runbook or startup scripts, then confirm startup in PatchPilot."],
    conflictCommand: "$ORACLE_HOME/OPatch/opatch prereq CheckConflictAgainstOHWithDetail -ph ./",
    applyCommand: "$ORACLE_HOME/OPatch/opatch apply",
    rollbackCommand: "$ORACLE_HOME/OPatch/opatch rollback -id <patch_id>",
    spbatSteps,
    spbPhases: ["prestop", "downtime", "poststart"]
  };
}

function uniqueValues(items) {
  return [...new Set(items.filter(Boolean))];
}

function suggestedSpbTypes() {
  const home = selectedHome();
  const domainText = `${home.domainHome || ""}`.toLowerCase();
  const text = `${home.product || ""} ${home.label || ""} ${home.oracleHome || ""} ${domainText}`.toLowerCase();
  const values = [];
  const hasOig = /\boig\b|\boim\b|identity\s+governance|oracle\s+identity\s+manager/.test(text);
  const hasOam = /\boam\b|access\s+manager/.test(text);
  if (/\boam(?:_|-)?domain\b|\/oam(?:_|-)?domain\b/.test(domainText)) values.push("oam");
  else if (/\b(?:oig|oim)(?:_|-)?domain\b|\/(?:oig|oim)(?:_|-)?domain\b/.test(domainText)) values.push("oig");
  else if (hasOig && !hasOam) values.push("oig");
  else if (hasOam && !hasOig) values.push("oam");
  else if (hasOam && hasOig) values.push("oam", "oig");
  if (text.includes("oud")) values.push("oud");
  if (text.includes("oid")) values.push("oid");
  if (!hasOig && hasOam && !values.includes("oam")) values.push("oam");
  return uniqueValues(values.length ? values : ["oam", "oig", "oud", "oid"]);
}

function spbInstallType() {
  const explicit = String(state.form.spbInstallType || "").toLowerCase();
  if (["oam", "oig", "oud", "oid"].includes(explicit)) return explicit;
  return suggestedSpbTypes()[0] || "oam";
}

function spbInstallTypeLabel() {
  const resolved = spbInstallType().toUpperCase();
  return String(state.form.spbInstallType || "auto").toLowerCase() === "auto"
    ? `Auto (${resolved})`
    : resolved;
}

function spbExtraArgs() {
  return String(state.form.spbExtraArgs || "").trim();
}

function opatchHeapOptions() {
  const value = String(state.form.opatchHeapOptions || DEFAULT_OPATCH_HEAP_OPTIONS).trim();
  return value || DEFAULT_OPATCH_HEAP_OPTIONS;
}

function shellSingleQuote(value) {
  return "'" + String(value).replace(/'/g, "'\\''") + "'";
}

function opatchHeapExportCommand() {
  return `export OPATCH_JRE_MEMORY_OPTIONS=${shellSingleQuote(opatchHeapOptions())}`;
}

function opatchHeapDisplay(heap) {
  if (heap && heap.effective) {
    return `${heap.effective}${heap.action ? ` (${heap.action})` : ""}`;
  }
  return opatchHeapOptions();
}

function spbTypeOptionsMarkup(autoType) {
  return `
    <option value="auto" ${String(state.form.spbInstallType || "auto").toLowerCase() === "auto" ? "selected" : ""}>Auto - ${escapeHtml(autoType.toUpperCase())} suggested</option>
    ${["oig", "oam", "oud", "oid"].map((type) => `<option value="${type}" ${String(state.form.spbInstallType || "").toLowerCase() === type ? "selected" : ""}>${type.toUpperCase()}${type === autoType ? " - suggested" : ""}</option>`).join("")}
  `;
}

function invalidateSpbCommandSettings(options = {}) {
  const refreshReadme = options.refreshReadme !== false;
  state.spbPrepared = false;
  state.spbPrepareResult = null;
  state.spbPrepareStatus = "idle";
  state.spbPrepareError = "";
  state.spbPrestopDone = false;
  state.spbDowntimeDone = false;
  state.spbPoststartDone = false;
  resetOigPostinstallState();
  state.opatchReady = false;
  state.completed.delete("spbPrepare");
  state.failed.delete("spbPrepare");
  state.completed.delete("spbPrestop");
  state.completed.delete("spbDowntime");
  state.completed.delete("spbPoststart");
  state.completed.delete("opatch");
  if (refreshReadme && state.readmeText) {
    state.readmeAnalysis = analyzeReadme(state.readmeText);
    resetPostinstallState();
  }
}

function updateVisibleSpbCommandSettings() {
  const typeSummary = document.getElementById("spbTypeSummary");
  if (typeSummary) typeSummary.textContent = spbInstallType().toUpperCase();
  const extraSummary = document.getElementById("spbExtraArgsSummary");
  if (extraSummary) extraSummary.textContent = spbExtraArgs() || "None";
  const heapSummary = document.getElementById("opatchHeapSummary");
  if (heapSummary) heapSummary.textContent = opatchHeapOptions();
  document.querySelectorAll("[data-spb-command-phase]").forEach((node) => {
    const phase = node.dataset.spbCommandPhase;
    node.textContent = phase === "status" ? spbatStatusCommand() : spbatCommand(phase);
  });
}

function safeChangeRef() {
  return String(state.form.changeRef || "patchpilot-run").replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/^_+|_+$/g, "") || "patchpilot-run";
}

function unixParentPath(path) {
  const normalized = String(path || "").trim().replace(/\/+$/g, "");
  if (!normalized || normalized === "/") return "";
  const index = normalized.lastIndexOf("/");
  if (index <= 0) return "/";
  return normalized.slice(0, index);
}

function spbLogDir() {
  return state.form.spbLogDir.trim() || `${state.form.patchPath.replace(/\/+$/g, "")}/spbat_logs`;
}

function defaultBackupDirForRun() {
  const homeParent = unixParentPath(selectedHome().oracleHome);
  if (homeParent) return `${homeParent.replace(/\/+$/g, "")}/backups`;
  return "/u01/backups";
}

function backupDirForRun() {
  if (state.backupDirOverride.trim()) return state.backupDirOverride.trim();
  return defaultBackupDirForRun();
}

function backupReferenceText() {
  const archives = state.backupResult && Array.isArray(state.backupResult.results)
    ? state.backupResult.results.map((item) => item.archive).filter(Boolean)
    : [];
  if (archives.length) return archives.join(", ");
  if (state.backupExternalApproved) return state.backupExternalNote || "External backup confirmed";
  return backupDirForRun();
}

function requiresDatabaseBackupForRun() {
  const patchType = String(state.form.patchType || "");
  const readmeText = String(state.readmeText || "");
  return Boolean(
    isStackPatchBundle()
    || /\b(?:PSU|SPU|CSPU)\b/i.test(patchType)
    || /bundle\s+patch/i.test(patchType)
    || /\b(?:PSU|SPU|CSPU)\b|stack\s+patch\s+bundle|critical\s+stack\s+patch\s+update|bundle\s+patch/i.test(readmeText)
  );
}

function backupFilesComplete() {
  return Boolean(state.backupsDone || state.backupExternalApproved);
}

function backupGateComplete() {
  return backupFilesComplete() && (!requiresDatabaseBackupForRun() || state.databaseBackupConfirmed);
}

function backupShutdownStepId() {
  return isStackPatchBundle() ? "spbBackupShutdown" : "shutdown";
}

function backupShutdownComplete() {
  return shutdownGateSatisfied(backupShutdownStepId());
}

function backupGateBlockReason() {
  if (!backupFilesComplete()) return "Complete file-system backups or confirm approved external backups.";
  if (requiresDatabaseBackupForRun() && !state.databaseBackupConfirmed) return "Confirm the customer/DBA completed the required database backup for this patch type.";
  return "";
}

function syncBackupCompletion() {
  if (backupGateComplete()) {
    state.completed.add("backup");
  } else {
    state.completed.delete("backup");
  }
}

function resetBackupPreflight() {
  state.backupDestinationDirty = false;
  state.backupPreflight = null;
  state.backupPreflightConfirmed = false;
  state.backupResult = null;
  state.backupOutput = "";
  state.backupsDone = false;
  state.completed.delete("backup");
}

function resetDatabaseBackupState() {
  state.databaseBackupConfirmed = false;
  state.databaseBackupNote = "";
  state.completed.delete("backup");
}

function resetPostinstallState() {
  state.postinstallConfirmed = false;
  state.postinstallDone = false;
  state.completed.delete("postinstall");
  resetSpbCleanupState();
}

function resetSpbCleanupState(options = {}) {
  state.spbCleanupApproved = false;
  if (!options.keepTargets) state.spbCleanupTargetsText = "";
  state.spbCleanupResult = null;
  state.spbCleanupAttempts = [];
  state.spbCleanupOutput = "";
  state.spbCleanupError = "";
  state.spbCleanupStatus = "idle";
  state.completed.delete("postinstall");
  state.failed.delete("postinstall");
}

function resetPatchApplyState() {
  state.patchApplied = false;
  state.patchResult = null;
  state.patchApplyOutput = "";
  state.patchProgressPhase = "idle";
  state.patchStartedAt = 0;
  state.opatchDebugUsed = false;
  state.rollbackInProgress = false;
  state.rollbackDone = false;
  state.rollbackResult = null;
  state.rollbackOutput = "";
  state.completed.delete("apply");
  state.failed.delete("apply");
  resetPostinstallState();
}

function rollbackPatchId() {
  const result = state.patchResult || {};
  if (result.primaryPatchId) return result.primaryPatchId;
  if (Array.isArray(result.patchIds) && result.patchIds.length) return result.patchIds[0];
  const matches = String(state.form.patchPath || "").match(/\d{5,}/g);
  return matches && matches.length ? matches[matches.length - 1] : "";
}

function completedPatchLabel() {
  if (isStackPatchBundle()) {
    const type = spbInstallType();
    const matches = String(state.form.patchPath || "").match(/\d{5,}(?:[._-]\d+)*/g);
    const bundle = matches && matches.length ? matches[matches.length - 1] : "SPB";
    return `${bundle} (${String(type || "SPB").toUpperCase()})`;
  }
  const result = state.patchResult || {};
  const found = Array.isArray(result.foundPatchIds) ? result.foundPatchIds.filter(Boolean) : [];
  if (found.length) return found.join(", ");
  if (result.primaryPatchId) return result.primaryPatchId;
  if (Array.isArray(result.patchIds) && result.patchIds.length) return result.patchIds.join(", ");
  return rollbackPatchId() || "selected patch";
}

function patchIdsFromPath(path) {
  const matches = String(path || "").match(/\d{5,}/g);
  return matches && matches.length ? [matches[matches.length - 1]] : [];
}

function buildClientPatchFailureResult(error, output = "", dryRun = false) {
  const home = selectedHome();
  const patchIds = patchIdsFromPath(state.form.patchPath);
  const logLocations = extractOpatchLogLocations(output || state.patchApplyOutput || "");
  return {
    status: "failed",
    dryRun,
    debug: state.opatchDebugUsed || opatchDebugEnabled(),
    oracleHome: home.oracleHome,
    patchPath: state.form.patchPath,
    applyDir: state.form.patchPath,
    patchIds,
    primaryPatchId: patchIds[0] || "",
    opatchVersion: home.opatchVersion || "",
    opatchHeap: { effective: opatchHeapOptions(), action: "configured" },
    inventoryVerified: false,
    opatchLogLocations: logLocations,
    opatchLogLocation: logLocations.length ? logLocations[logLocations.length - 1] : "",
    error: error || "OPatch failed before PatchPilot received a detailed failure payload.",
    applyOutput: output || state.patchApplyOutput || ""
  };
}

function patchFailureOutput(report) {
  const patch = report.patchResult || {};
  return patch.applyOutput ||
    patch.conflictOutput ||
    patch.inventoryOutput ||
    patch.versionOutput ||
    report.patchApplyOutput ||
    "";
}

function patchFailureError(report) {
  const patch = report.patchResult || {};
  const output = patchFailureOutput(report);
  if (patch.error) return patch.error;
  if (patch.message && patch.status === "failed") return patch.message;
  const lines = String(output || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const matched = lines.filter((line) => /(zop-|opatch failed|failed|failure|error|conflict|not applicable|not valid|already applied)/i.test(line));
  return matched.length ? matched.slice(-3).join(" | ") : "OPatch failed. Review the captured output in this report.";
}

function extractOpatchLogLocation(text) {
  const locations = extractOpatchLogLocations(text);
  return locations.length ? locations[locations.length - 1] : "";
}

function extractOpatchLogLocations(text) {
  const matches = [...String(text || "").matchAll(/Log file location\s*:\s*([^\r\n]+)/gi)];
  return uniqueValues(matches.map((match) => match[1].trim()));
}

function opatchLogLocationsFromPatch(patch = state.patchResult, output = state.patchApplyOutput) {
  const patchLogs = Array.isArray(patch && patch.opatchLogLocations) ? patch.opatchLogLocations : [];
  return uniqueValues([
    ...patchLogs,
    patch && patch.opatchLogLocation,
    ...extractOpatchLogLocations(output || ""),
    ...extractOpatchLogLocations((patch && patch.applyOutput) || ""),
    ...extractOpatchLogLocations((patch && patch.conflictOutput) || ""),
    ...extractOpatchLogLocations((patch && patch.inventoryOutput) || ""),
    ...extractOpatchLogLocations((patch && patch.versionOutput) || "")
  ]);
}

function currentOpatchLogLocation() {
  const logs = opatchLogLocationsFromPatch();
  return logs.length ? logs[logs.length - 1] : "";
}

function opatchDebugEnabled() {
  return Boolean(dom.opatchDebugToggle && dom.opatchDebugToggle.checked);
}

function opatchDebugArgsText() {
  return opatchDebugEnabled() ? " -verbose -debug" : "";
}

function parseSizeToGb(value) {
  const match = String(value || "").match(/([0-9]+(?:\.[0-9]+)?)\s*([KMGT])?B?/i);
  if (!match) return null;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount)) return null;
  const unit = (match[2] || "G").toUpperCase();
  if (unit === "T") return amount * 1024;
  if (unit === "M") return amount / 1024;
  if (unit === "K") return amount / (1024 * 1024);
  return amount;
}

function selectedOracleHomeSizeGb() {
  const rows = state.backupPreflight && Array.isArray(state.backupPreflight.results) ? state.backupPreflight.results : [];
  const oracleHomeRow = rows.find((item) => item.target === "ORACLE_HOME");
  return parseSizeToGb(oracleHomeRow && oracleHomeRow.sourceSize);
}

function inventoryPatchCountFromOutput(output) {
  const text = String(output || "");
  const interim = text.match(/Interim\s+patches\s*\(\s*(\d+)\s*\)/i);
  if (interim) return Number(interim[1]);
  const lspatches = text.match(/^\s*\d{5,}\s*;/gm);
  return lspatches && lspatches.length ? lspatches.length : null;
}

function currentInventoryPatchCount() {
  const patch = state.patchResult || {};
  const count = inventoryPatchCountFromOutput([
    state.patchApplyOutput || "",
    patch.inventoryTail || "",
    patch.inventoryOutput || "",
    patch.beforeInventory || ""
  ].join("\n"));
  return Number.isFinite(count) ? count : null;
}

function patchDurationEstimate(options = {}) {
  const type = reportPatchType({ patchType: state.form.patchType, readmeAnalysis: state.readmeAnalysis || {} }).toLowerCase();
  const dryRun = typeof options.dryRun === "boolean" ? options.dryRun : Boolean(dom.dryRunToggle && dom.dryRunToggle.checked);
  const debug = typeof options.debug === "boolean" ? options.debug : opatchDebugEnabled();
  const homeSize = selectedOracleHomeSizeGb();
  const inventoryPatchCount = currentInventoryPatchCount();
  let low = dryRun ? 3 : 8;
  let high = dryRun ? 10 : 25;
  if (/psu|spu|bundle/.test(type)) {
    low += dryRun ? 4 : 12;
    high += dryRun ? 10 : 25;
  }
  if (/stack|spb|cspu/.test(type)) {
    low = dryRun ? 10 : 35;
    high = dryRun ? 25 : 90;
  }
  if (homeSize !== null) {
    if (homeSize > 20) {
      low += 15;
      high += 35;
    } else if (homeSize > 10) {
      low += 8;
      high += 18;
    } else if (homeSize > 5) {
      low += 4;
      high += 10;
    }
  }
  if (debug) {
    low += 2;
    high += 8;
  }
  if (inventoryPatchCount !== null && inventoryPatchCount > 30) {
    low += 5;
    high += 15;
  }
  const basis = [
    homeSize !== null ? `ORACLE_HOME size ${homeSize.toFixed(homeSize >= 10 ? 0 : 1)} GB` : "ORACLE_HOME size not checked yet",
    state.form.patchType || "patch type not selected",
    inventoryPatchCount !== null ? `${inventoryPatchCount} patches visible in OPatch inventory output` : "inventory patch count not seen yet",
    dryRun ? "dry-run only" : "real apply",
    debug ? "verbose/debug enabled" : "standard OPatch output"
  ];
  return {
    label: `Planning estimate: ${low}-${high} minutes`,
    basis: basis.join("; ")
  };
}

function normalizeTimestamp(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function nowSeconds() {
  return Date.now() / 1000;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const remainingSeconds = total % 60;
  if (minutes < 60) return remainingSeconds ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function formatTimestamp(value) {
  const timestamp = normalizeTimestamp(value);
  if (!timestamp) return "Not started";
  return new Date(timestamp * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function inactivePatchCountForEstimate(check = state.spbInactiveCheck) {
  if (!check) return 0;
  if (Number.isFinite(Number(check.inactivePatchCount))) return Number(check.inactivePatchCount);
  if (Array.isArray(check.inactiveChainCounts) && check.inactiveChainCounts.length) {
    return check.inactiveChainCounts.reduce((sum, item) => sum + (Number(item) || 0), 0);
  }
  return check.hasInactive ? 1 : 0;
}

function runtimePercent(status, startedAt, finishedAt, estimate, stagePercent = 0) {
  if (status === "succeeded") return 100;
  if (status === "failed") return 100;
  const started = normalizeTimestamp(startedAt);
  if (!started) return 0;
  const elapsed = Math.max(0, (normalizeTimestamp(finishedAt) || nowSeconds()) - started);
  const highSeconds = Math.max(60, Number(estimate && estimate.highMinutes) * 60 || 60);
  const timePercent = Math.round((elapsed / highSeconds) * 92);
  return Math.min(96, Math.max(6, stagePercent, timePercent));
}

function runtimeRemainingLabel(status, startedAt, finishedAt, estimate) {
  const started = normalizeTimestamp(startedAt);
  if (!started) return "Waiting to start";
  const elapsed = Math.max(0, (normalizeTimestamp(finishedAt) || nowSeconds()) - started);
  if (status === "succeeded") return `Completed in ${formatDuration(elapsed)}`;
  if (status === "failed") return `Stopped after ${formatDuration(elapsed)}`;
  const lowSeconds = Math.max(60, Number(estimate && estimate.lowMinutes) * 60 || 60);
  const highSeconds = Math.max(lowSeconds, Number(estimate && estimate.highMinutes) * 60 || lowSeconds);
  if (elapsed < lowSeconds) return `At least ${formatDuration(lowSeconds - elapsed)} before the lower estimate`;
  if (elapsed < highSeconds) return `About ${formatDuration(highSeconds - elapsed)} before the high estimate`;
  return `Past estimate by ${formatDuration(elapsed - highSeconds)}; still running`;
}

function runtimeEstimateLabel(estimate) {
  const low = Number(estimate && estimate.lowMinutes) || 0;
  const high = Number(estimate && estimate.highMinutes) || low;
  return `${low}-${high} minutes`;
}

function spbRuntimeSizeAdjustment(estimate) {
  const homeSize = selectedOracleHomeSizeGb();
  if (homeSize !== null) {
    if (homeSize > 20) {
      estimate.lowMinutes += 10;
      estimate.highMinutes += 30;
    } else if (homeSize > 10) {
      estimate.lowMinutes += 5;
      estimate.highMinutes += 15;
    } else if (homeSize > 5) {
      estimate.lowMinutes += 2;
      estimate.highMinutes += 8;
    }
    estimate.basis.push(`ORACLE_HOME size ${homeSize.toFixed(homeSize >= 10 ? 0 : 1)} GB`);
  } else {
    estimate.basis.push("ORACLE_HOME size not measured");
  }
}

function spbPhaseRuntimeEstimate(phase) {
  const type = spbInstallType().toUpperCase();
  const estimate = {
    lowMinutes: phase === "downtime" ? 25 : phase === "poststart" ? 8 : 12,
    highMinutes: phase === "downtime" ? 75 : phase === "poststart" ? 30 : 40,
    basis: [`${spbPhaseLabel(phase)} phase`, `${type} SPBAT type`]
  };
  if (type === "OIG") {
    estimate.lowMinutes += 5;
    estimate.highMinutes += 15;
  }
  const inactiveCount = inactivePatchCountForEstimate();
  if (inactiveCount > 0) {
    if (state.spbInactiveSkipConfirmed) {
      estimate.lowMinutes += Math.min(20, inactiveCount * 3);
      estimate.highMinutes += Math.min(90, inactiveCount * 10);
      estimate.basis.push(`${inactiveCount} inactive patch(es) kept`);
    } else if (state.spbInactiveResult && state.spbInactiveResult.status === "removed") {
      estimate.basis.push("inactive patch cleanup completed");
    } else {
      estimate.lowMinutes += Math.min(10, inactiveCount * 2);
      estimate.highMinutes += Math.min(45, inactiveCount * 6);
      estimate.basis.push(`${inactiveCount} inactive patch(es) reported`);
    }
  } else {
    estimate.basis.push("no inactive patches reported");
  }
  if (/debug/i.test(spbExtraArgs())) {
    estimate.lowMinutes += 3;
    estimate.highMinutes += 12;
    estimate.basis.push("debug arguments enabled");
  }
  spbRuntimeSizeAdjustment(estimate);
  return estimate;
}

function spbInactiveRuntimeEstimate() {
  const inactiveCount = inactivePatchCountForEstimate();
  const estimate = {
    lowMinutes: 2,
    highMinutes: 8,
    basis: [
      inactiveCount ? `${inactiveCount} inactive patch(es) from OPatch review` : "inactive patch count not available",
      `retain level N-${spbInactiveRetainLevel()}`
    ]
  };
  if (inactiveCount > 3) {
    estimate.lowMinutes += Math.min(6, Math.ceil(inactiveCount / 3));
    estimate.highMinutes += Math.min(25, inactiveCount * 3);
  }
  spbRuntimeSizeAdjustment(estimate);
  return estimate;
}

function spbPhaseStageInfo(phase) {
  const status = effectiveSpbPhaseStatus(phase);
  if (status === "succeeded") return { label: "SPBAT phase completed", percent: 100 };
  if (status === "failed") return { label: "SPBAT phase failed", percent: 100 };
  if (status === "accepted") return { label: "Accepted outside PatchPilot", percent: 100 };
  const output = String(state.spbPhaseOutput[phase] || "");
  if (/status report/i.test(output)) return { label: "Reading SPBAT status report", percent: 82 };
  if (/latest log\/report|still running/i.test(output)) return { label: "SPBAT phase is still running", percent: 28 };
  if (/PatchPilot phase log:/i.test(output)) return { label: "Capturing SPBAT phase log", percent: 18 };
  return { label: status === "running" ? "Running SPBAT phase command" : spbPhaseStatusText(phase), percent: 8 };
}

function spbInactiveStageInfo() {
  const output = String(state.spbInactiveJobOutput || "");
  if (state.spbInactiveJobStatus === "cancelled") return { label: "Stopped by user", percent: 100 };
  if (state.spbInactiveJobKind === "check") {
    if (/Waiting for another OPatch/i.test(output)) return { label: "Waiting for another OPatch process", percent: 18 };
    if (/listorderedinactivepatches|Loading the Java runtime|Oracle inventory/i.test(output)) return { label: "Reading Oracle inventory", percent: 35 };
    return { label: "Starting inactive patch review", percent: 8 };
  }
  if (/OPatch cleanup completed/i.test(output)) return { label: "OPatch cleanup completed", percent: 100 };
  if (/Starting OPatch cleanup|util cleanup|cleanup prompts/i.test(output)) return { label: "Running OPatch cleanup", percent: 72 };
  if (/deleteinactivepatches|Inactive Patches Cleanup option/i.test(output)) return { label: "Deleting inactive patches", percent: 36 };
  if (/RETAIN_INACTIVE_PATCHES|retain property/i.test(output)) return { label: "Setting retain property", percent: 12 };
  return { label: "Starting inactive patch cleanup", percent: 8 };
}

function spbInactiveDelayExplanation() {
  const output = String(state.spbInactiveJobOutput || "");
  const elapsed = Math.max(0, nowSeconds() - (normalizeTimestamp(state.spbInactiveJobStartedAt) || nowSeconds()));
  if (/Waiting for another OPatch/i.test(output)) {
    return "Another OPatch inactive-patch utility is using this ORACLE_HOME. PatchPilot is waiting to avoid an Oracle inventory lock conflict.";
  }
  if (elapsed < 30) {
    return "OPatch is starting its Java runtime and loading the Oracle inventory. It may not print output during this startup period.";
  }
  if (state.spbInactiveJobKind === "cleanup") {
    return "OPatch is updating inventory metadata and removing inactive patch files. A large inventory, slow disk or NFS storage, high CPU or memory use, or an existing inventory lock can extend this stage.";
  }
  return "OPatch is still reading the Oracle inventory. Common causes are a large patch inventory, a slow disk or NFS mount, high CPU or memory use, an inventory lock held by another OPatch session, or SSH latency. OPatch does not provide an exact percentage or ETA for this command.";
}

function renderRuntimeProgressPanel({ title, status, startedAt, finishedAt, estimate, stage }) {
  const started = normalizeTimestamp(startedAt);
  if (!started && status !== "running") return "";
  const finished = normalizeTimestamp(finishedAt);
  const effectiveStatus = status || "running";
  const elapsed = Math.max(0, (finished || nowSeconds()) - (started || nowSeconds()));
  const percent = runtimePercent(effectiveStatus, started || nowSeconds(), finished, estimate, stage && stage.percent);
  const statusLabel = effectiveStatus === "running"
    ? `Running for ${formatDuration(elapsed)}`
    : effectiveStatus === "succeeded"
      ? `Completed in ${formatDuration(elapsed)}`
      : effectiveStatus === "failed"
        ? `Failed after ${formatDuration(elapsed)}`
        : effectiveStatus === "cancelled"
          ? `Stopped after ${formatDuration(elapsed)}`
        : `${effectiveStatus} after ${formatDuration(elapsed)}`;
  const progressLabel = effectiveStatus === "running" ? "Remaining" : "Outcome";
  const progressValue = effectiveStatus === "running"
    ? runtimeRemainingLabel(effectiveStatus, started || nowSeconds(), finished, estimate)
    : effectiveStatus === "succeeded"
      ? "Completed successfully"
      : effectiveStatus === "failed"
        ? "Review required"
        : effectiveStatus === "cancelled"
          ? "Stopped by user"
        : runtimeRemainingLabel(effectiveStatus, started || nowSeconds(), finished, estimate);
  return `
    <div class="runtime-panel is-${escapeHtml(effectiveStatus)}">
      <div class="runtime-panel-header">
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(statusLabel)}</span>
      </div>
      <div class="meter runtime-meter"><span style="width: ${escapeHtml(String(percent))}%"></span></div>
      <div class="runtime-grid">
        <div><span>Elapsed</span><strong>${escapeHtml(formatDuration(elapsed))}</strong></div>
        <div><span>Estimate</span><strong>${escapeHtml(runtimeEstimateLabel(estimate))}</strong></div>
        <div><span>${escapeHtml(progressLabel)}</span><strong>${escapeHtml(progressValue)}</strong></div>
        <div><span>Stage</span><strong>${escapeHtml((stage && stage.label) || "Running")}</strong></div>
      </div>
      <p class="runtime-basis">Started ${escapeHtml(formatTimestamp(started))}. Estimate basis: ${escapeHtml((estimate && estimate.basis || []).join("; "))}.</p>
    </div>
  `;
}

function renderSpbPhaseRuntimePanel(phase) {
  const job = state.spbPhaseJobs[phase] || {};
  const status = effectiveSpbPhaseStatus(phase);
  const terminal = ["succeeded", "failed", "accepted"].includes(status);
  return renderRuntimeProgressPanel({
    title: `${spbPhaseLabel(phase)} runtime`,
    status: terminal ? status : job.status || status,
    startedAt: job.startedAt,
    finishedAt: job.finishedAt,
    estimate: spbPhaseRuntimeEstimate(phase),
    stage: spbPhaseStageInfo(phase)
  });
}

function renderSpbInactiveRuntimePanel() {
  return renderRuntimeProgressPanel({
    title: state.spbInactiveJobKind === "check" ? "Inactive patch review runtime" : "Inactive patch cleanup runtime",
    status: state.spbInactiveJobStatus,
    startedAt: state.spbInactiveJobStartedAt,
    finishedAt: state.spbInactiveJobFinishedAt,
    estimate: spbInactiveRuntimeEstimate(),
    stage: spbInactiveStageInfo()
  });
}

const PATCH_PROGRESS_PHASES = {
  idle: { percent: 0, label: "Not started" },
  preparing: { percent: 8, label: "Preparing OPatch job" },
  version: { percent: 15, label: "Checking OPatch version" },
  inventoryBefore: { percent: 25, label: "Reading current inventory" },
  conflict: { percent: 40, label: "Running conflict check" },
  apply: { percent: 60, label: "Applying patch files" },
  inventoryAfter: { percent: 85, label: "Verifying updated inventory" },
  dryRunComplete: { percent: 100, label: "Dry-run complete" },
  complete: { percent: 100, label: "Complete" },
  failed: { percent: 0, label: "Failed" }
};

function patchProgressLabel() {
  const phase = PATCH_PROGRESS_PHASES[state.patchProgressPhase] || PATCH_PROGRESS_PHASES.idle;
  return phase.label;
}

function setPatchProgressPhase(phase) {
  const next = PATCH_PROGRESS_PHASES[phase] || PATCH_PROGRESS_PHASES.preparing;
  const current = PATCH_PROGRESS_PHASES[state.patchProgressPhase] || PATCH_PROGRESS_PHASES.idle;
  if (next.percent >= current.percent || phase === "failed" || phase === "complete" || phase === "dryRunComplete") {
    state.patchProgressPhase = phase;
  }
  state.progress = Math.max(state.progress || 0, next.percent);
}

function updatePatchProgressFromOutput(output, dryRun) {
  const text = String(output || "");
  const lower = text.toLowerCase();
  const applyIndex = lower.lastIndexOf("opatch apply");
  const inventoryIndex = lower.lastIndexOf("opatch lsinventory");
  if (/PatchPilot OPatch run started|Resolved OPatch apply directory/i.test(text)) setPatchProgressPhase("preparing");
  if (/\$ .*opatch\s+version|Oracle Interim Patch Installer version|OPatch Version/i.test(text)) setPatchProgressPhase("version");
  if (inventoryIndex >= 0 && (applyIndex < 0 || inventoryIndex < applyIndex)) setPatchProgressPhase("inventoryBefore");
  if (/CheckConflictAgainstOHWithDetail|checkconflictagainstohwithdetail/i.test(text)) setPatchProgressPhase("conflict");
  if (dryRun) return;
  if (applyIndex >= 0 || /Applying patch/i.test(text)) setPatchProgressPhase("apply");
  if (applyIndex >= 0 && inventoryIndex > applyIndex) setPatchProgressPhase("inventoryAfter");
}

function patchFailureCategory(report) {
  const patch = report.patchResult || {};
  const text = `${patch.error || ""}\n${patch.message || ""}\n${patchFailureOutput(report)}`.toLowerCase();
  if (patch.alreadyApplied || /already\s+(exists|applied|present)/i.test(text)) return "Patch already applied or already present";
  if (/conflict|checkconflictagainstohwithdetail/i.test(text)) return "OPatch conflict or prerequisite failure";
  if (/not applicable|does not apply|not suitable|not needed/i.test(text)) return "Patch not applicable to selected ORACLE_HOME";
  if (/patch location is not valid|correct metadata|metadata/i.test(text)) return "Patch source or metadata issue";
  if (/lsinventory/i.test(text)) return "OPatch inventory check failure";
  return "OPatch apply failure";
}

function spbatDir() {
  return `${state.form.patchPath.replace(/\/+$/g, "")}/tools/spbat/generic/SPBAT`;
}

function spbatCommand(phase) {
  const home = selectedHome();
  const extra = spbExtraArgs();
  return `${opatchHeapExportCommand()} && cd ${spbatDir()} && ./spbat.sh -type ${spbInstallType()} -phase ${phase} -mw_home ${home.oracleHome} -spb_download_dir ${state.form.patchPath} -log_dir ${spbLogDir()} -verbose true${extra ? ` ${extra}` : ""}`;
}

function spbatStatusCommand() {
  const home = selectedHome();
  return `${opatchHeapExportCommand()} && cd ${spbatDir()} && ./spbat.sh -status report -type ${spbInstallType()} -mw_home ${home.oracleHome} -log_dir ${spbLogDir()}`;
}

function spbInactiveListCommand() {
  const home = selectedHome();
  return `${opatchHeapExportCommand()} && ${home.oracleHome}/OPatch/opatch util listorderedinactivepatches -oh ${home.oracleHome}`;
}

function spbInactiveRetainCommand() {
  const home = selectedHome();
  const level = spbInactiveRetainLevel();
  return `Set RETAIN_INACTIVE_PATCHES=${level} in ${home.oracleHome}/OPatch/config/opatch.properties`;
}

function spbInactiveDeleteCommand() {
  const home = selectedHome();
  return `${opatchHeapExportCommand()} && printf 'y\\n' | ${home.oracleHome}/OPatch/opatch util deleteinactivepatches -oh ${home.oracleHome}`;
}

function spbInactiveCleanupCommand() {
  const home = selectedHome();
  return `${opatchHeapExportCommand()} && printf 'y\\n' | ${home.oracleHome}/OPatch/opatch util cleanup -oh ${home.oracleHome}`;
}

function spbInactiveRetainLevel() {
  const value = Number.parseInt(String(state.spbInactiveRetainLevel || "1"), 10);
  if (!Number.isFinite(value) || value < 1) return 1;
  return Math.min(value, 9);
}

function spbInactiveHasPatches(check = state.spbInactiveCheck) {
  return Boolean(check && check.hasInactive);
}

function spbInactiveDesiredReached(check = state.spbInactiveCheck) {
  return Boolean(check && check.desiredReached);
}

function spbInactiveSummary(check = state.spbInactiveCheck) {
  if (!check) return "Not checked";
  if (!check.hasInactive) return "No inactive patches reported";
  if (check.totalLine) return check.totalLine;
  const patchCount = check.inactivePatchCount;
  const overlayCount = check.inactiveOverlayCount;
  const parts = [];
  if (patchCount !== null && patchCount !== undefined) parts.push(`${patchCount} inactive patch chain(s)`);
  if (overlayCount !== null && overlayCount !== undefined) parts.push(`${overlayCount} inactive overlay patch(es)`);
  return parts.length ? parts.join(", ") : "Inactive patches reported; review OPatch output";
}

function spbInactiveCleanupSatisfied(check = state.spbInactiveCheck, result = state.spbInactiveResult) {
  if (result && ["removed", "already-retained", "none"].includes(result.status)) return true;
  if (check && !check.hasInactive) return true;
  return false;
}

function spbInactiveCleanupSucceeded(result = state.spbInactiveResult) {
  return Boolean(result && ["removed", "already-retained", "none"].includes(result.status));
}

function spbInactiveCleanupIssue(result = state.spbInactiveResult) {
  return Boolean(!state.spbInactiveSkipConfirmed && result && result.status && !spbInactiveCleanupSucceeded(result));
}

function acceptSpbInactiveKeepDecision(options = {}) {
  const previousResult = state.spbInactiveResult || {};
  const previousOutput = previousResult.output || previousResult.cleanupOutput || state.spbInactiveJobOutput || "";
  state.spbInactiveResult = {
    ...previousResult,
    status: "skipped",
    error: "",
    output: previousOutput || state.spbInactiveError || "",
    beforeCheck: previousResult.beforeCheck || state.spbInactiveCheck || null,
    afterCheck: state.spbInactiveCheck || previousResult.afterCheck || null,
    beforeSummary: previousResult.beforeSummary || spbInactiveSummary(state.spbInactiveCheck),
    afterSummary: spbInactiveSummary(state.spbInactiveCheck),
    failedCommand: "",
    returnCode: "",
    opatchHeap: previousResult.opatchHeap || (state.spbInactiveCheck && state.spbInactiveCheck.opatchHeap) || { effective: opatchHeapOptions(), action: "configured" },
    deleteRuns: previousResult.deleteRuns || 0
  };
  state.spbInactiveSkipConfirmed = true;
  state.spbInactiveRemoveConfirmed = false;
  state.spbInactiveReviewed = true;
  state.spbInactiveError = "";
  state.completed.add("spbInactive");
  state.failed.delete("spbInactive");
  log("Inactive patches will be kept for this run. SPBAT may take longer; this decision will be captured in the report.", "warn");
  render();
  if (options.advance && canContinue()) {
    goNext();
  }
}

function spbInactiveBackupSafetyConfirmed() {
  return backupShutdownComplete() && backupGateComplete();
}

function spbInactiveDeleteReady() {
  return Boolean(
    state.spbInactiveCheck
    && spbInactiveHasPatches()
    && !spbInactiveDesiredReached()
    && state.spbInactiveRemoveConfirmed
    && !state.spbInactiveSkipConfirmed
    && spbInactiveBackupSafetyConfirmed()
  );
}

function spbInactiveEarlyComplete() {
  if (!isStackPatchBundle()) return true;
  if (spbInactiveCleanupSatisfied()) return true;
  return state.spbInactiveSkipConfirmed;
}

function spbInactiveDowntimeReady() {
  if (!isStackPatchBundle()) return true;
  if (spbInactiveCleanupSatisfied()) return true;
  return state.spbInactiveSkipConfirmed;
}

function oigProfilePath() {
  return `${selectedHome().oracleHome}/idm/server/bin/patch_oim_wls.profile`;
}

function oigScriptPath() {
  return `${selectedHome().oracleHome}/idm/server/bin/patch_oim_wls.sh`;
}

function oigScriptCommand() {
  const scriptPath = oigScriptPath();
  return `cd ${unixParentPath(scriptPath)} && ${scriptPath}`;
}

function oigProfileEditableEntries() {
  const entries = state.oigProfile && Array.isArray(state.oigProfile.entries) ? state.oigProfile.entries : [];
  return entries.filter((entry) => !entry.secret);
}

function oigProfileSecretEntries() {
  const entries = state.oigProfile && Array.isArray(state.oigProfile.entries) ? state.oigProfile.entries : [];
  return entries.filter((entry) => entry.secret);
}

function oigProfileFieldValue(entry) {
  if (!entry || !entry.key) return "";
  if (Object.prototype.hasOwnProperty.call(state.oigProfileEdits, entry.key)) {
    return state.oigProfileEdits[entry.key];
  }
  return entry.suggestedValue || entry.value || "";
}

function oigProfileFieldHelp(entry) {
  if (!entry) return "";
  const source = entry.suggestedSource ? `Source: ${entry.suggestedSource}` : "";
  if (entry.suggestedValue && entry.value && entry.suggestedValue !== entry.value) {
    return `${source || "Suggested value"}; current profile value is ${entry.value}`;
  }
  return source;
}

function oigSecretStatus(entry) {
  if (entry.runtimePrompt) return "Runtime prompt";
  if (entry.filled) return "Looks filled";
  return "Needs value";
}

function oigScriptStatusText() {
  const labels = {
    idle: "Not run",
    running: "Running",
    succeeded: "Completed",
    manual: "Confirmed manually",
    failed: "Failed"
  };
  return labels[state.oigScriptStatus] || state.oigScriptStatus || "Not run";
}

function hydrateOigProfileEdits(profile) {
  const edits = {};
  const entries = profile && Array.isArray(profile.entries) ? profile.entries : [];
  entries.forEach((entry) => {
    if (!entry.secret && entry.key) edits[entry.key] = entry.value || entry.suggestedValue || "";
  });
  state.oigProfileEdits = edits;
}

function spbPhaseLabel(phase) {
  const labels = { prestop: "PreStop", downtime: "Downtime", poststart: "PostStart" };
  return labels[phase] || phase;
}

function spbPhaseStatusText(phase) {
  const status = effectiveSpbPhaseStatus(phase);
  const labels = {
    idle: "Not started",
    running: "Running",
    succeeded: "Completed",
    failed: "Failed",
    accepted: "Accepted outside PatchPilot"
  };
  return labels[status] || status;
}

function spbPhaseIsRunning(phase) {
  const job = state.spbPhaseJobs[phase] || {};
  return state.spbPhaseStatus[phase] === "running" || job.status === "running";
}

function effectiveSpbPhaseStatus(phase) {
  if (phase === "prestop" && state.spbPrestopExternalApproved && !state.spbPrestopDone) return "accepted";
  if (phase === "prestop" && state.spbPrestopDone) return "succeeded";
  if (phase === "downtime" && state.spbDowntimeDone) return "succeeded";
  if (phase === "poststart" && state.spbPhaseStatus.poststart === "succeeded") return "succeeded";
  return state.spbPhaseStatus[phase] || "idle";
}

function renderSpbPhaseStatus(phase) {
  const status = effectiveSpbPhaseStatus(phase);
  const logPath = state.spbPhaseLogs[phase] || "";
  const error = status === "failed" ? (state.spbPhaseErrors[phase] || "") : "";
  return `
    <div class="phase-status is-${escapeHtml(status)}">
      <div>
        <span>${escapeHtml(spbPhaseLabel(phase))} status</span>
        <strong>${escapeHtml(spbPhaseStatusText(phase))}</strong>
      </div>
      ${logPath ? `<p class="mono">${escapeHtml(logPath)}</p>` : `<p>PatchPilot will tail the phase log after the run starts.</p>`}
    </div>
    ${error ? `
      <div class="review-box is-danger">
        <strong>${escapeHtml(spbPhaseLabel(phase))} failure detail</strong>
        <p class="mono">${escapeHtml(error)}</p>
      </div>
    ` : ""}
  `;
}

function commandPreview(options = {}) {
  const home = selectedHome();
  const analysis = state.readmeAnalysis || analyzeReadme(state.readmeText);
  const patchApplyDir = state.patchResult && state.patchResult.applyDir ? state.patchResult.applyDir : state.form.patchPath;
  const debugArgs = typeof options.debug === "boolean" ? (options.debug ? " -verbose -debug" : "") : opatchDebugArgsText();
  if (isStackPatchBundle()) {
    const inactivePatchCommands = [
      "Stop-for-backup gate must pass before file-system tar backups",
      "Backup gate must be complete before inactive patch cleanup, SPBAT setup, OPatch validation, or SPBAT phases",
      "Inactive patch review after stopped-home backup, before SPBAT setup, OPatch validation, Services Up, and SPBAT PreStop",
      spbInactiveListCommand(),
      "Run deleteinactivepatches after stopped-home file-system/database backup confirmation and before SPBAT PreStop.",
      spbInactiveRetainCommand(),
      spbInactiveDeleteCommand(),
      spbInactiveCleanupCommand(),
      "SPBAT setup verifies the bundle layout and log directory",
      `${home.oracleHome}/OPatch/opatch version`,
      "OPatch validation/upgrade gate must pass before Services Up and SPBAT phases"
    ];
    const commands = state.spbFreshInstallNoDomain ? [
      ...inactivePatchCommands,
      "Services Up gate not applicable: fresh install before domain creation",
      spbatCommand("prestop"),
      spbatStatusCommand(),
      "Shutdown and Backup gates must be complete before SPBAT Downtime",
      spbatCommand("downtime"),
      spbatCommand("poststart")
    ] : [
      ...inactivePatchCommands,
      "Services Up gate must pass before SPBAT PreStop",
      spbatCommand("prestop"),
      spbatStatusCommand(),
      "Shutdown and Backup gates must be complete before SPBAT Downtime",
      spbatCommand("downtime"),
      spbatCommand("poststart")
    ];
    if (spbInstallType() === "oig") commands.push(oigScriptCommand());
    return commands;
  }
  if (isBatchNapply()) {
    return [
      `cd ${state.form.patchPath}`,
      opatchHeapExportCommand(),
      `${home.oracleHome}/OPatch/opatch version`,
      "Run CheckConflictAgainstOHWithDetail for every selected patch directory",
      batchNapplyCommand(home, debugArgs),
      `${home.oracleHome}/OPatch/opatch lsinventory`
    ];
  }
  return [
    `cd ${patchApplyDir}`,
    opatchHeapExportCommand(),
    `${home.oracleHome}/OPatch/opatch version`,
    `${home.oracleHome}/OPatch/opatch prereq CheckConflictAgainstOHWithDetail -ph ./${debugArgs}`,
    `${home.oracleHome}/OPatch/opatch apply -silent${debugArgs}`,
    `${home.oracleHome}/OPatch/opatch lsinventory`,
    analysis.rollbackCommand.replace("$ORACLE_HOME", home.oracleHome)
  ];
}

function stepStatus(stepId) {
  if (state.failed.has(stepId)) return "failed";
  if (currentStep().id === stepId) return "active";
  if (state.completed.has(stepId)) return "complete";
  return "pending";
}

function canContinue() {
  const id = currentStep().id;
  if (id === "connect") return state.connected;
  if (id === "discover") return state.discovered && discoveredHomes.length > 0;
  if (id === "readme") return Boolean(state.readmeAnalysis);
  if (id === "spbBackupShutdown") return shutdownGateSatisfied("spbBackupShutdown");
  if (id === "spbUp") return state.servicesUpVerified || state.servicesUpManualAccepted || state.spbFreshInstallNoDomain;
  if (id === "spbPrepare") return state.spbPrepared;
  if (id === "spbPrestop") return state.spbPrestopDone || state.spbPrestopExternalApproved;
  if (id === "shutdown") return shutdownGateSatisfied("shutdown");
  if (id === "backup") return backupShutdownComplete() && backupGateComplete();
  if (id === "opatch") return state.opatchReady || syncOpatchReadyFromDiscoveredVersion();
  if (id === "spbInactive") return spbInactiveEarlyComplete();
  if (id === "spbDowntime") return state.spbDowntimeDone;
  if (id === "apply") return state.patchApplied || state.rollbackDone;
  if (id === "postinstall") return state.postinstallDone;
  if (id === "restart") return state.restartDone;
  if (id === "spbPoststart") return state.spbPoststartDone;
  return true;
}

function secondaryActionLabel(stepId = currentStep().id) {
  const labels = {
    connect: "Test SSH + Path",
    discover: "Discover Homes",
    readme: "Load README",
    spbUp: state.spbFreshInstallNoDomain ? "Accept Fresh Install" : "Verify Services Up",
    spbPrepare: "Prepare SPBAT",
    spbBackupShutdown: state.backupShutdownCheck ? "Recheck Shutdown" : "Verify Shutdown",
    shutdown: state.shutdownCheck ? "Recheck Shutdown" : "Verify Shutdown",
    backup: state.backupPreflightConfirmed ? "Take Backup" : "Check Backup Space",
    opatch: opatchMeetsReadmeMinimum() ? "OPatch Ready" : isStackPatchBundle() ? "Upgrade / Validate OPatch" : "Validate OPatch",
    spbInactive: spbInactiveDeleteReady() ? (dom.dryRunToggle && dom.dryRunToggle.checked ? "Preview Inactive Cleanup" : "Remove Inactive Patches") : state.spbInactiveCheck ? "Recheck Inactive Patches" : "Check Inactive Patches",
    spbPrestop: state.spbFreshInstallNoDomain ? "Run PreStop Baseline" : "Run PreStop",
    spbDowntime: "Run Downtime",
    apply: dom.dryRunToggle.checked ? `Run ${patchApplyCommandLabel()} Dry-run` : isBatchNapply() ? "Apply Batch" : "Apply Patch",
    postinstall: isStackPatchBundle() ? state.postinstallDone ? "Cleanup Complete" : spbCleanupDryRunEnabled() ? "Preview Tmp/Cache Cleanup" : "Clear Tmp/Cache" : "Confirm Postinstall",
    restart: state.restartDone ? "Services Started" : "Confirm Services Started",
    spbPoststart: state.spbPoststartDone ? "PostStart Complete" : spbInstallType() === "oig" && state.spbPhaseStatus.poststart === "succeeded" && !state.oigScriptDone ? "Run OIG Script" : "Run PostStart",
    report: state.report ? "Download HTML Report" : "Build HTML Report"
  };
  return labels[stepId] || "Run Check";
}

function stepActionBlockReason(stepId = currentStep().id) {
  const home = selectedHome();
  const hasHome = Boolean(home.oracleHome);
  if (hasHome) syncOpatchReadyFromDiscoveredVersion();
  if (stepId === "connect") return "";
  if (stepId === "discover" && !state.connected) return "Test SSH and validate the patch directory before discovering Oracle homes.";
  if (stepId === "readme" && !state.connected) return "Test SSH and validate the patch directory before loading the README from the patch location.";
  if (stepId === "spbBackupShutdown" && (!state.readmeAnalysis || !hasHome)) return "Load the SPB README and select the target Oracle home before stopping services for backup.";
  if (stepId === "spbInactive" && (!state.readmeAnalysis || !hasHome)) return "Load the SPB README and select the target Oracle home before reviewing inactive patches.";
  if (stepId === "spbInactive" && isStackPatchBundle() && !backupShutdownComplete()) return "Complete Stop for Backup before inactive patch review so cleanup happens only after stopped-home backups are in place.";
  if (stepId === "spbInactive" && isStackPatchBundle() && !backupGateComplete()) return backupGateBlockReason();
  if (stepId === "spbPrepare" && (!state.readmeAnalysis || !hasHome)) return "Load the SPB README and select the target Oracle home before preparing SPBAT.";
  if (stepId === "spbPrepare" && isStackPatchBundle() && !shutdownGateSatisfied("spbBackupShutdown")) return "Complete Stop for Backup before SPB Setup so file-system backups are taken from a stopped home.";
  if (stepId === "spbPrepare" && isStackPatchBundle() && !backupGateComplete()) return backupGateBlockReason();
  if (stepId === "spbPrepare" && isStackPatchBundle() && !spbInactiveEarlyComplete()) return "Complete inactive patch review before SPB Setup. Remove inactive patches or explicitly keep them for this run.";
  if (stepId === "opatch" && (!state.readmeAnalysis || !hasHome)) return "Load the README and select the target Oracle home before validating OPatch.";
  if (stepId === "opatch" && isStackPatchBundle() && !state.spbPrepared) return "Prepare SPBAT before validating OPatch for this SPB run.";
  if (stepId === "spbUp" && (!state.discovered || !hasHome)) return "Discover and select the target Oracle home before verifying services.";
  if (stepId === "spbUp" && isStackPatchBundle() && !spbInactiveEarlyComplete()) return "Complete inactive patch review before verifying services for PreStop.";
  if (stepId === "spbUp" && isStackPatchBundle() && (!state.spbPrepared || !state.opatchReady)) return "Complete SPB Setup and OPatch validation before verifying services for PreStop.";
  if (stepId === "spbPrestop" && state.spbFreshInstallNoDomain && (!state.spbPrepared || !state.opatchReady)) return "Complete SPB Setup and OPatch before running SPBAT PreStop baseline.";
  if (stepId === "spbPrestop" && !state.spbFreshInstallNoDomain && (!(state.servicesUpVerified || state.servicesUpManualAccepted) || !state.spbPrepared || !state.opatchReady)) return "Complete Services Up, SPB Setup, and OPatch before running SPBAT PreStop.";
  if (stepId === "spbPrestop" && isStackPatchBundle() && !spbInactiveEarlyComplete()) return "Check inactive patches after stopped-home backup confirmation and before SPBAT PreStop. Remove them or explicitly keep them for this run.";
  if (stepId === "shutdown" && isStackPatchBundle() && !(state.spbPrestopDone || state.spbPrestopExternalApproved)) return "Run or confirm SPBAT PreStop before stopping services.";
  if (stepId === "shutdown" && !isStackPatchBundle() && (!state.readmeAnalysis || !hasHome)) return "Load the README and select the target Oracle home before shutdown verification.";
  if (stepId === "backup" && !hasHome) return "Discover and select the target Oracle home before checking backup space.";
  if (stepId === "backup" && !backupShutdownComplete()) return "Verify selected-home services are stopped before creating file-system tar backups.";
  if (stepId === "spbDowntime" && isStackPatchBundle() && !(state.spbPrestopDone || state.spbPrestopExternalApproved)) return "Run or confirm SPBAT PreStop before running SPBAT Downtime.";
  if (stepId === "spbDowntime" && (!shutdownGateSatisfied("shutdown") || !backupGateComplete())) return `Complete shutdown and backup gates before running SPBAT Downtime. ${backupGateBlockReason()}`.trim();
  if (stepId === "spbDowntime" && isStackPatchBundle() && !spbInactiveDowntimeReady()) return "Check inactive patches and either remove them or explicitly keep them before SPBAT Downtime.";
  if (stepId === "apply" && !shutdownGateSatisfied("shutdown")) return "Verify shutdown or explicitly accept the shutdown gate before applying the patch.";
  if (stepId === "apply" && (!state.opatchReady || !backupGateComplete())) return `Validate OPatch and complete the backup gate before applying the patch. ${backupGateBlockReason()}`.trim();
  if (stepId === "apply" && isBatchNapply() && state.form.batchSelection === "ids" && !String(state.form.batchPatchIds || "").trim()) return "Enter the patch IDs to apply with OPatch napply.";
  if (stepId === "apply" && isBatchNapply() && state.form.batchSelection === "file" && !String(state.form.patchListFile || "").trim()) return "Enter the server-side patch-list file for OPatch napply.";
  if (stepId === "postinstall" && isStackPatchBundle() && !state.spbDowntimeDone) return "Run SPBAT Downtime before confirming pre-start cleanup.";
  if (stepId === "postinstall" && !isStackPatchBundle() && !state.patchApplied) return "Apply the patch before running postinstall.";
  if (stepId === "restart" && isStackPatchBundle() && !state.postinstallDone) return "Complete the tmp/cache cleanup before starting services.";
  if (stepId === "restart" && !isStackPatchBundle() && !state.postinstallDone) return "Complete postinstall before starting services.";
  if (stepId === "spbPoststart" && !state.restartDone) return "Start services before running SPBAT PostStart.";
  return "";
}

function renderRail() {
  const steps = workflowSteps();
  dom.wizardSteps.innerHTML = steps.map((step, index) => {
    const status = stepStatus(step.id);
    return `
      <button class="wizard-step ${status === "active" ? "is-active" : ""} ${status === "complete" ? "is-complete" : ""} ${status === "failed" ? "is-failed" : ""}" data-step-index="${index}" type="button">
        <span>${index + 1}</span>
        <strong>${escapeHtml(step.label)}</strong>
        <small>${escapeHtml(status)}</small>
      </button>
    `;
  }).join("");
}

function renderControls() {
  const steps = workflowSteps();
  const active = currentStep();
  const showMonitor = shouldShowMonitor(active.id);
  const actionBlockReason = stepActionBlockReason(active.id);
  const opatchAlreadySatisfied = active.id === "opatch" && opatchMeetsReadmeMinimum();
  const completedReadOnly = state.completed.has(active.id) && ["spbPrestop", "spbDowntime", "spbPoststart", "postinstall", "apply", "backup"].includes(active.id);
  if (dom.stepTitle) dom.stepTitle.textContent = active.title;
  if (dom.stepHint) dom.stepHint.textContent = active.hint;
  dom.wizardLayout.classList.toggle("is-monitor-hidden", !showMonitor);
  dom.monitorPanel.classList.toggle("is-hidden", !showMonitor);
  dom.backButton.disabled = state.activeStep === 0 || state.running;
  const isReportStep = active.id === "report";
  const reportBuilt = Boolean(state.report);
  dom.nextButton.disabled = state.running || (!isReportStep && (!canContinue() || state.activeStep === steps.length - 1)) || (isReportStep && !reportBuilt);
  dom.nextButton.textContent = isReportStep ? "Start Another Patch" : state.activeStep === steps.length - 2 ? "Finish" : "Continue";
  dom.secondaryActionButton.disabled = state.running || Boolean(actionBlockReason) || opatchAlreadySatisfied || completedReadOnly;
  dom.secondaryActionButton.title = completedReadOnly
    ? "This step is complete. Review its result or continue; completed commands are not rerun from the result view."
    : opatchAlreadySatisfied
      ? "Current OPatch already meets the README minimum. Continue to the next step."
      : actionBlockReason;
  dom.downloadReportButton.disabled = !(state.report && state.report.outcome === currentReportOutcome() && !state.rollbackInProgress);
  dom.rollbackButton.disabled = state.running || !rollbackAvailableForCurrentRun();
  if (dom.autoRollbackToggle) {
    const batchMode = isBatchNapply();
    if (batchMode) dom.autoRollbackToggle.checked = false;
    dom.autoRollbackToggle.disabled = state.running || batchMode;
    dom.autoRollbackToggle.title = batchMode
      ? "Batch napply rollback requires a reviewed patch-by-patch recovery plan."
      : "Rollback on conflict/failure";
  }

  const id = active.id;
  dom.secondaryActionButton.textContent = secondaryActionLabel(id);
}

function rollbackAvailableForCurrentRun() {
  if (isStackPatchBundle()) {
    return state.spbDowntimeDone || state.spbPoststartDone || state.rollbackDone || state.failed.has("spbDowntime") || state.failed.has("spbPoststart");
  }
  if (isBatchNapply()) return false;
  return state.patchApplied || state.failed.size > 0;
}

function shouldShowMonitor(stepId) {
  return ["opatch", "spbInactive", "spbPrepare", "spbUp", "spbPrestop", "spbBackupShutdown", "shutdown", "backup", "spbDowntime", "postinstall", "restart", "spbPoststart", "apply", "report"].includes(stepId);
}

function render() {
  renderRail();
  renderStep();
  renderControls();
}

function renderStep() {
  const id = currentStep().id;
  const renderers = {
    connect: renderConnectStep,
    discover: renderDiscoverStep,
    readme: renderReadmeStep,
    spbBackupShutdown: renderShutdownStep,
    spbUp: renderSpbUpStep,
    spbPrepare: renderSpbPrepareStep,
    spbPrestop: renderSpbPrestopStep,
    shutdown: renderShutdownStep,
    backup: renderBackupStep,
    opatch: renderOpatchStep,
    spbInactive: renderSpbInactiveStep,
    spbDowntime: renderSpbDowntimeStep,
    apply: renderApplyStep,
    postinstall: renderPostinstallStep,
    restart: renderRestartStep,
    spbPoststart: renderSpbPoststartStep,
    report: renderReportStep
  };
  const actionBlockReason = stepActionBlockReason(id);
  const previewNotice = actionBlockReason ? `
    <div class="review-box is-info preview-notice">
      <strong>Preview mode</strong>
      <p>You can review this step now. Complete the required earlier gates before running ${escapeHtml(secondaryActionLabel(id))}: ${escapeHtml(actionBlockReason)}</p>
    </div>
  ` : "";
  dom.stepContent.innerHTML = `${previewNotice}${renderers[id]()}`;
  bindStepEvents(id);
}

function renderConnectStep() {
  const result = state.connectionResult;
  const resultTone = result ? result.tone === "danger" ? "is-danger" : result.tone === "good" ? "is-good" : "is-info" : "";
  const resultDetails = result && Array.isArray(result.details) ? result.details.filter((item) => item && item.label) : [];
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Server Access</p>
      <h2>SSH and Patch Source</h2>
    </div>
    ${state.integrationContext ? `
      <div class="review-box is-info integration-context">
        <strong>Opened from FMW Monitoring Dashboard</strong>
        <p>Target details were prefilled from ${escapeHtml(state.integrationContext.customer || "the selected environment")}. Enter the SSH password, then connect so PatchPilot can validate the server and patch directory independently.</p>
      </div>
    ` : ""}
    <div class="form-grid">
      <label class="field"><span>SSH Host or IP</span><input id="hostInput" value="${escapeHtml(state.form.host)}" placeholder="Example: appserver01.example.com or 192.0.2.10"></label>
      <label class="field"><span>SSH Port</span><input id="portInput" value="${escapeHtml(state.form.port)}"></label>
      <label class="field"><span>SSH User</span><input id="userInput" value="${escapeHtml(state.form.user)}"></label>
      <label class="field"><span>SSH Password</span><input id="passwordInput" class="secret-input" type="password" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-lpignore="true" data-1p-ignore="true" data-form-type="other" placeholder="Not saved" value="${escapeHtml(state.form.password)}"></label>
      <label class="field"><span>Oracle Software Owner / Run Commands As</span><input id="runAsUserInput" value="${escapeHtml(state.form.runAsUser)}" placeholder="oracle"><small>Patch commands are verified and executed as this operating-system user.</small></label>
      <label class="field"><span>Sudo Authentication</span>
        <select id="sudoAuthSelect">
          <option value="ssh-password" ${state.form.sudoAuth === "ssh-password" ? "selected" : ""}>Use SSH password</option>
          <option value="separate" ${state.form.sudoAuth === "separate" ? "selected" : ""}>Use separate sudo password</option>
          <option value="passwordless" ${state.form.sudoAuth === "passwordless" ? "selected" : ""}>Passwordless sudo</option>
        </select>
        <small>Used only when the SSH user differs from the Oracle software owner. Passwords are not saved.</small>
      </label>
      <label class="field"><span>Sudo Password</span><input id="sudoPasswordInput" class="secret-input" type="password" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-lpignore="true" data-1p-ignore="true" data-form-type="other" placeholder="Not saved" value="${escapeHtml(state.form.sudoPassword)}" ${state.form.sudoAuth === "separate" ? "" : "disabled"}><small>${state.form.sudoAuth === "ssh-password" ? "The SSH password will also be used for sudo." : state.form.sudoAuth === "passwordless" ? "No sudo password will be sent." : "Enter the password required by sudo."}</small></label>
      <label class="field"><span>Patch Type</span>
        <select id="patchTypeSelect">
          ${["One-off", "PSU", "SPU", "CSPU", "BSU", "Bundle Patch", "Stack Patch Bundle"].map((type) => `<option ${type === state.form.patchType ? "selected" : ""}>${escapeHtml(type)}</option>`).join("")}
        </select>
      </label>
      ${state.form.patchType === "One-off" ? `
        <label class="field"><span>Application Mode</span>
          <select id="applyModeSelect">
            <option value="single" ${state.form.applyMode === "single" ? "selected" : ""}>Single patch - OPatch apply</option>
            <option value="batch" ${state.form.applyMode === "batch" ? "selected" : ""}>Batch one-offs - OPatch napply</option>
          </select>
          <small>Batch mode validates every selected one-off and verifies all patch IDs after napply.</small>
        </label>
      ` : ""}
      ${state.form.patchType === "One-off" && state.form.applyMode === "batch" ? `
        <label class="field"><span>Batch Selection</span>
          <select id="batchSelectionSelect">
            <option value="all" ${state.form.batchSelection === "all" ? "selected" : ""}>All discovered patch directories</option>
            <option value="ids" ${state.form.batchSelection === "ids" ? "selected" : ""}>Selected patch IDs</option>
            <option value="file" ${state.form.batchSelection === "file" ? "selected" : ""}>OPatch patch-list file</option>
          </select>
        </label>
        ${state.form.batchSelection === "ids" ? `<label class="field"><span>Patch IDs</span><input id="batchPatchIdsInput" value="${escapeHtml(state.form.batchPatchIds)}" placeholder="39100536, 39951765, 39987730"><small>Comma- or space-separated numeric patch IDs found below the patch directory.</small></label>` : ""}
        ${state.form.batchSelection === "file" ? `<label class="field wide"><span>Patch-list File on Target Server</span><input id="patchListFileInput" value="${escapeHtml(state.form.patchListFile)}" placeholder="/u01/stage/OHS/binary_patches/linux64_patchlist.txt"><small>PatchPilot validates every listed directory before passing this file to OPatch -phBaseFile.</small></label>` : ""}
      ` : ""}
      <label class="field wide"><span>Patch Directory on Target Server</span><input id="patchPathInput" value="${escapeHtml(state.form.patchPath)}" placeholder="Example: /u01/stage/IDM_SPB_14.1.2.1.VERSION"></label>
    </div>
    ${result ? `
      <div class="review-box connect-result ${resultTone}">
        <strong>${escapeHtml(result.title)}</strong>
        <p>${escapeHtml(result.message)}</p>
        ${resultDetails.length ? `
          <div class="detail-grid connect-validation-grid">
            ${resultDetails.map((item) => `
              <div>
                <span>${escapeHtml(item.label)}</span>
                <strong class="${item.mono ? "mono" : ""}">${escapeHtml(item.value)}</strong>
              </div>
            `).join("")}
          </div>
        ` : ""}
      </div>
    ` : ""}
  `;
}

function renderDiscoverStep() {
  const emptyMessage = state.discoveryError
    ? `<strong>Discovery failed</strong><p>${escapeHtml(state.discoveryError)}</p>`
    : state.discovered
      ? `No Oracle homes were discovered from ${escapeHtml(state.form.user)}@${escapeHtml(state.form.host)}. Check SSH access and ORACLE_HOME layout.`
      : "Discovery has not run yet.";
  const cards = state.discovered && discoveredHomes.length ? discoveredHomes.map((home) => `
    <label class="select-card ${home.id === state.selectedHomeId ? "is-selected" : ""}">
      <input type="radio" name="homeChoice" value="${escapeHtml(home.id)}" ${home.id === state.selectedHomeId ? "checked" : ""}>
      <span>
        <strong>${escapeHtml(home.label)}</strong>
        <small>${escapeHtml(home.oracleHome)}</small>
        ${home.discoveredHome && home.discoveredHome !== home.oracleHome ? `<small>Discovered as ${escapeHtml(home.discoveredHome)}</small>` : ""}
        <small>${escapeHtml(home.product)} | OPatch ${escapeHtml(home.opatchVersion)}</small>
      </span>
    </label>
  `).join("") : `<div class="empty-state ${state.discoveryError ? "is-danger" : ""}">${emptyMessage}</div>`;

  const home = selectedHome();
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Inventory</p>
      <h2>Select Target Oracle Home</h2>
    </div>
    <div class="option-list">${cards}</div>
    ${state.discovered && discoveredHomes.length ? `
      <div class="detail-grid">
        <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(home.oracleHome)}</strong></div>
        <div><span>DOMAIN_HOME</span><strong class="mono">${escapeHtml(home.domainHome || (isStackPatchBundle() ? "Not discovered - OK for fresh install before domain creation" : "Not discovered"))}</strong></div>
        <div><span>INSTANCE_HOME</span><strong class="mono">${escapeHtml(home.instanceHome || "Not applicable")}</strong></div>
        <div><span>Running Services</span><strong>${escapeHtml(home.services.join(", "))}</strong></div>
      </div>
    ` : ""}
  `;
}

function renderReadmeStep() {
  const analysis = state.readmeAnalysis;
  const source = state.readmePath
    ? `${state.form.user}@${state.form.host}:${state.readmePath}`
    : `${state.form.user}@${state.form.host}:${state.form.patchPath}`;
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Patch Instructions</p>
      <h2>README Analysis</h2>
    </div>
    <div class="review-box ${state.readmePath ? "is-good" : ""}">
      <strong>${state.readmePath ? "README loaded from SSH patch location" : "README will be loaded from SSH patch location"}</strong>
      <p class="mono">${escapeHtml(source)}</p>
    </div>
    <div class="readme-grid">
      <div>
        <label class="field readme-text-field">
          <span>Patch README Text</span>
          <textarea id="readmeInput" rows="18" wrap="soft" placeholder="Click Load README to read this from the patch directory on the SSH target.">${escapeHtml(state.readmeText)}</textarea>
        </label>
      </div>
      <div class="analysis-panel">
        ${analysis ? renderAnalysis(analysis) : `<div class="empty-state">README has not been analyzed.</div>`}
      </div>
    </div>
  `;
}

function renderAnalysis(analysis) {
  const spb = isStackPatchBundle();
  const autoType = suggestedSpbTypes()[0] || "oam";
  const generatedCommands = spb
    ? [
        ["PreStop", "prestop", spbatCommand("prestop")],
        ["Status Report", "status", spbatStatusCommand()],
        ["Downtime", "downtime", spbatCommand("downtime")],
        ["PostStart", "poststart", spbatCommand("poststart")]
      ]
    : [
        ["Conflict Check", "", analysis.conflictCommand],
        ["Patch Apply", "", analysis.applyCommand],
        ["Rollback", "", analysis.rollbackCommand]
      ];
  return `
    <div class="analysis-list readme-summary-list">
      <div><span>Detected Method</span><strong>${escapeHtml(analysis.detectedPatchFamily || (isStackPatchBundle() ? "SPBAT / Stack Patch Bundle" : "OPatch"))}</strong></div>
      ${spb ? `<div><span>SPBAT Type</span><strong>${escapeHtml(spbInstallTypeLabel())}</strong></div>` : ""}
      <div><span>Minimum OPatch</span><strong>${escapeHtml(analysis.minimumOpatch)}</strong></div>
      <div><span>Backup Scope</span><strong>${escapeHtml(analysis.backupTargets.join(", "))}</strong></div>
    </div>
    ${spb ? `
      <div class="spb-inline-settings">
        <label class="field">
          <span>SPBAT Install Type</span>
          <select id="spbInstallTypeSelect">${spbTypeOptionsMarkup(autoType)}</select>
        </label>
        <label class="field">
          <span>Additional SPBAT Arguments</span>
          <input id="spbExtraArgsInput" value="${escapeHtml(spbExtraArgs())}" placeholder="-debug true">
        </label>
        <label class="field">
          <span>OPatch JVM Heap Options</span>
          <input id="opatchHeapInput" value="${escapeHtml(opatchHeapOptions())}" placeholder="-Xmx3072m">
        </label>
        <div class="detail-grid spb-inline-summary">
          <div><span>Resolved SPBAT Type</span><strong id="spbTypeSummary">${escapeHtml(spbInstallType().toUpperCase())}</strong></div>
          <div><span>Extra Arguments</span><strong id="spbExtraArgsSummary" class="mono">${escapeHtml(spbExtraArgs() || "None")}</strong></div>
          <div><span>OPatch Heap</span><strong id="opatchHeapSummary" class="mono">${escapeHtml(opatchHeapOptions())}</strong></div>
        </div>
      </div>
    ` : ""}
    <div class="readme-command-panel">
      <h3>${spb ? "Generated SPBAT Commands" : "Generated OPatch Commands"}</h3>
      <div class="readme-command-list">
        ${generatedCommands.map(([label, phase, command]) => `
          <div class="readme-command-row">
            <span>${escapeHtml(label)}</span>
            <code ${spb && phase ? `data-spb-command-phase="${escapeHtml(phase)}"` : ""}>${escapeHtml(command)}</code>
          </div>
        `).join("")}
      </div>
    </div>
    ${spb ? `
      ${analysis.spbatSteps.length ? `
        <h3>README SPBAT Notes</h3>
        <ul class="compact-list">${analysis.spbatSteps.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
      ` : ""}
      <div class="review-box is-warning readme-warning">
        <strong>SPBAT rollback/backout safety</strong>
        <p>Downtime should only be run after ORACLE_HOME, DOMAIN_HOME, and required INSTANCE_HOME backups are confirmed. The SPBAT README says the utility does not provide automated rollback support; backups are the recovery path if Downtime/PostStart has to be backed out.</p>
      </div>
      ${analysis.inactivePatchCleanupRecommended ? `
        <div class="review-box is-warning readme-warning">
          <strong>Inactive patch cleanup can improve SPBAT runtime</strong>
          <p>README section 4.6 recommends checking inactive patches because they can slow SPBAT. PatchPilot places this after services are stopped and backups are confirmed, but before SPBAT setup, formal OPatch validation, Services Up, and PreStop, so cleanup does not happen without a recovery point.</p>
        </div>
      ` : ""}
      ${analysis.javaPrereqRecommended ? `
        <div class="review-box is-info readme-warning">
          <strong>Java prerequisite needs operator review</strong>
          <p>The SPB README lists a minimum Java/JDK update level. PatchPilot highlights this prerequisite, but it does not upgrade Java automatically.</p>
        </div>
      ` : ""}
      ${analysis.opatchHeapRecommended ? `
        <div class="review-box is-info readme-warning">
          <strong>OPatch heap setting noted</strong>
          <p>The SPB README suggests setting OPATCH_JRE_MEMORY_OPTIONS before OPatch activity. PatchPilot applies the configured value to OPatch and SPBAT commands.</p>
        </div>
      ` : ""}
    ` : ""}
    <h3>Stop Notes</h3>
    <ul class="compact-list">${analysis.stopSteps.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    <h3>Postinstall Notes</h3>
    ${analysis.postinstallRequiresManual ? `
      <div class="review-box is-warning readme-warning">
        <strong>Manual postinstall action required</strong>
        <p>PatchPilot found postinstall steps that require credentials or product-specific operator action. These must be completed or explicitly confirmed before continuing.</p>
      </div>
    ` : ""}
    ${renderInstructionCards(analysis.postinstallSteps, analysis.postinstallManualSteps, "Postinstall")}
  `;
}

function renderInstructionCards(items, manualFlags = [], label = "Step", completed = false) {
  return `
    <div class="instruction-list">
      ${items.map((item, index) => {
        const manual = Boolean(manualFlags[index]);
        return `
          <article class="instruction-card ${manual ? "is-manual" : ""}">
            <div class="instruction-card-heading">
              <strong>${escapeHtml(label)} ${index + 1}</strong>
              ${manual ? `<span class="small-pill manual-pill">Manual</span>` : ""}
            </div>
            <pre class="instruction-body">${escapeHtml(item)}</pre>
            ${completed ? `<span class="state-dot is-pass" title="Confirmed"></span>` : ""}
          </article>
        `;
      }).join("")}
    </div>
  `;
}

function serviceSummary(item) {
  if (typeof item === "string") {
    return {
      key: item,
      service: item,
      category: "Discovered Service",
      matchedScopes: [],
      processCount: 0,
      shutdownHint: ""
    };
  }
  return {
    key: item.key || item.service,
    service: item.service || "Oracle service",
    category: item.category || "Oracle Process",
    matchedScopes: Array.isArray(item.matchedScopes) ? item.matchedScopes : [],
    processCount: item.processCount || 0,
    pids: Array.isArray(item.pids) ? item.pids : [],
    shutdownHint: item.shutdownHint || "Use the product stop script for the selected domain/home."
  };
}

function reportServiceInventory(report) {
  const services = report && report.selectedHome && Array.isArray(report.selectedHome.services) ? report.selectedHome.services : [];
  const names = uniqueValues(services.map((item) => serviceSummary(item).service));
  return names.length ? names.join(", ") : "No service inventory captured";
}

function reportServiceValidation(report) {
  if (report.servicesUpVerified) return "Automated services-up verification passed";
  if (report.servicesUpManualAccepted) return "Services-up state accepted manually";
  if (report.restartDone && report.startupConfirmed) return "Startup confirmed by the operator";
  return "Not performed";
}

function shutdownGroup(service) {
  const category = String(service.category || "").toLowerCase();
  const name = String(service.service || "").toLowerCase();
  if (category.includes("node manager") || name.includes("nodemanager")) return "nodeManager";
  if (category.includes("weblogic") && name === "adminserver") return "adminServer";
  if (category.includes("weblogic")) return "managedServer";
  if (category.includes("oracle internet directory") || category.includes("oracle http server") || category.includes("oracle unified directory") || category.includes("derby")) return "systemComponent";
  return "other";
}

function shutdownOrderValue(service) {
  const order = {
    systemComponent: 10,
    managedServer: 20,
    adminServer: 30,
    nodeManager: 40,
    other: 50
  };
  return order[shutdownGroup(service)] || 50;
}

function shutdownGroupLabel(group) {
  return {
    systemComponent: "System components",
    managedServer: "Managed servers",
    adminServer: "AdminServer",
    nodeManager: "Node Manager",
    other: "Other selected services"
  }[group] || "Selected services";
}

function pathForHint(path, fallback) {
  return String(path || "").trim() || fallback;
}

function startupCommandHints(home, isSpb) {
  const productText = `${home.product || ""} ${(Array.isArray(home.services) ? home.services : []).join(" ")}`.toLowerCase();
  const domainHome = pathForHint(home.domainHome, "$DOMAIN_HOME");
  const oracleHome = pathForHint(home.oracleHome, "$ORACLE_HOME");
  const instanceHome = pathForHint(home.instanceHome, "$INSTANCE_HOME");
  const hints = [];
  if (isSpb || /weblogic|adminserver|managed|oam|oig|oim|soa/.test(productText)) {
    hints.push(`Node Manager: ${domainHome}/bin/startNodeManager.sh`);
    hints.push(`AdminServer: ${domainHome}/bin/startWebLogic.sh`);
    hints.push(`Managed servers: ${domainHome}/bin/startManagedWebLogic.sh <managed_server_name>`);
  }
  if (/oid|internet directory/.test(productText)) {
    hints.push(`OID components: ${oracleHome}/bin/opmnctl startall or ${domainHome}/bin/startComponent.sh <oid_component>`);
  }
  if (/oud|unified directory/.test(productText)) {
    hints.push(`OUD instance: ${instanceHome}/bin/start-ds`);
  }
  if (/ohs|http server/.test(productText)) {
    hints.push(`OHS component: ${domainHome}/bin/startComponent.sh <ohs_component>`);
  }
  return hints.length
    ? hints
    : [
      "Use the approved site startup script or product startup runbook",
      "Start AdminServer with startWebLogic.sh, not startManagedWebLogic.sh",
      "Start managed servers with startManagedWebLogic.sh <managed_server_name>"
    ];
}

function selectedShutdownServices(stepId = currentStep().id) {
  const meta = shutdownGateMeta(stepId);
  const check = state[meta.checkKey];
  const selections = state[meta.selectionsKey];
  if (!check || !Array.isArray(check.running)) return [];
  return check.running
    .map(serviceSummary)
    .filter((service) => selections.has(service.key))
    .sort((left, right) => shutdownOrderValue(left) - shutdownOrderValue(right) || left.service.localeCompare(right.service));
}

function renderShutdownRows(home, stepId = currentStep().id) {
  const meta = shutdownGateMeta(stepId);
  const check = state[meta.checkKey];
  const selections = state[meta.selectionsKey];
  if (check && Array.isArray(check.running) && check.running.length) {
    return check.running.map((item) => {
      const service = serviceSummary(item);
      const checked = selections.has(service.key) ? "checked" : "";
      const count = service.processCount > 1 ? `${service.processCount} matching processes` : "1 matching process";
      const scopes = service.matchedScopes.length ? service.matchedScopes.join(", ") : "Selected home";
      return `
        <article class="check-row check-row-danger">
          <label class="service-select-row">
            <input class="shutdown-service-input" type="checkbox" data-shutdown-key="${escapeHtml(service.key)}" ${checked}>
            <span>
              <strong>${escapeHtml(service.service)}</strong>
              <p class="check-copy">${escapeHtml(service.category)} | ${escapeHtml(scopes)} | ${escapeHtml(count)}</p>
              <p class="check-copy mono">${escapeHtml(service.shutdownHint)}</p>
            </span>
          </label>
          <span class="state-dot is-fail" title="Still running"></span>
        </article>
      `;
    }).join("");
  }

  if (check && check.status === "stopped") {
    return `
      <article class="check-row check-row-good">
        <div>
          <strong>No target-home services running</strong>
          <p class="check-copy">No process matched the selected ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME.</p>
        </div>
        <span class="state-dot is-pass" title="Stopped"></span>
      </article>
    `;
  }

  const discovered = Array.isArray(home.services) && home.services.length ? home.services.join(", ") : "";
  return `
    <div class="empty-state">
      Current shutdown verification has not run yet. Click Verify Shutdown to inspect the live process table for the selected ORACLE_HOME, DOMAIN_HOME, and INSTANCE_HOME.
      ${discovered ? `<br><span class="mono">Previously discovered service names: ${escapeHtml(discovered)}</span>` : ""}
    </div>
  `;
}

function renderSpbUpStep() {
  const home = selectedHome();
  const check = state.servicesUpCheck;
  const running = check && Array.isArray(check.running) ? check.running : [];
  const isChecking = state.servicesUpStatus === "checking";
  const expectedServices = Array.isArray(home.services) ? home.services.filter(Boolean) : [];
  const statusClass = state.spbFreshInstallNoDomain
    ? "is-good"
    : state.servicesUpVerified
      ? "is-good"
      : state.servicesUpManualAccepted
        ? "is-good"
      : isChecking
        ? "is-info"
        : state.servicesUpStatus === "failed" || state.servicesUpStatus === "missing"
          ? "is-danger"
          : running.length
            ? "is-warning"
            : "is-warning";
  const statusTitle = state.spbFreshInstallNoDomain
    ? "Fresh install path selected"
    : state.servicesUpVerified
      ? "Services verified up"
      : state.servicesUpManualAccepted
        ? "Services-up manually accepted"
      : isChecking
        ? "Checking live services"
        : state.servicesUpStatus === "failed"
          ? "Services-up check failed"
          : state.servicesUpStatus === "missing"
            ? "No selected-home services found"
            : running.length
              ? "Services found; confirm stack readiness"
              : "Verification required";
  const statusCopy = state.spbFreshInstallNoDomain
    ? "Domain is not created yet, so Services Up is not applicable. PatchPilot will not ask you to start services, but SPBAT PreStop still runs next to create the status logs required by Downtime."
    : state.servicesUpVerified
      ? "Selected-home services were found running, so SPBAT PreStop can be run safely."
      : state.servicesUpManualAccepted
        ? "The operator confirmed services are up even though PatchPilot could not match a live selected-home process. This decision will be captured in the report."
      : isChecking
        ? "PatchPilot is inspecting the SSH target process table for the selected ORACLE_HOME, DOMAIN_HOME, INSTANCE_HOME, and discovered service names."
        : state.servicesUpStatus === "failed"
          ? state.servicesUpError || "PatchPilot could not complete the Services Up verification."
          : state.servicesUpStatus === "missing"
            ? "PatchPilot did not find a running process matching the selected home or discovered service names. Start the required services, then verify again."
            : running.length
              ? "PatchPilot found running selected-home services. Confirm the full IDM stack is up before continuing."
              : "SPBAT PreStop must be run before shutdown, while IDM/WebLogic services are up.";

  return `
    <div class="step-heading">
      <p class="page-breadcrumb">SPBAT Gate</p>
      <h2>Confirm Services Are Up</h2>
    </div>
    <label class="toggle-row decision-row">
      <input id="servicesUpConfirmedInput" type="checkbox" ${state.servicesUpConfirmed ? "checked" : ""}>
      <span>Confirm all required IDM/WebLogic services are currently running for ${escapeHtml(home.oracleHome)}.</span>
    </label>
    <label class="toggle-row decision-row">
      <input id="spbFreshInstallInput" type="checkbox" ${state.spbFreshInstallNoDomain ? "checked" : ""}>
      <span>Fresh install before domain creation. Continue without starting services; still run SPBAT PreStop baseline.</span>
    </label>
    <div class="review-box ${statusClass}">
      <strong>${escapeHtml(statusTitle)}</strong>
      <p>${escapeHtml(statusCopy)}</p>
    </div>
    ${state.spbFreshInstallNoDomain && !home.domainHome ? `
      <div class="review-box is-info">
        <strong>DOMAIN_HOME is not required for this path</strong>
        <p>PatchPilot will use ORACLE_HOME for OPatch and SPBAT. DOMAIN_HOME backup, shutdown, cleanup, and startup checks are skipped until a domain exists. SPBAT PreStop still creates the status logs required by Downtime.</p>
      </div>
    ` : ""}
    <div class="service-checks">
      ${state.spbFreshInstallNoDomain ? `<div class="empty-state">Services Up verification is skipped for a fresh install where the domain has not been created yet.</div>` : isChecking ? `
        <div class="empty-state">Checking live process table on ${escapeHtml(state.form.host)}...</div>
      ` : running.length ? running.map((item) => {
        const service = serviceSummary(item);
        const scopes = service.matchedScopes.length ? service.matchedScopes.join(", ") : "Selected home";
        return `
          <article class="check-row check-row-good">
            <div>
              <strong>${escapeHtml(service.service)}</strong>
              <p class="check-copy">${escapeHtml(service.category)} | ${escapeHtml(scopes)} | ${escapeHtml(service.processCount)} matching process(es)</p>
            </div>
            <span class="state-dot is-pass" title="Running"></span>
          </article>
        `;
      }).join("") : check || state.servicesUpStatus === "missing" ? `
        <article class="check-row check-row-danger">
          <div>
            <strong>No matching running services found</strong>
            <p class="check-copy">PatchPilot checked ${escapeHtml(home.oracleHome || "the selected ORACLE_HOME")}${home.domainHome ? `, ${escapeHtml(home.domainHome)}` : ""}${home.instanceHome ? `, ${escapeHtml(home.instanceHome)}` : ""}${expectedServices.length ? ` and discovered services: ${escapeHtml(expectedServices.join(", "))}` : ""}.</p>
          </div>
          <span class="state-dot is-fail" title="Not found"></span>
        </article>
      ` : `<div class="empty-state">Click Verify Services Up to inspect running selected-home services on the SSH target.</div>`}
    </div>
    ${!state.spbFreshInstallNoDomain && !state.servicesUpVerified && !state.servicesUpManualAccepted && state.servicesUpConfirmed && ["missing", "failed"].includes(state.servicesUpStatus) ? `
      <div class="review-box is-warning">
        <strong>Manual Services Up confirmation available</strong>
        <p>Use this only when you have verified from the customer runbook, console, or process table that the required services for this selected home are running.</p>
        <button id="servicesUpManualButton" class="button button-secondary" type="button">Accept Manual Services Up</button>
      </div>
    ` : ""}
  `;
}

function renderSpbPrepareStep() {
  const suggested = suggestedSpbTypes();
  const autoType = suggested[0] || "oam";
  const selectedType = spbInstallType();
  const result = state.spbPrepareResult;
  const preparing = state.spbPrepareStatus === "running";
  const failed = state.spbPrepareStatus === "failed";
  const statusClass = state.spbPrepared ? "is-good" : failed ? "is-danger" : preparing ? "is-info" : "is-warning";
  const statusTitle = state.spbPrepared
    ? "SPBAT bundle verified"
    : failed
      ? "SPBAT prepare failed"
      : preparing
        ? "Preparing SPBAT"
        : "Prepare will verify SPBAT and create log directory";
  const statusCopy = state.spbPrepared
    ? `SPBAT is ready. Log directory: ${result && result.logDir ? result.logDir : spbLogDir()}`
    : failed
      ? state.spbPrepareError || "PatchPilot could not verify the SPBAT bundle or log directory."
      : preparing
        ? `Checking ${spbatDir()} on ${state.form.host} and preparing ${spbLogDir()}.`
        : spbatDir();
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">SPBAT Setup</p>
      <h2>Install Type and Log Directory</h2>
    </div>
    <div class="form-grid">
      <label class="field">
        <span>SPBAT Install Type</span>
        <select id="spbInstallTypeSelect">${spbTypeOptionsMarkup(autoType)}</select>
      </label>
      <label class="field wide">
        <span>SPBAT Log Directory</span>
        <input id="spbLogDirInput" value="${escapeHtml(spbLogDir())}">
      </label>
      <label class="field wide">
        <span>Additional SPBAT Arguments</span>
        <input id="spbExtraArgsInput" value="${escapeHtml(spbExtraArgs())}" placeholder="-debug true">
      </label>
      <label class="field wide">
        <span>OPatch JVM Heap Options</span>
        <input id="opatchHeapInput" value="${escapeHtml(opatchHeapOptions())}" placeholder="-Xmx3072m">
        <p class="field-help">README default is -Xmx3072m. Increase only when OPatch/SPBAT reports Java heap errors and the host has enough RAM.</p>
      </label>
      <div class="detail-grid spb-setup-summary">
        <div><span>Resolved SPBAT Type</span><strong id="spbTypeSummary">${escapeHtml(selectedType.toUpperCase())}</strong></div>
        <div><span>Extra Arguments</span><strong id="spbExtraArgsSummary" class="mono">${escapeHtml(spbExtraArgs() || "None")}</strong></div>
        <div><span>OPatch Heap</span><strong id="opatchHeapSummary" class="mono">${escapeHtml(opatchHeapOptions())}</strong></div>
      </div>
    </div>
    <div class="review-box ${statusClass}">
      <strong>${escapeHtml(statusTitle)}</strong>
      <p class="mono">${escapeHtml(statusCopy)}</p>
    </div>
    ${preparing ? `
      <div class="runtime-panel is-running">
        <div class="runtime-panel-header">
          <strong>SPBAT setup check running</strong>
          <span>SSH validation in progress</span>
        </div>
        <div class="runtime-meter"><span style="width: 35%"></span></div>
        <p class="runtime-basis">Validating patch directory, SPBAT script, log directory permissions, and OPatch candidate location.</p>
      </div>
    ` : ""}
    ${failed ? `
      <div class="review-box is-danger">
        <strong>Prepare failure detail</strong>
        <p class="mono">${escapeHtml(state.spbPrepareError)}</p>
      </div>
    ` : ""}
    ${result ? `
      <div class="detail-grid">
        <div><span>SPBAT Script</span><strong class="mono">${escapeHtml(result.spbatScript || "Not found")}</strong></div>
        <div><span>Log Directory</span><strong class="mono">${escapeHtml(result.logDir || spbLogDir())}</strong></div>
      </div>
    ` : ""}
  `;
}

function renderShutdownStep() {
  const home = selectedHome();
  const meta = shutdownGateMeta();
  const check = state[meta.checkKey];
  const running = check && Array.isArray(check.running) ? check.running : [];
  const verified = state[meta.verifiedKey];
  const override = state[meta.overrideKey];
  const statusClass = verified ? "is-good" : override ? "is-warning" : running.length ? "is-danger" : "is-warning";
  const statusTitle = verified ? meta.verifiedTitle : override ? meta.overrideTitle : running.length ? meta.checkTitle : "Verification required";
  const statusCopy = verified
    ? (meta.stepId === "spbBackupShutdown"
      ? "PatchPilot found no process scoped to the selected ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME. The file-system tar backup can be created from a stopped home."
      : "PatchPilot found no process scoped to the selected ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME.")
    : override
      ? "You confirmed the remaining process list was handled manually or is safe to ignore for this selected home."
    : running.length
      ? "Stop the selected services gracefully, then recheck. Only selected-home paths are included."
      : "PatchPilot will inspect the SSH target process table and match only the selected home/domain paths.";
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">${escapeHtml(meta.breadcrumb)}</p>
      <h2>${escapeHtml(meta.heading)}</h2>
    </div>
    <label class="toggle-row decision-row">
      <input id="customerStoppedInput" type="checkbox" ${state[meta.customerStoppedKey] ? "checked" : ""}>
      <span>${escapeHtml(meta.confirmText)} for ${escapeHtml(home.oracleHome)}.</span>
    </label>
    <div class="review-box ${statusClass}">
      <strong>${escapeHtml(statusTitle)}</strong>
      <p>${escapeHtml(statusCopy)}</p>
    </div>
    <div class="service-checks">
      ${renderShutdownRows(home, meta.stepId)}
    </div>
    ${running.length ? `
      <div class="shutdown-actions">
        <button id="shutdownPlanButton" class="button button-secondary" type="button">Stop Selected Services</button>
        <button id="shutdownKillButton" class="button button-secondary danger-button" type="button">Kill Selected Processes</button>
        <button id="shutdownOverrideButton" class="button button-secondary" type="button">Continue Anyway</button>
      </div>
    ` : ""}
  `;
}

function renderBackupStep() {
  const targets = backupTargetPayload();
  const backup = state.backupResult;
  const dbBackupRequired = requiresDatabaseBackupForRun();
  const stoppedForBackup = backupShutdownComplete();
  const destinationChanged = state.backupDestinationDirty;
  const preflight = destinationChanged ? null : state.backupPreflight;
  const preflightByTarget = new Map(((preflight && preflight.results) || []).map((item) => [item.target, item]));
  const preflightTone = preflight
    ? preflight.enoughSpace
      ? preflight.hasLargeTargets ? "is-warning" : "is-good"
      : "is-danger"
    : "";

  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Rollback Safety</p>
      <h2>Backup Plan</h2>
    </div>
    <label class="field">
      <span>Backup destination on target server</span>
      <input id="backupDirInput" value="${escapeHtml(backupDirForRun())}" placeholder="/path/with/enough/free/space">
      <p class="field-help">Services must be stopped before file-system tar backups. Default destination is outside the patch staging area: dirname(ORACLE_HOME)/backups. Change this path if needed, then run Check Backup Space to validate free space and planned tar files at the new location.</p>
    </label>
    <div class="review-box ${stoppedForBackup ? "is-good" : "is-danger"}">
      <strong>${stoppedForBackup ? "Services stopped for backup" : "Stop services before backup"}</strong>
      <p>${stoppedForBackup ? "PatchPilot verified or recorded customer acceptance that selected-home services are down before tar backup creation." : `Go back to ${isStackPatchBundle() ? "Stop for Backup" : "Shutdown"} and verify no selected-home services are running before creating ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME tar files.`}</p>
    </div>
    <div id="backupDestinationChangedNotice" class="review-box is-warning" ${destinationChanged ? "" : "hidden"}>
      <strong>Backup destination changed</strong>
      <p>Run Check Backup Space again so PatchPilot can verify free space and recalculate the tar file names for this location.</p>
    </div>
    <div class="backup-list">
      ${targets.map((item) => `
        <article class="check-row">
          <div>
            <strong>${escapeHtml(item.target)}</strong>
            <p class="check-copy mono">${escapeHtml(item.path)}</p>
            ${preflightByTarget.get(item.target) ? `
              <p class="check-copy">Directory size: <strong>${escapeHtml(preflightByTarget.get(item.target).sourceSize || "unknown")}</strong>${preflightByTarget.get(item.target).large ? ` | Larger than ${escapeHtml(preflight.largeThreshold || "5 GB")}` : ""}</p>
              <p class="check-copy mono">Backup tar: ${escapeHtml(preflightByTarget.get(item.target).archive || "")}</p>
              ${preflightByTarget.get(item.target).error ? `<p class="check-copy danger-text">${escapeHtml(preflightByTarget.get(item.target).error)}</p>` : ""}
            ` : `<p class="check-copy">Run backup space check to calculate size and planned archive path.</p>`}
          </div>
          <span class="state-dot ${state.backupsDone ? "is-pass" : "is-warn"}"></span>
        </article>
      `).join("")}
    </div>
    ${preflight ? `
      <div id="backupPreflightPanel">
      <div class="review-box ${preflightTone}">
        <strong>${preflight.enoughSpace ? "Backup space check completed" : "Backup space check needs attention"}</strong>
        <p>${preflight.enoughSpace ? "Review the destination and planned tar files, then confirm before PatchPilot starts the backup." : "Change the destination path and run the space check again, or confirm that backups were completed outside PatchPilot."}</p>
      </div>
      <div class="detail-grid backup-space-grid">
        <div><span>Backup directory</span><strong class="mono">${escapeHtml(preflight.backupDir || backupDirForRun())}</strong></div>
        <div><span>Disk checked</span><strong class="mono">${escapeHtml(preflight.diskPath || "")}</strong></div>
        <div><span>Free space</span><strong>${escapeHtml(preflight.freeSpace || "unknown")}</strong></div>
        <div><span>Selected source size</span><strong>${escapeHtml(preflight.requiredSpace || "unknown")}</strong></div>
      </div>
      ${(preflight.errors || []).length ? `
        <div class="review-box is-danger">
          <strong>Backup cannot start yet</strong>
          <p>${escapeHtml(preflight.errors.join(" "))}</p>
        </div>
      ` : ""}
      ${preflight.enoughSpace ? `
        <label class="toggle-row decision-row">
          <input id="backupPreflightConfirmInput" type="checkbox" ${state.backupPreflightConfirmed ? "checked" : ""}>
          <span>Confirm backup destination and available space. Create the tar files shown above.</span>
        </label>
      ` : ""}
      </div>
    ` : `
      <div class="review-box is-warning">
        <strong>Backup space check required</strong>
        <p>After services are stopped, PatchPilot will calculate ORACLE_HOME, DOMAIN_HOME, and INSTANCE_HOME sizes, check free disk space on the target server, and show the exact tar paths before backup starts.</p>
      </div>
    `}
    ${dbBackupRequired ? `
      <div class="review-box ${state.databaseBackupConfirmed ? "is-good" : "is-warning"}">
        <strong>${state.databaseBackupConfirmed ? "Database backup confirmed" : "Database backup required"}</strong>
        <p>For SPB/CSPU, PSU/SPU, and Bundle Patch runs, ask the customer or DBA to complete the approved database backup before inactive patch cleanup or patch execution. PatchPilot records the confirmation and reference but does not run database backup commands.</p>
      </div>
      <label class="toggle-row decision-row">
        <input id="databaseBackupConfirmInput" type="checkbox" ${state.databaseBackupConfirmed ? "checked" : ""}>
        <span>Confirm the required database backup is complete and usable for recovery.</span>
      </label>
      <label class="field">
        <span>Database backup reference</span>
        <input id="databaseBackupNoteInput" value="${escapeHtml(state.databaseBackupNote)}" placeholder="RMAN backup tag, snapshot id, ticket, DBA confirmation, or operator note">
      </label>
    ` : ""}
    <label class="toggle-row decision-row">
      <input id="backupExternalInput" type="checkbox" ${state.backupExternalApproved ? "checked" : ""}>
      <span>Confirm required file-system backups were completed outside PatchPilot or by another approved process.</span>
    </label>
    ${state.backupExternalApproved ? `
      <label class="field">
        <span>External Backup Location / Reference</span>
        <input id="backupExternalNoteInput" value="${escapeHtml(state.backupExternalNote)}" placeholder="Optional path, ticket, snapshot, or operator note">
      </label>
      <div class="review-box is-warning">
        <strong>External backup accepted</strong>
        <p>PatchPilot records this as externally completed file-system backup evidence. If this patch type requires a database backup, confirm it separately above.</p>
      </div>
    ` : ""}
    ${backup ? `
      <div class="review-box ${backup.status === "succeeded" ? "is-good" : "is-danger"}">
        <strong>${backup.status === "succeeded" ? "Backup archives created" : "Backup needs review"}</strong>
        <p>Backup log: <span class="mono">${escapeHtml(backup.logPath || "")}</span></p>
      </div>
      <div class="backup-result-list" aria-label="Backup archive details">
        ${(backup.results || []).map((item) => `
          <article class="backup-result-card ${item.status === "succeeded" ? "is-good" : "is-danger"}">
            <div>
              <span>Target</span>
              <strong>${escapeHtml(item.target)} ${escapeHtml(item.status || "")}</strong>
            </div>
            <div>
              <span>Source</span>
              <strong class="mono">${escapeHtml(item.source || "")}</strong>
            </div>
            <div>
              <span>Backup file</span>
              <strong class="mono">${escapeHtml(item.archive || item.error || "No archive created")}</strong>
            </div>
            <div>
              <span>Archive size</span>
              <strong>${escapeHtml(item.archiveSize || "not available")}</strong>
            </div>
            ${item.sourceSize ? `
              <div>
                <span>Source size</span>
                <strong>${escapeHtml(item.sourceSize)}</strong>
              </div>
            ` : ""}
          </article>
        `).join("")}
      </div>
    ` : ""}
  `;
}

function backupTargetsForRun() {
  const analysis = state.readmeAnalysis || analyzeReadme(state.readmeText);
  const targets = isStackPatchBundle()
    ? ["ORACLE_HOME", "DOMAIN_HOME"]
    : [...analysis.backupTargets];
  if ((spbInstallType() === "oud" || /oud/i.test(selectedHome().product || "")) && selectedHome().instanceHome) {
    targets.push("INSTANCE_HOME");
  }
  return uniqueValues(targets);
}

function backupTargetPayload() {
  const home = selectedHome();
  return backupTargetsForRun()
    .map((target) => ({
      target,
      path: target === "ORACLE_HOME" ? home.oracleHome : target === "DOMAIN_HOME" ? home.domainHome : home.instanceHome
    }))
    .filter((item) => item.path);
}

function renderOpatchStep() {
  const home = selectedHome();
  const analysis = state.readmeAnalysis || analyzeReadme(state.readmeText);
  const meetsMinimum = compareVersions(home.opatchVersion, analysis.minimumOpatch) >= 0;
  if (meetsMinimum) syncOpatchReadyFromDiscoveredVersion();
  const opatchCandidates = state.spbPrepareResult && Array.isArray(state.spbPrepareResult.opatchCandidates) ? state.spbPrepareResult.opatchCandidates : [];
  const opatchReadmes = state.spbPrepareResult && Array.isArray(state.spbPrepareResult.opatchReadmes) ? state.spbPrepareResult.opatchReadmes : [];
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Prerequisite Gate</p>
      <h2>OPatch Version Check</h2>
    </div>
    <div class="detail-grid">
      <div><span>Current OPatch</span><strong>${escapeHtml(home.opatchVersion)}</strong></div>
      <div><span>README Minimum</span><strong>${escapeHtml(analysis.minimumOpatch)}</strong></div>
      <div><span>Status</span><strong>${meetsMinimum ? "Meets minimum" : state.opatchReady ? "Ready" : "Upgrade required"}</strong></div>
    </div>
    ${meetsMinimum ? `
      <div class="review-box is-good">
        <strong>OPatch requirement satisfied</strong>
        <p>Current OPatch ${escapeHtml(home.opatchVersion)} is already higher than or equal to the README minimum ${escapeHtml(analysis.minimumOpatch)}. No revalidation or upgrade is required; continue to the next step.</p>
      </div>
    ` : ""}
    ${meetsMinimum || state.opatchReady ? "" : `
      <div class="review-box is-warning">
        <strong>Minimum OPatch version is not met</strong>
        <p>${isStackPatchBundle() ? "PatchPilot first looks inside the SPB download for opatch_generic.jar. If it is not there, download OPatch patch 28186730 from My Oracle Support, stage it on this server, and enter that zip, extracted directory, or opatch_generic.jar path below." : "Download the latest OPatch bundle, then provide its server path below."}</p>
      </div>
      ${isStackPatchBundle() && opatchCandidates.length ? `
        <div class="review-box is-good">
          <strong>OPatch candidate found in SPB download</strong>
          <p class="mono">${escapeHtml(opatchCandidates[0])}</p>
        </div>
      ` : ""}
      ${isStackPatchBundle() && opatchReadmes.length ? `
        <div class="review-box is-good">
          <strong>OPatch README found</strong>
          <p class="mono">${escapeHtml(opatchReadmes[0])}</p>
        </div>
      ` : ""}
      <label class="field">
        <span>OPatch Upgrade Path</span>
        <input id="opatchBundleInput" value="${escapeHtml(state.form.opatchBundlePath || (opatchCandidates[0] || ""))}" placeholder="${escapeHtml(`${state.form.patchPath}/tools/opatch/generic or /path/to/p28186730...zip`)}">
      </label>
      ${isStackPatchBundle() ? `
        <div class="review-box is-warning">
          <strong>PatchPilot can upgrade OPatch</strong>
          <p>PatchPilot accepts a directory, zip file, or direct opatch_generic.jar path. If the SPB does not ship it, stage MOS patch 28186730 and enter that path.</p>
          <p><strong>Dry-run commands first</strong> is currently ${dom.dryRunToggle.checked ? "enabled, so Upgrade / Validate OPatch will print the java -jar command without changing this home." : "disabled, so PatchPilot will back up OPatch, run the java -jar upgrade, and recheck the real version."}</p>
        </div>
      ` : ""}
    `}
  `;
}

function renderSpbPrestopStep() {
  const prestopAccepted = state.spbPrestopDone || state.spbPrestopExternalApproved;
  if (state.spbFreshInstallNoDomain) {
    return `
      <div class="step-heading">
        <p class="page-breadcrumb">SPBAT Phase</p>
        <h2>PreStop Baseline</h2>
      </div>
      <div class="review-box ${prestopAccepted ? "is-good" : "is-warning"}">
        <strong>${state.spbPrestopDone ? "PreStop baseline completed" : state.spbPrestopExternalApproved ? "PreStop baseline accepted as already completed" : "PreStop baseline required"}</strong>
        <p>SPBAT Downtime requires valid status logs from a prior phase. For a fresh install before domain creation, do not start services, but run SPBAT PreStop once with the same log directory before Downtime.</p>
      </div>
      <div class="command-list">
        <code>${escapeHtml(spbatCommand("prestop"))}</code>
        <code>${escapeHtml(spbatStatusCommand())}</code>
      </div>
      ${renderSpbPhaseStatus("prestop")}
      ${renderSpbPhaseRuntimePanel("prestop")}
      <div class="detail-grid">
        <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(selectedHome().oracleHome)}</strong></div>
        <div><span>DOMAIN_HOME</span><strong class="mono">${escapeHtml(selectedHome().domainHome || "Not created yet")}</strong></div>
      </div>
      <div class="review-box is-info">
        <strong>No services need to be started</strong>
        <p>This PreStop run is for SPBAT phase/status initialization. Use the same SPBAT log directory for Downtime.</p>
      </div>
      <label class="toggle-row decision-row">
        <input id="spbPrestopExternalInput" type="checkbox" ${state.spbPrestopExternalApproved ? "checked" : ""}>
        <span>Confirm SPBAT PreStop baseline was already completed successfully.</span>
      </label>
      ${state.spbPrestopExternalApproved ? `
        <label class="field">
          <span>PreStop Report / Log Reference</span>
          <input id="spbPrestopExternalNoteInput" value="${escapeHtml(state.spbPrestopExternalNote)}" placeholder="Optional report path, log directory, ticket, or operator note">
        </label>
      ` : ""}
      <div class="inline-actions">
        <button class="button button-secondary spb-report-button" type="button">Open SPBAT Report / Log</button>
        <span class="mono">${escapeHtml(spbLogDir())}</span>
      </div>
    `;
  }
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">SPBAT Phase</p>
      <h2>PreStop While Services Are Up</h2>
    </div>
    <div class="command-list">
      <code>${escapeHtml(spbatCommand("prestop"))}</code>
      <code>${escapeHtml(spbatStatusCommand())}</code>
    </div>
    ${renderSpbPhaseStatus("prestop")}
    ${renderSpbPhaseRuntimePanel("prestop")}
    <div class="review-box ${prestopAccepted ? "is-good" : "is-warning"}">
      <strong>${state.spbPrestopDone ? "PreStop completed" : state.spbPrestopExternalApproved ? "PreStop accepted as already completed" : "PreStop must complete before shutdown"}</strong>
      <p>${state.spbPrestopExternalApproved ? "PatchPilot will allow the workflow to continue and record this PreStop as completed outside the current PatchPilot run." : "Review the generated HTML report under the SPBAT log directory before moving into the shutdown gate."}</p>
    </div>
    <label class="toggle-row decision-row">
      <input id="spbPrestopExternalInput" type="checkbox" ${state.spbPrestopExternalApproved ? "checked" : ""}>
      <span>Confirm SPBAT PreStop was already completed successfully.</span>
    </label>
    ${state.spbPrestopExternalApproved ? `
      <label class="field">
        <span>PreStop Report / Log Reference</span>
        <input id="spbPrestopExternalNoteInput" value="${escapeHtml(state.spbPrestopExternalNote)}" placeholder="Optional report path, log directory, ticket, or operator note">
      </label>
      <div class="review-box is-warning">
        <strong>External PreStop accepted</strong>
        <p>Use this only when the PreStop report has already been reviewed and is successful.</p>
      </div>
    ` : ""}
    <div class="inline-actions">
      <button class="button button-secondary spb-report-button" type="button">Open SPBAT Report / Log</button>
      <span class="mono">${escapeHtml(spbLogDir())}</span>
    </div>
  `;
}

function renderSpbInactiveStep() {
  const check = state.spbInactiveCheck;
  const result = state.spbInactiveResult;
  const hasInactive = spbInactiveHasPatches(check);
  const desiredReached = spbInactiveDesiredReached(check);
  const deletionCandidate = Boolean(check && hasInactive && !desiredReached);
  const retainDecisionCandidate = Boolean(check && hasInactive && desiredReached && !spbInactiveCleanupSucceeded(result));
  const backupComplete = spbInactiveBackupSafetyConfirmed();
  const backupSafetyConfirmed = spbInactiveBackupSafetyConfirmed();
  const earlyComplete = spbInactiveEarlyComplete();
  const downtimeReady = spbInactiveDowntimeReady();
  const jobRunning = state.spbInactiveJobStatus === "running";
  const output = state.spbInactiveJobOutput || (result && result.output) || (check && check.output) || "";
  const cleanupSucceeded = spbInactiveCleanupSucceeded(result);
  const cleanupIssue = spbInactiveCleanupIssue(result);
  const blockingCleanupError = Boolean(!state.spbInactiveSkipConfirmed && (cleanupIssue || state.spbInactiveError));
  const exitDecisionAvailable = Boolean(!state.spbInactiveSkipConfirmed && backupComplete && (deletionCandidate || retainDecisionCandidate || blockingCleanupError));
  const dryRunCleanupPreview = Boolean(dom.dryRunToggle && dom.dryRunToggle.checked && spbInactiveDeleteReady());
  const statusClass = jobRunning
    ? "is-info"
    : blockingCleanupError
    ? "is-danger"
    : cleanupSucceeded
      ? "is-good"
      : state.spbInactiveSkipConfirmed
        ? "is-warning"
        : deletionCandidate && state.spbInactiveRemoveConfirmed && !backupSafetyConfirmed
          ? "is-danger"
          : retainDecisionCandidate
            ? "is-warning"
          : check
            ? deletionCandidate ? "is-warning" : "is-good"
            : "is-info";
  const statusTitle = jobRunning
    ? state.spbInactiveStopRequested
      ? "Stopping OPatch operation"
      : state.spbInactiveJobKind === "check" ? "Inactive patch review running" : "Inactive cleanup running"
    : cleanupIssue
    ? "Inactive cleanup did not complete"
    : state.spbInactiveError
      ? "Inactive patch check failed"
    : result && result.status === "removed"
      ? "Inactive patches removed"
      : result && result.status === "already-retained"
        ? "Rollback retain level already satisfied"
        : state.spbInactiveSkipConfirmed
          ? "Inactive patches kept for this run"
          : deletionCandidate && state.spbInactiveRemoveConfirmed && !backupSafetyConfirmed
            ? "Backup confirmation required"
            : retainDecisionCandidate
              ? "Inactive patches within retain level"
            : check
              ? deletionCandidate ? "Inactive patches found" : "Inactive patch review complete"
              : "Check inactive patches before PreStop";
  const statusCopy = jobRunning
    ? state.spbInactiveStopRequested
      ? "PatchPilot sent a stop request for the selected ORACLE_HOME and is waiting for the background job to close."
      : state.spbInactiveJobKind === "check"
        ? "PatchPilot is running listorderedinactivepatches for the selected ORACLE_HOME and streaming available output below."
        : "PatchPilot is answering the OPatch deleteinactivepatches and cleanup prompts with y from the approved confirmation, and streaming the OPatch output below."
    : cleanupIssue
    ? `${result.error || "OPatch inactive cleanup did not reach the requested retain level."} Continue is blocked until cleanup succeeds, or the customer explicitly accepts keeping inactive patches for this run.`
    : state.spbInactiveError
      ? state.spbInactiveError
    : result && result.status === "removed"
      ? "PatchPilot updated RETAIN_INACTIVE_PATCHES, ran deleteinactivepatches, and performed OPatch cleanup. The post-approval list recheck is skipped because the pre-confirmation review already captured the inactive patch details."
      : result && result.status === "already-retained"
        ? "The selected ORACLE_HOME is already at or below the requested rollback retain level."
        : state.spbInactiveSkipConfirmed
          ? "PatchPilot will record that inactive patches were intentionally retained. SPBAT can take longer when many inactive patches remain."
          : deletionCandidate && state.spbInactiveRemoveConfirmed && !backupSafetyConfirmed
            ? "Complete the Backup step before deleting inactive patches."
            : retainDecisionCandidate
              ? "OPatch found inactive patches, but the selected ORACLE_HOME is already within the requested N-1 retain level. Explicitly keep them for this run so the report records why PatchPilot did not delete anything."
            : check
              ? deletionCandidate
                ? "Inactive patches can slow SPBAT PreStop and Downtime. Because backups are already confirmed, remove them now or explicitly keep them for this run."
                : "The selected ORACLE_HOME has no extra inactive patch cleanup required for the requested retain level."
              : "SPB README section 4.6 recommends reviewing inactive patches because they can increase storage use and slow SPBAT execution.";
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">SPBAT Prerequisite</p>
      <h2>Inactive Patch Cleanup</h2>
    </div>
    <div class="command-list">
      <code>${escapeHtml(spbInactiveListCommand())}</code>
      <code>${escapeHtml(spbInactiveRetainCommand())}</code>
      <code>${escapeHtml(spbInactiveDeleteCommand())}</code>
      <code>${escapeHtml(spbInactiveCleanupCommand())}</code>
    </div>
    <div class="review-box is-info">
      <strong>Optional step</strong>
      <p>You may review and remove inactive patches before SPBAT PreStop, or skip this step and continue. Keeping inactive patches can make SPBAT take longer; PatchPilot records that decision in the final report.</p>
      ${!jobRunning && !state.spbInactiveSkipConfirmed ? `
        <div class="inline-actions">
          <button id="spbInactiveSkipOptionalButton" class="button button-secondary" type="button">Skip This Optional Step</button>
        </div>
      ` : ""}
    </div>
    <div class="review-box ${statusClass}">
      <strong>${escapeHtml(statusTitle)}</strong>
      <p>${escapeHtml(statusCopy)}</p>
    </div>
    ${renderSpbInactiveRuntimePanel()}
    ${jobRunning ? `
      <div class="review-box is-warning">
        <strong>${state.spbInactiveStopRequested ? "Stop requested" : "Why this may take longer"}</strong>
        <p>${escapeHtml(spbInactiveDelayExplanation())}</p>
        <div class="inline-actions">
          <button id="spbInactiveStopButton" class="button button-secondary" type="button" ${state.spbInactiveStopRequested ? "disabled" : ""}>${state.spbInactiveStopRequested ? "Stopping..." : "Stop OPatch Operation"}</button>
          <button id="spbInactiveStopAndSkipButton" class="button button-secondary" type="button" ${state.spbInactiveStopRequested ? "disabled" : ""}>Stop and Skip Step</button>
        </div>
      </div>
    ` : ""}
    ${dryRunCleanupPreview ? `
      <div class="review-box is-warning">
        <strong>Dry-run is on</strong>
        <p>Click Preview Inactive Cleanup to show the OPatch commands only. Clear Dry-run commands first before running real inactive patch deletion, or keep inactive patches for this run and continue.</p>
      </div>
    ` : ""}
    ${exitDecisionAvailable ? `
      <div class="review-box ${blockingCleanupError ? "is-danger" : "is-warning"}">
        <strong>${blockingCleanupError ? "Continue needs a decision" : "Exit path available"}</strong>
        <p>${blockingCleanupError
          ? "PatchPilot could not confirm inactive patch cleanup completed successfully. Retry after reviewing the output, generate an SR report, or explicitly keep inactive patches for this run so the risk is recorded before continuing."
          : retainDecisionCandidate
            ? "The inactive patch count is already within the requested rollback retain level. Use this option to continue without deleting and record the retain decision in the report."
            : "If the customer does not want to remove inactive patches now, explicitly keep them for this run so PatchPilot records the longer SPBAT runtime risk in the report."}</p>
        <div class="inline-actions">
          <button id="spbInactiveKeepAndContinueButton" class="button button-secondary" type="button">Continue Without Deleting</button>
          ${blockingCleanupError ? `<button id="spbInactiveFailureReportButton" class="button button-secondary" type="button">Generate SR Report</button>` : ""}
        </div>
      </div>
    ` : ""}
    <label class="field">
      <span>Rollback retain level</span>
      <input id="spbInactiveRetainInput" type="number" min="1" max="9" value="${escapeHtml(String(spbInactiveRetainLevel()))}">
      <p class="field-help">README section 4.6 recommends RETAIN_INACTIVE_PATCHES=1 to keep rollback capability at N-1.</p>
    </label>
    ${check ? `
      <div class="detail-grid">
        <div><span>Inactive patch summary</span><strong>${escapeHtml(spbInactiveSummary(check))}</strong></div>
        <div><span>Desired retain level</span><strong>N-${escapeHtml(String(spbInactiveRetainLevel()))}</strong></div>
        <div><span>Current retain property</span><strong>${escapeHtml(check.currentRetainInactivePatches || "Not set")}</strong></div>
        <div><span>OPatch Heap</span><strong class="mono">${escapeHtml(opatchHeapDisplay(check.opatchHeap))}</strong></div>
        <div><span>OPatch properties</span><strong class="mono">${escapeHtml(check.propertiesPath || "")}</strong></div>
        <div><span>Services stopped before backup</span><strong>${escapeHtml(backupShutdownComplete() ? "Confirmed" : "Not confirmed")}</strong></div>
        <div><span>Backup safety</span><strong>${escapeHtml(backupComplete ? "Backup gate complete" : "Not confirmed yet")}</strong></div>
        <div><span>Downtime gate</span><strong>${escapeHtml(downtimeReady ? "Ready" : cleanupIssue ? "Blocked by cleanup failure" : deletionCandidate || retainDecisionCandidate ? "Decision required" : "Not checked")}</strong></div>
        ${state.spbInactiveJobId ? `<div><span>${state.spbInactiveJobKind === "check" ? "Review" : "Cleanup"} job</span><strong>${escapeHtml(state.spbInactiveJobStatus || "unknown")}</strong></div>` : ""}
      </div>
    ` : ""}
    ${result ? `
      <div class="detail-grid">
        <div><span>Cleanup status</span><strong>${escapeHtml(result.status || "unknown")}</strong></div>
        <div><span>Delete runs</span><strong>${escapeHtml(String(result.deleteRuns || 0))}</strong></div>
        <div><span>OPatch Heap</span><strong class="mono">${escapeHtml(opatchHeapDisplay(result.opatchHeap))}</strong></div>
        <div><span>Properties backup</span><strong class="mono">${escapeHtml(result.propertiesBackup || "Not needed")}</strong></div>
        <div><span>After cleanup</span><strong>${escapeHtml(spbInactiveSummary(result.afterCheck || check))}</strong></div>
        ${result.failedCommand ? `<div><span>Failed command</span><strong class="mono">${escapeHtml(result.failedCommand)}</strong></div>` : ""}
        ${result.returnCode ? `<div><span>Return code</span><strong>${escapeHtml(String(result.returnCode))}</strong></div>` : ""}
      </div>
    ` : ""}
    ${check && hasInactive ? `
      ${backupComplete ? `
        <div class="review-box is-good">
          <strong>Backup gate complete</strong>
          <p>PatchPilot has backup confirmation, including database backup confirmation when required for this patch type.</p>
        </div>
      ` : `
        <div class="review-box is-danger">
          <strong>Backup gate incomplete</strong>
          <p>${escapeHtml(backupGateBlockReason())}</p>
        </div>
      `}
      ${!desiredReached ? `
        <label class="toggle-row decision-row">
          <input id="spbInactiveRemoveConfirmInput" type="checkbox" ${state.spbInactiveRemoveConfirmed ? "checked" : ""}>
          <span>Confirm the customer approved deleting inactive patches and setting RETAIN_INACTIVE_PATCHES=${escapeHtml(String(spbInactiveRetainLevel()))} for N-${escapeHtml(String(spbInactiveRetainLevel()))} rollback retention.</span>
        </label>
      ` : ""}
      <label class="toggle-row decision-row">
        <input id="spbInactiveSkipInput" type="checkbox" ${state.spbInactiveSkipConfirmed ? "checked" : ""}>
        <span>${desiredReached
          ? "Keep inactive patches for this run because the selected ORACLE_HOME is already within the requested retain level. The report will record that cleanup was not required."
          : "Keep inactive patches for this run. I understand SPBAT may take longer and the report will record that cleanup was skipped."}</span>
      </label>
    ` : ""}
    ${earlyComplete ? `
      <div class="review-box ${downtimeReady ? "is-good" : "is-warning"}">
        <strong>${downtimeReady ? "Ready for SPBAT PreStop" : "Inactive cleanup needs review"}</strong>
        <p>${downtimeReady ? "Inactive patch review is complete for this run." : "Remove inactive patches or explicitly keep them before continuing."}</p>
      </div>
    ` : ""}
    ${output ? `<pre class="oig-log-preview spb-inactive-output">${escapeHtml(output.slice(-16000))}</pre>` : ""}
  `;
}

function renderSpbDowntimeStep() {
  const backupAccepted = backupGateComplete();
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">SPBAT Phase</p>
      <h2>Downtime Patching</h2>
    </div>
    <div class="command-list">
      <code>${escapeHtml(spbatCommand("downtime"))}</code>
      <code>${escapeHtml(spbatStatusCommand())}</code>
    </div>
    ${renderSpbPhaseStatus("downtime")}
    ${renderSpbPhaseRuntimePanel("downtime")}
    <div class="review-box ${state.spbDowntimeDone ? "is-good" : state.failed.has("spbDowntime") ? "is-danger" : "is-warning"}">
      <strong>${state.spbDowntimeDone ? "Downtime phase completed" : "Ready for SPBAT Downtime"}</strong>
      <p>Services must remain stopped for this phase. Use the same log directory created during SPBAT setup.</p>
    </div>
    <div class="review-box is-warning">
      <strong>Confirm rollback safety before Downtime</strong>
      <p>SPBAT backout depends on the required backups because the SPBAT utility does not provide automated rollback support. PatchPilot will ask for backup confirmation before running this phase.</p>
    </div>
    ${backupAccepted && state.backupExternalApproved ? `
      <div class="review-box is-warning">
        <strong>External backup accepted</strong>
        <p>PatchPilot will allow SPBAT Downtime using the backup confirmation from the Backup step${state.backupExternalNote ? `: ${escapeHtml(state.backupExternalNote)}` : "."}</p>
      </div>
    ` : backupAccepted ? `
      <div class="review-box is-good">
        <strong>Backup gate complete</strong>
        <p>Required file-system and database backup confirmations were completed before downtime.</p>
      </div>
    ` : `
      <div class="review-box is-warning">
        <strong>Backup gate incomplete</strong>
        <p>${escapeHtml(backupGateBlockReason())}</p>
      </div>
    `}
    <div class="inline-actions">
      <button class="button button-secondary spb-report-button" type="button">Open SPBAT Report / Log</button>
      <span class="mono">${escapeHtml(spbLogDir())}</span>
    </div>
  `;
}

function cleanupPathBase(path) {
  return String(path || "")
    .trim()
    .replace(/\\/g, "/")
    .replace(/\/\*$/g, "")
    .replace(/\/+$/g, "");
}

function cleanupPathWithContents(base) {
  return `${cleanupPathBase(base)}/*`;
}

function cleanupServiceNameCandidate(item) {
  const service = serviceSummary(item);
  const group = shutdownGroup(service);
  const name = String(service.service || "").trim();
  if (!name || group === "nodeManager" || group === "systemComponent" || group === "other") return "";
  if (group !== "adminServer" && group !== "managedServer") return "";
  if (/process\s+under|node\s*manager|derby|directory|http\s+server/i.test(name)) return "";
  const clean = name.replace(/^WebLogic\s+Server\s+/i, "").trim();
  return /^[A-Za-z0-9_.-]+$/.test(clean) ? clean : "";
}

function spbCleanupServerNames() {
  const names = [];
  const seen = new Set();
  const add = (item) => {
    const name = cleanupServiceNameCandidate(item);
    const key = name.toLowerCase();
    if (!name || seen.has(key)) return;
    seen.add(key);
    names.push(name);
  };
  const home = selectedHome();
  (Array.isArray(home.services) ? home.services : []).forEach(add);
  [state.servicesUpCheck, state.shutdownCheck, state.backupShutdownCheck].forEach((check) => {
    if (check && Array.isArray(check.running)) check.running.forEach(add);
  });
  if (!names.length && home.domainHome) names.push("<SERVER_NAME>");
  return names;
}

function defaultSpbCleanupTargets() {
  const home = selectedHome();
  const domainHome = cleanupPathBase(home.domainHome);
  if (!domainHome) return [];
  return spbCleanupServerNames().flatMap((serverName) => [
    cleanupPathWithContents(`${domainHome}/servers/${serverName}/tmp`),
    cleanupPathWithContents(`${domainHome}/servers/${serverName}/cache`)
  ]);
}

function spbCleanupTargetsText() {
  return state.spbCleanupTargetsText.trim() || defaultSpbCleanupTargets().join("\n");
}

function spbCleanupTargets() {
  return spbCleanupTargetsText()
    .split(/[\r\n,]+/)
    .map((path) => path.trim())
    .filter(Boolean);
}

function spbCleanupCommands() {
  return spbCleanupTargets().map((path) => `Clear contents of ${cleanupPathWithContents(path)}`);
}

function spbCleanupDryRunEnabled() {
  return Boolean(dom.dryRunToggle && dom.dryRunToggle.checked);
}

function spbCleanupResultTone(result = state.spbCleanupResult) {
  if (!result) return state.spbCleanupStatus === "failed" ? "is-danger" : "is-warning";
  if (result.dryRun || result.status === "preview") return "is-warning";
  if (result.status === "succeeded" || result.status === "partial") return "is-good";
  return "is-danger";
}

function spbCleanupResultTitle(result = state.spbCleanupResult) {
  if (state.spbCleanupStatus === "running") return "Tmp/cache cleanup running";
  if (result && (result.dryRun || result.status === "preview")) return "Cleanup preview completed";
  if (result && result.status === "succeeded") return "Tmp/cache cleanup completed";
  if (result && result.status === "partial") return "Tmp/cache cleanup completed with warnings";
  if (state.spbCleanupError) return "Tmp/cache cleanup failed";
  return "Tmp/cache cleanup required";
}

function spbCleanupResultMessage(result = state.spbCleanupResult) {
  if (state.spbCleanupStatus === "running") return "PatchPilot is clearing only the listed tmp/cache directory contents on the SSH target.";
  if (result && (result.dryRun || result.status === "preview")) return "Dry-run is still enabled, so PatchPilot validated the paths but did not delete files. Uncheck dry-run when you are ready to clear them.";
  if (result && result.status === "succeeded") return "PatchPilot removed the listed tmp/cache contents and left the directories themselves in place.";
  if (result && result.status === "partial") return "PatchPilot finished without delete errors, but one or more listed paths were missing or empty. Review the per-path results below.";
  if (state.spbCleanupError) return state.spbCleanupError;
  return "Review or edit the paths, keep services stopped, then approve and clear tmp/cache before startup.";
}

function renderSpbCleanupTargetCards() {
  const targets = spbCleanupTargets();
  if (!targets.length) {
    return `
      <div class="review-box is-danger">
        <strong>No cleanup paths listed</strong>
        <p>Add one path per line using DOMAIN_HOME/servers/&lt;server&gt;/tmp/* and DOMAIN_HOME/servers/&lt;server&gt;/cache/*.</p>
      </div>
    `;
  }
  return `
    <div class="backup-list cleanup-target-list">
      ${targets.map((path) => `
        <article class="check-row">
          <div>
            <strong>${escapeHtml(path.includes("/cache") ? "Cache contents" : path.includes("/tmp") ? "Tmp contents" : "Cleanup path")}</strong>
            <p class="check-copy mono">${escapeHtml(cleanupPathWithContents(path))}</p>
          </div>
          <span class="state-dot ${state.postinstallDone ? "is-pass" : "is-warn"}"></span>
        </article>
      `).join("")}
    </div>
  `;
}

function renderSpbCleanupResultCards() {
  const result = state.spbCleanupResult;
  if (!result || !Array.isArray(result.targets) || !result.targets.length) return "";
  return `
    <div class="backup-result-list cleanup-result-list" aria-label="Tmp cache cleanup results">
      ${result.targets.map((item) => {
        const failed = item.status === "failed" || item.status === "rejected";
        const warning = item.status === "missing" || item.status === "preview";
        const tone = failed ? "is-danger" : warning ? "is-warning" : "is-good";
        return `
          <article class="backup-result-card cleanup-result-card ${tone}">
            <div>
              <span>Status</span>
              <strong>${escapeHtml(item.status || "unknown")}</strong>
            </div>
            <div>
              <span>Target</span>
              <strong class="mono">${escapeHtml(item.input || item.path || "")}</strong>
            </div>
            <div>
              <span>Directory</span>
              <strong class="mono">${escapeHtml(item.path || "")}</strong>
            </div>
            <div>
              <span>Items</span>
              <strong>${escapeHtml(String(item.removedCount ?? item.beforeCount ?? 0))}</strong>
            </div>
            ${item.error ? `
              <div>
                <span>Error</span>
                <strong>${escapeHtml(item.error)}</strong>
              </div>
            ` : ""}
          </article>
        `;
      }).join("")}
    </div>
  `;
}

function renderApplyStep() {
  const result = state.patchResult;
  const rollback = state.rollbackResult;
  const dryRunDone = result && result.status === "dry-run";
  const dryRunStillEnabled = dom.dryRunToggle.checked;
  const failed = state.failed.has("apply");
  const latestOpatchLog = currentOpatchLogLocation();
  const estimate = patchDurationEstimate();
  const progressPhase = patchProgressLabel();
  const applyLabel = patchApplyCommandLabel();
  const statusClass = state.rollbackDone ? "is-good" : state.patchApplied ? "is-good" : failed ? "is-danger" : dryRunDone ? dryRunStillEnabled ? "is-warning" : "is-good" : "";
  const title = state.rollbackDone
    ? rollback && rollback.alreadyRolledBack ? "Patch already rolled back and verified" : "Patch rolled back and verified"
    : state.patchApplied
    ? result && result.alreadyApplied ? "Patch already applied and verified" : "Patch applied and verified"
    : failed
      ? "Patch apply failed"
      : dryRunDone
        ? dryRunStillEnabled ? "Dry-run completed; patch not applied" : "Dry-run passed; ready to apply"
        : "Ready for conflict check and apply";
  const message = state.rollbackDone
    ? rollback && rollback.alreadyRolledBack
      ? "PatchPilot confirmed the patch id is not present in OPatch inventory for the selected ORACLE_HOME."
      : "OPatch rollback completed and lsinventory confirmed the patch id is no longer present."
    : state.patchApplied
    ? result && result.alreadyApplied
      ? `PatchPilot found every expected patch id in OPatch inventory for the selected ORACLE_HOME, so ${applyLabel} was skipped.`
      : `${applyLabel} completed and lsinventory confirmed every expected patch id in the selected ORACLE_HOME.`
    : failed
      ? (result && result.error) || "Review the OPatch output before retrying."
      : dryRunDone
        ? dryRunStillEnabled
          ? `Conflict checks passed, but Dry-run commands first is checked. Uncheck dry-run and click Apply Patch when you are ready to run ${applyLabel}.`
          : "Conflict check passed and Dry-run commands first is now unchecked. Click Apply Patch to apply this patch to the selected ORACLE_HOME."
        : `PatchPilot checks every selected patch for conflicts, runs ${applyLabel} when dry-run is off, then verifies all expected patch IDs in lsinventory before marking this complete.`;
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Patch Execution</p>
      <h2>Apply Patch</h2>
    </div>
    <div class="progress-block">
      <div class="meter"><span style="width: ${state.progress}%"></span></div>
      <strong>${state.progress}%</strong>
    </div>
    <div class="command-list">
      ${commandPreview().map((command) => `<code>${escapeHtml(command)}</code>`).join("")}
    </div>
    <div class="detail-grid patch-runtime-grid">
      <div>
        <span>Current OPatch log</span>
        <strong class="mono">${escapeHtml(latestOpatchLog || "Waiting for OPatch to report the log file location")}</strong>
      </div>
      <div>
        <span>Progress phase</span>
        <strong>${escapeHtml(progressPhase)}</strong>
      </div>
      <div>
        <span>Estimated duration</span>
        <strong>${escapeHtml(estimate.label)}</strong>
      </div>
      <div>
        <span>Estimate basis</span>
        <strong>${escapeHtml(estimate.basis)}</strong>
      </div>
    </div>
    <div class="review-box ${statusClass}">
      <strong>${escapeHtml(title)}</strong>
      <p>${escapeHtml(message)}</p>
    </div>
    ${failed ? `
      <div class="review-box is-warning">
        <strong>Generate an Oracle Support SR report</strong>
        <p>PatchPilot can build an HTML report with the OPatch version, patch number, target ORACLE_HOME, patch source, command list, and captured OPatch error. Ask the customer to create a Service Request with Oracle Support and attach the report.</p>
        <div class="inline-actions">
          <button id="applyFailureReportButton" class="button button-secondary" type="button">Generate Support Report</button>
        </div>
      </div>
    ` : ""}
    ${result ? `
      <div class="detail-grid">
        <div><span>Patch source</span><strong class="mono">${escapeHtml(result.patchPath || state.form.patchPath)}</strong></div>
        <div><span>Application mode</span><strong>${escapeHtml(result.applyMode === "batch" || isBatchNapply() ? "Batch one-offs - OPatch napply" : "Single patch - OPatch apply")}</strong></div>
        ${result.batchSelection ? `<div><span>Batch selection</span><strong>${escapeHtml(result.batchSelection)}</strong></div>` : ""}
        ${result.patchListFile ? `<div><span>Patch-list file</span><strong class="mono">${escapeHtml(result.patchListFile)}</strong></div>` : ""}
        <div><span>OPatch apply directory</span><strong class="mono">${escapeHtml(result.applyDir || result.patchPath || state.form.patchPath)}</strong></div>
        <div><span>Oracle home</span><strong class="mono">${escapeHtml(result.oracleHome || selectedHome().oracleHome)}</strong></div>
        <div><span>OPatch version</span><strong>${escapeHtml(result.opatchVersion || selectedHome().opatchVersion || "not recorded")}</strong></div>
        <div><span>OPatch heap</span><strong class="mono">${escapeHtml(opatchHeapDisplay(result.opatchHeap))}</strong></div>
        <div><span>OPatch log location</span><strong class="mono">${escapeHtml(currentOpatchLogLocation() || "not reported yet")}</strong></div>
        <div><span>Verbose/debug</span><strong>${state.opatchDebugUsed || result.debug ? "Enabled" : "Disabled"}</strong></div>
        <div><span>Patch ids expected</span><strong>${escapeHtml((result.targetPatchIds || result.patchIds || []).join(", ") || result.primaryPatchId || "not discovered")}</strong></div>
        <div><span>Inventory verified</span><strong>${result.inventoryVerified ? "Yes" : "No"}</strong></div>
        ${(result.foundPatchIds || []).length ? `<div><span>Patch ids found</span><strong>${escapeHtml(result.foundPatchIds.join(", "))}</strong></div>` : ""}
        ${(result.missingPatchIds || []).length ? `<div><span>Patch ids missing</span><strong>${escapeHtml(result.missingPatchIds.join(", "))}</strong></div>` : ""}
      </div>
    ` : ""}
    ${rollback ? `
      <div class="detail-grid">
        <div><span>Rollback patch id</span><strong>${escapeHtml(rollback.patchId || rollbackPatchId() || "not determined")}</strong></div>
        <div><span>Oracle home</span><strong class="mono">${escapeHtml(rollback.oracleHome || selectedHome().oracleHome)}</strong></div>
        <div><span>OPatch heap</span><strong class="mono">${escapeHtml(opatchHeapDisplay(rollback.opatchHeap))}</strong></div>
        <div><span>Inventory verified</span><strong>${rollback.inventoryVerified ? "Yes" : "No"}</strong></div>
        <div><span>Rollback status</span><strong>${escapeHtml(rollback.status || "unknown")}</strong></div>
      </div>
    ` : ""}
  `;
}

function renderPostinstallStep() {
  const analysis = state.readmeAnalysis || analyzeReadme(state.readmeText);
  if (isStackPatchBundle()) {
    const home = selectedHome();
    const targetsText = spbCleanupTargetsText();
    const cleanupResult = state.spbCleanupResult;
    const dryRun = spbCleanupDryRunEnabled();
    return `
      <div class="step-heading">
        <p class="page-breadcrumb">Before Startup</p>
        <h2>Cleanup Before Restart</h2>
      </div>
      <div class="review-box is-warning">
        <strong>Clear WebLogic tmp/cache before startup</strong>
        <p>PatchPilot will clear only the listed directory contents. Stage directories are not included by default. Keep services stopped while this runs.</p>
      </div>
      <div class="detail-grid">
        <div><span>DOMAIN_HOME</span><strong class="mono">${escapeHtml(home.domainHome || "Not discovered")}</strong></div>
        <div><span>Cleanup mode</span><strong>${dryRun ? "Dry-run preview only" : "Delete listed contents"}</strong></div>
      </div>
      <div class="command-list">
        ${spbCleanupCommands().map((command) => `<code>${escapeHtml(command)}</code>`).join("")}
      </div>
      <label class="field readme-text-field cleanup-path-field">
        <span>Tmp/cache cleanup paths</span>
        <textarea id="spbCleanupTargetsInput" rows="8" spellcheck="false">${escapeHtml(targetsText)}</textarea>
        <p class="field-help">One path per line. Use ${escapeHtml("DOMAIN_HOME/servers/<server>/tmp/*")} and ${escapeHtml("DOMAIN_HOME/servers/<server>/cache/*")}. Edit these paths if the server directory name is different.</p>
      </label>
      ${renderSpbCleanupTargetCards()}
      <div class="review-box ${spbCleanupResultTone(cleanupResult)}">
        <strong>${escapeHtml(spbCleanupResultTitle(cleanupResult))}</strong>
        <p>${escapeHtml(spbCleanupResultMessage(cleanupResult))}</p>
      </div>
      <label class="toggle-row decision-row">
        <input id="spbCleanupApprovedInput" type="checkbox" ${state.spbCleanupApproved ? "checked" : ""}>
        <span>Confirm the listed tmp/cache paths are correct, services are stopped, and PatchPilot should clear only those directory contents.</span>
      </label>
      ${renderSpbCleanupResultCards()}
      ${state.spbCleanupOutput ? `<pre class="oig-log-preview cleanup-output">${escapeHtml(state.spbCleanupOutput.slice(-16000))}</pre>` : ""}
    `;
  }
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Postinstall</p>
      <h2>README Postinstall Steps</h2>
    </div>
    ${analysis.postinstallRequiresManual ? `
      <div class="review-box is-warning">
        <strong>Manual action required</strong>
        <p>One or more README postinstall steps require credentials, database access, or product-specific judgement. PatchPilot will not run those steps automatically; perform them with the approved runbook, then confirm below.</p>
      </div>
    ` : `
      <div class="review-box is-info">
        <strong>Review required</strong>
        <p>Confirm every README postinstall instruction was completed or marked not applicable before starting services.</p>
      </div>
    `}
    ${renderInstructionCards(analysis.postinstallSteps, analysis.postinstallManualSteps, "Step", state.postinstallDone)}
    <label class="toggle-row decision-row">
      <input id="postinstallConfirmedInput" type="checkbox" ${state.postinstallConfirmed ? "checked" : ""}>
      <span>Confirm all README postinstallation steps above are complete or explicitly not applicable for this environment.</span>
    </label>
    ${analysis.postinstallSection ? `
      <div class="review-box is-info">
        <strong>Source section</strong>
        <p>Extracted from the README Post-Installation Instructions section and stopped before Deinstallation/Rollback instructions.</p>
      </div>
    ` : `
      <div class="review-box is-warning">
        <strong>No explicit section found</strong>
        <p>PatchPilot did not find a dedicated Post-Installation Instructions section. Review the full README text before confirming.</p>
      </div>
    `}
  `;
}

function renderOigProfileFields() {
  const editable = oigProfileEditableEntries();
  const secret = oigProfileSecretEntries();
  const missingSecretKeys = state.oigProfile && Array.isArray(state.oigProfile.missingSecretKeys) ? state.oigProfile.missingSecretKeys : [];
  return `
    ${editable.length ? `
      <h3>Non-password profile fields</h3>
      <div class="oig-profile-grid">
        ${editable.map((entry) => `
          <label class="field">
            <span>${escapeHtml(entry.key)}</span>
            <input class="oig-profile-input" data-oig-profile-key="${escapeHtml(entry.key)}" value="${escapeHtml(oigProfileFieldValue(entry))}" placeholder="${escapeHtml(entry.suggestedValue || "")}">
            ${oigProfileFieldHelp(entry) ? `<p class="field-help">${escapeHtml(oigProfileFieldHelp(entry))}</p>` : ""}
          </label>
        `).join("")}
      </div>
    ` : `<div class="empty-state">No editable non-password fields were found in the OIG profile.</div>`}
    ${secret.length ? `
      <h3>Password fields kept on server</h3>
      <div class="secret-field-grid">
        ${secret.map((entry) => `
          <div class="secret-field ${entry.filled ? "is-filled" : "is-missing"}">
            <span>${escapeHtml(entry.key)}</span>
            <strong>${escapeHtml(oigSecretStatus(entry))}</strong>
          </div>
        `).join("")}
      </div>
      ${missingSecretKeys.length ? `
        <div class="review-box is-warning">
          <strong>Password or runtime prompt review needed</strong>
          <p>PatchPilot cannot answer interactive password prompts from patch_oim_wls.sh. Fill these fields directly in ${escapeHtml(state.oigProfile.profilePath)} before running from PatchPilot, or run the script manually in a terminal and confirm completion here: ${escapeHtml(missingSecretKeys.join(", "))}.</p>
        </div>
      ` : ""}
    ` : ""}
  `;
}

function renderOigPostinstallHelper() {
  const profile = state.oigProfile;
  const profilePath = profile && profile.profilePath ? profile.profilePath : oigProfilePath();
  const scriptPath = profile && profile.scriptPath ? profile.scriptPath : oigScriptPath();
  const logPath = state.oigScriptLogPath || (profile && profile.logPath) || `${selectedHome().oracleHome}/idm/server/bin/patch_oim_wls.log`;
  const missingSecretKeys = profile && Array.isArray(profile.missingSecretKeys) ? profile.missingSecretKeys : [];
  const runDisabled = state.oigScriptStatus === "running" || missingSecretKeys.length > 0 || !state.oigPasswordsConfirmed;
  const statusClass = state.oigScriptStatus === "succeeded" || state.oigScriptStatus === "manual" ? "is-good" : state.oigScriptStatus === "failed" ? "is-danger" : "is-warning";
  const statusTitle = state.oigScriptStatus === "manual"
    ? "OIG postinstall confirmed manually"
    : state.oigScriptDone
      ? "OIG postinstall script completed"
      : state.oigScriptStatus === "running"
        ? "OIG postinstall script running"
        : state.oigScriptStatus === "failed"
          ? "OIG postinstall script failed"
          : "OIG postinstall script not run";
  const statusCopy = state.oigScriptStatus === "manual"
    ? "The operator confirmed patch_oim_wls.profile was handled and patch_oim_wls.sh was run or accepted outside PatchPilot for this environment."
    : state.oigScriptError || "After SPBAT PostStart completes, run patch_oim_wls.sh from PatchPilot only when password fields are already filled in the server-side profile. Runtime prompt mode should be run manually in a terminal, then confirmed here.";
  return `
    <div class="oig-helper">
      <div class="review-box is-info">
        <strong>OIG patch_oim_wls helper</strong>
        <p>PatchPilot prepares non-password values in patch_oim_wls.profile from config.xml, JDBC data sources, and process hints. Password fields stay on the server; browser runs require them to be filled before patch_oim_wls.sh starts.</p>
      </div>
      <div class="detail-grid">
        <div><span>Profile</span><strong class="mono">${escapeHtml(profilePath)}</strong></div>
        <div><span>Script</span><strong class="mono">${escapeHtml(scriptPath)}</strong></div>
        <div><span>Log</span><strong class="mono">${escapeHtml(logPath)}</strong></div>
        <div><span>Script status</span><strong>${escapeHtml(oigScriptStatusText())}</strong></div>
      </div>
      <div class="inline-actions">
        <button id="oigProfileLoadButton" class="button button-secondary" type="button">Load OIG Profile</button>
        ${profile ? `<button id="oigProfileSaveButton" class="button button-secondary" type="button">Backup + Fill Non-password Fields</button>` : ""}
        ${profile ? `<button id="oigRunScriptButton" class="button button-primary" type="button" ${runDisabled ? "disabled" : ""}>Run patch_oim_wls.sh</button>` : ""}
        <button id="oigLogTailButton" class="button button-secondary" type="button">Tail patch_oim_wls.log</button>
      </div>
      ${profile ? renderOigProfileFields() : `
        <div class="empty-state">Load the OIG profile to review fields and prepare non-password values.</div>
      `}
      ${profile ? `
        <label class="toggle-row decision-row">
          <input id="oigPasswordConfirmInput" type="checkbox" ${state.oigPasswordsConfirmed ? "checked" : ""}>
          <span>Confirm the customer filled/reviewed schema, WebLogic, and xelsysadm password fields directly in patch_oim_wls.profile after PatchPilot created patch_oim_wls.profile_backup. Commented password fields mean the script prompts at runtime and must be run manually in a terminal.</span>
        </label>
      ` : ""}
      <label class="toggle-row decision-row">
        <input id="oigManualAcceptInput" type="checkbox" ${state.oigManualAccepted ? "checked" : ""} ${state.spbPhaseStatus.poststart === "succeeded" ? "" : "disabled"}>
        <span>I will edit/update patch_oim_wls.profile and run patch_oim_wls.sh outside PatchPilot, or the customer accepts this OIG action as not applicable. Record this decision and allow Continue.</span>
      </label>
      <div class="review-box ${statusClass}">
        <strong>${escapeHtml(statusTitle)}</strong>
        <p>${escapeHtml(statusCopy)}</p>
      </div>
      ${state.oigScriptOutput ? `<pre class="oig-log-preview">${escapeHtml(state.oigScriptOutput.slice(-12000))}</pre>` : ""}
    </div>
  `;
}

function renderSpbPoststartStep() {
  const isOig = spbInstallType() === "oig";
  const spbatPoststartComplete = state.spbPhaseStatus.poststart === "succeeded";
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">After Startup</p>
      <h2>SPBAT PostStart and Product Actions</h2>
    </div>
    <div class="command-list">
      <code>${escapeHtml(spbatCommand("poststart"))}</code>
      <code>${escapeHtml(spbatStatusCommand())}</code>
      ${isOig ? `<code>${escapeHtml(`${selectedHome().oracleHome}/idm/server/bin/patch_oim_wls.sh`)}</code>` : ""}
    </div>
    ${renderSpbPhaseStatus("poststart")}
    ${renderSpbPhaseRuntimePanel("poststart")}
    ${isOig && !spbatPoststartComplete ? `
      <div class="review-box is-info">
        <strong>SPBAT PostStart runs first</strong>
        <p>Run the SPBAT utility with -phase poststart for the selected OIG home. PatchPilot will show the OIG patch_oim_wls profile/script actions only after the PostStart phase succeeds.</p>
      </div>
    ` : ""}
    ${isOig && spbatPoststartComplete ? renderOigPostinstallHelper() : ""}
    <div class="review-box ${state.spbPoststartDone ? "is-good" : "is-warning"}">
      <strong>${state.spbPoststartDone ? "PostStart completed" : "Run PostStart after services are started"}</strong>
      <p>${isOig ? "For OIG, this step is complete after SPBAT PostStart plus either a successful PatchPilot-run patch_oim_wls.sh or an explicit manual OIG action confirmation." : "Review SPBAT PostStart logs and complete validation after the phase finishes."}</p>
    </div>
    <div class="inline-actions">
      <button class="button button-secondary spb-report-button" type="button">Open SPBAT Report / Log</button>
      <span class="mono">${escapeHtml(spbLogDir())}</span>
    </div>
  `;
}

function renderRestartStep() {
  const home = selectedHome();
  const isSpb = isStackPatchBundle();
  const statusClass = state.restartDone ? "is-good" : "is-warning";
  const statusTitle = state.restartDone ? "Services start confirmed" : isSpb ? "Start services before SPBAT PostStart" : "Start services outside PatchPilot";
  const statusCopy = state.restartDone
    ? "Startup confirmation has been recorded for this run."
    : isSpb
      ? "Start the IDM/WebLogic services using the site runbook or approved startup scripts, then confirm before running SPBAT PostStart."
      : "Start the services using the customer runbook, custom scripts, Node Manager/AdminServer scripts, or product-specific startup procedure. PatchPilot records the confirmation but does not guess customer startup commands for one-off or bundle patches.";
  const services = Array.isArray(home.services) && home.services.length ? home.services : ["Selected Oracle home services"];
  const order = startupCommandHints(home, isSpb);
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Startup Gate</p>
      <h2>Start Services</h2>
    </div>
    <div class="review-box ${statusClass}">
      <strong>${escapeHtml(statusTitle)}</strong>
      <p>${escapeHtml(statusCopy)}</p>
    </div>
    <div class="detail-grid">
      <div><span>Target ORACLE_HOME</span><strong class="mono">${escapeHtml(home.oracleHome || "Not selected")}</strong></div>
      <div><span>Startup mode</span><strong>${escapeHtml(isSpb ? "SPB service start before PostStart" : "Manual/customer startup confirmation")}</strong></div>
    </div>
    <div class="command-list">
      ${order.map((item) => `<code>${escapeHtml(item)}</code>`).join("")}
    </div>
    <label class="toggle-row decision-row">
      <input id="startupConfirmedInput" type="checkbox" ${state.startupConfirmed ? "checked" : ""}>
      <span>${escapeHtml(isSpb ? "Confirm SPB services were started and are ready for PostStart." : "Confirm services were started after patching.")}</span>
    </label>
    <label class="field">
      <span>Startup command / script / note</span>
      <input id="startupNoteInput" value="${escapeHtml(state.startupNote)}" placeholder="Optional script path, ticket, runbook note, or operator confirmation">
    </label>
    <div class="service-checks">
      ${services.map((service) => `
        <article class="check-row">
          <div>
            <strong>${escapeHtml(service)}</strong>
            <p class="check-copy">${state.restartDone ? "Startup confirmed for this run." : "Waiting for startup confirmation."}</p>
          </div>
          <span class="state-dot ${state.restartDone ? "is-pass" : "is-warn"}"></span>
        </article>
      `).join("")}
    </div>
  `;
}

function reportText(value, fallback = "Not recorded") {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    const filtered = value.filter((item) => item !== null && item !== undefined && String(item).trim());
    return filtered.length ? filtered.join(", ") : fallback;
  }
  const text = String(value).trim();
  return text || fallback;
}

function reportDate(value) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return reportText(value);
  return date.toLocaleString();
}

function reportPatchType(report) {
  const analysis = report.readmeAnalysis || {};
  if (/\bcspu\b/i.test(report.patchType || "") || (analysis.patchMethod === "spbat" && /cspu/i.test(analysis.detectedPatchFamily || ""))) {
    return "CSPU (SPBAT / Stack Patch Bundle)";
  }
  return /stack\s+patch\s+bundle|\bspb\b/i.test(report.patchType || "") || analysis.patchMethod === "spbat" ? "Stack Patch Bundle (SPBAT)" : reportText(report.patchType);
}

function reportPatchLabel(report) {
  const analysis = report.readmeAnalysis || {};
  const patch = report.patchResult || {};
  const found = Array.isArray(patch.foundPatchIds) ? patch.foundPatchIds.filter(Boolean) : [];
  if (found.length) return found.join(", ");
  if (patch.primaryPatchId) return patch.primaryPatchId;
  if (Array.isArray(patch.patchIds) && patch.patchIds.length) return patch.patchIds.join(", ");
  const matches = String(report.patchPath || "").match(/\d{5,}(?:[._-]\d+)*/g);
  if (matches && matches.length) return matches[matches.length - 1];
  return /stack\s+patch\s+bundle|\bspb\b|\bcspu\b/i.test(report.patchType || "") || analysis.patchMethod === "spbat" ? "SPB" : "Selected patch";
}

function reportTone(outcome) {
  if (outcome === "completed") return "is-good";
  if (outcome === "rolled back" || outcome === "rollback in progress" || outcome === "rollback required") return "is-warning";
  if (outcome === "failed" || outcome === "rollback failed") return "is-danger";
  return "is-neutral";
}

function reportBadge(text, tone = "is-neutral") {
  return `<span class="report-status ${tone}">${escapeHtml(reportText(text))}</span>`;
}

function reportGrid(rows) {
  return `
    <div class="report-grid">
      ${rows.map(([label, value, className = ""]) => `
        <div>
          <span>${escapeHtml(label)}</span>
          <strong class="${escapeHtml(className)}">${escapeHtml(reportText(value))}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function reportSection(title, content, intro = "") {
  return `
    <section class="report-section">
      <h4>${escapeHtml(title)}</h4>
      ${intro ? `<p>${escapeHtml(intro)}</p>` : ""}
      ${content}
    </section>
  `;
}

function reportList(items, emptyText = "No entries recorded.") {
  const values = Array.isArray(items) ? items.filter((item) => item !== null && item !== undefined && String(item).trim()) : [];
  if (!values.length) return `<p class="report-empty">${escapeHtml(emptyText)}</p>`;
  return `
    <ul class="report-list">
      ${values.map((item) => `<li>${escapeHtml(reportText(item))}</li>`).join("")}
    </ul>
  `;
}

function reportCommandList(commands) {
  const values = Array.isArray(commands) ? commands.filter(Boolean) : [];
  if (!values.length) return `<p class="report-empty">No commands recorded.</p>`;
  return `
    <div class="report-command-list">
      ${values.map((command) => `<code>${escapeHtml(command)}</code>`).join("")}
    </div>
  `;
}

function reportInstructionList(steps, manualFlags = []) {
  const values = Array.isArray(steps) ? steps.filter((step) => step !== null && step !== undefined && String(step).trim()) : [];
  if (!values.length) return `<p class="report-empty">No postinstall instructions were captured.</p>`;
  return `
    <ol class="report-instruction-list">
      ${values.map((step, index) => `
        <li>
          <div class="report-instruction-heading">
            <strong>Postinstall ${index + 1}</strong>
            ${manualFlags[index] ? reportBadge("Manual action", "is-warning") : reportBadge("Review", "is-neutral")}
          </div>
          <pre>${escapeHtml(reportText(step))}</pre>
        </li>
      `).join("")}
    </ol>
  `;
}

function reportTable(headers, rows, emptyText = "No entries recorded.") {
  const safeRows = Array.isArray(rows) ? rows : [];
  if (!safeRows.length) return `<p class="report-empty">${escapeHtml(emptyText)}</p>`;
  return `
    <div class="report-table-wrap">
      <table>
        <thead>
          <tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${safeRows.map((row) => `
            <tr>${row.map((cell) => `<td>${escapeHtml(reportText(cell))}</td>`).join("")}</tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function reportStepNames(report, key) {
  const labels = new Map([...standardSteps, ...spbSteps].map((step) => [step.id, step.label]));
  return (Array.isArray(report[key]) ? report[key] : []).map((stepId) => labels.get(stepId) || stepId);
}

function reportBackupRows(report) {
  const backup = report.backupResult || {};
  const preflight = report.backupPreflight || {};
  const completedRows = Array.isArray(backup.results) ? backup.results : [];
  const plannedRows = Array.isArray(preflight.results) ? preflight.results : [];
  const sourceRows = completedRows.length ? completedRows : plannedRows;
  return sourceRows.map((item) => [
    item.target,
    item.status || (completedRows.length ? "unknown" : "planned"),
    item.source,
    item.sourceSize,
    item.archive,
    item.archiveSize || (completedRows.length ? "" : "pending")
  ]);
}

function patchLogLocationsForReport(report) {
  const patch = report.patchResult || {};
  const logs = opatchLogLocationsFromPatch(patch, report.patchApplyOutput || "");
  logs.push(...extractOpatchLogLocations(patchFailureOutput(report)));
  return uniqueValues(logs).join(", ");
}

function reportPatchRows(report) {
  const patch = report.patchResult || {};
  const estimate = report.patchEstimate || {};
  const rows = [
    ["Status", patch.status || (report.patchApplied ? "succeeded" : "not recorded")],
    ["Failure category", report.outcome === "failed" ? patchFailureCategory(report) : ""],
    ["Patch IDs expected", patch.patchIds || patch.primaryPatchId || reportPatchLabel(report)],
    ["Patch IDs found", patch.foundPatchIds || ""],
    ["Inventory verified", patch.inventoryVerified],
    ["Already applied", patch.alreadyApplied],
    ["OPatch version", patch.opatchVersion || (report.selectedHome && report.selectedHome.opatchVersion) || ""],
    ["OPatch heap options", (patch.opatchHeap && patch.opatchHeap.effective) || report.opatchHeapOptions || ""],
    ["Patch source", patch.patchPath || report.patchPath],
    ["OPatch apply directory", patch.applyDir || ""],
    ["OPatch log location", patchLogLocationsForReport(report)],
    ["OPatch verbose/debug", report.opatchDebugEnabled ? "Enabled" : "Disabled"],
    ["Estimated duration", estimate.label || ""],
    ["Estimate basis", estimate.basis || ""]
  ];
  if (report.outcome === "failed") {
    rows.push(["Error", patchFailureError(report)]);
  }
  return rows;
}

function reportRollbackRows(report) {
  const rollback = report.rollbackResult || {};
  const rows = [
    ["Rollback available", report.rollbackAvailable],
    ["Rollback completed", report.rollbackDone],
    ["Rollback patch ID", rollback.patchId || ""],
    ["Rollback status", rollback.status || ""],
    ["OPatch heap options", (rollback.opatchHeap && rollback.opatchHeap.effective) || report.opatchHeapOptions || ""],
    ["Inventory verified", rollback.inventoryVerified],
    ["Rollback ORACLE_HOME", rollback.oracleHome || ""]
  ];
  if (rollback.spb) {
    rows.push(
      ["SPBAT backout mode", rollback.phase || "manual backout"],
      ["SPBAT log directory", rollback.logDir || report.spbLogDir || ""],
      ["Backup/backout reference", rollback.backupReference || ""],
      ["SPBAT note", "SPBAT utility does not provide automated rollback; restore from confirmed backups if backout is required."]
    );
  }
  if (rollback.error) rows.push(["Rollback error", rollback.error]);
  return rows;
}

function reportSpbInactiveRows(report) {
  const check = report.spbInactiveCheck || null;
  const result = report.spbInactiveResult || {};
  const decision = report.spbInactiveSkipConfirmed
    ? "kept inactive patches"
    : result.status === "removed"
        ? "removed inactive patches"
        : result.status === "already-retained"
          ? "retain level already satisfied"
          : result.status === "none" || (check && check.hasInactive === false)
            ? "no inactive patches reported"
            : "not recorded";
  return [
    ["Review status", report.spbInactiveReviewed ? "completed" : "not completed"],
    ["Decision", decision],
    ["Retain level", report.spbInactiveRetainLevel ? `N-${report.spbInactiveRetainLevel}` : ""],
    ["Before cleanup", reportText(result.beforeSummary || (check ? check.totalLine : "") || spbInactiveSummary(check))],
    ["After cleanup", reportText(result.afterSummary || (result.afterCheck ? spbInactiveSummary(result.afterCheck) : ""))],
    ["Current RETAIN_INACTIVE_PATCHES", check ? check.currentRetainInactivePatches || "" : ""],
    ["OPatch heap options", (result.opatchHeap && result.opatchHeap.effective) || (check && check.opatchHeap && check.opatchHeap.effective) || report.opatchHeapOptions || ""],
    ["OPatch properties", check && check.propertiesPath ? check.propertiesPath : result.propertiesPath || ""],
    ["Delete runs", result.deleteRuns || ""],
    ["Failed command", result.failedCommand || ""],
    ["Return code", result.returnCode || ""],
    ["Properties backup", result.propertiesBackup || ""],
    ["Error", report.spbInactiveError || result.error || ""]
  ];
}

function reportSpbCleanupRows(report) {
  const result = report.spbCleanupResult || {};
  return [
    ["Cleanup approved", report.spbCleanupApproved],
    ["Cleanup status", report.spbCleanupStatus || result.status || ""],
    ["Dry-run", result.dryRun ? "Yes" : "No"],
    ["Target paths", report.spbCleanupTargets || ""],
    ["Error", report.spbCleanupError || result.error || ""]
  ];
}

function reportSpbCleanupTargetRows(report) {
  const result = report.spbCleanupResult || {};
  const rows = Array.isArray(result.targets) && result.targets.length
    ? result.targets.map((item) => [
      item.input || "",
      item.path || "",
      item.status || "",
      item.removedCount ?? item.beforeCount ?? "",
      item.error || ""
    ])
    : (Array.isArray(report.spbCleanupTargets) ? report.spbCleanupTargets : []).map((path) => [
      path,
      cleanupPathBase(path),
      "planned",
      "",
      ""
    ]);
  return rows;
}

function reportSpbCleanupAttemptRows(report) {
  const attempts = Array.isArray(report.spbCleanupAttempts) ? report.spbCleanupAttempts : [];
  return attempts.map((attempt, index) => {
    const targets = Array.isArray(attempt.targets) ? attempt.targets : [];
    const cleared = targets.reduce((total, item) => total + Number(item.removedCount || 0), 0);
    const alreadyEmpty = targets.filter((item) => item.status === "empty").length;
    return [
      attempt.attempt || index + 1,
      attempt.recordedAt || attempt.startedAt || "",
      attempt.status || "",
      cleared,
      alreadyEmpty,
      attempt.error || ""
    ];
  });
}

function reportSpbInactiveFailure(report) {
  const result = report.spbInactiveResult || {};
  if (report.spbInactiveSkipConfirmed || result.status === "skipped") return false;
  return Boolean(
    report.spbInactiveError
    || (result.status && !["removed", "already-retained", "none"].includes(result.status))
    || result.error
  );
}

function reportSpbInactiveSupportHandoff(report) {
  const result = report.spbInactiveResult || {};
  const check = report.spbInactiveCheck || {};
  const home = report.selectedHome || {};
  const output = result.output || result.cleanupOutput || check.output || "";
  return reportSection("Oracle Support SR Handoff", `
    <div class="report-callout is-danger">
      <h5>Recommended Next Action</h5>
      <p>Create a Service Request with Oracle Support and attach this HTML report. It includes the selected ORACLE_HOME, OPatch version, inactive patch retain level, failed OPatch utility command, return code, and captured output.</p>
    </div>
    ${reportGrid([
      ["SR summary", `Inactive patch cleanup failed before SPBAT PreStop on ${home.oracleHome || result.oracleHome || "selected ORACLE_HOME"}`],
      ["Patch", reportPatchLabel(report)],
      ["Patch type", reportPatchType(report)],
      ["OPatch version", home.opatchVersion || ""],
      ["OPatch heap options", (result.opatchHeap && result.opatchHeap.effective) || report.opatchHeapOptions || "", "mono"],
      ["ORACLE_HOME", home.oracleHome || result.oracleHome || "", "mono"],
      ["Retain level", report.spbInactiveRetainLevel ? `N-${report.spbInactiveRetainLevel}` : ""],
      ["OPatch properties", check.propertiesPath || result.propertiesPath || "", "mono"],
      ["Failed command", result.failedCommand || "OPatch inactive patch cleanup", "mono"],
      ["Return code", result.returnCode || ""],
      ["Error", report.spbInactiveError || result.error || ""]
    ])}
    <h5>Captured OPatch Output</h5>
    ${output ? `<pre class="report-log">${escapeHtml(output.slice(-20000))}</pre>` : `<p class="report-empty">No inactive cleanup output was captured.</p>`}
  `);
}

function reportSpbRows(report) {
  const statuses = report.spbPhaseStatus || {};
  const logs = report.spbPhaseLogs || {};
  const errors = report.spbPhaseErrors || {};
  return ["prestop", "downtime", "poststart"].map((phase) => [
    spbPhaseLabel(phase),
    phase === "prestop" && report.spbPrestopExternalApproved && !report.spbPrestopDone ? "accepted outside PatchPilot" : phase === "prestop" && report.spbPrestopDone ? "succeeded" : statuses[phase] || "not started",
    logs[phase] || "",
    phase === "prestop" && (report.spbPrestopExternalApproved || report.spbPrestopDone) ? "" : errors[phase] || ""
  ]);
}

function reportSupportHandoff(report) {
  const patch = report.patchResult || {};
  const home = report.selectedHome || {};
  const output = patchFailureOutput(report);
  const logLocation = patchLogLocationsForReport(report);
  return reportSection("Oracle Support SR Handoff", `
    <div class="report-callout is-danger">
      <h5>Recommended Next Action</h5>
      <p>Create a Service Request with Oracle Support and attach this HTML report. It includes the target host, selected ORACLE_HOME, OPatch version, patch number, patch source, command list, and captured OPatch failure output.</p>
    </div>
    ${reportGrid([
      ["SR summary", `Patch ${reportPatchLabel(report)} failed during ${patch.dryRun ? "OPatch dry-run" : "OPatch apply"} on ${home.oracleHome || patch.oracleHome || "selected ORACLE_HOME"}`],
      ["Failure category", patchFailureCategory(report)],
      ["Patch", reportPatchLabel(report)],
      ["Patch type", reportPatchType(report)],
      ["OPatch version", patch.opatchVersion || home.opatchVersion || ""],
      ["OPatch verbose/debug", report.opatchDebugEnabled ? "Enabled" : "Disabled"],
      ["OPatch heap options", (patch.opatchHeap && patch.opatchHeap.effective) || report.opatchHeapOptions || "", "mono"],
      ["ORACLE_HOME", home.oracleHome || patch.oracleHome || "", "mono"],
      ["Patch source", patch.patchPath || report.patchPath, "mono"],
      ["OPatch apply directory", patch.applyDir || "", "mono"],
      ["OPatch log location", logLocation, "mono"],
      ["Error", patchFailureError(report)]
    ])}
    <h5>Captured OPatch Output</h5>
    ${output ? `<pre class="report-log">${escapeHtml(output.slice(-20000))}</pre>` : `<p class="report-empty">No OPatch output was captured.</p>`}
  `);
}

function buildReportHtmlBody(report) {
  const home = report.selectedHome || {};
  const analysis = report.readmeAnalysis || {};
  const isSpbReport = /stack\s+patch\s+bundle|\bspb\b|\bcspu\b/i.test(report.patchType || "") || analysis.patchMethod === "spbat";
  const patchLabel = reportPatchLabel(report);
  const statusTone = reportTone(report.outcome);
  const activity = Array.isArray(report.activityLog) ? report.activityLog.slice(-150) : [];
  return `
    <article class="report-document">
      <header class="report-doc-header">
        <div>
          <p class="report-brand">PatchPilot</p>
          <h3>Oracle Patch Execution Report</h3>
          <p>Generated ${escapeHtml(reportDate(report.generatedAt))}</p>
        </div>
        ${reportBadge(report.outcome, statusTone)}
      </header>

      ${reportSection("Executive Summary", reportGrid([
        ["Outcome", report.outcome],
        ["Patch", patchLabel],
        ["Patch Type", reportPatchType(report)],
        ["Application Mode", report.applyMode === "batch" ? "Batch one-offs - OPatch napply" : isSpbReport ? "SPBAT" : "Single patch - OPatch apply"],
        ["Run ID", report.runId, "mono"],
        ["SSH Target", report.sshTarget, "mono"],
        ["Rollback", report.rollbackAvailable ? "Available" : "Not available"]
      ]))}

      ${reportSection("Target Home", reportGrid([
        ["Product", home.product || home.label],
        ["Inventory Detection", home.detectedProduct || home.product || home.label],
        ["ORACLE_HOME", home.oracleHome, "mono"],
        ["DOMAIN_HOME", home.domainHome || "Not discovered", "mono"],
        ["INSTANCE_HOME", home.instanceHome || "Not applicable", "mono"],
        ["OPatch Version", home.opatchVersion],
        ["OPatch Heap Options", report.opatchHeapOptions || "", "mono"],
        ["Service Inventory", reportServiceInventory(report)],
        ["Service Validation", reportServiceValidation(report)]
      ]))}

      ${reportSection("README Analysis", `
        ${reportGrid([
          ["README Path", report.readmePath, "mono"],
          ["Minimum OPatch", analysis.minimumOpatch],
          ["Backup Scope", analysis.backupTargets],
          ["Inactive patch cleanup", analysis.inactivePatchCleanupRecommended ? "Review required" : "Not flagged"],
          ["Java prerequisite", analysis.javaPrereqRecommended ? "Review required" : "Not flagged"],
          ["OPatch heap setting", analysis.opatchHeapRecommended ? "Review required" : "Not flagged"],
          ["Manual Postinstall", report.postinstallRequiresManual ? "Required" : "Not flagged"]
        ])}
        <h5>Stop Notes</h5>
        ${reportList(analysis.stopSteps, "No stop notes were captured from the README.")}
        <h5>Postinstall Instructions</h5>
        ${reportInstructionList(analysis.postinstallSteps, analysis.postinstallManualSteps)}
      `)}

      ${reportSection("Backup", `
        ${reportGrid([
          ["Backup gate", report.backupGateComplete ? "Complete" : "Incomplete"],
          ["Services stopped before file backup", report.backupShutdownSatisfied ? "Yes" : "No"],
          ["File backup status", report.backupCompleted ? "PatchPilot backup complete" : report.backupExternalApproved ? "External backup confirmed" : "Not completed"],
          ["Backup directory", report.backupDirectory, "mono"],
          ["Database backup required", report.databaseBackupRequired ? "Yes" : "No"],
          ["Database backup confirmed", report.databaseBackupRequired ? report.databaseBackupConfirmed ? "Yes" : "No" : "Not applicable"],
          ["Database backup reference", report.databaseBackupNote || ""],
          ["Preflight confirmed", report.backupPreflightConfirmed],
          ["External backup note", report.backupExternalNote || ""]
        ])}
        ${reportTable(["Target", "Status", "Source", "Source Size", "Archive", "Archive Size"], reportBackupRows(report), "No backup files or planned backup files were recorded.")}
      `)}

      ${reportSection("Shutdown And Startup", reportGrid([
        ["Backup shutdown verified", isSpbReport ? report.backupShutdownVerified : "Same as shutdown gate"],
        ["Backup shutdown override accepted", isSpbReport ? report.backupShutdownOverrideApproved : "Same as shutdown gate"],
        ["Shutdown verified", report.shutdownVerified],
        ["Shutdown override accepted", report.shutdownOverrideApproved],
        ["Services-up verified", isSpbReport ? report.servicesUpVerified : "Not applicable"],
        ["Services-up manually accepted", isSpbReport ? report.servicesUpManualAccepted : "Not applicable"],
        ["Services-up status", isSpbReport ? report.servicesUpStatus || "" : "Not applicable"],
        ["Fresh install without domain", isSpbReport ? report.spbFreshInstallNoDomain : "Not applicable"],
        ["Startup confirmed", report.startupConfirmed],
        ["Startup note", report.startupNote || ""]
      ]))}

      ${isSpbReport ? reportSection("SPBAT Phases", `
        ${reportGrid([
          ["SPB install type", report.spbInstallType ? String(report.spbInstallType).toUpperCase() : ""],
          ["SPBAT extra arguments", report.spbExtraArgs || "None", "mono"],
          ["OPatch heap options", report.opatchHeapOptions || "", "mono"],
          ["SPBAT log directory", report.spbLogDir, "mono"],
          ["SPB prepared", report.spbPrepared],
          ["OIG profile ready", report.oigProfileReady],
          ["OIG profile path", report.oigProfilePath || "", "mono"],
          ["OIG passwords confirmed", report.oigPasswordsConfirmed],
          ["OIG manual actions accepted", report.oigManualAccepted],
          ["OIG script status", report.oigScriptStatus || ""],
          ["OIG script log", report.oigScriptLogPath || "", "mono"]
        ])}
        <h5>Inactive Patch Cleanup</h5>
        ${reportTable(["Field", "Value"], reportSpbInactiveRows(report))}
        <h5>Pre-start Tmp/Cache Cleanup</h5>
        ${reportTable(["Field", "Value"], reportSpbCleanupRows(report))}
        ${reportTable(["Attempt", "Recorded", "Status", "Items Cleared", "Already Empty", "Error"], reportSpbCleanupAttemptRows(report), "No cleanup attempts were recorded.")}
        ${reportTable(["Requested Path", "Resolved Directory", "Status", "Items", "Error"], reportSpbCleanupTargetRows(report), "No tmp/cache cleanup paths were recorded.")}
        <h5>Phase Execution</h5>
        ${reportTable(["Phase", "Status", "Phase Log", "Error"], reportSpbRows(report))}
      `) : reportSection("OPatch Apply", reportTable(["Field", "Value"], reportPatchRows(report)))}

      ${!isSpbReport && report.outcome === "failed" ? reportSupportHandoff(report) : ""}
      ${isSpbReport && report.outcome === "failed" && reportSpbInactiveFailure(report) ? reportSpbInactiveSupportHandoff(report) : ""}

      ${reportSection("Rollback", reportTable(["Field", "Value"], reportRollbackRows(report)))}

      ${reportSection("Commands", reportCommandList(report.commands))}

      ${reportSection("Step Status", `
        ${reportGrid([
          ["Completed steps", reportStepNames(report, "completedSteps")],
          ["Failed steps", reportStepNames(report, "failedSteps")]
        ])}
      `)}

      ${reportSection("Activity Log", activity.length ? `<pre class="report-log">${escapeHtml(activity.join("\n"))}</pre>` : `<p class="report-empty">No activity log entries were captured.</p>`)}
    </article>
  `;
}

function reportDocumentStyles() {
  return `
    body { margin: 0; background: #f7f5f2; color: #161513; font-family: Arial, Helvetica, sans-serif; }
    .report-document { max-width: 1120px; margin: 28px auto; background: #fffdfb; border: 1px solid #d8d1c9; border-radius: 12px; padding: 28px; box-shadow: 0 16px 40px rgba(22, 21, 19, 0.08); }
    .report-doc-header { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; border-bottom: 1px solid #d8d1c9; padding-bottom: 18px; margin-bottom: 18px; }
    .report-brand { margin: 0 0 4px; color: #c74634; text-transform: uppercase; letter-spacing: 0; font-weight: 700; font-size: 12px; }
    h3 { margin: 0 0 8px; font-size: 32px; line-height: 1.1; }
    h4 { margin: 0 0 12px; font-size: 19px; }
    h5 { margin: 16px 0 8px; font-size: 13px; text-transform: uppercase; letter-spacing: 0; color: #665f58; }
    p { color: #665f58; line-height: 1.45; }
    .report-section { margin-top: 20px; }
    .report-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .report-grid div { border: 1px solid #d8d1c9; border-radius: 8px; background: #fbf9f7; padding: 11px; display: grid; gap: 5px; }
    .report-grid span { color: #665f58; text-transform: uppercase; letter-spacing: 0; font-size: 11px; }
    .report-grid strong, .report-table-wrap td { overflow-wrap: anywhere; }
    .mono, code, pre { font-family: Consolas, "Cascadia Mono", monospace; }
    .report-status { display: inline-flex; align-items: center; justify-content: center; min-width: 110px; border-radius: 999px; padding: 7px 12px; text-transform: uppercase; letter-spacing: 0; font-weight: 700; font-size: 11px; border: 1px solid #d8d1c9; }
    .report-status.is-good { background: #e7f3ea; color: #245b38; border-color: #a9cfb4; }
    .report-status.is-warning { background: #fff3d8; color: #7c5511; border-color: #dfbe78; }
    .report-status.is-danger { background: #fde8e3; color: #9d3222; border-color: #e5a89e; }
    .report-status.is-neutral { background: #f3efeb; color: #4e4944; }
    .report-callout { border: 1px solid #d8d1c9; border-radius: 8px; padding: 12px; margin-bottom: 12px; background: #fbf9f7; }
    .report-callout.is-danger { background: #fde8e3; border-color: #e5a89e; }
    .report-callout h5 { margin-top: 0; }
    .report-list { margin: 0; padding-left: 20px; color: #2f2b28; }
    .report-instruction-list { display: grid; gap: 10px; margin: 0; padding-left: 22px; }
    .report-instruction-list li { border: 1px solid #d8d1c9; border-radius: 8px; background: #fbf9f7; padding: 12px; }
    .report-instruction-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
    .report-command-list { display: grid; gap: 8px; }
    .report-command-list code { display: block; border: 1px solid #d8d1c9; border-radius: 8px; background: #fbf9f7; padding: 10px; overflow-wrap: anywhere; white-space: pre-wrap; }
    .report-table-wrap { overflow-x: auto; border: 1px solid #d8d1c9; border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; vertical-align: top; padding: 10px; border-bottom: 1px solid #eee7df; }
    th { color: #665f58; text-transform: uppercase; letter-spacing: 0; font-size: 11px; background: #f3efeb; }
    tr:last-child td { border-bottom: 0; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; background: #161513; color: #f7f0e7; border-radius: 8px; padding: 12px; line-height: 1.45; }
    .report-empty { margin: 0; border: 1px dashed #d8d1c9; border-radius: 8px; padding: 12px; background: #fbf9f7; }
    @media (max-width: 760px) { .report-document { margin: 12px; padding: 18px; } .report-doc-header, .report-grid { display: grid; grid-template-columns: 1fr; } h3 { font-size: 24px; } }
    @media print { body { background: #fff; } .report-document { box-shadow: none; margin: 0; border: 0; } }
  `;
}

function buildReportHtmlDocument(report) {
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>${escapeHtml(report.runId)} - PatchPilot Report</title>
    <style>${reportDocumentStyles()}</style>
  </head>
  <body>
    ${buildReportHtmlBody(report)}
  </body>
</html>`;
}

function currentReportOutcome() {
  if (state.rollbackInProgress) return "rollback in progress";
  if (state.rollbackDone) return "rolled back";
  if (state.rollbackResult && state.rollbackResult.status === "manual-backout-required") return "rollback required";
  if (state.rollbackResult && state.rollbackResult.status === "failed") return "rollback failed";
  if (state.patchApplied) return "completed";
  if (state.failed.has("apply")) return "failed";
  return "draft";
}

function reportForDisplay() {
  const outcome = currentReportOutcome();
  if (state.report && state.report.outcome === outcome && !state.rollbackInProgress) {
    return state.report;
  }
  return buildReport(outcome);
}

function renderReportStep() {
  const report = reportForDisplay();
  const rollbackRunning = report.outcome === "rollback in progress";
  const rolledBack = report.outcome === "rolled back";
  const rollbackRequired = report.outcome === "rollback required";
  const reportFailed = report.outcome === "failed" || report.outcome === "rollback failed";
  const persistedReportIsCurrent = Boolean(state.report && state.report.outcome === report.outcome);
  const reportReady = persistedReportIsCurrent && !rollbackRunning;
  const patchLabel = completedPatchLabel();
  const completedSuccessfully = reportReady && report.outcome === "completed";
  const completionTitle = completedSuccessfully
    ? `Congratulations, patch ${patchLabel} has been successfully completed.`
    : rollbackRunning
      ? `Rollback is in progress for patch ${patchLabel}.`
      : rolledBack
        ? `Patch ${patchLabel} was rolled back successfully.`
        : rollbackRequired
          ? `SPBAT backout is required for ${patchLabel}.`
          : reportFailed
            ? `Patch ${patchLabel} needs review.`
            : "Build the final report when the run is ready.";
  const completionCopy = completedSuccessfully
    ? "PatchPilot verified the run gates and recorded the final patching report. You can download the report, close this browser tab, or start another patch run."
    : rollbackRunning
      ? "PatchPilot is running OPatch rollback and tailing the remote output. The final HTML report will refresh after inventory verification completes."
      : rolledBack
        ? "PatchPilot verified OPatch inventory after rollback and rebuilt the HTML report with rollback status."
        : rollbackRequired
          ? "SPBAT does not provide automated rollback support. Restore from the confirmed backups or follow Oracle Support guidance, then review existing one-offs that may need to be reapplied."
          : reportFailed && reportReady
            ? "PatchPilot generated a support-ready failure report. Ask the customer to create an Oracle Support SR and attach the downloaded HTML report."
            : reportReady
            ? "PatchPilot generated the final report for this run. Review it, download it, or start another patch run."
            : "The report will summarize the target home, patch details, backups, shutdown/startup confirmations, commands, results, and rollback status.";
  const completionClass = completedSuccessfully
    ? "is-success"
    : rollbackRunning || rolledBack || rollbackRequired
      ? "is-warning"
      : reportFailed
        ? "is-danger"
        : reportReady
          ? "is-ready"
          : "";
  return `
    <div class="step-heading">
      <p class="page-breadcrumb">Run Complete</p>
      <h2>Final Report</h2>
    </div>
    <div class="completion-panel ${completionClass}">
      <div>
        <p class="page-breadcrumb">${rollbackRunning ? "Rollback Running" : rolledBack ? "Rollback Complete" : rollbackRequired ? "Backout Required" : reportReady ? "Patching Run Complete" : "Final Step"}</p>
        <h3>${escapeHtml(completionTitle)}</h3>
        <p>${escapeHtml(completionCopy)}</p>
      </div>
      ${reportReady ? `
        <div class="completion-actions">
          <button id="reportDownloadInlineButton" class="button button-secondary" type="button">Download HTML Report</button>
          <button id="reportAnotherInlineButton" class="button button-primary" type="button">Start Another Patch</button>
        </div>
      ` : ""}
    </div>
    <div class="detail-grid">
      <div><span>Outcome</span><strong>${escapeHtml(report.outcome)}</strong></div>
      <div><span>Target Home</span><strong class="mono">${escapeHtml(report.selectedHome.oracleHome)}</strong></div>
      <div><span>Patch</span><strong>${escapeHtml(patchLabel)}</strong></div>
      <div><span>Patch Type</span><strong>${escapeHtml(report.patchType)}</strong></div>
      <div><span>Command Execution User</span><strong>${escapeHtml(report.executionUser || state.form.runAsUser || state.form.user)}</strong></div>
      <div><span>Rollback</span><strong>${report.rollbackAvailable ? "Available" : "Not available"}</strong></div>
    </div>
    <div class="report-preview">
      ${buildReportHtmlBody(report)}
    </div>
  `;
}

function bindSpbCommandSettingControls() {
  const typeSelect = document.getElementById("spbInstallTypeSelect");
  if (typeSelect) typeSelect.addEventListener("change", () => {
    state.form.spbInstallType = typeSelect.value;
    invalidateSpbCommandSettings();
    render();
  });

  const extraArgsInput = document.getElementById("spbExtraArgsInput");
  if (extraArgsInput) extraArgsInput.addEventListener("input", () => {
    state.form.spbExtraArgs = extraArgsInput.value;
    invalidateSpbCommandSettings({ refreshReadme: false });
    updateVisibleSpbCommandSettings();
    renderControls();
  });

  const heapInput = document.getElementById("opatchHeapInput");
  if (heapInput) heapInput.addEventListener("input", () => {
    state.form.opatchHeapOptions = heapInput.value;
    invalidateSpbCommandSettings({ refreshReadme: false });
    updateVisibleSpbCommandSettings();
    renderControls();
  });
}

function bindStepEvents(id) {
  if (id === "connect") {
    ["host", "port", "user", "runAsUser", "password", "sudoPassword", "patchPath"].forEach((name) => {
      const node = document.getElementById(`${name}Input`);
      if (node) node.addEventListener("input", () => {
        state.form[name] = node.value;
        state.connected = false;
        state.connectionResult = null;
        state.completed.delete("connect");
        const resultNode = document.querySelector(".connect-result");
        if (resultNode) resultNode.remove();
        if (["host", "port", "user", "runAsUser", "password", "sudoPassword"].includes(name)) {
          state.discovered = false;
          state.discoveryError = "";
          discoveredHomes = [];
          state.completed.delete("discover");
          resetAllShutdownGates();
          state.selectedHomeId = "";
          state.selectedHomePath = "";
          resetSpbPhaseState();
          resetPatchApplyState();
        }
        if (name === "patchPath") {
          state.readmeText = "";
          state.readmePath = "";
          state.readmeAnalysis = null;
          state.completed.delete("readme");
          state.failed.delete("readme");
          state.backupDirOverride = "";
          resetBackupPreflight();
          resetDatabaseBackupState();
          resetSpbPhaseState();
          resetPatchApplyState();
        }
        renderControls();
      });
    });
    const sudoAuthSelect = document.getElementById("sudoAuthSelect");
    if (sudoAuthSelect) sudoAuthSelect.addEventListener("change", () => {
      state.form.sudoAuth = sudoAuthSelect.value;
      state.connected = false;
      state.connectionResult = null;
      state.completed.delete("connect");
      state.discovered = false;
      state.discoveryError = "";
      discoveredHomes = [];
      state.completed.delete("discover");
      resetAllShutdownGates();
      state.selectedHomeId = "";
      state.selectedHomePath = "";
      resetSpbPhaseState();
      resetPatchApplyState();
      render();
    });
    const patchType = document.getElementById("patchTypeSelect");
    if (patchType) patchType.addEventListener("change", () => {
      state.form.patchType = patchType.value;
      if (state.form.patchType !== "One-off") state.form.applyMode = "single";
      state.activeStep = Math.min(state.activeStep, workflowSteps().length - 1);
      resetAllShutdownGates();
      resetBackupPreflight();
      resetDatabaseBackupState();
      resetSpbPhaseState();
      render();
    });
    const applyMode = document.getElementById("applyModeSelect");
    if (applyMode) applyMode.addEventListener("change", () => {
      state.form.applyMode = applyMode.value;
      if (isBatchNapply() && dom.autoRollbackToggle) dom.autoRollbackToggle.checked = false;
      resetPatchApplyState();
      render();
    });
    const batchSelection = document.getElementById("batchSelectionSelect");
    if (batchSelection) batchSelection.addEventListener("change", () => {
      state.form.batchSelection = batchSelection.value;
      resetPatchApplyState();
      render();
    });
    const batchPatchIds = document.getElementById("batchPatchIdsInput");
    if (batchPatchIds) batchPatchIds.addEventListener("input", () => {
      state.form.batchPatchIds = batchPatchIds.value;
      resetPatchApplyState();
      renderControls();
    });
    const patchListFile = document.getElementById("patchListFileInput");
    if (patchListFile) patchListFile.addEventListener("input", () => {
      state.form.patchListFile = patchListFile.value;
      resetPatchApplyState();
      renderControls();
    });
  }

  if (id === "discover") {
    document.querySelectorAll("input[name='homeChoice']").forEach((node) => {
      node.addEventListener("change", () => {
        state.selectedHomeId = node.value;
        rememberSelectedHome(discoveredHomes.find((home) => home.id === state.selectedHomeId));
        state.opatchReady = false;
        state.patchApplied = false;
        resetAllShutdownGates();
        state.backupDirOverride = "";
        resetBackupPreflight();
        resetDatabaseBackupState();
        resetSpbPhaseState();
        resetPatchApplyState();
        if (state.readmeText) state.readmeAnalysis = analyzeReadme(state.readmeText);
        render();
      });
    });
  }

  if (id === "readme") {
    const input = document.getElementById("readmeInput");
    if (input) input.addEventListener("input", () => {
      state.readmeText = input.value;
      state.readmePath = "";
      state.readmeAnalysis = null;
      state.completed.delete("readme");
      resetBackupPreflight();
      resetPostinstallState();
      renderControls();
    });
    bindSpbCommandSettingControls();
  }

  if (id === "spbUp") {
    const checkbox = document.getElementById("servicesUpConfirmedInput");
    if (checkbox) checkbox.addEventListener("change", () => {
      state.servicesUpConfirmed = checkbox.checked;
      if (!state.servicesUpConfirmed) {
        state.servicesUpVerified = false;
        state.servicesUpManualAccepted = false;
        state.completed.delete("spbUp");
        if (state.servicesUpStatus === "verified") state.servicesUpStatus = state.servicesUpCheck ? "found" : "idle";
      } else {
        const running = state.servicesUpCheck && Array.isArray(state.servicesUpCheck.running) ? state.servicesUpCheck.running : [];
        if (running.length) {
          state.servicesUpVerified = true;
          state.servicesUpManualAccepted = false;
          state.servicesUpStatus = "verified";
          state.servicesUpError = "";
          state.completed.add("spbUp");
          resetShutdownGate("shutdown");
          log(`Services-up gate passed with ${running.length} selected-home service group(s) running.`, "pass");
        } else {
          resetShutdownGate("shutdown");
        }
      }
      render();
    });
    const freshInstallInput = document.getElementById("spbFreshInstallInput");
    if (freshInstallInput) freshInstallInput.addEventListener("change", () => {
      state.spbFreshInstallNoDomain = freshInstallInput.checked;
      if (state.spbFreshInstallNoDomain) {
        state.servicesUpConfirmed = false;
        state.servicesUpVerified = false;
        state.servicesUpManualAccepted = false;
        state.servicesUpStatus = "skipped";
        state.servicesUpError = "";
        state.servicesUpCheck = null;
        resetShutdownGate("shutdown");
        state.spbPrestopDone = false;
        state.spbPrestopExternalApproved = false;
        state.spbPrestopExternalNote = "";
        state.failed.delete("spbUp");
        state.failed.delete("spbPrestop");
        state.completed.add("spbUp");
        log("Fresh install path selected. Services Up is skipped; SPBAT PreStop baseline still runs once to create the status logs required by Downtime.", "warn");
      } else {
        resetShutdownGate("shutdown");
        state.completed.delete("spbUp");
        state.servicesUpManualAccepted = false;
        state.servicesUpStatus = "idle";
        state.servicesUpError = "";
        state.servicesUpCheck = null;
        if (!state.spbPrestopDone) state.completed.delete("spbPrestop");
        log("Fresh install path cleared. Services Up and SPBAT PreStop are required again.", "warn");
      }
      render();
    });
    const manualButton = document.getElementById("servicesUpManualButton");
    if (manualButton) manualButton.addEventListener("click", acceptServicesUpManual);
  }

  if (id === "spbPrepare") {
    bindSpbCommandSettingControls();
    const logDirInput = document.getElementById("spbLogDirInput");
    if (logDirInput) logDirInput.addEventListener("input", () => {
      state.form.spbLogDir = logDirInput.value;
      state.spbPrepared = false;
      state.spbPrepareResult = null;
      state.spbPrepareStatus = "idle";
      state.spbPrepareError = "";
      state.completed.delete("spbPrepare");
      renderControls();
    });
  }

  if (id === "spbPrestop") {
    const externalInput = document.getElementById("spbPrestopExternalInput");
    if (externalInput) externalInput.addEventListener("change", () => {
      state.spbPrestopExternalApproved = externalInput.checked;
      if (state.spbPrestopExternalApproved) {
        state.completed.add("spbPrestop");
        state.failed.delete("spbPrestop");
        state.spbPhaseErrors.prestop = "";
        if (state.spbPhaseStatus.prestop === "failed") state.spbPhaseStatus.prestop = "idle";
        log("External PreStop confirmation accepted for this run.", "warn");
      } else if (!state.spbPrestopDone) {
        state.completed.delete("spbPrestop");
      }
      render();
    });
    const noteInput = document.getElementById("spbPrestopExternalNoteInput");
    if (noteInput) noteInput.addEventListener("input", () => {
      state.spbPrestopExternalNote = noteInput.value;
    });
  }

  if (id === "shutdown" || id === "spbBackupShutdown") {
    const meta = shutdownGateMeta(id);
    const checkbox = document.getElementById("customerStoppedInput");
    if (checkbox) checkbox.addEventListener("change", () => {
      state[meta.customerStoppedKey] = checkbox.checked;
      if (!state[meta.customerStoppedKey]) {
        resetShutdownGate(meta.stepId);
      }
      render();
    });
    document.querySelectorAll(".shutdown-service-input").forEach((node) => {
      node.addEventListener("change", () => {
        const key = node.dataset.shutdownKey;
        if (!key) return;
        if (node.checked) {
          state[meta.selectionsKey].add(key);
        } else {
          state[meta.selectionsKey].delete(key);
        }
      });
    });
    const planButton = document.getElementById("shutdownPlanButton");
    if (planButton) planButton.addEventListener("click", generateShutdownCommands);
    const killButton = document.getElementById("shutdownKillButton");
    if (killButton) killButton.addEventListener("click", killSelectedProcesses);
    const overrideButton = document.getElementById("shutdownOverrideButton");
    if (overrideButton) overrideButton.addEventListener("click", approveShutdownOverride);
  }

  if (id === "backup") {
    const backupDirInput = document.getElementById("backupDirInput");
    if (backupDirInput) backupDirInput.addEventListener("input", () => {
      const previousDir = backupDirForRun();
      const value = backupDirInput.value.trim();
      const defaultDir = defaultBackupDirForRun();
      const nextOverride = value && value !== defaultDir ? value : "";
      const nextDir = nextOverride || defaultDir;
      state.backupDirOverride = nextOverride;
      if (nextDir !== previousDir || state.backupPreflight || state.backupPreflightConfirmed || state.backupResult || state.backupsDone) {
        resetBackupPreflight();
        state.backupDestinationDirty = true;
        const notice = document.getElementById("backupDestinationChangedNotice");
        if (notice) notice.hidden = false;
        const preflightPanel = document.getElementById("backupPreflightPanel");
        if (preflightPanel) preflightPanel.hidden = true;
      }
      renderControls();
    });
    if (backupDirInput) backupDirInput.addEventListener("change", () => {
      if (state.backupDestinationDirty) render();
    });
    const preflightConfirmInput = document.getElementById("backupPreflightConfirmInput");
    if (preflightConfirmInput) preflightConfirmInput.addEventListener("change", () => {
      state.backupPreflightConfirmed = preflightConfirmInput.checked;
      if (state.backupPreflightConfirmed) {
        log("Backup destination and free space check confirmed.", "pass");
      }
      renderControls();
    });
    const externalInput = document.getElementById("backupExternalInput");
    if (externalInput) externalInput.addEventListener("change", () => {
      state.backupExternalApproved = externalInput.checked;
      if (state.backupExternalApproved) {
        log("External backup confirmation accepted for this run.", "warn");
      }
      syncBackupCompletion();
      render();
    });
    const noteInput = document.getElementById("backupExternalNoteInput");
    if (noteInput) noteInput.addEventListener("input", () => {
      state.backupExternalNote = noteInput.value;
    });
    const databaseBackupConfirmInput = document.getElementById("databaseBackupConfirmInput");
    if (databaseBackupConfirmInput) databaseBackupConfirmInput.addEventListener("change", () => {
      state.databaseBackupConfirmed = databaseBackupConfirmInput.checked;
      if (state.databaseBackupConfirmed) {
        log("Database backup confirmation accepted for this run.", "pass");
      }
      syncBackupCompletion();
      render();
    });
    const databaseBackupNoteInput = document.getElementById("databaseBackupNoteInput");
    if (databaseBackupNoteInput) databaseBackupNoteInput.addEventListener("input", () => {
      state.databaseBackupNote = databaseBackupNoteInput.value;
    });
  }

  if (id === "opatch") {
    const input = document.getElementById("opatchBundleInput");
    if (input) input.addEventListener("input", () => {
      state.form.opatchBundlePath = input.value;
    });
  }

  if (id === "spbInactive") {
    const retainInput = document.getElementById("spbInactiveRetainInput");
    if (retainInput) retainInput.addEventListener("input", () => {
      state.spbInactiveRetainLevel = retainInput.value;
      state.spbInactiveCheck = null;
      state.spbInactiveResult = null;
      state.spbInactiveReviewed = false;
      state.spbInactiveRemoveConfirmed = false;
      state.spbInactiveSkipConfirmed = false;
      state.completed.delete("spbInactive");
      renderControls();
    });
    if (retainInput) retainInput.addEventListener("change", () => {
      state.spbInactiveRetainLevel = String(spbInactiveRetainLevel());
      render();
    });
    const removeInput = document.getElementById("spbInactiveRemoveConfirmInput");
    if (removeInput) removeInput.addEventListener("change", () => {
      state.spbInactiveRemoveConfirmed = removeInput.checked;
      if (state.spbInactiveRemoveConfirmed) {
        state.spbInactiveSkipConfirmed = false;
        state.spbInactiveReviewed = false;
        state.spbInactiveError = "";
        state.completed.delete("spbInactive");
        state.failed.delete("spbInactive");
      }
      render();
    });
    const skipInput = document.getElementById("spbInactiveSkipInput");
    if (skipInput) skipInput.addEventListener("change", () => {
      state.spbInactiveSkipConfirmed = skipInput.checked;
      if (state.spbInactiveSkipConfirmed) {
        acceptSpbInactiveKeepDecision();
      } else if (!(state.spbInactiveCheck && !state.spbInactiveCheck.hasInactive)) {
        state.spbInactiveReviewed = false;
        state.completed.delete("spbInactive");
        if (spbInactiveCleanupIssue()) {
          state.failed.add("spbInactive");
          state.spbInactiveError = (state.spbInactiveResult && state.spbInactiveResult.error) || "Inactive patch cleanup did not complete.";
        } else {
          state.failed.delete("spbInactive");
        }
      }
      render();
    });
    const keepAndContinueButton = document.getElementById("spbInactiveKeepAndContinueButton");
    if (keepAndContinueButton) keepAndContinueButton.addEventListener("click", () => {
      acceptSpbInactiveKeepDecision({ advance: true });
    });
    const skipOptionalButton = document.getElementById("spbInactiveSkipOptionalButton");
    if (skipOptionalButton) skipOptionalButton.addEventListener("click", () => {
      acceptSpbInactiveKeepDecision({ advance: true });
    });
    const stopButton = document.getElementById("spbInactiveStopButton");
    if (stopButton) stopButton.addEventListener("click", () => stopSpbInactiveOperation(false));
    const stopAndSkipButton = document.getElementById("spbInactiveStopAndSkipButton");
    if (stopAndSkipButton) stopAndSkipButton.addEventListener("click", () => stopSpbInactiveOperation(true));
    const failureReportButton = document.getElementById("spbInactiveFailureReportButton");
    if (failureReportButton) failureReportButton.addEventListener("click", generateSpbInactiveFailureReport);
  }

  if (id === "postinstall" && isStackPatchBundle()) {
    const targetInput = document.getElementById("spbCleanupTargetsInput");
    if (targetInput) targetInput.addEventListener("input", () => {
      state.spbCleanupTargetsText = targetInput.value;
      resetSpbCleanupState({ keepTargets: true });
      const checkbox = document.getElementById("spbCleanupApprovedInput");
      if (checkbox) checkbox.checked = false;
      renderControls();
    });
    const checkbox = document.getElementById("spbCleanupApprovedInput");
    if (checkbox) checkbox.addEventListener("change", () => {
      state.spbCleanupApproved = checkbox.checked;
      if (!checkbox.checked) {
        state.postinstallDone = false;
        state.completed.delete("postinstall");
      }
      renderControls();
    });
  }
  if (id === "postinstall" && !isStackPatchBundle()) {
    const checkbox = document.getElementById("postinstallConfirmedInput");
    if (checkbox) checkbox.addEventListener("change", () => {
      state.postinstallConfirmed = checkbox.checked;
      if (!checkbox.checked) {
        state.postinstallDone = false;
        state.completed.delete("postinstall");
      }
      renderControls();
    });
  }

  if (id === "restart") {
    const checkbox = document.getElementById("startupConfirmedInput");
    if (checkbox) checkbox.addEventListener("change", () => {
      state.startupConfirmed = checkbox.checked;
      if (!checkbox.checked) {
        state.restartDone = false;
        state.completed.delete("restart");
      }
      renderControls();
    });
    const noteInput = document.getElementById("startupNoteInput");
    if (noteInput) noteInput.addEventListener("input", () => {
      state.startupNote = noteInput.value;
    });
  }

  if (id === "spbPoststart") {
    const loadButton = document.getElementById("oigProfileLoadButton");
    if (loadButton) loadButton.addEventListener("click", () => inspectOigProfile(false));
    const saveButton = document.getElementById("oigProfileSaveButton");
    if (saveButton) saveButton.addEventListener("click", saveOigProfile);
    const runButton = document.getElementById("oigRunScriptButton");
    if (runButton) runButton.addEventListener("click", runOigPostinstallScript);
    const tailButton = document.getElementById("oigLogTailButton");
    if (tailButton) tailButton.addEventListener("click", () => tailOigPostinstallLog());
    const manualAccept = document.getElementById("oigManualAcceptInput");
    if (manualAccept) manualAccept.addEventListener("change", async () => {
      if (manualAccept.checked) {
        await acceptOigManualPostinstall({ requireDialog: false });
      } else {
        state.oigManualAccepted = false;
        if (state.oigScriptStatus === "manual") {
          state.oigProfileReady = false;
          state.oigScriptStatus = "idle";
          state.oigScriptDone = false;
          state.oigScriptResult = null;
          state.oigScriptError = "";
          state.spbPoststartDone = false;
          state.completed.delete("spbPoststart");
        }
        render();
      }
    });
    document.querySelectorAll(".oig-profile-input").forEach((input) => {
      input.addEventListener("input", () => {
        state.oigProfileEdits[input.dataset.oigProfileKey] = input.value;
        state.oigProfileSaved = false;
        state.oigManualAccepted = false;
        state.oigScriptDone = false;
        state.oigScriptStatus = "idle";
        state.oigScriptResult = null;
        state.oigScriptError = "";
        state.spbPoststartDone = false;
        state.completed.delete("spbPoststart");
        renderControls();
      });
    });
    const passwordConfirm = document.getElementById("oigPasswordConfirmInput");
    if (passwordConfirm) passwordConfirm.addEventListener("change", () => {
      state.oigPasswordsConfirmed = passwordConfirm.checked;
      state.oigProfileReady = passwordConfirm.checked;
      if (!passwordConfirm.checked) {
        state.oigManualAccepted = false;
        state.oigScriptDone = false;
        state.oigScriptStatus = "idle";
        state.spbPoststartDone = false;
        state.completed.delete("spbPoststart");
      }
      renderControls();
    });
  }

  if (["spbPrestop", "spbDowntime", "spbPoststart"].includes(id)) {
    document.querySelectorAll(".spb-report-button").forEach((button) => {
      button.addEventListener("click", openLatestSpbReport);
    });
  }

  if (id === "apply") {
    const supportReportButton = document.getElementById("applyFailureReportButton");
    if (supportReportButton) supportReportButton.addEventListener("click", generateApplyFailureReport);
  }

  if (id === "report") {
    const downloadButton = document.getElementById("reportDownloadInlineButton");
    if (downloadButton) downloadButton.addEventListener("click", downloadReport);
    const anotherButton = document.getElementById("reportAnotherInlineButton");
    if (anotherButton) anotherButton.addEventListener("click", resetRun);
  }
}

async function runStepAction() {
  const id = currentStep().id;
  const actionBlockReason = stepActionBlockReason(id);
  if (actionBlockReason) {
    log(`Preview only: ${actionBlockReason}`, "warn");
    render();
    return;
  }
  const actions = {
    connect: testSsh,
    discover: discoverEnvironment,
    readme: analyzeReadmeAction,
    spbUp: verifyServicesUp,
    spbPrepare: prepareSpbRun,
    spbBackupShutdown: verifyShutdown,
    spbPrestop: runSpbPrestop,
    shutdown: verifyShutdown,
    backup: takeBackups,
    opatch: validateOpatch,
    spbInactive: runSpbInactivePatchReview,
    spbDowntime: runSpbDowntime,
    apply: applyPatch,
    postinstall: runPostinstall,
    restart: restartServices,
    spbPoststart: runSpbPoststart,
    report: state.report ? downloadReport : finalizeReport
  };
  if (!actions[id]) {
    log(`No action is configured for ${currentStep().label}.`, "error");
    return;
  }
  await actions[id]();
}

async function testSsh() {
  setBusy(true, "Testing SSH");
  state.connectionResult = {
    tone: "info",
    title: "Testing SSH connection",
    message: `Checking ${state.form.user}@${state.form.host}:${state.form.port}, execution as ${state.form.runAsUser || state.form.user}, and validating ${state.form.patchPath}.`
  };
  render();
  try {
    const result = await postJson("api/ssh/test", connectionPayload());
    const validation = result.validation || {};
    const patchPath = validation.patchPath || {};
    const sshTarget = `${state.form.user}@${state.form.host}:${state.form.port}`;
    const remoteUser = validation.user && validation.host ? `${validation.user}@${validation.host}` : sshTarget;
    const details = [
      { label: "SSH Login", value: sshTarget, mono: true },
      { label: "Command Execution", value: `Verified as ${remoteUser}`, mono: true },
      { label: "Patch directory", value: patchPath.resolvedPath || state.form.patchPath || "Not provided", mono: true },
      { label: "Directory check", value: patchPath.ok ? "Exists, readable, and searchable" : (patchPath.message || "Failed") },
      { label: "README check", value: patchPath.readmePath ? patchPath.readmePath : "Not found", mono: Boolean(patchPath.readmePath) }
    ];
    if (!patchPath.ok) {
      state.connected = false;
      state.completed.delete("connect");
      dom.connectionStatus.textContent = "Patch directory check failed";
      state.connectionResult = {
        tone: "danger",
        title: "SSH verified, patch directory failed",
        message: patchPath.message || "Patch Directory on Server could not be validated.",
        details
      };
      log(`SSH connection validated for ${sshTarget}.`, "pass");
      log(patchPath.message || "Patch Directory on Server could not be validated.", "error");
      return;
    }
    state.connected = true;
    state.completed.add("connect");
    dom.connectionStatus.textContent = `SSH and patch path ready for ${sshTarget}`;
    state.connectionResult = {
      tone: "good",
      title: "SSH and patch directory verified",
      message: `PatchPilot can connect to ${sshTarget}, and the patch directory is reachable on the server.`,
      details
    };
    log(`SSH connection validated for ${sshTarget}.`, "pass");
    log(`Patch directory validated: ${patchPath.resolvedPath || state.form.patchPath}.`, "pass");
    if (patchPath.readmePath) log(`README found: ${patchPath.readmePath}.`, "pass");
  } catch (error) {
    state.connected = false;
    state.completed.delete("connect");
    dom.connectionStatus.textContent = "SSH test failed";
    state.connectionResult = {
      tone: "danger",
      title: "SSH or patch directory validation failed",
      message: error.message
    };
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function discoverEnvironment() {
  setBusy(true, "Discovering");
  setStatus("Discovering");
  state.discoveryError = "";
  const previousSelectedPath = homePathKey(state.selectedHomePath || (discoveredHomes.find((home) => home.id === state.selectedHomeId) || {}).oracleHome || "");
  log(`Discovering Oracle homes over SSH on ${state.form.user}@${state.form.host}:${state.form.port}.`);
  try {
    const result = await postJson("api/discover-homes", connectionPayload());
    const rawHomes = (result.inventory && Array.isArray(result.inventory.homes)) ? result.inventory.homes : [];
    discoveredHomes = dedupeDiscoveredHomes(rawHomes);
    state.discovered = true;
    resetAllShutdownGates();
    resetSpbPhaseState();
    resetBackupPreflight();
    if (discoveredHomes.length) {
      const preservedHome = discoveredHomes.find((home) => homeMatchesPath(home, previousSelectedPath));
      const nextHome = preservedHome || discoveredHomes[0];
      state.selectedHomeId = nextHome.id;
      rememberSelectedHome(nextHome);
      state.completed.add("discover");
      setStatus("Homes found", "good");
      log(`Discovered ${discoveredHomes.length} candidate Oracle home(s) on ${result.inventory.host || state.form.host}.`, "pass");
      if (preservedHome) {
        log(`Preserved selected ORACLE_HOME: ${preservedHome.oracleHome}.`, "pass");
      }
      if (rawHomes.length > discoveredHomes.length) {
        log(`Collapsed ${rawHomes.length - discoveredHomes.length} duplicate Oracle home entr${rawHomes.length - discoveredHomes.length === 1 ? "y" : "ies"} from discovery.`, "warn");
      }
    } else {
      state.selectedHomeId = "";
      state.selectedHomePath = "";
      state.completed.delete("discover");
      setStatus("No homes", "danger");
      log(`SSH worked, but no ORACLE_HOME with OPatch was discovered on ${state.form.host}.`, "warn");
    }
  } catch (error) {
    state.discovered = false;
    discoveredHomes = [];
    state.selectedHomeId = "";
    state.selectedHomePath = "";
    state.discoveryError = error.message;
    state.completed.delete("discover");
    setStatus("Discovery failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function analyzeReadmeAction() {
  setBusy(true, "Loading README");
  log(`Loading README from ${state.form.patchPath} on ${state.form.host}.`);
  try {
    const result = await postJson("api/readme/load", {
      ...connectionPayload(),
      patchPath: state.form.patchPath
    });
    state.readmeText = result.readme.text || "";
    state.readmePath = result.readme.readmePath || "";
    const selectedPatchType = state.form.patchType;
    const readmeMethod = detectedReadmePatchMethod(state.readmeText);
    if (readmeMethod === "spbat" && !isSpbPatchTypeLabel(state.form.patchType)) {
      state.form.patchType = /\bcspu\b|critical\s+stack\s+patch\s+update/i.test(state.readmeText) ? "CSPU" : "Stack Patch Bundle";
      resetSpbPhaseState();
      resetPatchApplyState();
      log(`README indicates ${state.form.patchType} / SPBAT. PatchPilot switched from ${selectedPatchType} to the SPBAT workflow.`, "warn");
    } else if (readmeMethod === "opatch" && isSpbPatchTypeLabel(state.form.patchType) && !pathIndicatesSpbat(state.form.patchPath)) {
      state.form.patchType = "One-off";
      resetSpbPhaseState();
      resetPatchApplyState();
      log(`README indicates a standard OPatch flow. PatchPilot switched from ${selectedPatchType} to the OPatch workflow.`, "warn");
    }
    state.readmeAnalysis = analyzeReadme(state.readmeText);
    resetDatabaseBackupState();
    syncBackupCompletion();
    resetPostinstallState();
    state.completed.add("readme");
    log(`Loaded README from ${state.readmePath}.`, "pass");
    log(`README analyzed. Minimum OPatch ${state.readmeAnalysis.minimumOpatch}; backup scope ${state.readmeAnalysis.backupTargets.join(", ")}.`, "pass");
  } catch (error) {
    state.readmeAnalysis = null;
    state.completed.delete("readme");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

function acceptServicesUpManual() {
  if (!state.servicesUpConfirmed) {
    log("Confirm all required services are running before manually accepting the Services Up gate.", "warn");
    render();
    return;
  }
  state.servicesUpManualAccepted = true;
  state.servicesUpVerified = false;
  state.servicesUpStatus = "manual";
  state.servicesUpError = "";
  resetShutdownGate("shutdown");
  state.completed.add("spbUp");
  state.failed.delete("spbUp");
  setStatus("Services up accepted", "good");
  log("Manual Services Up confirmation accepted. PatchPilot did not detect matching selected-home processes, so this decision will be recorded for the run.", "warn");
  render();
}

async function verifyServicesUp() {
  if (state.spbFreshInstallNoDomain) {
    state.servicesUpVerified = false;
    state.servicesUpManualAccepted = false;
    state.servicesUpStatus = "skipped";
    state.servicesUpError = "";
    state.servicesUpCheck = null;
    resetShutdownGate("shutdown");
    state.completed.add("spbUp");
    setStatus("Fresh install path", "good");
    log("Services Up check skipped because this is a fresh install before domain creation.", "warn");
    render();
    return;
  }
  setBusy(true, "Checking services");
  const home = selectedHome();
  state.servicesUpStatus = "checking";
  state.servicesUpError = "";
  state.servicesUpCheck = null;
  state.servicesUpVerified = false;
  state.servicesUpManualAccepted = false;
  state.completed.delete("spbUp");
  log(`Checking services are up on ${state.form.host} for ${home.oracleHome}.`);
  render();
  try {
    const result = await postJson("api/services-up/verify", {
      ...connectionPayload(),
      home: {
        oracleHome: home.oracleHome,
        domainHome: home.domainHome,
        instanceHome: home.instanceHome,
        product: home.product || "",
        label: home.label || "",
        services: Array.isArray(home.services) ? home.services : []
      },
      expectedServices: Array.isArray(home.services) ? home.services : []
    });
    state.servicesUpCheck = result.servicesUp || result.shutdown || null;
    const running = state.servicesUpCheck && Array.isArray(state.servicesUpCheck.running) ? state.servicesUpCheck.running : [];
    if (!running.length) {
      state.servicesUpStatus = "missing";
      state.servicesUpError = "No selected-home services were found running.";
      state.servicesUpVerified = false;
      state.completed.delete("spbUp");
      setStatus("Services down", "danger");
      log("No selected-home services were found running. SPBAT PreStop should not run until services are up.", "error");
    } else if (!state.servicesUpConfirmed) {
      state.servicesUpStatus = "found";
      state.servicesUpError = "";
      state.servicesUpVerified = false;
      state.completed.delete("spbUp");
      setStatus("Confirm services", "danger");
      log(`Found ${running.length} selected-home service group(s) running. Confirm the full IDM stack is up, then verify again.`, "warn");
    } else {
      state.servicesUpStatus = "verified";
      state.servicesUpError = "";
      state.servicesUpVerified = true;
      state.servicesUpManualAccepted = false;
      resetShutdownGate("shutdown");
      state.completed.add("spbUp");
      setStatus("Services up", "good");
      log(`Services-up gate passed with ${running.length} selected-home service group(s) running.`, "pass");
    }
  } catch (error) {
    state.servicesUpStatus = "failed";
    state.servicesUpError = error.message;
    state.servicesUpVerified = false;
    state.completed.delete("spbUp");
    setStatus("Service check failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function prepareSpbRun() {
  const typeSelect = document.getElementById("spbInstallTypeSelect");
  if (typeSelect) state.form.spbInstallType = typeSelect.value;
  const extraArgsInput = document.getElementById("spbExtraArgsInput");
  if (extraArgsInput) state.form.spbExtraArgs = extraArgsInput.value;
  const heapInput = document.getElementById("opatchHeapInput");
  if (heapInput) state.form.opatchHeapOptions = heapInput.value;
  const logDirInput = document.getElementById("spbLogDirInput");
  if (logDirInput) state.form.spbLogDir = logDirInput.value;

  setBusy(true, "Preparing SPBAT");
  state.spbPrepareStatus = "running";
  state.spbPrepareError = "";
  state.spbPrepareResult = null;
  state.spbPrepared = false;
  state.completed.delete("spbPrepare");
  state.failed.delete("spbPrepare");
  log(`Preparing SPBAT ${spbInstallType().toUpperCase()} run and log directory on ${state.form.host}.`);
  if (String(state.form.spbInstallType || "auto").toLowerCase() === "auto") {
    log(`SPBAT Auto type resolved to ${spbInstallType().toUpperCase()} for ${selectedHome().oracleHome}.`, "pass");
  }
  if (spbExtraArgs()) {
    log(`Additional SPBAT arguments configured: ${spbExtraArgs()}.`, "warn");
  }
  log(`OPatch JVM heap options configured: OPATCH_JRE_MEMORY_OPTIONS=${opatchHeapOptions()}.`, "warn");
  render();
  try {
    const result = await postJson("api/spb/prepare", {
      ...connectionPayload(),
      patchPath: state.form.patchPath,
      logDir: spbLogDir(),
      changeRef: state.form.changeRef
    });
    state.spbPrepareResult = result.spb;
    state.form.spbLogDir = result.spb.logDir || spbLogDir();
    const candidates = Array.isArray(result.spb.opatchCandidates) ? result.spb.opatchCandidates : [];
    const readmes = Array.isArray(result.spb.opatchReadmes) ? result.spb.opatchReadmes : [];
    if (candidates.length) state.form.opatchBundlePath = candidates[0];
    state.spbPrepared = true;
    state.spbPrepareStatus = "succeeded";
    state.spbPrepareError = "";
    state.completed.add("spbPrepare");
    state.failed.delete("spbPrepare");
    setStatus("SPBAT ready", "good");
    log(`SPBAT script verified at ${result.spb.spbatScript}.`, "pass");
    log(`SPBAT log directory ready: ${result.spb.logDir}.`, "pass");
    if (candidates.length) log(`Found OPatch upgrade candidate in SPB download: ${candidates[0]}.`, "pass");
    if (readmes.length) log(`Found OPatch upgrade README: ${readmes[0]}.`, "pass");
  } catch (error) {
    state.spbPrepared = false;
    state.spbPrepareStatus = "failed";
    state.spbPrepareError = error.message;
    state.completed.delete("spbPrepare");
    state.failed.add("spbPrepare");
    setStatus("SPBAT setup failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

function currentSpbReportPhase() {
  const id = currentStep().id;
  if (id === "spbPrestop") return "prestop";
  if (id === "spbDowntime") return "downtime";
  if (id === "spbPoststart") return "poststart";
  return "";
}

function spbTextArtifactHtml(title, path, text) {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>${escapeHtml(title)}</title>
  <style>
    body { margin: 0; background: #f7f5f2; color: #1f1f1f; font-family: Arial, sans-serif; }
    header { padding: 18px 22px; border-bottom: 1px solid #d8d0c7; background: white; }
    h1 { margin: 0 0 8px; font-size: 20px; }
    p { margin: 0; color: #5f5750; }
    pre { margin: 0; padding: 18px 22px; white-space: pre-wrap; word-break: break-word; font: 12px/1.5 Consolas, "Courier New", monospace; }
  </style>
</head>
<body>
  <header>
    <h1>${escapeHtml(title)}</h1>
    <p>${escapeHtml(path || "Captured SPBAT output")}</p>
  </header>
  <pre>${escapeHtml(text || "No log output was captured.")}</pre>
</body>
</html>`;
}

function closeArtifactWindow(viewer) {
  try {
    if (viewer && !viewer.closed) viewer.close();
  } catch (error) {
    // Best effort only; some browsers disallow script closing.
  }
}

function writeArtifactWindow(viewer, html) {
  if (!viewer || viewer.closed) return false;
  try {
    viewer.document.open();
    viewer.document.write(html);
    viewer.document.close();
    return true;
  } catch (error) {
    return false;
  }
}

function openHtmlArtifact(html, fallbackTitle, viewer = null) {
  const blob = new Blob([html], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  let opened = null;
  if (viewer && !viewer.closed) {
    try {
      viewer.location.href = url;
      opened = viewer;
    } catch (error) {
      opened = null;
    }
  }
  if (!opened) opened = window.open(url, "_blank", "noopener");
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
  if (!opened) {
    log(`${fallbackTitle || "SPBAT artifact"} was prepared, but the browser blocked the popup. Allow popups for PatchPilot and try again.`, "warn");
  }
}

function normalizeSpbReportStatus(status) {
  const text = String(status || "").toLowerCase();
  if (/success|succeed|succeeded|successful|complete|completed|passed/.test(text)) return "succeeded";
  if (/fail|failed|failure|error/.test(text)) return "failed";
  return "";
}

function applySpbReportOutcome(phase, report, reason = "latest SPBAT report") {
  if (!phase || !report) return "";
  if (Object.prototype.hasOwnProperty.call(report, "phaseMatched") && report.phaseMatched === false) {
    const reportPhase = report.reportPhase || report.latestLogPhase || "another phase";
    log(`${spbPhaseLabel(phase)} was not marked complete because the ${reason} belongs to ${reportPhase}. Run or open the ${spbPhaseLabel(phase)} report for this phase.`, "warn");
    return "";
  }
  const status = normalizeSpbReportStatus(report.reportStatus);
  if (!status) return "";
  const phaseName = spbPhaseLabel(phase);
  const summaryLines = Array.isArray(report.summaryLines) ? report.summaryLines : [];
  if (report.reportPath) state.spbPhaseLogs[phase] = report.reportPath;
  state.spbPhaseJobs[phase] = {
    ...(state.spbPhaseJobs[phase] || {}),
    status,
    startedAt: (state.spbPhaseJobs[phase] && state.spbPhaseJobs[phase].startedAt) || nowSeconds(),
    finishedAt: (state.spbPhaseJobs[phase] && state.spbPhaseJobs[phase].finishedAt) || nowSeconds()
  };

  if (status === "succeeded") {
    state.spbPhaseStatus[phase] = "succeeded";
    state.spbPhaseErrors[phase] = "";
    if (phase === "prestop") {
      state.spbPrestopDone = true;
      state.completed.add("spbPrestop");
      state.failed.delete("spbPrestop");
    } else if (phase === "downtime") {
      state.spbDowntimeDone = true;
      state.patchApplied = true;
      state.completed.add("spbDowntime");
      state.failed.delete("spbDowntime");
    } else if (phase === "poststart") {
      if (spbInstallType() === "oig" && !state.oigScriptDone) {
        state.spbPoststartDone = false;
        state.completed.delete("spbPoststart");
        state.failed.delete("spbPoststart");
        setStatus("OIG postinstall pending", "danger");
      } else {
        state.spbPoststartDone = true;
        state.completed.add("spbPoststart");
        state.failed.delete("spbPoststart");
      }
    }
    if (!(phase === "poststart" && spbInstallType() === "oig" && !state.oigScriptDone)) {
      setStatus(`${phaseName} complete`, "good");
    }
    log(`${phaseName} marked complete from ${reason}${report.reportPath ? `: ${report.reportPath}` : ""}.`, "pass");
    return status;
  }

  state.spbPhaseStatus[phase] = "failed";
  state.spbPhaseErrors[phase] = summaryLines.length ? summaryLines.join("\n") : `${phaseName} report indicates failure.`;
  if (phase === "prestop") {
    state.spbPrestopDone = false;
    state.completed.delete("spbPrestop");
    state.failed.add("spbPrestop");
  } else if (phase === "downtime") {
    state.spbDowntimeDone = false;
    state.completed.delete("spbDowntime");
    state.failed.add("spbDowntime");
  } else if (phase === "poststart") {
    state.spbPoststartDone = false;
    state.completed.delete("spbPoststart");
    state.failed.add("spbPoststart");
  }
  setStatus(`${phaseName} failed`, "danger");
  return status;
}

async function openLatestSpbReport() {
  setBusy(true, "Opening report");
  const phase = currentSpbReportPhase();
  const phaseLog = phase ? state.spbPhaseLogs[phase] || "" : "";
  const viewer = window.open("", "_blank");
  writeArtifactWindow(viewer, spbTextArtifactHtml("Loading SPBAT Artifact", spbLogDir(), "Reading the latest SPBAT report or phase log from the SSH target..."));
  log(`Looking for latest SPBAT report or log under ${spbLogDir()}.`);
  try {
    const result = await postJson("api/spb/report/latest", {
      ...connectionPayload(),
      logDir: spbLogDir(),
      phase,
      phaseLog
    });
    const report = result.report || {};
    if (report.reportPath && report.html) {
      openHtmlArtifact(report.html, "SPBAT HTML report", viewer);
      log(`Opened SPBAT report: ${report.reportPath}.`, "pass");
      applySpbReportOutcome(phase, report, "the opened SPBAT report");
      return;
    }
    const fallbackPath = report.latestLogPath || phaseLog;
    const fallbackText = report.latestLogText || (phase ? state.spbPhaseOutput[phase] : "");
    if (!fallbackText) {
      closeArtifactWindow(viewer);
      log(`No SPBAT HTML report or phase log was found yet under ${report.logDir || spbLogDir()}. If this was a real run, review the phase status output and retry after SPBAT writes its log files.`, "warn");
      return;
    }
    openHtmlArtifact(spbTextArtifactHtml("SPBAT Phase Log", fallbackPath, fallbackText), "SPBAT phase log", viewer);
    if (Array.isArray(report.summaryLines) && report.summaryLines.length) {
      report.summaryLines.slice(-6).forEach((line) => log(`SPBAT log: ${line}`, /fail|error|exception/i.test(line) ? "error" : "warn"));
    }
    applySpbReportOutcome(phase, report, "the latest SPBAT log/report");
    log(`Opened SPBAT phase log: ${fallbackPath || "captured phase output"}.`, "pass");
  } catch (error) {
    closeArtifactWindow(viewer);
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function recoverSpbPhaseFromLatestReport(phase, reason) {
  const phaseName = spbPhaseLabel(phase);
  log(`PatchPilot lost the ${phaseName} background job tracker${reason ? `: ${reason}` : ""}. Checking the latest SPBAT report before marking the phase failed.`, "warn");
  try {
    const result = await postJson("api/spb/report/latest", {
      ...connectionPayload(),
      logDir: spbLogDir(),
      phase,
      phaseLog: state.spbPhaseLogs[phase] || ""
    });
    const report = result.report || {};
    if (!report.reportPath) {
      log(`No SPBAT HTML report was found under ${report.logDir || spbLogDir()} for ${phaseName} recovery.`, "warn");
      return null;
    }
    const summaryLines = Array.isArray(report.summaryLines) ? report.summaryLines : [];
    log(`Latest SPBAT report for recovery: ${report.reportPath}.`, "pass");
    summaryLines.slice(-6).forEach((line) => log(`${phaseName} report: ${line}`));
    const outcome = applySpbReportOutcome(phase, report, "the latest SPBAT report");
    if (outcome === "succeeded") {
      log(`${phaseName} recovered as successful from the latest SPBAT report.`, "pass");
      return {
        status: "succeeded",
        reportPath: report.reportPath,
        summaryLines,
        recovered: true
      };
    }
    if (outcome === "failed") {
      log(`${phaseName} latest SPBAT report indicates failure.`, "error");
      return null;
    }
    log(`Latest SPBAT report was found, but PatchPilot could not determine whether ${phaseName} succeeded. Review ${report.reportPath}.`, "warn");
    return null;
  } catch (error) {
    log(`Unable to recover ${phaseName} from latest SPBAT report: ${error.message}`, "error");
    return null;
  }
}

async function verifyShutdown() {
  const meta = shutdownGateMeta();
  if (!state[meta.customerStoppedKey]) {
    log("Confirm graceful shutdown before process verification.", "warn");
    return;
  }
  setBusy(true, "Checking processes");
  const home = selectedHome();
  log(`Checking active processes on ${state.form.host} for ${home.oracleHome}${home.domainHome ? ` and ${home.domainHome}` : ""}.`);
  try {
    const result = await postJson("api/shutdown/verify", {
      ...connectionPayload(),
      home: {
        oracleHome: home.oracleHome,
        domainHome: home.domainHome,
        instanceHome: home.instanceHome
      }
    });
    state[meta.checkKey] = result.shutdown || null;
    const running = state[meta.checkKey] && Array.isArray(state[meta.checkKey].running) ? state[meta.checkKey].running : [];
    state[meta.selectionsKey] = new Set(running.map((item) => item.key).filter(Boolean));
    if (running.length) {
      state[meta.verifiedKey] = false;
      state[meta.overrideKey] = false;
      state.completed.delete(meta.stepId);
      setStatus("Services running", "danger");
      log(meta.runningLog.replace("{count}", String(running.length)), "warn");
      running.forEach((item) => {
        const service = serviceSummary(item);
        const scopes = service.matchedScopes.length ? service.matchedScopes.join(", ") : "selected path";
        log(`${service.service} is still running (${service.category}; ${scopes}).`, "warn");
      });
    } else {
      state[meta.verifiedKey] = true;
      state[meta.overrideKey] = false;
      state.completed.add(meta.stepId);
      setStatus(meta.verifiedStatus, "good");
      log(meta.verifyLog, "pass");
    }
  } catch (error) {
    state[meta.verifiedKey] = false;
    state[meta.overrideKey] = false;
    state.completed.delete(meta.stepId);
    setStatus("Shutdown check failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

function shutdownConfirmationMessage(selected) {
  const grouped = selected.reduce((groups, service) => {
    const group = shutdownGroup(service);
    groups[group] = groups[group] || [];
    groups[group].push(service);
    return groups;
  }, {});
  const orderedGroups = ["systemComponent", "managedServer", "adminServer", "nodeManager", "other"]
    .filter((group) => grouped[group] && grouped[group].length);
  return orderedGroups.map((group) => `
    <div class="shutdown-confirm-group">
      <strong>${escapeHtml(shutdownGroupLabel(group))}</strong>
      <ul>${grouped[group].map((service) => `<li>${escapeHtml(service.service)} <span>${escapeHtml(service.shutdownHint)}</span></li>`).join("")}</ul>
    </div>
  `).join("");
}

function confirmShutdownDialog(selected) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">Shutdown Confirmation</p>
          <h2>Stop Selected Services?</h2>
        </div>
        <p>PatchPilot will stop the selected services in this order: system components, managed servers, AdminServer, then Node Manager.</p>
        <div class="review-box is-danger">
          <strong>Services will be stopped</strong>
          <p>Confirm that you are ready to stop the selected services. PatchPilot will run the stop scripts for only the selected ORACLE_HOME/DOMAIN_HOME/INSTANCE_HOME scope, then recheck the process table.</p>
        </div>
        <div class="shutdown-confirm-list">${shutdownConfirmationMessage(selected)}</div>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button class="button button-primary" value="confirm" type="submit">Stop Services</button>
        </div>
      </form>
    `;
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmForceKillDialog(selected) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    const total = selected.reduce((sum, service) => sum + (service.pids || []).length, 0);
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">Force Stop Confirmation</p>
          <h2>Kill Selected Processes?</h2>
        </div>
        <div class="review-box is-danger">
          <strong>This is force stop</strong>
          <p>PatchPilot will recheck that each process still matches the selected ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME, then send TERM followed by KILL if needed.</p>
        </div>
        <p>${escapeHtml(total)} process id(s) are currently associated with the selected service groups.</p>
        <div class="shutdown-confirm-list">${shutdownConfirmationMessage(selected)}</div>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button class="button button-primary" value="confirm" type="submit">Kill Processes</button>
        </div>
      </form>
    `;
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmContinueAnywayDialog(selected) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">Manual Override</p>
          <h2>Continue With Running Processes?</h2>
        </div>
        <div class="review-box is-warning">
          <strong>Manual acceptance required</strong>
          <p>Use this only when the remaining processes were handled outside PatchPilot or are confirmed not to block patching.</p>
        </div>
        <div class="shutdown-confirm-list">${shutdownConfirmationMessage(selected)}</div>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button class="button button-primary" value="confirm" type="submit">Continue Anyway</button>
        </div>
      </form>
    `;
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmRollbackDialog(patchId, home, reason) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    const reasonText = reason === "manual"
      ? "Manual rollback was requested."
      : `Rollback was requested because ${reason}.`;
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">Rollback Confirmation</p>
          <h2>Rollback Patch ${escapeHtml(patchId)}?</h2>
        </div>
        <div class="review-box is-danger">
          <strong>Rollback will remove the patch from this ORACLE_HOME</strong>
          <p>${escapeHtml(reasonText)} PatchPilot will verify that selected-home services are stopped before running OPatch rollback.</p>
        </div>
        <div class="detail-grid">
          <div><span>Patch</span><strong>${escapeHtml(patchId)}</strong></div>
          <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(home.oracleHome || "")}</strong></div>
          <div><span>DOMAIN_HOME</span><strong class="mono">${escapeHtml(home.domainHome || "Not discovered")}</strong></div>
          <div><span>Action</span><strong>Verify shutdown, then rollback</strong></div>
        </div>
        <label class="toggle-row decision-row">
          <input id="rollbackConfirmInput" type="checkbox">
          <span>Confirm services have been gracefully stopped and rollback should proceed.</span>
        </label>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button id="rollbackConfirmButton" class="button button-primary" value="confirm" type="submit" disabled>Verify and Rollback</button>
        </div>
      </form>
    `;
    const checkbox = dialog.querySelector("#rollbackConfirmInput");
    const confirmButton = dialog.querySelector("#rollbackConfirmButton");
    if (checkbox && confirmButton) {
      checkbox.addEventListener("change", () => {
        confirmButton.disabled = !checkbox.checked;
      });
    }
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmPatchApplyDialog(home, patchLabel) {
  return new Promise((resolve) => {
    const applyLabel = patchApplyCommandLabel();
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">Apply Confirmation</p>
          <h2>Are you sure you want to apply ${escapeHtml(isBatchNapply() ? "the selected one-off patches" : `patch ${patchLabel}`)}?</h2>
        </div>
        <div class="review-box is-danger">
          <strong>Confirm prerequisites before applying</strong>
          <p>PatchPilot will run conflict checks for every selected patch, execute ${escapeHtml(applyLabel)} against the selected ORACLE_HOME, and verify every expected patch ID in OPatch inventory.</p>
        </div>
        <div class="detail-grid">
          <div><span>Patch</span><strong>${escapeHtml(patchLabel)}</strong></div>
          <div><span>Application mode</span><strong>${escapeHtml(applyLabel)}</strong></div>
          <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(home.oracleHome || "")}</strong></div>
          <div><span>Shutdown</span><strong>${state.shutdownVerified ? "Verified" : state.shutdownOverrideApproved ? "Accepted with override" : "Not verified"}</strong></div>
          <div><span>File backup</span><strong>${state.backupsDone ? "PatchPilot backup complete" : state.backupExternalApproved ? "External backup confirmed" : "Not confirmed"}</strong></div>
          <div><span>Database backup</span><strong>${requiresDatabaseBackupForRun() ? state.databaseBackupConfirmed ? "Confirmed" : "Not confirmed" : "Not required"}</strong></div>
        </div>
        <label class="toggle-row decision-row">
          <input id="applyConfirmInput" type="checkbox">
          <span>I confirm all patch prerequisites are complete, services are stopped, required backups are complete, and PatchPilot should run ${escapeHtml(applyLabel)}.</span>
        </label>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button id="applyConfirmButton" class="button button-primary" value="confirm" type="submit" disabled>Yes, Run ${escapeHtml(applyLabel)}</button>
        </div>
      </form>
    `;
    const checkbox = dialog.querySelector("#applyConfirmInput");
    const confirmButton = dialog.querySelector("#applyConfirmButton");
    if (checkbox && confirmButton) {
      checkbox.addEventListener("change", () => {
        confirmButton.disabled = !checkbox.checked;
      });
    }
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmOpatchUpgradeDialog(home, currentVersion, minimumVersion, upgradePath) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">OPatch Upgrade Confirmation</p>
          <h2>Upgrade OPatch for ${escapeHtml(home.oracleHome || "selected ORACLE_HOME")}?</h2>
        </div>
        <div class="review-box is-warning">
          <strong>PatchPilot will update the OPatch directory</strong>
          <p>The current OPatch version is below the README minimum. PatchPilot will back up the existing OPatch directory and run the OPatch generic installer.</p>
        </div>
        <div class="detail-grid">
          <div><span>Current OPatch</span><strong>${escapeHtml(currentVersion || "unknown")}</strong></div>
          <div><span>Required Minimum</span><strong>${escapeHtml(minimumVersion || "unknown")}</strong></div>
          <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(home.oracleHome || "")}</strong></div>
          <div><span>Upgrade Source</span><strong class="mono">${escapeHtml(upgradePath || "Auto-discover from SPB download")}</strong></div>
        </div>
        <label class="toggle-row decision-row">
          <input id="opatchUpgradeConfirmInput" type="checkbox">
          <span>Confirm OPatch should be upgraded for this ORACLE_HOME.</span>
        </label>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button id="opatchUpgradeConfirmButton" class="button button-primary" value="confirm" type="submit" disabled>Upgrade OPatch</button>
        </div>
      </form>
    `;
    const checkbox = dialog.querySelector("#opatchUpgradeConfirmInput");
    const confirmButton = dialog.querySelector("#opatchUpgradeConfirmButton");
    if (checkbox && confirmButton) {
      checkbox.addEventListener("change", () => {
        confirmButton.disabled = !checkbox.checked;
      });
    }
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmSpbPhaseDialog(phase) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    const phaseName = spbPhaseLabel(phase);
    const home = selectedHome();
    const phaseCopy = {
      prestop: "SPBAT PreStop prepares and records the required status baseline before downtime.",
      downtime: "SPBAT Downtime applies the stack patch bundle while services are stopped.",
      poststart: "SPBAT PostStart runs the after-start phase and product-specific actions."
    }[phase] || "PatchPilot will run the selected SPBAT phase.";
    const backupStatus = state.backupsDone
      ? "PatchPilot backup complete"
      : state.backupExternalApproved
        ? "External backup confirmed"
        : "Not confirmed";
    const phaseWarning = phase === "downtime"
      ? "Downtime changes the selected Oracle home. SPBAT backout depends on usable ORACLE_HOME, DOMAIN_HOME, required INSTANCE_HOME, and database backups."
      : `${phaseName} will run on the selected Oracle home`;
    const confirmText = phase === "downtime"
      ? "I confirm SPBAT PreStop is complete, selected services are stopped, required backups are complete and usable, and SPBAT Downtime should proceed."
      : `Confirm prerequisites are complete and SPBAT ${phaseName} should proceed.`;
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">SPBAT Confirmation</p>
          <h2>Run SPBAT ${escapeHtml(phaseName)}?</h2>
        </div>
        <div class="review-box ${phase === "downtime" ? "is-danger" : "is-warning"}">
          <strong>${escapeHtml(phaseWarning)}</strong>
          <p>${escapeHtml(phaseCopy)} PatchPilot will stream the phase output and report path.</p>
        </div>
        <div class="detail-grid">
          <div><span>SPBAT Type</span><strong>${escapeHtml(spbInstallTypeLabel())}</strong></div>
          <div><span>Phase</span><strong>${escapeHtml(phaseName)}</strong></div>
          <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(home.oracleHome || "")}</strong></div>
          <div><span>SPBAT Log Dir</span><strong class="mono">${escapeHtml(spbLogDir())}</strong></div>
          ${spbExtraArgs() ? `<div><span>Extra Arguments</span><strong class="mono">${escapeHtml(spbExtraArgs())}</strong></div>` : ""}
          ${phase === "downtime" ? `<div><span>Downtime shutdown</span><strong>${escapeHtml(shutdownGateSatisfied("shutdown") ? "Confirmed" : "Not confirmed")}</strong></div>` : ""}
          ${phase === "downtime" ? `<div><span>File backup</span><strong>${escapeHtml(backupStatus)}</strong></div>` : ""}
          ${phase === "downtime" ? `<div><span>Database backup</span><strong>${escapeHtml(requiresDatabaseBackupForRun() ? state.databaseBackupConfirmed ? "Confirmed" : "Not confirmed" : "Not required")}</strong></div>` : ""}
        </div>
        <label class="toggle-row decision-row">
          <input id="spbPhaseConfirmInput" type="checkbox">
          <span>${escapeHtml(confirmText)}</span>
        </label>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button id="spbPhaseConfirmButton" class="button button-primary" value="confirm" type="submit" disabled>Run ${escapeHtml(phaseName)}</button>
        </div>
      </form>
    `;
    const checkbox = dialog.querySelector("#spbPhaseConfirmInput");
    const confirmButton = dialog.querySelector("#spbPhaseConfirmButton");
    if (checkbox && confirmButton) {
      checkbox.addEventListener("change", () => {
        confirmButton.disabled = !checkbox.checked;
      });
    }
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmSpbInactiveDeletionDialog() {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    const home = selectedHome();
    const level = spbInactiveRetainLevel();
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">Inactive Patch Cleanup Confirmation</p>
          <h2>Delete inactive patches?</h2>
        </div>
        <div class="review-box is-danger">
          <strong>This reduces rollback history to the selected retain level</strong>
          <p>PatchPilot will set RETAIN_INACTIVE_PATCHES=${escapeHtml(String(level))}, answer the OPatch deleteinactivepatches and cleanup prompts with y from this approval, and use OPatch return codes to decide the result.</p>
        </div>
        <div class="detail-grid">
          <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(home.oracleHome || "")}</strong></div>
          <div><span>Retain level</span><strong>N-${escapeHtml(String(level))}</strong></div>
          <div><span>Current summary</span><strong>${escapeHtml(spbInactiveSummary())}</strong></div>
          <div><span>Services stopped for backup</span><strong>${escapeHtml(backupShutdownComplete() ? "Confirmed" : "Not confirmed")}</strong></div>
          <div><span>Backup safety</span><strong>${escapeHtml(backupGateComplete() ? "Backup gate complete" : "Not confirmed")}</strong></div>
          <div><span>OPatch Heap</span><strong class="mono">${escapeHtml(opatchHeapOptions())}</strong></div>
        </div>
        <div class="command-list">
          <code>${escapeHtml(spbInactiveRetainCommand())}</code>
          <code>${escapeHtml(spbInactiveDeleteCommand())}</code>
          <code>${escapeHtml(spbInactiveCleanupCommand())}</code>
        </div>
        <label class="toggle-row decision-row">
          <input id="spbInactiveDeleteConfirmInput" type="checkbox">
          <span>I confirm the required backups are usable, the customer approved inactive patch deletion, and rollback retention should be kept at N-${escapeHtml(String(level))}.</span>
        </label>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button id="spbInactiveDeleteConfirmButton" class="button button-primary" value="confirm" type="submit" disabled>Delete Inactive Patches</button>
        </div>
      </form>
    `;
    const checkbox = dialog.querySelector("#spbInactiveDeleteConfirmInput");
    const confirmButton = dialog.querySelector("#spbInactiveDeleteConfirmButton");
    if (checkbox && confirmButton) {
      checkbox.addEventListener("change", () => {
        confirmButton.disabled = !checkbox.checked;
      });
    }
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmOigPostinstallDialog() {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">OIG Postinstall Confirmation</p>
          <h2>Run patch_oim_wls.sh?</h2>
        </div>
        <div class="review-box is-warning">
          <strong>Confirm profile and credentials before running</strong>
          <p>PatchPilot will run patch_oim_wls.sh from the selected ORACLE_HOME/idm/server/bin directory, add executable permission if needed, and stream patch_oim_wls.log from that same directory. Password values must already be filled in the server-side profile file.</p>
        </div>
        <div class="detail-grid">
          <div><span>Profile</span><strong class="mono">${escapeHtml(oigProfilePath())}</strong></div>
          <div><span>Script</span><strong class="mono">${escapeHtml(oigScriptPath())}</strong></div>
          <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(selectedHome().oracleHome || "")}</strong></div>
          <div><span>SPBAT PostStart</span><strong>${state.spbPhaseStatus.poststart === "succeeded" ? "Completed" : "Not completed"}</strong></div>
        </div>
        <label class="toggle-row decision-row">
          <input id="oigScriptConfirmInput" type="checkbox">
          <span>I confirm SPBAT PostStart is complete, patch_oim_wls.profile has been reviewed, required password fields are filled on the server, and PatchPilot should run patch_oim_wls.sh.</span>
        </label>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button id="oigScriptConfirmButton" class="button button-primary" value="confirm" type="submit" disabled>Run OIG Script</button>
        </div>
      </form>
    `;
    const checkbox = dialog.querySelector("#oigScriptConfirmInput");
    const confirmButton = dialog.querySelector("#oigScriptConfirmButton");
    if (checkbox && confirmButton) {
      checkbox.addEventListener("change", () => {
        confirmButton.disabled = !checkbox.checked;
      });
    }
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmOigManualPostinstallDialog() {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">OIG Manual Postinstall Confirmation</p>
          <h2>Confirm OIG Actions?</h2>
        </div>
        <div class="review-box is-warning">
          <strong>Manual completion will be recorded</strong>
          <p>Use this only when patch_oim_wls.profile has been completed or reviewed on the server and patch_oim_wls.sh was run manually, or the customer explicitly accepted that the OIG postinstall script is not applicable for this environment.</p>
        </div>
        <div class="detail-grid">
          <div><span>Profile</span><strong class="mono">${escapeHtml(oigProfilePath())}</strong></div>
          <div><span>Script</span><strong class="mono">${escapeHtml(oigScriptPath())}</strong></div>
          <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(selectedHome().oracleHome || "")}</strong></div>
          <div><span>SPBAT PostStart</span><strong>${state.spbPhaseStatus.poststart === "succeeded" ? "Completed" : "Not completed"}</strong></div>
        </div>
        <label class="toggle-row decision-row">
          <input id="oigManualConfirmInput" type="checkbox">
          <span>I confirm the OIG patch_oim_wls.profile and patch_oim_wls.sh postinstall actions were completed manually or explicitly accepted as not applicable for this run.</span>
        </label>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button id="oigManualConfirmButton" class="button button-primary" value="confirm" type="submit" disabled>Confirm Manual Actions</button>
        </div>
      </form>
    `;
    const checkbox = dialog.querySelector("#oigManualConfirmInput");
    const confirmButton = dialog.querySelector("#oigManualConfirmButton");
    if (checkbox && confirmButton) {
      checkbox.addEventListener("change", () => {
        confirmButton.disabled = !checkbox.checked;
      });
    }
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

function confirmSpbBackoutDialog(patchLabel, home) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "approval-dialog shutdown-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <div>
          <p class="page-breadcrumb">SPBAT Backout Confirmation</p>
          <h2>Record SPBAT backout plan for ${escapeHtml(patchLabel)}?</h2>
        </div>
        <div class="review-box is-danger">
          <strong>SPBAT does not provide automated rollback</strong>
          <p>The SPBAT README says to use the backups created before Downtime to restore the environment if the SPB must be backed out. Existing one-off patches may also need inventory review and reapply after SPB activity.</p>
        </div>
        <div class="detail-grid">
          <div><span>ORACLE_HOME</span><strong class="mono">${escapeHtml(home.oracleHome || "")}</strong></div>
          <div><span>DOMAIN_HOME</span><strong class="mono">${escapeHtml(home.domainHome || "Not discovered / not applicable")}</strong></div>
          <div><span>SPBAT Log Dir</span><strong class="mono">${escapeHtml(spbLogDir())}</strong></div>
          <div><span>Backup Reference</span><strong class="mono">${escapeHtml(backupReferenceText())}</strong></div>
        </div>
        <label class="toggle-row decision-row">
          <input id="spbBackoutConfirmInput" type="checkbox">
          <span>Confirm required backups are available, selected services are stopped, and SPBAT backout will be handled by restoring the approved backups or following Oracle Support guidance.</span>
        </label>
        <div class="dialog-actions">
          <button class="button button-secondary" value="cancel" type="submit">Cancel</button>
          <button id="spbBackoutConfirmButton" class="button button-primary" value="confirm" type="submit" disabled>Record Backout Plan</button>
        </div>
      </form>
    `;
    const checkbox = dialog.querySelector("#spbBackoutConfirmInput");
    const confirmButton = dialog.querySelector("#spbBackoutConfirmButton");
    if (checkbox && confirmButton) {
      checkbox.addEventListener("change", () => {
        confirmButton.disabled = !checkbox.checked;
      });
    }
    dialog.addEventListener("close", () => {
      const approved = dialog.returnValue === "confirm";
      dialog.remove();
      resolve(approved);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

async function verifyRollbackReadiness(patchId, home) {
  setBusy(true, "Checking rollback readiness");
  setStatus("Checking rollback readiness");
  state.customerStopped = true;
  log(`Rollback gate: verifying no selected-home processes are running before rolling back patch ${patchId}.`, "warn");
  try {
    const result = await postJson("api/shutdown/verify", {
      ...connectionPayload(),
      home: {
        oracleHome: home.oracleHome,
        domainHome: home.domainHome,
        instanceHome: home.instanceHome
      }
    });
    state.shutdownCheck = result.shutdown || null;
    const running = state.shutdownCheck && Array.isArray(state.shutdownCheck.running) ? state.shutdownCheck.running : [];
    state.shutdownSelections = new Set(running.map((item) => item.key).filter(Boolean));
    if (running.length) {
      state.shutdownVerified = false;
      state.shutdownOverrideApproved = false;
      state.completed.delete("shutdown");
      setStatus("Rollback blocked", "danger");
      log(`Rollback blocked. ${running.length} selected-home service group(s) are still running. Stop them from the Shutdown step, then retry rollback.`, "error");
      running.forEach((item) => {
        const service = serviceSummary(item);
        const scopes = service.matchedScopes.length ? service.matchedScopes.join(", ") : "selected path";
        log(`${service.service} is still running (${service.category}; ${scopes}).`, "warn");
      });
      const shutdownIndex = workflowSteps().findIndex((step) => step.id === "shutdown");
      if (shutdownIndex >= 0) state.activeStep = shutdownIndex;
      return false;
    }
    state.shutdownVerified = true;
    state.shutdownOverrideApproved = false;
    state.completed.add("shutdown");
    setStatus("Rollback ready", "good");
    log("Rollback gate passed. No active process was found for the selected ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME.", "pass");
    return true;
  } catch (error) {
    state.shutdownVerified = false;
    state.shutdownOverrideApproved = false;
    state.completed.delete("shutdown");
    setStatus("Rollback check failed", "danger");
    log(error.message, "error");
    return false;
  } finally {
    setBusy(false);
    render();
  }
}

async function refreshShutdownAfterAction(actionLabel) {
  const meta = shutdownGateMeta();
  const home = selectedHome();
  log(`Rechecking shutdown after ${actionLabel}.`);
  const result = await postJson("api/shutdown/verify", {
    ...connectionPayload(),
    home: {
      oracleHome: home.oracleHome,
      domainHome: home.domainHome,
      instanceHome: home.instanceHome
    }
  });
  state[meta.checkKey] = result.shutdown || null;
  const running = state[meta.checkKey] && Array.isArray(state[meta.checkKey].running) ? state[meta.checkKey].running : [];
  state[meta.selectionsKey] = new Set(running.map((item) => item.key).filter(Boolean));
  if (running.length) {
    state[meta.verifiedKey] = false;
    state[meta.overrideKey] = false;
    state.completed.delete(meta.stepId);
    setStatus("Services still running", "danger");
    log(`After ${actionLabel}, ${running.length} selected-home service group(s) are still running.`, "warn");
    running.forEach((item) => {
      const service = serviceSummary(item);
      const scopes = service.matchedScopes.length ? service.matchedScopes.join(", ") : "selected path";
      log(`${service.service} is still running (${service.category}; ${scopes}).`, "warn");
    });
  } else {
    state[meta.customerStoppedKey] = true;
    state[meta.verifiedKey] = true;
    state[meta.overrideKey] = false;
    state.completed.add(meta.stepId);
    setStatus(meta.verifiedStatus, "good");
    log(meta.verifyLog, "pass");
  }
}

async function generateShutdownCommands() {
  const meta = shutdownGateMeta();
  const selected = selectedShutdownServices(meta.stepId);
  if (!selected.length) {
    log("Select at least one running service to stop.", "warn");
    return;
  }
  const approved = await confirmShutdownDialog(selected);
  if (!approved) {
    log("Service shutdown was cancelled.");
    return;
  }

  log("Graceful shutdown order for selected services:", "warn");
  selected.forEach((service) => {
    log(`${shutdownGroupLabel(shutdownGroup(service))}: ${service.service} - ${service.shutdownHint}`, "warn");
  });

  setBusy(true, "Stopping services");
  setStatus("Stopping services");
  const home = selectedHome();
  try {
    const result = await postJson("api/shutdown/stop", {
      ...connectionPayload(),
      home: {
        oracleHome: home.oracleHome,
        domainHome: home.domainHome,
        instanceHome: home.instanceHome
      },
      services: selected
    });
    const shutdown = result.shutdown || {};
    (shutdown.results || []).forEach((item) => {
      const level = item.status === "succeeded" ? "pass" : "error";
      log(`${item.label}: ${item.status} (${item.durationSeconds}s) - ${item.command}`, level);
      const tail = String(item.output || "").split("\n").filter(Boolean).slice(-4).join(" | ");
      if (tail) log(tail, level);
    });
    (shutdown.manual || []).forEach((item) => {
      log(`${item.service}: manual action required - ${item.reason}`, "warn");
    });
    state[meta.customerStoppedKey] = true;
    setStatus(shutdown.status === "succeeded" ? "Shutdown commands completed" : "Shutdown needs review", shutdown.status === "failed" ? "danger" : "good");
    await refreshShutdownAfterAction("stop commands");
  } catch (error) {
    setStatus("Shutdown failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function killSelectedProcesses() {
  const meta = shutdownGateMeta();
  const selected = selectedShutdownServices(meta.stepId);
  if (!selected.length) {
    log("Select at least one running service group to kill.", "warn");
    return;
  }
  const approved = await confirmForceKillDialog(selected);
  if (!approved) {
    log("Force stop was cancelled.");
    return;
  }
  setBusy(true, "Killing processes");
  setStatus("Force stopping");
  const home = selectedHome();
  try {
    const result = await postJson("api/shutdown/kill", {
      ...connectionPayload(),
      home: {
        oracleHome: home.oracleHome,
        domainHome: home.domainHome,
        instanceHome: home.instanceHome
      },
      services: selected
    });
    const shutdown = result.shutdown || {};
    (shutdown.results || []).forEach((item) => {
      const level = item.status === "sent" ? "warn" : "error";
      log(`PID ${item.pid}: ${item.signal} ${item.status} (${(item.matchedScopes || []).join(", ")})`, level);
    });
    (shutdown.skipped || []).forEach((item) => log(`PID ${item.pid}: skipped - ${item.reason}`, "warn"));
    if ((shutdown.remainingPids || []).length) {
      setStatus("Processes remain", "danger");
      log(`Some selected processes are still running: ${shutdown.remainingPids.join(", ")}.`, "error");
    } else {
      setStatus("Force stop complete", "good");
      log("Selected scoped processes were terminated. PatchPilot is rechecking shutdown before continuing.", "pass");
    }
    await refreshShutdownAfterAction("force stop");
  } catch (error) {
    setStatus("Force stop failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function approveShutdownOverride() {
  const meta = shutdownGateMeta();
  const selected = selectedShutdownServices(meta.stepId);
  const approved = await confirmContinueAnywayDialog(selected);
  if (!approved) {
    log("Shutdown override was cancelled.");
    return;
  }
  state[meta.customerStoppedKey] = true;
  state[meta.overrideKey] = true;
  state[meta.verifiedKey] = false;
  state.completed.add(meta.stepId);
  setStatus(meta.overrideStatus, "good");
  log(meta.overrideLog, "warn");
  render();
}

async function runBackupPreflight() {
  if (!backupShutdownComplete()) {
    const shutdownIndex = workflowSteps().findIndex((step) => step.id === backupShutdownStepId());
    if (shutdownIndex >= 0) state.activeStep = shutdownIndex;
    log("Backup blocked. Verify selected-home services are stopped before checking backup space or creating tar files.", "error");
    render();
    return;
  }
  setBusy(true, "Checking backup space");
  setStatus("Checking backup space");
  state.backupDestinationDirty = false;
  state.backupPreflightConfirmed = false;
  const targets = backupTargetPayload();
  const backupDir = backupDirForRun();
  log(`Checking backup source sizes and free disk space on ${state.form.host}.`);
  log(`Backup destination to check: ${backupDir}.`);
  targets.forEach((item) => log(`Backup preflight target ${item.target}: ${item.path}.`));
  try {
    const result = await postJson("api/backup/preflight", {
      ...connectionPayload(),
      backupDir,
      targets
    });
    state.backupPreflight = result.backup;
    const preflight = state.backupPreflight || {};
    if (preflight.enoughSpace) {
      setStatus("Backup space ready", preflight.hasLargeTargets ? "neutral" : "good");
      log(`Backup destination free space: ${preflight.freeSpace}; selected source size: ${preflight.requiredSpace}.`, "pass");
      if (preflight.hasLargeTargets) {
        log(`Large backup source detected over ${preflight.largeThreshold}: ${preflight.largeTargets.join(", ")}. Confirm before creating tar files.`, "warn");
      }
      (preflight.results || []).forEach((item) => {
        log(`${item.target} planned archive: ${item.archive} (${item.sourceSize || "size unknown"} source).`, "pass");
      });
    } else {
      setStatus("Backup space blocked", "danger");
      (preflight.errors || ["Backup preflight did not pass."]).forEach((message) => log(message, "error"));
      log("Change the backup destination and recheck, or confirm the backup was completed outside PatchPilot to continue.", "warn");
    }
  } catch (error) {
    state.backupPreflight = null;
    setStatus("Backup check failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function takeBackups() {
  if (!backupShutdownComplete()) {
    const shutdownIndex = workflowSteps().findIndex((step) => step.id === backupShutdownStepId());
    if (shutdownIndex >= 0) state.activeStep = shutdownIndex;
    log("Backup blocked. Verify selected-home services are stopped before creating ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME tar files.", "error");
    render();
    return;
  }
  if (state.backupDestinationDirty || !state.backupPreflightConfirmed) {
    await runBackupPreflight();
    return;
  }
  if (!state.backupPreflight || !state.backupPreflight.enoughSpace) {
    log("Backup cannot start until the space check passes and is confirmed.", "warn");
    render();
    return;
  }
  setBusy(true, "Backing up");
  const targets = backupTargetPayload();
  const backupDir = backupDirForRun();
  state.backupResult = null;
  state.backupOutput = "";
  log(`Creating backup directory on ${state.form.host}: ${backupDir}.`);
  targets.forEach((item) => log(`Backup target ${item.target}: ${item.path}.`));
  try {
    const start = await postJson("api/backup/start", {
      ...connectionPayload(),
      oracleHome: selectedHome().oracleHome,
      backupDir,
      targets,
      stamp: state.backupPreflight.stamp || ""
    });
    log(`Backup job started. Tailing backup output from ${state.form.host}.`);
    while (true) {
      await sleep(2000);
      const result = await postJson("api/backup/status", { jobId: start.jobId });
      const job = result.job || {};
      const output = job.output || "";
      let delta = "";
      if (output.startsWith(state.backupOutput || "")) {
        delta = output.slice((state.backupOutput || "").length);
      } else if (output !== state.backupOutput) {
        delta = output;
      }
      state.backupOutput = output;
      logCommandTail(delta, "Backup");
      if (job.status === "succeeded") {
        state.backupResult = job.backup;
        state.backupsDone = true;
        syncBackupCompletion();
        if (backupGateComplete()) {
          setStatus("Backup complete", "good");
        } else {
          setStatus("Database backup pending");
          log(backupGateBlockReason(), "warn");
        }
        log(`Backup log: ${job.backup.logPath}.`, "pass");
        (job.backup.results || []).forEach((item) => {
          log(`${item.target} archive: ${item.archive} (${item.archiveSize || "size unavailable"}).`, "pass");
        });
        break;
      }
      if (job.status === "failed") {
        state.backupResult = job.backup || null;
        state.backupsDone = false;
        syncBackupCompletion();
        setStatus("Backup failed", "danger");
        throw new Error(job.error || "Backup failed.");
      }
    }
  } catch (error) {
    state.backupsDone = false;
    syncBackupCompletion();
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function runOpatchUpgradeJob(home, upgradePath) {
  setBusy(true, "Upgrading OPatch");
  setStatus("OPatch upgrading");
  state.opatchUpgradeOutput = "";
  const start = await postJson("api/opatch/upgrade/start", {
    ...connectionPayload(),
    oracleHome: home.oracleHome,
    patchPath: state.form.patchPath,
    logDir: spbLogDir(),
    opatchPath: upgradePath,
    opatchHeapOptions: opatchHeapOptions()
  });
  state.opatchUpgradeJobId = start.jobId;
  log(`OPatch upgrade job started. Streaming installer log tail for ${home.oracleHome}.`);
  log(`OPatch upgrade will use OPATCH_JRE_MEMORY_OPTIONS=${opatchHeapOptions()} unless the target already has an equal or higher value.`, "warn");

  while (true) {
    await sleep(1500);
    const result = await postJson("api/opatch/upgrade/status", { jobId: start.jobId });
    const job = result.job || {};
    const output = job.output || "";
    let delta = "";
    if (output.startsWith(state.opatchUpgradeOutput)) {
      delta = output.slice(state.opatchUpgradeOutput.length);
    } else if (output !== state.opatchUpgradeOutput) {
      delta = output;
    }
    state.opatchUpgradeOutput = output;
    logCommandTail(delta, "OPatch log");

    if (job.status === "succeeded") {
      log("OPatch upgrade job completed.", "pass");
      state.opatchUpgradeJobId = "";
      return job.opatch;
    }
    if (job.status === "failed") {
      state.opatchUpgradeJobId = "";
      throw new Error(job.error || "OPatch upgrade failed.");
    }
  }
}

async function validateOpatch() {
  setBusy(true, "Checking OPatch");
  const home = selectedHome();
  const analysis = state.readmeAnalysis || analyzeReadme(state.readmeText);
  log(`Rechecking actual OPatch version on ${state.form.host} for ${home.oracleHome}. README minimum: ${analysis.minimumOpatch}.`);
  try {
    const result = await postJson("api/opatch/version", {
      ...connectionPayload(),
      home: { oracleHome: home.oracleHome }
    });
    home.opatchVersion = result.opatch.version || home.opatchVersion;
    log(`Remote OPatch version is ${home.opatchVersion}.`);
  } catch (error) {
    state.opatchReady = false;
    state.completed.delete("opatch");
    setStatus("OPatch check failed", "danger");
    log(error.message, "error");
    setBusy(false);
    render();
    return;
  }
  if (compareVersions(home.opatchVersion, analysis.minimumOpatch) < 0) {
    const candidates = state.spbPrepareResult && Array.isArray(state.spbPrepareResult.opatchCandidates) ? state.spbPrepareResult.opatchCandidates : [];
    const upgradePath = state.form.opatchBundlePath.trim() || candidates[0] || "";
    if (upgradePath) state.form.opatchBundlePath = upgradePath;
    if (isStackPatchBundle()) {
      const commandPath = upgradePath || `${state.form.patchPath}/tools/opatch/generic or /path/to/p28186730...zip`;
      if (dom.dryRunToggle.checked) {
        state.opatchReady = false;
        state.completed.delete("opatch");
        setStatus("OPatch dry-run", "danger");
        log(`Dry-run only: PatchPilot will locate opatch_generic.jar from ${commandPath}, then run java -jar opatch_generic.jar -silent oracle_home=${home.oracleHome}`, "warn");
        log("OPatch was not upgraded because Dry-run commands first is checked. Uncheck dry-run and click Upgrade / Validate OPatch to perform the upgrade.", "warn");
        setBusy(false);
        render();
        return;
      }
      setBusy(false);
      setStatus("OPatch upgrade approval");
      render();
      const approved = await confirmOpatchUpgradeDialog(home, home.opatchVersion, analysis.minimumOpatch, upgradePath || commandPath);
      if (!approved) {
        state.opatchReady = false;
        state.completed.delete("opatch");
        setStatus("OPatch upgrade cancelled", "danger");
        log("OPatch upgrade was cancelled. OPatch prerequisite is still not satisfied.", "warn");
        render();
        return;
      }
      log(`OPatch ${home.opatchVersion} is below required ${analysis.minimumOpatch}. Starting PatchPilot-managed OPatch upgrade.`, "warn");
      try {
        const upgraded = await runOpatchUpgradeJob(home, upgradePath);
        home.opatchVersion = upgraded.version || home.opatchVersion;
        state.form.opatchBundlePath = upgraded.sourcePath || upgraded.jarPath || upgradePath;
        log(`OPatch upgrade command: ${upgraded.command}`);
        if (upgraded.logPath) log(`OPatch upgrade log file: ${upgraded.logPath}.`, "pass");
        if (upgraded.readmePath) log(`OPatch README used for upgrade reference: ${upgraded.readmePath}.`, "pass");
        if (upgraded.backupPath) log(`Backed up existing OPatch directory to ${upgraded.backupPath}.`, "pass");
        log(`OPatch upgrade completed. Remote OPatch version is now ${home.opatchVersion}.`, "pass");
      } catch (error) {
        state.opatchReady = false;
        state.completed.delete("opatch");
        setStatus("OPatch upgrade failed", "danger");
        log(error.message, "error");
        setBusy(false);
        render();
        return;
      }
      if (compareVersions(home.opatchVersion, analysis.minimumOpatch) >= 0) {
        state.opatchReady = true;
        state.completed.add("opatch");
        setStatus("OPatch ready", "good");
        log("OPatch prerequisite satisfied after upgrade.", "pass");
        setBusy(false);
        render();
        return;
      }
      state.opatchReady = false;
      state.completed.delete("opatch");
      setStatus("OPatch upgrade required", "danger");
      log(`OPatch is still ${home.opatchVersion}; required minimum is ${analysis.minimumOpatch}.`, "error");
      setBusy(false);
      render();
      return;
    }
    state.opatchReady = false;
    state.completed.delete("opatch");
    setStatus("OPatch upgrade required", "danger");
    log(`OPatch ${home.opatchVersion} is below required ${analysis.minimumOpatch}. PatchPilot did not upgrade it.`, "warn");
    log(upgradePath ? `Use OPatch upgrade path: ${upgradePath}. Then click Validate OPatch again.` : "No OPatch upgrade artifact was found. Download OPatch patch 28186730 from My Oracle Support, stage it on the target, enter that path, then validate again.", "warn");
    setBusy(false);
    render();
    return;
  }
  state.opatchReady = true;
  state.completed.add("opatch");
  log("OPatch prerequisite satisfied.", "pass");
  setBusy(false);
  render();
}

async function stopSpbInactiveOperation(skipAfterCancel) {
  if (!state.spbInactiveJobId || state.spbInactiveJobStatus !== "running" || state.spbInactiveStopRequested) return;
  const home = selectedHome();
  state.spbInactiveStopRequested = true;
  state.spbInactiveSkipAfterCancel = Boolean(skipAfterCancel);
  setStatus("Stopping inactive patch operation", "neutral");
  log(`Stop requested for the inactive-patch OPatch operation under ${home.oracleHome}.`, "warn");
  render();
  try {
    const result = await postJson("api/spb/inactive/cancel", {
      ...connectionPayload(),
      jobId: state.spbInactiveJobId,
      oracleHome: home.oracleHome
    });
    const cancellation = result.cancellation || {};
    if (cancellation.message) log(cancellation.message, cancellation.status === "warning" ? "warn" : "pass");
    if (Array.isArray(cancellation.matchedPids) && cancellation.matchedPids.length) {
      log(`Targeted remote OPatch process IDs: ${cancellation.matchedPids.join(", ")}.`, "warn");
    }
  } catch (error) {
    state.spbInactiveStopRequested = false;
    state.spbInactiveSkipAfterCancel = false;
    setStatus("Stop request could not be confirmed", "danger");
    log(error.message, "error");
    render();
  }
}

async function runSpbInactivePatchReview() {
  if (state.spbInactiveJobStatus === "running") {
    log("An inactive-patch OPatch operation is already running. Stop it or wait for it to finish before starting another.", "warn");
    return;
  }
  const home = selectedHome();
  if (!home.oracleHome) {
    log("Select the target ORACLE_HOME before checking inactive patches.", "warn");
    return;
  }
  if (!backupShutdownComplete()) {
    const shutdownIndex = workflowSteps().findIndex((step) => step.id === backupShutdownStepId());
    if (shutdownIndex >= 0) state.activeStep = shutdownIndex;
    log("Inactive patch cleanup blocked. Verify selected-home services are stopped before backup and inactive cleanup.", "error");
    render();
    return;
  }
  if (!backupGateComplete()) {
    const backupIndex = workflowSteps().findIndex((step) => step.id === "backup");
    if (backupIndex >= 0) state.activeStep = backupIndex;
    log(`Inactive patch cleanup blocked. ${backupGateBlockReason()}`, "error");
    render();
    return;
  }
  log(`Inactive patch review will use OPatch utility commands from ${home.oracleHome}/OPatch/opatch. Formal SPB OPatch validation still runs after SPB Setup.`, "warn");
  log(`OPatch utilities will use OPATCH_JRE_MEMORY_OPTIONS=${opatchHeapOptions()} unless the target already has an equal or higher value.`, "warn");
  if (state.spbInactiveRemoveConfirmed && !spbInactiveBackupSafetyConfirmed()) {
    log("Stopped-home backup confirmation is required before deleting inactive patches before SPBAT PreStop.", "warn");
  }
  if (spbInactiveDeleteReady()) {
    if (dom.dryRunToggle && dom.dryRunToggle.checked) {
      setStatus("Inactive cleanup preview");
      log(`Dry-run only: would remove inactive patches for ${home.oracleHome} with RETAIN_INACTIVE_PATCHES=${spbInactiveRetainLevel()}.`, "warn");
      log(spbInactiveRetainCommand());
      log(spbInactiveDeleteCommand());
      log(spbInactiveCleanupCommand());
      log("Inactive patches were not deleted because Dry-run commands first is checked. Clear dry-run and run removal, or select Keep inactive patches for this run to continue with the decision recorded.", "warn");
      render();
      return;
    }
    const approved = await confirmSpbInactiveDeletionDialog();
    if (!approved) {
      log("Inactive patch deletion was cancelled before OPatch deleteinactivepatches was run.");
      return;
    }
    setBusy(true, "Removing inactive patches");
    setStatus("Inactive cleanup running");
  state.spbInactiveError = "";
  log(`Inactive patch cleanup sequence approved for ${home.oracleHome} with RETAIN_INACTIVE_PATCHES=${spbInactiveRetainLevel()}.`, "warn");
  log("PatchPilot will run these steps one after another: set retain property, run deleteinactivepatches, then run cleanup. It will not rerun listorderedinactivepatches after approval.");
  try {
    state.spbInactiveJobId = "";
    state.spbInactiveJobOutput = "";
    state.spbInactiveJobStatus = "running";
    state.spbInactiveJobKind = "cleanup";
    state.spbInactiveStopRequested = false;
    state.spbInactiveSkipAfterCancel = false;
    state.spbInactiveJobStartedAt = nowSeconds();
    state.spbInactiveJobFinishedAt = null;
    const start = await postJson("api/spb/inactive/delete/start", {
      ...connectionPayload(),
      oracleHome: home.oracleHome,
      retainLevel: spbInactiveRetainLevel(),
      opatchHeapOptions: opatchHeapOptions(),
      confirmed: true,
        beforeCheck: state.spbInactiveCheck || null
      });
      state.spbInactiveJobId = start.jobId || "";
      if (!state.spbInactiveJobId) {
        throw new Error("Inactive patch cleanup job did not return a job id.");
      }
      log("Inactive patch cleanup job started. Tailing OPatch deleteinactivepatches and cleanup output.");
      let finalJob = null;
      while (true) {
        await sleep(1500);
        const snapshot = await postJson("api/spb/inactive/status", { jobId: state.spbInactiveJobId });
        const job = snapshot.job || {};
        const nextOutput = job.output || "";
        const previousOutput = state.spbInactiveJobOutput || "";
        let delta = "";
        if (nextOutput.startsWith(previousOutput)) {
          delta = nextOutput.slice(previousOutput.length);
        } else if (nextOutput !== previousOutput) {
          delta = nextOutput;
        }
        state.spbInactiveJobOutput = nextOutput;
        state.spbInactiveJobStatus = job.status || "running";
        state.spbInactiveJobKind = job.kind || state.spbInactiveJobKind;
        state.spbInactiveStopRequested = Boolean(job.cancelRequested) || state.spbInactiveStopRequested;
        state.spbInactiveJobStartedAt = normalizeTimestamp(job.startedAt) || state.spbInactiveJobStartedAt;
        state.spbInactiveJobFinishedAt = normalizeTimestamp(job.finishedAt);
        logCommandTail(delta, "Inactive patch cleanup");
        render();
        if (["succeeded", "failed", "cancelled"].includes(job.status)) {
          finalJob = job;
          break;
        }
      }
      if (finalJob && finalJob.status === "cancelled") {
        state.spbInactiveError = "";
        state.spbInactiveReviewed = false;
        state.completed.delete("spbInactive");
        state.failed.delete("spbInactive");
        setStatus("Inactive cleanup stopped", "neutral");
        log(finalJob.error || "Inactive patch cleanup stopped by the user. Review OPatch inventory before retrying.", "warn");
        return;
      }
      const finalOutput = (finalJob && finalJob.output) || state.spbInactiveJobOutput || "";
      state.spbInactiveResult = finalJob && finalJob.inactive ? finalJob.inactive : {
        status: "failed",
        error: (finalJob && finalJob.error) || "Inactive patch cleanup job did not return a final result.",
        output: finalOutput,
        beforeCheck: state.spbInactiveCheck || null,
        afterCheck: state.spbInactiveCheck || null,
        beforeSummary: spbInactiveSummary(state.spbInactiveCheck),
        afterSummary: spbInactiveSummary(state.spbInactiveCheck),
        failedCommand: spbInactiveDeleteCommand(),
        commands: [spbInactiveRetainCommand(), spbInactiveDeleteCommand(), spbInactiveCleanupCommand()]
      };
      if (!state.spbInactiveResult.output && finalOutput) {
        state.spbInactiveResult.output = finalOutput;
      }
      if (state.spbInactiveResult && state.spbInactiveResult.afterCheck) {
        state.spbInactiveCheck = state.spbInactiveResult.afterCheck;
      }
      const status = state.spbInactiveResult ? state.spbInactiveResult.status : "";
      if (["removed", "already-retained", "none"].includes(status)) {
        state.spbInactiveReviewed = true;
        state.spbInactiveSkipConfirmed = false;
        state.completed.add("spbInactive");
        state.failed.delete("spbInactive");
        setStatus("Inactive cleanup complete", "good");
        log(`Inactive patch cleanup complete: ${spbInactiveSummary(state.spbInactiveCheck)}.`, "pass");
      } else {
        state.spbInactiveError = (state.spbInactiveResult && state.spbInactiveResult.error) || "Inactive patch cleanup did not reach the requested retain level.";
        state.spbInactiveReviewed = false;
        state.completed.delete("spbInactive");
        state.failed.add("spbInactive");
        setStatus("Inactive cleanup needs review", "danger");
        log("Inactive patch cleanup did not reach the requested retain level. Review OPatch output before Downtime.", "error");
      }
      if (state.spbInactiveResult && state.spbInactiveResult.output && !state.spbInactiveJobOutput) {
        logCommandTail(state.spbInactiveResult.output, "Inactive patch cleanup");
      }
    } catch (error) {
      state.spbInactiveError = error.message;
      state.spbInactiveJobStatus = "failed";
      state.spbInactiveJobFinishedAt = state.spbInactiveJobFinishedAt || nowSeconds();
      state.spbInactiveResult = {
        status: "failed",
        error: error.message,
        output: state.spbInactiveJobOutput || error.message,
        beforeCheck: state.spbInactiveCheck || null,
        afterCheck: state.spbInactiveCheck || null,
        beforeSummary: spbInactiveSummary(state.spbInactiveCheck),
        afterSummary: spbInactiveSummary(state.spbInactiveCheck),
        failedCommand: state.spbInactiveRemoveConfirmed ? spbInactiveDeleteCommand() : spbInactiveListCommand(),
        commands: [spbInactiveRetainCommand(), spbInactiveDeleteCommand(), spbInactiveCleanupCommand()]
      };
      state.spbInactiveReviewed = false;
      state.completed.delete("spbInactive");
      state.failed.add("spbInactive");
      setStatus("Inactive cleanup failed", "danger");
      log(error.message, "error");
    } finally {
      const skipAfterCancel = state.spbInactiveJobStatus === "cancelled" && state.spbInactiveSkipAfterCancel;
      state.spbInactiveStopRequested = false;
      state.spbInactiveSkipAfterCancel = false;
      setBusy(false);
      render();
      if (skipAfterCancel) acceptSpbInactiveKeepDecision({ advance: true });
    }
    return;
  }

  setBusy(true, "Checking inactive patches");
  setStatus("Checking inactive patches");
  state.spbInactiveError = "";
  state.spbInactiveJobId = "";
  state.spbInactiveJobOutput = "";
  state.spbInactiveJobStatus = "running";
  state.spbInactiveJobKind = "check";
  state.spbInactiveStopRequested = false;
  state.spbInactiveSkipAfterCancel = false;
  state.spbInactiveJobStartedAt = nowSeconds();
  state.spbInactiveJobFinishedAt = null;
  log(`Checking inactive patches for ${home.oracleHome}.`);
  log(spbInactiveListCommand());
  try {
    const start = await postJson("api/spb/inactive/check/start", {
      ...connectionPayload(),
      oracleHome: home.oracleHome,
      retainLevel: spbInactiveRetainLevel(),
      opatchHeapOptions: opatchHeapOptions()
    });
    state.spbInactiveJobId = start.jobId || "";
    if (!state.spbInactiveJobId) throw new Error("Inactive patch review job did not return a job id.");
    let finalJob = null;
    while (true) {
      await sleep(1500);
      const snapshot = await postJson("api/spb/inactive/status", { jobId: state.spbInactiveJobId });
      const job = snapshot.job || {};
      const nextOutput = job.output || "";
      const previousOutput = state.spbInactiveJobOutput || "";
      const delta = nextOutput.startsWith(previousOutput) ? nextOutput.slice(previousOutput.length) : (nextOutput !== previousOutput ? nextOutput : "");
      state.spbInactiveJobOutput = nextOutput;
      state.spbInactiveJobStatus = job.status || "running";
      state.spbInactiveJobKind = job.kind || "check";
      state.spbInactiveStopRequested = Boolean(job.cancelRequested) || state.spbInactiveStopRequested;
      state.spbInactiveJobStartedAt = normalizeTimestamp(job.startedAt) || state.spbInactiveJobStartedAt;
      state.spbInactiveJobFinishedAt = normalizeTimestamp(job.finishedAt);
      logCommandTail(delta, "Inactive patch check");
      render();
      if (["succeeded", "failed", "cancelled"].includes(job.status)) {
        finalJob = job;
        break;
      }
    }
    if (finalJob && finalJob.status === "cancelled") {
      state.spbInactiveError = "";
      state.spbInactiveReviewed = false;
      state.completed.delete("spbInactive");
      state.failed.delete("spbInactive");
      setStatus("Inactive review stopped", "neutral");
      log(finalJob.error || "Inactive patch review stopped by the user.", "warn");
      return;
    }
    if (!finalJob || finalJob.status !== "succeeded") {
      throw new Error((finalJob && finalJob.error) || "Inactive patch review did not complete.");
    }
    state.spbInactiveCheck = finalJob.inactive || null;
    state.spbInactiveResult = null;
    if (!state.spbInactiveCheck || !state.spbInactiveCheck.hasInactive) {
      state.spbInactiveReviewed = true;
      state.spbInactiveSkipConfirmed = false;
      state.completed.add("spbInactive");
      state.failed.delete("spbInactive");
      setStatus("Inactive review complete", "good");
      log(`Inactive patch review complete: ${spbInactiveSummary(state.spbInactiveCheck)}.`, "pass");
    } else if (state.spbInactiveSkipConfirmed) {
      state.spbInactiveReviewed = true;
      state.completed.add("spbInactive");
      state.failed.delete("spbInactive");
      setStatus("Inactive patches kept", "good");
      log(`Inactive patches retained by user confirmation: ${spbInactiveSummary(state.spbInactiveCheck)}. SPBAT may take longer.`, "warn");
    } else if (state.spbInactiveCheck.desiredReached) {
      state.spbInactiveReviewed = false;
      state.completed.delete("spbInactive");
      state.failed.delete("spbInactive");
      setStatus("Inactive patches within retain level", "neutral");
      log(`Inactive patches are already within retain level N-${spbInactiveRetainLevel()}: ${spbInactiveSummary(state.spbInactiveCheck)}. Choose Continue Without Deleting to record this decision.`, "warn");
    } else {
      state.spbInactiveReviewed = false;
      state.completed.delete("spbInactive");
      state.failed.delete("spbInactive");
      setStatus("Inactive patches found", "danger");
      log(`Inactive patches found: ${spbInactiveSummary(state.spbInactiveCheck)}. Confirm deletion at retain level N-${spbInactiveRetainLevel()} or explicitly keep them before PreStop, or keep them for this run.`, "warn");
    }
    if (state.spbInactiveCheck && state.spbInactiveCheck.output) {
      logCommandTail(state.spbInactiveCheck.output, "Inactive patch check");
    }
  } catch (error) {
    state.spbInactiveError = error.message;
    state.spbInactiveReviewed = false;
    state.completed.delete("spbInactive");
    state.failed.add("spbInactive");
    setStatus("Inactive check failed", "danger");
    log(error.message, "error");
  } finally {
    const skipAfterCancel = state.spbInactiveJobStatus === "cancelled" && state.spbInactiveSkipAfterCancel;
    state.spbInactiveStopRequested = false;
    state.spbInactiveSkipAfterCancel = false;
    setBusy(false);
    render();
    if (skipAfterCancel) acceptSpbInactiveKeepDecision({ advance: true });
  }
}

async function runSpbPrestop() {
  if (spbPhaseIsRunning("prestop")) {
    log("SPBAT PreStop is already running. Wait for the current phase job to finish before starting another.", "warn");
    return;
  }
  if (!state.spbFreshInstallNoDomain && !(state.servicesUpVerified || state.servicesUpManualAccepted)) {
    log("SPBAT PreStop requires the Services Up gate to pass first.", "warn");
    return;
  }
  if (!spbInactiveEarlyComplete()) {
    log("Check inactive patches after stopped-home backup confirmation and before SPBAT PreStop. Remove them or explicitly keep them for this run.", "warn");
    return;
  }
  if (dom.dryRunToggle.checked) {
    log(`Dry-run only: ${spbatCommand("prestop")}`, "warn");
    log("SPBAT PreStop was not executed, so no status logs or HTML report will be generated. Clear Dry-run commands first to run it.", "warn");
    return;
  }
  const approved = await confirmSpbPhaseDialog("prestop");
  if (!approved) {
    log("SPBAT PreStop was cancelled before any phase command was run.");
    return;
  }
  setBusy(true, "SPBAT PreStop");
  setStatus("PreStop running");
  log(state.spbFreshInstallNoDomain
    ? `Running SPBAT PreStop baseline for ${spbInstallType().toUpperCase()} fresh install without starting services.`
    : `Running SPBAT PreStop for ${spbInstallType().toUpperCase()} while services are up.`);
  log(spbatCommand("prestop"));
  try {
    const result = await runSpbPhase("prestop");
    if (result.phaseLog) log(`PreStop phase log: ${result.phaseLog}.`, "pass");
    if (result.reportPath) log(`PreStop report: ${result.reportPath}.`, "pass");
    (result.summaryLines || []).slice(-6).forEach((line) => log(`PreStop report: ${line}`));
    log(spbatStatusCommand());
    state.spbPrestopDone = true;
    state.completed.add("spbPrestop");
    state.failed.delete("spbPrestop");
    setStatus("PreStop complete", "good");
    log(state.spbFreshInstallNoDomain
      ? "SPBAT PreStop baseline completed. Downtime can now use the generated status logs."
      : "SPBAT PreStop completed. Review the HTML report before asking customer to stop services.", "pass");
  } catch (error) {
    state.spbPrestopDone = false;
    state.completed.delete("spbPrestop");
    state.failed.add("spbPrestop");
    setStatus("PreStop failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function runSpbDowntime() {
  if (spbPhaseIsRunning("downtime")) {
    log("SPBAT Downtime is already running. Wait for the current phase job to finish before starting another.", "warn");
    return;
  }
  if (!(state.spbPrestopDone || state.spbPrestopExternalApproved)) {
    log("SPBAT Downtime requires a completed PreStop phase/status log. Run PreStop baseline first using the same SPBAT log directory.", "warn");
    return;
  }
  if (!shutdownGateSatisfied("shutdown")) {
    log("SPBAT Downtime requires selected-home services to be fully stopped first.", "warn");
    return;
  }
  if (!backupGateComplete()) {
    log(`Complete the backup gate before SPBAT Downtime. ${backupGateBlockReason()}`, "warn");
    return;
  }
  if (!spbInactiveDowntimeReady()) {
    log("Check inactive patches and either remove them or explicitly keep them before SPBAT Downtime. Inactive patches can make SPBAT run longer.", "warn");
    return;
  }
  if (state.backupExternalApproved) {
    log("Using external backup confirmation for SPBAT Downtime.", "warn");
  }
  log("SPBAT backout depends on confirmed backups; Downtime will proceed only after stopped-home file backups and any required database backup are confirmed.", "warn");
  if (dom.dryRunToggle.checked) {
    log(`Dry-run only: ${spbatCommand("downtime")}`, "warn");
    log("SPBAT Downtime was not executed. Clear Dry-run commands first to run it.", "warn");
    return;
  }
  const approved = await confirmSpbPhaseDialog("downtime");
  if (!approved) {
    log("SPBAT Downtime was cancelled before any phase command was run.");
    return;
  }
  setBusy(true, "SPBAT Downtime");
  setStatus("Downtime running");
  state.progress = 8;
  render();
  log(`Running SPBAT Downtime for ${spbInstallType().toUpperCase()} with services stopped.`);
  log(spbatCommand("downtime"));
  try {
    const result = await runSpbPhase("downtime");
    state.progress = 100;
    if (result.phaseLog) log(`Downtime phase log: ${result.phaseLog}.`, "pass");
    if (result.reportPath) log(`Downtime report: ${result.reportPath}.`, "pass");
    (result.summaryLines || []).slice(-6).forEach((line) => log(`Downtime report: ${line}`));
    state.spbDowntimeDone = true;
    state.patchApplied = true;
    state.completed.add("spbDowntime");
    state.failed.delete("spbDowntime");
    setStatus("Downtime complete", "good");
    log(spbatStatusCommand());
  } catch (error) {
    state.spbDowntimeDone = false;
    state.completed.delete("spbDowntime");
    state.failed.add("spbDowntime");
    setStatus("Downtime failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function runSpbPhase(phase) {
  const phaseName = spbPhaseLabel(phase);
  state.spbPhaseStatus[phase] = "running";
  state.spbPhaseErrors[phase] = "";
  state.spbPhaseOutput[phase] = "";
  state.spbPhaseJobs[phase] = {
    id: "",
    status: "running",
    startedAt: nowSeconds(),
    finishedAt: null
  };
  render();
  try {
    const start = await postJson("api/spb/run-phase/start", {
      ...connectionPayload(),
      installType: spbInstallType(),
      phase,
      oracleHome: selectedHome().oracleHome,
      patchPath: state.form.patchPath,
      logDir: spbLogDir(),
      extraArgs: spbExtraArgs(),
      opatchHeapOptions: opatchHeapOptions()
    });
    state.spbPhaseJobs[phase] = {
      ...(state.spbPhaseJobs[phase] || {}),
      id: start.jobId || "",
      status: "running"
    };
    log(`${phaseName} job started. Tailing SPBAT phase output and generated log details.`);
    log(`SPBAT ${phaseName} will use OPATCH_JRE_MEMORY_OPTIONS=${opatchHeapOptions()} unless the target already has an equal or higher value.`, "warn");
    while (true) {
      await sleep(1500);
      let result;
      try {
        result = await postJson("api/spb/run-phase/status", { jobId: start.jobId });
      } catch (error) {
        if (/SPBAT phase job was not found/i.test(error.message)) {
          const recovered = await recoverSpbPhaseFromLatestReport(phase, error.message);
          if (recovered) return recovered;
        }
        throw error;
      }
      const job = result.job || {};
      const previousJob = state.spbPhaseJobs[phase] || {};
      state.spbPhaseJobs[phase] = {
        ...previousJob,
        id: job.id || previousJob.id || start.jobId || "",
        status: job.status || "running",
        startedAt: normalizeTimestamp(job.startedAt) || previousJob.startedAt || nowSeconds(),
        finishedAt: normalizeTimestamp(job.finishedAt)
      };
      const output = job.output || "";
      let delta = "";
      if (output.startsWith(state.spbPhaseOutput[phase] || "")) {
        delta = output.slice((state.spbPhaseOutput[phase] || "").length);
      } else if (output !== state.spbPhaseOutput[phase]) {
        delta = output;
      }
      state.spbPhaseOutput[phase] = output;
      const phaseLogMatch = output.match(/PatchPilot phase log:\s*(\S+)/);
      if (phaseLogMatch && state.spbPhaseLogs[phase] !== phaseLogMatch[1]) {
        state.spbPhaseLogs[phase] = phaseLogMatch[1];
        render();
      }
      logCommandTail(delta, `SPBAT ${phaseName}`);

      if (job.spb && job.spb.phaseLog) state.spbPhaseLogs[phase] = job.spb.phaseLog;
      if (job.status === "succeeded") {
        state.spbPhaseStatus[phase] = "succeeded";
        state.spbPhaseErrors[phase] = "";
        state.spbPhaseJobs[phase] = {
          ...(state.spbPhaseJobs[phase] || {}),
          status: "succeeded",
          finishedAt: normalizeTimestamp(job.finishedAt) || nowSeconds()
        };
        render();
        return job.spb || { output };
      }
      if (job.status === "failed") {
        const detail = (job.spb && (job.spb.error || (job.spb.summaryLines || []).join("\n"))) || job.error || `${phaseName} failed.`;
        state.spbPhaseStatus[phase] = "failed";
        state.spbPhaseErrors[phase] = detail;
        if (job.spb && job.spb.phaseLog) state.spbPhaseLogs[phase] = job.spb.phaseLog;
        state.spbPhaseJobs[phase] = {
          ...(state.spbPhaseJobs[phase] || {}),
          status: "failed",
          finishedAt: normalizeTimestamp(job.finishedAt) || nowSeconds()
        };
        render();
        throw new Error(detail);
      }
      render();
    }
  } catch (error) {
    state.spbPhaseStatus[phase] = "failed";
    state.spbPhaseErrors[phase] = error.message;
    state.spbPhaseJobs[phase] = {
      ...(state.spbPhaseJobs[phase] || {}),
      status: "failed",
      finishedAt: (state.spbPhaseJobs[phase] && state.spbPhaseJobs[phase].finishedAt) || nowSeconds()
    };
    render();
    throw error;
  }
}

async function applyPatch() {
  const dryRun = dom.dryRunToggle.checked;
  const home = selectedHome();
  const applyLabel = patchApplyCommandLabel();
  if (!shutdownGateSatisfied("shutdown")) {
    const shutdownIndex = workflowSteps().findIndex((step) => step.id === "shutdown");
    if (shutdownIndex >= 0) state.activeStep = shutdownIndex;
    log("Patch apply blocked. Verify shutdown or explicitly accept the shutdown gate before applying the patch.", "error");
    render();
    return;
  }
  if (!backupGateComplete()) {
    log(`Patch apply blocked. Complete the backup gate before applying the patch. ${backupGateBlockReason()}`, "error");
    render();
    return;
  }
  if (!state.opatchReady) {
    log("Patch apply blocked. Validate OPatch before applying the patch.", "error");
    render();
    return;
  }
  if (!dryRun) {
    const approved = await confirmPatchApplyDialog(home, completedPatchLabel());
    if (!approved) {
      log(`${applyLabel} was cancelled before any patch command was run.`);
      return;
    }
  }

  setBusy(true, "Patching");
  setStatus(dryRun ? "Apply dry-run" : "Patching");
  state.progress = 0;
  state.patchStartedAt = Date.now();
  state.patchProgressPhase = "idle";
  setPatchProgressPhase("preparing");
  state.opatchDebugUsed = opatchDebugEnabled();
  state.patchApplied = false;
  state.patchResult = null;
  state.patchApplyOutput = "";
  state.rollbackInProgress = false;
  state.rollbackDone = false;
  state.rollbackResult = null;
  state.rollbackOutput = "";
  state.completed.delete("apply");
  state.failed.delete("apply");
  log(`Starting ${dryRun ? "dry-run " : ""}${applyLabel} flow for ${state.form.patchPath}.`);
  log(`OPatch flow will use OPATCH_JRE_MEMORY_OPTIONS=${opatchHeapOptions()} unless the target already has an equal or higher value.`, "warn");
  if (state.opatchDebugUsed) {
    log("OPatch verbose/debug output is enabled for prereq and apply commands.", "warn");
  }
  render();

  if (dom.simulateConflictToggle.checked) {
    state.patchResult = buildClientPatchFailureResult(
      "Conflict detected by CheckConflictAgainstOHWithDetail. Patch apply stopped.",
      state.patchApplyOutput,
      dryRun
    );
    state.report = null;
    state.completed.delete("report");
    state.failed.add("apply");
    setPatchProgressPhase("failed");
    log("Conflict detected by CheckConflictAgainstOHWithDetail. Patch apply stopped.", "error");
    if (dom.autoRollbackToggle.checked) {
      log("Rollback was not started because the simulated conflict occurred before OPatch apply changed the Oracle home.", "warn");
    }
    setStatus("Conflict", "danger");
    setBusy(false);
    render();
    return;
  }

  try {
    const start = await postJson("api/patch/apply/start", {
        ...connectionPayload(),
        oracleHome: home.oracleHome,
        patchPath: state.form.patchPath,
        applyMode: isBatchNapply() ? "batch" : "single",
        batchSelection: state.form.batchSelection,
        batchPatchIds: state.form.batchPatchIds,
        patchListFile: state.form.patchListFile,
        dryRun,
        debug: state.opatchDebugUsed,
        opatchHeapOptions: opatchHeapOptions()
      });
    log(`${dryRun ? `${applyLabel} dry-run` : applyLabel} job started. Tailing remote OPatch output.`);
    while (true) {
      await sleep(2000);
      const result = await postJson("api/patch/apply/status", { jobId: start.jobId });
      const job = result.job || {};
      const output = job.output || "";
      let delta = "";
      if (output.startsWith(state.patchApplyOutput || "")) {
        delta = output.slice((state.patchApplyOutput || "").length);
      } else if (output !== state.patchApplyOutput) {
        delta = output;
      }
      const previousOpatchLog = currentOpatchLogLocation();
      state.patchApplyOutput = output;
      logCommandTail(delta, dryRun ? `${applyLabel} dry-run` : applyLabel);
      const latestOpatchLog = currentOpatchLogLocation();
      if (latestOpatchLog && latestOpatchLog !== previousOpatchLog) {
        log(`Current OPatch log file: ${latestOpatchLog}.`, "pass");
      }
      updatePatchProgressFromOutput(output, dryRun);
      render();

      if (job.status === "succeeded") {
        const patch = job.patch || {};
        state.patchResult = patch;
        state.progress = 100;
        if (patch.status === "dry-run") {
          setPatchProgressPhase("dryRunComplete");
          state.patchApplied = false;
          state.completed.delete("apply");
          setStatus("Dry-run passed", "neutral");
          log(`Dry-run completed. Conflict checks passed, but ${applyLabel} was not executed because Dry-run commands first is checked.`, "warn");
        } else if (patch.status === "succeeded" && patch.inventoryVerified) {
          setPatchProgressPhase("complete");
          state.patchApplied = true;
          state.completed.add("apply");
          setStatus("Patch applied", "good");
          if (patch.alreadyApplied) {
            log(`Patch already exists in OPatch inventory. Found patch id(s): ${(patch.foundPatchIds || []).join(", ") || patch.primaryPatchId || "verified"}.`, "pass");
          } else {
            log(`Patch applied and verified in OPatch inventory. Found patch id(s): ${(patch.foundPatchIds || []).join(", ") || patch.primaryPatchId || "verified"}.`, "pass");
          }
        } else {
          setPatchProgressPhase("failed");
          state.patchApplied = false;
          state.failed.add("apply");
          setStatus("Apply verification failed", "danger");
          log(patch.error || `${applyLabel} completed, but inventory verification did not pass.`, "error");
        }
        break;
      }

      if (job.status === "failed") {
        state.patchResult = job.patch || buildClientPatchFailureResult(job.error || `${applyLabel} failed.`, output, dryRun);
        setPatchProgressPhase("failed");
        state.patchApplied = false;
        state.failed.add("apply");
        setStatus("Patch apply failed", "danger");
        throw new Error((state.patchResult && state.patchResult.error) || job.error || `${applyLabel} failed.`);
      }
    }
  } catch (error) {
    state.patchApplied = false;
    state.completed.delete("apply");
    state.failed.add("apply");
    if (!state.patchResult || state.patchResult.status !== "failed") {
      state.patchResult = buildClientPatchFailureResult(error.message, state.patchApplyOutput, dryRun);
    }
    setPatchProgressPhase("failed");
    state.report = null;
    state.completed.delete("report");
    log(error.message, "error");
    log("Generate a Support Report from this Apply step and ask the customer to create an Oracle Support SR with the report attached.", "warn");
  } finally {
    setBusy(false);
    render();
  }
}

async function rollbackPatch(reason = "manual") {
  if (isStackPatchBundle()) {
    await rollbackSpb(reason);
    return;
  }

  const patchId = rollbackPatchId();
  if (!patchId) {
    log("PatchPilot could not determine the patch id to rollback from the patch path or apply result.", "error");
    return;
  }

  const home = selectedHome();
  const approved = await confirmRollbackDialog(patchId, home, reason);
  if (!approved) {
    log(`Rollback cancelled for patch ${patchId}.`);
    return;
  }
  const ready = await verifyRollbackReadiness(patchId, home);
  if (!ready) return;

  const reportWasVisible = Boolean(state.report) || currentStep().id === "report";
  if (reportWasVisible) {
    const reportIndex = workflowSteps().findIndex((step) => step.id === "report");
    if (reportIndex >= 0) state.activeStep = reportIndex;
  }
  setBusy(true, "Rolling back");
  setStatus("Rolling back");
  state.rollbackInProgress = true;
  state.rollbackResult = null;
  state.rollbackOutput = "";
  state.rollbackDone = false;
  state.report = null;
  state.completed.delete("report");
  log(`Rollback started (${reason}) for patch ${patchId}.`, "warn");
  render();
  try {
    const start = await postJson("api/patch/rollback/start", {
      ...connectionPayload(),
      oracleHome: home.oracleHome,
      patchPath: state.form.patchPath,
      patchId,
      opatchHeapOptions: opatchHeapOptions()
    });
    log(`OPatch rollback job started. Tailing remote rollback output for patch ${patchId}.`);
    while (true) {
      await sleep(2000);
      const result = await postJson("api/patch/rollback/status", { jobId: start.jobId });
      const job = result.job || {};
      const output = job.output || "";
      let delta = "";
      if (output.startsWith(state.rollbackOutput || "")) {
        delta = output.slice((state.rollbackOutput || "").length);
      } else if (output !== state.rollbackOutput) {
        delta = output;
      }
      state.rollbackOutput = output;
      logCommandTail(delta, "OPatch rollback");
      render();

      if (job.status === "succeeded") {
        const rollback = job.rollback || {};
        state.rollbackResult = rollback;
        if (rollback.status === "succeeded" && rollback.inventoryVerified) {
          state.rollbackDone = true;
          state.patchApplied = false;
          state.failed.delete("apply");
          state.completed.add("apply");
          state.progress = 0;
          setStatus("Rollback verified", "good");
          if (rollback.alreadyRolledBack) {
            log(`Patch ${rollback.patchId || patchId} was already absent from OPatch inventory. Rollback verified.`, "pass");
          } else {
            log(`Rollback completed and OPatch inventory no longer lists patch ${rollback.patchId || patchId}.`, "pass");
          }
          state.rollbackInProgress = false;
          state.completed.add("report");
          state.report = buildReport("rolled back");
        } else {
          state.rollbackDone = false;
          state.failed.add("apply");
          setStatus("Rollback verification failed", "danger");
          log(rollback.error || "OPatch rollback completed, but inventory verification did not pass.", "error");
          state.rollbackInProgress = false;
          state.report = buildReport("failed");
        }
        break;
      }

      if (job.status === "failed") {
        state.rollbackResult = job.rollback || null;
        state.rollbackDone = false;
        state.failed.add("apply");
        setStatus("Rollback failed", "danger");
        throw new Error(job.error || "OPatch rollback failed.");
      }
    }
  } catch (error) {
    state.rollbackDone = false;
    state.failed.add("apply");
    log(error.message, "error");
    state.rollbackInProgress = false;
    state.report = buildReport("failed");
  } finally {
    state.rollbackInProgress = false;
    setBusy(false);
    render();
  }
}

async function rollbackSpb(reason = "manual") {
  const patchLabel = rollbackPatchId() || "SPB";
  const home = selectedHome();
  if (!backupGateComplete()) {
    const backupIndex = workflowSteps().findIndex((step) => step.id === "backup");
    if (backupIndex >= 0) state.activeStep = backupIndex;
    log(`SPBAT backout requires confirmed backups. ${backupGateBlockReason()}`, "error");
    render();
    return;
  }
  const approved = await confirmSpbBackoutDialog(patchLabel, home);
  if (!approved) {
    log("SPBAT backout plan was cancelled before any action was recorded.");
    return;
  }
  const ready = state.spbFreshInstallNoDomain ? true : await verifyRollbackReadiness(patchLabel, home);
  if (!ready) return;

  const reportWasVisible = Boolean(state.report) || currentStep().id === "report";
  if (reportWasVisible) {
    const reportIndex = workflowSteps().findIndex((step) => step.id === "report");
    if (reportIndex >= 0) state.activeStep = reportIndex;
  }
  setBusy(true, "SPBAT backout");
  setStatus("SPBAT backout required");
  state.rollbackInProgress = false;
  state.rollbackDone = false;
  state.rollbackOutput = "";
  state.report = null;
  state.completed.delete("report");
  state.progress = 0;
  state.rollbackResult = {
    status: "manual-backout-required",
    spb: true,
    phase: "manual backout",
    reason,
    patchId: patchLabel,
    oracleHome: home.oracleHome,
    domainHome: home.domainHome || "",
    instanceHome: home.instanceHome || "",
    logDir: spbLogDir(),
    backupReference: backupReferenceText(),
    error: ""
  };
  log(`SPBAT backout recorded (${reason}) for ${patchLabel}. The SPBAT utility does not provide automated rollback support.`, "warn");
  log(`Use confirmed backups to restore the environment if SPB backout is required: ${backupReferenceText()}`, "warn");
  log("Review OPatch inventory for existing one-offs that may have been rolled back during SPB activity, then reapply any required one-offs.", "warn");
  state.report = buildReport("rollback required");
  state.completed.add("report");
  setBusy(false);
  render();
}

async function runPostinstall() {
  setBusy(true, "Postinstall");
  if (isStackPatchBundle()) {
    const targets = spbCleanupTargets();
    const dryRun = spbCleanupDryRunEnabled();
    const home = selectedHome();
    if (!targets.length) {
      state.spbCleanupStatus = "failed";
      state.spbCleanupError = "Add at least one DOMAIN_HOME/servers/<server>/tmp/* or cache/* path before cleanup.";
      log(state.spbCleanupError, "error");
      setBusy(false);
      render();
      return;
    }
    if (targets.some((path) => /<[^>]+>/.test(path))) {
      state.spbCleanupStatus = "failed";
      state.spbCleanupError = "Replace <SERVER_NAME> with the real WebLogic server directory name before running cleanup.";
      log(state.spbCleanupError, "error");
      setBusy(false);
      render();
      return;
    }
    if (!state.spbCleanupApproved) {
      state.spbCleanupStatus = "idle";
      state.spbCleanupError = "Approval is required before clearing the listed tmp/cache directories.";
      log(state.spbCleanupError, "warn");
      setBusy(false);
      render();
      return;
    }
    state.spbCleanupStatus = "running";
    state.spbCleanupError = "";
    state.spbCleanupResult = null;
    state.spbCleanupOutput = "";
    state.postinstallDone = false;
    state.completed.delete("postinstall");
    state.failed.delete("postinstall");
    setBusy(true, dryRun ? "Previewing cleanup" : "Clearing tmp/cache");
    render();
    try {
      log(`${dryRun ? "Previewing" : "Clearing"} ${targets.length} tmp/cache path(s) under ${home.domainHome || "the selected domain"}.`);
      const result = await postJson("api/spb/prestart-cleanup", {
        ...connectionPayload(),
        home: {
          oracleHome: home.oracleHome,
          domainHome: home.domainHome,
          instanceHome: home.instanceHome,
          product: home.product || "",
          label: home.label || ""
        },
        targets,
        dryRun
      });
      const cleanup = result.cleanup || {};
      state.spbCleanupAttempts.push({
        ...cleanup,
        attempt: state.spbCleanupAttempts.length + 1,
        recordedAt: new Date().toISOString()
      });
      state.spbCleanupResult = cleanup;
      state.spbCleanupOutput = cleanup.output || "";
      state.spbCleanupStatus = cleanup.dryRun ? "preview" : cleanup.status || "failed";
      if (state.spbCleanupOutput) logCommandTail(state.spbCleanupOutput, "Pre-start cleanup");
      if (cleanup.dryRun || cleanup.status === "preview") {
        state.postinstallDone = false;
        state.completed.delete("postinstall");
        state.failed.delete("postinstall");
        setStatus("Cleanup preview complete", "neutral");
        log("Tmp/cache cleanup preview completed. Dry-run is still enabled, so no files were deleted.", "warn");
      } else if (cleanup.status === "succeeded" || cleanup.status === "partial") {
        state.postinstallDone = true;
        state.completed.add("postinstall");
        state.failed.delete("postinstall");
        setStatus("Cleanup complete", "good");
          log(cleanup.status === "partial" ? "Tmp/cache cleanup completed with warnings. Review missing path details before startup." : "Tmp/cache cleanup completed for the listed paths.", cleanup.status === "partial" ? "warn" : "pass");
      } else {
        state.postinstallDone = false;
        state.completed.delete("postinstall");
        state.failed.add("postinstall");
        state.spbCleanupStatus = "failed";
        state.spbCleanupError = cleanup.error || "Tmp/cache cleanup failed. Review the per-path results.";
        setStatus("Cleanup failed", "danger");
        log(state.spbCleanupError, "error");
      }
    } catch (error) {
      state.spbCleanupStatus = "failed";
      state.spbCleanupError = error.message;
      state.spbCleanupResult = {
        status: "failed",
        error: error.message,
        targets: targets.map((target) => ({ input: target, path: cleanupPathBase(target), status: "failed", error: error.message })),
        output: error.message
      };
      state.spbCleanupOutput = error.message;
      state.postinstallDone = false;
      state.completed.delete("postinstall");
      state.failed.add("postinstall");
      setStatus("Cleanup failed", "danger");
      log(error.message, "error");
    } finally {
      setBusy(false);
      render();
    }
    return;
  }
  const stepsToRun = (state.readmeAnalysis || analyzeReadme(state.readmeText)).postinstallSteps;
  if (!state.postinstallConfirmed) {
    log("Confirm that the README postinstallation steps were completed or explicitly not applicable before continuing.", "warn");
    setBusy(false);
    render();
    return;
  }
  const analysis = state.readmeAnalysis || analyzeReadme(state.readmeText);
  for (const [index, step] of stepsToRun.entries()) {
    const firstLine = step.split(/\r?\n/).find((line) => line.trim()) || `Step ${index + 1}`;
    const level = analysis.postinstallManualSteps[index] ? "warn" : "pass";
    log(`Postinstall confirmed: ${firstLine.trim()}`, level);
    await sleep(120);
  }
  state.postinstallDone = true;
  state.completed.add("postinstall");
  log("README postinstallation steps were confirmed for this run.", "pass");
  setBusy(false);
  render();
}

function oigProfilePayload(extra = {}) {
  const home = selectedHome();
  return {
    ...connectionPayload(),
    oracleHome: home.oracleHome,
    domainHome: home.domainHome || "",
    patchPath: state.form.patchPath,
    logDir: spbLogDir(),
    targetHost: state.form.host,
    ...extra
  };
}

async function inspectOigProfile(save = false, values = {}) {
  setBusy(true, save ? "Saving OIG profile" : "Loading OIG profile");
  try {
    const result = await postJson("api/oig/profile", oigProfilePayload({ save, values }));
    state.oigProfile = result.profile || null;
    hydrateOigProfileEdits(state.oigProfile);
    state.oigProfileSaved = save || Boolean(state.oigProfile && !state.oigProfile.suggestedKeys?.length);
    state.oigScriptLogPath = (state.oigProfile && state.oigProfile.logPath) || state.oigScriptLogPath;
    const profilePath = state.oigProfile && state.oigProfile.profilePath ? state.oigProfile.profilePath : oigProfilePath();
    log(`${save ? "Saved" : "Loaded"} OIG profile helper data from ${profilePath}.`, "pass");
    if (save && state.oigProfile && state.oigProfile.backupPath) {
      if (state.oigProfile.backupCreated) {
        log(`Profile backup created before save: ${state.oigProfile.backupPath}.`, "pass");
      } else if (state.oigProfile.backupAlreadyExisted) {
        log(`Existing profile backup preserved: ${state.oigProfile.backupPath}.`, "pass");
      } else {
        log(`Profile backup path: ${state.oigProfile.backupPath}.`, "pass");
      }
    }
    if (state.oigProfile && Array.isArray(state.oigProfile.updatedKeys) && state.oigProfile.updatedKeys.length) {
      log(`Updated non-password profile fields: ${state.oigProfile.updatedKeys.join(", ")}.`, "pass");
    }
    if (state.oigProfile && Array.isArray(state.oigProfile.suggestedKeys) && state.oigProfile.suggestedKeys.length && !save) {
      log(`PatchPilot suggested values for: ${state.oigProfile.suggestedKeys.join(", ")}. Review and click Save Non-password Fields.`, "warn");
    }
    if (state.oigProfile && Array.isArray(state.oigProfile.missingSecretKeys) && state.oigProfile.missingSecretKeys.length) {
      log(`Password/runtime prompt fields still need server-side review before PatchPilot can run patch_oim_wls.sh: ${state.oigProfile.missingSecretKeys.join(", ")}.`, "warn");
      if (save) {
        log(`Ask the customer to fill those fields in ${profilePath}, then run ${oigScriptCommand()} or run it manually in a terminal and click Tail patch_oim_wls.log.`, "warn");
      }
    }
    setStatus(save ? "OIG profile saved" : "OIG profile loaded", "good");
  } catch (error) {
    state.oigProfile = null;
    state.oigProfileSaved = false;
    setStatus("OIG profile error", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function saveOigProfile() {
  const values = {};
  document.querySelectorAll(".oig-profile-input").forEach((input) => {
    if (input.dataset.oigProfileKey) values[input.dataset.oigProfileKey] = input.value;
  });
  Object.assign(state.oigProfileEdits, values);
  await inspectOigProfile(true, state.oigProfileEdits);
}

async function tailOigPostinstallLog(options = {}) {
  const quiet = Boolean(options.quiet);
  if (spbInstallType() !== "oig") return null;
  if (!quiet) {
    setBusy(true, "Tailing OIG log");
  }
  try {
    const result = await postJson("api/oig/log/tail", oigProfilePayload());
    const payload = result.log || {};
    if (payload.logPath) state.oigScriptLogPath = payload.logPath;
    if (payload.logTail) state.oigScriptOutput = payload.logTail;
    if (payload.status === "found") {
      if (!quiet) {
        log(`Loaded patch_oim_wls log tail from ${payload.logPath}.`, "pass");
        if (payload.scriptExists && !payload.scriptExecutable) {
          log(`${payload.scriptPath} is not executable. PatchPilot will run chmod u+x before launching it from the Run button.`, "warn");
        }
      }
    } else if (!quiet) {
      log(payload.error || `No patch_oim_wls log was found yet under ${unixParentPath(oigScriptPath())}.`, "warn");
    }
    return payload;
  } catch (error) {
    if (!quiet) {
      setStatus("OIG log tail failed", "danger");
      log(error.message, "error");
    }
    return null;
  } finally {
    if (!quiet) {
      setBusy(false);
      render();
    }
  }
}

async function acceptOigManualPostinstall(options = {}) {
  if (spbInstallType() !== "oig") return false;
  if (state.spbPhaseStatus.poststart !== "succeeded") {
    log("Run SPBAT PostStart successfully before confirming OIG manual postinstall actions.", "warn");
    return false;
  }
  const requireDialog = options.requireDialog !== false;
  if (requireDialog) {
    const approved = await confirmOigManualPostinstallDialog();
    if (!approved) {
      log("Manual OIG postinstall confirmation was cancelled.");
      return false;
    }
  }
  const logPayload = await tailOigPostinstallLog({ quiet: true });
  state.oigManualAccepted = true;
  state.oigProfileReady = true;
  state.oigScriptStatus = "manual";
  state.oigScriptDone = true;
  state.oigScriptResult = {
    status: "manual",
    profilePath: oigProfilePath(),
    scriptPath: oigScriptPath(),
    logPath: logPayload && logPayload.logPath ? logPayload.logPath : state.oigScriptLogPath,
    logTailAvailable: Boolean(logPayload && logPayload.status === "found"),
    message: "OIG patch_oim_wls.profile and patch_oim_wls.sh actions were confirmed manually or accepted as not applicable."
  };
  state.oigScriptError = "";
  state.spbPoststartDone = true;
  state.completed.add("spbPoststart");
  state.failed.delete("spbPoststart");
  setStatus("PostStart complete", "good");
  log("OIG postinstall actions were marked for manual/customer completion. PatchPilot will record this decision in the report and allow Continue.", "pass");
  if (state.oigScriptLogPath) {
    log(`Latest OIG postinstall log reference: ${state.oigScriptLogPath}.`, logPayload && logPayload.status === "found" ? "pass" : "warn");
  }
  render();
  return true;
}

async function runOigPostinstallScript() {
  if (spbInstallType() !== "oig") return true;
  if (state.spbPhaseStatus.poststart !== "succeeded") {
    log("Run SPBAT PostStart successfully before running patch_oim_wls.sh.", "warn");
    return false;
  }
  const missingSecretKeys = state.oigProfile && Array.isArray(state.oigProfile.missingSecretKeys) ? state.oigProfile.missingSecretKeys : [];
  if (missingSecretKeys.length) {
    log(`PatchPilot cannot run patch_oim_wls.sh while password fields are blank, placeholders, or commented for runtime prompts: ${missingSecretKeys.join(", ")}. Fill them on the server, or run the script manually and check the manual OIG action box to continue.`, "warn");
    render();
    return false;
  }
  if (!state.oigPasswordsConfirmed) {
    log("Confirm that the customer filled/reviewed the OIG password fields in patch_oim_wls.profile before running patch_oim_wls.sh.", "warn");
    render();
    return false;
  }
  if (dom.dryRunToggle.checked) {
    log(`Dry-run only: ${oigScriptCommand()}`, "warn");
    log("OIG patch_oim_wls.sh was not executed because Dry-run commands first is checked.", "warn");
    return false;
  }
  const approved = await confirmOigPostinstallDialog();
  if (!approved) {
    log("OIG postinstall script was cancelled before patch_oim_wls.sh was run.");
    return false;
  }
  setBusy(true, "OIG postinstall");
  setStatus("OIG script running");
  state.oigManualAccepted = false;
  state.oigScriptStatus = "running";
  state.oigScriptDone = false;
  state.oigScriptError = "";
  state.oigScriptOutput = "";
  render();
  try {
    const start = await postJson("api/oig/script/start", oigProfilePayload());
    log(`OIG postinstall job started. Tailing patch_oim_wls.sh output and log from ${state.form.host}.`);
    while (true) {
      await sleep(1500);
      const result = await postJson("api/oig/script/status", { jobId: start.jobId });
      const job = result.job || {};
      const output = job.output || "";
      let delta = "";
      if (output.startsWith(state.oigScriptOutput || "")) {
        delta = output.slice((state.oigScriptOutput || "").length);
      } else if (output !== state.oigScriptOutput) {
        delta = output;
      }
      state.oigScriptOutput = output;
      const logMatch = output.match(/PatchPilot OIG log:\s*(\S+)/);
      if (logMatch && state.oigScriptLogPath !== logMatch[1]) {
        state.oigScriptLogPath = logMatch[1];
        render();
      }
      logCommandTail(delta, "OIG postinstall");

      if (job.oig && job.oig.logPath) state.oigScriptLogPath = job.oig.logPath;
      if (job.status === "succeeded") {
        state.oigScriptStatus = "succeeded";
        state.oigScriptDone = true;
        state.oigScriptResult = job.oig || {};
        state.oigScriptError = "";
        state.oigProfileReady = true;
        state.oigManualAccepted = false;
        state.spbPoststartDone = true;
        state.completed.add("spbPoststart");
        state.failed.delete("spbPoststart");
        setStatus("PostStart complete", "good");
        if (job.oig && job.oig.chmodApplied) {
          log(`PatchPilot applied chmod u+x before running ${job.oig.scriptPath || oigScriptPath()}.`, "pass");
        }
        log(`OIG postinstall script completed. Log file: ${state.oigScriptLogPath || "patch_oim_wls.log"}.`, "pass");
        return true;
      }
      if (job.status === "failed") {
        const detail = (job.oig && job.oig.error) || job.error || "OIG postinstall script failed.";
        state.oigScriptStatus = "failed";
        state.oigScriptDone = false;
        state.oigScriptResult = job.oig || null;
        state.oigScriptError = detail;
        state.spbPoststartDone = false;
        state.completed.delete("spbPoststart");
        state.failed.add("spbPoststart");
        setStatus("OIG postinstall failed", "danger");
        throw new Error(detail);
      }
    }
  } catch (error) {
    state.oigScriptStatus = "failed";
    state.oigScriptDone = false;
    state.oigScriptError = error.message;
    state.spbPoststartDone = false;
    state.completed.delete("spbPoststart");
    state.failed.add("spbPoststart");
    log(error.message, "error");
    return false;
  } finally {
    setBusy(false);
    render();
  }
}

async function runSpbPoststart() {
  const isOig = spbInstallType() === "oig";
  if (state.spbPoststartDone) {
    log("SPBAT PostStart is already complete. The completed result is read-only; start a new run if another execution is required.", "warn");
    return;
  }
  if (spbPhaseIsRunning("poststart")) {
    log("SPBAT PostStart is already running. Wait for the current phase job to finish before starting another.", "warn");
    return;
  }
  if (isOig && state.spbPhaseStatus.poststart === "succeeded" && !state.oigScriptDone) {
    await runOigPostinstallScript();
    return;
  }
  if (dom.dryRunToggle.checked) {
    log(`Dry-run only: ${spbatCommand("poststart")}`, "warn");
    if (isOig) log(`Dry-run only: ${oigScriptCommand()}`, "warn");
    log("SPBAT PostStart was not executed. Clear Dry-run commands first to run it.", "warn");
    return;
  }
  const approved = await confirmSpbPhaseDialog("poststart");
  if (!approved) {
    log("SPBAT PostStart was cancelled before any phase command was run.");
    return;
  }
  setBusy(true, "SPBAT PostStart");
  setStatus("PostStart running");
  log(`Running SPBAT PostStart for ${spbInstallType().toUpperCase()}.`);
  log(spbatCommand("poststart"));
  try {
    const result = await runSpbPhase("poststart");
    if (result.phaseLog) log(`PostStart phase log: ${result.phaseLog}.`, "pass");
    if (result.reportPath) log(`PostStart report: ${result.reportPath}.`, "pass");
    (result.summaryLines || []).slice(-6).forEach((line) => log(`PostStart report: ${line}`));
    log(spbatStatusCommand());
    if (isOig) {
      state.spbPoststartDone = state.oigScriptDone;
      state.completed.delete("spbPoststart");
      state.failed.delete("spbPoststart");
      setStatus("OIG postinstall pending", "danger");
      log(`SPBAT PostStart completed. OIG postinstall is still required: ${oigScriptPath()}.`, "warn");
      log("Use the OIG helper to prepare patch_oim_wls.profile, confirm passwords, then run patch_oim_wls.sh.", "warn");
      return;
    }
    state.spbPoststartDone = true;
    state.completed.add("spbPoststart");
    state.failed.delete("spbPoststart");
    setStatus("PostStart complete", "good");
    log("SPBAT PostStart completed. Review reports and complete product-specific validation.", "pass");
  } catch (error) {
    state.spbPoststartDone = false;
    state.completed.delete("spbPoststart");
    state.failed.add("spbPoststart");
    setStatus("PostStart failed", "danger");
    log(error.message, "error");
  } finally {
    setBusy(false);
    render();
  }
}

async function restartServices() {
  if (!state.startupConfirmed) {
    state.restartDone = false;
    state.completed.delete("restart");
    log("Confirm that services were started before continuing. PatchPilot will not mark startup complete without confirmation.", "warn");
    render();
    return;
  }
  setBusy(true, "Confirming startup");
  await sleep(150);
  if (isStackPatchBundle()) {
    log("SPB services startup confirmed. You can continue to SPBAT PostStart.", "pass");
  } else {
    log("Services startup confirmed after patching. PatchPilot did not execute customer-specific startup scripts.", "pass");
  }
  if (state.startupNote.trim()) {
    log(`Startup note: ${state.startupNote.trim()}.`, "pass");
  }
  state.restartDone = true;
  state.completed.add("restart");
  setStatus("Services started", "good");
  setBusy(false);
  render();
}

async function finalizeReport() {
  setBusy(true, "Reporting");
  await sleep(250);
  state.report = buildReport(currentReportOutcome());
  state.completed.add("report");
  log(`Final report generated for ${state.report.runId}.`, "pass");
  setBusy(false);
  render();
}

async function generateApplyFailureReport() {
  setBusy(true, "Reporting");
  await sleep(150);
  state.patchApplied = false;
  state.failed.add("apply");
  if (!state.patchResult || state.patchResult.status !== "failed") {
    state.patchResult = buildClientPatchFailureResult(
      "OPatch apply or dry-run failed. Review the captured OPatch output before retrying.",
      state.patchApplyOutput,
      dom.dryRunToggle.checked
    );
  }
  state.report = buildReport("failed");
  state.completed.add("report");
  const reportIndex = workflowSteps().findIndex((step) => step.id === "report");
  if (reportIndex >= 0) state.activeStep = reportIndex;
  log(`Support-ready HTML report generated for patch ${reportPatchLabel(state.report)}. Create an Oracle Support SR and attach this report.`, "pass");
  setBusy(false);
  render();
}

async function generateSpbInactiveFailureReport() {
  setBusy(true, "Reporting");
  await sleep(150);
  state.spbInactiveReviewed = false;
  state.failed.add("spbInactive");
  if (!state.spbInactiveResult || !state.spbInactiveResult.status) {
    state.spbInactiveResult = {
      status: "failed",
      error: state.spbInactiveError || "Inactive patch cleanup failed or did not reach the requested retain level.",
      output: state.spbInactiveError || ""
    };
  }
  state.spbInactiveError = state.spbInactiveError || state.spbInactiveResult.error || "Inactive patch cleanup failed or did not reach the requested retain level.";
  state.report = buildReport("failed");
  state.completed.add("report");
  const reportIndex = workflowSteps().findIndex((step) => step.id === "report");
  if (reportIndex >= 0) state.activeStep = reportIndex;
  log(`Support-ready HTML report generated for inactive patch cleanup failure on ${selectedHome().oracleHome || "selected ORACLE_HOME"}. Create an Oracle Support SR and attach this report.`, "pass");
  setBusy(false);
  render();
}

function buildReport(outcome) {
  const patchDebug = state.patchResult && typeof state.patchResult.debug === "boolean" ? state.patchResult.debug : state.opatchDebugUsed;
  const patchDryRun = state.patchResult && typeof state.patchResult.dryRun === "boolean" ? state.patchResult.dryRun : Boolean(dom.dryRunToggle && dom.dryRunToggle.checked);
  const report = {
    runId: `PATCHPILOT-${new Date().toISOString().slice(0, 10).replaceAll("-", "")}-${safeChangeRef()}`,
    generatedAt: new Date().toISOString(),
    outcome,
    sshTarget: `${state.form.user}@${state.form.host}:${state.form.port}`,
    executionUser: state.form.runAsUser || state.form.user,
    patchType: state.form.patchType,
    applyMode: isBatchNapply() ? "batch" : "single",
    batchSelection: isBatchNapply() ? state.form.batchSelection : "",
    batchPatchIds: isBatchNapply() ? state.form.batchPatchIds : "",
    patchListFile: isBatchNapply() ? state.form.patchListFile : "",
    patchPath: state.form.patchPath,
    readmePath: state.readmePath,
    selectedHome: reportSelectedHome(),
    readmeAnalysis: state.readmeAnalysis || analyzeReadme(state.readmeText),
    opatchHeapOptions: opatchHeapOptions(),
    spbInstallType: isStackPatchBundle() ? spbInstallType() : "",
    spbExtraArgs: isStackPatchBundle() ? spbExtraArgs() : "",
    spbLogDir: isStackPatchBundle() ? spbLogDir() : "",
    servicesUpVerified: state.servicesUpVerified,
    servicesUpManualAccepted: state.servicesUpManualAccepted,
    servicesUpStatus: state.servicesUpStatus,
    servicesUpError: state.servicesUpError,
    servicesUpCheck: state.servicesUpCheck,
    spbFreshInstallNoDomain: state.spbFreshInstallNoDomain,
    spbPrepared: state.spbPrepared,
    spbPrepareResult: state.spbPrepareResult,
    spbInactiveReviewed: state.spbInactiveReviewed,
    spbInactiveRetainLevel: String(spbInactiveRetainLevel()),
    spbInactiveRemoveConfirmed: state.spbInactiveRemoveConfirmed,
    spbInactiveSkipConfirmed: state.spbInactiveSkipConfirmed,
    spbInactiveCheck: state.spbInactiveCheck,
    spbInactiveResult: state.spbInactiveResult,
    spbInactiveError: state.spbInactiveError,
    spbPrestopDone: state.spbPrestopDone,
    spbPrestopExternalApproved: state.spbPrestopExternalApproved,
    spbPrestopExternalNote: state.spbPrestopExternalNote,
    spbDowntimeDone: state.spbDowntimeDone,
    spbPoststartDone: state.spbPoststartDone,
    spbPhaseStatus: state.spbPhaseStatus,
    spbPhaseLogs: state.spbPhaseLogs,
    spbPhaseErrors: state.spbPhaseErrors,
    oigProfileReady: state.oigProfileReady,
    oigProfilePath: state.oigProfile && state.oigProfile.profilePath ? state.oigProfile.profilePath : (isStackPatchBundle() && spbInstallType() === "oig" ? oigProfilePath() : ""),
    oigProfileSaved: state.oigProfileSaved,
    oigPasswordsConfirmed: state.oigPasswordsConfirmed,
    oigManualAccepted: state.oigManualAccepted,
    oigScriptStatus: state.oigScriptStatus,
    oigScriptDone: state.oigScriptDone,
    oigScriptLogPath: state.oigScriptLogPath,
    oigScriptResult: state.oigScriptResult,
    oigScriptError: state.oigScriptError,
    backupCompleted: state.backupsDone,
    backupGateComplete: backupGateComplete(),
    backupShutdownSatisfied: backupShutdownComplete(),
    backupShutdownVerified: isStackPatchBundle() ? state.backupShutdownVerified : state.shutdownVerified,
    backupShutdownOverrideApproved: isStackPatchBundle() ? state.backupShutdownOverrideApproved : state.shutdownOverrideApproved,
    backupShutdownCheck: isStackPatchBundle() ? state.backupShutdownCheck : state.shutdownCheck,
    backupDirectory: backupDirForRun(),
    backupPreflightConfirmed: state.backupPreflightConfirmed,
    backupPreflight: state.backupPreflight,
    backupExternalApproved: state.backupExternalApproved,
    backupExternalNote: state.backupExternalNote,
    databaseBackupRequired: requiresDatabaseBackupForRun(),
    databaseBackupConfirmed: state.databaseBackupConfirmed,
    databaseBackupNote: state.databaseBackupNote,
    backupResult: state.backupResult,
    shutdownVerified: state.shutdownVerified,
    shutdownOverrideApproved: state.shutdownOverrideApproved,
    shutdownCheck: state.shutdownCheck,
    opatchReady: state.opatchReady,
    opatchDebugEnabled: patchDebug,
    patchEstimate: patchDurationEstimate({ debug: patchDebug, dryRun: patchDryRun }),
    patchApplied: state.patchApplied,
    patchResult: state.patchResult,
    patchApplyOutput: state.patchApplyOutput,
    rollbackAvailable: rollbackAvailableForCurrentRun(),
    rollbackDone: state.rollbackDone,
    rollbackResult: state.rollbackResult,
    postinstallConfirmed: state.postinstallConfirmed,
    postinstallRequiresManual: Boolean((state.readmeAnalysis || analyzeReadme(state.readmeText)).postinstallRequiresManual),
    postinstallDone: state.postinstallDone,
    spbCleanupApproved: state.spbCleanupApproved,
    spbCleanupTargets: isStackPatchBundle() ? spbCleanupTargets() : [],
    spbCleanupStatus: state.spbCleanupStatus,
    spbCleanupResult: state.spbCleanupResult,
    spbCleanupAttempts: state.spbCleanupAttempts,
    spbCleanupError: state.spbCleanupError,
    startupConfirmed: state.startupConfirmed,
    startupNote: state.startupNote,
    startupMode: isStackPatchBundle() ? "SPB services started before PostStart" : "Manual/customer service start after patching",
    restartDone: state.restartDone,
    commands: commandPreview({ debug: patchDebug }),
    activityLog: Array.from(dom.activityLog.children).map((node) => node.textContent || "").filter(Boolean),
    completedSteps: Array.from(state.completed),
    failedSteps: Array.from(state.failed)
  };
  if (state.form.customer.trim()) report.customer = state.form.customer.trim();
  if (state.form.changeRef.trim()) report.changeReference = state.form.changeRef.trim();
  return report;
}

function goNext() {
  if (currentStep().id === "report" && state.report) {
    resetRun();
    return;
  }
  if (!canContinue()) return;
  const steps = workflowSteps();
  state.completed.add(currentStep().id);
  if (state.activeStep < steps.length - 1) {
    state.activeStep += 1;
  }
  render();
}

function goBack() {
  if (state.activeStep > 0) {
    state.activeStep -= 1;
  }
  render();
}

function resetRun() {
  state.activeStep = 0;
  state.completed.clear();
  state.failed.clear();
  state.connected = false;
  state.connectionResult = null;
  state.discovered = false;
  state.discoveryError = "";
  discoveredHomes = [];
  state.selectedHomeId = "";
  state.selectedHomePath = "";
  state.readmeText = "";
  state.readmePath = "";
  state.readmeAnalysis = null;
  state.servicesUpConfirmed = false;
  state.servicesUpVerified = false;
  state.servicesUpManualAccepted = false;
  state.servicesUpCheck = null;
  state.servicesUpStatus = "idle";
  state.servicesUpError = "";
  state.spbFreshInstallNoDomain = false;
  state.spbPrepared = false;
  state.spbPrepareResult = null;
  state.spbPrepareStatus = "idle";
  state.spbPrepareError = "";
  resetSpbInactiveState();
  state.spbPrestopDone = false;
  state.spbPrestopExternalApproved = false;
  state.spbPrestopExternalNote = "";
  state.spbDowntimeDone = false;
  state.spbPoststartDone = false;
  state.spbPhaseStatus = { prestop: "idle", downtime: "idle", poststart: "idle" };
  state.spbPhaseLogs = { prestop: "", downtime: "", poststart: "" };
  state.spbPhaseErrors = { prestop: "", downtime: "", poststart: "" };
  state.spbPhaseOutput = { prestop: "", downtime: "", poststart: "" };
  state.spbPhaseJobs = { prestop: null, downtime: null, poststart: null };
  resetSpbCleanupState();
  resetOigPostinstallState();
  resetAllShutdownGates();
  state.backupsDone = false;
  state.backupDirOverride = "";
  state.backupDestinationDirty = false;
  state.backupPreflight = null;
  state.backupPreflightConfirmed = false;
  state.backupExternalApproved = false;
  state.backupExternalNote = "";
  state.databaseBackupConfirmed = false;
  state.databaseBackupNote = "";
  state.backupResult = null;
  state.backupOutput = "";
  state.opatchReady = false;
  state.opatchUpgradeJobId = "";
  state.opatchUpgradeOutput = "";
  state.patchApplied = false;
  state.patchResult = null;
  state.patchApplyOutput = "";
  state.patchProgressPhase = "idle";
  state.patchStartedAt = 0;
  state.opatchDebugUsed = false;
  state.postinstallConfirmed = false;
  state.postinstallDone = false;
  state.startupConfirmed = false;
  state.startupNote = "";
  state.restartDone = false;
  state.rollbackInProgress = false;
  state.rollbackDone = false;
  state.rollbackResult = null;
  state.rollbackOutput = "";
  state.progress = 0;
  state.report = null;
  dom.activityLog.innerHTML = "";
  dom.connectionStatus.textContent = "Ready";
  setStatus("Ready");
  log("PatchPilot guided workflow reset.");
  render();
}

function downloadReport() {
  if (!state.report) return;
  const blob = new Blob([buildReportHtmlDocument(state.report)], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.report.runId.toLowerCase()}-report.html`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function bindGlobalEvents() {
  dom.appThemeSelect.addEventListener("change", () => {
    document.body.dataset.appTheme = dom.appThemeSelect.value;
    localStorage.setItem(STORAGE_KEYS.theme, dom.appThemeSelect.value);
  });

  dom.resetRunButton.addEventListener("click", resetRun);
  dom.downloadReportButton.addEventListener("click", downloadReport);
  dom.backButton.addEventListener("click", goBack);
  dom.nextButton.addEventListener("click", goNext);
  dom.secondaryActionButton.addEventListener("click", runStepAction);
  [dom.dryRunToggle, dom.opatchDebugToggle, dom.autoRollbackToggle, dom.simulateConflictToggle].filter(Boolean).forEach((toggle) => {
    toggle.addEventListener("change", () => {
      render();
    });
  });
  dom.clearLogButton.addEventListener("click", () => {
    dom.activityLog.innerHTML = "";
  });
  dom.rollbackButton.addEventListener("click", () => rollbackPatch("manual"));

  dom.wizardSteps.addEventListener("click", (event) => {
    const button = event.target.closest("[data-step-index]");
    if (!button || state.running) return;
    const nextIndex = Number(button.dataset.stepIndex);
    const steps = workflowSteps();
    if (steps[nextIndex]) {
      state.activeStep = nextIndex;
      render();
    }
  });
}

function init() {
  applyLaunchContext();
  const savedTheme = localStorage.getItem(STORAGE_KEYS.theme) || "redwood";
  dom.appThemeSelect.value = savedTheme;
  document.body.dataset.appTheme = savedTheme;
  bindGlobalEvents();
  log("PatchPilot guided workflow ready.");
  render();
}

function applyLaunchContext() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("source") !== "fmw-dashboard") return;
  const allowedPatchTypes = new Set(["One-off", "PSU", "SPU", "CSPU", "BSU", "Bundle Patch", "Stack Patch Bundle"]);
  const patchType = params.get("patchType") || "";
  state.form.host = (params.get("host") || "").trim();
  state.form.port = (params.get("port") || "22").trim() || "22";
  state.form.user = (params.get("user") || "oracle").trim() || "oracle";
  state.form.runAsUser = (params.get("runAsUser") || "oracle").trim() || "oracle";
  state.form.patchPath = (params.get("patchPath") || "").trim();
  state.form.patchType = allowedPatchTypes.has(patchType) ? patchType : state.form.patchType;
  state.form.customer = (params.get("customer") || "").trim();
  state.form.changeRef = (params.get("changeRef") || "").trim();
  state.selectedHomePath = homePathKey(params.get("oracleHome") || params.get("instanceHome") || "");
  state.integrationContext = {
    environmentId: (params.get("environmentId") || "").trim(),
    customer: state.form.customer,
    product: (params.get("product") || "").trim(),
    oracleHome: (params.get("oracleHome") || "").trim(),
    domainHome: (params.get("domainHome") || "").trim(),
    instanceHome: (params.get("instanceHome") || "").trim()
  };
}

init();
