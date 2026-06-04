import os
import sqlite3
from datetime import datetime


DEFAULT_UPDATE_PROXY_SETTINGS = {
    "httpProxy": "",
    "httpsProxy": "",
    "noProxy": "",
    "configured": False,
}


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
        CREATE TABLE IF NOT EXISTS support_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            update_http_proxy TEXT NOT NULL DEFAULT '',
            update_https_proxy TEXT NOT NULL DEFAULT '',
            update_no_proxy TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _settings_from_row(row):
    settings = dict(DEFAULT_UPDATE_PROXY_SETTINGS)
    if row:
        settings.update(
            {
                "httpProxy": row["update_http_proxy"] or "",
                "httpsProxy": row["update_https_proxy"] or "",
                "noProxy": row["update_no_proxy"] or "",
            }
        )
    settings["configured"] = bool(settings.get("httpProxy") or settings.get("httpsProxy") or settings.get("noProxy"))
    return settings


def get_update_proxy_settings(db_path):
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT update_http_proxy, update_https_proxy, update_no_proxy
            FROM support_settings
            WHERE id = 1
            """
        ).fetchone()
    return _settings_from_row(row)


def save_update_proxy_settings(db_path, payload):
    payload = payload or {}
    current = get_update_proxy_settings(db_path)
    now = _now_utc_iso()
    settings = {
        "httpProxy": str(payload.get("httpProxy", current.get("httpProxy") or "") or "").strip(),
        "httpsProxy": str(payload.get("httpsProxy", current.get("httpsProxy") or "") or "").strip(),
        "noProxy": str(payload.get("noProxy", current.get("noProxy") or "") or "").strip(),
    }

    with _connect(db_path) as conn:
        existing = conn.execute("SELECT created_at FROM support_settings WHERE id = 1").fetchone()
        if existing:
            conn.execute(
                """
                UPDATE support_settings
                SET update_http_proxy = ?, update_https_proxy = ?, update_no_proxy = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    settings["httpProxy"],
                    settings["httpsProxy"],
                    settings["noProxy"],
                    now,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO support_settings(
                    id, update_http_proxy, update_https_proxy, update_no_proxy, created_at, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    settings["httpProxy"],
                    settings["httpsProxy"],
                    settings["noProxy"],
                    now,
                    now,
                ),
            )
        conn.commit()

    return get_update_proxy_settings(db_path)
