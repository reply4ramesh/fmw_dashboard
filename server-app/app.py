#!/usr/bin/env python3
import json
import base64
import io
import mimetypes
import os
import re
import shlex
import smtplib
import ssl
import threading
import time
import uuid
import zipfile
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from collector import (
    build_discovered_weblogic_cluster_targets,
    build_environment_error_dashboard,
    build_environment_target,
    build_oaa_database_summary,
    build_oaa_target,
    build_weblogic_target,
    collect_oaa_install_properties_from_host,
    collect_oaa_schema_table_metrics,
    collect_environment_dashboard,
    collect_monitoring_server,
    extract_environment_overview,
    hydrate_dashboard_payload,
    merge_oaa_database_settings,
    oaa_database_missing_fields,
    oaa_database_password_from_install,
    run_target,
)
from config_store import (
    load_config,
    normalize_ssh_profile_payload,
)
from environment_registry import (
    delete_environment,
    get_environment,
    list_environments,
    migrate_config_environments,
    save_environment,
)
from job_runner import (
    bootstrap_environment_runtime,
    build_pending_dashboard,
    clear_environment_runtime_state,
    collect_environment_now,
    environment_overview_from_snapshot,
    get_default_collection_minutes,
    launch_collection_job,
    load_environment_snapshot,
    read_job_status,
)
from notification_store import (
    get_notification_settings,
    notification_payload,
    save_notification_recipient,
    save_notification_settings,
    delete_notification_recipient,
)
from support_store import (
    get_update_proxy_settings,
    save_update_proxy_settings,
)
from upgrade_runtime import (
    append_upgrade_log,
    queue_github_upgrade,
    read_upgrade_status,
    write_upgrade_status,
)


APP_ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_ROOT = os.path.join(APP_ROOT, "static")
CONFIG_PATH = os.path.join(APP_ROOT, "config.json")
VERSION_PATH = os.path.join(APP_ROOT, "VERSION")
STATE_ROOT = os.path.join(APP_ROOT, "state")
DB_PATH = os.environ.get(
    "IAM_MONITORING_DB_PATH",
    os.environ.get("IAM_DASHBOARD_DB_PATH", os.path.join(STATE_ROOT, "iam-monitoring.sqlite")),
)
HOST = os.environ.get("IAM_MONITORING_HOST", os.environ.get("IAM_DASHBOARD_HOST", "0.0.0.0"))
PORT = int(os.environ.get("IAM_MONITORING_PORT", os.environ.get("IAM_DASHBOARD_PORT", "8081")))
CACHE_SECONDS = int(os.environ.get("IAM_MONITORING_CACHE_SECONDS", os.environ.get("IAM_DASHBOARD_CACHE_SECONDS", "60")))
LOG_DIR = os.environ.get("IAM_MONITORING_LOG_DIR", os.path.join(os.path.dirname(DB_PATH), "logs"))
CONFIG_FILE_PATH = os.environ.get("IAM_MONITORING_CONFIG", "/etc/iam-monitoring.env")
SERVICE_NAME = "iam-monitoring"
SERVICE_FILE_PATH = "/etc/systemd/system/{0}.service".format(SERVICE_NAME)
UPGRADE_SERVICE_NAME = "iam-monitoring-upgrader"
UPGRADE_SERVICE_FILE_PATH = "/etc/systemd/system/{0}.service".format(UPGRADE_SERVICE_NAME)
CRON_FILE_PATH = "/etc/cron.d/iam-monitoring"
GITHUB_OWNER = os.environ.get("IAM_MONITORING_GITHUB_OWNER", "reply4ramesh")
GITHUB_REPO = os.environ.get("IAM_MONITORING_GITHUB_REPO", "fmw_dashboard")
GITHUB_BRANCH = os.environ.get("IAM_MONITORING_GITHUB_BRANCH", "main")
UPLOADED_KEY_ROOT = os.path.join(os.path.dirname(DB_PATH), "uploaded_keys")
PRODUCT_NAME = "FMW Monitoring Dashboard"
LEGACY_PRODUCT_NAMES = {
    "Oracle Identity & Access Management Dashboard",
}


def normalize_product_name(value):
    text = str(value or "").strip()
    if not text or text in LEGACY_PRODUCT_NAMES:
        return PRODUCT_NAME
    return text


def read_version():
    try:
        with open(VERSION_PATH, "r", encoding="utf-8") as handle:
            return handle.read().strip() or "unknown"
    except Exception:
        return "unknown"


def read_int_env(primary_name, legacy_name, default_value):
    raw_value = os.environ.get(primary_name, os.environ.get(legacy_name, str(default_value)))
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default_value


def build_help_details():
    version = read_version()
    state_dir = os.path.dirname(DB_PATH)
    scheduler_minutes = read_int_env("IAM_MONITORING_SCHEDULER_MINUTES", "IAM_DASHBOARD_SCHEDULER_MINUTES", 5)
    default_collection_minutes = get_default_collection_minutes()
    local_health_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    runtime_env_dir = os.path.join(state_dir, "runtime_env")
    snapshot_dir = os.path.join(state_dir, "snapshots")
    job_state_dir = os.path.join(state_dir, "job_state")
    update_proxy = merge_update_check_proxy_settings()
    return {
        "productName": PRODUCT_NAME,
        "version": version,
        "packageName": "iam-monitoring",
        "platform": "Linux systemd service",
        "serviceName": SERVICE_NAME,
        "serviceFile": SERVICE_FILE_PATH,
        "upgradeServiceName": UPGRADE_SERVICE_NAME,
        "upgradeServiceFile": UPGRADE_SERVICE_FILE_PATH,
        "serviceUser": os.environ.get("IAM_MONITORING_SERVICE_USER", SERVICE_NAME),
        "installDirectory": APP_ROOT,
        "runtimeEnvFile": CONFIG_FILE_PATH,
        "databasePath": DB_PATH,
        "stateDirectory": state_dir,
        "runtimeEnvDirectory": runtime_env_dir,
        "snapshotDirectory": snapshot_dir,
        "jobStateDirectory": job_state_dir,
        "logDirectory": LOG_DIR,
        "schedulerLogPath": os.path.join(LOG_DIR, "scheduler.log"),
        "cronFile": CRON_FILE_PATH,
        "host": HOST,
        "port": PORT,
        "healthPath": "/healthz",
        "healthUrl": "http://{0}:{1}/healthz".format(local_health_host, PORT),
        "schedulerWakeMinutes": scheduler_minutes,
        "defaultCollectionMinutes": default_collection_minutes,
        "updateProxy": update_proxy,
        "githubUpgrade": {
            "enabled": True,
            "repoUrl": github_repo_url(),
            "archiveUrl": github_archive_url(),
            "branch": GITHUB_BRANCH,
            "serviceName": UPGRADE_SERVICE_NAME,
            "status": read_upgrade_status(DB_PATH),
        },
        "checks": [
            {
                "label": "Service status",
                "command": "sudo systemctl status {0} --no-pager".format(SERVICE_NAME),
            },
            {
                "label": "Upgrade helper status",
                "command": "sudo systemctl status {0} --no-pager".format(UPGRADE_SERVICE_NAME),
            },
            {
                "label": "Service logs",
                "command": "sudo journalctl -u {0} -n 100 --no-pager".format(SERVICE_NAME),
            },
            {
                "label": "Health check",
                "command": "curl http://127.0.0.1:{0}/healthz".format(PORT),
            },
            {
                "label": "Health response headers",
                "command": "curl -I http://127.0.0.1:{0}/healthz".format(PORT),
            },
            {
                "label": "Scheduler log",
                "command": "sudo tail -F {0}".format(os.path.join(LOG_DIR, "scheduler.log")),
            },
        ],
        "uninstallCommands": [
            {
                "label": "Stop services",
                "command": "sudo systemctl stop {0} {1} 2>/dev/null || true".format(SERVICE_NAME, UPGRADE_SERVICE_NAME),
            },
            {
                "label": "Disable services",
                "command": "sudo systemctl disable {0} {1} 2>/dev/null || true".format(SERVICE_NAME, UPGRADE_SERVICE_NAME),
            },
            {
                "label": "Remove service units and cron",
                "command": "sudo rm -f {0} {1} {2}".format(SERVICE_FILE_PATH, UPGRADE_SERVICE_FILE_PATH, CRON_FILE_PATH),
            },
            {
                "label": "Reload systemd",
                "command": "sudo systemctl daemon-reload",
            },
            {
                "label": "Remove application files",
                "command": "sudo rm -rf {0}".format(APP_ROOT),
            },
            {
                "label": "Optional: remove runtime config and customer state",
                "command": "sudo rm -f {0} && sudo rm -rf {1} {2}".format(CONFIG_FILE_PATH, state_dir, LOG_DIR),
            },
            {
                "label": "Optional: remove service user",
                "command": "sudo userdel {0} 2>/dev/null || true".format(os.environ.get("IAM_MONITORING_SERVICE_USER", SERVICE_NAME)),
            },
        ],
        "notes": [
            "This dashboard is installed as a Linux systemd service and uses a host cron scheduler for due environment collectors.",
            "Administration is where environments, notifications, and support details live. Environment pages stay focused on that selected IAM environment.",
            "Use Save And Bootstrap when adding an environment so the dashboard can switch from the initial SSH login to its installed runtime key for ongoing collection.",
            "Fresh installs start with an empty SQLite environment registry and pick up runtime settings from the local environment file.",
            "If GitHub update checks need an outbound proxy, save it under Administration / Help / GitHub Update Proxy. The Linux env file remains the service-level fallback.",
            "GitHub upgrades can be queued from Administration / Help. The helper downloads the bundle and runs its bundled upgrade.sh before the main service restarts on the new build.",
        ],
    }


