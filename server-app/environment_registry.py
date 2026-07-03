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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_settings (
            setting_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _clear_inherited_paths(environment, paths):
    for path in paths or []:
        parts = str(path).split(".")
        if parts and parts[0] == "cluster":
            for node in ((((environment.get("weblogic") or {}).get("cluster") or {}).get("nodes")) or []):
                node[parts[-1]] = ""
            continue
        target = environment
        for part in parts[:-1]:
            target = target.get(part) if isinstance(target, dict) else None
            if target is None:
                break
        if isinstance(target, dict) and parts:
            target[parts[-1]] = ""
    return environment


def _row_to_environment(row):
    if not row:
        return None
    payload = json.loads(row["payload_json"] or "{}")
    payload.setdefault("id", row["environment_id"])
    payload.setdefault("name", row["environment_name"])
    payload.setdefault("server", {})
    payload["server"].setdefault("host", row["server_host"] or "")
    inherited_paths = payload.get("inheritGlobalDefaults") or []
    normalized = normalize_environment(repair_bootstrap_server_profile(payload))
    normalized["inheritGlobalDefaults"] = inherited_paths
    return _clear_inherited_paths(normalized, inherited_paths)


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


def default_global_defaults():
    return {
        "ssh": {"username": "", "sshMode": "", "password": "", "privateKeyPath": "", "passphrase": "", "port": 22},
        "weblogic": {"oracleHome": "", "adminUsername": "", "adminPassword": ""},
        "keystores": {
            "identityPath": "", "identityType": "JKS", "identityPassword": "",
            "trustPath": "", "trustType": "JKS", "trustPassword": "",
        },
        "history": {"retentionDays": 90, "maxSnapshots": 500},
    }


def get_global_defaults(db_path, include_secret=False):
    defaults = default_global_defaults()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT payload_json FROM global_settings WHERE setting_key = 'connection_defaults'").fetchone()
    if row:
        saved = json.loads(row["payload_json"] or "{}")
        for section in defaults:
            if isinstance(defaults[section], dict):
                defaults[section].update(saved.get(section) or {})
    if not include_secret:
        for section, keys in (("ssh", ("password", "passphrase")), ("weblogic", ("adminPassword",)), ("keystores", ("identityPassword", "trustPassword"))):
            for key in keys:
                value = defaults[section].pop(key, "")
                defaults[section]["has{0}".format(key[0].upper() + key[1:])] = bool(value)
    return defaults


def save_global_defaults(db_path, payload):
    current = get_global_defaults(db_path, include_secret=True)
    payload = payload or {}
    normalized = default_global_defaults()
    for section in normalized:
        incoming = payload.get(section) or {}
        for key, default_value in normalized[section].items():
            value = incoming.get(key)
            if key.lower().endswith("password") or key == "passphrase":
                value = value if value not in (None, "") else (current.get(section) or {}).get(key, "")
            if isinstance(default_value, int):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = default_value
            normalized[section][key] = default_value if value is None else value
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO global_settings(setting_key, payload_json, updated_at) VALUES ('connection_defaults', ?, ?)",
            (json.dumps(normalized, indent=2), _now_utc_iso()),
        )
        conn.commit()
    return get_global_defaults(db_path, include_secret=False)


