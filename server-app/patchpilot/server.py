#!/usr/bin/env python3
"""Small static server for PatchPilot."""

import http.server
import json
import os
import errno
import re
import select
import shlex
import shutil
import signal
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

try:
    import pty
except ImportError:
    pty = None


ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("PATCHSCOPE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PATCHSCOPE_PORT", "4128"))
KNOWN_HOSTS = str(ROOT / "known_hosts")
SSH_CONNECT_TIMEOUT = int(os.environ.get("PATCHSCOPE_SSH_CONNECT_TIMEOUT", "90"))
SSH_TIMEOUT_GRACE = int(os.environ.get("PATCHSCOPE_SSH_TIMEOUT_GRACE", "30"))
SSH_QUICK_TIMEOUT = max(SSH_CONNECT_TIMEOUT + SSH_TIMEOUT_GRACE, 120)
SPB_INACTIVE_CHECK_TIMEOUT = int(os.environ.get("PATCHSCOPE_SPB_INACTIVE_CHECK_TIMEOUT", "3600"))
SPB_INACTIVE_CLEANUP_TIMEOUT = int(os.environ.get("PATCHSCOPE_SPB_INACTIVE_CLEANUP_TIMEOUT", "10800"))
SPB_INACTIVE_PROCESS_WAIT_TIMEOUT = int(os.environ.get("PATCHSCOPE_SPB_INACTIVE_PROCESS_WAIT_TIMEOUT", "900"))
SPB_PHASE_TIMEOUT = int(os.environ.get("PATCHSCOPE_SPB_PHASE_TIMEOUT", "14400"))
SPB_PHASE_TIMEOUT_RECOVERY_WAIT = int(os.environ.get("PATCHSCOPE_SPB_PHASE_TIMEOUT_RECOVERY_WAIT", "300"))
SPB_PHASE_STREAM_INITIAL_LINES = int(os.environ.get("PATCHSCOPE_SPB_PHASE_STREAM_INITIAL_LINES", "120"))
SPB_PHASE_HEARTBEAT_SECONDS = int(os.environ.get("PATCHSCOPE_SPB_PHASE_HEARTBEAT_SECONDS", "60"))
OPATCH_JOBS = {}
OPATCH_JOB_LOCK = threading.Lock()
SPB_JOBS = {}
SPB_JOB_LOCK = threading.Lock()
SPB_INACTIVE_JOBS = {}
SPB_INACTIVE_JOB_LOCK = threading.Lock()
OIG_JOBS = {}
OIG_JOB_LOCK = threading.Lock()
BACKUP_JOBS = {}
BACKUP_JOB_LOCK = threading.Lock()
PATCH_JOBS = {}
PATCH_JOB_LOCK = threading.Lock()
ROLLBACK_JOBS = {}
ROLLBACK_JOB_LOCK = threading.Lock()
ACTIVE_TARGET_JOBS = {}
ACTIVE_TARGET_JOB_LOCK = threading.Lock()
MAX_JOB_OUTPUT = 512 * 1024
DEFAULT_OPATCH_HEAP_OPTIONS = "-Xmx3072m"


class JobCancelledError(RuntimeError):
    pass


def ssh_timeout(seconds):
    return max(int(seconds), SSH_QUICK_TIMEOUT)


def sanitize_opatch_heap_options(value):
    text = str(value or DEFAULT_OPATCH_HEAP_OPTIONS).strip()
    if not text:
        return DEFAULT_OPATCH_HEAP_OPTIONS
    if any(ch in text for ch in "\r\n\0"):
        raise ValueError("OPatch heap options must be a single line.")
    try:
        parts = shlex.split(text)
    except ValueError as error:
        raise ValueError("OPatch heap options are not valid shell-style arguments: %s" % error)
    if not parts:
        return DEFAULT_OPATCH_HEAP_OPTIONS
    for part in parts:
        if not part.startswith("-"):
            raise ValueError("OPatch heap option must start with '-': %s" % part)
    return " ".join(parts)


REMOTE_OPATCH_HEAP_HELPER = r'''
PATCHPILOT_DEFAULT_OPATCH_HEAP_OPTIONS = "-Xmx3072m"


def _patchpilot_xmx_mb(options):
    best = None
    for match in re.finditer(r"(?:^|\s)-Xmx([0-9]+)([kKmMgG]?)", str(options or "")):
        value = int(match.group(1))
        unit = match.group(2).lower()
        if unit == "g":
            value *= 1024
        elif unit == "k":
            value = max(1, value // 1024)
        if best is None or value > best:
            best = value
    return best


def configure_opatch_heap(requested):
    requested = str(requested or PATCHPILOT_DEFAULT_OPATCH_HEAP_OPTIONS).strip() or PATCHPILOT_DEFAULT_OPATCH_HEAP_OPTIONS
    previous = os.environ.get("OPATCH_JRE_MEMORY_OPTIONS", "").strip()
    requested_mb = _patchpilot_xmx_mb(requested)
    previous_mb = _patchpilot_xmx_mb(previous)
    action = "set"
    effective = requested
    if previous:
        if previous_mb is not None and requested_mb is not None and previous_mb >= requested_mb:
            effective = previous
            action = "preserved"
        elif requested_mb is None:
            effective = previous
            action = "preserved"
        else:
            action = "raised"
    os.environ["OPATCH_JRE_MEMORY_OPTIONS"] = effective
    return {
        "requested": requested,
        "previous": previous,
        "effective": effective,
        "action": action,
    }
'''


REMOTE_PYTHON_COMPAT = r'''from __future__ import print_function
import os
import re
import subprocess
import sys
import time

try:
    import io
    open = io.open
except Exception:
    pass

try:
    PermissionError
except NameError:
    PermissionError = OSError

if not hasattr(subprocess, "TimeoutExpired"):
    class _PatchPilotTimeoutExpired(Exception):
        def __init__(self, cmd, timeout, output=None, stdout=None, stderr=None):
            Exception.__init__(self, "Command timed out after %ss: %s" % (timeout, cmd))
            self.cmd = cmd
            self.timeout = timeout
            self.output = output
            self.stdout = stdout if stdout is not None else output
            self.stderr = stderr
    subprocess.TimeoutExpired = _PatchPilotTimeoutExpired

_patchpilot_check_output = subprocess.check_output
def _patchpilot_check_output_wrapper(*popenargs, **kwargs):
    kwargs.pop("timeout", None)
    return _patchpilot_check_output(*popenargs, **kwargs)
subprocess.check_output = _patchpilot_check_output_wrapper

_patchpilot_check_call = subprocess.check_call
def _patchpilot_check_call_wrapper(*popenargs, **kwargs):
    kwargs.pop("timeout", None)
    return _patchpilot_check_call(*popenargs, **kwargs)
subprocess.check_call = _patchpilot_check_call_wrapper

if not hasattr(subprocess, "run"):
    class _PatchPilotCompletedProcess(object):
        def __init__(self, args, returncode, stdout=None, stderr=None):
            self.args = args
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _patchpilot_run(*popenargs, **kwargs):
        timeout = kwargs.pop("timeout", None)
        check = kwargs.pop("check", False)
        command = popenargs[0] if popenargs else kwargs.get("args")
        proc = subprocess.Popen(*popenargs, **kwargs)
        if timeout is None:
            stdout, stderr = proc.communicate()
        else:
            deadline = time.time() + timeout
            while proc.poll() is None:
                if time.time() > deadline:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    stdout, stderr = proc.communicate()
                    raise subprocess.TimeoutExpired(command, timeout, output=stdout, stdout=stdout, stderr=stderr)
                time.sleep(0.1)
            stdout, stderr = proc.communicate()
        result = _PatchPilotCompletedProcess(command, proc.returncode, stdout, stderr)
        if check and proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, command, output=stdout)
        return result
    subprocess.run = _patchpilot_run

_patchpilot_makedirs = os.makedirs
def _patchpilot_makedirs_wrapper(name, mode=0o777, exist_ok=False):
    try:
        return _patchpilot_makedirs(name, mode)
    except OSError:
        if exist_ok and os.path.isdir(name):
            return
        raise
os.makedirs = _patchpilot_makedirs_wrapper

if not hasattr(re, "fullmatch"):
    def _patchpilot_fullmatch(pattern, string, flags=0):
        return re.match(r"(?:%s)\Z" % pattern, string or "", flags)
    re.fullmatch = _patchpilot_fullmatch

if not hasattr(os.path, "commonpath"):
    def _patchpilot_commonpath(paths):
        paths = [os.path.abspath(path) for path in paths if path]
        if not paths:
            return ""
        common = os.path.commonprefix(paths)
        while common and not all(path == common or path.startswith(common.rstrip(os.sep) + os.sep) for path in paths):
            parent = os.path.dirname(common.rstrip(os.sep))
            if parent == common:
                break
            common = parent
        return common or os.sep
    os.path.commonpath = _patchpilot_commonpath

try:
    import shlex
    shlex.quote
except AttributeError:
    def _patchpilot_shell_quote(value):
        value = str(value)
        if not value:
            return "''"
        if re.search(r"[^A-Za-z0-9_@%+=:,./-]", value):
            return "'" + value.replace("'", "'\"'\"'") + "'"
        return value
    shlex.quote = _patchpilot_shell_quote
'''


def remote_python_command(script):
    return """PATCHPILOT_PY=""
if command -v python3 >/dev/null 2>&1; then
  PATCHPILOT_PY="$(command -v python3)"
elif [ -x /usr/libexec/platform-python ]; then
  PATCHPILOT_PY="/usr/libexec/platform-python"
elif command -v python >/dev/null 2>&1; then
  PATCHPILOT_PY="$(command -v python)"
fi
if [ -z "$PATCHPILOT_PY" ]; then
  echo "PatchPilot requires Python on the SSH target for this operation. Install python3 or expose python/platform-python." >&2
  exit 127
fi
PATCHPILOT_PY_VERSION="$("$PATCHPILOT_PY" -c 'import sys; print("%%s.%%s" %% sys.version_info[:2])' 2>/dev/null || true)"
"$PATCHPILOT_PY" - <<'PY'
%s
%s
PY
""" % (REMOTE_PYTHON_COMPAT, script)


DISCOVER_SCRIPT = r'''
import json
import os
import re
import socket
import zipfile
import subprocess


def walk_limited(base, max_depth):
    base = os.path.abspath(base)
    if not os.path.isdir(base):
        return
    base_depth = base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        dirs[:] = [name for name in dirs if name not in (".git", "tmp", "cache", "logs", "stage", "backups", ".patch_storage")]
        if depth >= max_depth:
            dirs[:] = []
        yield root


def unique(items):
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def canonical_path(path):
    if not path:
        return ""
    try:
        return os.path.realpath(os.path.abspath(path)).rstrip(os.sep)
    except Exception:
        return os.path.abspath(path).rstrip(os.sep)


def unique_paths(paths):
    seen = set()
    result = []
    for path in paths:
        if not path:
            continue
        key = canonical_path(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(os.path.abspath(path).rstrip(os.sep))
    return result


def is_patch_artifact(path):
    low = path.lower()
    parts = [part for part in low.split(os.sep) if part]
    if any(part in ("spb_logs", "precheck", "patch", "patches", "stage", "backup", "backups", "tmp") for part in parts):
        return True
    if any(part.startswith("orainstall") for part in parts):
        return True
    blocked = [
        "/spb_logs/",
        "/precheck/",
        "/orainstall",
        "/patch/",
        "/patches/",
        "/stage/",
        "/backup",
        "/backups/",
        "/tmp/",
        "/.patch_storage/",
    ]
    return any(token in low for token in blocked)


def looks_like_oracle_home(home):
    if is_patch_artifact(home):
        return False
    if not os.path.isfile(os.path.join(home, "OPatch", "opatch")):
        return False
    signals = [
        os.path.isfile(os.path.join(home, "domain-registry.xml")),
        os.path.isfile(os.path.join(home, "oui", "bin", "runInstaller")),
        os.path.isdir(os.path.join(home, "oracle_common")),
        os.path.isdir(os.path.join(home, "wlserver")),
        os.path.isdir(os.path.join(home, "idm")),
        os.path.isdir(os.path.join(home, "oud")),
        os.path.isdir(os.path.join(home, "ohs")),
        os.path.basename(home).lower().startswith("dbhome"),
    ]
    return any(signals)


def opatch_version(home):
    opatch = os.path.join(home, "OPatch", "opatch")
    if not os.path.exists(opatch):
        return "not found"
    try:
        output = subprocess.check_output([opatch, "version"], stderr=subprocess.STDOUT, timeout=12).decode("utf-8", "replace")
    except Exception as error:
        return "unavailable: %s" % error
    match = re.search(r"OPatch Version:\s*([0-9.]+)", output)
    if match:
        return match.group(1)
    version = re.search(r"([0-9]+(?:\.[0-9]+){2,})", output)
    return version.group(1) if version else output.strip().splitlines()[-1][:80]


def domains_from_registry(home):
    registry = os.path.join(home, "domain-registry.xml")
    if not os.path.isfile(registry):
        return []
    try:
        text = open(registry, "r").read()
    except Exception:
        return []
    domains = []
    for match in re.finditer(r'\blocation\s*=\s*["\']([^"\']+)["\']', text):
        domains.append(match.group(1))
    for match in re.finditer(r'<domain[^>]*>\s*([^<]+)\s*</domain>', text, re.I):
        domains.append(match.group(1).strip())
    return [path for path in unique(domains) if path and os.path.isdir(path)]


def detect_products(home):
    low = home.lower()
    base = os.path.basename(home).lower()
    products = []
    has_oig = (
        "oig" in low or
        "oim" in low or
        os.path.isfile(os.path.join(home, "idm", "server", "bin", "patch_oim_wls.sh")) or
        os.path.isfile(os.path.join(home, "idm", "server", "bin", "patch_oim_wls.profile")) or
        os.path.isdir(os.path.join(home, "oim"))
    )
    has_oam = (
        "oam" in low or
        os.path.isdir(os.path.join(home, "oam")) or
        os.path.isdir(os.path.join(home, "iam", "oam"))
    )
    if base.startswith("dbhome") or "/database" in low:
        products.append("Database")
    if "oud" in low:
        products.append("OUD")
    if "oid" in low:
        products.append("OID")
    if has_oig:
        products.append("OIG")
    if has_oam:
        products.append("OAM")
    if "soa" in low:
        products.append("SOA")
    if "ohs" in low or "webtier" in low:
        products.append("OHS")
    if os.path.isdir(os.path.join(home, "wlserver")):
        products.append("WebLogic")
    return unique(products) or ["Oracle Home"]


def find_domains():
    bases = [
        "/refresh/home/Domains",
        "/u01/app/oracle/user_projects/domains",
        "/u01/oracle/user_projects/domains",
        "/opt/oracle/user_projects/domains",
    ]
    domains = []
    for base in bases:
        for root in walk_limited(base, 3) or []:
            if os.path.isfile(os.path.join(root, "config", "config.xml")) or os.path.isfile(os.path.join(root, "bin", "startWebLogic.sh")):
                domains.append(root)
    return unique(domains)


def find_oud_instances():
    bases = ["/refresh/home/Instances", "/u01", "/u02", "/opt/oracle"]
    instances = []
    for base in bases:
        for root in walk_limited(base, 6) or []:
            if os.path.isfile(os.path.join(root, "bin", "status")) and os.path.isfile(os.path.join(root, "bin", "start-ds")):
                instances.append(root)
    return unique(instances)


def domain_for(home, domains):
    registry_domains = domains_from_registry(home)
    if registry_domains:
        return registry_domains[0]
    if "Database" in detect_products(home):
        return ""
    low = home.lower()
    for domain in domains:
        name = os.path.basename(domain).lower()
        if "oud" in low and "oud" in name:
            return domain
        if "oam" in low and "oam" in name:
            return domain
        if ("oig" in low or "oim" in low) and ("oig" in name or "oim" in name):
            return domain
        if "ohs" in low and ("web" in name or "ohs" in name):
            return domain
    return ""


def path_in_command(command, path):
    if not path:
        return False
    path = os.path.normpath(path).rstrip(os.sep)
    if not path:
        return False
    for match in re.finditer(re.escape(path), command):
        end = match.end()
        if end == len(command) or command[end] in "/ \t:=,;\"')":
            return True
    return False


def is_patching_utility_process(command):
    lower = command.lower()
    utility_tokens = [
        "/opatch/",
        " oracle/opatch/opatch ",
        " oracle.opatch.",
        "-dopatch.",
        "/cfgtoollogs/opatch",
        "orainstaller.jar",
        "installer-launch.jar",
        "oui/modules",
        "patchpilot",
        "patchscope",
    ]
    return any(token in lower for token in utility_tokens)


def service_name_for(command, home="", domain="", instance=""):
    match = re.search(r"-Dweblogic\.Name=([^\s]+)", command)
    if match:
        return match.group(1)
    if "startweblogic.sh" in command.lower():
        return "AdminServer"
    if "NodeManager" in command or "weblogic.nodemanager" in command.lower():
        return "NodeManager"
    if "org.apache.derby.drda.NetworkServerControl" in command:
        return "Derby Network Server"
    if re.search(r"/oidmon(\s|$)", command):
        return "OID Monitor"
    if re.search(r"/oidldapd(\s|$)", command):
        instance_match = re.search(r"\b(?:instance|inst)=([^\s]+)", command)
        suffix = " instance %s" % instance_match.group(1) if instance_match else ""
        return "OID LDAP Server%s" % suffix
    if re.search(r"/oidrepld(\s|$)", command):
        return "OID Replication Server"
    ohs_match = re.search(r"/OHS/([^/\s]+)/", command)
    if ohs_match:
        return "OHS %s" % ohs_match.group(1)
    if "DirectoryServer" in command or "org.opends" in command or "start-ds" in command:
        return "OUD %s" % (os.path.basename(instance.rstrip(os.sep)) if instance else "instance")
    if "ohs" in command.lower() or "httpd" in command.lower():
        return "OHS"
    if "dbhome" in home.lower():
        return "Database process under %s" % os.path.basename(home.rstrip(os.sep))
    if "java" in command.lower():
        scope = os.path.basename(domain.rstrip(os.sep)) if domain else os.path.basename(home.rstrip(os.sep))
        return "Java process under %s" % scope
    return "Oracle process under %s" % os.path.basename(home.rstrip(os.sep))


def services_for(paths):
    try:
        output = subprocess.check_output(["ps", "-eo", "pid,args"], stderr=subprocess.STDOUT, timeout=8).decode("utf-8", "replace")
    except Exception:
        return []
    services = []
    home = paths[0] if len(paths) > 0 else ""
    domain = paths[1] if len(paths) > 1 else ""
    instance = paths[2] if len(paths) > 2 else ""
    for line in output.splitlines():
        if is_patching_utility_process(line):
            continue
        if not any(path_in_command(line, path) for path in paths):
            continue
        services.append(service_name_for(line, home, domain, instance))
    return unique(services)


oracle_homes = []
home_bases = unique([
    os.environ.get("ORACLE_HOME", ""),
    "/refresh/home/Oracle",
    "/refresh/home",
    "/u01",
    "/u02",
    "/opt/oracle",
])
for base in home_bases:
    for root in walk_limited(base, 6) or []:
        if looks_like_oracle_home(root):
            oracle_homes.append(root)
oracle_homes = unique_paths(oracle_homes)
domains = find_domains()
oud_instances = find_oud_instances()

homes = []
for index, home in enumerate(oracle_homes):
    canonical_home = canonical_path(home)
    command_home = canonical_home if canonical_home and os.path.isdir(canonical_home) else home
    products = detect_products(command_home)
    domain = domain_for(command_home, domains) or domain_for(home, domains)
    instance = oud_instances[0] if "OUD" in products and oud_instances else ""
    services = services_for(unique([command_home, home, domain, instance]))
    label = "%s Home" % " / ".join(products[:2])
    homes.append({
        "id": "home-%s" % (index + 1),
        "label": label,
        "host": socket.getfqdn(),
        "product": ", ".join(products),
        "oracleHome": command_home,
        "discoveredHome": home if home != command_home else "",
        "canonicalHome": canonical_home,
        "domainHome": domain,
        "instanceHome": instance,
        "opatchVersion": opatch_version(command_home),
        "services": services,
    })

print("__PATCHSCOPE_JSON_START__")
print(json.dumps({"host": socket.getfqdn(), "homes": homes}))
print("__PATCHSCOPE_JSON_END__")
'''


def build_readme_script(patch_path):
    return r'''
import json
import os
import re

try:
    import html
except ImportError:
    import HTMLParser
    class _PatchPilotHtml(object):
        def unescape(self, value):
            return HTMLParser.HTMLParser().unescape(value)
    html = _PatchPilotHtml()

patch_dir = %s
names = []
for root, dirs, files in os.walk(patch_dir):
    depth = os.path.relpath(root, patch_dir).count(os.sep)
    if depth > 2:
        dirs[:] = []
        continue
    dirs[:] = [name for name in dirs if name.lower() not in ("backup", "backups", ".git", "__macosx")]
    for name in files:
        low = name.lower()
        if low.startswith("readme") or low.endswith(".readme") or low in ("patch_readme.txt", "patch_readme.html"):
            names.append(os.path.join(root, name))

payload = {"patchPath": patch_dir, "readmePath": "", "text": "", "candidates": names[:20]}
if names:
    def read_text(path):
        size = os.path.getsize(path)
        if size > 2 * 1024 * 1024:
            raise RuntimeError("README is larger than 2 MB: %%s" %% path)
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8", "replace")

    def html_to_text(value):
        value = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
        value = re.sub(r"<\s*(br|p|div|li|tr|h[1-6]|section|table|pre|blockquote)\b[^>]*>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", " ", value)
        value = html.unescape(value)
        lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line)

    def rank(path):
        low = os.path.basename(path).lower()
        if low == "readme.txt":
            kind = 0
        elif low in ("readme.html", "readme.htm"):
            kind = 1
        elif low.startswith("readme"):
            kind = 2
        else:
            kind = 3
        return (kind, len(path))

    chosen = sorted(names, key=rank)[0]
    text = read_text(chosen)
    html_candidates = [path for path in names if os.path.basename(path).lower() in ("readme.html", "readme.htm") or path.lower().endswith((".html", ".htm"))]
    if re.search(r"\brefer\s+to\s+readme\.html\b", text, re.I) and html_candidates:
        chosen = sorted(html_candidates, key=lambda path: (0 if os.path.basename(path).lower() == "readme.html" else 1, len(path)))[0]
        text = read_text(chosen)
    if chosen.lower().endswith((".html", ".htm")):
        text = html_to_text(text)
    payload["readmePath"] = chosen
    payload["text"] = text

print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
''' % json.dumps(patch_path)


def build_verify_shutdown_script(target):
    script = r'''
import json
import os
import re
import subprocess

target = __PATCHSCOPE_TARGET_JSON__
mode = str(target.get("mode") or "shutdown").strip().lower()
services_up_mode = mode in ("servicesup", "services_up", "services-up")
expected_services = target.get("expectedServices") or target.get("services") or []
if not isinstance(expected_services, list):
    expected_services = []
expected_products = [target.get("product") or "", target.get("label") or ""]


def clean(value):
    value = str(value or "").strip()
    if value.lower() in ("not discovered", "not applicable", "none", "null"):
        return ""
    return value


paths = [
    ("ORACLE_HOME", clean(target.get("oracleHome"))),
    ("DOMAIN_HOME", clean(target.get("domainHome"))),
    ("INSTANCE_HOME", clean(target.get("instanceHome"))),
]
paths = [(scope, os.path.normpath(path)) for scope, path in paths if path]


def path_in_command(command, path):
    if not path:
        return False
    normalized = os.path.normpath(path).rstrip(os.sep)
    if not normalized:
        return False
    for match in re.finditer(re.escape(normalized), command):
        end = match.end()
        if end == len(command) or command[end] in "/ \t:=,;\"')":
            return True
    return False


def matched_scopes(command):
    return [scope for scope, path in paths if path_in_command(command, path)]


def normalize_service(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def generic_service_name(value):
    normalized = normalize_service(value)
    return (
        normalized.startswith("java process under") or
        normalized.startswith("oracle process under") or
        normalized.startswith("database process under")
    )


expected_service_names = [
    normalize_service(service) for service in expected_services
    if normalize_service(service) and not generic_service_name(service)
]
expected_product_text = normalize_service(" ".join([str(item or "") for item in expected_products]))


def service_matches_expected(item):
    if not expected_service_names:
        return False
    service = normalize_service(item.get("service"))
    category = normalize_service(item.get("category"))
    candidates = [service, category, normalize_service("%s %s" % (category, service))]
    for expected in expected_service_names:
        for candidate in candidates:
            if expected and candidate and (expected in candidate or candidate in expected):
                return True
    return False


def product_matches_expected(item):
    if not expected_product_text:
        return False
    service = normalize_service(item.get("service"))
    category = normalize_service(item.get("category"))
    combined = normalize_service("%s %s" % (category, service))
    if ("oid" in expected_product_text or "internet directory" in expected_product_text) and "oracle internet directory" in category:
        return True
    if ("oud" in expected_product_text or "unified directory" in expected_product_text) and "oracle unified directory" in category:
        return True
    if ("ohs" in expected_product_text or "http server" in expected_product_text) and "oracle http server" in category:
        return True
    if ("weblogic" in expected_product_text or "oam" in expected_product_text or "oig" in expected_product_text or "oim" in expected_product_text or "soa" in expected_product_text) and category in ("weblogic server", "node manager"):
        return True
    if "derby" in expected_product_text and "derby" in combined:
        return True
    return False


def is_patching_utility_process(command):
    lower = command.lower()
    utility_tokens = [
        "/opatch/",
        " oracle/opatch/opatch ",
        " oracle.opatch.",
        "-dopatch.",
        "/cfgtoollogs/opatch",
        "orainstaller.jar",
        "installer-launch.jar",
        "oui/modules",
        "patchpilot",
        "patchscope",
    ]
    return any(token in lower for token in utility_tokens)


def basename(scope_name):
    for scope, path in paths:
        if scope == scope_name and path:
            return os.path.basename(path.rstrip(os.sep))
    return ""


def option(command, pattern):
    match = re.search(pattern, command)
    if not match:
        return ""
    return match.group(1).strip("\"'")


def infer_service(command, scopes):
    lower = command.lower()
    weblogic_name = option(command, r"-Dweblogic\.Name=([^\s]+)")
    if weblogic_name:
        if weblogic_name.lower() == "adminserver":
            return {
                "service": "AdminServer",
                "category": "WebLogic Server",
                "shutdownHint": "$DOMAIN_HOME/bin/stopWebLogic.sh",
            }
        return {
            "service": weblogic_name,
            "category": "WebLogic Server",
            "shutdownHint": "$DOMAIN_HOME/bin/stopManagedWebLogic.sh %s" % weblogic_name,
        }
    if "startweblogic.sh" in lower:
        return {
            "service": "AdminServer",
            "category": "WebLogic Server",
            "shutdownHint": "$DOMAIN_HOME/bin/stopWebLogic.sh",
        }
    if "weblogic.nodemanager" in lower or "nodemanager" in lower:
        domain = basename("DOMAIN_HOME")
        suffix = " (%s)" % domain if domain else ""
        return {
            "service": "NodeManager%s" % suffix,
            "category": "Node Manager",
            "shutdownHint": "$DOMAIN_HOME/bin/stopNodeManager.sh",
        }
    if "org.apache.derby.drda.networkservercontrol" in lower:
        return {
            "service": "Derby Network Server",
            "category": "Embedded Derby",
            "shutdownHint": "Stop Derby from the selected DOMAIN_HOME before patching.",
        }
    if re.search(r"/oidmon(\s|$)", command):
        return {
            "service": "OID Monitor",
            "category": "Oracle Internet Directory",
            "shutdownHint": "$ORACLE_HOME/bin/opmnctl stopall or stop the OID component from the domain.",
        }
    if re.search(r"/oidldapd(\s|$)", command):
        instance_match = re.search(r"\b(?:instance|inst)=([^\s]+)", command)
        suffix = " instance %s" % instance_match.group(1) if instance_match else ""
        return {
            "service": "OID LDAP Server%s" % suffix,
            "category": "Oracle Internet Directory",
            "shutdownHint": "$ORACLE_HOME/bin/opmnctl stopall or stop the OID component from the domain.",
        }
    if re.search(r"/oidrepld(\s|$)", command):
        return {
            "service": "OID Replication Server",
            "category": "Oracle Internet Directory",
            "shutdownHint": "$ORACLE_HOME/bin/opmnctl stopall or stop the OID component from the domain.",
        }
    ohs_component = (
        option(command, r"/OHS/([^/\s]+)/") or
        option(command, r"(?:componentName|COMPONENT_NAME)=([^\s]+)")
    )
    if "ohs" in lower or "httpd" in lower or ohs_component:
        service = "OHS %s" % ohs_component if ohs_component else "OHS"
        return {
            "service": service,
            "category": "Oracle HTTP Server",
            "shutdownHint": "$DOMAIN_HOME/bin/stopComponent.sh %s" % (ohs_component or "<ohs_component>"),
        }
    if "directoryserver" in command or "org.opends" in lower or "start-ds" in lower:
        instance = basename("INSTANCE_HOME") or basename("ORACLE_HOME") or "instance"
        return {
            "service": "OUD %s" % instance,
            "category": "Oracle Unified Directory",
            "shutdownHint": "$INSTANCE_HOME/bin/stop-ds",
        }
    oracle_home = basename("ORACLE_HOME")
    if "dbhome" in oracle_home.lower() or "/dbhome" in command.lower():
        return {
            "service": "Database process under %s" % oracle_home,
            "category": "Database",
            "shutdownHint": "Stop the database/listener from this ORACLE_HOME before patching.",
        }
    if "java" in lower:
        scope = basename("DOMAIN_HOME") or oracle_home or "selected home"
        return {
            "service": "Java process under %s" % scope,
            "category": "Java",
            "shutdownHint": "Use the product stop script for the selected domain/home.",
        }
    scope = basename("DOMAIN_HOME") or oracle_home or "selected home"
    return {
        "service": "Oracle process under %s" % scope,
        "category": "Oracle Process",
        "shutdownHint": "Use the product stop script for the selected domain/home.",
    }


current_pid = os.getpid()
parent_pid = os.getppid()
running = {}

try:
    output = subprocess.check_output(["ps", "-eo", "pid=,ppid=,args="], stderr=subprocess.STDOUT, timeout=10).decode("utf-8", "replace")
except Exception as error:
    payload = {"status": "error", "error": str(error), "pathsChecked": paths, "running": []}
else:
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if pid in (current_pid, parent_pid) or ppid in (current_pid, parent_pid):
            continue
        command = parts[2]
        if "ps -eo" in command or "PATCHSCOPE_JSON" in command:
            continue
        if is_patching_utility_process(command):
            continue
        scopes = matched_scopes(command)
        item = None
        if not scopes and services_up_mode:
            candidate = infer_service(command, [])
            if service_matches_expected(candidate) or product_matches_expected(candidate):
                item = candidate
                scopes = ["Discovered service name"]
        if not scopes:
            continue
        if item is None:
            item = infer_service(command, scopes)
        key = "%s|%s" % (item["service"], item["category"])
        if key not in running:
            running[key] = {
                "key": re.sub(r"[^A-Za-z0-9_.:-]+", "-", key).strip("-"),
                "service": item["service"],
                "category": item["category"],
                "matchedScopes": [],
                "processCount": 0,
                "pids": [],
                "shutdownHint": item["shutdownHint"],
            }
        for scope in scopes:
            if scope not in running[key]["matchedScopes"]:
                running[key]["matchedScopes"].append(scope)
        running[key]["processCount"] += 1
        running[key]["pids"].append(pid)

    services = sorted(running.values(), key=lambda item: (item["category"], item["service"]))
    payload = {
        "status": "running" if services else "stopped",
        "pathsChecked": [{"scope": scope, "path": path} for scope, path in paths],
        "running": services,
    }

print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_TARGET_JSON__", json.dumps(target))


def build_spb_prepare_script(patch_path, log_dir, change_ref):
    payload = {
        "patchPath": patch_path,
        "logDir": log_dir,
        "changeRef": change_ref,
    }
    script = r'''