def github_repo_url():
    return "https://github.com/{0}/{1}".format(GITHUB_OWNER, GITHUB_REPO)


def github_archive_url():
    return "https://github.com/{0}/{1}/archive/refs/heads/{2}.tar.gz".format(
        GITHUB_OWNER,
        GITHUB_REPO,
        GITHUB_BRANCH,
    )


def github_version_url():
    return "https://raw.githubusercontent.com/{0}/{1}/{2}/server-app/VERSION".format(
        GITHUB_OWNER,
        GITHUB_REPO,
        GITHUB_BRANCH,
    )


def build_github_upgrade_response(status_payload):
    return {
        "enabled": True,
        "repoUrl": github_repo_url(),
        "archiveUrl": github_archive_url(),
        "branch": GITHUB_BRANCH,
        "serviceName": UPGRADE_SERVICE_NAME,
        "status": status_payload,
    }


def _letter_version_value(value):
    total = 0
    for character in str(value or "").strip().lower():
        if not ("a" <= character <= "z"):
            return None
        total = (total * 26) + (ord(character) - 96)
    return total


def _parse_version_value(value):
    text = str(value or "").strip().lower()
    if not text:
        return None
    match = re.match(r"^(\d+)([a-z]+)$", text)
    if match:
        return ("letter", int(match.group(1)), _letter_version_value(match.group(2)))
    if re.match(r"^\d+(?:\.\d+)+$", text):
        return ("dot", tuple(int(part) for part in text.split(".")))
    if re.match(r"^\d+$", text):
        return ("number", int(text))
    return None


def compare_version_values(local_version, remote_version):
    if str(local_version or "").strip() == str(remote_version or "").strip():
        return 0
    local_parsed = _parse_version_value(local_version)
    remote_parsed = _parse_version_value(remote_version)
    if not local_parsed or not remote_parsed or local_parsed[0] != remote_parsed[0]:
        return None
    if local_parsed[1:] < remote_parsed[1:]:
        return -1
    if local_parsed[1:] > remote_parsed[1:]:
        return 1
    return 0


def env_update_check_proxy_settings():
    return {
        "httpProxy": str(
            os.environ.get(
                "IAM_MONITORING_HTTP_PROXY",
                os.environ.get("http_proxy", os.environ.get("HTTP_PROXY", "")),
            )
            or ""
        ).strip(),
        "httpsProxy": str(
            os.environ.get(
                "IAM_MONITORING_HTTPS_PROXY",
                os.environ.get("https_proxy", os.environ.get("HTTPS_PROXY", "")),
            )
            or ""
        ).strip(),
        "noProxy": str(
            os.environ.get(
                "IAM_MONITORING_NO_PROXY",
                os.environ.get("no_proxy", os.environ.get("NO_PROXY", "")),
            )
            or ""
        ).strip(),
    }


def proxy_settings_have_values(proxy_settings):
    proxy_settings = proxy_settings or {}
    return bool(
        str(proxy_settings.get("httpProxy") or "").strip()
        or str(proxy_settings.get("httpsProxy") or "").strip()
        or str(proxy_settings.get("noProxy") or "").strip()
    )


def proxy_settings_have_routing(proxy_settings):
    proxy_settings = proxy_settings or {}
    return bool(
        str(proxy_settings.get("httpProxy") or "").strip()
        or str(proxy_settings.get("httpsProxy") or "").strip()
    )


def merge_update_check_proxy_settings(saved_settings=None, env_settings=None):
    saved_settings = saved_settings or get_update_proxy_settings(DB_PATH)
    env_settings = env_settings or env_update_check_proxy_settings()
    effective_settings = {
        "httpProxy": str(saved_settings.get("httpProxy") or env_settings.get("httpProxy") or "").strip(),
        "httpsProxy": str(saved_settings.get("httpsProxy") or env_settings.get("httpsProxy") or "").strip(),
        "noProxy": str(saved_settings.get("noProxy") or env_settings.get("noProxy") or "").strip(),
    }
    saved_configured = proxy_settings_have_values(saved_settings)
    env_configured = proxy_settings_have_values(env_settings)
    if saved_configured and env_configured:
        source = "dashboard_and_env"
        source_label = "Dashboard saved proxy with service env fallback"
    elif saved_configured:
        source = "dashboard"
        source_label = "Dashboard saved proxy"
    elif env_configured:
        source = "service_env"
        source_label = "Service env file"
    else:
        source = "none"
        source_label = "No proxy configured"
    return {
        "savedSettings": {
            "httpProxy": str(saved_settings.get("httpProxy") or "").strip(),
            "httpsProxy": str(saved_settings.get("httpsProxy") or "").strip(),
            "noProxy": str(saved_settings.get("noProxy") or "").strip(),
            "configured": saved_configured,
        },
        "envSettings": {
            "httpProxy": str(env_settings.get("httpProxy") or "").strip(),
            "httpsProxy": str(env_settings.get("httpsProxy") or "").strip(),
            "noProxy": str(env_settings.get("noProxy") or "").strip(),
            "configured": env_configured,
        },
        "effectiveSettings": effective_settings,
        "savedConfigured": saved_configured,
        "envConfigured": env_configured,
        "configured": proxy_settings_have_routing(effective_settings),
        "source": source,
        "sourceLabel": source_label,
    }


def update_check_proxy_hint(proxy_context=None):
    proxy_context = proxy_context or merge_update_check_proxy_settings()
    if proxy_context.get("configured"):
        return (
            "Proxy is already configured for GitHub update checks. Recheck the proxy host, "
            "port, and any required SSL or egress policy."
        )
    return (
        "If this host requires an outbound proxy, save it under Administration / Help / "
        "GitHub Update Proxy, or set IAM_MONITORING_HTTP_PROXY and "
        "IAM_MONITORING_HTTPS_PROXY in /etc/iam-monitoring.env and restart iam-monitoring."
    )


