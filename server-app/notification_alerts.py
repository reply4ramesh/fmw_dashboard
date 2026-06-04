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


def _alert_state_path(db_path, environment_id):
    root = os.path.join(os.path.dirname(os.path.abspath(db_path)), "notifications")
    if not os.path.isdir(root):
        os.makedirs(root)
    safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in str(environment_id or "environment"))
    return os.path.join(root, "alert-{0}.json".format(safe_id or "environment"))


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


def _event(severity, title, detail):
    return {
        "severity": str(severity or "warning").lower(),
        "title": str(title or "").strip(),
        "detail": str(detail or "").strip(),
    }


def build_dashboard_alert_events(dashboard):
    dashboard = dashboard or {}
    events = []
    overall = str(dashboard.get("status") or "").lower()
    if overall in ("warning", "critical", "down"):
        events.append(_event("critical" if overall in ("critical", "down") else "warning", "Environment status", "Environment is {0}.".format(overall)))

    server = dashboard.get("server") or {}
    if server.get("reachable") is False:
        events.append(_event("critical", "Server unreachable", server.get("error") or "Configured SSH host could not be reached."))
    for key, check in ((server.get("health") or {}).get("checks") or {}).items():
        severity = str((check or {}).get("severity") or "").lower()
        if severity not in ("warning", "critical"):
            continue
        label = (check or {}).get("label") or key
        value = (check or {}).get("value")
        events.append(_event(severity, label, "{0} threshold reached; current value is {1}.".format(label, value)))

    for node in server.get("clusterNodes") or []:
        node_name = node.get("nodeName") or node.get("configuredHost") or "Cluster node"
        if node.get("reachable") is False:
            events.append(_event("critical", "{0} unreachable".format(node_name), node.get("error") or "Cluster node could not be collected."))
        for key, check in (((node.get("health") or {}).get("checks") or {}).items()):
            severity = str((check or {}).get("severity") or "").lower()
            if severity in ("warning", "critical"):
                events.append(_event(severity, "{0} {1}".format(node_name, (check or {}).get("label") or key), "Current value is {0}.".format((check or {}).get("value"))))

    weblogic = ((dashboard.get("productMetrics") or {}).get("weblogic") or {})
    for row in weblogic.get("serverInventory") or []:
        state = str(row.get("state") or "").upper()
        if state and state not in ("RUNNING", "STARTING"):
            events.append(_event("critical", "WebLogic server {0}".format(row.get("name") or row.get("server") or "-"), "Runtime state is {0}.".format(state)))
    if (weblogic.get("stuckThreadCount") or 0) > 0:
        events.append(_event("critical", "WebLogic stuck threads", "{0} stuck thread(s) detected.".format(weblogic.get("stuckThreadCount"))))
    if (weblogic.get("hoggingThreadCount") or 0) > 0:
        events.append(_event("warning", "WebLogic hogging threads", "{0} hogging thread(s) detected.".format(weblogic.get("hoggingThreadCount"))))

    return [event for event in events if event.get("title")]


def _recipient_matches(recipient, severity):
    if not recipient.get("enabled", True):
        return False
    wanted = str(recipient.get("severity") or "any").lower()
    if wanted == "any":
        return True
    return _severity_rank(severity) >= _severity_rank(wanted)


def _send_mail(settings, recipients, subject, body):
    message = EmailMessage()
    sender_name = settings.get("senderName") or "FMW Monitoring Dashboard"
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


def send_environment_alerts(db_path, environment, dashboard, progress=None):
    settings = get_notification_settings(db_path, include_secret=True)
    if not settings.get("enabled"):
        return {"sent": False, "reason": "disabled"}
    if not settings.get("smtpHost") or not settings.get("smtpPort") or not settings.get("senderEmail"):
        return {"sent": False, "reason": "smtp_not_configured"}

    events = build_dashboard_alert_events(dashboard)
    severity = "healthy"
    for event in events:
        if _severity_rank(event.get("severity")) > _severity_rank(severity):
            severity = event.get("severity")

    state_path = _alert_state_path(db_path, environment.get("id") or (dashboard.get("environment") or {}).get("id"))
    previous = _read_alert_state(state_path)
    signature = json.dumps(events, sort_keys=True)
    now = int(time.time())
    if not events:
        if previous.get("signature"):
            recipients = [recipient for recipient in list_notification_recipients(db_path) if _recipient_matches(recipient, "warning")]
            if recipients:
                subject = "FMW Dashboard recovered: {0}".format(environment.get("name") or environment.get("id") or "environment")
                body = "The latest collector snapshot is healthy.\n\nEnvironment: {0}\nStatus: healthy".format(environment.get("name") or environment.get("id") or "environment")
                _send_mail(settings, recipients, subject, body)
        _write_alert_state(state_path, {"signature": "", "sentAt": now, "severity": "healthy"})
        return {"sent": False, "reason": "healthy"}

    if previous.get("signature") == signature and now - int(previous.get("sentAt") or 0) < 21600:
        return {"sent": False, "reason": "deduped"}

    recipients = [recipient for recipient in list_notification_recipients(db_path) if _recipient_matches(recipient, severity)]
    if not recipients:
        _write_alert_state(state_path, {"signature": signature, "sentAt": now, "severity": severity})
        return {"sent": False, "reason": "no_matching_recipients"}

    environment_name = environment.get("name") or environment.get("id") or "environment"
    subject = "FMW Dashboard {0}: {1}".format(severity.upper(), environment_name)
    body_lines = [
        "FMW Monitoring Dashboard detected {0} alert(s).".format(len(events)),
        "",
        "Environment: {0}".format(environment_name),
        "Status: {0}".format((dashboard or {}).get("status") or severity),
        "",
    ]
    for event in events[:20]:
        body_lines.append("[{0}] {1}: {2}".format(str(event.get("severity") or "").upper(), event.get("title"), event.get("detail")))
    if len(events) > 20:
        body_lines.append("... {0} more alert(s) omitted from this email.".format(len(events) - 20))
    _send_mail(settings, recipients, subject, "\n".join(body_lines))
    _write_alert_state(state_path, {"signature": signature, "sentAt": now, "severity": severity})
    if progress:
        progress("Sent {0} notification alert to {1} recipient(s).".format(severity, len(recipients)))
    return {"sent": True, "severity": severity, "recipientCount": len(recipients), "eventCount": len(events)}
