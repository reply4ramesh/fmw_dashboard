import json
import os
import sqlite3
from datetime import datetime

from config_store import normalize_environment, repair_bootstrap_server_profile, serialize_environment, slugify


def _now_utc_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ensure_parent(path):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)


def _connect(db_path):
    _ensure_parent(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS environments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            environment_id TEXT NOT NULL UNIQUE,
            environment_name TEXT NOT NULL,
            server_host TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _row_to_environment(row):
    if not row:
        return None
    payload = json.loads(row["payload_json"] or "{}")
    payload.setdefault("id", row["environment_id"])
    payload.setdefault("name", row["environment_name"])
    payload.setdefault("server", {})
    payload["server"].setdefault("host", row["server_host"] or "")
    return normalize_environment(repair_bootstrap_server_profile(payload))


def _list_raw(db_path):
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT environment_id, environment_name, server_host, payload_json, created_at, updated_at
            FROM environments
            ORDER BY lower(environment_name), id
            """
        ).fetchall()
    return [_row_to_environment(row) for row in rows]


def list_environments(db_path, include_secret=False):
    return [serialize_environment(item, include_sensitive=include_secret) for item in _list_raw(db_path)]


def get_environment(db_path, environment_id, include_secret=False):
    if not environment_id:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT environment_id, environment_name, server_host, payload_json, created_at, updated_at
            FROM environments
            WHERE environment_id = ?
            """,
            (str(environment_id),),
        ).fetchone()
    env = _row_to_environment(row)
    if not env:
        return None
    if include_secret:
        return env
    return serialize_environment(env, include_sensitive=False)


def _next_unique_environment_id(existing_ids, preferred_id, exclude_id=None):
    taken = {str(item) for item in existing_ids if str(item) != str(exclude_id or "")}
    candidate = slugify(preferred_id)
    if candidate not in taken:
        return candidate
    index = 2
    while True:
        trial = "{0}-{1}".format(candidate, index)
        if trial not in taken:
            return trial
        index += 1


def save_environment(db_path, payload, environment_id=None):
    current = get_environment(db_path, environment_id or (payload or {}).get("id"), include_secret=True)
    normalized = normalize_environment(payload, current)
    existing = _list_raw(db_path)
    normalized["id"] = _next_unique_environment_id(
        [item.get("id") for item in existing],
        normalized.get("id") or normalized.get("name") or "environment",
        exclude_id=(current or {}).get("id"),
    )
    now = _now_utc_iso()
    payload_json = json.dumps(normalized, indent=2, sort_keys=False)
    host = str(((normalized.get("server") or {}).get("host")) or "").strip()

    with _connect(db_path) as conn:
        if current:
            created_at = conn.execute(
                "SELECT created_at FROM environments WHERE environment_id = ?",
                (str(current.get("id")),),
            ).fetchone()
            conn.execute(
                """
                UPDATE environments
                SET environment_id = ?, environment_name = ?, server_host = ?, payload_json = ?, updated_at = ?
                WHERE environment_id = ?
                """,
                (
                    normalized["id"],
                    normalized.get("name") or "IAM Environment",
                    host,
                    payload_json,
                    now,
                    str(current.get("id")),
                ),
            )
            if not created_at:
                conn.execute(
                    "UPDATE environments SET created_at = ? WHERE environment_id = ?",
                    (now, normalized["id"]),
                )
        else:
            conn.execute(
                """
                INSERT INTO environments(environment_id, environment_name, server_host, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["id"],
                    normalized.get("name") or "IAM Environment",
                    host,
                    payload_json,
                    now,
                    now,
                ),
            )
        conn.commit()

    return get_environment(db_path, normalized["id"], include_secret=True)


def delete_environment(db_path, environment_id):
    with _connect(db_path) as conn:
        before = conn.total_changes
        conn.execute("DELETE FROM environments WHERE environment_id = ?", (str(environment_id),))
        conn.commit()
        return conn.total_changes > before


def migrate_config_environments(db_path, config):
    if _list_raw(db_path):
        return
    for environment in (config or {}).get("environments", []):
        save_environment(db_path, environment, environment_id=environment.get("id"))
