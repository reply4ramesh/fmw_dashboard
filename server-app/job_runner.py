#!/usr/bin/env python3
import json
import os
import re
import shlex
import subprocess
import sys
import time

from collector import (
    build_environment_error_dashboard,
    build_environment_target,
    collect_environment_dashboard,
    extract_environment_overview,
    run_target,
)
from environment_registry import (
    get_environment,
    list_environments,
    save_environment,
)
from notification_alerts import send_environment_alerts


APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def _now_utc_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slugify(value):
    return re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "")).strip("-").lower() or "environment"


def _default_collection_minutes():
    raw = os.environ.get(
        "IAM_MONITORING_DEFAULT_COLLECTION_MINUTES",
        os.environ.get("IAM_DASHBOARD_DEFAULT_COLLECTION_MINUTES", "60"),
    )
    try:
        return max(5, int(raw))
    except (TypeError, ValueError):
        return 60


def _env_quote_line(value):
    text = str(value or "")
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _state_root(db_path):
    return os.path.dirname(os.path.abspath(db_path))


def _runtime_env_dir(db_path):
    return os.path.join(_state_root(db_path), "runtime_env")


def _snapshot_dir(db_path):
    return os.path.join(_state_root(db_path), "snapshots")


def _job_state_dir(db_path):
    return os.path.join(_state_root(db_path), "job_state")


def _scheduler_state_dir(db_path):
    return os.path.join(_state_root(db_path), "scheduler")


def _bootstrap_key_dir(db_path):
    return os.path.join(_state_root(db_path), "bootstrap_keys")


def _log_dir(db_path):
    default_path = os.path.join(_state_root(db_path), "logs")
    return os.environ.get("IAM_MONITORING_LOG_DIR", default_path)


def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def ensure_runtime_layout(db_path):
    for path in (
        _runtime_env_dir(db_path),
        _snapshot_dir(db_path),
        _job_state_dir(db_path),
        _scheduler_state_dir(db_path),
        _bootstrap_key_dir(db_path),
        _log_dir(db_path),
    ):
        _ensure_dir(path)


def _runtime_env_path(db_path, environment_id):
    return os.path.join(_runtime_env_dir(db_path), "environment-{0}.env".format(environment_id))


def _snapshot_path(db_path, environment_id):
    return os.path.join(_snapshot_dir(db_path), "environment-{0}.json".format(environment_id))


def _job_state_path(db_path, environment_id):
    return os.path.join(_job_state_dir(db_path), "collector-{0}.state".format(environment_id))


def _job_log_path(db_path, environment_id):
    return os.path.join(_log_dir(db_path), "collector-{0}.log".format(environment_id))


def _job_tail(path, max_lines=10):
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
        return "".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


def _local_log_timestamp():
    return time.strftime("%Y-%m-%d %I:%M:%S %p %Z", time.localtime())


def _environment_label(environment):
    environment = environment or {}
    name = str(environment.get("name") or "").strip()
    environment_id = str(environment.get("id") or "").strip()
    if name and environment_id:
        return "{0} ({1})".format(name, environment_id)
    return name or environment_id or "Environment"


def _append_job_log_line(path, environment, message):
    directory = os.path.dirname(path)
    if directory:
        _ensure_dir(directory)
    line = "[{0}] [{1}] {2}\n".format(_local_log_timestamp(), _environment_label(environment), str(message))
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def _read_state_file(path):
    state = {}
    if not os.path.isfile(path):
        return state
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if "=" not in line:
                    continue
                key, value = line.rstrip("\n").split("=", 1)
                state[key] = value
    except Exception:
        return {}
    return state


def _write_state_file(path, payload):
    directory = os.path.dirname(path)
    if directory:
        _ensure_dir(directory)
    temp_path = "{0}.tmp".format(path)
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        for key, value in payload.items():
            handle.write("{0}={1}\n".format(key, "" if value is None else str(value)))
    os.replace(temp_path, path)


