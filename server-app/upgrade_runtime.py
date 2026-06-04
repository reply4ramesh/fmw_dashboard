import json
import os
import time
import uuid
try:
    import pwd
except ImportError:
    pwd = None


BUSY_UPGRADE_STATUSES = {
    "queued",
    "starting",
    "downloading",
    "applying",
}

DEFAULT_SERVICE_USER = os.environ.get("IAM_MONITORING_SERVICE_USER", "iam-monitoring")


def _state_root(db_path):
    return os.path.dirname(os.path.abspath(db_path))


def _upgrade_dir(db_path):
    return os.path.join(_state_root(db_path), "upgrade")


def _service_account_ids():
    service_user = str(os.environ.get("IAM_MONITORING_SERVICE_USER") or DEFAULT_SERVICE_USER).strip() or DEFAULT_SERVICE_USER
    if pwd is None:
        return None, None
    try:
        record = pwd.getpwnam(service_user)
    except Exception:
        return None, None
    return record.pw_uid, record.pw_gid


def _apply_permissions(path, is_dir=False):
    mode = 0o775 if is_dir else 0o664
    try:
        os.chmod(path, mode)
    except Exception:
        pass
    uid, gid = _service_account_ids()
    if uid is None or gid is None:
        return
    try:
        os.chown(path, uid, gid)
    except Exception:
        pass


def _repair_upgrade_layout(db_path):
    upgrade_dir = _upgrade_dir(db_path)
    if os.path.isdir(upgrade_dir):
        _apply_permissions(upgrade_dir, is_dir=True)
    for path in (upgrade_request_path(db_path), upgrade_status_path(db_path), upgrade_log_path(db_path)):
        if os.path.exists(path):
            _apply_permissions(path, is_dir=False)


def upgrade_request_path(db_path):
    return os.path.join(_upgrade_dir(db_path), "github-upgrade-request.json")


def upgrade_status_path(db_path):
    return os.path.join(_upgrade_dir(db_path), "github-upgrade-status.json")


def upgrade_log_path(db_path):
    return os.path.join(_upgrade_dir(db_path), "github-upgrade.log")


def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)
    _apply_permissions(path, is_dir=True)


def ensure_upgrade_layout(db_path):
    _ensure_dir(_upgrade_dir(db_path))
    _repair_upgrade_layout(db_path)


def _now_utc_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _local_log_timestamp():
    return time.strftime("%Y-%m-%d %I:%M:%S %p %Z", time.localtime())


def _read_json(path, default_value):
    if not os.path.isfile(path):
        return default_value
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default_value


def _write_json(path, payload):
    parent = os.path.dirname(path)
    if parent:
        _ensure_dir(parent)
    temp_path = "{0}.tmp".format(path)
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(temp_path, path)
    _apply_permissions(path, is_dir=False)


def _tail(path, max_lines=12):
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
        return "".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


def default_upgrade_status(db_path):
    return {
        "status": "idle",
        "requestedAt": "",
        "startedAt": "",
        "finishedAt": "",
        "message": "No GitHub upgrade has been requested.",
        "repoUrl": "",
        "archiveUrl": "",
        "branch": "main",
        "currentVersion": "",
        "targetVersion": "",
        "requestId": "",
        "lastError": "",
        "logPath": upgrade_log_path(db_path),
        "logTail": "",
    }


def read_upgrade_status(db_path):
    ensure_upgrade_layout(db_path)
    status = default_upgrade_status(db_path)
    status.update(_read_json(upgrade_status_path(db_path), {}))
    status["logPath"] = upgrade_log_path(db_path)
    status["logTail"] = _tail(status["logPath"])
    return status


def write_upgrade_status(db_path, payload):
    ensure_upgrade_layout(db_path)
    current = read_upgrade_status(db_path)
    merged = dict(current)
    merged.update(payload or {})
    merged["logPath"] = upgrade_log_path(db_path)
    _write_json(upgrade_status_path(db_path), merged)
    return read_upgrade_status(db_path)


def append_upgrade_log(db_path, message):
    ensure_upgrade_layout(db_path)
    path = upgrade_log_path(db_path)
    line = "[{0}] {1}\n".format(_local_log_timestamp(), str(message))
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    _apply_permissions(path, is_dir=False)
    return path


def upgrade_is_busy(status_name):
    return str(status_name or "").strip().lower() in BUSY_UPGRADE_STATUSES


def queue_github_upgrade(db_path, payload):
    ensure_upgrade_layout(db_path)
    current = read_upgrade_status(db_path)
    request_path = upgrade_request_path(db_path)
    if upgrade_is_busy(current.get("status")) or os.path.isfile(request_path):
        raise ValueError("A GitHub upgrade is already in progress.")

    request = {
        "requestId": str(uuid.uuid4()),
        "requestedAt": _now_utc_iso(),
        "requestedBy": str((payload or {}).get("requestedBy") or "ui"),
        "repoUrl": str((payload or {}).get("repoUrl") or "").strip(),
        "archiveUrl": str((payload or {}).get("archiveUrl") or "").strip(),
        "branch": str((payload or {}).get("branch") or "main").strip() or "main",
        "currentVersion": str((payload or {}).get("currentVersion") or "").strip(),
        "targetVersion": str((payload or {}).get("targetVersion") or "").strip(),
        "proxySettings": dict((payload or {}).get("proxySettings") or {}),
    }
    _write_json(request_path, request)
    append_upgrade_log(
        db_path,
        "Queued GitHub upgrade request {0} for branch {1}.".format(request["requestId"], request["branch"]),
    )
    return write_upgrade_status(
        db_path,
        {
            "status": "queued",
            "requestedAt": request["requestedAt"],
            "startedAt": "",
            "finishedAt": "",
            "message": "GitHub upgrade request queued. Waiting for the root helper to start.",
            "repoUrl": request["repoUrl"],
            "archiveUrl": request["archiveUrl"],
            "branch": request["branch"],
            "currentVersion": request["currentVersion"],
            "targetVersion": request["targetVersion"],
            "requestId": request["requestId"],
            "lastError": "",
        },
    )


def consume_upgrade_request(db_path):
    ensure_upgrade_layout(db_path)
    path = upgrade_request_path(db_path)
    request = _read_json(path, None)
    if request is None:
        return None
    try:
        os.remove(path)
    except Exception:
        pass
    return request