def build_update_check_payload():
    current_version = read_version()
    proxy_context = merge_update_check_proxy_settings()
    proxy_settings = proxy_context.get("effectiveSettings") or {}
    payload = {
        "currentVersion": current_version,
        "remoteVersion": "",
        "repoUrl": github_repo_url(),
        "versionUrl": github_version_url(),
        "branch": GITHUB_BRANCH,
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "idle",
        "message": "",
        "proxyConfigured": bool(proxy_context.get("configured")),
        "proxySource": proxy_context.get("source") or "none",
        "proxySourceLabel": proxy_context.get("sourceLabel") or "No proxy configured",
    }
    try:
        request = Request(payload["versionUrl"], headers={"User-Agent": "iam-monitoring-update-check"})
        proxy_handler_settings = {}
        if proxy_settings.get("httpProxy"):
            proxy_handler_settings["http"] = proxy_settings.get("httpProxy")
        if proxy_settings.get("httpsProxy"):
            proxy_handler_settings["https"] = proxy_settings.get("httpsProxy")
        if proxy_handler_settings:
            opener = build_opener(ProxyHandler(proxy_handler_settings))
            response_handle = opener.open(request, timeout=6)
        else:
            response_handle = urlopen(request, timeout=6)
        with response_handle as response:
            remote_version = response.read().decode("utf-8").strip()
        if not remote_version:
            raise ValueError("GitHub did not return a VERSION value.")
        payload["remoteVersion"] = remote_version
        comparison = compare_version_values(current_version, remote_version)
        if comparison == 0:
            payload["status"] = "current"
            payload["message"] = "This dashboard is up to date with GitHub {0}.".format(GITHUB_BRANCH)
        elif comparison == -1:
            payload["status"] = "update_available"
            payload["message"] = "GitHub {0} has a newer version available: {1}.".format(GITHUB_BRANCH, remote_version)
        elif comparison == 1:
            payload["status"] = "ahead"
            payload["message"] = "This dashboard is ahead of GitHub {0}.".format(GITHUB_BRANCH)
        else:
            payload["status"] = "different"
            payload["message"] = "GitHub {0} reports version {1}; compare it with the running version {2}.".format(
                GITHUB_BRANCH,
                remote_version,
                current_version,
            )
    except Exception as exc:
        payload["status"] = "error"
        payload["message"] = "GitHub update check failed: {0} {1}".format(str(exc), update_check_proxy_hint(proxy_context))
    return payload


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def build_configured_environment_overview(environment):
    server = environment.get("server") or {}
    return {
        "id": environment.get("id"),
        "name": environment.get("name"),
        "description": environment.get("description"),
        "environmentType": environment.get("environmentType"),
        "host": server.get("host"),
        "status": "configured",
        "generatedAtLocal": None,
        "actualHostname": None,
        "products": environment.get("products") or {},
        "sshMode": server.get("sshMode"),
        "authType": server.get("authType"),
        "collection": environment.get("collection") or {},
        "bootstrap": environment.get("bootstrap") or {},
        "summary": {
            "totalApps": 0,
            "healthyApps": 0,
            "warningApps": 0,
            "downApps": 0,
        },
    }


def load_runtime_config():
    config = load_config(CONFIG_PATH)
    migrate_config_environments(DB_PATH, config)
    return config


class DashboardCache(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.monitoring_entry = {"data": None, "generated": 0, "error": None}
        self.environment_entries = {}

    def _expired(self, generated_at):
        return not generated_at or (time.time() - generated_at) > CACHE_SECONDS

    def invalidate(self, environment_id=None):
        with self.lock:
            self.monitoring_entry = {"data": None, "generated": 0, "error": None}
            if environment_id is None:
                self.environment_entries = {}
            else:
                self.environment_entries.pop(environment_id, None)

    def get_monitoring(self, config, force=False):
        with self.lock:
            if force or self.monitoring_entry["data"] is None or self._expired(self.monitoring_entry["generated"]):
                try:
                    self.monitoring_entry["data"] = collect_monitoring_server(config.get("monitoring_server") or {})
                    self.monitoring_entry["generated"] = time.time()
                    self.monitoring_entry["error"] = None
                except Exception as exc:
                    self.monitoring_entry["error"] = str(exc)
                    if self.monitoring_entry["data"] is None:
                        raise

            payload = dict(self.monitoring_entry["data"] or {})
            if self.monitoring_entry["error"]:
                payload["collectorError"] = self.monitoring_entry["error"]
            return payload

    def get_environment(self, config, environment_id, force=False):
        environment = get_environment(DB_PATH, environment_id, include_secret=True)
        if not environment:
            raise KeyError("Unknown environment: {0}".format(environment_id))

        try:
            payload = collect_environment_now(DB_PATH, environment_id, trigger="api_force") if force else load_environment_snapshot(DB_PATH, environment_id)
        except Exception as exc:
            payload = load_environment_snapshot(DB_PATH, environment_id) or build_pending_dashboard(environment, str(exc))
            payload["collectorError"] = str(exc)

        if not payload:
            payload = build_pending_dashboard(environment)
        payload = hydrate_dashboard_payload(payload)
        payload["job"] = read_job_status(DB_PATH, environment_id)
        return payload

    def get_app_shell(self, config, force=False):
        with self.lock:
            monitoring_cache = dict(self.monitoring_entry["data"] or {})
            monitoring_error = self.monitoring_entry["error"]

        monitoring = {
            "name": (config.get("monitoring_server") or {}).get("name"),
            "host": (config.get("monitoring_server") or {}).get("host"),
            "description": (config.get("monitoring_server") or {}).get("description"),
            "servicePort": (config.get("monitoring_server") or {}).get("servicePort"),
            "status": "configured",
        }
        if monitoring_cache:
            monitoring.update(monitoring_cache)
        if monitoring_error:
            monitoring["collectorError"] = monitoring_error

        environments = []
        for environment in list_environments(DB_PATH, include_secret=False):
            snapshot_overview = environment_overview_from_snapshot(DB_PATH, environment)
            environments.append(snapshot_overview or build_configured_environment_overview(environment))

        return {
            "title": normalize_product_name(config.get("dashboard_title")),
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "generatedAtLocal": time.strftime("%A, %B %d %Y %H:%M:%S %Z"),
            "monitoringServer": monitoring,
            "environments": environments,
            "operations": config.get("operations") or {},
            "help": build_help_details(),
        }


CACHE = DashboardCache()


def parse_json_body(request_handler):
    length = int(request_handler.headers.get("Content-Length") or "0")
    raw_body = request_handler.rfile.read(length) if length > 0 else b"{}"
    if not raw_body:
        return {}
    return json.loads(raw_body.decode("utf-8"))


def logscope_slug(value):
    text = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "")).strip("-").lower()
    return text or "logs"


def logscope_unix_join(*parts):
    cleaned = []
    absolute = False
    for index, part in enumerate(parts):
        text = str(part or "").strip()
        if not text:
            continue
        if index == 0 and text.startswith("/"):
            absolute = True
        cleaned.append(text.strip("/"))
    joined = "/".join(item for item in cleaned if item)
    return "/{0}".format(joined) if absolute and joined else joined


def logscope_products(environment):
    products = dict((environment or {}).get("products") or {})
    environment_type = str((environment or {}).get("environmentType") or "").strip().lower()
    if environment_type == "oim":
        environment_type = "oig"
    if environment_type:
        products[environment_type] = True
    weblogic = (environment or {}).get("weblogic") or {}
    admin_host = weblogic.get("adminHost") or {}
    if (
        products.get("oam")
        or products.get("oig")
        or products.get("soa")
        or products.get("weblogic")
        or weblogic.get("enabled")
        or str(weblogic.get("adminUrl") or "").strip()
        or str(weblogic.get("oracleHome") or "").strip()
        or str(admin_host.get("host") or "").strip()
    ):
        products["weblogic"] = True
    if products.get("oig"):
        products["soa"] = False
    return products


def logscope_product_name(code):
    return {
        "WEBLOGIC": "WebLogic",
        "OIG": "Oracle Identity Governance",
        "OAM": "Oracle Access Manager",
        "SOA": "Oracle SOA Suite",
        "OUD": "Oracle Unified Directory",
        "OID": "Oracle Internet Directory",
        "OAA": "Oracle Advanced Authentication",
    }.get(str(code or "").upper(), "Logs")


def logscope_product_code_for_server(server_name):
    name = str(server_name or "").lower()
    if "oim" in name or "oig" in name:
        return "OIG"
    if "soa" in name:
        return "SOA"
    if "oam" in name:
        return "OAM"
    if "oud" in name:
        return "OUD"
    if "oid" in name or "ods" in name:
        return "OID"
    return "WEBLOGIC"


def logscope_important_files(code, server_name):
    code = str(code or "").upper()
    server = str(server_name or "")
    if code == "OUD":
        return ["access", "errors", "admin", "replication", "audit"]
    if code == "OID":
        return ["oidldapd", "access", "error", "diagnostic"]
    if code == "OAA":
        return ["oaa", "admin", "risk", "spui", "error", "warn"]
    if code == "OIG":
        return ["{0}.log".format(server), "{0}-diagnostic.log".format(server), "oim", "IAM-", ".out"]
    if code == "SOA":
        return ["{0}.log".format(server), "{0}-diagnostic.log".format(server), "soa", ".out"]
    if code == "OAM":
        return ["{0}.log".format(server), "{0}-diagnostic.log".format(server), "oam", ".out"]
    return ["{0}.log".format(server), "{0}-diagnostic.log".format(server), "access.log", ".out", ".log"]


def logscope_clone_target(target, host=None):
    cloned = dict(target or {})
    cloned["mode"] = cloned.get("mode") or "ssh"
    if host:
        cloned["host"] = host
    return cloned


def logscope_node_profile_id(environment, host, node_name):
    return "iam-dashboard-{0}-{1}".format(
        logscope_slug((environment or {}).get("id") or (environment or {}).get("name") or "environment"),
        logscope_slug(node_name or host or "primary"),
    )