def _emit_progress(progress, message):
    if not callable(progress):
        return
    try:
        progress(str(message))
    except Exception:
        return


def _pid_is_running(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def get_default_collection_minutes():
    return _default_collection_minutes()


def read_job_status(db_path, environment_id):
    ensure_runtime_layout(db_path)
    path = _job_state_path(db_path, environment_id)
    log_path = _job_log_path(db_path, environment_id)
    state = _read_state_file(path)
    status = state.get("status", "idle")
    pid = state.get("pid", "")
    if status == "running" and pid and not _pid_is_running(pid):
        state["status"] = "unknown"
        status = "unknown"
        _write_state_file(path, state)
    return {
        "job": "collector",
        "environmentId": str(environment_id),
        "status": status,
        "startedAt": state.get("started_at", ""),
        "finishedAt": state.get("finished_at", ""),
        "lastExit": state.get("last_exit", ""),
        "pid": pid,
        "trigger": state.get("trigger", ""),
        "logPath": log_path,
        "tailCommand": "tail -F {0}".format(log_path),
        "tail": _job_tail(log_path),
    }


def _read_snapshot(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def load_environment_snapshot(db_path, environment_id):
    return _read_snapshot(_snapshot_path(db_path, environment_id))


def save_environment_snapshot(db_path, environment_id, payload):
    ensure_runtime_layout(db_path)
    path = _snapshot_path(db_path, environment_id)
    temp_path = "{0}.tmp".format(path)
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(temp_path, path)
    return path


def build_pending_dashboard(environment, message=None):
    environment = environment or {}
    server = environment.get("server") or {}
    overview_message = message or (
        "No collector snapshot has been saved for this environment yet. "
        "Use Save And Bootstrap or Run Jobs Now, or wait for the scheduled collector window."
    )
    return {
        "environment": {
            "id": environment.get("id"),
            "name": environment.get("name"),
            "description": environment.get("description"),
            "environmentType": environment.get("environmentType"),
            "products": environment.get("products") or {},
            "operations": environment.get("operations") or {},
            "collection": environment.get("collection") or {},
            "bootstrap": environment.get("bootstrap") or {},
            "server": {
                "host": server.get("host"),
                "port": server.get("port"),
                "username": server.get("username"),
                "sshMode": server.get("sshMode"),
                "authType": server.get("authType"),
                "sudoRequired": server.get("sudoRequired"),
            },
        },
        "generatedAt": None,
        "generatedAtLocal": None,
        "generatedAtEpoch": None,
        "status": "configured",
        "server": {
            "reachable": False,
            "status": "configured",
            "actualHostname": None,
            "error": overview_message,
        },
        "appChecks": [],
        "productMetrics": {},
        "summary": {
            "totalApps": 0,
            "healthyApps": 0,
            "warningApps": 0,
            "downApps": 0,
        },
        "notes": [
            "Bootstrap uses the initial SSH access one time and then switches the environment to the installed runtime key for ongoing collection.",
            "Run Jobs Now collects a fresh environment snapshot immediately without waiting for the scheduler.",
            overview_message,
        ],
    }


def _runtime_key_path(db_path, environment):
    safe_name = _slugify(environment.get("name") or environment.get("id") or "environment")
    return os.path.join(_bootstrap_key_dir(db_path), "{0}-runtime-ed25519".format(safe_name))


def ensure_runtime_keypair(db_path, environment):
    ensure_runtime_layout(db_path)
    private_key_path = _runtime_key_path(db_path, environment)
    if os.path.isfile(private_key_path) and os.path.isfile(private_key_path + ".pub"):
        return private_key_path
    subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            private_key_path,
            "-C",
            "{0}@iam-monitoring".format(_slugify(environment.get("name") or environment.get("id"))),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        os.chmod(private_key_path, 0o600)
        os.chmod(private_key_path + ".pub", 0o644)
    except Exception:
        pass
    return private_key_path


def write_runtime_env_file(db_path, environment):
    ensure_runtime_layout(db_path)
    environment = environment or {}
    server = environment.get("server") or {}
    bootstrap = environment.get("bootstrap") or {}
    collection = environment.get("collection") or {}
    oud = environment.get("oud") or {}
    path = _runtime_env_path(db_path, environment.get("id"))
    lines = [
        "IAM_ENVIRONMENT_ID={0}".format(_env_quote_line(environment.get("id") or "")),
        "IAM_ENVIRONMENT_NAME={0}".format(_env_quote_line(environment.get("name") or "")),
        "IAM_ENVIRONMENT_TYPE={0}".format(_env_quote_line(environment.get("environmentType") or "")),
        "IAM_ENVIRONMENT_HOST={0}".format(_env_quote_line(server.get("host") or "")),
        "IAM_ENVIRONMENT_PORT={0}".format(_env_quote_line(server.get("port") or 22)),
        "IAM_ENVIRONMENT_USERNAME={0}".format(_env_quote_line(server.get("username") or "")),
        "IAM_ENVIRONMENT_SSH_MODE={0}".format(_env_quote_line(server.get("sshMode") or "")),
        "IAM_ENVIRONMENT_AUTH_TYPE={0}".format(_env_quote_line(server.get("authType") or "")),
        "IAM_ENVIRONMENT_SUDO_REQUIRED={0}".format(_env_quote_line("true" if server.get("sudoRequired") else "false")),
        "IAM_ENVIRONMENT_PRIVATE_KEY={0}".format(_env_quote_line(server.get("privateKeyPath") or bootstrap.get("runtimeKeyPath") or "")),
        "IAM_ENVIRONMENT_OUD_HOST={0}".format(_env_quote_line(oud.get("host") or "")),
        "IAM_ENVIRONMENT_OUD_DOMAIN_HOME={0}".format(_env_quote_line(oud.get("domainHome") or "")),
        "IAM_ENVIRONMENT_OUD_INSTANCE_HOME={0}".format(_env_quote_line(oud.get("instanceHome") or "")),
        "IAM_ENVIRONMENT_DIRECTORY_MANAGER_DN={0}".format(_env_quote_line(oud.get("bindDn") or "")),
        "IAM_ENVIRONMENT_DIRECTORY_MANAGER_PASSWORD={0}".format(_env_quote_line(oud.get("bindPassword") or "")),
        "IAM_ENVIRONMENT_LDAP_PORT={0}".format(_env_quote_line(oud.get("ldapPort") or 1389)),
        "IAM_ENVIRONMENT_ADMIN_PORT={0}".format(_env_quote_line(oud.get("adminPort") or 4444)),
        "IAM_ENVIRONMENT_COLLECTION_ENABLED={0}".format(_env_quote_line("true" if collection.get("enabled", True) else "false")),
        "IAM_ENVIRONMENT_COLLECTION_SCHEDULE_MINUTES={0}".format(
            _env_quote_line(collection.get("scheduleMinutes") or _default_collection_minutes())
        ),
        "IAM_MONITORING_DB_PATH={0}".format(_env_quote_line(db_path)),
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _bootstrap_target(environment):
    target = build_environment_target(environment)
    target["sudoRequired"] = False
    return target


def _runtime_target(environment, private_key_path):
    server = environment.get("server") or {}
    username = str(server.get("username") or "root").strip() or "root"
    sudo_required = bool(server.get("sudoRequired"))
    return {
        "mode": "ssh",
        "host": server.get("host") or "",
        "port": server.get("port") or 22,
        "username": username,
        "sshMode": "root_key" if username == "root" and not sudo_required else ("user_key_sudo" if sudo_required else "user_key"),
        "authType": "private_key",
        "sudoRequired": sudo_required,
        "password": server.get("password") if sudo_required else "",
        "privateKeyPath": private_key_path,
        "passphrase": "",
    }


def _mark_bootstrap_ready(environment, db_path, initial_ssh_mode, runtime_key_path, strategy, message):
    bootstrap = environment.get("bootstrap") or {}
    bootstrap["status"] = "ready"
    bootstrap["strategy"] = strategy
    bootstrap["initialSshMode"] = initial_ssh_mode
    bootstrap["runtimeKeyPath"] = runtime_key_path or ""
    bootstrap["runtimeEnvPath"] = _runtime_env_path(db_path, environment.get("id"))
    bootstrap["lastBootstrappedAt"] = _now_utc_iso()
    bootstrap["message"] = message
    environment["bootstrap"] = bootstrap
    return environment


def bootstrap_environment_runtime(db_path, environment_id, initial_target_override=None):
    environment = get_environment(db_path, environment_id, include_secret=True)
    if not environment:
        raise KeyError("Environment not found.")

    server = environment.get("server") or {}
    if not str(server.get("host") or "").strip():
        raise ValueError("Server SSH host is required before bootstrap can run.")
    if not str(server.get("username") or "").strip():
        raise ValueError("Server SSH username is required before bootstrap can run.")

    initial_ssh_mode = str(server.get("sshMode") or "root_password").strip() or "root_password"
    runtime_key_path = ensure_runtime_keypair(db_path, environment)
    public_key_path = runtime_key_path + ".pub"
    with open(public_key_path, "r", encoding="utf-8") as handle:
        public_key = handle.read().strip()

    install_command = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        "grep -qxF {0} ~/.ssh/authorized_keys 2>/dev/null || echo {0} >> ~/.ssh/authorized_keys"
    ).format(shlex.quote(public_key))

    bootstrap_target = _bootstrap_target(environment)
    override_target = initial_target_override or {}
    if override_target:
        bootstrap_target.update(
            {
                "mode": override_target.get("mode") or bootstrap_target.get("mode") or "ssh",
                "host": override_target.get("host") or bootstrap_target.get("host") or "",
                "port": override_target.get("port") or bootstrap_target.get("port") or 22,
                "username": override_target.get("username") or bootstrap_target.get("username") or "",
                "sshMode": override_target.get("sshMode") or bootstrap_target.get("sshMode") or "root_password",
                "authType": override_target.get("authType") or bootstrap_target.get("authType") or "password",
                "password": override_target.get("password") or bootstrap_target.get("password") or "",
                "privateKeyPath": override_target.get("privateKeyPath") or bootstrap_target.get("privateKeyPath") or "",
                "passphrase": override_target.get("passphrase") or bootstrap_target.get("passphrase") or "",
                "sudoRequired": False,
            }
        )

    bootstrap_result = run_target(bootstrap_target, install_command)
    if bootstrap_result.get("exit_code") != 0:
        raise ValueError(
            "Bootstrap could not install the runtime SSH key with the current initial SSH access. {0}".format(
                bootstrap_result.get("output") or "Check the SSH host, username, password, or key."
            )
        )

    runtime_target = _runtime_target(environment, runtime_key_path)
    verify_result = run_target(runtime_target, "true")
    if verify_result.get("exit_code") != 0:
        password_target = _bootstrap_target(environment)
        password_verify = run_target(password_target, "true")
        if password_verify.get("exit_code") == 0:
            environment = _mark_bootstrap_ready(
                environment,
                db_path,
                initial_ssh_mode,
                "",
                "initial_ssh_saved_password_fallback",
                (
                    "Bootstrap installed the runtime public key, but this host did not accept key-based SSH. "
                    "The dashboard will continue using the saved password SSH profile for collection. "
                    "Enable key-based login later if you want passwordless runtime collection."
                ),
            )
            environment["server"] = server
            saved = save_environment(db_path, environment, environment_id=environment_id)
            runtime_env_path = write_runtime_env_file(db_path, saved)
            saved["bootstrap"]["runtimeEnvPath"] = runtime_env_path
            save_environment(db_path, saved, environment_id=environment_id)
            return get_environment(db_path, environment_id, include_secret=True)
        if runtime_target.get("sudoRequired"):
            if runtime_target.get("password"):
                raise ValueError(
                    "Bootstrap installed the runtime key, but sudo verification failed with the saved SSH password. "
                    "Re-enter the SSH password and confirm the user can run sudo."
                )
            raise ValueError(
                "Bootstrap installed the runtime key, but sudo/root access verification failed for the saved SSH user. "
                "Re-enter the SSH password or allow passwordless sudo for key-based collection."
            )
        raise ValueError(
            "Bootstrap installed the runtime key, but key-based SSH verification failed. "
            "Check that the SSH user home and authorized_keys permissions allow key login."
        )

    environment = _mark_bootstrap_ready(
        environment,
        db_path,
        initial_ssh_mode,
        runtime_key_path,
        "initial_ssh_then_runtime_key",
        (
            "Bootstrap used the saved initial SSH access one time. "
            "This environment now uses the installed runtime key for ongoing collection."
        ),
    )

    environment["server"] = server
    saved = save_environment(db_path, environment, environment_id=environment_id)
    runtime_env_path = write_runtime_env_file(db_path, saved)
    saved["bootstrap"]["runtimeEnvPath"] = runtime_env_path
    save_environment(db_path, saved, environment_id=environment_id)
    return get_environment(db_path, environment_id, include_secret=True)


def _decorate_dashboard(environment, dashboard_payload, trigger, runtime_env_path, duration_ms=None, snapshot_path=None, db_path=None):
    dashboard_payload = dashboard_payload or {}
    dashboard_payload["generatedAtEpoch"] = int(time.time())
    dashboard_payload["environment"] = dashboard_payload.get("environment") or {}
    dashboard_payload["environment"]["collection"] = environment.get("collection") or {}
    dashboard_payload["environment"]["bootstrap"] = environment.get("bootstrap") or {}
    updated_files = [path for path in (runtime_env_path, snapshot_path) if path]
    products = environment.get("products") or {}
    dashboard_payload["runtime"] = {
        "trigger": trigger,
        "runtimeEnvPath": runtime_env_path,
        "snapshotPath": snapshot_path,
        "databasePath": db_path,
        "updatedFiles": updated_files,
        "productsCollected": [key for key, enabled in products.items() if enabled],
        "durationMs": duration_ms,
    }
    notes = list(dashboard_payload.get("notes") or [])
    bootstrap_note = str(((environment.get("bootstrap") or {}).get("message")) or "").strip()
    if bootstrap_note and bootstrap_note not in notes:
        notes.insert(0, bootstrap_note)
    schedule_minutes = ((environment.get("collection") or {}).get("scheduleMinutes")) or _default_collection_minutes()
    schedule_note = "Scheduled collection runs every {0} minutes when this environment is enabled.".format(schedule_minutes)
    if schedule_note not in notes:
        notes.append(schedule_note)
    dashboard_payload["notes"] = notes
    return dashboard_payload


def collect_environment_now(db_path, environment_id, trigger="manual", progress=None):
    environment = get_environment(db_path, environment_id, include_secret=True)
    if not environment:
        raise KeyError("Environment not found.")
    bootstrap = environment.get("bootstrap") or {}
    if trigger in ("manual", "scheduler", "bootstrap") and str(bootstrap.get("status") or "").lower() != "ready":
        raise ValueError("Run bootstrap first. This environment has not switched to its runtime SSH key yet.")
    products = [key.upper() for key, enabled in (environment.get("products") or {}).items() if enabled]
    _emit_progress(progress, "Loaded environment profile from registry: {0}".format(db_path))
    _emit_progress(
        progress,
        "Environment type {0}; products enabled: {1}.".format(
            str(environment.get("environmentType") or "not-set").upper() or "NOT-SET",
            ", ".join(products) or "None",
        ),
    )
    runtime_env_path = write_runtime_env_file(db_path, environment)
    _emit_progress(progress, "Updated runtime environment file: {0}".format(runtime_env_path))
    server = environment.get("server") or {}
    _emit_progress(
        progress,
        "Collecting host metrics from {0}:{1} as {2} using {3}.".format(
            server.get("host") or "-",
            server.get("port") or 22,
            server.get("username") or "root",
            server.get("sshMode") or "root_password",
        ),
    )
    if (environment.get("products") or {}).get("oud"):
        oud = environment.get("oud") or {}
        _emit_progress(
            progress,
            "Collecting OUD runtime from host {0}; instance home {1}; OUD root user {2}.".format(
                oud.get("host") or server.get("host") or "-",
                oud.get("instanceHome") or "-",
                oud.get("bindDn") or "cn=Directory Manager",
            ),
        )
        _emit_progress(progress, "Running ./status, ./dsreplication status -X --Advanced -n, ./start-ds -F, and ./start-ds -s from the OUD bin directory.")
    if (environment.get("products") or {}).get("weblogic"):
        weblogic = environment.get("weblogic") or {}
        admin_host = (weblogic.get("adminHost") or {}).get("host") or server.get("host") or "-"
        _emit_progress(
            progress,
            "Collecting WebLogic runtime from admin host {0}; admin URL {1}.".format(
                admin_host,
                weblogic.get("adminUrl") or "-",
            ),
        )
        _emit_progress(progress, "Running combined WLST runtime collection from ORACLE_HOME/oracle_common/common/bin/wlst.sh. OPatch lspatches and keytool keystore checks start in parallel for OIG/OIM.")
    started = time.time()
    try:
        dashboard = collect_environment_dashboard(environment, progress=progress)
        duration_ms = int((time.time() - started) * 1000)
        snapshot_path = _snapshot_path(db_path, environment_id)
        dashboard = _decorate_dashboard(environment, dashboard, trigger, runtime_env_path, duration_ms, snapshot_path, db_path)
        save_environment_snapshot(db_path, environment_id, dashboard)
        _emit_progress(progress, "Updated dashboard snapshot file: {0}".format(snapshot_path))
        try:
            alert_result = send_environment_alerts(db_path, environment, dashboard, progress=progress)
            if alert_result.get("sent"):
                _emit_progress(
                    progress,
                    "Notification alert sent to {0} recipient(s).".format(alert_result.get("recipientCount") or 0),
                )
        except Exception as exc:
            _emit_progress(progress, "Notification alert check failed: {0}".format(exc))
        _emit_progress(
            progress,
            "Run updated files: {0}.".format(", ".join([runtime_env_path, snapshot_path])),
        )
        _emit_progress(progress, "Collection duration: {0} ms.".format(duration_ms))
        return dashboard
    except Exception as exc:
        dashboard = build_environment_error_dashboard(environment, str(exc))
        duration_ms = int((time.time() - started) * 1000)
        snapshot_path = _snapshot_path(db_path, environment_id)
        dashboard = _decorate_dashboard(environment, dashboard, trigger, runtime_env_path, duration_ms, snapshot_path, db_path)
        save_environment_snapshot(db_path, environment_id, dashboard)
        _emit_progress(progress, "Updated dashboard snapshot file after error: {0}".format(snapshot_path))
        raise


def due_for_collection(db_path, environment):
    collection = environment.get("collection") or {}
    if not collection.get("enabled", True):
        return False
    try:
        interval_minutes = max(5, int(collection.get("scheduleMinutes") or _default_collection_minutes()))
    except (TypeError, ValueError):
        interval_minutes = _default_collection_minutes()
    snapshot = load_environment_snapshot(db_path, environment.get("id"))
    if not snapshot:
        return True
    last_epoch = snapshot.get("generatedAtEpoch")
    if not last_epoch:
        try:
            last_epoch = int(os.path.getmtime(_snapshot_path(db_path, environment.get("id"))))
        except OSError:
            return True
    return (time.time() - int(last_epoch)) >= (interval_minutes * 60)


def launch_collection_job(db_path, environment_id, trigger="manual"):
    ensure_runtime_layout(db_path)
    current = read_job_status(db_path, environment_id)
    if current.get("status") == "running":
        return current, False

    environment = get_environment(db_path, environment_id, include_secret=True)
    if not environment:
        raise KeyError("Environment not found.")
    if str(((environment.get("bootstrap") or {}).get("status")) or "").lower() != "ready":
        raise ValueError("Run bootstrap first. This environment has not switched to its runtime SSH key yet.")

    write_runtime_env_file(db_path, environment)
    started_at = _now_utc_iso()
    state_path = _job_state_path(db_path, environment_id)
    log_path = _job_log_path(db_path, environment_id)
    trigger_label = {
        "manual": "Run Jobs Now requested from the dashboard. Starting background collector.",
        "bootstrap": "Bootstrap started the collector. Starting background collector.",
        "scheduler": "Scheduled collector window reached. Starting background collector.",
    }.get(str(trigger or "").strip().lower(), "Starting background collector.")
    _write_state_file(
        state_path,
        {
            "status": "running",
            "started_at": started_at,
            "finished_at": "",
            "last_exit": "",
            "pid": "",
            "trigger": trigger,
        },
    )
    _append_job_log_line(log_path, environment, trigger_label)

    script_path = os.path.join(APP_ROOT, "collect_environment.py")
    wrapper = (
        "{python} -u {script} --db-path {db_path} --env-id {environment_id} --trigger {trigger} >> {log_path} 2>&1"
    ).format(
        python=shlex.quote(sys.executable),
        script=shlex.quote(script_path),
        db_path=shlex.quote(db_path),
        environment_id=shlex.quote(str(environment_id)),
        trigger=shlex.quote(str(trigger)),
        log_path=shlex.quote(log_path),
    )
    process = subprocess.Popen(
        ["/usr/bin/env", "bash", "-lc", wrapper],
        cwd=APP_ROOT,
        start_new_session=True,
    )
    _write_state_file(
        state_path,
        {
            "status": "running",
            "started_at": started_at,
            "finished_at": "",
            "last_exit": "",
            "pid": str(process.pid),
            "trigger": trigger,
        },
    )
    return read_job_status(db_path, environment_id), True


def run_due_collection_jobs(db_path):
    ensure_runtime_layout(db_path)
    launched = []
    for environment in list_environments(db_path, include_secret=True):
        environment_id = environment.get("id")
        if not environment_id:
            continue
        if str(((environment.get("bootstrap") or {}).get("status")) or "").lower() != "ready":
            continue
        if read_job_status(db_path, environment_id).get("status") == "running":
            continue
        if due_for_collection(db_path, environment):
            try:
                status, started = launch_collection_job(db_path, environment_id, trigger="scheduler")
                launched.append(
                    {
                        "environmentId": environment_id,
                        "environmentName": environment.get("name"),
                        "started": bool(started),
                        "job": status,
                    }
                )
            except Exception as exc:
                launched.append(
                    {
                        "environmentId": environment_id,
                        "environmentName": environment.get("name"),
                        "started": False,
                        "error": str(exc),
                    }
                )
    return launched


def clear_environment_runtime_state(db_path, environment_id):
    for path in (
        _runtime_env_path(db_path, environment_id),
        _snapshot_path(db_path, environment_id),
        _job_state_path(db_path, environment_id),
        _job_log_path(db_path, environment_id),
    ):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def environment_overview_from_snapshot(db_path, environment):
    environment_id = (environment or {}).get("id")
    if not environment_id:
        return None
    snapshot = load_environment_snapshot(db_path, environment_id)
    if not snapshot:
        return None
    overview = extract_environment_overview(snapshot)
    overview["collection"] = (snapshot.get("environment") or {}).get("collection") or (environment.get("collection") or {})
    overview["bootstrap"] = (snapshot.get("environment") or {}).get("bootstrap") or (environment.get("bootstrap") or {})
    return overview
