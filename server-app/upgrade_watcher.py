#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import tempfile
import time
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from upgrade_runtime import (
    append_upgrade_log,
    consume_upgrade_request,
    read_upgrade_status,
    write_upgrade_status,
)


APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.environ.get("IAM_MONITORING_DB_PATH", os.path.join(APP_ROOT, "state", "iam-monitoring.sqlite"))
DEFAULT_CONFIG_PATH = os.environ.get("IAM_MONITORING_CONFIG", "/etc/iam-monitoring.env")
DEFAULT_STATE_DIR = os.path.dirname(DEFAULT_DB_PATH)
DEFAULT_LOG_DIR = os.environ.get("IAM_MONITORING_LOG_DIR", "/var/log/iam-monitoring")
DEFAULT_SERVICE_USER = os.environ.get("IAM_MONITORING_SERVICE_USER", "iam-monitoring")


def read_version():
    version_path = os.path.join(APP_ROOT, "VERSION")
    try:
        with open(version_path, "r", encoding="utf-8") as handle:
            return handle.read().strip() or "unknown"
    except Exception:
        return "unknown"


def read_bundle_version(bundle_source_dir):
    version_path = os.path.join(bundle_source_dir, "VERSION")
    try:
        with open(version_path, "r", encoding="utf-8") as handle:
            return handle.read().strip() or "unknown"
    except Exception:
        return "unknown"


def db_path():
    return os.environ.get("IAM_MONITORING_DB_PATH", DEFAULT_DB_PATH)