def logscope_create_profile(environment, target, node_name):
    target = target or {}
    return {
        "id": logscope_node_profile_id(environment, target.get("host"), node_name),
        "name": "{0} - {1}".format((environment or {}).get("name") or "Environment", node_name or target.get("host") or "Logs"),
        "host": target.get("host") or "",
        "port": int(target.get("port") or 22),
        "username": target.get("username") or "oracle",
        "products": [],
        "logGroups": [],
        "_target": target,
    }


def logscope_add_group(profile, group):
    directory = str((group or {}).get("directory") or "").strip()
    if not profile or not directory:
        return
    code = str(group.get("productCode") or "WEBLOGIC").strip().upper()
    base_id = logscope_slug("{0}-{1}".format(code, group.get("name") or "logs"))
    existing_ids = set(item.get("id") for item in profile.get("logGroups") or [])
    group_id = base_id
    suffix = 2
    while group_id in existing_ids:
        group_id = "{0}-{1}".format(base_id, suffix)
        suffix += 1
    profile["logGroups"].append({
        "id": group_id,
        "name": str(group.get("name") or "Logs").strip(),
        "product": logscope_product_name(code),
        "productCode": code,
        "serverType": str(group.get("serverType") or "").strip().lower(),
        "directory": directory,
        "important": group.get("important") or logscope_important_files(code, group.get("name")),
    })
    if code not in profile["products"]:
        profile["products"].append(code)


def logscope_public_profiles(profiles):
    public = []
    for profile in profiles:
        if not profile.get("host") or not profile.get("logGroups"):
            continue
        public.append({
            "id": profile.get("id"),
            "name": profile.get("name"),
            "host": profile.get("host"),
            "port": profile.get("port") or 22,
            "username": profile.get("username") or "oracle",
            "products": profile.get("products") or [],
            "logGroups": profile.get("logGroups") or [],
        })
    return public


def logscope_install_config_value(metrics, names):
    wanted = [str(item or "").lower() for item in (names or [])]
    for row in (metrics or {}).get("installConfig") or []:
        text = " ".join(str(row.get(key) or "").lower() for key in ("name", "label", "property", "setting"))
        if any(name in text for name in wanted):
            return str(row.get("value") or "").strip()
    return ""


def build_environment_logscope_profiles(environment, dashboard=None):
    environment = environment or {}
    dashboard = dashboard or {}
    products = logscope_products(environment)
    primary_target = build_environment_target(environment)
    weblogic_target = build_weblogic_target(environment, primary_target)
    profiles = {}

    def ensure(target, node_name):
        profile = logscope_create_profile(environment, target, node_name)
        existing = profiles.get(profile["id"])
        if existing:
            return existing
        profiles[profile["id"]] = profile
        return profile

    primary_profile = ensure(primary_target, "Primary")
    product_metrics = dashboard.get("productMetrics") or {}

    if products.get("weblogic"):
        weblogic = environment.get("weblogic") or {}
        weblogic_metrics = product_metrics.get("weblogic") or {}
        oam_metrics = product_metrics.get("oam") or {}
        oam_runtime = oam_metrics.get("runtime") or {}
        effective_weblogic_metrics = dict(weblogic_metrics or {})
        for key in ("serverInventory", "domainHome", "oracleHome", "domainHomeSource", "domainRegistryFile"):
            if not effective_weblogic_metrics.get(key) and oam_runtime.get(key):
                effective_weblogic_metrics[key] = oam_runtime.get(key)
        if not effective_weblogic_metrics.get("serverInventory") and oam_runtime.get("serverInventory"):
            effective_weblogic_metrics["serverInventory"] = oam_runtime.get("serverInventory")
        domain_home = (
            weblogic.get("domainHome")
            or (environment.get("oig") or {}).get("domainHome")
            or (environment.get("oam") or {}).get("domainHome")
            or (environment.get("soa") or {}).get("domainHome")
            or effective_weblogic_metrics.get("domainHome")
            or oam_metrics.get("domainHome")
            or ""
        )
        if domain_home:
            node_profiles = {}
            nodes = build_discovered_weblogic_cluster_targets(environment, weblogic_target, effective_weblogic_metrics)
            for index, node in enumerate(nodes or []):
                node_name = node.get("name") or "Node {0}".format(index + 1)
                node_target = node.get("target") or weblogic_target
                node_profiles[node_name] = ensure(node_target, node_name)
            inventory = effective_weblogic_metrics.get("serverInventory") or []
            if not inventory:
                inventory = [{"name": name, "type": "ADMIN" if "admin" in str(name).lower() else "MANAGED"} for name in weblogic.get("serverNames") or []]
            if not inventory:
                inventory = [{"name": "AdminServer", "type": "ADMIN"}]
            for row in inventory:
                server_name = str(row.get("name") or row.get("server") or "").strip()
                if not server_name:
                    continue
                profile = None
                for node in nodes or []:
                    if server_name in (node.get("sourceServers") or []):
                        profile = ensure(node.get("target") or weblogic_target, node.get("name") or node.get("id") or "Node")
                        break
                if not profile:
                    profile = ensure(weblogic_target, "AdminServer Host")
                code = logscope_product_code_for_server(server_name)
                logscope_add_group(profile, {
                    "name": server_name,
                    "productCode": code,
                    "serverType": "admin" if "admin" in str(row.get("type") or server_name).lower() else "managed",
                    "directory": logscope_unix_join(domain_home, "servers", server_name, "logs"),
                    "important": logscope_important_files(code, server_name),
                })

    if products.get("oud"):
        oud = environment.get("oud") or {}
        target = logscope_clone_target(primary_target, oud.get("host") or primary_target.get("host"))
        profile = ensure(target, "OUD")
        instances = oud.get("instances") or []
        if not instances and oud.get("instanceHome"):
            instances = [{"name": "OUD Instance", "instanceHome": oud.get("instanceHome")}]
        for index, instance in enumerate(instances):
            name = instance.get("name") or "OUD Instance {0}".format(index + 1)
            home = instance.get("instanceHome") or instance.get("path") or ""
            logscope_add_group(profile, {
                "name": name,
                "productCode": "OUD",
                "directory": logscope_unix_join(home, "logs"),
                "important": logscope_important_files("OUD", name),
            })

    if products.get("oid"):
        oid = environment.get("oid") or {}
        target = logscope_clone_target(primary_target, oid.get("host") or primary_target.get("host"))
        profile = ensure(target, "OID")
        oracle_home = oid.get("oracleHome") or (environment.get("weblogic") or {}).get("oracleHome") or ""
        logscope_add_group(profile, {
            "name": "OID LDAP Logs",
            "productCode": "OID",
            "directory": logscope_unix_join(oracle_home, "ldap", "log"),
            "important": logscope_important_files("OID", "oidldapd"),
        })
        logscope_add_group(profile, {
            "name": "OID Diagnostics",
            "productCode": "OID",
            "directory": logscope_unix_join(oracle_home, "diagnostics", "logs", "OID"),
            "important": logscope_important_files("OID", "diagnostic"),
        })

    if products.get("oaa"):
        oaa = environment.get("oaa") or {}
        kube_host = oaa.get("kubeHost") or {}
        target = logscope_clone_target(kube_host or primary_target, kube_host.get("host") or primary_target.get("host"))
        profile = ensure(target, "OAA")
        logs_path = logscope_install_config_value(product_metrics.get("oaa") or {}, ["install.mount.logs.path", "logs mount path", "logs.mount"])
        if not logs_path:
            settings_path = (
                (product_metrics.get("oaa") or {}).get("settingsPath")
                or logscope_install_config_value(product_metrics.get("oaa") or {}, ["install.mount.config.path", "config mount path", "settings path"])
            )
            settings_path = str(settings_path or "").strip().rstrip("/")
            if settings_path:
                parent_path = settings_path.rsplit("/", 1)[0] if "/" in settings_path else ""
                if parent_path:
                    logs_path = logscope_unix_join(parent_path, "logs")
        if logs_path:
            logscope_add_group(profile, {
                "name": "OAA Mounted Logs",
                "productCode": "OAA",
                "directory": logs_path,
                "important": logscope_important_files("OAA", "OAA"),
            })

    return [profile for profile in profiles.values() if profile.get("host") and profile.get("logGroups")]


def load_environment_logscope_profiles(environment_id):
    environment = get_environment(DB_PATH, environment_id, include_secret=True)
    if not environment:
        raise KeyError("Environment not found.")
    dashboard = load_environment_snapshot(DB_PATH, environment_id) or {}
    try:
        dashboard = hydrate_dashboard_payload(dashboard)
    except Exception:
        pass
    return environment, build_environment_logscope_profiles(environment, dashboard)