import json
import os
import re
import socket
import zipfile

request = __PATCHSCOPE_SPB_JSON__
patch_dir = os.path.abspath(str(request.get("patchPath") or ""))
change_ref = str(request.get("changeRef") or "spb-run")
safe_ref = re.sub(r"[^A-Za-z0-9_.-]+", "_", change_ref).strip("_") or "spb-run"
log_dir = str(request.get("logDir") or "").strip()
if not log_dir:
    log_dir = os.path.join(patch_dir, "spbat_logs")
log_dir = os.path.abspath(log_dir)

if not patch_dir or not os.path.isdir(patch_dir):
    raise RuntimeError("SPB download directory does not exist: %s" % patch_dir)

spbat_dir = os.path.join(patch_dir, "tools", "spbat", "generic", "SPBAT")
spbat_sh = os.path.join(spbat_dir, "spbat.sh")
bundle_props = os.path.join(patch_dir, "spbat-bundle.properties")
version_file = os.path.join(spbat_dir, "version.txt")

if not os.path.isfile(spbat_sh):
    raise RuntimeError("SPBAT shell script was not found under %s." % spbat_dir)

os.makedirs(log_dir, exist_ok=True)
if not os.access(log_dir, os.W_OK):
    raise RuntimeError("SPBAT log directory is not writable: %s" % log_dir)

version = ""
if os.path.isfile(version_file):
    try:
        version = open(version_file, "r", encoding="utf-8", errors="replace").read().strip().splitlines()[0][:120]
    except Exception:
        version = ""

opatch_candidates = []
opatch_readmes = []


def inspect_opatch_file(path, root_low):
    low = os.path.basename(path).lower()
    if low == "opatch_generic.jar":
        opatch_candidates.insert(0, path)
    elif low.endswith(".zip") and ("opatch" in low or "p6880880" in low or "/tools/opatch/generic" in root_low):
        opatch_candidates.append(path)
        try:
            with zipfile.ZipFile(path) as archive:
                members = [member for member in archive.namelist() if os.path.basename(member).lower().startswith("readme")]
            for member in members[:5]:
                opatch_readmes.append("%s::%s" % (path, member))
        except Exception:
            pass


search_roots = [
    (os.path.join(patch_dir, "tools", "opatch", "generic"), 4),
    (os.path.join(patch_dir, "upgrade_installers"), 3),
    (os.path.join(patch_dir, "tools"), 2),
]
for base, max_depth in search_roots:
    if not os.path.isdir(base):
        continue
    base_depth = base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = [name for name in dirs if name.lower() not in ("logs", "spbat-logs", "reports")]
        for name in files:
            low = name.lower()
            root_low = root.lower()
            path = os.path.join(root, name)
            in_opatch_area = "opatch" in root_low or "/tools/opatch/generic" in root_low
            if in_opatch_area and low.startswith("readme"):
                opatch_readmes.append(path)
            inspect_opatch_file(path, root_low)
        if len(opatch_candidates) >= 20:
            break
    if len(opatch_candidates) >= 20:
        break

try:
    for name in os.listdir(patch_dir):
        path = os.path.join(patch_dir, name)
        if os.path.isfile(path):
            inspect_opatch_file(path, patch_dir.lower())
except Exception:
    pass

payload = {
    "host": socket.getfqdn(),
    "patchPath": patch_dir,
    "spbatDir": spbat_dir,
    "spbatScript": spbat_sh,
    "bundleProperties": bundle_props if os.path.isfile(bundle_props) else "",
    "version": version,
    "logDir": log_dir,
    "opatchCandidates": list(dict.fromkeys(opatch_candidates))[:20],
    "opatchReadmes": list(dict.fromkeys(opatch_readmes))[:20],
}

print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_SPB_JSON__", json.dumps(payload))


def build_spb_report_script(log_dir, preferred_log="", phase=""):
    return r'''
import json
import os
import re

try:
    import html as html_module
except ImportError:
    import HTMLParser
    class _PatchPilotHtml(object):
        def unescape(self, value):
            return HTMLParser.HTMLParser().unescape(value)
    html_module = _PatchPilotHtml()

log_dir = %s
preferred_log = %s
requested_phase = %s
log_dir = os.path.abspath(str(log_dir or ""))
preferred_log = os.path.abspath(str(preferred_log or "")) if preferred_log else ""
requested_phase = str(requested_phase or "").strip().lower()
if requested_phase not in ("", "prestop", "downtime", "poststart"):
    requested_phase = ""
if not log_dir or not os.path.isdir(log_dir):
    raise RuntimeError("SPBAT log directory does not exist: %%s" %% log_dir)


def inside_log_dir(path):
    if not path:
        return False
    normalized = os.path.abspath(path)
    base = log_dir.rstrip(os.sep)
    return normalized == base or normalized.startswith(base + os.sep)


def read_tail(path, limit=256 * 1024):
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read().decode("utf-8", "replace")


def html_to_plain(value):
    plain = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(value or ""), flags=re.I | re.S)
    plain = re.sub(r"<[^>]+>", "\n", plain)
    plain = html_module.unescape(plain)
    return plain


def phase_from_text(value):
    compact = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    if "poststart" in compact:
        return "poststart"
    if "prestop" in compact:
        return "prestop"
    if "downtime" in compact:
        return "downtime"
    return ""


def artifact_phase(path, text=""):
    return phase_from_text("%%s\n%%s" %% (path or "", text or ""))


def phase_matches(path, text=""):
    if not requested_phase:
        return True
    return artifact_phase(path, text) == requested_phase


def text_summary(value):
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(value or "").splitlines()]
    lines = [line for line in lines if line]
    important = []
    for index, line in enumerate(lines):
        if re.search(r"\b(fail|failed|failure|error|exception|traceback|not found|not met|return code|status|result)\b", line, re.I):
            start = max(0, index - 1)
            end = min(len(lines), index + 2)
            important.extend(lines[start:end])
    deduped = []
    seen = set()
    for line in important:
        if line not in seen:
            seen.add(line)
            deduped.append(line)
    return deduped[-20:]


def text_status(value):
    text = str(value or "")
    if re.search(r"\b(fail|failed|failure|error|exception|traceback|return code)\b", text, re.I):
        return "failed"
    if re.search(r"\b(status|result)\b.{0,80}\b(success|succeeded|successful|passed|completed)\b|\bOPatch succeeded\b|\bSPBAT .* complete\b", text, re.I | re.S):
        return "succeeded"
    return ""


def latest_log_candidates():
    candidates = []
    if preferred_log and inside_log_dir(preferred_log) and os.path.isfile(preferred_log):
        try:
            text = read_tail(preferred_log, 64 * 1024)
            if phase_matches(preferred_log, text):
                candidates.append((os.path.getmtime(preferred_log), os.path.getsize(preferred_log), preferred_log, True, artifact_phase(preferred_log, text)))
        except Exception:
            pass
    for root, dirs, files in os.walk(log_dir):
        depth = root.rstrip(os.sep).count(os.sep) - log_dir.rstrip(os.sep).count(os.sep)
        if depth >= 8:
            dirs[:] = []
        for name in files:
            low = name.lower()
            if not low.endswith((".log", ".out", ".txt")):
                continue
            path = os.path.join(root, name)
            try:
                text = read_tail(path, 64 * 1024)
                if phase_matches(path, text):
                    candidates.append((os.path.getmtime(path), os.path.getsize(path), path, False, artifact_phase(path, text)))
            except Exception:
                pass
    candidates.sort(key=lambda item: (1 if item[3] else 0, item[0]), reverse=True)
    return candidates


def report_summary(value):
    plain = html_to_plain(value)
    lines = [re.sub(r"\s+", " ", line).strip() for line in plain.splitlines()]
    lines = [line for line in lines if line]
    important = []
    status = ""
    report_phase = phase_from_text(plain)
    for line in lines:
        lower = line.lower()
        if re.search(r"\b(status|result|summary|phase)\b", lower) and re.search(r"\b(failed|failure|error)\b", lower):
            status = "failed"
            important.append(line)
        elif re.search(r"\b(status|result|summary|phase)\b", lower) and re.search(r"\b(success|succeeded|successful|passed|completed)\b", lower):
            if status != "failed":
                status = "succeeded"
            important.append(line)
        elif re.search(r"\b(failed|failure|error)\b", lower):
            important.append(line)
        elif re.search(r"\b(success|succeeded|successful|passed|completed)\b", lower):
            important.append(line)
    if not status:
        status = text_status("\n".join(lines[-80:]))
    return status, important[-20:], report_phase


reports = []
matching_reports = []
base_depth = log_dir.rstrip(os.sep).count(os.sep)
for root, dirs, files in os.walk(log_dir):
    depth = root.rstrip(os.sep).count(os.sep) - base_depth
    if depth >= 8:
        dirs[:] = []
    for name in files:
        low = name.lower()
        if low.endswith((".html", ".htm")):
            path = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
                html = ""
                status = ""
                summary_lines = []
                report_phase = artifact_phase(path, "")
                if size <= 2 * 1024 * 1024:
                    with open(path, "rb") as handle:
                        html = handle.read().decode("utf-8", "replace")
                    status, summary_lines, detected_phase = report_summary(html)
                    report_phase = detected_phase or report_phase
                item = {
                    "mtime": mtime,
                    "path": path,
                    "size": size,
                    "html": html,
                    "status": status,
                    "summaryLines": summary_lines,
                    "phase": report_phase,
                }
                reports.append(item)
                if not requested_phase or report_phase == requested_phase:
                    matching_reports.append(item)
            except Exception:
                pass

reports.sort(key=lambda item: item.get("mtime", 0), reverse=True)
matching_reports.sort(key=lambda item: item.get("mtime", 0), reverse=True)
chosen_report = matching_reports[0] if matching_reports else None

if not chosen_report:
    log_candidates = latest_log_candidates()
    latest_log = log_candidates[0][2] if log_candidates else ""
    latest_phase = log_candidates[0][4] if log_candidates else ""
    latest_text = read_tail(latest_log) if latest_log else ""
    payload = {
        "logDir": log_dir,
        "phase": requested_phase,
        "reportPath": "",
        "html": "",
        "reports": [item.get("path") for item in reports[:20]],
        "matchingReports": [item.get("path") for item in matching_reports[:20]],
        "reportStatus": text_status(latest_text),
        "reportPhase": "",
        "latestLogPhase": latest_phase,
        "phaseMatched": bool(not requested_phase or latest_phase == requested_phase),
        "summaryLines": text_summary(latest_text),
        "latestLogPath": latest_log,
        "latestLogText": latest_text,
        "latestArtifacts": [path for _, _, path, _, _ in log_candidates[:20]],
    }
else:
    chosen = chosen_report.get("path")
    size = os.path.getsize(chosen)
    if size > 2 * 1024 * 1024:
        raise RuntimeError("SPBAT report is larger than 2 MB: %%s" %% chosen)
    html = chosen_report.get("html") or ""
    if not html:
        with open(chosen, "rb") as handle:
            html = handle.read().decode("utf-8", "replace")
    report_status = chosen_report.get("status") or ""
    summary_lines = chosen_report.get("summaryLines") or []
    report_phase = chosen_report.get("phase") or artifact_phase(chosen, html)
    payload = {
        "logDir": log_dir,
        "phase": requested_phase,
        "reportPath": chosen,
        "html": html,
        "reports": [item.get("path") for item in reports[:20]],
        "matchingReports": [item.get("path") for item in matching_reports[:20]],
        "reportStatus": report_status,
        "reportPhase": report_phase,
        "phaseMatched": bool(not requested_phase or report_phase == requested_phase),
        "summaryLines": summary_lines,
    }

print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
''' % (json.dumps(log_dir), json.dumps(preferred_log), json.dumps(phase))


def spb_inactive_retain_level(value):
    try:
        level = int(str(value or "1").strip())
    except ValueError:
        level = 1
    return max(1, min(level, 9))


def build_spb_inactive_check_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    retain_level = spb_inactive_retain_level(body.get("retainLevel"))
    opatch_heap_options = sanitize_opatch_heap_options(body.get("opatchHeapOptions"))
    if not oracle_home:
        raise ValueError("ORACLE_HOME is required for inactive patch review.")
    payload = {
        "oracleHome": oracle_home,
        "retainLevel": retain_level,
        "opatchHeapOptions": opatch_heap_options,
        "opatchWaitTimeoutSeconds": SPB_INACTIVE_PROCESS_WAIT_TIMEOUT,
    }
    script = r'''
import json
import os
import re
import shlex
import subprocess
import sys
import time

request = json.loads(__PATCHSCOPE_SPB_INACTIVE_JSON__)
oracle_home = os.path.realpath(os.path.abspath(str(request.get("oracleHome") or "")))
requested_oracle_home = os.path.abspath(str(request.get("oracleHome") or "")).rstrip(os.sep)
retain_level = int(request.get("retainLevel") or 1)
opatch = os.path.join(oracle_home, "OPatch", "opatch")
properties_path = os.path.join(oracle_home, "OPatch", "config", "opatch.properties")
__PATCHSCOPE_OPATCH_HEAP_HELPER__
opatch_heap = configure_opatch_heap(request.get("opatchHeapOptions"))

if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isfile(opatch):
    raise RuntimeError("OPatch was not found under %s" % oracle_home)

OPATCH_YES_INPUT = "y\n" * 20


def command_text(command):
    return " ".join(shlex.quote(part) for part in command)


def run_command(command, allow_failure=False, input_text=None):
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stdin_data = input_text.encode("utf-8") if hasattr(input_text, "encode") else input_text
    stdout, _ = proc.communicate(stdin_data)
    output = stdout.decode("utf-8", "replace") if hasattr(stdout, "decode") else str(stdout or "")
    if proc.returncode != 0:
        message = "%s failed with return code %s.\n%s" % (command_text(command), proc.returncode, output[-8000:])
        if allow_failure:
            return proc.returncode, output, message
        raise RuntimeError(message)
    if allow_failure:
        return proc.returncode, output, ""
    return output


def emit(text):
    sys.stdout.write(str(text or ""))
    if not str(text or "").endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def running_opatch_utilities():
    try:
        output = subprocess.check_output(["ps", "-eo", "pid=,args="], stderr=subprocess.STDOUT)
    except Exception:
        return []
    text = output.decode("utf-8", "replace") if hasattr(output, "decode") else str(output or "")
    matches = []
    own_pids = set([os.getpid(), os.getppid()])
    home_tokens = [oracle_home.lower()]
    requested_token = requested_oracle_home.lower()
    if requested_token and requested_token not in home_tokens:
        home_tokens.append(requested_token)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        if pid in own_pids:
            continue
        command = parts[1]
        lower = command.lower()
        if not any(token and token in lower for token in home_tokens):
            continue
        if not re.search(r"\butil\s+(listorderedinactivepatches|deleteinactivepatches|cleanup)\b", lower):
            continue
        if "/opatch" not in lower and "oracle/opatch" not in lower:
            continue
        matches.append({"pid": pid, "command": command[:700]})
    return matches


def wait_for_opatch_utilities_to_clear(next_step, timeout_seconds=900):
    deadline = time.time() + timeout_seconds
    last_message = 0
    while True:
        matches = running_opatch_utilities()
        if not matches:
            return
        pids = ", ".join(str(item["pid"]) for item in matches)
        if time.time() > deadline:
            details = "; ".join("pid %s: %s" % (item["pid"], item["command"]) for item in matches)
            raise RuntimeError("Cannot start %s because another OPatch inactive-patch utility is still running for %s: %s" % (next_step, oracle_home, details))
        if time.time() - last_message > 30:
            emit("Waiting for existing OPatch inactive-patch utility process(es) to finish before %s: %s" % (next_step, pids))
            last_message = time.time()
        time.sleep(5)


def current_retain_value():
    if not os.path.isfile(properties_path):
        return ""
    try:
        text = open(properties_path, "r", encoding="utf-8", errors="replace").read()
    except TypeError:
        text = open(properties_path, "r").read()
    match = re.search(r"^\s*RETAIN_INACTIVE_PATCHES\s*=\s*(\S+)", text, re.M)
    return match.group(1) if match else ""


def parse_inactive_output(output):
    total_line = ""
    chain_counts = []
    for line in output.splitlines():
        if re.search(r"\bTotal\b", line, re.I) and re.search(r"\binactive\b", line, re.I):
            total_line = line.strip()
        chain_match = re.search(r"\bThere\s+are\s+(\d+)\s+inactive\b.*\bin\s+chain\s+\d+\b", line, re.I)
        if chain_match:
            chain_counts.append(int(chain_match.group(1)))
    numbers = [int(item) for item in re.findall(r"(\d+)\s+inactive", total_line, re.I)]
    lower = output.lower()
    no_inactive = bool(re.search(r"\b(no|zero|0)\s+inactive\b", lower))
    has_inactive = False
    if chain_counts:
        has_inactive = sum(chain_counts) > 0
    elif numbers:
        has_inactive = sum(numbers) > 0
    elif "inactive" in lower and not no_inactive:
        has_inactive = True
    inactive_patch_count = sum(chain_counts) if chain_counts else (numbers[0] if len(numbers) >= 1 else None)
    inactive_overlay_count = None if chain_counts else (numbers[1] if len(numbers) >= 2 else None)
    highest_chain_count = max(chain_counts) if chain_counts else None
    desired_reached = (not has_inactive) or (bool(chain_counts) and max(chain_counts) <= retain_level) or (bool(numbers) and max(numbers) <= retain_level)
    if chain_counts and not total_line:
        total_line = "%s inactive patch(es) across %s RU chain(s); highest chain retain count is %s" % (sum(chain_counts), len(chain_counts), highest_chain_count)
    return {
        "oracleHome": oracle_home,
        "retainLevel": retain_level,
        "opatchHeap": opatch_heap,
        "propertiesPath": properties_path,
        "currentRetainInactivePatches": current_retain_value(),
        "command": command_text([opatch, "util", "listorderedinactivepatches", "-oh", oracle_home]),
        "output": output[-50000:],
        "totalLine": total_line,
        "inactivePatchCount": inactive_patch_count,
        "inactiveOverlayCount": inactive_overlay_count,
        "inactiveChainCounts": chain_counts,
        "highestInactiveChainCount": highest_chain_count,
        "hasInactive": has_inactive,
        "desiredReached": desired_reached,
        "status": "retained" if desired_reached else "cleanup-recommended",
    }


emit("Checking for another OPatch inactive-patch utility before starting the review.")
wait_for_opatch_utilities_to_clear("inactive patch review", timeout_seconds=int(request.get("opatchWaitTimeoutSeconds") or 900))
emit("Starting OPatch listorderedinactivepatches. OPatch is loading its Java runtime and Oracle inventory; large or locked inventories can take several minutes.")
payload = parse_inactive_output(run_command([opatch, "util", "listorderedinactivepatches", "-oh", oracle_home]))
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return (
        script
        .replace("__PATCHSCOPE_SPB_INACTIVE_JSON__", json.dumps(json.dumps(payload)))
        .replace("__PATCHSCOPE_OPATCH_HEAP_HELPER__", REMOTE_OPATCH_HEAP_HELPER)
    )


def build_spb_inactive_cancel_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    if not oracle_home:
        raise ValueError("ORACLE_HOME is required to stop an inactive-patch operation.")
    payload = {"oracleHome": oracle_home}
    script = r'''
import json
import os
import re
import signal
import subprocess
import time

request = json.loads(__PATCHSCOPE_SPB_INACTIVE_CANCEL_JSON__)
requested_home = str(request.get("oracleHome") or "").rstrip("/")
oracle_home = os.path.realpath(requested_home)
if not oracle_home or oracle_home == "/":
    raise RuntimeError("A specific ORACLE_HOME is required for cancellation.")

try:
    raw = subprocess.check_output(["ps", "-eo", "pid=,ppid=,args="], stderr=subprocess.STDOUT)
except Exception as error:
    raise RuntimeError("Could not inspect remote OPatch processes: %s" % error)

text = raw.decode("utf-8", "replace") if hasattr(raw, "decode") else str(raw or "")
processes = {}
matches = set()
home_tokens = set([requested_home.lower(), oracle_home.lower()])
own_pids = set([os.getpid(), os.getppid()])
for line in text.splitlines():
    parts = line.strip().split(None, 2)
    if len(parts) != 3:
        continue
    try:
        pid = int(parts[0])
        ppid = int(parts[1])
    except Exception:
        continue
    command = parts[2]
    processes[pid] = {"ppid": ppid, "command": command}
    lower = command.lower()
    if pid in own_pids or not any(token and token in lower for token in home_tokens):
        continue
    if re.search(r"\butil\s+(listorderedinactivepatches|deleteinactivepatches|cleanup)\b", lower) and ("/opatch" in lower or "oracle/opatch" in lower):
        matches.add(pid)

changed = True
while changed:
    changed = False
    for pid, item in processes.items():
        if pid not in matches and item["ppid"] in matches:
            matches.add(pid)
            changed = True

terminated = []
for pid in sorted(matches, reverse=True):
    try:
        os.kill(pid, signal.SIGTERM)
        terminated.append(pid)
    except OSError:
        pass

deadline = time.time() + 5
while time.time() < deadline:
    alive = []
    for pid in terminated:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except OSError:
            pass
    if not alive:
        break
    time.sleep(0.5)

killed = []
for pid in (alive if 'alive' in locals() else []):
    try:
        os.kill(pid, signal.SIGKILL)
        killed.append(pid)
    except OSError:
        pass

result = {
    "status": "cancelled",
    "oracleHome": oracle_home,
    "matchedPids": sorted(matches),
    "terminatedPids": terminated,
    "killedPids": killed,
    "message": "No matching OPatch inactive-patch utility was still running." if not matches else "Stop signal sent only to matching inactive-patch OPatch utility processes.",
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(result))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_SPB_INACTIVE_CANCEL_JSON__", json.dumps(json.dumps(payload)))


