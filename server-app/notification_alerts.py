import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage

from notification_store import get_notification_settings, list_notification_recipients


SEVERITY_RANK = {"healthy": 0, "configured": 0, "warning": 1, "critical": 2, "down": 2}


def _severity_rank(value):
    return SEVERITY_RANK.get(str(value or "").strip().lower(), 0)


def _alert_state_path(db_path, environment_id, prefix="alert"):
    root = os.path.join(os.path.dirname(os.path.abspath(db_path)), "notifications")
    if not os.path.isdir(root):
        os.makedirs(root)
    safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in str(environment_id or "environment"))
    return os.path.join(root, "{0}-{1}.json".format(prefix, safe_id or "environment"))


def _read_alert_state(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _write_alert_state(path, payload):
    temp_path = "{0}.tmp".format(path)
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temp_path, path)


def _event(event_type, severity, title, detail):
    return {
        "eventType": str(event_type or "applicationHealth").strip(),
        "severity": str(severity or "warning").lower(),
        "title": str(title or "").strip(),
        "detail": str(detail or "").strip(),
    }


def _event_enabled(settings, event_type):
    return bool(((settings or {}).get("eventTypes") or {}).get(event_type, False))


def _numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _threshold_severity(value, warning, critical):
    numeric = _numeric(value)
    if numeric is None:
        return None
    if numeric >= float(critical):
        return "critical"
    if numeric >= float(warning):
        return "warning"
    return None


def _server_metric_events(server, settings, label="Server"):
    server = server or {}
    thresholds = settings.get("thresholds") or {}
    events = []
    if server.get("reachable") is False and _event_enabled(settings, "serverUnreachable"):
        events.append(_event("serverUnreachable", "critical", "{0} unreachable".format(label), server.get("error") or "Configured SSH host could not be reached."))
        return events

    uptime = server.get("uptime") or {}
    cpu = uptime.get("cpuBreakdown") or {}
    idle = _numeric(cpu.get("idlePercent"))
    used = round(100.0 - idle, 1) if idle is not None else _numeric(uptime.get("cpuPressure"))
    if _event_enabled(settings, "cpu"):
        severity = _threshold_severity(used, thresholds.get("cpuWarning", 85), thresholds.get("cpuCritical", 95))
        if severity:
            events.append(_event("cpu", severity, "{0} CPU utilization".format(label), "CPU utilization is {0}%; warning {1}%, critical {2}%.".format(used, thresholds.get("cpuWarning", 85), thresholds.get("cpuCritical", 95))))

    memory_used = (server.get("memory") or {}).get("usedPercent")
    if _event_enabled(settings, "memory"):
        severity = _threshold_severity(memory_used, thresholds.get("memoryWarning", 85), thresholds.get("memoryCritical", 92))
        if severity:
            events.append(_event("memory", severity, "{0} memory utilization".format(label), "Memory utilization is {0}%; warning {1}%, critical {2}%.".format(memory_used, thresholds.get("memoryWarning", 85), thresholds.get("memoryCritical", 92))))

    if _event_enabled(settings, "disk"):
        disks = []
        for disk_label, key in (("Root filesystem", "rootDisk"), ("Refresh filesystem", "refreshDisk")):
            disk = server.get(key) or {}
            if disk.get("size") or disk.get("usedPercent") is not None:
                disks.append((disk_label, disk))
        for row in server.get("productFilesystems") or []:
            labels = ", ".join(row.get("labels") or [])
            mount = row.get("mount") or row.get("filesystem") or "configured filesystem"
            disks.append(("{0}{1}".format(mount, " ({0})".format(labels) if labels else ""), row))
        seen = set()
        for disk_label, disk in disks:
            key = (disk_label, disk.get("usedPercent"))
            if key in seen:
                continue
            seen.add(key)
            used_percent = disk.get("usedPercent")
            severity = _threshold_severity(used_percent, thresholds.get("diskWarning", 80), thresholds.get("diskCritical", 90))
            if severity:
                events.append(_event("disk", severity, "{0} disk utilization".format(label), "{0} is {1}% used; warning {2}%, critical {3}%.".format(disk_label, used_percent, thresholds.get("diskWarning", 80), thresholds.get("diskCritical", 90))))
    return events


