import os
import sqlite3
from datetime import datetime


DEFAULT_NOTIFICATION_SETTINGS = {
    "smtpHost": "",
    "smtpPort": 587,
    "smtpUsername": "",
    "smtpPassword": "",
    "senderName": "FMW Monitoring Dashboard",
    "senderEmail": "",
    "securityMode": "starttls",
    "enabled": False,
}

VALID_SECURITY_MODES = {"none", "starttls", "ssl"}
VALID_SEVERITIES = {"any", "warning", "critical"}


def _now_utc_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _connect(db_path):
    directory = os.path.dirname(db_path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            smtp_host TEXT NOT NULL DEFAULT '',
            smtp_port INTEGER NOT NULL DEFAULT 587,
            smtp_username TEXT NOT NULL DEFAULT '',
            smtp_password TEXT NOT NULL DEFAULT '',
            sender_name TEXT NOT NULL DEFAULT '',
            sender_email TEXT NOT NULL DEFAULT '',
            security_mode TEXT NOT NULL DEFAULT 'starttls',
            enabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'any',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def _normalize_security_mode(value):
    mode = str(value or DEFAULT_NOTIFICATION_SETTINGS["securityMode"]).strip().lower()
    return mode if mode in VALID_SECURITY_MODES else DEFAULT_NOTIFICATION_SETTINGS["securityMode"]


def _normalize_severity(value):
    severity = str(value or "any").strip().lower()
    return severity if severity in VALID_SEVERITIES else "any"


def _settings_from_row(row, include_secret=False):
    settings = dict(DEFAULT_NOTIFICATION_SETTINGS)
    if row:
        settings.update(
            {
                "smtpHost": row["smtp_host"] or "",
                "smtpPort": row["smtp_port"] or 587,
                "smtpUsername": row["smtp_username"] or "",
                "smtpPassword": row["smtp_password"] or "",
                "senderName": row["sender_name"] or "",
                "senderEmail": row["sender_email"] or "",
                "securityMode": _normalize_security_mode(row["security_mode"]),
                "enabled": bool(row["enabled"]),
            }
        )
    if include_secret:
        return settings
    settings["hasPassword"] = bool(settings.get("smtpPassword"))
    settings["smtpPassword"] = ""
    return settings


def get_notification_settings(db_path, include_secret=False):
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT smtp_host, smtp_port, smtp_username, smtp_password, sender_name,
                   sender_email, security_mode, enabled
            FROM notification_settings
            WHERE id = 1
            """
        ).fetchone()
    return _settings_from_row(row, include_secret=include_secret)


def save_notification_settings(db_path, payload):
    payload = payload or {}
    current = get_notification_settings(db_path, include_secret=True)
    now = _now_utc_iso()
    smtp_password = str(payload.get("smtpPassword") or "").strip() or current.get("smtpPassword", "")
    settings = {
        "smtpHost": str(payload.get("smtpHost") or current.get("smtpHost") or "").strip(),
        "smtpPort": _coerce_int(payload.get("smtpPort"), current.get("smtpPort") or 587),
        "smtpUsername": str(payload.get("smtpUsername") or current.get("smtpUsername") or "").strip(),
        "smtpPassword": smtp_password,
        "senderName": str(payload.get("senderName") or current.get("senderName") or "").strip() or DEFAULT_NOTIFICATION_SETTINGS["senderName"],
        "senderEmail": str(payload.get("senderEmail") or current.get("senderEmail") or "").strip(),
        "securityMode": _normalize_security_mode(payload.get("securityMode") or current.get("securityMode")),
        "enabled": _coerce_bool(payload.get("enabled"), current.get("enabled", False)),
    }

    with _connect(db_path) as conn:
        existing = conn.execute("SELECT created_at FROM notification_settings WHERE id = 1").fetchone()
        if existing:
            conn.execute(
                """
                UPDATE notification_settings
                SET smtp_host = ?, smtp_port = ?, smtp_username = ?, smtp_password = ?,
                    sender_name = ?, sender_email = ?, security_mode = ?, enabled = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    settings["smtpHost"],
                    settings["smtpPort"],
                    settings["smtpUsername"],
                    settings["smtpPassword"],
                    settings["senderName"],
                    settings["senderEmail"],
                    settings["securityMode"],
                    1 if settings["enabled"] else 0,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO notification_settings(
                    id, smtp_host, smtp_port, smtp_username, smtp_password,
                    sender_name, sender_email, security_mode, enabled, created_at, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settings["smtpHost"],
                    settings["smtpPort"],
                    settings["smtpUsername"],
                    settings["smtpPassword"],
                    settings["senderName"],
                    settings["senderEmail"],
                    settings["securityMode"],
                    1 if settings["enabled"] else 0,
                    now,
                    now,
                ),
            )
        conn.commit()

    return get_notification_settings(db_path, include_secret=False)


def list_notification_recipients(db_path):
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, name, email, severity, enabled, created_at, updated_at
            FROM notification_recipients
            ORDER BY lower(name), id
            """
        ).fetchall()
    recipients = []
    for row in rows:
        recipients.append(
            {
                "id": row["id"],
                "name": row["name"] or "",
                "email": row["email"] or "",
                "severity": _normalize_severity(row["severity"]),
                "enabled": bool(row["enabled"]),
            }
        )
    return recipients


def save_notification_recipient(db_path, payload):
    payload = payload or {}
    name = str(payload.get("name") or "").strip()
    email = str(payload.get("email") or "").strip()
    recipient_id = payload.get("id")
    if not name:
        raise ValueError("Recipient name is required.")
    if not email:
        raise ValueError("Recipient email is required.")

    severity = _normalize_severity(payload.get("severity"))
    enabled = _coerce_bool(payload.get("enabled"), True)
    now = _now_utc_iso()

    with _connect(db_path) as conn:
        if recipient_id:
            existing = conn.execute("SELECT id FROM notification_recipients WHERE id = ?", (int(recipient_id),)).fetchone()
            if not existing:
                raise ValueError("Recipient not found.")
            conn.execute(
                """
                UPDATE notification_recipients
                SET name = ?, email = ?, severity = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, email, severity, 1 if enabled else 0, now, int(recipient_id)),
            )
        else:
            conn.execute(
                """
                INSERT INTO notification_recipients(name, email, severity, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, email, severity, 1 if enabled else 0, now, now),
            )
        conn.commit()

    return list_notification_recipients(db_path)


def delete_notification_recipient(db_path, recipient_id):
    with _connect(db_path) as conn:
        before = conn.total_changes
        conn.execute("DELETE FROM notification_recipients WHERE id = ?", (int(recipient_id),))
        conn.commit()
        return conn.total_changes > before


def notification_payload(db_path):
    return {
        "settings": get_notification_settings(db_path, include_secret=False),
        "recipients": list_notification_recipients(db_path),
    }