def build_spb_inactive_delete_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    retain_level = spb_inactive_retain_level(body.get("retainLevel"))
    confirmed = bool(body.get("confirmed"))
    opatch_heap_options = sanitize_opatch_heap_options(body.get("opatchHeapOptions"))
    if not oracle_home:
        raise ValueError("ORACLE_HOME is required for inactive patch cleanup.")
    if not confirmed:
        raise ValueError("Inactive patch deletion requires explicit user confirmation.")
    payload = {
        "oracleHome": oracle_home,
        "retainLevel": retain_level,
        "confirmed": confirmed,
        "opatchHeapOptions": opatch_heap_options,
        "opatchUtilityTimeoutSeconds": SPB_INACTIVE_CLEANUP_TIMEOUT,
        "opatchWaitTimeoutSeconds": SPB_INACTIVE_PROCESS_WAIT_TIMEOUT,
    }
    script = r'''
import json
import os
import errno
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

request = json.loads(__PATCHSCOPE_SPB_INACTIVE_JSON__)
oracle_home = os.path.realpath(os.path.abspath(str(request.get("oracleHome") or "")))
requested_oracle_home = os.path.abspath(str(request.get("oracleHome") or "")).rstrip(os.sep)
retain_level = int(request.get("retainLevel") or 1)
confirmed = bool(request.get("confirmed"))
opatch = os.path.join(oracle_home, "OPatch", "opatch")
properties_path = os.path.join(oracle_home, "OPatch", "config", "opatch.properties")
__PATCHSCOPE_OPATCH_HEAP_HELPER__
opatch_heap = configure_opatch_heap(request.get("opatchHeapOptions"))
OPATCH_UTILITY_TIMEOUT_SECONDS = int(request.get("opatchUtilityTimeoutSeconds") or 10800)
OPATCH_WAIT_TIMEOUT_SECONDS = int(request.get("opatchWaitTimeoutSeconds") or 900)

if not confirmed:
    raise RuntimeError("Inactive patch deletion requires explicit user confirmation.")
if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isfile(opatch):
    raise RuntimeError("OPatch was not found under %s" % oracle_home)

OPATCH_YES_INPUT = "y\n" * 20


def command_text(command):
    return " ".join(shlex.quote(part) for part in command)


def emit(text):
    sys.stdout.write(str(text or ""))
    if not str(text or "").endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def run_command(command, allow_failure=False, input_text=None, timeout_seconds=None):
    if timeout_seconds is None:
        timeout_seconds = OPATCH_UTILITY_TIMEOUT_SECONDS
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stdin_data = input_text.encode("utf-8") if hasattr(input_text, "encode") else input_text
    output_chunks = []
    timed_out = [False]
    timer = None
    if timeout_seconds:
        def kill_timed_out_process():
            timed_out[0] = True
            try:
                proc.kill()
            except Exception:
                pass
        timer = threading.Timer(timeout_seconds, kill_timed_out_process)
        timer.daemon = True
        timer.start()
    if stdin_data is not None:
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass
    while True:
        chunk = proc.stdout.readline()
        if chunk:
            output_chunks.append(chunk)
            text = chunk.decode("utf-8", "replace") if hasattr(chunk, "decode") else str(chunk or "")
            emit(text)
            continue
        if proc.poll() is not None:
            remainder = proc.stdout.read()
            if remainder:
                output_chunks.append(remainder)
                text = remainder.decode("utf-8", "replace") if hasattr(remainder, "decode") else str(remainder or "")
                emit(text)
            break
        time.sleep(0.1)
    proc.wait()
    if timer:
        timer.cancel()
    stdout = b"".join(output_chunks)
    output = stdout.decode("utf-8", "replace") if hasattr(stdout, "decode") else str(stdout or "")
    if timed_out[0]:
        message = "%s timed out after %s seconds.\n%s" % (command_text(command), timeout_seconds, output[-8000:])
        if allow_failure:
            return 124, output, message
        raise RuntimeError(message)
    if proc.returncode != 0:
        message = "%s failed with return code %s.\n%s" % (command_text(command), proc.returncode, output[-8000:])
        if allow_failure:
            return proc.returncode, output, message
        raise RuntimeError(message)
    if allow_failure:
        return proc.returncode, output, ""
    return output


def running_opatch_utilities():
    try:
        output = subprocess.check_output(["ps", "-eo", "pid=,args="], stderr=subprocess.STDOUT)
    except Exception:
        return []
    text = output.decode("utf-8", "replace") if hasattr(output, "decode") else str(output or "")
    matches = []
    own_pids = set([os.getpid(), os.getppid()])
    home_tokens = [oracle_home.lower()]
    requested_token = requested_oracle_home.lower()
    if requested_token and requested_token not in home_tokens:
        home_tokens.append(requested_token)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        if pid in own_pids:
            continue
        command = parts[1]
        lower = command.lower()
        if not any(token and token in lower for token in home_tokens):
            continue
        if not re.search(r"\butil\s+(deleteinactivepatches|cleanup)\b", lower):
            continue
        if "/opatch" not in lower and "oracle/opatch" not in lower:
            continue
        matches.append({"pid": pid, "command": command[:700]})
    return matches


def wait_for_opatch_utilities_to_clear(next_step, timeout_seconds=180):
    deadline = time.time() + timeout_seconds
    last_message = 0
    while True:
        matches = running_opatch_utilities()
        if not matches:
            return
        pids = ", ".join(str(item["pid"]) for item in matches)
        if time.time() > deadline:
            details = "; ".join("pid %s: %s" % (item["pid"], item["command"]) for item in matches)
            raise RuntimeError("Cannot start %s because another OPatch inactive-patch utility is still running for %s: %s" % (next_step, oracle_home, details))
        if time.time() - last_message > 10:
            emit("Waiting for existing OPatch inactive-patch utility process(es) to finish before %s: %s" % (next_step, pids))
            last_message = time.time()
        time.sleep(2)


def acquire_cleanup_lock():
    try:
        import fcntl
    except Exception:
        return None
    lock_path = os.path.join(oracle_home, "OPatch", ".patchpilot-inactive-cleanup.lock")
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as error:
        if getattr(error, "errno", None) in (errno.EACCES, errno.EAGAIN):
            raise RuntimeError("Another PatchPilot inactive patch cleanup job is already running for %s." % oracle_home)
        raise
    handle.seek(0)
    handle.truncate()
    handle.write("pid=%s\nstarted=%s\noracle_home=%s\n" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S"), oracle_home))
    handle.flush()
    return handle


def read_properties():
    if not os.path.isfile(properties_path):
        return ""
    try:
        return open(properties_path, "r", encoding="utf-8", errors="replace").read()
    except TypeError:
        return open(properties_path, "r").read()


def current_retain_value():
    text = read_properties()
    match = re.search(r"^\s*RETAIN_INACTIVE_PATCHES\s*=\s*(\S+)", text, re.M)
    return match.group(1) if match else ""


def write_properties(text):
    parent = os.path.dirname(properties_path)
    if not os.path.isdir(parent):
        raise RuntimeError("OPatch config directory does not exist: %s" % parent)
    tmp_path = properties_path + ".patchpilot.tmp"
    try:
        handle = open(tmp_path, "w", encoding="utf-8")
    except TypeError:
        handle = open(tmp_path, "w")
    with handle:
        handle.write(text)
    os.rename(tmp_path, properties_path)


def ensure_retain_property():
    backup_path = ""
    text = read_properties()
    if os.path.isfile(properties_path):
        backup_path = properties_path + ".patchpilot-%s.bak" % time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(properties_path, backup_path)
    line = "RETAIN_INACTIVE_PATCHES=%s" % retain_level
    if re.search(r"^\s*RETAIN_INACTIVE_PATCHES\s*=", text, re.M):
        text = re.sub(r"^\s*RETAIN_INACTIVE_PATCHES\s*=.*$", line, text, flags=re.M)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    write_properties(text)
    return backup_path


output_parts = []

def record(text):
    output_parts.append(text)
    emit(text)

cleanup_lock = acquire_cleanup_lock()
wait_for_opatch_utilities_to_clear("inactive patch cleanup", timeout_seconds=OPATCH_WAIT_TIMEOUT_SECONDS)
record("OPatch heap options %s: OPATCH_JRE_MEMORY_OPTIONS=%s" % (opatch_heap.get("action"), opatch_heap.get("effective")))
provided_before = request.get("beforeCheck") if isinstance(request.get("beforeCheck"), dict) else {}
before = dict(provided_before)
if not before:
    before = {
        "oracleHome": oracle_home,
        "retainLevel": retain_level,
        "propertiesPath": properties_path,
        "currentRetainInactivePatches": current_retain_value(),
        "command": "",
        "output": "",
        "totalLine": "Inactive patches were reviewed before approval.",
        "inactivePatchCount": None,
        "inactiveOverlayCount": None,
        "hasInactive": True,
        "desiredReached": False,
        "status": "cleanup-approved",
        "opatchHeap": opatch_heap,
    }
before_summary = before.get("totalLine") or ("No inactive patches reported" if not before.get("hasInactive") else "Inactive patches reported")
properties_backup = ""
delete_runs = 0
cleanup_output = ""
cleanup_error = ""
failed_command = ""
failed_return_code = 0
after = before
status = ""

if not before.get("hasInactive"):
    status = "none"
elif before.get("desiredReached"):
    status = "already-retained"
else:
    properties_backup = ensure_retain_property()
    record("Set RETAIN_INACTIVE_PATCHES=%s in %s" % (retain_level, properties_path))
    if properties_backup:
        record("Backed up opatch.properties to %s" % properties_backup)
    delete_runs = 1
    delete_command = [opatch, "util", "deleteinactivepatches", "-oh", oracle_home]
    record("$ printf 'y\\n' | " + command_text(delete_command))
    record("PatchPilot answered OPatch deleteinactivepatches prompts with y based on the UI confirmation.")
    return_code, delete_output, delete_error = run_command(delete_command, allow_failure=True, input_text=OPATCH_YES_INPUT)
    output_parts.append(delete_output)
    if delete_error:
        cleanup_error = delete_error
        failed_command = "printf 'y\\n' | " + command_text(delete_command)
        failed_return_code = return_code
        output_parts.append(delete_error)
    if not cleanup_error:
        record("OPatch deleteinactivepatches completed; waiting for the utility process to exit before cleanup.")
        try:
            wait_for_opatch_utilities_to_clear("OPatch cleanup", timeout_seconds=OPATCH_WAIT_TIMEOUT_SECONDS)
        except Exception as wait_error:
            cleanup_error = str(wait_error)
            failed_command = "wait for deleteinactivepatches to exit"
            record(cleanup_error)
    if not cleanup_error:
        cleanup_command = [opatch, "util", "cleanup", "-oh", oracle_home]
        record("Starting OPatch cleanup after deleteinactivepatches completed.")
        record("$ printf 'y\\n' | " + command_text(cleanup_command))
        record("PatchPilot answered OPatch cleanup prompts with y based on the UI confirmation.")
        return_code, cleanup_output, command_error = run_command(cleanup_command, allow_failure=True, input_text=OPATCH_YES_INPUT)
        output_parts.append(cleanup_output)
        if command_error:
            cleanup_error = command_error
            failed_command = "printf 'y\\n' | " + command_text(cleanup_command)
            failed_return_code = return_code
            output_parts.append(command_error)
        else:
            record("OPatch cleanup completed. Final listorderedinactivepatches recheck skipped because the pre-confirmation review already captured inactive patch details.")
            after = dict(before)
            after["currentRetainInactivePatches"] = current_retain_value()
            after["desiredReached"] = True
            after["status"] = "cleanup-completed"
            after["output"] = cleanup_output[-50000:]
            after["totalLine"] = "Inactive patch cleanup completed by OPatch; final list recheck skipped."
    if cleanup_error:
        status = "failed"
    else:
        status = "removed"

after_summary = after.get("totalLine") or ("No inactive patches reported" if not after.get("hasInactive") else "Inactive patches reported")
payload = {
    "oracleHome": oracle_home,
    "retainLevel": retain_level,
    "status": status,
    "beforeCheck": before,
    "afterCheck": after,
    "beforeSummary": before_summary,
    "afterSummary": after_summary,
    "propertiesPath": properties_path,
    "propertiesBackup": properties_backup,
    "opatchHeap": opatch_heap,
    "deleteRuns": delete_runs,
    "cleanupOutput": cleanup_output[-50000:],
    "failedCommand": failed_command,
    "returnCode": failed_return_code,
    "commands": [
        "Set RETAIN_INACTIVE_PATCHES=%s in %s" % (retain_level, properties_path),
        "printf 'y\\n' | " + command_text([opatch, "util", "deleteinactivepatches", "-oh", oracle_home]),
        "printf 'y\\n' | " + command_text([opatch, "util", "cleanup", "-oh", oracle_home]),
    ],
    "output": "\n".join(output_parts)[-80000:],
    "error": "" if status in ("removed", "already-retained", "none") else (cleanup_error or "Inactive patch cleanup did not reach the requested retain level."),
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return (
        script
        .replace("__PATCHSCOPE_SPB_INACTIVE_JSON__", json.dumps(json.dumps(payload)))
        .replace("__PATCHSCOPE_OPATCH_HEAP_HELPER__", REMOTE_OPATCH_HEAP_HELPER)
    )


def build_spb_prestart_cleanup_script(body):
    home = body.get("home") or {}
    oracle_home = str(home.get("oracleHome") or body.get("oracleHome") or "").strip()
    domain_home = str(home.get("domainHome") or body.get("domainHome") or "").strip()
    instance_home = str(home.get("instanceHome") or body.get("instanceHome") or "").strip()
    targets = body.get("targets")
    dry_run = bool(body.get("dryRun"))
    if not isinstance(targets, list) or not [item for item in targets if str(item or "").strip()]:
        raise ValueError("At least one tmp/cache cleanup target path is required.")
    payload = {
        "oracleHome": oracle_home,
        "domainHome": domain_home,
        "instanceHome": instance_home,
        "targets": [str(item or "").strip() for item in targets if str(item or "").strip()],
        "dryRun": dry_run,
    }
    script = r'''
import json
import os
import shutil
import sys
import time

request = json.loads(__PATCHSCOPE_SPB_CLEANUP_JSON__)
oracle_home = str(request.get("oracleHome") or "").strip()
domain_home = str(request.get("domainHome") or "").strip()
instance_home = str(request.get("instanceHome") or "").strip()
targets = request.get("targets") or []
dry_run = bool(request.get("dryRun"))


def emit(text):
    sys.stdout.write(str(text or ""))
    if not str(text or "").endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def clean_path(value):
    path = str(value or "").strip().replace("\\", "/")
    if path.endswith("/*"):
        path = path[:-2]
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def absolute_path(value):
    value = clean_path(value)
    if not value.startswith("/"):
        return value
    return os.path.abspath(value)


def same_path(left, right):
    return os.path.normpath(left or "") == os.path.normpath(right or "")


def validate_target(input_path):
    raw = str(input_path or "").strip()
    path = absolute_path(raw)
    if not raw:
        return path, "rejected", "Cleanup path is blank."
    if "<" in raw or ">" in raw:
        return path, "rejected", "Replace placeholder server names before cleanup."
    if "*" in clean_path(raw):
        return path, "rejected", "Wildcards are only allowed as the final /* contents marker."
    if not path.startswith("/"):
        return path, "rejected", "Cleanup path must be absolute."
    kind = os.path.basename(path)
    if kind not in ("tmp", "cache"):
        return path, "rejected", "Cleanup path must end with /tmp/* or /cache/*."
    parent = os.path.dirname(path)
    server_name = os.path.basename(parent)
    servers_dir = os.path.dirname(parent)
    if not server_name or server_name.startswith("<"):
        return path, "rejected", "Cleanup path must include a real server directory name."
    if os.path.basename(servers_dir) != "servers":
        return path, "rejected", "Cleanup path must be under DOMAIN_HOME/servers/<server>."
    if domain_home:
        expected_servers = os.path.join(os.path.abspath(domain_home), "servers")
        if not same_path(servers_dir, expected_servers):
            return path, "rejected", "Cleanup path is outside the selected DOMAIN_HOME/servers directory."
    if "/stage/" in path or path.endswith("/stage"):
        return path, "rejected", "Stage directories are not part of this cleanup gate."
    return path, "", ""