def build_dashboard_alert_events(dashboard, settings=None):
    dashboard = dashboard or {}
    settings = settings or get_notification_settings(":memory:")
    thresholds = settings.get("thresholds") or {}
    events = []
    overall = str(dashboard.get("status") or "").lower()
    if _event_enabled(settings, "environmentStatus") and overall in ("warning", "critical", "down"):
        severity = "critical" if overall in ("critical", "down") else "warning"
        events.append(_event("environmentStatus", severity, "Environment status", "Environment is {0}.".format(overall)))
    server = dashboard.get("server") or {}
    events.extend(_server_metric_events(server, settings, "Primary server"))
    for node in server.get("clusterNodes") or []:
        node_name = node.get("nodeName") or node.get("configuredHost") or "Cluster node"
        events.extend(_server_metric_events(node, settings, node_name))

    if _event_enabled(settings, "applicationHealth"):
        for check in dashboard.get("appChecks") or []:
            status = str(check.get("status") or "").lower()
            if status in ("warning", "critical", "down"):
                severity = "critical" if status in ("critical", "down") else "warning"
                title = check.get("name") or check.get("label") or check.get("url") or "Application check"
                detail = check.get("message") or check.get("error") or "Application check returned {0}.".format(status)
                events.append(_event("applicationHealth", severity, title, detail))

    weblogic = ((dashboard.get("productMetrics") or {}).get("weblogic") or {})
    if _event_enabled(settings, "weblogicState"):
        for row in weblogic.get("serverInventory") or []:
            state = str(row.get("state") or "").upper()
            if state and state not in ("RUNNING", "STARTING"):
                events.append(_event("weblogicState", "critical", "WebLogic server {0}".format(row.get("name") or row.get("server") or "-"), "Runtime state is {0}.".format(state)))

    stuck_count = int(weblogic.get("stuckThreadCount") or 0)
    if _event_enabled(settings, "stuckThreads"):
        severity = _threshold_severity(stuck_count, thresholds.get("stuckThreadsWarning", 1), thresholds.get("stuckThreadsCritical", 5))
        if severity:
            events.append(_event("stuckThreads", severity, "WebLogic stuck threads", "{0} stuck thread(s) detected; warning {1}, critical {2}.".format(stuck_count, thresholds.get("stuckThreadsWarning", 1), thresholds.get("stuckThreadsCritical", 5))))

    hogging_count = int(weblogic.get("hoggingThreadCount") or 0)
    if _event_enabled(settings, "hoggingThreads"):
        severity = _threshold_severity(hogging_count, thresholds.get("hoggingThreadsWarning", 1), thresholds.get("hoggingThreadsCritical", 10))
        if severity:
            events.append(_event("hoggingThreads", severity, "WebLogic hogging threads", "{0} hogging thread(s) detected; warning {1}, critical {2}.".format(hogging_count, thresholds.get("hoggingThreadsWarning", 1), thresholds.get("hoggingThreadsCritical", 10))))
    return [event for event in events if event.get("title")]


def _recipient_matches(recipient, severity):
    if not recipient.get("enabled", True):
        return False
    wanted = str(recipient.get("severity") or "any").lower()
    if wanted == "any":
        return True
    return _severity_rank(severity) >= _severity_rank(wanted)


def _settings_ready(settings):
    return bool(settings.get("enabled") and settings.get("smtpHost") and settings.get("smtpPort") and settings.get("senderEmail"))


def _send_mail(settings, recipients, subject, body):
    message = EmailMessage()
    sender_name = settings.get("senderName") or "FMW Operations Console"
    sender_email = settings.get("senderEmail") or settings.get("smtpUsername") or ""
    message["Subject"] = subject
    message["From"] = "{0} <{1}>".format(sender_name, sender_email)
    message["To"] = ", ".join([recipient["email"] for recipient in recipients])
    message.set_content(body)

    host = settings.get("smtpHost")
    port = int(settings.get("smtpPort") or 0)
    username = settings.get("smtpUsername") or ""
    password = settings.get("smtpPassword") or ""
    mode = settings.get("securityMode") or "starttls"
    if mode == "ssl":
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            if mode == "starttls":
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)


def _recipients_for_severity(db_path, severity):
    return [recipient for recipient in list_notification_recipients(db_path) if _recipient_matches(recipient, severity)]


