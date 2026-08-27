import base64
import json
import os
import re
import time
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from environment_registry import get_global_defaults, save_global_defaults
from support_store import get_update_proxy_settings
from upgrade_runtime import append_upgrade_log, queue_github_upgrade, read_upgrade_status, upgrade_is_busy


APP_ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_PATH = os.path.join(APP_ROOT, "VERSION")
GITHUB_OWNER = os.environ.get("IAM_MONITORING_GITHUB_OWNER", "reply4ramesh")
GITHUB_REPO = os.environ.get("IAM_MONITORING_GITHUB_REPO", "fmw_dashboard")
GITHUB_BRANCH = os.environ.get("IAM_MONITORING_GITHUB_BRANCH", "main")


def read_version():
    try:
        with open(VERSION_PATH, "r", encoding="utf-8") as handle:
            return handle.read().strip() or "unknown"
    except Exception:
        return "unknown"


def github_repo_url():
    return "https://github.com/{0}/{1}".format(GITHUB_OWNER, GITHUB_REPO)


def github_archive_url():
    return "https://github.com/{0}/{1}/archive/refs/heads/{2}.tar.gz".format(GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH)


def github_version_url():
    return "https://raw.githubusercontent.com/{0}/{1}/{2}/server-app/VERSION".format(GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH)


def github_version_api_url():
    return "https://api.github.com/repos/{0}/{1}/contents/server-app/VERSION?ref={2}".format(GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH)


def _env_proxy_settings():
    return {
        "httpProxy": str(os.environ.get("IAM_MONITORING_HTTP_PROXY", os.environ.get("http_proxy", "")) or "").strip(),
        "httpsProxy": str(os.environ.get("IAM_MONITORING_HTTPS_PROXY", os.environ.get("https_proxy", "")) or "").strip(),
        "noProxy": str(os.environ.get("IAM_MONITORING_NO_PROXY", os.environ.get("no_proxy", "")) or "").strip(),
    }


def _proxy_settings(db_path):
    saved = get_update_proxy_settings(db_path)
    env = _env_proxy_settings()
    return {
        "httpProxy": str(saved.get("httpProxy") or env.get("httpProxy") or "").strip(),
        "httpsProxy": str(saved.get("httpsProxy") or env.get("httpsProxy") or "").strip(),
        "noProxy": str(saved.get("noProxy") or env.get("noProxy") or "").strip(),
    }


def _open_url(url, proxy_settings):
    request = Request(url, headers={"User-Agent": "iam-monitoring-auto-update", "Cache-Control": "no-cache"})
    routing = {}
    if proxy_settings.get("httpProxy"):
        routing["http"] = proxy_settings.get("httpProxy")
    if proxy_settings.get("httpsProxy"):
        routing["https"] = proxy_settings.get("httpsProxy")
    if routing:
        return build_opener(ProxyHandler(routing)).open(request, timeout=8)
    return urlopen(request, timeout=8)


def read_github_version(proxy_settings):
    try:
        with _open_url(github_version_api_url(), proxy_settings) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = base64.b64decode(str(payload.get("content") or "").encode("utf-8")).decode("utf-8").strip()
        if content:
            return content
    except Exception:
        pass
    with _open_url("{0}?t={1}".format(github_version_url(), int(time.time())), proxy_settings) as response:
        return response.read().decode("utf-8").strip()


def _parse_version(value):
    text = str(value or "").strip().lower()
    if re.match(r"^\d+$", text):
        return ("number", int(text))
    if re.match(r"^\d+(?:\.\d+)+$", text):
        return ("dot", tuple(int(part) for part in text.split(".")))
    return None


def is_remote_newer(current_version, remote_version):
    current = _parse_version(current_version)
    remote = _parse_version(remote_version)
    if not current or not remote or current[0] != remote[0]:
        return False
    return current[1] < remote[1]


def maybe_run_daily_auto_update(db_path):
    defaults = get_global_defaults(db_path, include_secret=True)
    updates = defaults.get("updates") or {}
    if not updates.get("automaticEnabled"):
        return {"checked": False, "message": "Automatic GitHub updates are disabled."}

    now = time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    hour = int(updates.get("automaticHour") or 2)
    if str(updates.get("lastAutomaticCheckDate") or "") == today or now.tm_hour < hour:
        return {"checked": False, "message": "Automatic GitHub update check is not due."}

    status = read_upgrade_status(db_path)
    if upgrade_is_busy(status.get("status")):
        return {"checked": False, "message": "GitHub upgrade is already in progress."}

    updates["lastAutomaticCheckDate"] = today
    save_global_defaults(db_path, {"updates": updates})
    proxy = _proxy_settings(db_path)
    current_version = read_version()
    try:
        remote_version = read_github_version(proxy)
    except Exception as exc:
        append_upgrade_log(db_path, "Automatic GitHub update check failed: {0}".format(exc))
        return {"checked": True, "queued": False, "error": str(exc)}

    if not is_remote_newer(current_version, remote_version):
        append_upgrade_log(db_path, "Automatic GitHub update check found no newer version. Current {0}, GitHub {1}.".format(current_version, remote_version))
        return {"checked": True, "queued": False, "currentVersion": current_version, "remoteVersion": remote_version}

    queue_github_upgrade(
        db_path,
        {
            "requestedBy": "automatic_daily",
            "repoUrl": github_repo_url(),
            "archiveUrl": github_archive_url(),
            "branch": GITHUB_BRANCH,
            "currentVersion": current_version,
            "targetVersion": remote_version,
            "proxySettings": proxy,
        },
    )
    return {"checked": True, "queued": True, "currentVersion": current_version, "remoteVersion": remote_version}