def find_logscope_group(profiles, profile_id, group_id):
    for profile in profiles:
        if str(profile.get("id")) != str(profile_id):
            continue
        for group in profile.get("logGroups") or []:
            if str(group.get("id")) == str(group_id):
                return profile, group
    raise KeyError("Log source not found.")


def parse_logscope_file_rows(output):
    rows = []
    for line in str(output or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append({"name": parts[0], "size": parts[1], "sizeBytes": parts[1], "modified": parts[2]})
    return rows


def list_logscope_files(profile, group):
    directory = str(group.get("directory") or "").strip()
    command = (
        "base={base}; "
        "if [ ! -d \"$base\" ]; then echo '__IAM_LOG_ERROR__ Directory not found: '\"$base\"; exit 2; fi; "
        "find \"$base\" -maxdepth 1 -type f 2>/dev/null | while IFS= read -r f; do "
        "name=$(basename \"$f\"); size=$(wc -c < \"$f\" 2>/dev/null || echo 0); "
        "mtime=$(date -r \"$f\" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo ''); "
        "printf '%s\\t%s\\t%s\\n' \"$name\" \"$size\" \"$mtime\"; "
        "done | sort"
    ).format(base=shlex.quote(directory))
    result = run_target(profile.get("_target") or {}, command, timeout=45)
    output = result.get("output") or ""
    if result.get("exit_code") != 0 or "__IAM_LOG_ERROR__" in output:
        raise RuntimeError(output.replace("__IAM_LOG_ERROR__", "").strip() or "Could not list log files.")
    return parse_logscope_file_rows(output)


def read_logscope_file(profile, group, file_name, tail_lines=500, complete=False, latest_first=False):
    safe_name = os.path.basename(str(file_name or "").strip())
    if not safe_name or safe_name != str(file_name or "").strip() or safe_name in (".", ".."):
        raise ValueError("Invalid log file name.")
    try:
        tail_lines = max(50, min(5000, int(tail_lines or 500)))
    except (TypeError, ValueError):
        tail_lines = 500
    directory = str(group.get("directory") or "").strip()
    read_command = (
        "if command -v tac >/dev/null 2>&1; then tac -- \"$target\"; "
        "else awk '{line[NR]=$0} END{for(i=NR;i>0;i--) print line[i]}' \"$target\"; fi"
        if complete and latest_first
        else "cat -- \"$target\""
        if complete
        else "tail -n {lines} -- \"$target\" | {reverse}".format(
            lines=int(tail_lines),
            reverse=(
                "(if command -v tac >/dev/null 2>&1; then tac; "
                "else awk '{line[NR]=$0} END{for(i=NR;i>0;i--) print line[i]}'; fi)"
                if latest_first
                else "cat"
            ),
        )
    )
    command = (
        "base={base}; file={file}; target=\"$base/$file\"; "
        "if [ ! -f \"$target\" ]; then echo '__IAM_LOG_ERROR__ File not found: '\"$target\"; exit 2; fi; "
        "{read_command}"
    ).format(base=shlex.quote(directory), file=shlex.quote(safe_name), read_command=read_command)
    result = run_target(profile.get("_target") or {}, command, timeout=None if complete else 60)
    output = result.get("output") or ""
    if result.get("exit_code") != 0 or "__IAM_LOG_ERROR__" in output:
        raise RuntimeError(output.replace("__IAM_LOG_ERROR__", "").strip() or "Could not read log file.")
    return output


def sanitize_logscope_file_names(file_names, max_files=100):
    if isinstance(file_names, str):
        file_names = [file_names]
    names = []
    for item in file_names or []:
        raw = str(item or "").strip()
        safe_name = os.path.basename(raw)
        if not safe_name or safe_name != raw or safe_name in (".", ".."):
            raise ValueError("Invalid log file name.")
        if safe_name not in names:
            names.append(safe_name)
    if not names:
        raise ValueError("Select one or more log files.")
    if len(names) > max_files:
        raise ValueError(f"Select {max_files} or fewer log files at a time.")
    return names


def download_logscope_files(profile, group, file_names):
    names = sanitize_logscope_file_names(file_names)
    directory = str(group.get("directory") or "").strip()
    file_args = " ".join(shlex.quote(name) for name in names)
    archive_command = (
        "base={base}; tmp=$(mktemp /tmp/iam-log-download.XXXXXX.tar.gz); "
        "err=$(mktemp /tmp/iam-log-download.XXXXXX.err); "
        "cleanup(){{ rm -f \"$tmp\" \"$err\"; }}; trap cleanup EXIT; "
        "if [ ! -d \"$base\" ]; then echo '__IAM_LOG_ERROR__ Directory not found: '\"$base\"; exit 2; fi; "
        "cd \"$base\" || exit 2; "
        "tar -czf \"$tmp\" -- {files} 2>\"$err\" || "
        "{{ echo '__IAM_LOG_ERROR__ Could not archive selected logs.'; cat \"$err\"; exit 2; }}; "
        "base64 \"$tmp\""
    ).format(base=shlex.quote(directory), files=file_args)
    try:
        result = run_target(profile.get("_target") or {}, archive_command, timeout=None)
        output = result.get("output") or ""
        if result.get("exit_code") == 0 and "__IAM_LOG_ERROR__" not in output:
            payload = "".join(output.split())
            if payload:
                return base64.b64decode(payload), names, "application/gzip", "tar.gz"
        if "__IAM_LOG_ERROR__" in output:
            raise RuntimeError(output.replace("__IAM_LOG_ERROR__", "").strip())
    except Exception:
        # Fall back to the portable ZIP path if tar/base64 is unavailable on the target.
        pass
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            try:
                content = read_logscope_file(profile, group, name, complete=True, latest_first=False)
                archive.writestr(name, content)
            except Exception as exc:
                archive.writestr(f"{name}.ERROR.txt", str(exc))
    return buffer.getvalue(), names, "application/zip", "zip"


def search_logscope_files(profile, group, file_names, query, max_matches=500):
    names = sanitize_logscope_file_names(file_names)
    term = str(query or "").strip()
    if not term:
        raise ValueError("Enter a search string.")
    if "\n" in term or "\r" in term:
        raise ValueError("Search string must be one line.")
    try:
        max_matches = max(1, min(2000, int(max_matches or 500)))
    except (TypeError, ValueError):
        max_matches = 500
    directory = str(group.get("directory") or "").strip()
    file_args = " ".join(shlex.quote(name) for name in names)
    command = (
        "base={base}; term={term}; "
        "if [ ! -d \"$base\" ]; then echo '__IAM_LOG_ERROR__ Directory not found: '\"$base\"; exit 2; fi; "
        "for file in {files}; do "
        "target=\"$base/$file\"; [ -f \"$target\" ] || continue; "
        "grep -n -i -F -- \"$term\" \"$target\" 2>/dev/null | "
        "while IFS= read -r line; do printf '%s\\t%s\\n' \"$file\" \"$line\"; done; "
        "done | head -n {limit}"
    ).format(
        base=shlex.quote(directory),
        term=shlex.quote(term),
        files=file_args,
        limit=int(max_matches),
    )
    result = run_target(profile.get("_target") or {}, command, timeout=120)
    output = result.get("output") or ""
    if result.get("exit_code") != 0 or "__IAM_LOG_ERROR__" in output:
        raise RuntimeError(output.replace("__IAM_LOG_ERROR__", "").strip() or "Could not search log files.")
    matches = []
    for line in output.splitlines():
        if "\t" not in line:
            continue
        file_name, rest = line.split("\t", 1)
        line_number = ""
        text = rest
        if ":" in rest:
            possible_line, text = rest.split(":", 1)
            if possible_line.isdigit():
                line_number = possible_line
            else:
                text = rest
        matches.append({"fileName": file_name, "lineNumber": line_number, "text": text})
    return {
        "query": term,
        "files": names,
        "matches": matches,
        "matchCount": len(matches),
        "maxMatches": max_matches,
        "truncated": len(matches) >= max_matches,
    }


def sanitize_uploaded_key_name(file_name):
    name = os.path.basename(str(file_name or "").strip())
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name or "private-key.pem"


def save_uploaded_private_key(db_path, payload):
    payload = payload or {}
    file_name = sanitize_uploaded_key_name(payload.get("fileName") or payload.get("name"))
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Private key file content is required.")

    encoded = content.encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise ValueError("Private key upload is too large.")

    os.makedirs(UPLOADED_KEY_ROOT, exist_ok=True)
    try:
        os.chmod(UPLOADED_KEY_ROOT, 0o700)
    except Exception:
        pass

    timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    stored_name = "{0}-{1}-{2}".format(timestamp, uuid.uuid4().hex[:8], file_name)
    full_path = os.path.join(UPLOADED_KEY_ROOT, stored_name)
    normalized_content = content.replace("\r\n", "\n")
    with open(full_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalized_content)
    try:
        os.chmod(full_path, 0o600)
    except Exception:
        pass

    return {
        "path": full_path,
        "fileName": file_name,
        "storedName": stored_name,
        "size": len(encoded),
    }


def test_ssh_target(payload):
    payload = payload or {}
    profile = normalize_ssh_profile_payload(payload, default_username="")
    if not str(profile.get("host") or "").strip():
        raise ValueError("SSH host is required.")
    if not str(profile.get("username") or "").strip():
        raise ValueError("SSH username is required.")
    if profile.get("authType") == "password" and not str(profile.get("password") or "").strip():
        raise ValueError("SSH password is required for this test.")
    if profile.get("authType") == "private_key" and not str(profile.get("privateKeyPath") or "").strip():
        raise ValueError("Private key path is required for this test.")

    def clean_failure(output):
        text = " ".join(str(output or "").split())
        return text[:500] if text else "No command output was returned."

    plain_profile = dict(profile)
    plain_profile["sudoRequired"] = False
    result = run_target(plain_profile, "hostname; printf '\\n'; id -un")
    if result.get("exit_code") != 0:
        raise ValueError("SSH login failed from the dashboard host: {0}".format(clean_failure(result.get("output"))))

    output_lines = [line.strip() for line in str(result.get("output") or "").splitlines() if line.strip()]
    hostname = output_lines[0] if output_lines else ""
    login_user = output_lines[1] if len(output_lines) > 1 else profile.get("username") or ""
    if profile.get("sudoRequired"):
        sudo_result = run_target(profile, "hostname; printf '\\n'; id -un")
        if sudo_result.get("exit_code") != 0:
            raise ValueError(
                "SSH login successful for {0}@{1}, but sudo verification failed: {2} Choose the non-sudo SSH mode if this host should run commands as {0}, or grant sudo access.".format(
                    profile.get("username") or "",
                    profile.get("host") or "",
                    clean_failure(sudo_result.get("output")),
                )
            )
        sudo_lines = [line.strip() for line in str(sudo_result.get("output") or "").splitlines() if line.strip()]
        hostname = sudo_lines[0] if sudo_lines else hostname
        login_user = sudo_lines[1] if len(sudo_lines) > 1 else login_user

    return {
        "ok": True,
        "host": profile.get("host") or "",
        "port": profile.get("port") or 22,
        "sshMode": profile.get("sshMode") or "root_password",
        "sudoRequired": bool(profile.get("sudoRequired")),
        "hostname": hostname,
        "loginUser": login_user,
        "message": "SSH test succeeded for {0}@{1}{2}.".format(
            profile.get("username") or "",
            profile.get("host") or "",
            " with sudo" if profile.get("sudoRequired") else "",
        ),
    }


def send_notification_test(db_path, payload):
    payload = payload or {}
    settings = get_notification_settings(db_path, include_secret=True)
    if not settings.get("smtpHost") or not settings.get("smtpPort"):
        raise ValueError("SMTP host and port are required before sending a test email.")
    if not settings.get("senderEmail"):
        raise ValueError("Sender email is required before sending a test email.")

    target_email = str(payload.get("targetEmail") or "").strip()
    if not target_email:
        raise ValueError("Test email target is required.")

    message = EmailMessage()
    sender_name = normalize_product_name(settings.get("senderName"))
    message["Subject"] = "FMW Monitoring Dashboard notification test"
    message["From"] = "{0} <{1}>".format(sender_name, settings.get("senderEmail"))
    message["To"] = target_email
    message.set_content(
        "This is a test email from FMW Monitoring Dashboard.\n\n"
        "If you received this message, the SMTP configuration is working."
    )

    host = settings.get("smtpHost")
    port = int(settings.get("smtpPort") or 0)
    username = settings.get("smtpUsername") or ""
    password = settings.get("smtpPassword") or ""
    mode = settings.get("securityMode") or "starttls"
    timeout = 20

    if mode == "ssl":
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=timeout) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            if mode == "starttls":
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)

    return {"sent": True, "targetEmail": target_email}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        query = parse_qs(parsed.query or "")

        if path == "/healthz":
            return self.handle_healthz()

        if path == "/api/app":
            return self.handle_app_shell(query)

        if path == "/api/admin/environments":
            return self.handle_admin_environments()

        if path == "/api/admin/notifications":
            return self.handle_admin_notifications()

        if path == "/api/admin/updates/check":
            return self.handle_admin_update_check()

        if path == "/api/admin/upgrade/status":
            return self.handle_admin_upgrade_status()

        match = re.match(r"^/api/admin/environments/([^/]+)/jobs$", path)
        if match:
            return self.handle_environment_jobs(match.group(1))

        match = re.match(r"^/api/environments/([^/]+)/dashboard$", path)
        if match:
            return self.handle_environment_dashboard(match.group(1), query)

        match = re.match(r"^/api/environments/([^/]+)/logs$", path)
        if match:
            return self.handle_environment_logs(match.group(1))

        if path == "/" or path == "":
            return self.handle_file(os.path.join(STATIC_ROOT, "index.html"))

        if path in ("/manual.html", "/help-feedback.html"):
            return self.handle_file(os.path.join(STATIC_ROOT, path.lstrip("/")))

        if path.startswith("/assets/"):
            return self.handle_file(os.path.join(STATIC_ROOT, path.lstrip("/")))

        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"

        if path == "/api/admin/ssh-keys/upload":
            return self.handle_upload_private_key()

        if path == "/api/admin/ssh-test":
            return self.handle_test_ssh()

        if path == "/api/admin/environments":
            return self.handle_create_environment()

        match = re.match(r"^/api/admin/environments/([^/]+)/bootstrap$", path)
        if match:
            return self.handle_bootstrap_environment(match.group(1))

        match = re.match(r"^/api/admin/environments/([^/]+)/jobs/run$", path)
        if match:
            return self.handle_run_environment_job(match.group(1))

        match = re.match(r"^/api/environments/([^/]+)/oaa/schema-metrics$", path)
        if match:
            return self.handle_oaa_schema_metrics(match.group(1))

        match = re.match(r"^/api/environments/([^/]+)/logs/list$", path)
        if match:
            return self.handle_environment_log_file_list(match.group(1))

        match = re.match(r"^/api/environments/([^/]+)/logs/read$", path)
        if match:
            return self.handle_environment_log_file_read(match.group(1))

        match = re.match(r"^/api/environments/([^/]+)/logs/download$", path)
        if match:
            return self.handle_environment_log_file_download(match.group(1))

        match = re.match(r"^/api/environments/([^/]+)/logs/search$", path)
        if match:
            return self.handle_environment_log_file_search(match.group(1))

        if path == "/api/admin/notifications/recipients":
            return self.handle_save_notification_recipient()

        if path == "/api/admin/notifications/test":
            return self.handle_notification_test()

        if path == "/api/admin/upgrade/run":
            return self.handle_run_github_upgrade()

        self.send_error(404, "Not found")

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path == "/api/admin/notifications/settings":
            return self.handle_save_notification_settings()
        if path == "/api/admin/help/proxy":
            return self.handle_save_update_proxy_settings()
        match = re.match(r"^/api/admin/environments/([^/]+)$", path)
        if match:
            return self.handle_update_environment(match.group(1))
        self.send_error(404, "Not found")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        match = re.match(r"^/api/admin/notifications/recipients/([^/]+)$", path)
        if match:
            return self.handle_delete_notification_recipient(match.group(1))
        match = re.match(r"^/api/admin/environments/([^/]+)$", path)
        if match:
            return self.handle_delete_environment(match.group(1))
        self.send_error(404, "Not found")

    def send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_binary(self, status_code, content, content_type, file_name=None):
        body = content or b""
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if file_name:
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(file_name or "").strip()).strip("-") or "logs.zip"
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.end_headers()
        self.wfile.write(body)

    def handle_app_shell(self, query):
        try:
            config = load_runtime_config()
            force = str((query.get("force") or ["0"])[0]).strip() in ("1", "true", "yes")
            payload = CACHE.get_app_shell(config, force=force)
            self.send_json(200, payload)
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def handle_healthz(self):
        self.send_json(200, {"status": "ok"})

    def handle_environment_dashboard(self, environment_id, query):
        try:
            config = load_runtime_config()
            force = str((query.get("force") or ["0"])[0]).strip() in ("1", "true", "yes")
            payload = CACHE.get_environment(config, environment_id, force=force)
            self.send_json(200, payload)
        except KeyError:
            self.send_json(404, {"error": "Environment not found."})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def handle_environment_logs(self, environment_id):
        try:
            environment, profiles = load_environment_logscope_profiles(environment_id)
            public_profiles = logscope_public_profiles(profiles)
            self.send_json(
                200,
                {
                    "environmentId": environment.get("id"),
                    "environmentName": environment.get("name"),
                    "profiles": public_profiles,
                    "profileCount": len(public_profiles),
                    "sourceCount": sum(len(profile.get("logGroups") or []) for profile in public_profiles),
                },
            )
        except KeyError:
            self.send_json(404, {"error": "Environment not found."})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def handle_environment_log_file_list(self, environment_id):
        try:
            payload = parse_json_body(self)
            _, profiles = load_environment_logscope_profiles(environment_id)
            profile, group = find_logscope_group(
                profiles,
                payload.get("profileId"),
                payload.get("groupId"),
            )
            files = list_logscope_files(profile, group)
            self.send_json(
                200,
                {
                    "profileId": profile.get("id"),
                    "groupId": group.get("id"),
                    "directory": group.get("directory"),
                    "files": files,
                    "fileCount": len(files),
                },
            )
        except KeyError:
            self.send_json(404, {"error": "Log source not found."})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_environment_log_file_read(self, environment_id):
        try:
            payload = parse_json_body(self)
            _, profiles = load_environment_logscope_profiles(environment_id)
            profile, group = find_logscope_group(
                profiles,
                payload.get("profileId"),
                payload.get("groupId"),
            )
            file_name = str(payload.get("fileName") or "").strip()
            try:
                tail_lines = max(50, min(5000, int(payload.get("tailLines") or 500)))
            except (TypeError, ValueError):
                tail_lines = 500
            complete = bool(payload.get("complete"))
            latest_first = bool(payload.get("latestFirst"))
            text = read_logscope_file(
                profile,
                group,
                file_name,
                tail_lines=tail_lines,
                complete=complete,
                latest_first=latest_first,
            )
            self.send_json(
                200,
                {
                    "profileId": profile.get("id"),
                    "groupId": group.get("id"),
                    "directory": group.get("directory"),
                    "fileName": file_name,
                    "tailLines": tail_lines,
                    "complete": complete,
                    "latestFirst": latest_first,
                    "mode": "complete" if complete else "tail",
                    "text": text,
                },
            )
        except KeyError:
            self.send_json(404, {"error": "Log source not found."})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_environment_log_file_download(self, environment_id):
        try:
            payload = parse_json_body(self)
            environment, profiles = load_environment_logscope_profiles(environment_id)
            profile, group = find_logscope_group(
                profiles,
                payload.get("profileId"),
                payload.get("groupId"),
            )
            content, names, content_type, extension = download_logscope_files(profile, group, payload.get("fileNames") or [])
            stem = f"{environment.get('name') or environment_id}-{group.get('name') or 'logs'}"
            self.send_binary(200, content, content_type, f"{stem}-{len(names)}-files.{extension}")
        except KeyError:
            self.send_json(404, {"error": "Log source not found."})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_environment_log_file_search(self, environment_id):
        try:
            payload = parse_json_body(self)
            _, profiles = load_environment_logscope_profiles(environment_id)
            profile, group = find_logscope_group(
                profiles,
                payload.get("profileId"),
                payload.get("groupId"),
            )
            result = search_logscope_files(
                profile,
                group,
                payload.get("fileNames") or [],
                payload.get("query") or "",
                payload.get("maxMatches") or 500,
            )
            self.send_json(
                200,
                {
                    "profileId": profile.get("id"),
                    "groupId": group.get("id"),
                    "directory": group.get("directory"),
                    **result,
                },
            )
        except KeyError:
            self.send_json(404, {"error": "Log source not found."})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_admin_environments(self):
        try:
            config = load_runtime_config()
            payload = {
                "environments": list_environments(DB_PATH, include_secret=False),
                "monitoringServer": config.get("monitoring_server") or {},
                "operations": config.get("operations") or {},
                "defaultCollectionMinutes": get_default_collection_minutes(),
            }
            self.send_json(200, payload)
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def handle_admin_notifications(self):
        try:
            self.send_json(200, notification_payload(DB_PATH))
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def handle_admin_update_check(self):
        try:
            self.send_json(200, build_update_check_payload())
        except Exception as exc:
            self.send_json(
                200,
                {
                    "currentVersion": read_version(),
                    "remoteVersion": "",
                    "repoUrl": github_repo_url(),
                    "versionUrl": github_version_url(),
                    "branch": GITHUB_BRANCH,
                    "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status": "error",
                    "message": "GitHub update check failed: {0}".format(str(exc)),
                    "proxyConfigured": False,
                    "proxySource": "none",
                    "proxySourceLabel": "No proxy configured",
                },
            )

    def handle_save_update_proxy_settings(self):
        try:
            payload = parse_json_body(self)
            settings = save_update_proxy_settings(DB_PATH, payload)
            self.send_json(
                200,
                {
                    "settings": settings,
                    "proxy": merge_update_check_proxy_settings(saved_settings=settings),
                },
            )
        except Exception as exc:
            if isinstance(exc, PermissionError):
                upgrade_state_dir = os.path.join(os.path.dirname(DB_PATH), "upgrade")
                service_user = os.environ.get("IAM_MONITORING_SERVICE_USER", "iam-monitoring")
                self.send_json(
                    400,
                    {
                        "error": (
                            "GitHub upgrade state is not writable by the dashboard service. "
                            "Repair {0} ownership back to the {1} service user and retry."
                        ).format(upgrade_state_dir, service_user)
                    },
                )
                return
            self.send_json(400, {"error": str(exc)})

    def handle_admin_upgrade_status(self):
        try:
            self.send_json(
                200,
                {
                    "enabled": True,
                    "repoUrl": github_repo_url(),
                    "archiveUrl": github_archive_url(),
                    "branch": GITHUB_BRANCH,
                    "serviceName": UPGRADE_SERVICE_NAME,
                    "status": read_upgrade_status(DB_PATH),
                },
            )
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def handle_run_github_upgrade(self):
        try:
            load_runtime_config()
            update_check = build_update_check_payload()
            current_version = read_version()
            remote_version = str(update_check.get("remoteVersion") or "").strip()
            update_proxy = merge_update_check_proxy_settings()

            if update_check.get("status") == "error":
                raise ValueError(update_check.get("message") or "GitHub version check failed.")

            if update_check.get("status") in ("current", "ahead"):
                status_name = "current" if update_check.get("status") == "current" else "ahead"
                message = (
                    "You are already on the latest version."
                    if status_name == "current"
                    else (update_check.get("message") or "This dashboard is already ahead of the GitHub branch.")
                )
                append_upgrade_log(DB_PATH, message)
                status = write_upgrade_status(
                    DB_PATH,
                    {
                        "status": status_name,
                        "requestedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "startedAt": "",
                        "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "message": message,
                        "repoUrl": github_repo_url(),
                        "archiveUrl": github_archive_url(),
                        "branch": GITHUB_BRANCH,
                        "currentVersion": current_version,
                        "targetVersion": remote_version or current_version,
                        "requestId": "",
                        "lastError": "",
                    },
                )
                self.send_json(
                    200,
                    {
                        "alreadyCurrent": True,
                        "message": message,
                        "updateCheck": update_check,
                        "upgrade": build_github_upgrade_response(status),
                    },
                )
                return

            status = queue_github_upgrade(
                DB_PATH,
                {
                    "requestedBy": "ui",
                    "repoUrl": github_repo_url(),
                    "archiveUrl": github_archive_url(),
                    "branch": GITHUB_BRANCH,
                    "currentVersion": current_version,
                    "targetVersion": remote_version,
                    "proxySettings": update_proxy.get("effectiveSettings") or {},
                },
            )
            self.send_json(
                202,
                {
                    "alreadyCurrent": False,
                    "message": update_check.get("message") or "GitHub upgrade queued.",
                    "updateCheck": update_check,
                    "upgrade": build_github_upgrade_response(status),
                },
            )
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_create_environment(self):
        try:
            payload = parse_json_body(self)
            load_runtime_config()
            environment = save_environment(DB_PATH, payload)
            CACHE.invalidate()
            self.send_json(201, {"environment": get_environment(DB_PATH, environment.get("id"), include_secret=False)})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_update_environment(self, environment_id):
        try:
            payload = parse_json_body(self)
            load_runtime_config()
            if not get_environment(DB_PATH, environment_id, include_secret=True):
                return self.send_json(404, {"error": "Environment not found."})
            environment = save_environment(DB_PATH, payload, environment_id=environment_id)
            CACHE.invalidate(environment_id)
            self.send_json(200, {"environment": get_environment(DB_PATH, environment.get("id"), include_secret=False)})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_oaa_schema_metrics(self, environment_id):
        try:
            load_runtime_config()
            environment = get_environment(DB_PATH, environment_id, include_secret=True)
            if not environment:
                return self.send_json(404, {"error": "Environment not found."})
            if not ((environment.get("products") or {}).get("oaa")):
                return self.send_json(400, {"error": "OAA is not enabled for this environment."})

            payload = parse_json_body(self)
            oaa_settings = environment.get("oaa") or {}
            target = build_oaa_target(environment, build_environment_target(environment))
            install_rows, install_values, install_command, install_error, install_path = collect_oaa_install_properties_from_host(
                target,
                oaa_settings.get("installSettingsPath"),
            )
            install_database = build_oaa_database_summary(install_values)
            install_password = oaa_database_password_from_install(install_values)

            dashboard = load_environment_snapshot(DB_PATH, environment_id) or {}
            snapshot_database = (((dashboard.get("productMetrics") or {}).get("oaa") or {}).get("database") or {})
            saved_database = oaa_settings.get("database") or {}
            request_database = (payload or {}).get("database") or {}
            database = merge_oaa_database_settings(
                install_database,
                saved_database,
                snapshot_database,
                request_database,
            )
            if install_password:
                database["hasPassword"] = True

            missing_fields = oaa_database_missing_fields(database)
            request_password = str((payload or {}).get("databasePassword") or request_database.get("password") or "").strip()
            effective_password = request_password or install_password
            if missing_fields or not effective_password:
                message_parts = []
                if missing_fields:
                    message_parts.append("Missing OAA database details: {0}".format(", ".join(missing_fields)))
                if not effective_password:
                    message_parts.append("Missing OAA database password")
                return self.send_json(
                    400,
                    {
                        "error": ". ".join(message_parts) + ".",
                        "needsDatabaseDetails": bool(missing_fields),
                        "needsDatabasePassword": not bool(effective_password),
                        "missingFields": missing_fields,
                        "database": database,
                        "installConfigSourcePath": install_path,
                        "installConfigError": install_error,
                        "installConfigCommand": install_command,
                    },
                )

            if request_database:
                environment.setdefault("oaa", {})["database"] = merge_oaa_database_settings(saved_database, request_database)
                save_environment(DB_PATH, environment, environment_id=environment_id)
                CACHE.invalidate(environment_id)

            fallback_sqlplus_targets = []
            products = environment.get("products") or {}
            if products.get("oam") or products.get("weblogic") or products.get("oig") or products.get("oid"):
                weblogic_settings = environment.get("weblogic") or {}
                oam_settings = environment.get("oam") or {}
                oig_settings = environment.get("oig") or {}
                oid_settings = environment.get("oid") or {}
                fallback_oracle_home = (
                    weblogic_settings.get("oracleHome")
                    or oam_settings.get("oracleHome")
                    or oig_settings.get("oracleHome")
                    or oid_settings.get("oracleHome")
                    or ""
                )
                fallback_target = build_weblogic_target(environment, build_environment_target(environment))
                if fallback_oracle_home and (fallback_target or {}).get("host"):
                    fallback_sqlplus_targets.append(
                        {
                            "label": "WebLogic/OAM host {0}".format(fallback_target.get("host")),
                            "target": fallback_target,
                            "oracleHome": fallback_oracle_home,
                        }
                    )

            result = collect_oaa_schema_table_metrics(
                target,
                database,
                effective_password,
                oaa_settings=oaa_settings,
                fallback_sqlplus_targets=fallback_sqlplus_targets,
            )
            self.send_json(
                200,
                {
                    "database": database,
                    "schemaTables": result.get("tables") or [],
                    "schemaTableSummary": result.get("summary") or {},
                    "schemaTablesCommand": result.get("command") or "",
                    "schemaTablesError": result.get("error") or "",
                    "installConfigSourcePath": install_path,
                    "installConfigError": install_error,
                    "installConfigCommand": install_command,
                },
            )
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_delete_environment(self, environment_id):
        try:
            load_runtime_config()
            deleted = delete_environment(DB_PATH, environment_id)
            if not deleted:
                return self.send_json(404, {"error": "Environment not found."})
            clear_environment_runtime_state(DB_PATH, environment_id)
            CACHE.invalidate(environment_id)
            self.send_json(200, {"deleted": True})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_bootstrap_environment(self, environment_id):
        try:
            load_runtime_config()
            if not get_environment(DB_PATH, environment_id, include_secret=True):
                return self.send_json(404, {"error": "Environment not found."})
            payload = parse_json_body(self)
            initial_target_override = (payload or {}).get("initialTarget") or {}
            environment = bootstrap_environment_runtime(
                DB_PATH,
                environment_id,
                initial_target_override=initial_target_override,
            )
            job_status, started = launch_collection_job(DB_PATH, environment_id, trigger="bootstrap")
            CACHE.invalidate(environment_id)
            self.send_json(
                200,
                {
                    "environment": get_environment(DB_PATH, environment_id, include_secret=False),
                    "runtimeEnvPath": ((environment.get("bootstrap") or {}).get("runtimeEnvPath")) or "",
                    "collectorJob": job_status,
                    "collectorJobStarted": bool(started),
                    "collectorJobAlreadyRunning": not bool(started) and job_status.get("status") == "running",
                },
            )
        except KeyError:
            self.send_json(404, {"error": "Environment not found."})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_environment_jobs(self, environment_id):
        try:
            load_runtime_config()
            if not get_environment(DB_PATH, environment_id, include_secret=True):
                return self.send_json(404, {"error": "Environment not found."})
            snapshot = load_environment_snapshot(DB_PATH, environment_id)
            self.send_json(
                200,
                {
                    "collectorJob": read_job_status(DB_PATH, environment_id),
                    "lastSnapshot": extract_environment_overview(snapshot) if snapshot else None,
                },
            )
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_run_environment_job(self, environment_id):
        try:
            load_runtime_config()
            if not get_environment(DB_PATH, environment_id, include_secret=True):
                return self.send_json(404, {"error": "Environment not found."})
            job_status, started = launch_collection_job(DB_PATH, environment_id, trigger="manual")
            self.send_json(
                200,
                {
                    "collectorJob": job_status,
                    "collectorJobStarted": bool(started),
                    "collectorJobAlreadyRunning": not bool(started) and job_status.get("status") == "running",
                },
            )
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_save_notification_settings(self):
        try:
            payload = parse_json_body(self)
            settings = save_notification_settings(DB_PATH, payload)
            self.send_json(200, {"settings": settings})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_save_notification_recipient(self):
        try:
            payload = parse_json_body(self)
            recipients = save_notification_recipient(DB_PATH, payload)
            self.send_json(200, {"recipients": recipients})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_delete_notification_recipient(self, recipient_id):
        try:
            deleted = delete_notification_recipient(DB_PATH, recipient_id)
            if not deleted:
                return self.send_json(404, {"error": "Recipient not found."})
            self.send_json(200, {"deleted": True, "recipients": notification_payload(DB_PATH)["recipients"]})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_notification_test(self):
        try:
            payload = parse_json_body(self)
            result = send_notification_test(DB_PATH, payload)
            self.send_json(200, result)
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_upload_private_key(self):
        try:
            payload = parse_json_body(self)
            result = save_uploaded_private_key(DB_PATH, payload)
            self.send_json(200, result)
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_test_ssh(self):
        try:
            payload = parse_json_body(self)
            result = test_ssh_target(payload)
            self.send_json(200, result)
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def handle_file(self, path):
        full_path = os.path.abspath(path)
        if not full_path.startswith(os.path.abspath(STATIC_ROOT)):
            return self.send_error(403, "Forbidden")
        if not os.path.isfile(full_path):
            return self.send_error(404, "Not found")
        content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        with open(full_path, "rb") as handle:
            content = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("IAM dashboard listening on http://{0}:{1}".format(HOST, PORT))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
