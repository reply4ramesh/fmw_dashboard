#!/usr/bin/env python3
"""Check GitHub for a newer dashboard build and queue the existing upgrader."""

import argparse
import base64
import json
import os
import re
import time
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from support_store import get_update_proxy_settings
from upgrade_runtime import append_upgrade_log, queue_github_upgrade


APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = "/etc/iam-monitoring.env"


def _truthy(value, default=True):
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "no", "off", "disabled"}


def load_environment_file(path):
    if not path or not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


def db_path():
    return os.environ.get(
        "IAM_MONITORING_DB_PATH",
        os.path.join(APP_ROOT, "state", "iam-monitoring.sqlite"),
    )


def read_version():
    try:
        with open(os.path.join(APP_ROOT, "VERSION"), "r", encoding="utf-8") as handle:
            return handle.read().strip() or "unknown"
    except Exception:
        return "unknown"


def github_settings():
    owner = os.environ.get("IAM_MONITORING_GITHUB_OWNER", "reply4ramesh").strip()
    repo = os.environ.get("IAM_MONITORING_GITHUB_REPO", "fmw_dashboard").strip()
    branch = os.environ.get("IAM_MONITORING_GITHUB_BRANCH", "main").strip() or "main"
    return {
        "branch": branch,
        "repoUrl": "https://github.com/{0}/{1}".format(owner, repo),
        "archiveUrl": "https://github.com/{0}/{1}/archive/refs/heads/{2}.tar.gz".format(
            owner, repo, branch
        ),
        "versionApiUrl": "https://api.github.com/repos/{0}/{1}/contents/server-app/VERSION?ref={2}".format(
            owner, repo, branch
        ),
        "versionRawUrl": "https://raw.githubusercontent.com/{0}/{1}/{2}/server-app/VERSION".format(
            owner, repo, branch
        ),
    }


def effective_proxy_settings(database_path):
    saved = get_update_proxy_settings(database_path)
    return {
        "httpProxy": str(
            saved.get("httpProxy")
            or os.environ.get("IAM_MONITORING_HTTP_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
            or ""
        ).strip(),
        "httpsProxy": str(
            saved.get("httpsProxy")
            or os.environ.get("IAM_MONITORING_HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or ""
        ).strip(),
        "noProxy": str(
            saved.get("noProxy")
            or os.environ.get("IAM_MONITORING_NO_PROXY")
            or os.environ.get("no_proxy")
            or os.environ.get("NO_PROXY")
            or ""
        ).strip(),
    }


def _open_url(url, proxy_settings):
    request = Request(
        url,
        headers={
            "User-Agent": "iam-monitoring-daily-update-check",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    proxy_map = {}
    if proxy_settings.get("httpProxy"):
        proxy_map["http"] = proxy_settings["httpProxy"]
    if proxy_settings.get("httpsProxy"):
        proxy_map["https"] = proxy_settings["httpsProxy"]
    if proxy_map:
        return build_opener(ProxyHandler(proxy_map)).open(request, timeout=15)
    return urlopen(request, timeout=15)


def read_github_version(settings, proxy_settings):
    errors = []
    try:
        with _open_url(settings["versionApiUrl"], proxy_settings) as response:
            payload = json.loads(response.read().decode("utf-8"))
        encoded = str(payload.get("content") or "")
        content = base64.b64decode(encoded.encode("utf-8")).decode("utf-8").strip()
        if content:
            return content
        errors.append("GitHub contents API returned an empty VERSION")
    except Exception as exc:
        errors.append("GitHub contents API failed: {0}".format(str(exc)))

    try:
        raw_url = "{0}?t={1}".format(settings["versionRawUrl"], int(time.time()))
        with _open_url(raw_url, proxy_settings) as response:
            content = response.read().decode("utf-8").strip()
        if content:
            return content
        errors.append("raw GitHub VERSION was empty")
    except Exception as exc:
        errors.append("raw GitHub VERSION failed: {0}".format(str(exc)))
    raise RuntimeError("; ".join(errors))


def _letter_version_value(value):
    total = 0
    for character in str(value or "").strip().lower():
        if not ("a" <= character <= "z"):
            return None
        total = (total * 26) + (ord(character) - 96)
    return total


def _parse_version(value):
    text = str(value or "").strip().lower()
    match = re.match(r"^(\d+)([a-z]+)$", text)
    if match:
        return ("letter", int(match.group(1)), _letter_version_value(match.group(2)))
    if re.match(r"^\d+(?:\.\d+)+$", text):
        return ("dot", tuple(int(part) for part in text.split(".")))
    if re.match(r"^\d+$", text):
        return ("number", int(text))
    return None


def compare_versions(local_version, remote_version):
    if str(local_version or "").strip() == str(remote_version or "").strip():
        return 0
    local_parsed = _parse_version(local_version)
    remote_parsed = _parse_version(remote_version)
    if not local_parsed or not remote_parsed or local_parsed[0] != remote_parsed[0]:
        return None
    return -1 if local_parsed[1:] < remote_parsed[1:] else 1


def check_and_queue_update(version_reader=read_github_version):
    database_path = db_path()
    if not _truthy(os.environ.get("IAM_MONITORING_AUTO_UPDATE_ENABLED"), default=True):
        message = "Daily GitHub auto-update check is disabled by IAM_MONITORING_AUTO_UPDATE_ENABLED."
        append_upgrade_log(database_path, message)
        return "disabled", message

    settings = github_settings()
    proxy_settings = effective_proxy_settings(database_path)
    current_version = read_version()
    remote_version = version_reader(settings, proxy_settings)
    comparison = compare_versions(current_version, remote_version)

    if comparison == 0:
        message = "Daily GitHub auto-update check: version {0} is current.".format(current_version)
        append_upgrade_log(database_path, message)
        return "current", message
    if comparison == 1:
        message = (
            "Daily GitHub auto-update check skipped: installed version {0} is ahead of GitHub version {1}."
        ).format(current_version, remote_version)
        append_upgrade_log(database_path, message)
        return "ahead", message
    if comparison is None:
        message = (
            "Daily GitHub auto-update check skipped: versions {0} and {1} cannot be safely ordered."
        ).format(current_version, remote_version)
        append_upgrade_log(database_path, message)
        return "different", message

    try:
        queue_github_upgrade(
            database_path,
            {
                "requestedBy": "daily-auto-update",
                "repoUrl": settings["repoUrl"],
                "archiveUrl": settings["archiveUrl"],
                "branch": settings["branch"],
                "currentVersion": current_version,
                "targetVersion": remote_version,
                "proxySettings": proxy_settings,
            },
        )
    except ValueError as exc:
        message = "Daily GitHub auto-update check did not queue another request: {0}".format(str(exc))
        append_upgrade_log(database_path, message)
        return "busy", message

    message = "Daily GitHub auto-update queued version {0}; installed version is {1}.".format(
        remote_version, current_version
    )
    append_upgrade_log(database_path, message)
    return "queued", message


def main():
    parser = argparse.ArgumentParser(description="Check GitHub and queue a newer FMW dashboard version.")
    parser.add_argument(
        "--config-file",
        default=os.environ.get("IAM_MONITORING_CONFIG", DEFAULT_CONFIG_PATH),
        help="Path to the dashboard environment file.",
    )
    args = parser.parse_args()
    load_environment_file(args.config_file)
    try:
        _, message = check_and_queue_update()
        print(message)
        return 0
    except Exception as exc:
        database_path = db_path()
        message = "Daily GitHub auto-update check failed: {0}".format(str(exc))
        try:
            append_upgrade_log(database_path, message)
        except Exception:
            pass
        print(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