def apply_global_defaults(environment, defaults):
    environment = environment or {}
    defaults = defaults or default_global_defaults()
    inherited = []
    ssh = defaults.get("ssh") or {}
    weblogic_defaults = defaults.get("weblogic") or {}
    keystore_defaults = defaults.get("keystores") or {}

    def inherit(target, key, value, label):
        if value not in (None, "") and target.get(key) in (None, ""):
            target[key] = value
            inherited.append(label)

    server = environment.setdefault("server", {})
    for key in ("username", "sshMode", "password", "privateKeyPath", "passphrase", "port"):
        inherit(server, key, ssh.get(key), "server.{0}".format(key))
    weblogic = environment.setdefault("weblogic", {})
    inherit(weblogic, "oracleHome", weblogic_defaults.get("oracleHome"), "weblogic.oracleHome")
    inherit(weblogic, "adminUsername", weblogic_defaults.get("adminUsername"), "weblogic.adminUsername")
    inherit(weblogic, "adminPassword", weblogic_defaults.get("adminPassword"), "weblogic.adminPassword")
    admin_host = weblogic.setdefault("adminHost", {})
    for key in ("username", "sshMode", "password", "privateKeyPath", "passphrase", "port"):
        inherit(admin_host, key, ssh.get(key), "weblogic.adminHost.{0}".format(key))
    keystores = weblogic.setdefault("keystores", {})
    for key in ("identityPath", "identityType", "identityPassword", "trustPath", "trustType", "trustPassword"):
        inherit(keystores, key, keystore_defaults.get(key), "weblogic.keystores.{0}".format(key))
    for node in ((weblogic.get("cluster") or {}).get("nodes") or []):
        for key in ("username", "sshMode", "password", "privateKeyPath", "passphrase", "port"):
            inherit(node, key, ssh.get(key), "cluster.{0}".format(key))
        inherit(node, "oracleHome", weblogic.get("oracleHome"), "cluster.oracleHome")
    environment["inheritedDefaults"] = inherited
    environment["globalDefaultsApplied"] = bool(inherited)
    return environment


def list_environments(db_path, include_secret=False):
    defaults = get_global_defaults(db_path, include_secret=True)
    return [serialize_environment(apply_global_defaults(item, defaults), include_sensitive=include_secret) for item in _list_raw(db_path)]


def get_environment(db_path, environment_id, include_secret=False, apply_defaults=True):
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
    if apply_defaults:
        env = apply_global_defaults(env, get_global_defaults(db_path, include_secret=True))
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
    current = get_environment(db_path, environment_id or (payload or {}).get("id"), include_secret=True, apply_defaults=False)
    payload = json.loads(json.dumps(payload or {}))
    inheritable_paths = (
        "server.username", "server.sshMode", "server.password", "server.privateKeyPath", "server.passphrase",
        "weblogic.oracleHome", "weblogic.adminUsername", "weblogic.adminPassword",
        "weblogic.adminHost.username", "weblogic.adminHost.sshMode", "weblogic.adminHost.password",
        "weblogic.adminHost.privateKeyPath", "weblogic.adminHost.passphrase",
        "weblogic.keystores.identityPath", "weblogic.keystores.identityType", "weblogic.keystores.identityPassword",
        "weblogic.keystores.trustPath", "weblogic.keystores.trustType", "weblogic.keystores.trustPassword",
    )
    explicitly_blank = set()
    for path in inheritable_paths:
        parts = path.split(".")
        target = payload
        present = True
        for part in parts[:-1]:
            if not isinstance(target, dict) or part not in target:
                present = False
                break
            target = target.get(part)
        if present and isinstance(target, dict) and parts[-1] in target and target.get(parts[-1]) in (None, ""):
            explicitly_blank.add(path)
    # Values shown as inherited must remain inherited when an unrelated field is edited.
    inherited_paths = set(payload.pop("inheritedDefaults", []) or [])
    for path in inherited_paths:
        parts = str(path).split(".")
        if parts and parts[0] == "cluster":
            for node in ((((payload.get("weblogic") or {}).get("cluster") or {}).get("nodes")) or []):
                node.pop(parts[-1], None)
            continue
        target = payload
        for part in parts[:-1]:
            target = target.get(part) if isinstance(target, dict) else None
            if target is None:
                break
        if isinstance(target, dict) and parts:
            target.pop(parts[-1], None)
    normalized = normalize_environment(payload, current)
    normalized["inheritGlobalDefaults"] = sorted(explicitly_blank | inherited_paths)
    _clear_inherited_paths(normalized, normalized["inheritGlobalDefaults"])
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