def send_environment_alerts(db_path, environment, dashboard, progress=None):
    settings = get_notification_settings(db_path, include_secret=True)
    if not settings.get("enabled"):
        return {"sent": False, "reason": "disabled"}
    if not _settings_ready(settings):
        return {"sent": False, "reason": "smtp_not_configured"}

    events = build_dashboard_alert_events(dashboard, settings=settings)
    severity = "healthy"
    for event in events:
        if _severity_rank(event.get("severity")) > _severity_rank(severity):
            severity = event.get("severity")

    environment_id = environment.get("id") or (dashboard.get("environment") or {}).get("id")
    state_path = _alert_state_path(db_path, environment_id)
    previous = _read_alert_state(state_path)
    signature = json.dumps(events, sort_keys=True)
    now = int(time.time())
    if not events:
        sent = False
        recipient_count = 0
        if previous.get("signature") and _event_enabled(settings, "recovery"):
            recipients = _recipients_for_severity(db_path, "warning")
            if recipients:
                environment_name = environment.get("name") or environment_id or "environment"
                subject = "FMW Operations Console recovered: {0}".format(environment_name)
                body = "The latest collector snapshot no longer contains the previous alert conditions.\n\nEnvironment: {0}\nStatus: {1}".format(environment_name, (dashboard or {}).get("status") or "healthy")
                _send_mail(settings, recipients, subject, body)
                sent = True
                recipient_count = len(recipients)
        _write_alert_state(state_path, {"signature": "", "sentAt": now, "severity": "healthy"})
        return {"sent": sent, "reason": "recovered" if sent else "healthy", "recipientCount": recipient_count}

    repeat_seconds = max(300, int(settings.get("repeatMinutes") or 360) * 60)
    if previous.get("signature") == signature and now - int(previous.get("sentAt") or 0) < repeat_seconds:
        return {"sent": False, "reason": "deduped"}

    recipients = _recipients_for_severity(db_path, severity)
    if not recipients:
        return {"sent": False, "reason": "no_matching_recipients"}

    environment_name = environment.get("name") or environment_id or "environment"
    subject = "FMW Operations Console {0}: {1}".format(severity.upper(), environment_name)
    body_lines = [
        "FMW Operations Console detected {0} alert(s).".format(len(events)),
        "",
        "Environment: {0}".format(environment_name),
        "Status: {0}".format((dashboard or {}).get("status") or severity),
        "Collected: {0}".format((dashboard or {}).get("generatedAt") or "-"),
        "",
    ]
    for event in events[:30]:
        body_lines.append("[{0}] {1}: {2}".format(str(event.get("severity") or "").upper(), event.get("title"), event.get("detail")))
    if len(events) > 30:
        body_lines.append("... {0} more alert(s) omitted from this email.".format(len(events) - 30))
    _send_mail(settings, recipients, subject, "\n".join(body_lines))
    _write_alert_state(state_path, {"signature": signature, "sentAt": now, "severity": severity})
    if progress:
        progress("Sent {0} notification alert to {1} recipient(s).".format(severity, len(recipients)))
    return {"sent": True, "severity": severity, "recipientCount": len(recipients), "eventCount": len(events)}


def send_collection_success_notification(db_path, environment, dashboard, progress=None):
    settings = get_notification_settings(db_path, include_secret=True)
    if not _settings_ready(settings) or not _event_enabled(settings, "collectorSuccess"):
        return {"sent": False, "reason": "disabled"}
    recipients = _recipients_for_severity(db_path, "healthy")
    if not recipients:
        return {"sent": False, "reason": "no_matching_recipients"}
    environment_name = environment.get("name") or environment.get("id") or "environment"
    job = (dashboard or {}).get("job") or {}
    subject = "FMW Operations Console collection completed: {0}".format(environment_name)
    body = "\n".join([
        "The environment collector completed successfully.",
        "",
        "Environment: {0}".format(environment_name),
        "Dashboard status: {0}".format((dashboard or {}).get("status") or "-"),
        "Trigger: {0}".format(job.get("trigger") or "scheduled"),
        "Duration: {0} ms".format(job.get("durationMs") or 0),
        "Collected: {0}".format((dashboard or {}).get("generatedAt") or "-"),
    ])
    _send_mail(settings, recipients, subject, body)
    _write_alert_state(_alert_state_path(db_path, environment.get("id"), prefix="failure"), {"signature": "", "sentAt": int(time.time()), "severity": "healthy"})
    if progress:
        progress("Sent collector success notification to {0} recipient(s).".format(len(recipients)))
    return {"sent": True, "recipientCount": len(recipients)}


def send_collection_failure_notification(db_path, environment, error, trigger="scheduled", progress=None):
    settings = get_notification_settings(db_path, include_secret=True)
    if not _settings_ready(settings) or not _event_enabled(settings, "collectorFailure"):
        return {"sent": False, "reason": "disabled"}
    recipients = _recipients_for_severity(db_path, "critical")
    if not recipients:
        return {"sent": False, "reason": "no_matching_recipients"}
    environment_id = environment.get("id") or "environment"
    state_path = _alert_state_path(db_path, environment_id, prefix="failure")
    signature = str(error or "Collector failed").strip()
    previous = _read_alert_state(state_path)
    now = int(time.time())
    repeat_seconds = max(300, int(settings.get("repeatMinutes") or 360) * 60)
    if previous.get("signature") == signature and now - int(previous.get("sentAt") or 0) < repeat_seconds:
        return {"sent": False, "reason": "deduped"}
    environment_name = environment.get("name") or environment_id
    subject = "FMW Operations Console collector failed: {0}".format(environment_name)
    body = "\n".join([
        "The environment collector failed before completing its snapshot.",
        "",
        "Environment: {0}".format(environment_name),
        "Trigger: {0}".format(trigger or "scheduled"),
        "Error: {0}".format(signature),
    ])
    _send_mail(settings, recipients, subject, body)
    _write_alert_state(state_path, {"signature": signature, "sentAt": now, "severity": "critical"})
    if progress:
        progress("Sent collector failure notification to {0} recipient(s).".format(len(recipients)))
    return {"sent": True, "recipientCount": len(recipients)}