def _effective_proxy_settings(request):
    request_proxy = dict((request or {}).get("proxySettings") or {})
    http_proxy = str(
        request_proxy.get("httpProxy")
        or os.environ.get("IAM_MONITORING_HTTP_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()
    https_proxy = str(
        request_proxy.get("httpsProxy")
        or os.environ.get("IAM_MONITORING_HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or ""
    ).strip()
    no_proxy = str(
        request_proxy.get("noProxy")
        or os.environ.get("IAM_MONITORING_NO_PROXY")
        or os.environ.get("no_proxy")
        or os.environ.get("NO_PROXY")
        or ""
    ).strip()
    return {
        "httpProxy": http_proxy,
        "httpsProxy": https_proxy,
        "noProxy": no_proxy,
    }


def _proxy_env(proxy_settings):
    env = os.environ.copy()
    if proxy_settings.get("httpProxy"):
        env["http_proxy"] = proxy_settings["httpProxy"]
        env["HTTP_PROXY"] = proxy_settings["httpProxy"]
    if proxy_settings.get("httpsProxy"):
        env["https_proxy"] = proxy_settings["httpsProxy"]
        env["HTTPS_PROXY"] = proxy_settings["httpsProxy"]
    if proxy_settings.get("noProxy"):
        env["no_proxy"] = proxy_settings["noProxy"]
        env["NO_PROXY"] = proxy_settings["noProxy"]
    return env


def _download_archive(archive_url, destination_path, proxy_settings):
    request = Request(archive_url, headers={"User-Agent": "iam-monitoring-ui-upgrade"})
    proxy_handler_settings = {}
    if proxy_settings.get("httpProxy"):
        proxy_handler_settings["http"] = proxy_settings["httpProxy"]
    if proxy_settings.get("httpsProxy"):
        proxy_handler_settings["https"] = proxy_settings["httpsProxy"]
    if proxy_handler_settings:
        opener = build_opener(ProxyHandler(proxy_handler_settings))
        response_handle = opener.open(request, timeout=20)
    else:
        response_handle = urlopen(request, timeout=20)
    with response_handle as response, open(destination_path, "wb") as handle:
        shutil.copyfileobj(response, handle)


def _resolve_bundle_source_dir(extract_root):
    direct_app = os.path.join(extract_root, "app.py")
    direct_requirements = os.path.join(extract_root, "requirements.txt")
    if os.path.isfile(direct_app) and os.path.isfile(direct_requirements):
        return extract_root

    for root, dirs, files in os.walk(extract_root):
        if "app.py" in files and "requirements.txt" in files:
            return root
    raise RuntimeError("Could not locate the IAM dashboard bundle inside the downloaded archive.")


def _extract_bundle_source_dir(archive_path, extract_root):
    shutil.unpack_archive(archive_path, extract_root)
    return _resolve_bundle_source_dir(extract_root)


def _run_upgrade_script(bundle_source_dir, proxy_settings, log_path):
    upgrade_script = os.path.join(bundle_source_dir, "upgrade.sh")
    env = _proxy_env(proxy_settings)
    env["IAM_MONITORING_CONFIG"] = DEFAULT_CONFIG_PATH
    env["IAM_MONITORING_UPGRADE_HANDOFF"] = "1"
    with open(log_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("---- upgrade.sh output ----\n")
        handle.flush()
        return subprocess.run(
            [
                "bash",
                upgrade_script,
                "--install-dir",
                APP_ROOT,
                "--config-file",
                DEFAULT_CONFIG_PATH,
                "--state-dir",
                DEFAULT_STATE_DIR,
                "--log-dir",
                DEFAULT_LOG_DIR,
                "--user",
                DEFAULT_SERVICE_USER,
            ],
            cwd=bundle_source_dir,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )


def process_upgrade_request():
    current_db_path = db_path()
    request = consume_upgrade_request(current_db_path)
    if not request:
        return False

    log_path = read_upgrade_status(current_db_path).get("logPath")
    proxy_settings = _effective_proxy_settings(request)
    archive_url = str(request.get("archiveUrl") or "").strip()
    request_id = str(request.get("requestId") or "").strip()
    branch = str(request.get("branch") or "main").strip() or "main"

    write_upgrade_status(
        current_db_path,
        {
            "status": "starting",
            "requestedAt": request.get("requestedAt") or "",
            "startedAt": request.get("requestedAt") or "",
            "finishedAt": "",
            "message": "GitHub upgrade helper picked up the request.",
            "repoUrl": request.get("repoUrl") or "",
            "archiveUrl": archive_url,
            "branch": branch,
            "currentVersion": request.get("currentVersion") or read_version(),
            "targetVersion": request.get("targetVersion") or "",
            "requestId": request_id,
            "lastError": "",
        },
    )
    append_upgrade_log(current_db_path, "Starting GitHub upgrade request {0} for branch {1}.".format(request_id, branch))

    temp_dir = tempfile.mkdtemp(prefix="iam-monitoring-upgrade-")
    archive_path = os.path.join(temp_dir, "github-upgrade.tar.gz")
    extract_dir = os.path.join(temp_dir, "bundle")
    try:
        write_upgrade_status(
            current_db_path,
            {
                "status": "downloading",
                "message": "Downloading the GitHub upgrade bundle.",
            },
        )
        append_upgrade_log(current_db_path, "Downloading {0}".format(archive_url))
        _download_archive(archive_url, archive_path, proxy_settings)
        append_upgrade_log(current_db_path, "Downloaded archive to {0}".format(archive_path))
        bundle_source_dir = _extract_bundle_source_dir(archive_path, extract_dir)
        append_upgrade_log(current_db_path, "Resolved bundle source directory {0}".format(bundle_source_dir))
        current_version = read_version()
        bundle_version = read_bundle_version(bundle_source_dir)
        write_upgrade_status(
            current_db_path,
            {
                "currentVersion": current_version,
                "targetVersion": bundle_version,
            },
        )
        append_upgrade_log(
            current_db_path,
            "Current installed version is {0}; downloaded bundle version is {1}.".format(
                current_version,
                bundle_version,
            ),
        )
        if bundle_version and current_version == bundle_version:
            append_upgrade_log(
                current_db_path,
                "Downloaded bundle already matches the running version. No upgrade was applied.",
            )
            write_upgrade_status(
                current_db_path,
                {
                    "status": "current",
                    "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "message": "You are already on the latest version.",
                    "currentVersion": current_version,
                    "targetVersion": bundle_version,
                    "lastError": "",
                },
            )
            return True

        write_upgrade_status(
            current_db_path,
            {
                "status": "applying",
                "message": "Running the bundle upgrade script. The dashboard service will restart during this step.",
            },
        )
        append_upgrade_log(current_db_path, "Running bundled upgrade.sh from {0}.".format(bundle_source_dir))
        result = _run_upgrade_script(bundle_source_dir, proxy_settings, log_path)
        if result.returncode != 0:
            raise RuntimeError("upgrade.sh exited with status {0}.".format(result.returncode))

        new_version = read_version()
        append_upgrade_log(current_db_path, "GitHub upgrade completed successfully on version {0}.".format(new_version))
        write_upgrade_status(
            current_db_path,
            {
                "status": "completed",
                "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "message": "GitHub upgrade completed successfully.",
                "currentVersion": new_version,
                "targetVersion": new_version,
                "lastError": "",
            },
        )
    except Exception as exc:
        append_upgrade_log(current_db_path, "GitHub upgrade failed: {0}".format(str(exc)))
        write_upgrade_status(
            current_db_path,
            {
                "status": "failed",
                "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "message": "GitHub upgrade failed.",
                "lastError": str(exc),
            },
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return True


def main():
    parser = argparse.ArgumentParser(description="Process a pending UI GitHub upgrade request for IAM Monitoring.")
    parser.parse_args()
    process_upgrade_request()


if __name__ == "__main__":
    main()