def remove_child(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
        return
    if os.path.isdir(path):
        shutil.rmtree(path)
        return
    os.unlink(path)


def cleanup_target(input_path):
    path, rejected_status, rejected_error = validate_target(input_path)
    result = {
        "input": str(input_path or "").strip(),
        "path": path,
        "status": "pending",
        "beforeCount": 0,
        "removedCount": 0,
        "sample": [],
        "error": "",
    }
    if rejected_status:
        result["status"] = rejected_status
        result["error"] = rejected_error
        emit("Rejected cleanup path %s: %s" % (input_path, rejected_error))
        return result
    if not os.path.exists(path):
        result["status"] = "missing"
        emit("Cleanup path missing, skipped: %s" % path)
        return result
    if not os.path.isdir(path):
        result["status"] = "failed"
        result["error"] = "Cleanup target exists but is not a directory."
        emit("Cleanup path failed: %s is not a directory." % path)
        return result
    try:
        children = sorted(os.listdir(path))
    except Exception as error:
        result["status"] = "failed"
        result["error"] = "Could not list directory: %s" % error
        emit("Cleanup path failed: %s: %s" % (path, result["error"]))
        return result
    result["beforeCount"] = len(children)
    result["sample"] = children[:25]
    if dry_run:
        result["status"] = "preview"
        emit("Preview cleanup: %s contains %s item(s)." % (path, len(children)))
        return result
    if not children:
        result["status"] = "empty"
        emit("Cleanup path already empty: %s" % path)
        return result
    errors = []
    removed = 0
    for name in children:
        child_path = os.path.join(path, name)
        try:
            remove_child(child_path)
            removed += 1
        except Exception as error:
            errors.append("%s: %s" % (name, error))
    result["removedCount"] = removed
    if errors:
        result["status"] = "failed"
        result["error"] = "; ".join(errors[:8])
        emit("Cleanup path failed: %s removed %s/%s item(s); %s" % (path, removed, len(children), result["error"]))
    else:
        result["status"] = "cleared"
        emit("Cleanup path cleared: %s removed %s item(s)." % (path, removed))
    return result


emit("Starting PatchPilot pre-start tmp/cache cleanup%s." % (" preview" if dry_run else ""))
results = [cleanup_target(target) for target in targets]
statuses = [item.get("status") for item in results]
has_failure = any(status in ("failed", "rejected") for status in statuses)
has_warning = any(status in ("missing", "empty") for status in statuses)
if dry_run:
    status = "preview"
elif has_failure:
    status = "failed"
elif has_warning:
    status = "partial"
else:
    status = "succeeded"

payload = {
    "oracleHome": oracle_home,
    "domainHome": domain_home,
    "instanceHome": instance_home,
    "dryRun": dry_run,
    "status": status,
    "targets": results,
    "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    "output": "",
    "error": "" if status in ("succeeded", "partial", "preview") else "One or more tmp/cache cleanup paths failed validation or deletion.",
}
payload["output"] = "\n".join([
    "%s | %s | before=%s removed=%s%s" % (
        item.get("status") or "unknown",
        item.get("path") or item.get("input") or "",
        item.get("beforeCount") or 0,
        item.get("removedCount") or 0,
        (" | " + item.get("error")) if item.get("error") else "",
    )
    for item in results
])
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_SPB_CLEANUP_JSON__", json.dumps(json.dumps(payload)))


def build_opatch_version_script(oracle_home):
    return r'''
import json
import os
import re
import subprocess

home = %s
opatch = os.path.join(home, "OPatch", "opatch")
if not os.path.isfile(opatch):
    raise RuntimeError("OPatch was not found under %%s" %% home)
try:
    output = subprocess.check_output([opatch, "version"], stderr=subprocess.STDOUT, timeout=20).decode("utf-8", "replace")
except Exception as error:
    raise RuntimeError(str(error))
match = re.search(r"OPatch Version:\s*([0-9.]+)", output)
version = match.group(1) if match else ""
if not version:
    fallback = re.search(r"([0-9]+(?:\.[0-9]+){2,})", output)
    version = fallback.group(1) if fallback else "unknown"
payload = {"oracleHome": home, "version": version, "output": output}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
''' % json.dumps(oracle_home)


def build_opatch_upgrade_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    patch_path = str(body.get("patchPath") or "").strip()
    log_dir = str(body.get("logDir") or "").strip()
    opatch_path = str(body.get("opatchPath") or "").strip()
    opatch_heap_options = sanitize_opatch_heap_options(body.get("opatchHeapOptions"))
    if not oracle_home:
        raise ValueError("Selected ORACLE_HOME is required for OPatch upgrade.")
    if not patch_path:
        raise ValueError("SPB download directory is required for OPatch upgrade.")
    payload = {
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "logDir": log_dir,
        "opatchPath": opatch_path,
        "opatchHeapOptions": opatch_heap_options,
    }
    script = r'''
import json
import os
import re
import shutil
import shlex
import subprocess
import time
import zipfile

request = __PATCHSCOPE_OPATCH_JSON__
oracle_home = os.path.abspath(str(request.get("oracleHome") or ""))
patch_dir = os.path.abspath(str(request.get("patchPath") or ""))
log_dir = str(request.get("logDir") or "").strip()
opatch_path = str(request.get("opatchPath") or "").strip()
__PATCHSCOPE_OPATCH_HEAP_HELPER__
opatch_heap = configure_opatch_heap(request.get("opatchHeapOptions"))
if not log_dir:
    log_dir = os.path.join(patch_dir, "spbat_logs")
log_dir = os.path.abspath(log_dir)
work_dir = os.path.join(log_dir, "opatch_upgrade")
os.makedirs(work_dir, exist_ok=True)

if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isdir(patch_dir):
    raise RuntimeError("SPB download directory does not exist: %s" % patch_dir)


def unique(items):
    result = []
    seen = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def candidate_paths():
    candidates = []
    extra_roots = []
    if opatch_path:
        absolute = os.path.abspath(opatch_path)
        if os.path.isdir(absolute):
            extra_roots.append(absolute)
        else:
            candidates.append(absolute)
    roots = extra_roots + [
        os.path.join(patch_dir, "tools", "opatch", "generic"),
        os.path.join(patch_dir, "upgrade_installers"),
        os.path.join(patch_dir, "tools"),
        patch_dir,
    ]
    for base in roots:
        if not os.path.isdir(base):
            continue
        base_depth = base.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(base):
            depth = root.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= 5:
                dirs[:] = []
            dirs[:] = [name for name in dirs if name.lower() not in ("logs", "spbat-logs", "reports")]
            for name in files:
                low = name.lower()
                root_low = root.lower()
                path = os.path.join(root, name)
                if low == "opatch_generic.jar":
                    candidates.insert(0, path)
                elif low.endswith(".zip") and ("opatch" in low or "p6880880" in low or "/tools/opatch/generic" in root_low):
                    candidates.append(path)
    return unique(candidates)


def jar_from_candidate(path):
    if not os.path.isfile(path):
        return ""
    low = path.lower()
    if low.endswith(".jar") and os.path.basename(low) == "opatch_generic.jar":
        return path
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            jar_members = [name for name in archive.namelist() if name.lower().endswith("/opatch_generic.jar") or name.lower() == "opatch_generic.jar"]
            if not jar_members:
                return ""
            member = sorted(jar_members, key=len)[0]
            extract_dir = os.path.join(work_dir, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            target = os.path.join(extract_dir, "opatch_generic.jar")
            with archive.open(member) as source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest)
            return target
    return ""


def readme_from_dir(base):
    if not base or not os.path.isdir(base):
        return ""
    base_depth = base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= 3:
            dirs[:] = []
        dirs[:] = [name for name in dirs if name.lower() not in ("logs", "spbat-logs", "reports")]
        for name in sorted(files):
            low = name.lower()
            if low.startswith("readme") and (low.endswith(".txt") or low.endswith(".html") or low.endswith(".htm") or low.endswith(".pdf")):
                return os.path.join(root, name)
    return ""


def readme_from_candidate(path):
    if not path:
        return ""
    if os.path.isdir(path):
        return readme_from_dir(path)
    if not os.path.isfile(path):
        return ""
    low = path.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if os.path.basename(name).lower().startswith("readme")]
            if not members:
                return ""
            def member_rank(name):
                low_name = name.lower()
                if low_name.endswith(".txt"):
                    return (0, len(name))
                if low_name.endswith(".html") or low_name.endswith(".htm"):
                    return (1, len(name))
                if low_name.endswith(".pdf"):
                    return (2, len(name))
                return (3, len(name))
            member = sorted(members, key=member_rank)[0]
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.basename(member)) or "README"
            target = os.path.join(work_dir, "opatch_%s" % safe_name)
            with archive.open(member) as source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest)
            return target
    return readme_from_dir(os.path.dirname(path))


jar_path = ""
source_path = ""
candidates = candidate_paths()
for candidate in candidates:
    try:
        jar_path = jar_from_candidate(candidate)
    except Exception:
        jar_path = ""
    if jar_path:
        source_path = candidate
        break

if not jar_path:
    raise RuntimeError("Could not find opatch_generic.jar under %s/tools/opatch/generic or the provided OPatch path. Download OPatch patch 28186730 from My Oracle Support, stage the zip/extracted directory on this host, enter that path in PatchPilot, and run OPatch validation again." % patch_dir)

readme_path = ""
for candidate in unique([source_path] + candidates):
    try:
        readme_path = readme_from_candidate(candidate)
    except Exception:
        readme_path = ""
    if readme_path:
        break

java_home = os.environ.get("JAVA_HOME", "")
java = os.path.join(java_home, "bin", "java") if java_home else ""
if not java or not os.path.isfile(java):
    java = shutil.which("java") or ""
if not java:
    raise RuntimeError("Java was not found on the target host. Set JAVA_HOME or make java available in PATH.")

backup_path = ""
opatch_dir = os.path.join(oracle_home, "OPatch")
if os.path.isdir(opatch_dir):
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup_path = os.path.join(work_dir, "OPatch_%s.tar.gz" % stamp)
    subprocess.check_call(["tar", "-czf", backup_path, "-C", oracle_home, "OPatch"], timeout=600)

stamp = time.strftime("%Y%m%d%H%M%S")
upgrade_log = os.path.join(work_dir, "opatch_upgrade_%s.log" % stamp)
command = [java, "-jar", jar_path, "-silent", "oracle_home=%s" % oracle_home]
command_text = " ".join(shlex.quote(part) for part in command)

def emit(message):
    print(message)
    sys.stdout.flush()

with open(upgrade_log, "ab") as log_handle:
    header = [
        "OPatch upgrade started: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "Oracle home: %s" % oracle_home,
        "Source artifact: %s" % source_path,
        "Installer jar: %s" % jar_path,
        "Reference README: %s" % (readme_path or "not found"),
        "OPatch heap options %s: OPATCH_JRE_MEMORY_OPTIONS=%s" % (opatch_heap.get("action"), opatch_heap.get("effective")),
        "Command: %s" % command_text,
        "",
    ]
    for line in header:
        log_handle.write((line + "\n").encode("utf-8", "replace"))
        emit(line)
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    start = time.time()
    while True:
        if time.time() - start > 1200:
            proc.kill()
            raise RuntimeError("OPatch upgrade timed out after 1200 seconds. Log: %s" % upgrade_log)
        chunk = proc.stdout.readline()
        if chunk:
            log_handle.write(chunk)
            log_handle.flush()
            emit(chunk.decode("utf-8", "replace").rstrip())
        elif proc.poll() is not None:
            break
        else:
            time.sleep(0.2)
    returncode = proc.wait()

with open(upgrade_log, "rb") as handle:
    output = handle.read()[-1024 * 1024:].decode("utf-8", "replace")
if proc.returncode != 0:
    raise RuntimeError(output or "OPatch upgrade failed with exit code %s. Log: %s" % (returncode, upgrade_log))

opatch = os.path.join(oracle_home, "OPatch", "opatch")
version_output = subprocess.check_output([opatch, "version"], stderr=subprocess.STDOUT, timeout=60).decode("utf-8", "replace")
match = re.search(r"OPatch Version:\s*([0-9.]+)", version_output)
version = match.group(1) if match else "unknown"
payload = {
    "oracleHome": oracle_home,
    "jarPath": jar_path,
    "sourcePath": source_path,
    "readmePath": readme_path,
    "logPath": upgrade_log,
    "logTail": output[-20000:],
    "backupPath": backup_path,
    "opatchHeap": opatch_heap,
    "command": command_text,
    "output": output,
    "version": version,
    "versionOutput": version_output,
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return (
        script
        .replace("__PATCHSCOPE_OPATCH_JSON__", json.dumps(payload))
        .replace("__PATCHSCOPE_OPATCH_HEAP_HELPER__", REMOTE_OPATCH_HEAP_HELPER)
    )


def build_spb_run_script(body):
    install_type = str(body.get("installType") or "").lower()
    phase = str(body.get("phase") or "").lower()
    oracle_home = str(body.get("oracleHome") or "").strip()
    patch_path = str(body.get("patchPath") or "").strip()
    log_dir = str(body.get("logDir") or "").strip()
    extra_args_text = str(body.get("extraArgs") or "").strip()
    opatch_heap_options = sanitize_opatch_heap_options(body.get("opatchHeapOptions"))
    if install_type not in ("oam", "oig", "oud", "oid"):
        raise ValueError("SPBAT install type must be oam, oig, oud, or oid.")
    if phase not in ("prestop", "downtime", "poststart"):
        raise ValueError("SPBAT phase must be prestop, downtime, or poststart.")
    if not oracle_home or not patch_path or not log_dir:
        raise ValueError("SPBAT phase requires ORACLE_HOME, patch path, and log directory.")
    try:
        extra_args = shlex.split(extra_args_text) if extra_args_text else []
    except ValueError as error:
        raise ValueError("Additional SPBAT arguments are not valid shell-style arguments: %s" % error)
    spbat_dir = patch_path.rstrip("/") + "/tools/spbat/generic/SPBAT"
    command = (
        "set -e; "
        "if [ -z \"${{OPATCH_JRE_MEMORY_OPTIONS:-}}\" ]; then export OPATCH_JRE_MEMORY_OPTIONS={opatch_heap_options}; fi; "
        "mkdir -p {log_dir}; cd {spbat_dir}; "
        "./spbat.sh -type {install_type} -phase {phase} -mw_home {oracle_home} "
        "-spb_download_dir {patch_path} -log_dir {log_dir} -verbose true{extra_args}"
    ).format(
        opatch_heap_options=shlex.quote(opatch_heap_options),
        log_dir=shlex.quote(log_dir),
        spbat_dir=shlex.quote(spbat_dir),
        install_type=shlex.quote(install_type),
        phase=shlex.quote(phase),
        oracle_home=shlex.quote(oracle_home),
        patch_path=shlex.quote(patch_path),
        extra_args=(" " + " ".join(shlex.quote(arg) for arg in extra_args)) if extra_args else "",
    )
    return command


def build_spb_phase_stream_script(body):
    install_type = str(body.get("installType") or "").lower()
    phase = str(body.get("phase") or "").lower()
    oracle_home = str(body.get("oracleHome") or "").strip()
    patch_path = str(body.get("patchPath") or "").strip()
    log_dir = str(body.get("logDir") or "").strip()
    extra_args_text = str(body.get("extraArgs") or "").strip()
    opatch_heap_options = sanitize_opatch_heap_options(body.get("opatchHeapOptions"))
    if install_type not in ("oam", "oig", "oud", "oid"):
        raise ValueError("SPBAT install type must be oam, oig, oud, or oid.")
    if phase not in ("prestop", "downtime", "poststart"):
        raise ValueError("SPBAT phase must be prestop, downtime, or poststart.")
    if not oracle_home or not patch_path or not log_dir:
        raise ValueError("SPBAT phase requires ORACLE_HOME, patch path, and log directory.")
    payload = {
        "installType": install_type,
        "phase": phase,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "logDir": log_dir,
        "extraArgs": extra_args_text,
        "opatchHeapOptions": opatch_heap_options,
        "streamInitialLines": SPB_PHASE_STREAM_INITIAL_LINES,
        "heartbeatSeconds": SPB_PHASE_HEARTBEAT_SECONDS,
    }
    script = r'''
import json
import os
import re
import select
import shlex
import subprocess
import sys
import time

try:
    import html
except ImportError:
    import HTMLParser
    class _PatchPilotHtml(object):
        def unescape(self, value):
            return HTMLParser.HTMLParser().unescape(value)
    html = _PatchPilotHtml()

request = __PATCHSCOPE_SPB_PHASE_JSON__
install_type = request["installType"]
phase = request["phase"]
oracle_home = os.path.abspath(request["oracleHome"])
patch_dir = os.path.abspath(request["patchPath"])
log_dir = os.path.abspath(request["logDir"])
extra_args_text = request.get("extraArgs") or ""
stream_initial_lines = max(20, int(request.get("streamInitialLines") or 120))
heartbeat_seconds = max(15, int(request.get("heartbeatSeconds") or 60))
spbat_dir = os.path.join(patch_dir, "tools", "spbat", "generic", "SPBAT")
spbat_sh = os.path.join(spbat_dir, "spbat.sh")
__PATCHSCOPE_OPATCH_HEAP_HELPER__
opatch_heap = configure_opatch_heap(request.get("opatchHeapOptions"))

if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isdir(patch_dir):
    raise RuntimeError("SPB download directory does not exist: %s" % patch_dir)
if not os.path.isfile(spbat_sh):
    raise RuntimeError("SPBAT shell script was not found: %s" % spbat_sh)

try:
    extra_args = shlex.split(extra_args_text) if extra_args_text else []
except ValueError as error:
    raise RuntimeError("Additional SPBAT arguments are not valid shell-style arguments: %s" % error)

os.makedirs(log_dir, exist_ok=True)
stamp = time.strftime("%Y%m%d_%H%M%S")
phase_log = os.path.join(log_dir, "patchscope_spbat_%s_%s.log" % (phase, stamp))
command = [
    spbat_sh,
    "-type", install_type,
    "-phase", phase,
    "-mw_home", oracle_home,
    "-spb_download_dir", patch_dir,
    "-log_dir", log_dir,
    "-verbose", "true",
]
command.extend(extra_args)
command_text = " ".join(shlex.quote(part) for part in command)


def emit(message):
    print(message)
    sys.stdout.flush()


def find_latest_report():
    reports = []
    for root, dirs, files in os.walk(log_dir):
        dirs[:] = [name for name in dirs if name.lower() not in ("tmp", "cache")]
        for name in files:
            if name.lower().endswith((".html", ".htm")):
                path = os.path.join(root, name)
                try:
                    reports.append((os.path.getmtime(path), path))
                except Exception:
                    pass
    if not reports:
        return ""
    reports.sort(reverse=True)
    return reports[0][1]


def newest_log_hint():
    candidates = []
    for root, dirs, files in os.walk(log_dir):
        dirs[:] = [name for name in dirs if name.lower() not in ("tmp", "cache")]
        for name in files:
            if name.lower().endswith((".log", ".out", ".txt", ".html", ".htm")):
                path = os.path.join(root, name)
                try:
                    candidates.append((os.path.getmtime(path), os.path.getsize(path), path))
                except Exception:
                    pass
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    _, size, path = candidates[0]
    return "%s (%s bytes)" % (path, size)


def phase_log_hint():
    try:
        return "%s (%s bytes)" % (phase_log, os.path.getsize(phase_log))
    except Exception:
        return phase_log


def running_spbat_phase_processes():
    try:
        output = subprocess.check_output(["ps", "-eo", "pid=,args="], stderr=subprocess.STDOUT)
    except Exception:
        return []
    text = output.decode("utf-8", "replace") if hasattr(output, "decode") else str(output or "")
    matches = []
    own_pids = set([os.getpid(), os.getppid()])
    phase_pattern = re.compile(r"(?:^|\s)-phase\s+%s(?:\s|$)" % re.escape(phase), re.I)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        if pid in own_pids:
            continue
        command = parts[1]
        lower = command.lower()
        if "spbat.sh" not in lower:
            continue
        if not phase_pattern.search(command):
            continue
        if oracle_home.lower() not in lower and patch_dir.lower() not in lower and log_dir.lower() not in lower:
            continue
        matches.append({"pid": pid, "command": command[:700]})
    return matches


def should_stream_line(line, streamed_count):
    text = str(line or "").strip()
    if not text:
        return False
    if streamed_count < stream_initial_lines:
        return True
    return bool(re.search(r"\b(status|result|success|succeeded|successful|completed|failed|failure|error|exception|traceback|conflict|opatch succeeded|opatch failed|n-apply process)\b", text, re.I))


def read_text(path, limit=256 * 1024):
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read().decode("utf-8", "replace")


def report_summary(path):
    text = read_text(path, limit=1024 * 1024)
    if not text:
        return {"status": "", "lines": []}
    plain = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    plain = re.sub(r"<[^>]+>", "\n", plain)
    plain = html.unescape(plain)
    lines = [re.sub(r"\s+", " ", line).strip() for line in plain.splitlines()]
    lines = [line for line in lines if line]
    important = []
    status = ""
    for index, line in enumerate(lines):
        status_match = re.search(r"\bStatus\s*[-:]\s*([A-Za-z]+)", line, re.I)
        if status_match:
            status = status_match.group(1)
            important.append(line)
        if re.search(r"\b(fail|failed|failure|error|exception|conflict|missing|not found|manual intervention)\b", line, re.I):
            start = max(0, index - 1)
            end = min(len(lines), index + 2)
            important.extend(lines[start:end])
        if line.upper().startswith("RESULT:"):
            important.append(line)
    deduped = []
    seen = set()
    for line in important:
        if line not in seen:
            seen.add(line)
            deduped.append(line)
    return {"status": status, "lines": deduped[-20:]}


with open(phase_log, "ab") as log_handle:
    header = [
        "SPBAT %s started: %s" % (phase, time.strftime("%Y-%m-%d %H:%M:%S")),
        "Oracle home: %s" % oracle_home,
        "Patch download: %s" % patch_dir,
        "SPBAT log directory: %s" % log_dir,
        "OPatch heap options %s: OPATCH_JRE_MEMORY_OPTIONS=%s" % (opatch_heap.get("action"), opatch_heap.get("effective")),
        "PatchPilot phase log: %s" % phase_log,
        "Command: %s" % command_text,
        "",
    ]
    for line in header:
        log_handle.write((line + "\n").encode("utf-8", "replace"))
        emit(line)
    existing_spbat = running_spbat_phase_processes()
    if existing_spbat:
        details = "; ".join("pid %s: %s" % (item["pid"], item["command"]) for item in existing_spbat)
        raise RuntimeError("Another SPBAT %s process is already running for this target. PatchPilot will not start a duplicate phase run: %s" % (phase, details))
    proc = subprocess.Popen(command, cwd=spbat_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    fd = proc.stdout.fileno()
    last_heartbeat = time.time()
    start = time.time()
    streamed_lines = 0
    suppressed_lines = 0
    while True:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            chunk = os.read(fd, 4096)
            if chunk:
                log_handle.write(chunk)
                log_handle.flush()
                for line in chunk.decode("utf-8", "replace").splitlines():
                    if should_stream_line(line, streamed_lines):
                        emit(line)
                        streamed_lines += 1
                    else:
                        suppressed_lines += 1
            elif proc.poll() is not None:
                break
        elif proc.poll() is not None:
            break
        if time.time() - last_heartbeat > heartbeat_seconds:
            elapsed = int(time.time() - start)
            suppressed_note = ""
            if suppressed_lines:
                suppressed_note = " %s routine output line(s) captured in the phase log instead of the browser stream." % suppressed_lines
                suppressed_lines = 0
            emit("SPBAT %s still running after %ss. Phase log: %s.%s" % (phase, elapsed, phase_log_hint(), suppressed_note))
            last_heartbeat = time.time()
    returncode = proc.wait()

report_path = find_latest_report()
summary = report_summary(report_path)
phase_output = read_text(phase_log)
report_status = summary.get("status", "").lower()
failed_report = report_status in ("failed", "failure", "error")
succeeded_report = report_status in ("success", "succeeded", "successful", "passed", "completed")
failed_output = bool(re.search(r"\b(prestop|downtime|poststart).{0,40}(failed|failure|error)|\b(failed|failure|exception)\b", phase_output, re.I))
if returncode != 0 or failed_report:
    status = "failed"
elif succeeded_report:
    status = "succeeded"
else:
    status = "failed" if failed_output else "succeeded"
error_lines = summary.get("lines") or []
if status == "failed" and not error_lines:
    error_lines = [line.strip() for line in phase_output.splitlines() if re.search(r"\b(failed|failure|error|exception)\b", line, re.I)][-12:]
if status == "failed" and not error_lines:
    error_lines = ["SPBAT %s exited with return code %s. Review phase log: %s" % (phase, returncode, phase_log)]
    if report_path:
        error_lines.append("Latest SPBAT report: %s" % report_path)

payload = {
    "phase": phase,
    "installType": install_type,
    "extraArgs": extra_args_text,
    "command": command_text,
    "oracleHome": oracle_home,
    "logDir": log_dir,
    "opatchHeap": opatch_heap,
    "phaseLog": phase_log,
    "reportPath": report_path,
    "reportStatus": summary.get("status", ""),
    "summaryLines": summary.get("lines", [])[-20:],
    "returnCode": returncode,
    "status": status,
    "output": phase_output[-50000:],
    "error": "\n".join(error_lines[-12:]) if status == "failed" else "",
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return (
        script
        .replace("__PATCHSCOPE_SPB_PHASE_JSON__", json.dumps(payload))
        .replace("__PATCHSCOPE_OPATCH_HEAP_HELPER__", REMOTE_OPATCH_HEAP_HELPER)
    )


def build_oig_profile_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    domain_home = str(body.get("domainHome") or "").strip()
    patch_path = str(body.get("patchPath") or "").strip()
    log_dir = str(body.get("logDir") or "").strip()
    values = body.get("values") or {}
    save = bool(body.get("save"))
    if not oracle_home:
        raise ValueError("ORACLE_HOME is required for the OIG profile helper.")
    if not isinstance(values, dict):
        raise ValueError("OIG profile values must be a key/value object.")
    payload = {
        "oracleHome": oracle_home,
        "domainHome": domain_home,
        "patchPath": patch_path,
        "logDir": log_dir,
        "targetHost": str(body.get("targetHost") or body.get("host") or "").strip(),
        "values": {str(key): str(value) for key, value in values.items()},
        "save": save,
    }
    script = r'''
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

request = json.loads(__PATCHSCOPE_OIG_PROFILE_JSON__)
oracle_home = os.path.abspath(request.get("oracleHome") or "")
domain_home = os.path.abspath(request.get("domainHome") or "") if request.get("domainHome") else ""
patch_path = os.path.abspath(request.get("patchPath") or "") if request.get("patchPath") else ""
log_dir = os.path.abspath(request.get("logDir") or "") if request.get("logDir") else ""
target_host = request.get("targetHost") or ""
values = request.get("values") or {}
save = bool(request.get("save"))

profile_path = os.path.join(oracle_home, "idm", "server", "bin", "patch_oim_wls.profile")
script_path = os.path.join(oracle_home, "idm", "server", "bin", "patch_oim_wls.sh")
log_path = os.path.join(oracle_home, "idm", "server", "bin", "patch_oim_wls.log")

if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isfile(profile_path):
    raise RuntimeError("OIG profile file was not found: %s" % profile_path)

ASSIGN_RE = re.compile(r'^([ \t]*(?:export[ \t]+)?)([A-Za-z_][A-Za-z0-9_.-]*)([ \t]*=[ \t]*)(.*?)(\r?\n?)$')
COMMENT_ASSIGN_RE = re.compile(r'^([ \t]*#[ \t]*)([A-Za-z_][A-Za-z0-9_.-]*)([ \t]*=[ \t]*)(.*?)(\r?\n?)$')


def key_upper(key):
    return str(key or "").strip().upper()


def is_secret_key(key):
    upper = key_upper(key)
    return (
        "PASSWORD" in upper or
        "PASSWD" in upper or
        "SECRET" in upper or
        "CREDENTIAL" in upper or
        upper == "PWD" or
        upper.endswith("_PWD") or
        "_PWD_" in upper or
        upper.endswith("_PASS") or
        "_PASS_" in upper
    )


def strip_inline_comment(raw):
    return re.split(r"\s+#", str(raw or ""), 1)[0].strip()


def clean_value(raw):
    value = strip_inline_comment(raw)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def looks_filled(raw):
    value = clean_value(raw)
    low = value.lower()
    if not value:
        return False
    if value.startswith("<") and value.endswith(">"):
        return False
    if low in ("password", "passwd", "pwd", "changeme", "change_me", "replace_me", "todo", "null", "none"):
        return False
    if "replace" in low or "change this" in low:
        return False
    return True


def local_name(tag):
    return str(tag or "").split("}", 1)[-1]


def child_text(element, name):
    for child in list(element):
        if local_name(child.tag).lower() == name.lower():
            return (child.text or "").strip()
    return ""


def first_desc_text(root, name):
    for element in root.iter():
        if local_name(element.tag).lower() == name.lower():
            text = (element.text or "").strip()
            if text:
                return text
    return ""


def process_lines():
    try:
        output = subprocess.check_output(["ps", "-eo", "args="], stderr=subprocess.STDOUT, timeout=5)
        return output.decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def detect_java_home():
    for line in process_lines():
        if oracle_home not in line and (not domain_home or domain_home not in line):
            continue
        match = re.search(r"(/\S+/(?:jre/)?bin/java)\b", line)
        if not match:
            continue
        java_bin = match.group(1)
        if java_bin.endswith("/jre/bin/java"):
            return java_bin[:-len("/jre/bin/java")]
        if java_bin.endswith("/bin/java"):
            return java_bin[:-len("/bin/java")]
    candidates = [
        os.path.join(os.path.dirname(oracle_home), "IDMJDK", "jdk-17.0.12"),
        os.path.join(os.path.dirname(os.path.dirname(oracle_home)), "IDMJDK", "jdk-17.0.12"),
        os.path.join(oracle_home, "jdk"),
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return ""


def parse_domain_servers():
    config_path = os.path.join(domain_home, "config", "config.xml") if domain_home else ""
    servers = []
    if not config_path or not os.path.isfile(config_path):
        return {"configPath": config_path, "servers": servers}
    try:
        root = ET.parse(config_path).getroot()
    except Exception:
        return {"configPath": config_path, "servers": servers}
    for element in root.iter():
        if local_name(element.tag).lower() != "server":
            continue
        name = child_text(element, "name")
        if not name:
            continue
        servers.append({
            "name": name,
            "listenAddress": child_text(element, "listen-address"),
            "listenPort": child_text(element, "listen-port"),
            "sslListenPort": child_text(element, "ssl-listen-port"),
        })
    return {"configPath": config_path, "servers": servers}


def server_by_role(role):
    servers = DOMAIN_INFO.get("servers") or []
    if role == "admin":
        for server in servers:
            if server.get("name", "").lower() == "adminserver":
                return server
    tokens = {
        "oim": ("oim", "oig"),
        "soa": ("soa",),
    }.get(role, ())
    for server in servers:
        low = server.get("name", "").lower()
        if any(token in low for token in tokens):
            return server
    return {}


def server_host_port(role, default_port):
    server = server_by_role(role)
    host = server.get("listenAddress") or target_host
    port = server.get("listenPort") or str(default_port)
    return host, port


def parse_jdbc_url(url):
    text = str(url or "").strip()
    result = {}
    match = re.search(r"@//([^/:)]+):(\d+)[/:]([^?\s)]+)", text)
    if match:
        result.update({"host": match.group(1), "port": match.group(2), "serviceName": match.group(3)})
        return result
    match = re.search(r"@([^/:)]+):(\d+):([^?\s)]+)", text)
    if match:
        result.update({"host": match.group(1), "port": match.group(2), "serviceName": match.group(3)})
        return result
    host = re.search(r"\bHOST\s*=\s*([^)]+)", text, re.I)
    port = re.search(r"\bPORT\s*=\s*(\d+)", text, re.I)
    service = re.search(r"\bSERVICE_NAME\s*=\s*([^)]+)", text, re.I)
    sid = re.search(r"\bSID\s*=\s*([^)]+)", text, re.I)
    if host:
        result["host"] = host.group(1).strip()
    if port:
        result["port"] = port.group(1).strip()
    if service or sid:
        result["serviceName"] = (service or sid).group(1).strip()
    return result


def parse_jdbc_file(path):
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    properties = {}
    for prop in root.iter():
        if local_name(prop.tag).lower() != "property":
            continue
        prop_name = child_text(prop, "name")
        prop_value = child_text(prop, "value")
        if prop_name:
            properties[prop_name.lower()] = prop_value
    names = []
    for tag in ("name", "jndi-name"):
        value = first_desc_text(root, tag)
        if value:
            names.append(value)
    url = first_desc_text(root, "url")
    db = parse_jdbc_url(url)
    db["user"] = properties.get("user") or properties.get("username") or ""
    return {
        "path": path,
        "nameText": " ".join(names),
        "url": url,
        "properties": properties,
        "db": db,
    }


def datasource_inventory():
    base = os.path.join(domain_home, "config", "jdbc") if domain_home else ""
    datasources = []
    if not base or not os.path.isdir(base):
        return datasources
    for root_dir, _, files in os.walk(base):
        for filename in files:
            if not filename.lower().endswith(".xml"):
                continue
            datasource = parse_jdbc_file(os.path.join(root_dir, filename))
            if datasource:
                datasources.append(datasource)
    return datasources


def datasource_score(datasource, role):
    text = "%s %s %s %s" % (
        datasource.get("nameText", ""),
        datasource.get("path", ""),
        datasource.get("url", ""),
        datasource.get("db", {}).get("user", ""),
    )
    low = text.lower()
    score = 0
    if role == "operations":
        for token in ("operations", "oim", "oig", "oimjdbc", "oim_ops"):
            if token in low:
                score += 4
        if "mds" in low:
            score -= 6
    elif role == "mds":
        if "mds" in low:
            score += 8
        if "operations" in low:
            score -= 4
    return score


def datasource_for(role):
    scored = [(datasource_score(item, role), item) for item in DATASOURCES]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return {}
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def detect_bip_domain_home():
    candidates = []
    if domain_home:
        candidates.append(os.path.dirname(domain_home))
    candidates.extend([
        "/opt/oracle/user_projects/domains",
        "/u01/oracle/user_projects/domains",
        "/u01/app/oracle/user_projects/domains",
    ])
    for base in candidates:
        if not base or not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except Exception:
            continue
        for name in names:
            path = os.path.join(base, name)
            if "bip" in name.lower() and os.path.isfile(os.path.join(path, "config", "config.xml")):
                return path
    return ""


DOMAIN_INFO = parse_domain_servers()
DATASOURCES = datasource_inventory()
OPERATIONS_DS = datasource_for("operations")
MDS_DS = datasource_for("mds")
BIP_DOMAIN_HOME = detect_bip_domain_home()
JAVA_HOME = detect_java_home()


def suggestion_for(key):
    upper = key_upper(key)
    db_key = upper.replace("_", ".")
    if is_secret_key(key):
        return {"value": "", "source": ""}
    if upper in ("ORACLE_HOME", "OIG_ORACLE_HOME", "IAM_ORACLE_HOME", "IAM_HOME"):
        return {"value": oracle_home, "source": "selected ORACLE_HOME"}
    if upper == "JAVA_HOME":
        return {"value": JAVA_HOME, "source": "selected-home Java process or common IDMJDK path"}
    if upper == "ANT_HOME":
        path = os.path.join(oracle_home, "oracle_common", "modules", "thirdparty", "org.apache.ant", "apache-ant")
        return {"value": path, "source": "ORACLE_HOME oracle_common ant path"}
    if upper in ("MW_HOME", "MIDDLEWARE_HOME", "FMW_HOME", "MIDDLEWARE_ORACLE_HOME"):
        return {"value": oracle_home, "source": "selected ORACLE_HOME"}
    if upper == "OIM_ORACLE_HOME":
        return {"value": os.path.join(oracle_home, "idm"), "source": "selected ORACLE_HOME/idm"}
    if upper == "SOA_HOME":
        return {"value": os.path.join(oracle_home, "soa"), "source": "selected ORACLE_HOME/soa"}
    if upper == "WEBLOGIC.SERVER.DIR":
        return {"value": os.path.join(oracle_home, "wlserver"), "source": "selected ORACLE_HOME/wlserver"}
    if upper in ("WL_HOME", "WEBLOGIC_HOME"):
        wl_home = os.path.join(oracle_home, "wlserver")
        return {"value": wl_home if os.path.isdir(wl_home) else "", "source": "selected ORACLE_HOME/wlserver"}
    if upper in ("DOMAIN_HOME", "OIM_DOMAIN_HOME", "OIG_DOMAIN_HOME", "WLS_DOMAIN_HOME"):
        return {"value": domain_home, "source": "selected domain home"}
    if upper in ("SPB_DOWNLOAD_DIR", "PATCH_TOP", "PATCH_DIR", "PATCH_HOME"):
        return {"value": patch_path, "source": "selected patch directory"}
    if upper in ("LOG_DIR", "PATCH_LOG_DIR"):
        return {"value": log_dir, "source": "selected SPBAT log directory"}
    if upper == "IS_BIP_CONFIGURED":
        return {"value": "true" if BIP_DOMAIN_HOME else "false", "source": "BIP domain discovery"}
    if upper == "BIP_DOMAIN_HOME":
        return {"value": BIP_DOMAIN_HOME, "source": "BIP domain discovery"}
    if upper in ("WEBLOGIC_USER", "WLS_USER", "ADMIN_USER", "ADMIN_USERNAME", "WEBLOGIC_USERNAME"):
        return {"value": "weblogic", "source": "OIG profile default"}
    if upper in ("XELSYSADM_USER", "XELSYSADM_USERNAME", "OIM_ADMIN_USER", "OIM_ADMIN_USERNAME"):
        return {"value": "xelsysadm", "source": "OIG profile default"}
    if upper in ("OIM_USERNAME", "OIG_USERNAME"):
        return {"value": "xelsysadm", "source": "OIG profile default"}
    if upper in ("ADMIN_URL", "WLS_ADMIN_URL", "WLST_ADMIN_URL", "WLS_SERVERURL"):
        host, port = server_host_port("admin", 7001)
        return {"value": "t3://%s:%s" % (host, port) if host and port else "", "source": "DOMAIN_HOME/config/config.xml AdminServer"}
    if upper == "OIM_SERVERURL":
        host, port = server_host_port("oim", 14000)
        return {"value": "t3://%s:%s" % (host, port) if host and port else "", "source": "DOMAIN_HOME/config/config.xml OIM server"}
    if upper == "SOA_HOST":
        host, _ = server_host_port("soa", 8001)
        return {"value": host, "source": "DOMAIN_HOME/config/config.xml SOA server"}
    if upper == "SOA_PORT":
        _, port = server_host_port("soa", 8001)
        return {"value": port, "source": "DOMAIN_HOME/config/config.xml SOA server"}
    if db_key.startswith("OPERATIONSDB."):
        field = db_key.split(".", 1)[1].lower()
        mapped = "serviceName" if field == "servicename" else field
        return {"value": OPERATIONS_DS.get("db", {}).get(mapped, ""), "source": OPERATIONS_DS.get("path", "")}
    if db_key.startswith("MDSDB."):
        field = db_key.split(".", 1)[1].lower()
        mapped = "serviceName" if field == "servicename" else field
        return {"value": MDS_DS.get("db", {}).get(mapped, ""), "source": MDS_DS.get("path", "")}
    if upper == "ATPD":
        db_text = "%s %s" % (OPERATIONS_DS.get("url", ""), MDS_DS.get("url", ""))
        return {"value": "TRUE" if "TNS_ADMIN" in db_text.upper() else "FALSE", "source": "datasource URL"}
    if upper == "OPSS_CUSTOMIZATIONS_PRESENT":
        return {"value": "false", "source": "default unless customer confirms OPSS customizations"}
    return {"value": "", "source": ""}


def quote_shell_value(value):
    value = str(value or "")
    if not value:
        return '""'
    if re.search(r"[\s#'\"$`\\]", value):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return value


def parse_entries(lines):
    entries = []
    for index, line in enumerate(lines):
        match = ASSIGN_RE.match(line)
        commented = False
        if not match:
            match = COMMENT_ASSIGN_RE.match(line)
            commented = True
        if not match:
            continue
        key = match.group(2)
        secret = is_secret_key(key)
        if commented and not secret:
            continue
        current = clean_value(match.group(4))
        suggestion = suggestion_for(key)
        suggested = "" if secret else suggestion.get("value", "")
        entries.append({
            "line": index + 1,
            "key": key,
            "secret": secret,
            "commented": commented,
            "runtimePrompt": commented and secret,
            "filled": (looks_filled(match.group(4)) and not commented) if secret else bool(current),
            "value": "" if secret else current,
            "suggestedValue": suggested,
            "suggestedSource": "" if secret else suggestion.get("source", ""),
            "needsUpdate": bool((not secret) and suggested and suggested != current),
        })
    return entries


with open(profile_path, "rb") as handle:
    lines = handle.read().decode("utf-8", "replace").splitlines(True)

updated_keys = []
backup_path = ""
backup_created = False
backup_already_existed = False
if save:
    backup_path = "%s_backup" % profile_path
    if os.path.exists(backup_path):
        backup_already_existed = True
    else:
        shutil.copy2(profile_path, backup_path)
        backup_created = True
    for index, line in enumerate(lines):
        match = ASSIGN_RE.match(line)
        if not match:
            continue
        key = match.group(2)
        if is_secret_key(key) or key not in values:
            continue
        new_value = str(values.get(key) or "").strip()
        if not new_value:
            continue
        current = clean_value(match.group(4))
        if new_value == current:
            continue
        ending = match.group(5) or "\n"
        lines[index] = "%s%s%s%s%s" % (match.group(1), key, match.group(3), quote_shell_value(new_value), ending)
        updated_keys.append(key)
    with open(profile_path, "wb") as handle:
        handle.write("".join(lines).encode("utf-8"))

entries = parse_entries(lines)
secret_entries = [entry for entry in entries if entry.get("secret")]
missing_secret_keys = [entry.get("key") for entry in secret_entries if not entry.get("filled")]
runtime_prompt_secret_keys = [entry.get("key") for entry in secret_entries if entry.get("runtimePrompt")]
suggested_keys = [entry.get("key") for entry in entries if not entry.get("secret") and entry.get("suggestedValue") and entry.get("suggestedValue") != entry.get("value")]
payload = {
    "status": "saved" if save else "inspected",
    "oracleHome": oracle_home,
    "domainHome": domain_home,
    "profilePath": profile_path,
    "scriptPath": script_path,
    "logPath": log_path,
    "scriptExists": os.path.isfile(script_path),
    "entries": entries,
    "nonSecretCount": len([entry for entry in entries if not entry.get("secret")]),
    "secretCount": len(secret_entries),
    "missingSecretKeys": missing_secret_keys,
    "runtimePromptSecretKeys": runtime_prompt_secret_keys,
    "suggestedKeys": suggested_keys,
    "updatedKeys": updated_keys,
    "backupPath": backup_path,
    "backupCreated": backup_created,
    "backupAlreadyExisted": backup_already_existed,
    "discovery": {
        "domainConfig": DOMAIN_INFO.get("configPath", ""),
        "datasourceFiles": [item.get("path", "") for item in DATASOURCES],
        "operationsDatasource": OPERATIONS_DS.get("path", ""),
        "mdsDatasource": MDS_DS.get("path", ""),
        "bipDomainHome": BIP_DOMAIN_HOME,
        "javaHome": JAVA_HOME,
    },
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_OIG_PROFILE_JSON__", json.dumps(json.dumps(payload)))


def build_oig_postinstall_stream_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    domain_home = str(body.get("domainHome") or "").strip()
    if not oracle_home:
        raise ValueError("ORACLE_HOME is required to run the OIG postinstall script.")
    payload = {
        "oracleHome": oracle_home,
        "domainHome": domain_home,
    }
    script = r'''
import glob
import json
import os
import re
import select
import subprocess
import sys
import time

request = json.loads(__PATCHSCOPE_OIG_SCRIPT_JSON__)
oracle_home = os.path.abspath(request.get("oracleHome") or "")
domain_home = os.path.abspath(request.get("domainHome") or "") if request.get("domainHome") else ""
script_dir = os.path.join(oracle_home, "idm", "server", "bin")
profile_path = os.path.join(script_dir, "patch_oim_wls.profile")
script_path = os.path.join(script_dir, "patch_oim_wls.sh")
default_log_path = os.path.join(script_dir, "patch_oim_wls.log")

if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isfile(profile_path):
    raise RuntimeError("OIG profile file was not found: %s" % profile_path)
if not os.path.isfile(script_path):
    raise RuntimeError("OIG postinstall script was not found: %s" % script_path)

ASSIGN_RE = re.compile(r'^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*=[ \t]*(.*?)(?:\r?\n)?$')
COMMENT_ASSIGN_RE = re.compile(r'^[ \t]*#[ \t]*([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*=[ \t]*(.*?)(?:\r?\n)?$')


def emit(message):
    print(message)
    sys.stdout.flush()


def key_upper(key):
    return str(key or "").strip().upper()


def is_secret_key(key):
    upper = key_upper(key)
    return (
        "PASSWORD" in upper or
        "PASSWD" in upper or
        "SECRET" in upper or
        "CREDENTIAL" in upper or
        upper == "PWD" or
        upper.endswith("_PWD") or
        "_PWD_" in upper or
        upper.endswith("_PASS") or
        "_PASS_" in upper
    )


def strip_inline_comment(raw):
    return re.split(r"\s+#", str(raw or ""), 1)[0].strip()


def clean_value(raw):
    value = strip_inline_comment(raw)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def looks_filled(raw):
    value = clean_value(raw)
    low = value.lower()
    if not value:
        return False
    if value.startswith("<") and value.endswith(">"):
        return False
    if low in ("password", "passwd", "pwd", "changeme", "change_me", "replace_me", "todo", "null", "none"):
        return False
    if "replace" in low or "change this" in low:
        return False
    return True


def missing_secret_keys():
    missing = []
    with open(profile_path, "rb") as handle:
        text = handle.read().decode("utf-8", "replace")
    for line in text.splitlines():
        match = ASSIGN_RE.match(line)
        if match:
            key = match.group(1)
            if is_secret_key(key) and not looks_filled(match.group(2)):
                missing.append(key)
            continue
        commented = COMMENT_ASSIGN_RE.match(line)
        if not commented:
            continue
        key = commented.group(1)
        if is_secret_key(key):
            missing.append(key)
    return missing


def newest_oig_log():
    candidates = []
    patterns = [
        os.path.join(script_dir, "patch_oim_wls*.log"),
        os.path.join(script_dir, "patch_oim_wls*.out"),
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                candidates.append((os.path.getmtime(path), path))
            except Exception:
                pass
    if not candidates and os.path.isfile(default_log_path):
        try:
            candidates.append((os.path.getmtime(default_log_path), default_log_path))
        except Exception:
            pass
    if not candidates:
        return default_log_path
    candidates.sort(reverse=True)
    return candidates[0][1]


def read_from(path, start=0):
    if not path or not os.path.isfile(path):
        return "", start
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if start > size:
            start = 0
        handle.seek(start)
        data = handle.read()
        return data.decode("utf-8", "replace"), handle.tell()


def tail_text(path, limit=50000):
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read().decode("utf-8", "replace")


missing = missing_secret_keys()
if missing:
    payload = {
        "status": "failed",
        "oracleHome": oracle_home,
        "domainHome": domain_home,
        "profilePath": profile_path,
        "scriptPath": script_path,
        "logPath": newest_oig_log(),
        "missingSecretKeys": missing,
        "returnCode": None,
        "error": "Password fields are blank, placeholders, or commented for runtime prompts in patch_oim_wls.profile: %s. PatchPilot cannot answer interactive password prompts; fill them in the server-side profile before running from PatchPilot, or run patch_oim_wls.sh manually in a terminal and confirm manual completion." % ", ".join(missing),
    }
    print("__PATCHSCOPE_JSON_START__")
    print(json.dumps(payload))
    print("__PATCHSCOPE_JSON_END__")
    sys.exit(0)

chmod_applied = False
if not os.access(script_path, os.X_OK):
    try:
        subprocess.check_call(["chmod", "u+x", script_path])
        chmod_applied = True
        emit("PatchPilot added executable permission to %s." % script_path)
    except Exception as error:
        raise RuntimeError("patch_oim_wls.sh is not executable and PatchPilot could not chmod u+x %s: %s" % (script_path, error))

command = [script_path]
command_text = "./%s" % os.path.basename(script_path)
emit("PatchPilot OIG profile: %s" % profile_path)
emit("PatchPilot OIG script: %s" % script_path)
emit("PatchPilot OIG log: %s" % newest_oig_log())
emit("$ cd %s && %s" % (script_dir, command_text))

proc = subprocess.Popen(command, cwd=script_dir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
fd = proc.stdout.fileno()
log_path = newest_oig_log()
log_pos = os.path.getsize(log_path) if os.path.isfile(log_path) else 0
captured = []
last_heartbeat = time.time()
start = time.time()

while True:
    ready, _, _ = select.select([fd], [], [], 0.5)
    if ready:
        chunk = os.read(fd, 4096)
        if chunk:
            text = chunk.decode("utf-8", "replace")
            captured.append(text)
            for line in text.splitlines():
                emit(line)
        elif proc.poll() is not None:
            break
    current_log = newest_oig_log()
    if current_log != log_path:
        log_path = current_log
        log_pos = 0
        emit("PatchPilot OIG log: %s" % log_path)
    log_delta, log_pos = read_from(log_path, log_pos)
    if log_delta:
        captured.append(log_delta)
        for line in log_delta.splitlines():
            emit("log: %s" % line)
    if proc.poll() is not None:
        break
    if time.time() - last_heartbeat > 10:
        emit("OIG postinstall script still running after %ss. Log: %s" % (int(time.time() - start), log_path))
        last_heartbeat = time.time()

rest = proc.stdout.read()
if rest:
    text = rest.decode("utf-8", "replace")
    captured.append(text)
    for line in text.splitlines():
        emit(line)
log_delta, log_pos = read_from(log_path, log_pos)
if log_delta:
    captured.append(log_delta)
    for line in log_delta.splitlines():
        emit("log: %s" % line)
returncode = proc.wait()
output = "".join(captured)
status = "succeeded" if returncode == 0 else "failed"
payload = {
    "status": status,
    "oracleHome": oracle_home,
    "domainHome": domain_home,
    "profilePath": profile_path,
    "scriptPath": script_path,
    "logPath": log_path,
    "command": command_text,
    "chmodApplied": chmod_applied,
    "returnCode": returncode,
    "missingSecretKeys": [],
    "output": output[-50000:],
    "logTail": tail_text(log_path),
    "error": "" if status == "succeeded" else "patch_oim_wls.sh failed with return code %s. Review log: %s" % (returncode, log_path),
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_OIG_SCRIPT_JSON__", json.dumps(json.dumps(payload)))


def build_oig_log_tail_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    if not oracle_home:
        raise ValueError("ORACLE_HOME is required to tail the OIG postinstall log.")
    payload = {
        "oracleHome": oracle_home,
    }
    script = r'''
import glob
import json
import os

request = json.loads(__PATCHSCOPE_OIG_LOG_JSON__)
oracle_home = os.path.abspath(request.get("oracleHome") or "")
script_dir = os.path.join(oracle_home, "idm", "server", "bin")
profile_path = os.path.join(script_dir, "patch_oim_wls.profile")
script_path = os.path.join(script_dir, "patch_oim_wls.sh")
default_log_path = os.path.join(script_dir, "patch_oim_wls.log")

if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isdir(script_dir):
    raise RuntimeError("OIG idm/server/bin directory was not found: %s" % script_dir)


def newest_oig_log():
    candidates = []
    patterns = [
        os.path.join(script_dir, "patch_oim_wls*.log"),
        os.path.join(script_dir, "patch_oim_wls*.out"),
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                candidates.append((os.path.getmtime(path), path))
            except Exception:
                pass
    if not candidates and os.path.isfile(default_log_path):
        try:
            candidates.append((os.path.getmtime(default_log_path), default_log_path))
        except Exception:
            pass
    if not candidates:
        return default_log_path
    candidates.sort(reverse=True)
    return candidates[0][1]


def tail_text(path, limit=60000):
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read().decode("utf-8", "replace")


log_path = newest_oig_log()
exists = os.path.isfile(log_path)
payload = {
    "status": "found" if exists else "missing",
    "oracleHome": oracle_home,
    "profilePath": profile_path,
    "scriptPath": script_path,
    "logPath": log_path,
    "profileExists": os.path.isfile(profile_path),
    "scriptExists": os.path.isfile(script_path),
    "scriptExecutable": os.access(script_path, os.X_OK) if os.path.isfile(script_path) else False,
    "logTail": tail_text(log_path),
    "error": "" if exists else "No patch_oim_wls log or output file was found under %s yet." % script_dir,
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_OIG_LOG_JSON__", json.dumps(json.dumps(payload)))


def build_shutdown_stop_script(body):
    home = body.get("home") or {}
    services = body.get("services") or []
    target = {
        "oracleHome": str(home.get("oracleHome") or body.get("oracleHome") or "").strip(),
        "domainHome": str(home.get("domainHome") or body.get("domainHome") or "").strip(),
        "instanceHome": str(home.get("instanceHome") or body.get("instanceHome") or "").strip(),
        "services": services,
    }
    if not target["oracleHome"]:
        raise ValueError("Selected ORACLE_HOME is required for service shutdown.")
    if not isinstance(services, list) or not services:
        raise ValueError("At least one selected running service is required for shutdown.")
    script = r'''
import json
import os
import re
import subprocess
import time

request = __PATCHSCOPE_SHUTDOWN_JSON__


def clean(value):
    value = str(value or "").strip()
    if value.lower() in ("not discovered", "not applicable", "none", "null"):
        return ""
    return value


oracle_home = os.path.abspath(clean(request.get("oracleHome")))
domain_home = os.path.abspath(clean(request.get("domainHome"))) if clean(request.get("domainHome")) else ""
instance_home = os.path.abspath(clean(request.get("instanceHome"))) if clean(request.get("instanceHome")) else ""
services = request.get("services") or []

if not oracle_home or not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)


def service_name(service):
    return str(service.get("service") or service.get("key") or "Oracle service")


def category(service):
    return str(service.get("category") or "")


def group_for(service):
    cat = category(service).lower()
    name = service_name(service).lower()
    if "node manager" in cat or "nodemanager" in name:
        return "nodeManager"
    if "weblogic" in cat and name == "adminserver":
        return "adminServer"
    if "weblogic" in cat:
        return "managedServer"
    if "oracle internet directory" in cat or "oracle http server" in cat or "oracle unified directory" in cat or "derby" in cat:
        return "systemComponent"
    return "other"


order = {"systemComponent": 10, "managedServer": 20, "adminServer": 30, "nodeManager": 40, "other": 50}
selected = sorted(services, key=lambda item: (order.get(group_for(item), 50), service_name(item)))


def executable(path):
    return path and os.path.isfile(path) and os.access(path, os.X_OK)


def output_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def component_names(*relative_roots):
    names = []
    for relative in relative_roots:
        base = os.path.join(domain_home, relative) if domain_home else ""
        if not base or not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isdir(path) and not name.startswith("."):
                names.append(name)
    result = []
    seen = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def add_command(commands, label, command, cwd="", timeout=300):
    key = tuple(command)
    if key in {tuple(item["command"]) for item in commands}:
        return
    commands.append({"label": label, "command": command, "cwd": cwd or None, "timeout": timeout})


commands = []
manual = []
oid_stop_added = False

for service in selected:
    name = service_name(service)
    cat = category(service).lower()
    group = group_for(service)
    if group == "systemComponent" and "oracle internet directory" in cat:
        opmnctl = os.path.join(oracle_home, "bin", "opmnctl")
        stop_component = os.path.join(domain_home, "bin", "stopComponent.sh") if domain_home else ""
        if executable(opmnctl):
            if not oid_stop_added:
                add_command(commands, "Oracle Internet Directory components", [opmnctl, "stopall"], cwd=oracle_home, timeout=600)
                oid_stop_added = True
        elif executable(stop_component):
            names = component_names(
                "config/fmwconfig/components/OID",
                "config/fmwconfig/components/OIDComponent",
                "config/fmwconfig/components/OracleInternetDirectory",
            )
            if names:
                for component in names:
                    add_command(commands, "Oracle Internet Directory component %s" % component, [stop_component, component], cwd=domain_home, timeout=600)
            else:
                manual.append({"service": name, "reason": "DOMAIN_HOME/bin/stopComponent.sh exists, but no OID component name was found under DOMAIN_HOME/config/fmwconfig/components."})
        else:
            manual.append({"service": name, "reason": "Neither ORACLE_HOME/bin/opmnctl nor DOMAIN_HOME/bin/stopComponent.sh was found or executable."})
    elif group == "systemComponent" and "oracle http server" in cat:
        stop_component = os.path.join(domain_home, "bin", "stopComponent.sh") if domain_home else ""
        component = name.replace("OHS", "").strip()
        if executable(stop_component) and component:
            add_command(commands, name, [stop_component, component], cwd=domain_home, timeout=300)
        else:
            manual.append({"service": name, "reason": "stopComponent.sh or OHS component name was not available."})
    elif group == "systemComponent" and "oracle unified directory" in cat:
        stop_ds = os.path.join(instance_home, "bin", "stop-ds") if instance_home else ""
        if executable(stop_ds):
            add_command(commands, name, [stop_ds], cwd=instance_home, timeout=300)
        else:
            manual.append({"service": name, "reason": "INSTANCE_HOME/bin/stop-ds was not found or executable."})
    elif group == "systemComponent" and "derby" in cat:
        stop_derby = os.path.join(domain_home, "bin", "stopDerby.sh") if domain_home else ""
        if executable(stop_derby):
            add_command(commands, name, [stop_derby], cwd=domain_home, timeout=300)
        else:
            manual.append({"service": name, "reason": "DOMAIN_HOME/bin/stopDerby.sh was not found; Derby shutdown requires the domain-specific Derby stop command."})
    elif group == "managedServer":
        script = os.path.join(domain_home, "bin", "stopManagedWebLogic.sh") if domain_home else ""
        if executable(script):
            add_command(commands, name, [script, name], cwd=domain_home, timeout=600)
        else:
            manual.append({"service": name, "reason": "DOMAIN_HOME/bin/stopManagedWebLogic.sh was not found or executable."})
    elif group == "adminServer":
        script = os.path.join(domain_home, "bin", "stopWebLogic.sh") if domain_home else ""
        if executable(script):
            add_command(commands, "AdminServer", [script], cwd=domain_home, timeout=600)
        else:
            manual.append({"service": name, "reason": "DOMAIN_HOME/bin/stopWebLogic.sh was not found or executable."})
    elif group == "nodeManager":
        script = os.path.join(domain_home, "bin", "stopNodeManager.sh") if domain_home else ""
        if executable(script):
            add_command(commands, name, [script], cwd=domain_home, timeout=300)
        else:
            manual.append({"service": name, "reason": "DOMAIN_HOME/bin/stopNodeManager.sh was not found or executable."})
    else:
        manual.append({"service": name, "reason": "No safe automatic stop command is known for this service type."})

results = []
for item in commands:
    start = time.time()
    try:
        proc = subprocess.run(
            item["command"],
            cwd=item["cwd"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=item["timeout"],
            universal_newlines=True,
        )
        output = output_text(proc.stdout)[-20000:]
        results.append({
            "label": item["label"],
            "command": " ".join(item["command"]),
            "returnCode": proc.returncode,
            "durationSeconds": round(time.time() - start, 1),
            "output": output,
            "status": "succeeded" if proc.returncode == 0 else "failed",
        })
    except subprocess.TimeoutExpired as error:
        results.append({
            "label": item["label"],
            "command": " ".join(item["command"]),
            "returnCode": -1,
            "durationSeconds": item["timeout"],
            "output": output_text(error.stdout)[-20000:],
            "status": "timeout",
        })
    except Exception as error:
        results.append({
            "label": item["label"],
            "command": " ".join(item["command"]),
            "returnCode": -2,
            "durationSeconds": round(time.time() - start, 1),
            "output": str(error),
            "status": "failed",
        })

status = "succeeded"
if any(item["status"] != "succeeded" for item in results):
    status = "failed"
elif manual:
    status = "partial"

payload = {
    "status": status,
    "oracleHome": oracle_home,
    "domainHome": domain_home,
    "instanceHome": instance_home,
    "results": results,
    "manual": manual,
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_SHUTDOWN_JSON__", json.dumps(target))


def build_shutdown_kill_script(body):
    home = body.get("home") or {}
    services = body.get("services") or []
    target = {
        "oracleHome": str(home.get("oracleHome") or body.get("oracleHome") or "").strip(),
        "domainHome": str(home.get("domainHome") or body.get("domainHome") or "").strip(),
        "instanceHome": str(home.get("instanceHome") or body.get("instanceHome") or "").strip(),
        "services": services,
    }
    if not target["oracleHome"]:
        raise ValueError("Selected ORACLE_HOME is required for force stop.")
    if not isinstance(services, list) or not services:
        raise ValueError("At least one selected running service is required for force stop.")
    script = r'''
import json
import os
import re
import signal
import subprocess
import time

request = __PATCHSCOPE_KILL_JSON__


def clean(value):
    value = str(value or "").strip()
    if value.lower() in ("not discovered", "not applicable", "none", "null"):
        return ""
    return value


paths = [
    ("ORACLE_HOME", clean(request.get("oracleHome"))),
    ("DOMAIN_HOME", clean(request.get("domainHome"))),
    ("INSTANCE_HOME", clean(request.get("instanceHome"))),
]
paths = [(scope, os.path.normpath(path)) for scope, path in paths if path]
services = request.get("services") or []
selected_pids = set()
selected_keys = set()
for service in services:
    selected_keys.add(str(service.get("key") or ""))
    for pid in service.get("pids") or []:
        try:
            selected_pids.add(int(pid))
        except Exception:
            pass

if not paths:
    raise RuntimeError("No selected ORACLE_HOME, DOMAIN_HOME, or INSTANCE_HOME path was provided.")
if not selected_pids:
    raise RuntimeError("No process ids were available for the selected services. Recheck shutdown and try again.")


def path_in_command(command, path):
    if not path:
        return False
    normalized = os.path.normpath(path).rstrip(os.sep)
    if not normalized:
        return False
    for match in re.finditer(re.escape(normalized), command):
        end = match.end()
        if end == len(command) or command[end] in "/ \t:=,;\"')":
            return True
    return False


def is_patching_utility_process(command):
    lower = command.lower()
    utility_tokens = [
        "/opatch/",
        " oracle/opatch/opatch ",
        " oracle.opatch.",
        "-dopatch.",
        "/cfgtoollogs/opatch",
        "orainstaller.jar",
        "installer-launch.jar",
        "oui/modules",
        "patchpilot",
        "patchscope",
    ]
    return any(token in lower for token in utility_tokens)


def pid_command_map():
    output = subprocess.check_output(["ps", "-eo", "pid=,args="], stderr=subprocess.STDOUT, timeout=10).decode("utf-8", "replace")
    result = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        result[pid] = parts[1]
    return result


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


current = pid_command_map()
eligible = []
skipped = []
for pid in sorted(selected_pids):
    command = current.get(pid, "")
    if not command:
        skipped.append({"pid": pid, "reason": "Process is no longer running."})
        continue
    if is_patching_utility_process(command):
        skipped.append({"pid": pid, "reason": "PatchPilot ignored an OPatch/OUI utility process; it is not a service shutdown target."})
        continue
    matched = [scope for scope, path in paths if path_in_command(command, path)]
    if not matched:
        skipped.append({"pid": pid, "reason": "Process command no longer matches selected home/domain paths."})
        continue
    eligible.append({"pid": pid, "matchedScopes": matched, "command": command[:500]})

results = []
for item in eligible:
    pid = item["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
        results.append({"pid": pid, "signal": "TERM", "status": "sent", "matchedScopes": item["matchedScopes"], "command": item["command"]})
    except Exception as error:
        results.append({"pid": pid, "signal": "TERM", "status": "failed", "error": str(error), "matchedScopes": item["matchedScopes"], "command": item["command"]})

time.sleep(8)
for item in eligible:
    pid = item["pid"]
    if not alive(pid):
        continue
    try:
        os.kill(pid, signal.SIGKILL)
        results.append({"pid": pid, "signal": "KILL", "status": "sent", "matchedScopes": item["matchedScopes"], "command": item["command"]})
    except Exception as error:
        results.append({"pid": pid, "signal": "KILL", "status": "failed", "error": str(error), "matchedScopes": item["matchedScopes"], "command": item["command"]})

time.sleep(2)
remaining = [item["pid"] for item in eligible if alive(item["pid"])]
payload = {
    "status": "failed" if remaining else "succeeded",
    "pathsChecked": [{"scope": scope, "path": path} for scope, path in paths],
    "eligible": eligible,
    "skipped": skipped,
    "results": results,
    "remainingPids": remaining,
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_KILL_JSON__", json.dumps(target))


def build_backup_script(body):
    backup_dir = str(body.get("backupDir") or "").strip()
    targets = body.get("targets") or []
    requested_stamp = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(body.get("stamp") or "").strip())
    if not backup_dir:
        raise ValueError("Backup destination directory is required.")
    if not isinstance(targets, list) or not targets:
        raise ValueError("At least one backup target is required.")
    payload = {"backupDir": backup_dir, "targets": targets, "stamp": requested_stamp}
    script = r'''
import json
import os
import re
import subprocess
import time

request = __PATCHSCOPE_BACKUP_JSON__
backup_dir = os.path.abspath(str(request.get("backupDir") or ""))
targets = request.get("targets") or []
requested_stamp = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(request.get("stamp") or "").strip())

if not backup_dir:
    raise RuntimeError("Backup destination directory is required.")

safe_stamp = requested_stamp or time.strftime("%Y%m%d_%H%M%S")


def clean_path(value):
    value = str(value or "").strip()
    if value.lower() in ("not discovered", "not applicable", "none", "null"):
        return ""
    return os.path.abspath(value)


def safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return value.strip("_") or "target"


def abort_backup(message):
    print(message)
    sys.stdout.flush()
    results = []
    for item in targets:
        label = safe_name(item.get("target") or "TARGET")
        source = clean_path(item.get("path"))
        results.append({
            "target": label,
            "source": source,
            "archive": "",
            "sourceSize": "",
            "archiveSize": "",
            "durationSeconds": 0,
            "status": "failed",
            "error": message,
            "output": message,
        })
    payload = {
        "status": "failed",
        "backupDir": backup_dir,
        "logPath": "",
        "results": results,
        "errors": [message],
    }
    print("__PATCHSCOPE_JSON_START__")
    print(json.dumps(payload))
    print("__PATCHSCOPE_JSON_END__")
    raise SystemExit(0)


try:
    os.makedirs(backup_dir, exist_ok=True)
except PermissionError:
    abort_backup("Backup destination cannot be created by the SSH user: %s. Ask the Unix administrator to create it with write access for this user, or choose another backup destination." % backup_dir)
except OSError as error:
    abort_backup("Backup destination cannot be created: %s (%s)." % (backup_dir, error))

if not os.access(backup_dir, os.W_OK | os.X_OK):
    abort_backup("Backup destination is not writable by the SSH user: %s. Change the destination or fix directory ownership/permissions." % backup_dir)

log_path = os.path.join(backup_dir, "patchscope_backup_%s.log" % safe_stamp)


def emit(message):
    print(message)
    sys.stdout.flush()
    with open(log_path, "a") as handle:
        handle.write(message + "\n")


def run_output(command, timeout=120):
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            universal_newlines=True,
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return -1, output


def human_size(path):
    code, output = run_output(["du", "-sh", path], timeout=300)
    if code == 0 and output.strip():
        return output.strip().split()[0]
    try:
        return "%s bytes" % os.path.getsize(path)
    except Exception:
        return ""


emit("PatchPilot backup started: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
emit("Backup destination: %s" % backup_dir)

results = []
overall = "succeeded"

for item in targets:
    label = safe_name(item.get("target") or "TARGET")
    source = clean_path(item.get("path"))
    started = time.time()
    if not source:
        results.append({"target": label, "source": source, "status": "skipped", "error": "No source path was provided."})
        overall = "failed"
        continue
    if not os.path.exists(source):
        message = "Source path does not exist: %s" % source
        emit("%s: %s" % (label, message))
        results.append({"target": label, "source": source, "status": "failed", "error": message})
        overall = "failed"
        continue
    if os.path.commonpath([backup_dir, source]) == source:
        message = "Backup destination cannot be inside the source path: %s" % source
        emit("%s: %s" % (label, message))
        results.append({"target": label, "source": source, "status": "failed", "error": message})
        overall = "failed"
        continue

    parent = os.path.dirname(source.rstrip(os.sep))
    base = os.path.basename(source.rstrip(os.sep))
    archive = os.path.join(backup_dir, "%s_%s_%s.tar.gz" % (label, safe_name(base), safe_stamp))
    before_size = human_size(source)
    command = ["tar", "-czpf", archive, "-C", parent, base]
    emit("%s: backing up %s" % (label, source))
    if before_size:
        emit("%s: source size %s" % (label, before_size))
    emit("%s: command %s" % (label, " ".join(command)))
    code, output = run_output(command, timeout=7200)
    duration = round(time.time() - started, 1)
    tail = output[-20000:]
    if code == 0 and os.path.isfile(archive):
        archive_size = human_size(archive)
        emit("%s: archive created %s" % (label, archive))
        if archive_size:
            emit("%s: archive size %s" % (label, archive_size))
        results.append({
            "target": label,
            "source": source,
            "archive": archive,
            "sourceSize": before_size,
            "archiveSize": archive_size,
            "durationSeconds": duration,
            "status": "succeeded",
            "output": tail,
        })
    else:
        overall = "failed"
        emit("%s: backup failed with return code %s" % (label, code))
        if tail.strip():
            emit("%s: %s" % (label, tail.strip().splitlines()[-1]))
        results.append({
            "target": label,
            "source": source,
            "archive": archive,
            "sourceSize": before_size,
            "archiveSize": "",
            "durationSeconds": duration,
            "status": "failed",
            "error": tail.strip() or "tar failed with return code %s" % code,
            "output": tail,
        })

payload = {
    "status": overall,
    "backupDir": backup_dir,
    "logPath": log_path,
    "results": results,
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_BACKUP_JSON__", json.dumps(payload))


def build_backup_preflight_script(body):
    backup_dir = str(body.get("backupDir") or "").strip()
    targets = body.get("targets") or []
    requested_stamp = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(body.get("stamp") or "").strip())
    if not backup_dir:
        raise ValueError("Backup destination directory is required.")
    if not isinstance(targets, list) or not targets:
        raise ValueError("At least one backup target is required.")
    payload = {"backupDir": backup_dir, "targets": targets, "stamp": requested_stamp}
    script = r'''
import json
import os
import re
import subprocess
import time

request = __PATCHSCOPE_BACKUP_PREFLIGHT_JSON__
backup_dir = os.path.abspath(str(request.get("backupDir") or ""))
targets = request.get("targets") or []
requested_stamp = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(request.get("stamp") or "").strip())
safe_stamp = requested_stamp or time.strftime("%Y%m%d_%H%M%S")
large_threshold = 5 * 1024 * 1024 * 1024


def clean_path(value):
    value = str(value or "").strip()
    if value.lower() in ("not discovered", "not applicable", "none", "null"):
        return ""
    return os.path.abspath(value)


def safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return value.strip("_") or "target"


def run_output(command, timeout=300):
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            universal_newlines=True,
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return -1, output


def dir_size_bytes(path):
    code, output = run_output(["du", "-sk", path], timeout=900)
    if code == 0 and output.strip():
        try:
            return int(output.strip().split()[0]) * 1024
        except Exception:
            pass
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except Exception:
            return 0
    return 0


def human_bytes(value):
    try:
        value = float(value)
    except Exception:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return "%d %s" % (value, units[index])
    return "%.1f %s" % (value, units[index])


def existing_parent(path):
    candidate = os.path.abspath(path)
    while candidate and not os.path.exists(candidate):
        parent = os.path.dirname(candidate.rstrip(os.sep))
        if parent == candidate:
            break
        candidate = parent
    return candidate if candidate and os.path.exists(candidate) else os.sep


def disk_free(path):
    parent = existing_parent(path)
    stats = os.statvfs(parent)
    free = stats.f_bavail * stats.f_frsize
    total = stats.f_blocks * stats.f_frsize
    return parent, free, total


def destination_permission_error(path):
    candidate = os.path.abspath(path)
    if os.path.exists(candidate):
        if not os.path.isdir(candidate):
            return "Backup destination exists but is not a directory: %s" % candidate
        if not os.access(candidate, os.W_OK | os.X_OK):
            return "Backup destination is not writable by the SSH user: %s" % candidate
        return ""
    parent = existing_parent(candidate)
    if not os.path.isdir(parent):
        return "Backup destination parent is not a directory: %s" % parent
    if not os.access(parent, os.W_OK | os.X_OK):
        return "Backup destination cannot be created by the SSH user because parent directory is not writable: %s" % parent
    return ""


results = []
errors = []
total_source_bytes = 0
large_targets = []

for item in targets:
    label = safe_name(item.get("target") or "TARGET")
    source = clean_path(item.get("path"))
    base = os.path.basename(source.rstrip(os.sep)) if source else ""
    archive = os.path.join(backup_dir, "%s_%s_%s.tar.gz" % (label, safe_name(base), safe_stamp))
    result = {
        "target": label,
        "source": source,
        "archive": archive,
        "exists": bool(source and os.path.exists(source)),
        "sourceBytes": 0,
        "sourceSize": "unknown",
        "large": False,
        "status": "pending",
        "error": "",
    }
    if not source:
        result["status"] = "failed"
        result["error"] = "No source path was provided."
        errors.append("%s: no source path was provided." % label)
    elif not os.path.exists(source):
        result["status"] = "failed"
        result["error"] = "Source path does not exist: %s" % source
        errors.append(result["error"])
    else:
        source_bytes = dir_size_bytes(source)
        result["sourceBytes"] = source_bytes
        result["sourceSize"] = human_bytes(source_bytes)
        result["large"] = source_bytes >= large_threshold
        result["status"] = "ready"
        total_source_bytes += source_bytes
        if result["large"]:
            large_targets.append(label)
        try:
            if os.path.commonpath([backup_dir, source]) == source:
                result["status"] = "failed"
                result["error"] = "Backup destination cannot be inside this source path."
                errors.append("%s: backup destination cannot be inside the source path." % label)
        except Exception:
            pass
    results.append(result)

disk_path = existing_parent(backup_dir)
free_bytes = 0
total_bytes = 0
disk_error = ""
destination_error = destination_permission_error(backup_dir)
if destination_error:
    errors.append(destination_error)
try:
    disk_path, free_bytes, total_bytes = disk_free(backup_dir)
except Exception as error:
    disk_error = str(error)
    errors.append("Unable to read free disk space for %s: %s" % (backup_dir, disk_error))

required_bytes = total_source_bytes
enough_space = bool(free_bytes and required_bytes and free_bytes > required_bytes and not errors)
if required_bytes and free_bytes and free_bytes <= required_bytes:
    errors.append("Backup destination does not have enough free space for the selected source directories.")

payload = {
    "status": "ready" if enough_space else "blocked",
    "backupDir": backup_dir,
    "stamp": safe_stamp,
    "diskPath": disk_path,
    "freeBytes": free_bytes,
    "freeSpace": human_bytes(free_bytes) if free_bytes else "unknown",
    "diskBytes": total_bytes,
    "diskSize": human_bytes(total_bytes) if total_bytes else "unknown",
    "requiredBytes": required_bytes,
    "requiredSpace": human_bytes(required_bytes) if required_bytes else "unknown",
    "destinationWritable": not bool(destination_error),
    "destinationWritableMessage": destination_error,
    "largeThresholdBytes": large_threshold,
    "largeThreshold": human_bytes(large_threshold),
    "hasLargeTargets": bool(large_targets),
    "largeTargets": large_targets,
    "enoughSpace": enough_space,
    "errors": errors,
    "results": results,
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_BACKUP_PREFLIGHT_JSON__", json.dumps(payload))


def build_patch_apply_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    patch_path = str(body.get("patchPath") or "").strip()
    dry_run = bool(body.get("dryRun"))
    debug = bool(body.get("debug"))
    opatch_heap_options = sanitize_opatch_heap_options(body.get("opatchHeapOptions"))
    if not oracle_home:
        raise ValueError("Selected ORACLE_HOME is required for patch apply.")
    if not patch_path:
        raise ValueError("Patch directory on server is required for patch apply.")
    payload = {"oracleHome": oracle_home, "patchPath": patch_path, "dryRun": dry_run, "debug": debug, "opatchHeapOptions": opatch_heap_options}
    script = r'''
import json
import os
import re
import subprocess
import time

request = json.loads(__PATCHSCOPE_PATCH_JSON__)
oracle_home = os.path.abspath(str(request.get("oracleHome") or ""))
patch_path = os.path.abspath(str(request.get("patchPath") or ""))
dry_run = bool(request.get("dryRun"))
debug = bool(request.get("debug"))
opatch = os.path.join(oracle_home, "OPatch", "opatch")
debug_args = ["-verbose", "-debug"] if debug else []
__PATCHSCOPE_OPATCH_HEAP_HELPER__
opatch_heap = configure_opatch_heap(request.get("opatchHeapOptions"))


def emit(message):
    print(message)
    sys.stdout.flush()


def unique(items):
    result = []
    seen = set()
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def run_command(command, cwd=None, timeout=7200):
    emit("$ %s%s" % ("cd %s && " % cwd if cwd else "", " ".join(command)))
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        env=dict(os.environ, ORACLE_HOME=oracle_home),
        bufsize=1,
    )
    output = []
    started = time.time()
    while True:
        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            output.append(line)
            emit(line.rstrip("\n"))
        elif proc.poll() is not None:
            break
        if time.time() - started > timeout:
            proc.kill()
            raise RuntimeError("Command timed out after %ss: %s" % (timeout, " ".join(command)))
    if proc.stdout:
        rest = proc.stdout.read()
        if rest:
            output.append(rest)
            for rest_line in rest.splitlines():
                emit(rest_line)
        proc.stdout.close()
    return proc.wait(), "".join(output)


def read_small(path, limit=256 * 1024):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except Exception:
        return ""


def discover_patch_ids(base):
    ids = []
    base_name = os.path.basename(base.rstrip(os.sep))
    if re.fullmatch(r"\d{5,}", base_name or ""):
        ids.append(base_name)
    if not os.path.isdir(base):
        return unique(ids)
    base_depth = base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= 5:
            dirs[:] = []
        root_name = os.path.basename(root.rstrip(os.sep))
        if re.fullmatch(r"\d{5,}", root_name or ""):
            ids.append(root_name)
        for name in files:
            low = name.lower()
            if low in ("inventory.xml", "actions.xml", "patch.xml") or low.startswith("readme"):
                text = read_small(os.path.join(root, name))
                ids.extend(re.findall(r"<\s*patch_id\s*>\s*(\d{5,})\s*<", text, re.I))
                ids.extend(re.findall(r"\bpatch(?:id|_id)?\s*=\s*[\"']?(\d{5,})", text, re.I))
        if depth >= 5:
            dirs[:] = []
    return unique(ids)


def patch_metadata_files(path):
    candidates = [
        os.path.join(path, "etc", "config", "actions.xml"),
        os.path.join(path, "etc", "config", "inventory.xml"),
        os.path.join(path, "etc", "config", "patch.xml"),
        os.path.join(path, "actions.xml"),
        os.path.join(path, "inventory.xml"),
        os.path.join(path, "patch.xml"),
    ]
    return [item for item in candidates if os.path.isfile(item)]


def has_patch_metadata(path):
    return bool(patch_metadata_files(path))


def ids_from_metadata(path):
    ids = []
    for metadata in patch_metadata_files(path):
        text = read_small(metadata)
        ids.extend(re.findall(r"<\s*patch_id\s*>\s*(\d{5,})\s*<", text, re.I))
        ids.extend(re.findall(r"\bpatch(?:id|_id)?\s*=\s*[\"']?(\d{5,})", text, re.I))
    name = os.path.basename(path.rstrip(os.sep))
    if re.fullmatch(r"\d{5,}", name or ""):
        ids.insert(0, name)
    return unique(ids)


def discover_patch_roots(base):
    roots = []
    if has_patch_metadata(base):
        roots.append(base)
    if not os.path.isdir(base):
        return roots
    base_depth = base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= 5:
            dirs[:] = []
        dirs[:] = [name for name in dirs if name.lower() not in ("logs", "backup", "backups", "tmp", "cache", "cfgtoollogs")]
        if root != base and has_patch_metadata(root):
            roots.append(root)
            dirs[:] = []
    return unique(roots)


def resolve_patch_apply_dir(base, primary_id, patch_ids):
    roots = discover_patch_roots(base)
    if not roots:
        return base, []
    desired = primary_id or (patch_ids[0] if patch_ids else "")
    base_depth = base.rstrip(os.sep).count(os.sep)

    def rank(path):
        ids = ids_from_metadata(path)
        name = os.path.basename(path.rstrip(os.sep))
        depth = path.rstrip(os.sep).count(os.sep) - base_depth
        score = 1000 + depth
        if path == base:
            score -= 200
        if desired and name == desired:
            score -= 500
        if desired and desired in ids:
            score -= 300
        if any(item in patch_ids for item in ids):
            score -= 100
        return score

    roots.sort(key=rank)
    return roots[0], roots


def inventory_contains(inventory, patch_id):
    pid = re.escape(str(patch_id))
    patterns = [
        r"\bPatch\s+%s\b" % pid,
        r"\bPatch\s+%s\s*:" % pid,
        r"\b%s\b" % pid,
    ]
    return any(re.search(pattern, inventory, re.I) for pattern in patterns)


def extract_opatch_version(output):
    match = re.search(r"OPatch\s+Version:\s*([0-9.]+)", output or "", re.I)
    if match:
        return match.group(1)
    match = re.search(r"OPatch\s+version\s*:\s*([0-9.]+)", output or "", re.I)
    if match:
        return match.group(1)
    match = re.search(r"\b([0-9]+(?:\.[0-9]+){2,})\b", output or "")
    return match.group(1) if match else ""


def opatch_log_locations(*outputs):
    logs = []
    for output in outputs:
        logs.extend(re.findall(r"Log file location\s*:\s*([^\r\n]+)", output or "", re.I))
    return unique(logs)


def attach_opatch_logs(payload, *outputs):
    logs = opatch_log_locations(*outputs)
    if logs:
        payload["opatchLogLocations"] = logs
        payload["opatchLogLocation"] = logs[-1]
    return payload


def emit_payload(payload, *outputs):
    payload["debug"] = debug
    payload["opatchHeap"] = opatch_heap
    attach_opatch_logs(payload, *outputs)
    print("__PATCHSCOPE_JSON_START__")
    print(json.dumps(payload))
    print("__PATCHSCOPE_JSON_END__")


if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isfile(opatch):
    raise RuntimeError("OPatch executable was not found under %s" % oracle_home)
if not os.path.isdir(patch_path):
    raise RuntimeError("Patch directory does not exist: %s" % patch_path)

patch_ids = discover_patch_ids(patch_path)
primary_patch_id = os.path.basename(patch_path.rstrip(os.sep)) if re.fullmatch(r"\d{5,}", os.path.basename(patch_path.rstrip(os.sep)) or "") else ""
if not primary_patch_id and patch_ids:
    primary_patch_id = patch_ids[0]
apply_dir, patch_roots = resolve_patch_apply_dir(patch_path, primary_patch_id, patch_ids)
emit("PatchPilot OPatch run started: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
emit("Oracle home: %s" % oracle_home)
emit("Patch directory: %s" % patch_path)
emit("Resolved OPatch apply directory: %s" % apply_dir)
if patch_roots:
    emit("Detected OPatch metadata director%s: %s" % ("y" if len(patch_roots) == 1 else "ies", ", ".join(patch_roots)))
emit("Dry-run only: %s" % ("yes" if dry_run else "no"))
emit("OPatch verbose/debug: %s" % ("yes" if debug else "no"))
emit("OPatch heap options %s: OPATCH_JRE_MEMORY_OPTIONS=%s" % (opatch_heap.get("action"), opatch_heap.get("effective")))
emit("Patch ids discovered from patch metadata/path: %s" % (", ".join(patch_ids) if patch_ids else "none"))

version_code, version_output = run_command([opatch, "version"], timeout=120)
opatch_version = extract_opatch_version(version_output)
if version_code != 0:
    payload = {
        "status": "failed",
        "dryRun": dry_run,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "applyDir": apply_dir,
        "metadataDirs": patch_roots,
        "patchIds": patch_ids,
        "primaryPatchId": primary_patch_id,
        "opatchVersion": opatch_version,
        "inventoryVerified": False,
        "error": "OPatch version command failed.",
        "versionOutput": version_output[-50000:],
    }
    emit_payload(payload, version_output)
    raise SystemExit(0)

before_code, before_inventory = run_command([opatch, "lsinventory"], timeout=900)
if before_code != 0:
    payload = {
        "status": "failed",
        "dryRun": dry_run,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "applyDir": apply_dir,
        "metadataDirs": patch_roots,
        "patchIds": patch_ids,
        "primaryPatchId": primary_patch_id,
        "opatchVersion": opatch_version,
        "inventoryVerified": False,
        "error": "OPatch lsinventory failed before apply.",
        "inventoryOutput": before_inventory[-50000:],
    }
    emit_payload(payload, before_inventory)
    raise SystemExit(0)

already_found_patch_ids = [patch_id for patch_id in patch_ids if inventory_contains(before_inventory, patch_id)]
already_verified = bool(primary_patch_id and inventory_contains(before_inventory, primary_patch_id))
if not primary_patch_id and already_found_patch_ids:
    already_verified = True
if already_verified:
    emit("Patch already exists in OPatch inventory. OPatch apply will be skipped.")
    payload = {
        "status": "succeeded",
        "alreadyApplied": True,
        "dryRun": dry_run,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "applyDir": apply_dir,
        "metadataDirs": patch_roots,
        "patchIds": patch_ids,
        "primaryPatchId": primary_patch_id,
        "opatchVersion": opatch_version,
        "foundPatchIds": already_found_patch_ids or ([primary_patch_id] if primary_patch_id else []),
        "missingPatchIds": [patch_id for patch_id in patch_ids if patch_id not in already_found_patch_ids],
        "inventoryVerified": True,
        "message": "Patch is already applied in the selected ORACLE_HOME.",
        "inventoryTail": before_inventory[-50000:],
    }
    emit_payload(payload, before_inventory)
    raise SystemExit(0)

conflict_code, conflict_output = run_command([opatch, "prereq", "CheckConflictAgainstOHWithDetail", "-ph", apply_dir] + debug_args, timeout=1800)
if conflict_code != 0:
    payload = {
        "status": "failed",
        "dryRun": dry_run,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "applyDir": apply_dir,
        "metadataDirs": patch_roots,
        "patchIds": patch_ids,
        "primaryPatchId": primary_patch_id,
        "opatchVersion": opatch_version,
        "inventoryVerified": False,
        "error": "OPatch conflict prerequisite failed.",
        "conflictOutput": conflict_output[-50000:],
    }
    emit_payload(payload, conflict_output)
    raise SystemExit(0)

if dry_run:
    payload = {
        "status": "dry-run",
        "dryRun": True,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "applyDir": apply_dir,
        "metadataDirs": patch_roots,
        "patchIds": patch_ids,
        "primaryPatchId": primary_patch_id,
        "opatchVersion": opatch_version,
        "inventoryVerified": False,
        "message": "Dry-run completed. Conflict check passed, but opatch apply was not executed.",
        "conflictOutput": conflict_output[-50000:],
    }
    emit_payload(payload, conflict_output)
    raise SystemExit(0)

apply_code, apply_output = run_command([opatch, "apply", "-silent"] + debug_args, cwd=apply_dir, timeout=7200)
if apply_code != 0:
    payload = {
        "status": "failed",
        "dryRun": False,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "applyDir": apply_dir,
        "metadataDirs": patch_roots,
        "patchIds": patch_ids,
        "primaryPatchId": primary_patch_id,
        "opatchVersion": opatch_version,
        "inventoryVerified": False,
        "error": "OPatch apply failed with return code %s." % apply_code,
        "applyOutput": apply_output[-50000:],
    }
    emit_payload(payload, conflict_output, apply_output)
    raise SystemExit(0)

after_code, after_inventory = run_command([opatch, "lsinventory"], timeout=900)
if after_code != 0:
    payload = {
        "status": "failed",
        "dryRun": False,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "applyDir": apply_dir,
        "metadataDirs": patch_roots,
        "patchIds": patch_ids,
        "primaryPatchId": primary_patch_id,
        "opatchVersion": opatch_version,
        "inventoryVerified": False,
        "error": "OPatch apply completed, but lsinventory failed after apply.",
        "applyOutput": apply_output[-50000:],
        "inventoryOutput": after_inventory[-50000:],
    }
    emit_payload(payload, conflict_output, apply_output, after_inventory)
    raise SystemExit(0)

found_patch_ids = [patch_id for patch_id in patch_ids if inventory_contains(after_inventory, patch_id)]
missing_patch_ids = [patch_id for patch_id in patch_ids if patch_id not in found_patch_ids]
primary_verified = bool(primary_patch_id and inventory_contains(after_inventory, primary_patch_id))
inventory_verified = primary_verified if primary_patch_id else bool(found_patch_ids or not patch_ids)

status = "succeeded" if inventory_verified else "failed"
error = ""
if not inventory_verified:
    if primary_patch_id:
        error = "OPatch apply finished, but patch %s was not found in lsinventory for %s." % (primary_patch_id, oracle_home)
    elif patch_ids:
        error = "OPatch apply finished, but none of the discovered patch ids were found in lsinventory for %s." % oracle_home
    else:
        error = "OPatch apply finished, but PatchPilot could not discover a patch id to verify in lsinventory."

payload = {
    "status": status,
    "dryRun": False,
    "oracleHome": oracle_home,
    "patchPath": patch_path,
    "applyDir": apply_dir,
    "metadataDirs": patch_roots,
    "patchIds": patch_ids,
    "primaryPatchId": primary_patch_id,
    "opatchVersion": opatch_version,
    "foundPatchIds": found_patch_ids,
    "missingPatchIds": missing_patch_ids,
    "inventoryVerified": inventory_verified,
    "error": error,
    "applyOutput": apply_output[-50000:],
    "inventoryTail": after_inventory[-50000:],
}
emit_payload(payload, conflict_output, apply_output, after_inventory)
'''
    return (
        script
        .replace("__PATCHSCOPE_PATCH_JSON__", json.dumps(json.dumps(payload)))
        .replace("__PATCHSCOPE_OPATCH_HEAP_HELPER__", REMOTE_OPATCH_HEAP_HELPER)
    )


def build_patch_rollback_script(body):
    oracle_home = str(body.get("oracleHome") or "").strip()
    patch_path = str(body.get("patchPath") or "").strip()
    patch_id = re.sub(r"[^0-9]", "", str(body.get("patchId") or "").strip())
    opatch_heap_options = sanitize_opatch_heap_options(body.get("opatchHeapOptions"))
    if not oracle_home:
        raise ValueError("Selected ORACLE_HOME is required for rollback.")
    if not patch_path:
        raise ValueError("Patch directory on server is required for rollback.")
    payload = {"oracleHome": oracle_home, "patchPath": patch_path, "patchId": patch_id, "opatchHeapOptions": opatch_heap_options}
    script = r'''
import json
import os
import re
import subprocess
import time

request = json.loads(__PATCHSCOPE_ROLLBACK_JSON__)
oracle_home = os.path.abspath(str(request.get("oracleHome") or ""))
patch_path = os.path.abspath(str(request.get("patchPath") or ""))
requested_patch_id = re.sub(r"[^0-9]", "", str(request.get("patchId") or ""))
opatch = os.path.join(oracle_home, "OPatch", "opatch")
__PATCHSCOPE_OPATCH_HEAP_HELPER__
opatch_heap = configure_opatch_heap(request.get("opatchHeapOptions"))


def emit(message):
    print(message)
    sys.stdout.flush()


def unique(items):
    result = []
    seen = set()
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def run_command(command, cwd=None, timeout=7200):
    emit("$ %s%s" % ("cd %s && " % cwd if cwd else "", " ".join(command)))
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        env=dict(os.environ, ORACLE_HOME=oracle_home),
        bufsize=1,
    )
    output = []
    started = time.time()
    while True:
        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            output.append(line)
            emit(line.rstrip("\n"))
        elif proc.poll() is not None:
            break
        if time.time() - started > timeout:
            proc.kill()
            raise RuntimeError("Command timed out after %ss: %s" % (timeout, " ".join(command)))
    if proc.stdout:
        rest = proc.stdout.read()
        if rest:
            output.append(rest)
            for rest_line in rest.splitlines():
                emit(rest_line)
        proc.stdout.close()
    return proc.wait(), "".join(output)


def read_small(path, limit=256 * 1024):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except Exception:
        return ""


def discover_patch_ids(base):
    ids = []
    base_name = os.path.basename(base.rstrip(os.sep))
    if re.fullmatch(r"\d{5,}", base_name or ""):
        ids.append(base_name)
    if not os.path.isdir(base):
        return unique(ids)
    base_depth = base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= 5:
            dirs[:] = []
        root_name = os.path.basename(root.rstrip(os.sep))
        if re.fullmatch(r"\d{5,}", root_name or ""):
            ids.append(root_name)
        for name in files:
            low = name.lower()
            if low in ("inventory.xml", "actions.xml", "patch.xml") or low.startswith("readme"):
                text = read_small(os.path.join(root, name))
                ids.extend(re.findall(r"<\s*patch_id\s*>\s*(\d{5,})\s*<", text, re.I))
                ids.extend(re.findall(r"\bpatch(?:id|_id)?\s*=\s*[\"']?(\d{5,})", text, re.I))
        if depth >= 5:
            dirs[:] = []
    return unique(ids)


def inventory_contains(inventory, patch_id):
    if not patch_id:
        return False
    pid = re.escape(str(patch_id))
    patterns = [
        r"\bPatch\s+%s\b" % pid,
        r"\bPatch\s+%s\s*:" % pid,
        r"\b%s\b" % pid,
    ]
    return any(re.search(pattern, inventory, re.I) for pattern in patterns)


if not os.path.isdir(oracle_home):
    raise RuntimeError("ORACLE_HOME does not exist: %s" % oracle_home)
if not os.path.isfile(opatch):
    raise RuntimeError("OPatch executable was not found under %s" % oracle_home)

patch_ids = discover_patch_ids(patch_path)
primary_patch_id = requested_patch_id or (os.path.basename(patch_path.rstrip(os.sep)) if re.fullmatch(r"\d{5,}", os.path.basename(patch_path.rstrip(os.sep)) or "") else "")
if not primary_patch_id and patch_ids:
    primary_patch_id = patch_ids[0]
if not primary_patch_id:
    raise RuntimeError("PatchPilot could not determine a patch id to rollback from %s." % patch_path)

emit("PatchPilot OPatch rollback started: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
emit("Oracle home: %s" % oracle_home)
emit("Patch directory: %s" % patch_path)
emit("Rollback patch id: %s" % primary_patch_id)
emit("OPatch heap options %s: OPATCH_JRE_MEMORY_OPTIONS=%s" % (opatch_heap.get("action"), opatch_heap.get("effective")))

before_code, before_inventory = run_command([opatch, "lsinventory"], timeout=900)
if before_code != 0:
    raise RuntimeError("OPatch lsinventory failed before rollback.")

if not inventory_contains(before_inventory, primary_patch_id):
    emit("Patch %s is not present in OPatch inventory. Rollback is already satisfied." % primary_patch_id)
    payload = {
        "status": "succeeded",
        "alreadyRolledBack": True,
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "patchId": primary_patch_id,
        "patchIds": patch_ids,
        "opatchHeap": opatch_heap,
        "inventoryVerified": True,
        "message": "Patch id is not present in OPatch inventory.",
        "inventoryTail": before_inventory[-50000:],
    }
    print("__PATCHSCOPE_JSON_START__")
    print(json.dumps(payload))
    print("__PATCHSCOPE_JSON_END__")
    raise SystemExit(0)

rollback_code, rollback_output = run_command([opatch, "rollback", "-id", primary_patch_id, "-silent"], timeout=7200)
if rollback_code != 0:
    payload = {
        "status": "failed",
        "oracleHome": oracle_home,
        "patchPath": patch_path,
        "patchId": primary_patch_id,
        "patchIds": patch_ids,
        "opatchHeap": opatch_heap,
        "inventoryVerified": False,
        "error": "OPatch rollback failed with return code %s." % rollback_code,
        "rollbackOutput": rollback_output[-50000:],
    }
    print("__PATCHSCOPE_JSON_START__")
    print(json.dumps(payload))
    print("__PATCHSCOPE_JSON_END__")
    raise SystemExit(0)

after_code, after_inventory = run_command([opatch, "lsinventory"], timeout=900)
if after_code != 0:
    raise RuntimeError("OPatch lsinventory failed after rollback.")

still_present = inventory_contains(after_inventory, primary_patch_id)
payload = {
    "status": "failed" if still_present else "succeeded",
    "alreadyRolledBack": False,
    "oracleHome": oracle_home,
    "patchPath": patch_path,
    "patchId": primary_patch_id,
    "patchIds": patch_ids,
    "opatchHeap": opatch_heap,
    "inventoryVerified": not still_present,
    "error": "OPatch rollback completed, but patch %s is still present in lsinventory." % primary_patch_id if still_present else "",
    "rollbackOutput": rollback_output[-50000:],
    "inventoryTail": after_inventory[-50000:],
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return (
        script
        .replace("__PATCHSCOPE_ROLLBACK_JSON__", json.dumps(json.dumps(payload)))
        .replace("__PATCHSCOPE_OPATCH_HEAP_HELPER__", REMOTE_OPATCH_HEAP_HELPER)
    )


def send_json(handler, status, payload):
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length > 128 * 1024:
        raise ValueError("Request body is too large.")
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw or "{}")


def ssh_profile(body):
    host = str(body.get("host") or "").strip()
    user = str(body.get("user") or body.get("username") or "").strip()
    port = int(str(body.get("port") or "22").strip())
    password = str(body.get("password") or "")
    if not host:
        raise ValueError("SSH host is required.")
    if not user:
        raise ValueError("SSH user is required.")
    if port < 1 or port > 65535:
        raise ValueError("SSH port must be between 1 and 65535.")
    return {"host": host, "user": user, "port": port, "password": password}


def job_oracle_home(body):
    home = body.get("home") or {}
    oracle_home = str(body.get("oracleHome") or home.get("oracleHome") or "").strip()
    if oracle_home:
        return oracle_home
    for target in body.get("targets") or []:
        if str(target.get("target") or "").strip().upper() == "ORACLE_HOME":
            return str(target.get("path") or "").strip()
    return ""


def normalize_remote_path(path):
    value = str(path or "").strip().replace("\\", "/")
    value = re.sub(r"/{2,}", "/", value)
    return value.rstrip("/") or "/"


def target_job_key(profile, body):
    if not job_oracle_home(body):
        raise ValueError("Selected ORACLE_HOME is required to start this job.")
    return "|".join([
        str(profile.get("host") or "").strip().lower().rstrip("."),
        str(int(profile.get("port") or 22)),
    ])


def target_job_label(profile, body):
    return "%s@%s:%s %s" % (
        str(profile.get("user") or "").strip(),
        str(profile.get("host") or "").strip(),
        str(profile.get("port") or 22),
        normalize_remote_path(job_oracle_home(body)),
    )


def acquire_target_job(profile, body, operation, job_id):
    key = target_job_key(profile, body)
    label = target_job_label(profile, body)
    with ACTIVE_TARGET_JOB_LOCK:
        existing = ACTIVE_TARGET_JOBS.get(key)
        if existing:
            raise RuntimeError(
                "%s is already running for %s. Wait for job %s to finish before starting %s. "
                "Jobs for a different SSH server may run concurrently."
                % (existing["operation"], label, existing["jobId"], operation)
            )
        ACTIVE_TARGET_JOBS[key] = {
            "jobId": job_id,
            "operation": operation,
            "label": label,
            "startedAt": time.time(),
        }
    return key


def release_target_job(key, job_id):
    if not key:
        return
    with ACTIVE_TARGET_JOB_LOCK:
        existing = ACTIVE_TARGET_JOBS.get(key)
        if existing and existing.get("jobId") == job_id:
            ACTIVE_TARGET_JOBS.pop(key, None)


def build_ssh_test_script(body):
    patch_path = str(body.get("patchPath") or "").strip()
    payload = {"patchPath": patch_path}
    script = r'''
import getpass
import json
import os
import socket

request = json.loads(__PATCHSCOPE_SSH_TEST_JSON__)
patch_path = str(request.get("patchPath") or "").strip()


def readme_candidates(base):
    names = []
    for root, dirs, files in os.walk(base):
        depth = os.path.relpath(root, base).count(os.sep)
        if depth > 2:
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs if name.lower() not in ("backup", "backups", ".git", "__macosx")]
        for name in files:
            low = name.lower()
            if low.startswith("readme") or low.endswith(".readme") or low in ("patch_readme.txt", "patch_readme.html"):
                names.append(os.path.join(root, name))
                if len(names) >= 20:
                    return names
    return names


def validate_patch_path(value):
    if not value:
        return {
            "ok": False,
            "path": "",
            "resolvedPath": "",
            "message": "Patch Directory on Server is required.",
            "readmePath": "",
            "entryCount": 0,
        }
    resolved = os.path.abspath(os.path.expanduser(value))
    if not os.path.exists(resolved):
        return {
            "ok": False,
            "path": value,
            "resolvedPath": resolved,
            "message": "Patch Directory on Server does not exist: %s" % resolved,
            "readmePath": "",
            "entryCount": 0,
        }
    if not os.path.isdir(resolved):
        return {
            "ok": False,
            "path": value,
            "resolvedPath": resolved,
            "message": "Patch Directory on Server is not a directory: %s" % resolved,
            "readmePath": "",
            "entryCount": 0,
        }
    if not os.access(resolved, os.R_OK | os.X_OK):
        return {
            "ok": False,
            "path": value,
            "resolvedPath": resolved,
            "message": "Patch Directory on Server is not readable/searchable by %s: %s" % (getpass.getuser(), resolved),
            "readmePath": "",
            "entryCount": 0,
        }
    try:
        entries = os.listdir(resolved)
    except Exception as error:
        return {
            "ok": False,
            "path": value,
            "resolvedPath": resolved,
            "message": "Patch Directory on Server exists but cannot be listed: %s" % error,
            "readmePath": "",
            "entryCount": 0,
        }
    candidates = readme_candidates(resolved)
    if not candidates:
        return {
            "ok": False,
            "path": value,
            "resolvedPath": resolved,
            "message": "Patch Directory on Server is reachable, but no README file was found in the patch directory or the first two subdirectory levels.",
            "readmePath": "",
            "entryCount": len(entries),
        }
    return {
        "ok": True,
        "path": value,
        "resolvedPath": resolved,
        "message": "Patch directory is reachable and README is present.",
        "readmePath": sorted(candidates, key=lambda item: (len(item), item.lower()))[0],
        "entryCount": len(entries),
    }


payload = {
    "sshOk": True,
    "host": socket.getfqdn() or socket.gethostname(),
    "user": getpass.getuser(),
    "patchPath": validate_patch_path(patch_path),
}
print("__PATCHSCOPE_JSON_START__")
print(json.dumps(payload))
print("__PATCHSCOPE_JSON_END__")
'''
    return script.replace("__PATCHSCOPE_SSH_TEST_JSON__", json.dumps(json.dumps(payload)))


def clean_ssh_output(output):
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output or "")
    lines = []
    for line in text.replace("\r", "").splitlines():
        if "password:" in line.lower():
            continue
        lines.append(line)
    return "\n".join(lines)


def ssh_error_message(output, returncode):
    text = clean_ssh_output(output).strip()
    lower = text.lower()
    if "permissionerror" in lower or "[errno 13]" in lower:
        path_match = re.search(r"Permission denied:\s*['\"]([^'\"]+)['\"]", text, re.I)
        if path_match:
            return "Target filesystem permission denied for %s. Create the directory with write access for the SSH user, or choose another backup destination." % path_match.group(1)
        return "Target filesystem permission denied. Create the backup directory with write access for the SSH user, or choose another backup destination."
    matches = re.findall(r"(?:RuntimeError|ValueError|Exception|PermissionError):\s*(.+)", text)
    if matches:
        return matches[-1].strip()
    if "permission denied" in lower:
        if "publickey" in lower or "password" in lower or "keyboard-interactive" in lower or "please try again" in lower:
            return "SSH permission denied. Check the username/password for the selected host."
        return "Target command permission denied. Review remote file and directory permissions for the selected operation."
    if "could not resolve hostname" in lower or "name or service not known" in lower:
        return "Host name could not be resolved from the PatchPilot server."
    if "connection timed out" in lower or "operation timed out" in lower:
        return "SSH connection timed out from the PatchPilot server after %s seconds. Slow hosts can be handled by increasing PATCHSCOPE_SSH_CONNECT_TIMEOUT." % SSH_CONNECT_TIMEOUT
    if "no route to host" in lower:
        return "No route to host from the PatchPilot server."
    if "connection refused" in lower:
        return "SSH connection refused by the target host."
    return text or "SSH command failed with exit code %s." % returncode


def build_ssh_command(profile, script):
    ssh = shutil.which("ssh")
    sshpass = shutil.which("sshpass")
    if not ssh:
        raise RuntimeError("No SSH client found on the PatchPilot server.")
    remote_command = "sh -lc " + shlex.quote(script)
    base = [
        ssh,
        "-p",
        str(profile["port"]),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=%s" % KNOWN_HOSTS,
        "-o",
        "ConnectTimeout=%s" % SSH_CONNECT_TIMEOUT,
        "%s@%s" % (profile["user"], profile["host"]),
        remote_command,
    ]
    if profile.get("password"):
        if sshpass:
            return [sshpass, "-p", profile["password"]] + base
        return base[:1] + [
            "-o",
            "PreferredAuthentications=password,keyboard-interactive",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
        ] + base[1:]
    return base[:1] + ["-o", "BatchMode=yes"] + base[1:]


def run_local_command(command, timeout=SSH_QUICK_TIMEOUT, max_output=2 * 1024 * 1024):
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise RuntimeError("SSH command timed out after %ss." % timeout)
    output = (stdout or b"") + (stderr or b"")
    output = output[-max_output:]
    if proc.returncode != 0:
        raise RuntimeError(ssh_error_message(output, proc.returncode))
    return clean_ssh_output(output)


def run_ssh_with_password(command, password, timeout=SSH_QUICK_TIMEOUT, max_output=2 * 1024 * 1024):
    if pty is None:
        raise RuntimeError("Password SSH requires sshpass or POSIX pty support on the PatchPilot server.")
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(command[0], command)

    output = b""
    sent_password = False
    deadline = time.time() + timeout
    returncode = None
    try:
        def capture(chunk):
            nonlocal output, sent_password
            if not chunk:
                return
            output += chunk
            output = output[-max_output:]
            if not sent_password and b"password:" in output.lower():
                os.write(fd, (password + "\n").encode("utf-8"))
                sent_password = True

        def read_chunk():
            try:
                return os.read(fd, 4096)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                return b""

        def drain_available():
            drained = False
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
                chunk = read_chunk()
                if not chunk:
                    return drained
                capture(chunk)
                drained = True
            return drained

        while True:
            if time.time() > deadline:
                try:
                    os.kill(pid, 9)
                except Exception:
                    pass
                raise RuntimeError("SSH command timed out after %ss." % timeout)
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                chunk = read_chunk()
                if chunk:
                    capture(chunk)
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                if os.WIFEXITED(status):
                    returncode = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    returncode = 128 + os.WTERMSIG(status)
                else:
                    returncode = 1
                drain_available()
                break
        if returncode != 0:
            raise RuntimeError(ssh_error_message(output, returncode))
        return clean_ssh_output(output)
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def run_ssh(profile, script, timeout=SSH_QUICK_TIMEOUT):
    command = build_ssh_command(profile, script)
    if profile.get("password") and not shutil.which("sshpass"):
        return run_ssh_with_password(command, profile["password"], timeout=timeout)
    return run_local_command(command, timeout=timeout)


def run_local_command_stream(command, timeout=SSH_QUICK_TIMEOUT, on_chunk=None, max_output=2 * 1024 * 1024, cancel_event=None):
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = b""
    deadline = time.time() + timeout
    try:
        fd = proc.stdout.fileno() if proc.stdout else None

        def capture(chunk):
            nonlocal output
            if not chunk:
                return
            output += chunk
            output = output[-max_output:]
            if on_chunk:
                on_chunk(chunk.decode("utf-8", "replace"))

        def drain_available():
            if fd is None:
                return False
            drained = False
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
                chunk = os.read(fd, 4096)
                if not chunk:
                    return drained
                capture(chunk)
                drained = True
            return drained

        while True:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise JobCancelledError("Operation stopped by the user.")
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError("SSH command timed out after %ss." % timeout)
            ready = []
            if fd is not None:
                ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                chunk = os.read(fd, 4096)
                if chunk:
                    capture(chunk)
                elif proc.poll() is not None:
                    break
            elif proc.poll() is not None:
                if drain_available():
                    continue
                break
        drain_available()
        if proc.returncode != 0:
            raise RuntimeError(ssh_error_message(output, proc.returncode))
        return clean_ssh_output(output)
    finally:
        if proc.stdout:
            proc.stdout.close()


def run_ssh_with_password_stream(command, password, timeout=SSH_QUICK_TIMEOUT, on_chunk=None, max_output=2 * 1024 * 1024, cancel_event=None):
    if pty is None:
        raise RuntimeError("Password SSH requires sshpass or POSIX pty support on the PatchPilot server.")
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(command[0], command)

    output = b""
    sent_password = False
    deadline = time.time() + timeout
    returncode = None
    try:
        def capture(chunk):
            nonlocal output, sent_password
            if not chunk:
                return
            output += chunk
            output = output[-max_output:]
            if not sent_password and b"password:" in output.lower():
                os.write(fd, (password + "\n").encode("utf-8"))
                sent_password = True
            if on_chunk:
                on_chunk(chunk.decode("utf-8", "replace"))

        def read_chunk():
            try:
                return os.read(fd, 4096)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                return b""

        def drain_available():
            drained = False
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
                chunk = read_chunk()
                if not chunk:
                    return drained
                capture(chunk)
                drained = True
            return drained

        while True:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.5)
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
                raise JobCancelledError("Operation stopped by the user.")
            if time.time() > deadline:
                try:
                    os.kill(pid, 9)
                except Exception:
                    pass
                raise RuntimeError("SSH command timed out after %ss." % timeout)
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                chunk = read_chunk()
                if chunk:
                    capture(chunk)
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                if os.WIFEXITED(status):
                    returncode = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    returncode = 128 + os.WTERMSIG(status)
                else:
                    returncode = 1
                drain_available()
                break
        if returncode != 0:
            raise RuntimeError(ssh_error_message(output, returncode))
        return clean_ssh_output(output)
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def run_ssh_stream(profile, script, timeout=SSH_QUICK_TIMEOUT, on_chunk=None, cancel_event=None):
    command = build_ssh_command(profile, script)
    if profile.get("password") and not shutil.which("sshpass"):
        return run_ssh_with_password_stream(command, profile["password"], timeout=timeout, on_chunk=on_chunk, cancel_event=cancel_event)
    return run_local_command_stream(command, timeout=timeout, on_chunk=on_chunk, cancel_event=cancel_event)


def parse_discovery_output(output):
    match = re.search(r"__PATCHSCOPE_JSON_START__\s*(\{.*\})\s*__PATCHSCOPE_JSON_END__", output, re.S)
    if not match:
        raise RuntimeError("Discovery completed but did not return valid inventory JSON.")
    return json.loads(match.group(1))


def visible_job_output(output):
    clean = clean_ssh_output(output)
    return re.sub(r"__PATCHSCOPE_JSON_START__\s*\{.*?\}\s*__PATCHSCOPE_JSON_END__", "", clean, flags=re.S).strip()


def concise_error(text):
    value = clean_ssh_output(str(text or "")).strip()
    matches = re.findall(r"(?:RuntimeError|ValueError|Exception):\s*(.+)", value)
    if matches:
        return matches[-1].strip()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1] if lines else "Command failed."


def append_opatch_job(job_id, text):
    if not text:
        return
    with OPATCH_JOB_LOCK:
        job = OPATCH_JOBS.get(job_id)
        if not job:
            return
        job["rawOutput"] = (job.get("rawOutput", "") + text)[-MAX_JOB_OUTPUT:]
        job["output"] = visible_job_output(job["rawOutput"])[-MAX_JOB_OUTPUT:]


def start_opatch_upgrade_job(profile, body):
    job_id = uuid.uuid4().hex
    target_key = acquire_target_job(profile, body, "OPatch upgrade", job_id)
    job = {
        "id": job_id,
        "status": "running",
        "startedAt": time.time(),
        "finishedAt": None,
        "output": "",
        "rawOutput": "",
        "opatch": None,
        "error": "",
        "targetKey": target_key,
    }
    with OPATCH_JOB_LOCK:
        OPATCH_JOBS[job_id] = job

    def worker():
        try:
            append_opatch_job(job_id, "Starting PatchPilot-managed OPatch upgrade job.\n")
            output = run_ssh_stream(
                profile,
                remote_python_command(build_opatch_upgrade_script(body)),
                timeout=1500,
                on_chunk=lambda chunk: append_opatch_job(job_id, chunk),
            )
            payload = parse_discovery_output(output)
            with OPATCH_JOB_LOCK:
                job = OPATCH_JOBS.get(job_id)
                if job:
                    job["status"] = "succeeded"
                    job["opatch"] = payload
                    job["finishedAt"] = time.time()
                    job["output"] = visible_job_output(job.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
        except Exception as error:
            append_opatch_job(job_id, "\n%s\n" % str(error))
            with OPATCH_JOB_LOCK:
                job = OPATCH_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = concise_error(str(error))
                    job["finishedAt"] = time.time()
        finally:
            release_target_job(target_key, job_id)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def opatch_job_snapshot(job_id):
    with OPATCH_JOB_LOCK:
        job = OPATCH_JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job["id"],
            "status": job["status"],
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "output": job.get("output", "")[-50000:],
            "opatch": job.get("opatch"),
            "error": job.get("error", ""),
        }


def append_spb_job(job_id, text):
    if not text:
        return
    with SPB_JOB_LOCK:
        job = SPB_JOBS.get(job_id)
        if not job:
            return
        job["rawOutput"] = (job.get("rawOutput", "") + text)[-MAX_JOB_OUTPUT:]
        job["output"] = visible_job_output(job["rawOutput"])[-MAX_JOB_OUTPUT:]


def spb_phase_payload_from_report(body, phase, report, recovered_from=""):
    report = report or {}
    report_phase = str(report.get("reportPhase") or report.get("phase") or report.get("latestLogPhase") or "").lower()
    if report.get("phaseMatched") is False:
        return None
    if phase and report_phase and report_phase != phase:
        return None
    status_text = str(report.get("reportStatus") or "").lower()
    if re.search(r"success|succeed|succeeded|successful|complete|completed|passed", status_text):
        status = "succeeded"
    elif re.search(r"fail|failed|failure|error", status_text):
        status = "failed"
    else:
        return None
    return {
        "phase": phase,
        "installType": str(body.get("installType") or "").lower(),
        "extraArgs": str(body.get("extraArgs") or ""),
        "command": build_spb_run_script(body),
        "oracleHome": str(body.get("oracleHome") or "").strip(),
        "logDir": str(body.get("logDir") or "").strip(),
        "opatchHeap": {"effective": sanitize_opatch_heap_options(body.get("opatchHeapOptions")), "action": "configured"},
        "phaseLog": "",
        "reportPath": report.get("reportPath") or "",
        "reportStatus": status,
        "summaryLines": report.get("summaryLines") or [],
        "returnCode": 0 if status == "succeeded" else 1,
        "status": status,
        "output": report.get("latestLogText") or "",
        "error": "" if status == "succeeded" else "\n".join((report.get("summaryLines") or [])[-12:]) or recovered_from,
        "recovered": True,
        "recoveryReason": recovered_from,
    }


def recover_spb_phase_from_latest_report(profile, body, phase, reason):
    log_dir = str(body.get("logDir") or "").strip()
    if not log_dir:
        return None
    deadline = time.time() + max(0, SPB_PHASE_TIMEOUT_RECOVERY_WAIT)
    last_error = ""
    while True:
        try:
            output = run_ssh(
                profile,
                remote_python_command(build_spb_report_script(log_dir, "", phase)),
                timeout=ssh_timeout(120),
            )
            report = parse_discovery_output(output)
            payload = spb_phase_payload_from_report(body, phase, report, recovered_from=reason)
            if payload:
                return payload
        except Exception as error:
            last_error = str(error)
        if time.time() >= deadline:
            break
        time.sleep(15)
    if last_error:
        append_message = "Latest SPBAT report recovery did not complete: %s\n" % concise_error(last_error)
    else:
        append_message = "Latest SPBAT report recovery did not find a completed report.\n"
    return {"recoveryError": append_message}


def start_spb_phase_job(profile, body):
    job_id = uuid.uuid4().hex
    phase = str(body.get("phase") or "").lower()
    oracle_home = str(body.get("oracleHome") or "").strip()
    log_dir = str(body.get("logDir") or "").strip()
    target_key = acquire_target_job(profile, body, "SPBAT %s" % (phase or "phase"), job_id)
    job_key = "|".join([target_key, phase, normalize_remote_path(log_dir).lower()])
    job = {
        "id": job_id,
        "phase": phase,
        "jobKey": job_key,
        "status": "running",
        "startedAt": time.time(),
        "finishedAt": None,
        "output": "",
        "rawOutput": "",
        "spb": None,
        "error": "",
        "targetKey": target_key,
    }
    with SPB_JOB_LOCK:
        SPB_JOBS[job_id] = job

    def worker():
        try:
            append_spb_job(job_id, "Starting SPBAT %s job.\n" % (phase or "phase"))
            output = run_ssh_stream(
                profile,
                remote_python_command(build_spb_phase_stream_script(body)),
                timeout=SPB_PHASE_TIMEOUT + SSH_TIMEOUT_GRACE,
                on_chunk=lambda chunk: append_spb_job(job_id, chunk),
            )
            payload = parse_discovery_output(output)
            failed = payload.get("status") == "failed"
            with SPB_JOB_LOCK:
                job = SPB_JOBS.get(job_id)
                if job:
                    job["status"] = "failed" if failed else "succeeded"
                    job["spb"] = payload
                    job["error"] = payload.get("error") or ("SPBAT %s failed." % phase if failed else "")
                    job["finishedAt"] = time.time()
                    job["output"] = visible_job_output(job.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
        except Exception as error:
            error_text = concise_error(str(error))
            append_spb_job(job_id, "\n%s\n" % error_text)
            recovered = recover_spb_phase_from_latest_report(profile, body, phase, error_text)
            if recovered and recovered.get("status") in ("succeeded", "failed"):
                append_spb_job(job_id, "Recovered SPBAT %s status from latest report: %s.\n" % (phase or "phase", recovered.get("status")))
                with SPB_JOB_LOCK:
                    job = SPB_JOBS.get(job_id)
                    if job:
                        job["status"] = "failed" if recovered.get("status") == "failed" else "succeeded"
                        job["spb"] = recovered
                        job["error"] = recovered.get("error") or ("SPBAT %s failed." % phase if recovered.get("status") == "failed" else "")
                        job["finishedAt"] = time.time()
                        job["output"] = visible_job_output(job.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
                return
            if recovered and recovered.get("recoveryError"):
                append_spb_job(job_id, recovered.get("recoveryError"))
            with SPB_JOB_LOCK:
                job = SPB_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = error_text
                    job["finishedAt"] = time.time()
        finally:
            release_target_job(target_key, job_id)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def spb_job_snapshot(job_id):
    with SPB_JOB_LOCK:
        job = SPB_JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job["id"],
            "phase": job.get("phase", ""),
            "status": job["status"],
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "output": job.get("output", "")[-50000:],
            "spb": job.get("spb"),
            "error": job.get("error", ""),
        }


def append_spb_inactive_job(job_id, text):
    if not text:
        return
    with SPB_INACTIVE_JOB_LOCK:
        job = SPB_INACTIVE_JOBS.get(job_id)
        if not job:
            return
        job["rawOutput"] = (job.get("rawOutput", "") + text)[-MAX_JOB_OUTPUT:]
        job["output"] = visible_job_output(job["rawOutput"])[-MAX_JOB_OUTPUT:]


def start_spb_inactive_check_job(profile, body):
    job_id = uuid.uuid4().hex
    oracle_home = str(body.get("oracleHome") or "").strip()
    if not oracle_home:
        raise ValueError("ORACLE_HOME is required for inactive patch review.")
    target_key = acquire_target_job(profile, body, "inactive patch review", job_id)
    cancel_event = threading.Event()
    job = {
        "id": job_id,
        "kind": "check",
        "oracleHome": oracle_home,
        "status": "running",
        "startedAt": time.time(),
        "finishedAt": None,
        "output": "",
        "rawOutput": "",
        "inactive": None,
        "error": "",
        "targetKey": target_key,
        "cancelEvent": cancel_event,
        "cancelRequested": False,
    }
    with SPB_INACTIVE_JOB_LOCK:
        SPB_INACTIVE_JOBS[job_id] = job

    def worker():
        try:
            append_spb_inactive_job(job_id, "Starting PatchPilot inactive patch review job.\n")
            output = run_ssh_stream(
                profile,
                remote_python_command(build_spb_inactive_check_script(body)),
                timeout=SPB_INACTIVE_CHECK_TIMEOUT + SSH_TIMEOUT_GRACE,
                on_chunk=lambda chunk: append_spb_inactive_job(job_id, chunk),
                cancel_event=cancel_event,
            )
            payload = parse_discovery_output(output)
            with SPB_INACTIVE_JOB_LOCK:
                current = SPB_INACTIVE_JOBS.get(job_id)
                if current:
                    current["status"] = "succeeded"
                    current["inactive"] = payload
                    current["finishedAt"] = time.time()
                    current["output"] = visible_job_output(current.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
        except JobCancelledError:
            append_spb_inactive_job(job_id, "\nInactive patch review stopped by the user.\n")
            with SPB_INACTIVE_JOB_LOCK:
                current = SPB_INACTIVE_JOBS.get(job_id)
                if current:
                    current["status"] = "cancelled"
                    current["error"] = "Inactive patch review stopped by the user."
                    current["finishedAt"] = time.time()
        except Exception as error:
            append_spb_inactive_job(job_id, "\n%s\n" % str(error))
            with SPB_INACTIVE_JOB_LOCK:
                current = SPB_INACTIVE_JOBS.get(job_id)
                if current:
                    current["status"] = "failed"
                    current["error"] = concise_error(str(error))
                    current["finishedAt"] = time.time()
        finally:
            release_target_job(target_key, job_id)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_spb_inactive_cleanup_job(profile, body):
    job_id = uuid.uuid4().hex
    oracle_home = str(body.get("oracleHome") or "").strip()
    if not oracle_home:
        raise ValueError("ORACLE_HOME is required for inactive patch cleanup.")
    target_key = acquire_target_job(profile, body, "inactive patch cleanup", job_id)
    oracle_home_key = target_key
    cancel_event = threading.Event()
    job = {
        "id": job_id,
        "kind": "cleanup",
        "oracleHome": oracle_home,
        "oracleHomeKey": oracle_home_key,
        "status": "running",
        "startedAt": time.time(),
        "finishedAt": None,
        "output": "",
        "rawOutput": "",
        "inactive": None,
        "error": "",
        "targetKey": target_key,
        "cancelEvent": cancel_event,
        "cancelRequested": False,
    }
    with SPB_INACTIVE_JOB_LOCK:
        SPB_INACTIVE_JOBS[job_id] = job

    def worker():
        try:
            append_spb_inactive_job(job_id, "Starting PatchPilot inactive patch cleanup job.\n")
            output = run_ssh_stream(
                profile,
                remote_python_command(build_spb_inactive_delete_script(body)),
                timeout=SPB_INACTIVE_CLEANUP_TIMEOUT + SSH_TIMEOUT_GRACE,
                on_chunk=lambda chunk: append_spb_inactive_job(job_id, chunk),
                cancel_event=cancel_event,
            )
            payload = parse_discovery_output(output)
            failed = payload.get("status") not in ("removed", "already-retained", "none")
            with SPB_INACTIVE_JOB_LOCK:
                job = SPB_INACTIVE_JOBS.get(job_id)
                if job:
                    job["status"] = "failed" if failed else "succeeded"
                    job["inactive"] = payload
                    job["error"] = payload.get("error") or ("Inactive patch cleanup did not complete." if failed else "")
                    job["finishedAt"] = time.time()
                    job["output"] = visible_job_output(job.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
        except JobCancelledError:
            append_spb_inactive_job(job_id, "\nInactive patch cleanup stopped by the user. Review OPatch inventory before retrying.\n")
            with SPB_INACTIVE_JOB_LOCK:
                job = SPB_INACTIVE_JOBS.get(job_id)
                if job:
                    job["status"] = "cancelled"
                    job["error"] = "Inactive patch cleanup stopped by the user. Review OPatch inventory before retrying."
                    job["finishedAt"] = time.time()
        except Exception as error:
            append_spb_inactive_job(job_id, "\n%s\n" % str(error))
            with SPB_INACTIVE_JOB_LOCK:
                job = SPB_INACTIVE_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = concise_error(str(error))
                    job["finishedAt"] = time.time()
        finally:
            release_target_job(target_key, job_id)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def spb_inactive_job_snapshot(job_id):
    with SPB_INACTIVE_JOB_LOCK:
        job = SPB_INACTIVE_JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job["id"],
            "kind": job.get("kind", "cleanup"),
            "oracleHome": job.get("oracleHome", ""),
            "status": job["status"],
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "output": job.get("output", "")[-50000:],
            "inactive": job.get("inactive"),
            "error": job.get("error", ""),
            "cancelRequested": bool(job.get("cancelRequested")),
        }


def cancel_spb_inactive_job(job_id, profile, body):
    already_finished = False
    with SPB_INACTIVE_JOB_LOCK:
        job = SPB_INACTIVE_JOBS.get(job_id)
        if not job:
            raise RuntimeError("Inactive-patch job was not found.")
        if job.get("status") != "running":
            already_finished = True
        else:
            if target_job_key(profile, body) != job.get("targetKey"):
                raise RuntimeError("Cancellation SSH target does not match the running job.")
            expected_home = normalize_remote_path(job.get("oracleHome") or "")
            requested_home = normalize_remote_path(body.get("oracleHome") or "")
            if requested_home != expected_home:
                raise RuntimeError("Cancellation ORACLE_HOME does not match the running job.")
            job["cancelRequested"] = True
            cancel_event = job.get("cancelEvent")
            if cancel_event:
                cancel_event.set()
    if already_finished:
        return spb_inactive_job_snapshot(job_id), None
    append_spb_inactive_job(job_id, "\nStop requested by the user. Closing the SSH command and targeting only inactive-patch OPatch utility processes for this ORACLE_HOME.\n")
    cancel_result = None
    try:
        output = run_ssh(
            profile,
            remote_python_command(build_spb_inactive_cancel_script(body)),
            timeout=ssh_timeout(45),
        )
        cancel_result = parse_discovery_output(output)
        append_spb_inactive_job(job_id, "%s\n" % (cancel_result.get("message") or "Remote stop request completed."))
    except Exception as error:
        append_spb_inactive_job(job_id, "Remote process stop check could not be confirmed: %s\n" % concise_error(str(error)))
        cancel_result = {"status": "warning", "error": concise_error(str(error))}
    return spb_inactive_job_snapshot(job_id), cancel_result


def append_oig_job(job_id, text):
    if not text:
        return
    with OIG_JOB_LOCK:
        job = OIG_JOBS.get(job_id)
        if not job:
            return
        job["rawOutput"] = (job.get("rawOutput", "") + text)[-MAX_JOB_OUTPUT:]
        job["output"] = visible_job_output(job["rawOutput"])[-MAX_JOB_OUTPUT:]


def start_oig_postinstall_job(profile, body):
    job_id = uuid.uuid4().hex
    target_key = acquire_target_job(profile, body, "OIG postinstall", job_id)
    job = {
        "id": job_id,
        "status": "running",
        "startedAt": time.time(),
        "finishedAt": None,
        "output": "",
        "rawOutput": "",
        "oig": None,
        "error": "",
        "targetKey": target_key,
    }
    with OIG_JOB_LOCK:
        OIG_JOBS[job_id] = job

    def worker():
        try:
            append_oig_job(job_id, "Starting OIG patch_oim_wls.sh postinstall job.\n")
            output = run_ssh_stream(
                profile,
                remote_python_command(build_oig_postinstall_stream_script(body)),
                timeout=7200,
                on_chunk=lambda chunk: append_oig_job(job_id, chunk),
            )
            payload = parse_discovery_output(output)
            failed = payload.get("status") == "failed"
            with OIG_JOB_LOCK:
                job = OIG_JOBS.get(job_id)
                if job:
                    job["status"] = "failed" if failed else "succeeded"
                    job["oig"] = payload
                    job["error"] = payload.get("error") or ("OIG postinstall script failed." if failed else "")
                    job["finishedAt"] = time.time()
                    job["output"] = visible_job_output(job.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
        except Exception as error:
            append_oig_job(job_id, "\n%s\n" % str(error))
            with OIG_JOB_LOCK:
                job = OIG_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = concise_error(str(error))
                    job["finishedAt"] = time.time()
        finally:
            release_target_job(target_key, job_id)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def oig_job_snapshot(job_id):
    with OIG_JOB_LOCK:
        job = OIG_JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job["id"],
            "status": job["status"],
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "output": job.get("output", "")[-50000:],
            "oig": job.get("oig"),
            "error": job.get("error", ""),
        }


def append_backup_job(job_id, text):
    if not text:
        return
    with BACKUP_JOB_LOCK:
        job = BACKUP_JOBS.get(job_id)
        if not job:
            return
        job["rawOutput"] = (job.get("rawOutput", "") + text)[-MAX_JOB_OUTPUT:]
        job["output"] = visible_job_output(job["rawOutput"])[-MAX_JOB_OUTPUT:]


def start_backup_job(profile, body):
    job_id = uuid.uuid4().hex
    target_key = acquire_target_job(profile, body, "backup", job_id)
    job = {
        "id": job_id,
        "status": "running",
        "startedAt": time.time(),
        "finishedAt": None,
        "output": "",
        "rawOutput": "",
        "backup": None,
        "error": "",
        "targetKey": target_key,
    }
    with BACKUP_JOB_LOCK:
        BACKUP_JOBS[job_id] = job

    def worker():
        try:
            append_backup_job(job_id, "Starting PatchPilot backup job.\n")
            output = run_ssh_stream(
                profile,
                remote_python_command(build_backup_script(body)),
                timeout=7500,
                on_chunk=lambda chunk: append_backup_job(job_id, chunk),
            )
            payload = parse_discovery_output(output)
            failed = payload.get("status") != "succeeded"
            with BACKUP_JOB_LOCK:
                job = BACKUP_JOBS.get(job_id)
                if job:
                    job["status"] = "failed" if failed else "succeeded"
                    job["backup"] = payload
                    job["error"] = "\n".join(
                        item.get("error", "") for item in payload.get("results", []) if item.get("status") == "failed" and item.get("error")
                    )
                    job["finishedAt"] = time.time()
                    job["output"] = visible_job_output(job.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
        except Exception as error:
            append_backup_job(job_id, "\n%s\n" % str(error))
            with BACKUP_JOB_LOCK:
                job = BACKUP_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = concise_error(str(error))
                    job["finishedAt"] = time.time()
        finally:
            release_target_job(target_key, job_id)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def backup_job_snapshot(job_id):
    with BACKUP_JOB_LOCK:
        job = BACKUP_JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job["id"],
            "status": job["status"],
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "output": job.get("output", "")[-50000:],
            "backup": job.get("backup"),
            "error": job.get("error", ""),
        }


def append_patch_job(job_id, text):
    if not text:
        return
    with PATCH_JOB_LOCK:
        job = PATCH_JOBS.get(job_id)
        if not job:
            return
        job["rawOutput"] = (job.get("rawOutput", "") + text)[-MAX_JOB_OUTPUT:]
        job["output"] = visible_job_output(job["rawOutput"])[-MAX_JOB_OUTPUT:]


def start_patch_apply_job(profile, body):
    job_id = uuid.uuid4().hex
    operation = "OPatch dry-run" if body.get("dryRun") else "OPatch apply"
    target_key = acquire_target_job(profile, body, operation, job_id)
    job = {
        "id": job_id,
        "status": "running",
        "startedAt": time.time(),
        "finishedAt": None,
        "output": "",
        "rawOutput": "",
        "patch": None,
        "error": "",
        "targetKey": target_key,
    }
    with PATCH_JOB_LOCK:
        PATCH_JOBS[job_id] = job

    def worker():
        try:
            append_patch_job(job_id, "Starting PatchPilot OPatch apply job.\n")
            output = run_ssh_stream(
                profile,
                remote_python_command(build_patch_apply_script(body)),
                timeout=9000,
                on_chunk=lambda chunk: append_patch_job(job_id, chunk),
            )
            payload = parse_discovery_output(output)
            failed = payload.get("status") == "failed"
            with PATCH_JOB_LOCK:
                job = PATCH_JOBS.get(job_id)
                if job:
                    job["status"] = "failed" if failed else "succeeded"
                    job["patch"] = payload
                    job["error"] = payload.get("error") or ("OPatch apply failed." if failed else "")
                    job["finishedAt"] = time.time()
                    job["output"] = visible_job_output(job.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
        except Exception as error:
            append_patch_job(job_id, "\n%s\n" % str(error))
            with PATCH_JOB_LOCK:
                job = PATCH_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = concise_error(str(error))
                    job["finishedAt"] = time.time()
        finally:
            release_target_job(target_key, job_id)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def patch_job_snapshot(job_id):
    with PATCH_JOB_LOCK:
        job = PATCH_JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job["id"],
            "status": job["status"],
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "output": job.get("output", "")[-50000:],
            "patch": job.get("patch"),
            "error": job.get("error", ""),
        }


def append_rollback_job(job_id, text):
    if not text:
        return
    with ROLLBACK_JOB_LOCK:
        job = ROLLBACK_JOBS.get(job_id)
        if not job:
            return
        job["rawOutput"] = (job.get("rawOutput", "") + text)[-MAX_JOB_OUTPUT:]
        job["output"] = visible_job_output(job["rawOutput"])[-MAX_JOB_OUTPUT:]


def start_patch_rollback_job(profile, body):
    job_id = uuid.uuid4().hex
    target_key = acquire_target_job(profile, body, "OPatch rollback", job_id)
    job = {
        "id": job_id,
        "status": "running",
        "startedAt": time.time(),
        "finishedAt": None,
        "output": "",
        "rawOutput": "",
        "rollback": None,
        "error": "",
        "targetKey": target_key,
    }
    with ROLLBACK_JOB_LOCK:
        ROLLBACK_JOBS[job_id] = job

    def worker():
        try:
            append_rollback_job(job_id, "Starting PatchPilot OPatch rollback job.\n")
            output = run_ssh_stream(
                profile,
                remote_python_command(build_patch_rollback_script(body)),
                timeout=9000,
                on_chunk=lambda chunk: append_rollback_job(job_id, chunk),
            )
            payload = parse_discovery_output(output)
            failed = payload.get("status") == "failed"
            with ROLLBACK_JOB_LOCK:
                job = ROLLBACK_JOBS.get(job_id)
                if job:
                    job["status"] = "failed" if failed else "succeeded"
                    job["rollback"] = payload
                    job["error"] = payload.get("error") or ("OPatch rollback failed." if failed else "")
                    job["finishedAt"] = time.time()
                    job["output"] = visible_job_output(job.get("rawOutput", ""))[-MAX_JOB_OUTPUT:]
        except Exception as error:
            append_rollback_job(job_id, "\n%s\n" % str(error))
            with ROLLBACK_JOB_LOCK:
                job = ROLLBACK_JOBS.get(job_id)
                if job:
                    job["status"] = "failed"
                    job["error"] = concise_error(str(error))
                    job["finishedAt"] = time.time()
        finally:
            release_target_job(target_key, job_id)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def rollback_job_snapshot(job_id):
    with ROLLBACK_JOB_LOCK:
        job = ROLLBACK_JOBS.get(job_id)
        if not job:
            return None
        return {
            "id": job["id"],
            "status": job["status"],
            "startedAt": job["startedAt"],
            "finishedAt": job["finishedAt"],
            "output": job.get("output", "")[-50000:],
            "rollback": job.get("rollback"),
            "error": job.get("error", ""),
        }


class PatchScopeHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path == "/healthz":
            version_path = ROOT / "VERSION"
            version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else "development"
            payload = json.dumps({"ok": True, "service": "patchpilot", "version": version}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/api/update-readiness":
            with ACTIVE_TARGET_JOB_LOCK:
                active_jobs = [dict(job) for job in ACTIVE_TARGET_JOBS.values()]
            version_path = ROOT / "VERSION"
            version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else "development"
            payload = json.dumps({
                "ok": True,
                "ready": not active_jobs,
                "version": version,
                "activeJobCount": len(active_jobs),
                "activeJobs": active_jobs,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        super().do_GET()

    def do_POST(self):
        try:
            body = read_json_body(self)
            if self.path == "/api/opatch/upgrade/status":
                job_id = str(body.get("jobId") or "").strip()
                if not job_id:
                    raise ValueError("OPatch upgrade job id is required.")
                snapshot = opatch_job_snapshot(job_id)
                if not snapshot:
                    raise RuntimeError("OPatch upgrade job was not found.")
                send_json(self, 200, {"ok": True, "job": snapshot})
                return
            if self.path == "/api/spb/run-phase/status":
                job_id = str(body.get("jobId") or "").strip()
                if not job_id:
                    raise ValueError("SPBAT phase job id is required.")
                snapshot = spb_job_snapshot(job_id)
                if not snapshot:
                    raise RuntimeError("SPBAT phase job was not found.")
                send_json(self, 200, {"ok": True, "job": snapshot})
                return
            if self.path in ("/api/spb/inactive/status", "/api/spb/inactive/delete/status"):
                job_id = str(body.get("jobId") or "").strip()
                if not job_id:
                    raise ValueError("Inactive patch job id is required.")
                snapshot = spb_inactive_job_snapshot(job_id)
                if not snapshot:
                    raise RuntimeError("Inactive patch job was not found.")
                send_json(self, 200, {"ok": True, "job": snapshot})
                return
            if self.path == "/api/oig/script/status":
                job_id = str(body.get("jobId") or "").strip()
                if not job_id:
                    raise ValueError("OIG postinstall job id is required.")
                snapshot = oig_job_snapshot(job_id)
                if not snapshot:
                    raise RuntimeError("OIG postinstall job was not found.")
                send_json(self, 200, {"ok": True, "job": snapshot})
                return
            if self.path == "/api/backup/status":
                job_id = str(body.get("jobId") or "").strip()
                if not job_id:
                    raise ValueError("Backup job id is required.")
                snapshot = backup_job_snapshot(job_id)
                if not snapshot:
                    raise RuntimeError("Backup job was not found.")
                send_json(self, 200, {"ok": True, "job": snapshot})
                return
            if self.path == "/api/patch/apply/status":
                job_id = str(body.get("jobId") or "").strip()
                if not job_id:
                    raise ValueError("Patch apply job id is required.")
                snapshot = patch_job_snapshot(job_id)
                if not snapshot:
                    raise RuntimeError("Patch apply job was not found.")
                send_json(self, 200, {"ok": True, "job": snapshot})
                return
            if self.path == "/api/patch/rollback/status":
                job_id = str(body.get("jobId") or "").strip()
                if not job_id:
                    raise ValueError("Patch rollback job id is required.")
                snapshot = rollback_job_snapshot(job_id)
                if not snapshot:
                    raise RuntimeError("Patch rollback job was not found.")
                send_json(self, 200, {"ok": True, "job": snapshot})
                return
            profile = ssh_profile(body)
            if self.path == "/api/ssh/test":
                output = run_ssh(profile, remote_python_command(build_ssh_test_script(body)), timeout=ssh_timeout(20))
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "validation": payload, "output": output})
                return
            if self.path == "/api/discover-homes":
                output = run_ssh(profile, remote_python_command(DISCOVER_SCRIPT), timeout=ssh_timeout(45))
                inventory = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "inventory": inventory})
                return
            if self.path == "/api/readme/load":
                patch_path = str(body.get("patchPath") or "").strip()
                if not patch_path:
                    raise ValueError("Patch directory on server is required.")
                output = run_ssh(profile, remote_python_command(build_readme_script(patch_path)), timeout=ssh_timeout(35))
                payload = parse_discovery_output(output)
                if not payload.get("readmePath"):
                    raise RuntimeError("No README file was found under %s." % patch_path)
                send_json(self, 200, {"ok": True, "readme": payload})
                return
            if self.path in ("/api/shutdown/verify", "/api/services-up/verify"):
                home = body.get("home") or {}
                oracle_home = str(home.get("oracleHome") or body.get("oracleHome") or "").strip()
                if not oracle_home:
                    raise ValueError("Selected ORACLE_HOME is required for service verification.")
                target = {
                    "oracleHome": oracle_home,
                    "domainHome": str(home.get("domainHome") or body.get("domainHome") or "").strip(),
                    "instanceHome": str(home.get("instanceHome") or body.get("instanceHome") or "").strip(),
                    "product": str(home.get("product") or body.get("product") or "").strip(),
                    "label": str(home.get("label") or body.get("label") or "").strip(),
                    "mode": "servicesUp" if self.path == "/api/services-up/verify" else "shutdown",
                    "expectedServices": body.get("expectedServices") or home.get("services") or [],
                }
                output = run_ssh(profile, remote_python_command(build_verify_shutdown_script(target)), timeout=ssh_timeout(35))
                payload = parse_discovery_output(output)
                if payload.get("status") == "error":
                    raise RuntimeError(payload.get("error") or "Service verification failed.")
                if self.path == "/api/services-up/verify":
                    send_json(self, 200, {"ok": True, "servicesUp": payload})
                else:
                    send_json(self, 200, {"ok": True, "shutdown": payload})
                return
            if self.path == "/api/shutdown/stop":
                output = run_ssh(profile, remote_python_command(build_shutdown_stop_script(body)), timeout=1800)
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "shutdown": payload})
                return
            if self.path == "/api/shutdown/kill":
                output = run_ssh(profile, remote_python_command(build_shutdown_kill_script(body)), timeout=120)
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "shutdown": payload})
                return
            if self.path == "/api/backup/start":
                job_id = start_backup_job(profile, body)
                send_json(self, 200, {"ok": True, "jobId": job_id})
                return
            if self.path == "/api/backup/preflight":
                output = run_ssh(profile, remote_python_command(build_backup_preflight_script(body)), timeout=1200)
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "backup": payload})
                return
            if self.path == "/api/spb/prepare":
                patch_path = str(body.get("patchPath") or "").strip()
                if not patch_path:
                    raise ValueError("SPB download directory on server is required.")
                log_dir = str(body.get("logDir") or "").strip()
                change_ref = str(body.get("changeRef") or "spb-run").strip()
                output = run_ssh(profile, remote_python_command(build_spb_prepare_script(patch_path, log_dir, change_ref)), timeout=ssh_timeout(35))
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "spb": payload})
                return
            if self.path == "/api/spb/report/latest":
                log_dir = str(body.get("logDir") or "").strip()
                if not log_dir:
                    raise ValueError("SPBAT log directory is required.")
                phase_log = str(body.get("phaseLog") or "").strip()
                output = run_ssh(profile, remote_python_command(build_spb_report_script(log_dir, phase_log, phase)), timeout=ssh_timeout(35))
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "report": payload})
                return
            if self.path == "/api/spb/prestart-cleanup":
                output = run_ssh(profile, remote_python_command(build_spb_prestart_cleanup_script(body)), timeout=ssh_timeout(600))
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "cleanup": payload})
                return
            if self.path == "/api/spb/inactive/check":
                output = run_ssh(profile, remote_python_command(build_spb_inactive_check_script(body)), timeout=SPB_INACTIVE_CHECK_TIMEOUT + SSH_TIMEOUT_GRACE)
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "inactive": payload})
                return
            if self.path == "/api/spb/inactive/check/start":
                job_id = start_spb_inactive_check_job(profile, body)
                send_json(self, 200, {"ok": True, "jobId": job_id})
                return
            if self.path == "/api/spb/inactive/delete":
                output = run_ssh(profile, remote_python_command(build_spb_inactive_delete_script(body)), timeout=SPB_INACTIVE_CLEANUP_TIMEOUT + SSH_TIMEOUT_GRACE)
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "inactive": payload})
                return
            if self.path == "/api/spb/inactive/delete/start":
                job_id = start_spb_inactive_cleanup_job(profile, body)
                send_json(self, 200, {"ok": True, "jobId": job_id})
                return
            if self.path == "/api/spb/inactive/cancel":
                job_id = str(body.get("jobId") or "").strip()
                if not job_id:
                    raise ValueError("Inactive patch job id is required.")
                snapshot, cancellation = cancel_spb_inactive_job(job_id, profile, body)
                send_json(self, 200, {"ok": True, "job": snapshot, "cancellation": cancellation})
                return
            if self.path == "/api/oig/profile":
                output = run_ssh(profile, remote_python_command(build_oig_profile_script(body)), timeout=ssh_timeout(120))
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "profile": payload})
                return
            if self.path == "/api/oig/log/tail":
                output = run_ssh(profile, remote_python_command(build_oig_log_tail_script(body)), timeout=ssh_timeout(45))
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "log": payload})
                return
            if self.path == "/api/opatch/version":
                home = body.get("home") or {}
                oracle_home = str(home.get("oracleHome") or body.get("oracleHome") or "").strip()
                if not oracle_home:
                    raise ValueError("Selected ORACLE_HOME is required for OPatch validation.")
                output = run_ssh(profile, remote_python_command(build_opatch_version_script(oracle_home)), timeout=ssh_timeout(35))
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "opatch": payload})
                return
            if self.path == "/api/opatch/upgrade":
                output = run_ssh(profile, remote_python_command(build_opatch_upgrade_script(body)), timeout=1500)
                payload = parse_discovery_output(output)
                send_json(self, 200, {"ok": True, "opatch": payload})
                return
            if self.path == "/api/opatch/upgrade/start":
                job_id = start_opatch_upgrade_job(profile, body)
                send_json(self, 200, {"ok": True, "jobId": job_id})
                return
            if self.path == "/api/spb/run-phase":
                command = build_spb_run_script(body)
                output = run_ssh(profile, command, timeout=1800)
                send_json(self, 200, {"ok": True, "output": output})
                return
            if self.path == "/api/spb/run-phase/start":
                job_id = start_spb_phase_job(profile, body)
                send_json(self, 200, {"ok": True, "jobId": job_id})
                return
            if self.path == "/api/oig/script/start":
                job_id = start_oig_postinstall_job(profile, body)
                send_json(self, 200, {"ok": True, "jobId": job_id})
                return
            if self.path == "/api/patch/apply/start":
                job_id = start_patch_apply_job(profile, body)
                send_json(self, 200, {"ok": True, "jobId": job_id})
                return
            if self.path == "/api/patch/rollback/start":
                job_id = start_patch_rollback_job(profile, body)
                send_json(self, 200, {"ok": True, "jobId": job_id})
                return
            send_json(self, 404, {"ok": False, "error": "Unknown API route."})
        except Exception as error:
            send_json(self, 400, {"ok": False, "error": str(error)})


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    os.chdir(str(ROOT))
    with ReusableTCPServer((HOST, PORT), PatchScopeHandler) as httpd:
        print(f"PatchPilot listening on http://{HOST}:{PORT}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
