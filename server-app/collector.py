import json
import os
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse


DEFAULT_SERVER_HEALTH_THRESHOLDS = {
    "load1": {
        "warning": {"ge": 8},
        "critical": {"ge": 16},
    },
    "memoryUsedPercent": {
        "warning": {"ge": 85},
        "critical": {"ge": 92},
    },
    "rootDiskUsedPercent": {
        "warning": {"ge": 70},
        "critical": {"ge": 80},
    },
    "refreshDiskUsedPercent": {
        "warning": {"ge": 70},
        "critical": {"ge": 80},
    },
    "cpuIoWaitPercent": {
        "warning": {"ge": 15},
        "critical": {"ge": 30},
    },
    "cpuIdlePercent": {
        "warning": {"le": 15},
        "critical": {"le": 5},
    },
}


def lines(text):
    if not text:
        return []
    return [line.strip() for line in str(text).splitlines() if line.strip()]


def percent(part, whole):
    if not whole:
        return 0
    return round((float(part) / float(whole)) * 100, 1)


def safe_float(value):
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int_safe(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run_parallel_tasks(tasks, max_workers=6):
    tasks = [(name, func) for name, func in (tasks or []) if name and callable(func)]
    if not tasks:
        return {}
    workers = max(1, min(int(max_workers or 1), len(tasks)))
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(func): name for name, func in tasks}
        for future, name in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = exc
    return results


def task_result(results, name, default=None):
    value = (results or {}).get(name, default)
    if isinstance(value, Exception):
        return default
    return value


def threshold_value(value):
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def threshold_matches(value, rule):
    if not isinstance(rule, dict) or not rule:
        return False
    numeric_value = threshold_value(value)
    matched = False
    for operator, threshold in rule.items():
        operator = str(operator).strip().lower()
        numeric_threshold = threshold_value(threshold)
        if operator in ("gt", "ge", "lt", "le"):
            if numeric_value is None or numeric_threshold is None:
                return False
            matched = True
            if operator == "gt" and not (numeric_value > numeric_threshold):
                return False
            if operator == "ge" and not (numeric_value >= numeric_threshold):
                return False
            if operator == "lt" and not (numeric_value < numeric_threshold):
                return False
            if operator == "le" and not (numeric_value <= numeric_threshold):
                return False
            continue
        left = str(value).strip().lower()
        right = str(threshold).strip().lower()
        matched = True
        if operator == "eq" and left != right:
            return False
        if operator == "ne" and left == right:
            return False
    return matched


def threshold_severity(value, rules):
    rules = rules or {}
    if threshold_matches(value, rules.get("critical")):
        return "critical"
    if threshold_matches(value, rules.get("warning")):
        return "warning"
    return "healthy"


def build_server_health(server_snapshot):
    server_snapshot = server_snapshot or {}
    if not server_snapshot.get("reachable"):
        return {
            "status": "down",
            "checks": {},
            "thresholds": DEFAULT_SERVER_HEALTH_THRESHOLDS,
        }

    uptime = server_snapshot.get("uptime") or {}
    memory = server_snapshot.get("memory") or {}
    root_disk = server_snapshot.get("rootDisk") or {}
    refresh_disk = server_snapshot.get("refreshDisk") or {}
    cpu_breakdown = uptime.get("cpuBreakdown") or {}

    checks = {
        "load1": {
            "label": "Load Average (1m)",
            "value": uptime.get("load1"),
            "severity": threshold_severity(uptime.get("load1"), DEFAULT_SERVER_HEALTH_THRESHOLDS["load1"]),
        },
        "memoryUsedPercent": {
            "label": "Memory Used",
            "value": memory.get("usedPercent"),
            "severity": threshold_severity(
                memory.get("usedPercent"),
                DEFAULT_SERVER_HEALTH_THRESHOLDS["memoryUsedPercent"],
            ),
        },
        "rootDiskUsedPercent": {
            "label": "Root Disk Used",
            "value": root_disk.get("usedPercent"),
            "severity": threshold_severity(
                root_disk.get("usedPercent"),
                DEFAULT_SERVER_HEALTH_THRESHOLDS["rootDiskUsedPercent"],
            ),
        },
        "refreshDiskUsedPercent": {
            "label": "Refresh Disk Used",
            "value": refresh_disk.get("usedPercent"),
            "severity": threshold_severity(
                refresh_disk.get("usedPercent"),
                DEFAULT_SERVER_HEALTH_THRESHOLDS["refreshDiskUsedPercent"],
            ) if refresh_disk.get("size") else "healthy",
        },
        "cpuIoWaitPercent": {
            "label": "CPU IO Wait",
            "value": cpu_breakdown.get("ioWaitPercent"),
            "severity": threshold_severity(
                cpu_breakdown.get("ioWaitPercent"),
                DEFAULT_SERVER_HEALTH_THRESHOLDS["cpuIoWaitPercent"],
            ),
        },
        "cpuIdlePercent": {
            "label": "CPU Idle",
            "value": cpu_breakdown.get("idlePercent"),
            "severity": threshold_severity(
                cpu_breakdown.get("idlePercent"),
                DEFAULT_SERVER_HEALTH_THRESHOLDS["cpuIdlePercent"],
            ),
        },
    }

    overall = "healthy"
    for item in checks.values():
        severity = item.get("severity")
        if severity == "critical":
            overall = "critical"
            break
        if severity == "warning" and overall == "healthy":
            overall = "warning"

    return {
        "status": overall,
        "checks": checks,
        "thresholds": DEFAULT_SERVER_HEALTH_THRESHOLDS,
    }


def run_local(command, timeout=25):
    try:
        process = subprocess.Popen(
            ["bash", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        output, _ = process.communicate(timeout=timeout)
        return {"exit_code": process.returncode, "output": (output or "").strip()}
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        return {"exit_code": 1, "output": ((output or "").strip() or "Local command timed out.")}


def run_ssh(target, command, timeout=25):
    auth_type = str(target.get("authType") or "password").lower()
    remote_command = "bash -lc {0}".format(shlex.quote(command))
    if target.get("sudoRequired"):
        if target.get("password"):
            remote_command = (
                "if [ \"$(id -u)\" -eq 0 ]; then "
                "bash -lc {cmd}; "
                "else "
                "printf '%s\\n' {password} | sudo -S -p '' bash -lc {cmd}; "
                "fi"
            ).format(
                cmd=shlex.quote(command),
                password=shlex.quote(str(target.get("password") or "")),
            )
        else:
            remote_command = (
                "if [ \"$(id -u)\" -eq 0 ]; then "
                "bash -lc {cmd}; "
                "else "
                "sudo -n bash -lc {cmd}; "
                "fi"
            ).format(cmd=shlex.quote(command))
    ssh_args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(target.get("port") or 22),
    ]

    private_key_path = target.get("privateKeyPath")
    if auth_type == "private_key" and private_key_path:
        ssh_args.extend(["-i", private_key_path])

    ssh_args.append("{0}@{1}".format(target.get("username"), target.get("host")))
    ssh_args.append(remote_command)

    command_args = list(ssh_args)
    if auth_type == "password":
        command_args = ["sshpass", "-p", str(target.get("password") or "")] + ssh_args
    elif auth_type == "private_key" and target.get("passphrase"):
        command_args = [
            "sshpass",
            "-P",
            "Enter passphrase for key",
            "-p",
            str(target.get("passphrase") or ""),
        ] + ssh_args

    try:
        process = subprocess.run(
            command_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=timeout,
            check=False,
        )
        return {
            "exit_code": process.returncode,
            "output": (process.stdout or "").strip(),
        }
    except FileNotFoundError as exc:
        return {"exit_code": 1, "output": "{0} not found on the monitoring host.".format(exc.filename)}
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": 1, "output": ((exc.stdout or "").strip() or "SSH command timed out.")}


def run_target(target, command, timeout=25):
    if target.get("mode") == "local":
        return run_local(command, timeout=timeout)
    return run_ssh(target, command, timeout=timeout)


def _matching_ssh_profiles(primary, secondary):
    primary = primary or {}
    secondary = secondary or {}
    return (
        str(primary.get("host") or "").strip() == str(secondary.get("host") or "").strip()
        and str(primary.get("port") or 22).strip() == str(secondary.get("port") or 22).strip()
        and str(primary.get("username") or "").strip() == str(secondary.get("username") or "").strip()
        and str(primary.get("sshMode") or "").strip() == str(secondary.get("sshMode") or "").strip()
        and str(primary.get("authType") or "").strip() == str(secondary.get("authType") or "").strip()
    )


def _effective_environment_server(environment):
    server = dict(environment.get("server") or {})
    weblogic = environment.get("weblogic") or {}
    admin_host = weblogic.get("adminHost") or {}
    if _matching_ssh_profiles(server, admin_host):
        if not str(server.get("password") or "").strip() and str(admin_host.get("password") or "").strip():
            server["password"] = admin_host.get("password") or ""
        if not str(server.get("privateKeyPath") or "").strip() and str(admin_host.get("privateKeyPath") or "").strip():
            server["privateKeyPath"] = admin_host.get("privateKeyPath") or ""
        if not str(server.get("passphrase") or "").strip() and str(admin_host.get("passphrase") or "").strip():
            server["passphrase"] = admin_host.get("passphrase") or ""
    return server


def parse_memory(text):
    for line in lines(text):
        if line.startswith("Mem:"):
            parts = re.split(r"\s+", line)
            if len(parts) >= 7:
                total_mb = int(parts[1])
                used_mb = int(parts[2])
                free_mb = int(parts[3])
                available_mb = int(parts[6])
                return {
                    "totalMb": total_mb,
                    "usedMb": used_mb,
                    "freeMb": free_mb,
                    "availableMb": available_mb,
                    "usedPercent": percent(used_mb, total_mb),
                }
    return None


def parse_disk(text):
    output_lines = lines(text)
    if len(output_lines) < 2:
        return None
    parts = re.split(r"\s+", output_lines[-1])
    if len(parts) < 6:
        return None
    return {
        "filesystem": parts[0],
        "size": parts[1],
        "used": parts[2],
        "available": parts[3],
        "usedPercent": float(parts[4].replace("%", "")),
        "mount": parts[5],
    }


def humanize_bytes(value):
    if value in (None, ""):
        return None
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    amount = float(value)
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024.0
        index += 1
    if index == 0:
        return "{0:.0f} {1}".format(amount, units[index])
    return "{0:.1f} {1}".format(amount, units[index])


def resolve_existing_directory(target, candidates):
    paths = [str(item or "").strip() for item in (candidates or []) if str(item or "").strip()]
    if not paths:
        return ""
    command = "for path in {0}; do if [ -d \"$path\" ]; then printf '%s' \"$path\"; break; fi; done".format(
        " ".join(shlex.quote(path) for path in paths)
    )
    result = run_target(target, command)
    if result.get("exit_code") != 0:
        return ""
    return str(result.get("output") or "").strip()


def directory_size_label(target, directory_path):
    path = str(directory_path or "").strip()
    if not path:
        return None
    result = run_target(
        target,
        "if [ -d {0} ]; then du -sk {0} 2>/dev/null | awk '{{print $1}}'; fi".format(shlex.quote(path)),
    )
    if result.get("exit_code") != 0:
        return None
    output = str(result.get("output") or "").strip()
    if output.isdigit():
        return humanize_bytes(int(output) * 1024)
    return output or None


def schema_match_files(target, schema_directory):
    path = str(schema_directory or "").strip()
    if not path:
        return []
    result = run_target(
        target,
        "if [ -d {0} ]; then cd {0} && grep -RilE 'attribute|objectClasses' . 2>/dev/null | sed 's#^\\./##'; fi".format(
            shlex.quote(path)
        ),
    )
    if result.get("exit_code") != 0:
        return []
    return [line for line in lines(result.get("output")) if line]


def parse_meminfo_proc(text):
    values = {}
    for raw_line in lines(text):
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        parts = value.strip().split()
        if not parts:
            continue
        try:
            values[key.strip()] = int(parts[0]) * 1024
        except (TypeError, ValueError):
            continue

    total_bytes = values.get("MemTotal")
    available_bytes = values.get("MemAvailable")
    if total_bytes is None:
        return None
    if available_bytes is None:
        available_bytes = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
    used_bytes = max(total_bytes - available_bytes, 0)
    return {
        "totalBytes": total_bytes,
        "usedBytes": used_bytes,
        "availableBytes": available_bytes,
        "totalMb": int(round(total_bytes / (1024.0 * 1024.0))),
        "usedMb": int(round(used_bytes / (1024.0 * 1024.0))),
        "availableMb": int(round(available_bytes / (1024.0 * 1024.0))),
        "usedPercent": percent(used_bytes, total_bytes),
    }


def parse_disk_bytes(text):
    output_lines = lines(text)
    if len(output_lines) < 2:
        return None
    parts = re.split(r"\s+", output_lines[-1])
    if len(parts) < 6:
        return None
    try:
        size_bytes = int(parts[1])
        used_bytes = int(parts[2])
        available_bytes = int(parts[3])
    except (TypeError, ValueError):
        return None
    return {
        "filesystem": parts[0],
        "totalBytes": size_bytes,
        "usedBytes": used_bytes,
        "availableBytes": available_bytes,
        "size": humanize_bytes(size_bytes),
        "used": humanize_bytes(used_bytes),
        "available": humanize_bytes(available_bytes),
        "usedPercent": float(parts[4].replace("%", "")),
        "mount": parts[5],
    }


def read_cpu_stat_snapshot(text):
    for raw_line in lines(text):
        if not raw_line.startswith("cpu "):
            continue
        parts = raw_line.split()
        values = []
        for item in parts[1:11]:
            try:
                values.append(int(item))
            except (TypeError, ValueError):
                values.append(0)
        keys = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice"]
        return dict(zip(keys, values))
    return None


def cpu_breakdown(first, second):
    if not first or not second:
        return {
            "userPercent": None,
            "systemPercent": None,
            "ioWaitPercent": None,
            "idlePercent": None,
        }

    def total(values):
        return sum(values.get(key, 0) for key in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"))

    idle_first = first.get("idle", 0) + first.get("iowait", 0)
    idle_second = second.get("idle", 0) + second.get("iowait", 0)
    non_idle_first = total(first) - idle_first
    non_idle_second = total(second) - idle_second
    total_delta = max((idle_second + non_idle_second) - (idle_first + non_idle_first), 1)
    user_delta = max((second.get("user", 0) + second.get("nice", 0)) - (first.get("user", 0) + first.get("nice", 0)), 0)
    system_delta = max(
        (second.get("system", 0) + second.get("irq", 0) + second.get("softirq", 0))
        - (first.get("system", 0) + first.get("irq", 0) + first.get("softirq", 0)),
        0,
    )
    io_wait_delta = max(second.get("iowait", 0) - first.get("iowait", 0), 0)
    idle_delta = max(idle_second - idle_first, 0)
    return {
        "userPercent": round((user_delta * 100.0) / total_delta, 2),
        "systemPercent": round((system_delta * 100.0) / total_delta, 2),
        "ioWaitPercent": round((io_wait_delta * 100.0) / total_delta, 2),
        "idlePercent": round((idle_delta * 100.0) / total_delta, 2),
    }


def parse_uptime(text, cpu_count):
    load1 = 0.0
    load5 = 0.0
    load15 = 0.0
    match = re.search(r"load average:\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)", text or "")
    if match:
        load1 = float(match.group(1))
        load5 = float(match.group(2))
        load15 = float(match.group(3))
    return {
        "raw": text or "",
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "cpuCount": cpu_count,
        "cpuPressure": percent(load1, cpu_count) if cpu_count else 0,
    }


def extract_xmx_mb(arguments):
    match = re.search(r"-Xmx(\d+)([mMgG])", arguments or "")
    if not match:
        return None

    amount = int(match.group(1))
    if match.group(2).lower() == "g":
        return amount * 1024
    return amount


def parse_jstat(text):
    output_lines = lines(text)
    if len(output_lines) < 2:
        return None

    headers = re.split(r"\s+", output_lines[0])
    values = re.split(r"\s+", output_lines[1])
    if len(values) < len(headers):
        return None

    raw = {}
    for index, header in enumerate(headers):
        raw[header] = values[index] if index < len(values) else None

    def value_as_int(key):
        value = safe_float(raw.get(key))
        return int(value) if value is not None else None

    return {
        "survivor0Percent": safe_float(raw.get("S0")),
        "survivor1Percent": safe_float(raw.get("S1")),
        "edenPercent": safe_float(raw.get("E")),
        "oldGenPercent": safe_float(raw.get("O")),
        "metaspacePercent": safe_float(raw.get("M")),
        "classSpacePercent": safe_float(raw.get("CCS")),
        "youngGcCount": value_as_int("YGC"),
        "youngGcTimeSeconds": safe_float(raw.get("YGCT")),
        "fullGcCount": value_as_int("FGC"),
        "fullGcTimeSeconds": safe_float(raw.get("FGCT")),
        "concurrentGcCount": value_as_int("CGC"),
        "concurrentGcTimeSeconds": safe_float(raw.get("CGCT")),
        "totalGcTimeSeconds": safe_float(raw.get("GCT")),
    }


def parse_sectioned_output(text):
    sections = {}
    current = None
    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        heading = re.match(r"^-{3}\s*(.*?)\s*-{3}$", stripped)
        if heading:
            current = heading.group(1).strip()
            sections[current] = []
            continue
        if current:
            sections.setdefault(current, []).append(raw_line.rstrip())
    return sections


def parse_key_value_banner_output(text):
    banner = ""
    values = {}
    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if not banner and ":" not in stripped:
            banner = stripped
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        values[key.strip()] = value.strip()
    return {
        "banner": banner,
        "values": values,
    }


def parse_oud_status(text):
    summary = {}
    listeners = []
    backends = []
    sections = parse_sectioned_output(text)

    for section_name in ("Server Status", "Server Details"):
        for raw_line in sections.get(section_name, []):
            stripped = raw_line.strip()
            if ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            summary[key.strip()] = value.strip()

    for raw_line in sections.get("Connection Handlers", []):
        stripped = raw_line.strip()
        if (
            not stripped
            or stripped.startswith("Address:Port")
            or stripped.startswith("-------------")
        ):
            continue
        match = re.match(r"^(.*?)\s+:\s+(.*?)\s+:\s+(.*?)$", stripped)
        if not match:
            continue
        listeners.append({
            "addressPort": match.group(1).strip(),
            "protocol": match.group(2).strip(),
            "state": match.group(3).strip(),
        })

    block = {}

    def flush_backend():
        if not block:
            return
        entries_value = block.get("Entries")
        backends.append({
            "baseDn": block.get("Base DN"),
            "backendId": block.get("Backend ID"),
            "entries": int(entries_value) if entries_value and str(entries_value).isdigit() else entries_value,
            "replication": block.get("Replication"),
        })
        block.clear()

    for raw_line in sections.get("Data Sources", []):
        stripped = raw_line.strip()
        if not stripped:
            flush_backend()
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        block[key.strip()] = value.strip()
    flush_backend()

    return {
        "summary": summary,
        "listeners": listeners,
        "backends": backends,
    }


def parse_oud_replication(text):
    sections = []
    current = None
    lines_buffer = []

    def flush_section():
        nonlocal current, lines_buffer
        if current is None:
            return
        table_lines = []
        for raw_line in lines_buffer:
            stripped = raw_line.rstrip()
            if not stripped.strip():
                continue
            if stripped.strip().startswith("Server ") or re.fullmatch(r"[-=:\s]+", stripped.strip()):
                continue
            table_lines.append(stripped)

        parsed_rows = []
        pending_prefix = ""
        for raw_line in table_lines:
            stripped = raw_line.rstrip()
            delimiter_count = len(re.findall(r"\s+:\s+", stripped))
            if delimiter_count == 0:
                pending_prefix += stripped.strip()
                continue
            combined = "{0}{1}".format(pending_prefix, stripped.lstrip()) if pending_prefix else stripped
            pending_prefix = ""
            parts = [part.strip() for part in re.split(r"\s+:\s+", combined) if part is not None]
            server_value = str(parts[0]).strip() if parts else ""
            if not server_value or re.fullmatch(r"[-=:\s]+", server_value) or not re.search(r"[A-Za-z0-9]", server_value):
                continue
            if current.get("enabled"):
                if len(parts) >= 7:
                    parsed_rows.append({
                        "server": server_value,
                        "entries": parts[1],
                        "missingChanges": parts[2],
                        "ageOfOldestMissingChange": parts[3],
                        "port": parts[4],
                        "status": parts[5],
                        "conflicts": parts[6],
                    })
            else:
                if len(parts) >= 3:
                    parsed_rows.append({
                        "server": server_value,
                        "entries": parts[1],
                        "changeLog": parts[2],
                    })

        current["servers"] = parsed_rows
        sections.append(current)
        current = None
        lines_buffer = []

    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        heading_match = re.match(r"^(.*?)\s*-\s*Replication\s+(Enabled|Disabled)$", stripped)
        if heading_match:
            flush_section()
            current = {
                "baseDn": heading_match.group(1).strip(),
                "enabled": heading_match.group(2).strip().lower() == "enabled",
                "servers": [],
            }
            continue
        if current is not None:
            lines_buffer.append(raw_line)

    flush_section()
    return sections


def parse_ldif_entries(text):
    entries = []
    current = {}
    last_key = None

    def looks_like_dn(value):
        text = str(value or "").strip()
        first_key = text.split("=", 1)[0].strip().lower()
        dn_keys = {"cn", "dc", "ou", "o", "uid", "c", "l", "st"}
        return first_key in dn_keys and bool(re.match(r"^[A-Za-z][A-Za-z0-9-]*=[^,]+,\s*[A-Za-z][A-Za-z0-9-]*=", text))

    def add_value(key, value):
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return
        if normalized_key.lower() in ("filter pattern", "returning", "filter is"):
            return
        current.setdefault(normalized_key, []).append(str(value or "").strip())

    for raw_line in str(text or "").splitlines():
        if raw_line.startswith(" "):
            if last_key and current.get(last_key):
                current[last_key][-1] = "{0}{1}".format(current[last_key][-1], raw_line[1:])
            continue
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if current:
                entries.append(current)
                current = {}
                last_key = None
            continue
        if looks_like_dn(line):
            if current and current.get("dn"):
                entries.append(current)
                current = {}
            current["dn"] = [line]
            last_key = "dn"
            continue
        if ":" not in line:
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            add_value(key, value)
            last_key = key.strip()
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        add_value(key, value)
        last_key = key
    if current:
        entries.append(current)
    return entries


def flatten_monitor_entries(entries, instance_name, category):
    rows = []
    ignored = {"dn", "objectClass", "cn"}
    for entry in entries or []:
        entry_name = ", ".join(entry.get("cn") or []) or category
        for key, values in entry.items():
            if key in ignored:
                continue
            for value in values[:8]:
                rows.append({
                    "instanceName": instance_name,
                    "entry": entry_name,
                    "category": category,
                    "name": key,
                    "value": value,
                })
    return rows[:120]


def parse_colon_table_rows(text):
    rows = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("-") or line.lower().startswith("password policy"):
            continue
        if ":" not in line:
            continue
        parts = [part.strip() for part in re.split(r"\s+:\s+", line)]
        if len(parts) >= 2 and parts[0]:
            rows.append(parts)
    return rows


def parse_password_policy_properties(text):
    rows = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("-") or line.lower().startswith("property"):
            continue
        if ":" not in line:
            continue
        parts = [part.strip() for part in re.split(r"\s+:\s+", line)]
        if len(parts) >= 2 and parts[0]:
            rows.append({"property": parts[0], "value": parts[-1]})
    return rows


def oud_secure_command(bin_directory, bind_dn, bind_password, body):
    return (
        "cd {bin_dir} && pwfile=$(mktemp ./pwd.XXXXXX.txt) && "
        "trap 'rm -f \"$pwfile\"' EXIT && printf %s {password} > \"$pwfile\" && chmod 600 \"$pwfile\" && "
        "BIND_DN={bind_dn}; {body}"
    ).format(
        bin_dir=shlex.quote(bin_directory),
        password=shlex.quote(str(bind_password or "")),
        bind_dn=shlex.quote(str(bind_dn or "")),
        body=body,
    )


def collect_oud_monitoring_and_policies(target, settings, bin_directory, instance_name, bind_dn, bind_password):
    admin_port = settings.get("adminPort") or 4444
    if not bind_dn or not bind_password:
        return {
            "monitorEntries": [],
            "monitorError": "OUD root user or password is not configured.",
            "passwordPolicies": [],
            "passwordPolicyError": "OUD root user or password is not configured.",
            "commands": [],
        }
    monitor_specs = [
        ("System Info", "cn=System Information,cn=monitor"),
        ("Version", "cn=Version,cn=monitor"),
        ("Client Connections", "cn=Client Connections,cn=monitor"),
        ("Entry Caches", "cn=Entry Caches,cn=monitor"),
        ("JVM Memory Usage", "cn=JVM Memory Usage,cn=monitor"),
        ("Work Queue", "cn=Work Queue,cn=monitor"),
        ("Active Operations", "cn=Active Operations,cn=monitor"),
    ]
    monitor_rows = []
    monitor_errors = []
    commands = []
    for label, base_dn in monitor_specs:
        body = (
            "LDAPSEARCH=./ldapsearch; [ -x \"$LDAPSEARCH\" ] || LDAPSEARCH=ldapsearch; "
            "\"$LDAPSEARCH\" -h localhost -p {port} -D \"$BIND_DN\" -j \"$pwfile\" "
            "--useSSL --trustAll -s base -b {base_dn} '(objectclass=*)'"
        ).format(
            port=shlex.quote(str(admin_port)),
            base_dn=shlex.quote(base_dn),
        )
        command = oud_secure_command(bin_directory, bind_dn, bind_password, body)
        result = run_target(target, command)
        commands.append({
            "label": "OUD Monitoring - {0}".format(label),
            "command": "cd {0} && ldapsearch -h localhost -p {1} -D \"{2}\" -j <temporary password file> --useSSL --trustAll -s base -b \"{3}\" \"(objectclass=*)\"".format(
                bin_directory,
                admin_port,
                bind_dn or "cn=Directory Manager",
                base_dn,
            ),
        })
        if result.get("exit_code") == 0:
            monitor_rows.extend(flatten_monitor_entries(parse_ldif_entries(result.get("output")), instance_name, label))
        else:
            monitor_errors.append("{0}: {1}".format(label, result.get("output") or "ldapsearch failed"))

    policy_rows = []
    policy_errors = []
    list_body = "DSCONFIG=./dsconfig; [ -x \"$DSCONFIG\" ] || DSCONFIG=dsconfig; \"$DSCONFIG\" -h localhost -p {0} -D \"$BIND_DN\" -j \"$pwfile\" -X -n list-password-policies".format(
        shlex.quote(str(admin_port))
    )
    list_command = oud_secure_command(bin_directory, bind_dn, bind_password, list_body)
    list_result = run_target(target, list_command)
    commands.append({
        "label": "Password Policies",
        "command": 'cd {0} && dsconfig -h localhost -p {1} -D "{2}" -j <temporary password file> -X -n list-password-policies'.format(
            bin_directory,
            admin_port,
            bind_dn or "cn=Directory Manager",
        ),
    })
    policy_names = []
    if list_result.get("exit_code") == 0:
        for parts in parse_colon_table_rows(list_result.get("output")):
            policy = parts[0]
            policy_type = parts[1] if len(parts) > 1 else ""
            policy_rows.append({
                "instanceName": instance_name,
                "policy": policy,
                "type": policy_type,
                "property": "Policy Type",
                "value": policy_type,
            })
            policy_names.append(policy)
    else:
        policy_errors.append(list_result.get("output") or "dsconfig list-password-policies failed")

    for policy in policy_names[:8]:
        prop_body = "DSCONFIG=./dsconfig; [ -x \"$DSCONFIG\" ] || DSCONFIG=dsconfig; \"$DSCONFIG\" -h localhost -p {port} -D \"$BIND_DN\" -j \"$pwfile\" -X -n get-password-policy-prop --policy-name {policy}".format(
            port=shlex.quote(str(admin_port)),
            policy=shlex.quote(policy),
        )
        prop_result = run_target(target, oud_secure_command(bin_directory, bind_dn, bind_password, prop_body))
        commands.append({
            "label": "Password Policy - {0}".format(policy),
            "command": 'cd {0} && dsconfig -h localhost -p {1} -D "{2}" -j <temporary password file> -X -n get-password-policy-prop --policy-name "{3}"'.format(
                bin_directory,
                admin_port,
                bind_dn or "cn=Directory Manager",
                policy,
            ),
        })
        if prop_result.get("exit_code") != 0:
            policy_errors.append("{0}: {1}".format(policy, prop_result.get("output") or "get-password-policy-prop failed"))
            continue
        for prop in parse_password_policy_properties(prop_result.get("output"))[:30]:
            policy_rows.append({
                "instanceName": instance_name,
                "policy": policy,
                "type": "",
                "property": prop.get("property"),
                "value": prop.get("value"),
            })

    return {
        "monitorEntries": monitor_rows,
        "monitorError": "; ".join(monitor_errors) if monitor_errors and not monitor_rows else None,
        "passwordPolicies": policy_rows,
        "passwordPolicyError": "; ".join(policy_errors) if policy_errors and not policy_rows else None,
        "commands": commands,
    }


def build_monitoring_target(monitoring_config):
    return {
        "mode": "local",
        "host": monitoring_config.get("host") or "localhost",
        "port": 22,
        "username": "",
        "authType": "password",
        "password": "",
        "privateKeyPath": "",
        "passphrase": "",
    }


def build_environment_target(environment):
    server = _effective_environment_server(environment)
    bootstrap = environment.get("bootstrap") or {}
    username = server.get("username") or "root"
    sudo_required = bool(server.get("sudoRequired"))
    runtime_key_path = str(bootstrap.get("runtimeKeyPath") or "").strip()
    bootstrap_ready = str(bootstrap.get("status") or "").strip().lower() == "ready"
    if bootstrap_ready and runtime_key_path:
        return {
            "mode": server.get("mode") or "ssh",
            "host": server.get("host") or "",
            "port": server.get("port") or 22,
            "username": username,
            "sshMode": "root_key" if username == "root" and not sudo_required else ("user_key_sudo" if sudo_required else "user_key"),
            "authType": "private_key",
            "sudoRequired": sudo_required,
            "password": server.get("password") if sudo_required else "",
            "privateKeyPath": runtime_key_path,
            "passphrase": "",
        }
    return {
        "mode": server.get("mode") or "ssh",
        "host": server.get("host") or "",
        "port": server.get("port") or 22,
        "username": username,
        "sshMode": server.get("sshMode") or "root_password",
        "authType": server.get("authType") or "password",
        "sudoRequired": sudo_required,
        "password": server.get("password") or "",
        "privateKeyPath": server.get("privateKeyPath") or "",
        "passphrase": server.get("passphrase") or "",
    }


def setting_has_value(value):
    return bool(str(value or "").strip())


def hostname_from_url(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text if "://" in text else "http://{0}".format(text))
        return parsed.hostname or ""
    except Exception:
        return ""


def effective_weblogic_settings(environment):
    environment = environment or {}
    products = environment.get("products") or {}
    weblogic = dict(environment.get("weblogic") or {})
    product_order = []
    for product_name in ("oam", "oig", "soa", "oid", "oud"):
        if products.get(product_name):
            product_order.append(product_name)
    for product_name in ("oam", "oig", "soa", "oid", "oud"):
        if product_name not in product_order:
            product_order.append(product_name)

    for product_name in product_order:
        product_settings = environment.get(product_name) or {}
        for key in ("adminUrl", "adminUsername", "adminPassword", "oracleHome", "domainHome", "jstatPath"):
            if not setting_has_value(weblogic.get(key)) and setting_has_value(product_settings.get(key)):
                weblogic[key] = product_settings.get(key)

    if products.get("weblogic") or products.get("oam") or products.get("oig") or products.get("soa"):
        weblogic["enabled"] = True

    admin_host = dict(weblogic.get("adminHost") or {})
    if not setting_has_value(admin_host.get("host")):
        server = environment.get("server") or {}
        derived_host = hostname_from_url(weblogic.get("adminUrl")) or str(server.get("host") or "").strip()
        if derived_host:
            admin_host = {
                "mode": admin_host.get("mode") or server.get("mode") or "ssh",
                "host": derived_host,
                "port": admin_host.get("port") or server.get("port") or 22,
                "username": admin_host.get("username") or server.get("username") or "oracle",
                "sshMode": admin_host.get("sshMode") or server.get("sshMode") or "user_password",
                "authType": admin_host.get("authType") or server.get("authType") or "password",
                "sudoRequired": bool(admin_host.get("sudoRequired") or server.get("sudoRequired")),
                "password": admin_host.get("password") or server.get("password") or "",
                "privateKeyPath": admin_host.get("privateKeyPath") or server.get("privateKeyPath") or "",
                "passphrase": admin_host.get("passphrase") or server.get("passphrase") or "",
            }
            weblogic["adminHost"] = admin_host
    return weblogic


def build_weblogic_target(environment, fallback_target=None):
    weblogic = effective_weblogic_settings(environment)
    admin_host = weblogic.get("adminHost") or {}
    if not str(admin_host.get("host") or "").strip():
        return fallback_target or build_environment_target(environment)
    return {
        "mode": admin_host.get("mode") or "ssh",
        "host": admin_host.get("host") or "",
        "port": admin_host.get("port") or 22,
        "username": admin_host.get("username") or "root",
        "sshMode": admin_host.get("sshMode") or "root_password",
        "authType": admin_host.get("authType") or "password",
        "sudoRequired": bool(admin_host.get("sudoRequired")),
        "password": admin_host.get("password") or "",
        "privateKeyPath": admin_host.get("privateKeyPath") or "",
        "passphrase": admin_host.get("passphrase") or "",
    }


def parse_domain_registry_location(text):
    xml_text = str(text or "").strip()
    if not xml_text:
        return ""
    try:
        root = ET.fromstring(xml_text)
        for element in root.iter():
            for key in ("location", "path", "domainHome", "domain-home"):
                value = str(element.attrib.get(key) or "").strip()
                if value.startswith("/"):
                    return value
    except ET.ParseError:
        pass
    match = re.search(r'\b(?:location|path|domainHome|domain-home)=["\']([^"\']+)["\']', xml_text)
    if match and match.group(1).startswith("/"):
        return match.group(1)
    return ""


def discover_domain_home_from_oracle_home(target, oracle_home, progress=None):
    oracle_home = str(oracle_home or "").strip().rstrip("/")
    result = {
        "domainHome": "",
        "source": "",
        "registryFile": "",
        "error": "",
        "command": "",
    }
    if not oracle_home:
        result["error"] = "ORACLE_HOME is not configured."
        return result
    command = (
        "set +e\n"
        "OH={oracle_home}\n"
        "for candidate in \"$OH/domain-registry.xml\" \"$(dirname \"$OH\")/domain-registry.xml\" \"$OH/oracle_common/common/bin/domain-registry.xml\"; do\n"
        "  if [ -r \"$candidate\" ]; then\n"
        "    printf 'IAM_MONITORING_DOMAIN_REGISTRY=%s\\n' \"$candidate\"\n"
        "    cat \"$candidate\"\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "found=$(find \"$OH\" \"$(dirname \"$OH\")\" -maxdepth 4 -name domain-registry.xml -type f -readable -print -quit 2>/dev/null)\n"
        "if [ -n \"$found\" ]; then\n"
        "  printf 'IAM_MONITORING_DOMAIN_REGISTRY=%s\\n' \"$found\"\n"
        "  cat \"$found\"\n"
        "  exit 0\n"
        "fi\n"
        "echo \"domain-registry.xml was not found under ORACLE_HOME or its parent directory.\"\n"
        "exit 1\n"
    ).format(oracle_home=shlex.quote(oracle_home))
    result["command"] = "Read domain-registry.xml from ORACLE_HOME to derive DOMAIN_HOME."
    if callable(progress):
        progress("Deriving DOMAIN_HOME from domain-registry.xml under ORACLE_HOME.")
    command_result = run_target(target, command, timeout=30)
    output = str(command_result.get("output") or "")
    marker_match = re.search(r"^IAM_MONITORING_DOMAIN_REGISTRY=(.+)$", output, re.MULTILINE)
    if marker_match:
        result["registryFile"] = marker_match.group(1).strip()
        xml_text = output[marker_match.end():].strip()
        domain_home = parse_domain_registry_location(xml_text)
        if domain_home:
            result["domainHome"] = domain_home
            result["source"] = result["registryFile"] or "domain-registry.xml"
            return result
        result["error"] = "domain-registry.xml was found, but no domain location was parsed."
        return result
    result["error"] = output.strip() or "domain-registry.xml was not found."
    return result


def resolve_fmw_home_paths(target, oracle_home, domain_home="", progress=None):
    oracle_home = str(oracle_home or "").strip().rstrip("/")
    domain_home = str(domain_home or "").strip().rstrip("/")
    result = {
        "oracleHome": oracle_home,
        "domainHome": domain_home,
        "oracleHomeSource": "saved profile" if oracle_home else "",
        "domainHomeSource": "saved profile" if domain_home else "",
        "warning": "",
        "command": "",
    }
    if not oracle_home:
        result["warning"] = "ORACLE_HOME is not configured."
        return result
    command = (
        "set +e\n"
        "OH=__ORACLE_HOME__\n"
        "DH=__DOMAIN_HOME__\n"
        "is_domain_home() { [ -n \"$1\" ] && { [ -r \"$1/config/config.xml\" ] || [ -x \"$1/bin/setDomainEnv.sh\" ]; }; }\n"
        "resolved_oh=''\n"
        "resolved_dh=''\n"
        "domain_prefix=$(printf '%s\\n' \"$OH\" | sed 's#/domains/.*##')\n"
        "user_projects_prefix=$(printf '%s\\n' \"$OH\" | sed 's#/user_projects/domains/.*##')\n"
        "mwconfig_sibling=''\n"
        "case \"$(basename \"$domain_prefix\")\" in mwconfig*) mwconfig_sibling=\"$(dirname \"$domain_prefix\")/$(basename \"$domain_prefix\" | sed 's/^mwconfig/mw/')\" ;; esac\n"
        "for candidate in \"$OH\" \"$(dirname \"$OH\")\" \"$(dirname \"$(dirname \"$OH\")\")\" \"$domain_prefix\" \"$user_projects_prefix\" \"$mwconfig_sibling\"; do\n"
        "  [ -n \"$candidate\" ] || continue\n"
        "  [ \"$candidate\" = \".\" ] && continue\n"
        "  if [ -r \"$candidate/oracle_common/common/bin/wlst.sh\" ]; then resolved_oh=\"$candidate\"; break; fi\n"
        "done\n"
        "if [ -z \"$resolved_oh\" ]; then\n"
        "  found=$(find \"$OH\" \"$(dirname \"$OH\")\" \"$(dirname \"$(dirname \"$OH\")\")\" \"$domain_prefix\" \"$user_projects_prefix\" \"$mwconfig_sibling\" -maxdepth 5 -path '*/oracle_common/common/bin/wlst.sh' -type f -print -quit 2>/dev/null)\n"
        "  [ -n \"$found\" ] && resolved_oh=${found%/oracle_common/common/bin/wlst.sh}\n"
        "fi\n"
        "if is_domain_home \"$DH\"; then resolved_dh=\"$DH\"; elif is_domain_home \"$OH\"; then resolved_dh=\"$OH\"; fi\n"
        "[ -n \"$resolved_oh\" ] && printf 'IAM_MONITORING_RESOLVED_ORACLE_HOME=%s\\n' \"$resolved_oh\"\n"
        "[ -n \"$resolved_dh\" ] && printf 'IAM_MONITORING_RESOLVED_DOMAIN_HOME=%s\\n' \"$resolved_dh\"\n"
    ).replace("__ORACLE_HOME__", shlex.quote(oracle_home)).replace("__DOMAIN_HOME__", shlex.quote(domain_home))
    result["command"] = "Validate ORACLE_HOME and DOMAIN_HOME paths before WLST collection."
    if callable(progress):
        progress("Validating WebLogic ORACLE_HOME and DOMAIN_HOME paths before WLST collection.")
    command_result = run_target(target, command, timeout=30)
    output = str(command_result.get("output") or "")
    resolved_oracle_home = ""
    resolved_domain_home = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("IAM_MONITORING_RESOLVED_ORACLE_HOME="):
            resolved_oracle_home = line.split("=", 1)[1].strip()
        elif line.startswith("IAM_MONITORING_RESOLVED_DOMAIN_HOME="):
            resolved_domain_home = line.split("=", 1)[1].strip()
    if resolved_oracle_home and resolved_oracle_home != oracle_home:
        result["oracleHome"] = resolved_oracle_home
        result["oracleHomeSource"] = "auto-corrected from configured path"
        result["warning"] = (
            "Configured ORACLE_HOME looked like a domain or child path; using {0} for WLST and OPatch."
        ).format(resolved_oracle_home)
    if resolved_domain_home and not domain_home:
        result["domainHome"] = resolved_domain_home
        result["domainHomeSource"] = "auto-detected from configured path"
    return result


def build_weblogic_cluster_targets(environment, fallback_target=None):
    weblogic = effective_weblogic_settings(environment)
    primary_target = build_weblogic_target(environment, fallback_target)
    targets = [{
        "id": "node1",
        "name": "Node 1",
        "role": "AdminServer host",
        "host": primary_target.get("host") or "",
        "port": primary_target.get("port") or 22,
        "username": primary_target.get("username") or "",
        "sshMode": primary_target.get("sshMode") or "user_password",
        "oracleHome": weblogic.get("oracleHome") or "",
        "domainHome": weblogic.get("domainHome") or "",
        "target": primary_target,
    }]

    cluster = weblogic.get("cluster") or {}
    cluster_nodes = cluster.get("nodes") if isinstance(cluster.get("nodes"), list) else []
    if not cluster_nodes and cluster.get("node2"):
        cluster_nodes = [cluster.get("node2") or {}]
    if cluster.get("enabled"):
        for index, node in enumerate(cluster_nodes):
            node = node or {}
            node_host = str(node.get("host") or "").strip()
            if not node_host:
                continue
            node_id = "node{0}".format(index + 2)
            node_name = "Node {0}".format(index + 2)
            node_port = node.get("port") or primary_target.get("port") or 22
            node_username = node.get("username") or primary_target.get("username") or "oracle"
            node_ssh_mode = node.get("sshMode") or primary_target.get("sshMode") or "user_password"
            targets.append({
                "id": node_id,
                "name": node_name,
                "role": "Cluster node",
                "host": node_host,
                "port": node_port,
                "username": node_username,
                "sshMode": node_ssh_mode,
                "oracleHome": node.get("oracleHome") or weblogic.get("oracleHome") or "",
                "domainHome": node.get("domainHome") or weblogic.get("domainHome") or "",
                "target": {
                    "mode": node.get("mode") or "ssh",
                    "host": node_host,
                    "port": node_port,
                    "username": node_username,
                    "sshMode": node_ssh_mode,
                    "authType": node.get("authType") or primary_target.get("authType") or "password",
                    "sudoRequired": bool(node.get("sudoRequired")),
                    "password": node.get("password") or "",
                    "privateKeyPath": node.get("privateKeyPath") or "",
                    "passphrase": node.get("passphrase") or "",
                },
            })
    return targets


def normalize_weblogic_inventory_host(value, allow_local=False):
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in ("none", "null", "n/a", "-", "unknown"):
        return ""
    if not allow_local and lowered in ("0.0.0.0", "::", "localhost", "127.0.0.1", "::1"):
        return ""
    return text.rstrip(".")


def split_weblogic_listen_host(value):
    text = str(value or "").strip()
    if "/" not in text:
        return normalize_weblogic_inventory_host(text), ""
    left, right = text.split("/", 1)
    return normalize_weblogic_inventory_host(left), normalize_weblogic_inventory_host(right)


def weblogic_inventory_host_details(row):
    row = row or {}
    listen_host, inline_ip = split_weblogic_listen_host(row.get("listenAddress"))
    ip_value = normalize_weblogic_inventory_host(row.get("ip")) or inline_ip
    machine = normalize_weblogic_inventory_host(row.get("machine"))
    host = ip_value or listen_host or machine
    return {
        "host": host,
        "listenAddress": listen_host,
        "ip": ip_value,
        "machine": machine,
    }


def weblogic_host_key(*values):
    for value in values:
        host = normalize_weblogic_inventory_host(value)
        if host:
            return host.lower()
    return ""


def weblogic_row_is_admin(row):
    row = row or {}
    row_type = str(row.get("type") or "").strip().upper()
    name = str(row.get("name") or "").strip().lower()
    return row_type == "ADMIN" or name == "adminserver"


def clone_weblogic_target_for_host(primary_target, host, override_node=None):
    override_node = override_node or {}
    override_target = override_node.get("target") or {}
    target = dict(primary_target or {})
    target.update({
        "mode": override_target.get("mode") or override_node.get("mode") or target.get("mode") or "ssh",
        "host": override_target.get("host") or override_node.get("host") or host or target.get("host") or "",
        "port": override_target.get("port") or override_node.get("port") or target.get("port") or 22,
        "username": override_target.get("username") or override_node.get("username") or target.get("username") or "oracle",
        "sshMode": override_target.get("sshMode") or override_node.get("sshMode") or target.get("sshMode") or "user_password",
        "authType": override_target.get("authType") or override_node.get("authType") or target.get("authType") or "password",
        "sudoRequired": bool(
            override_target.get("sudoRequired")
            if "sudoRequired" in override_target
            else override_node.get("sudoRequired")
            if "sudoRequired" in override_node
            else target.get("sudoRequired")
        ),
        "password": override_target.get("password") or override_node.get("password") or target.get("password") or "",
        "privateKeyPath": override_target.get("privateKeyPath") or override_node.get("privateKeyPath") or target.get("privateKeyPath") or "",
        "passphrase": override_target.get("passphrase") or override_node.get("passphrase") or target.get("passphrase") or "",
    })
    return target


def explicit_weblogic_cluster_overrides(environment, fallback_target=None):
    overrides = {}
    for node in build_weblogic_cluster_targets(environment, fallback_target)[1:]:
        node_target = node.get("target") or {}
        for key in (
            weblogic_host_key(node.get("host")),
            weblogic_host_key(node_target.get("host")),
        ):
            if key:
                overrides[key] = node
    return overrides


def build_discovered_weblogic_cluster_targets(environment, fallback_target=None, weblogic_metrics=None):
    weblogic_metrics = weblogic_metrics or {}
    inventory = weblogic_metrics.get("serverInventory") or []
    explicit_targets = build_weblogic_cluster_targets(environment, fallback_target)
    if not inventory:
        return explicit_targets

    weblogic = effective_weblogic_settings(environment)
    primary_target = build_weblogic_target(environment, fallback_target)
    primary = dict(explicit_targets[0] if explicit_targets else {})
    primary.setdefault("id", "node1")
    primary.setdefault("name", "Node 1")
    primary.setdefault("role", "AdminServer host")
    primary["target"] = primary_target
    primary["source"] = "wlst"
    primary["sourceServers"] = []

    admin_key = ""
    for row in inventory:
        details = weblogic_inventory_host_details(row)
        key = weblogic_host_key(details.get("ip"), details.get("listenAddress"), details.get("machine"))
        if key and weblogic_row_is_admin(row):
            admin_key = key
            primary["discoveredHost"] = details.get("listenAddress") or details.get("host") or ""
            primary["discoveredIp"] = details.get("ip") or ""
            primary["machine"] = details.get("machine") or ""
            break

    primary_host_keys = {
        weblogic_host_key(primary_target.get("host")),
        weblogic_host_key(primary.get("host")),
        admin_key,
    }
    primary_host_keys = set(key for key in primary_host_keys if key)

    groups = []
    group_by_key = {}
    for row in inventory:
        details = weblogic_inventory_host_details(row)
        key = weblogic_host_key(details.get("ip"), details.get("listenAddress"), details.get("machine"))
        server_name = str(row.get("name") or "").strip()
        if not key:
            continue
        if weblogic_row_is_admin(row) or key in primary_host_keys:
            if server_name and server_name not in primary["sourceServers"]:
                primary["sourceServers"].append(server_name)
            continue
        if key not in group_by_key:
            group = {
                "key": key,
                "host": details.get("host") or "",
                "listenAddress": details.get("listenAddress") or "",
                "ip": details.get("ip") or "",
                "machine": details.get("machine") or "",
                "sourceServers": [],
            }
            group_by_key[key] = group
            groups.append(group)
        if server_name and server_name not in group_by_key[key]["sourceServers"]:
            group_by_key[key]["sourceServers"].append(server_name)

    if not groups:
        return explicit_targets

    overrides = explicit_weblogic_cluster_overrides(environment, fallback_target)
    targets = [primary]
    for index, group in enumerate(groups, start=2):
        override = None
        for key in (
            group.get("key"),
            weblogic_host_key(group.get("host")),
            weblogic_host_key(group.get("listenAddress")),
            weblogic_host_key(group.get("ip")),
            weblogic_host_key(group.get("machine")),
        ):
            if key and overrides.get(key):
                override = overrides.get(key)
                break
        ssh_host = (override or {}).get("host") or group.get("host") or group.get("listenAddress") or group.get("machine") or ""
        target = clone_weblogic_target_for_host(primary_target, ssh_host, override)
        targets.append({
            "id": (override or {}).get("id") or "node{0}".format(index),
            "name": (override or {}).get("name") or "Node {0}".format(index),
            "role": (override or {}).get("role") or "Discovered WebLogic host",
            "host": target.get("host") or ssh_host,
            "port": target.get("port") or 22,
            "username": target.get("username") or "",
            "sshMode": target.get("sshMode") or "user_password",
            "oracleHome": (override or {}).get("oracleHome") or weblogic.get("oracleHome") or "",
            "domainHome": (override or {}).get("domainHome") or weblogic.get("domainHome") or "",
            "target": target,
            "source": "wlst",
            "discovered": True,
            "listenAddress": group.get("listenAddress") or "",
            "ip": group.get("ip") or "",
            "machine": group.get("machine") or "",
            "sourceServers": group.get("sourceServers") or [],
            "usesInheritedCredentials": override is None,
        })
    return targets


def build_oaa_target(environment, fallback_target=None):
    oaa = environment.get("oaa") or {}
    kube_host = oaa.get("kubeHost") or {}
    if not str(kube_host.get("host") or "").strip():
        return fallback_target or build_environment_target(environment)
    return {
        "mode": kube_host.get("mode") or "ssh",
        "host": kube_host.get("host") or "",
        "port": kube_host.get("port") or 22,
        "username": kube_host.get("username") or "root",
        "sshMode": kube_host.get("sshMode") or "root_password",
        "authType": kube_host.get("authType") or "password",
        "sudoRequired": bool(kube_host.get("sudoRequired")),
        "password": kube_host.get("password") or "",
        "privateKeyPath": kube_host.get("privateKeyPath") or "",
        "passphrase": kube_host.get("passphrase") or "",
    }


def get_server_snapshot(target, script_directory=None, process_matchers=None):
    hostname = run_target(target, "hostname")
    if hostname["exit_code"] != 0:
        return {
            "reachable": False,
            "status": "down",
            "actualHostname": None,
            "error": hostname["output"] or "Connection failed.",
        }

    kernel = run_target(target, "uname -r")
    os_name = run_target(target, "grep '^PRETTY_NAME=' /etc/os-release | cut -d= -f2- | tr -d '\"'")
    cpu = run_target(target, "nproc")
    uptime = run_target(target, "uptime")
    uptime_seconds = run_target(target, "cut -d' ' -f1 /proc/uptime 2>/dev/null || echo 0")
    load_average = run_target(target, "cat /proc/loadavg 2>/dev/null || echo ''")
    meminfo = run_target(target, "cat /proc/meminfo 2>/dev/null || echo ''")
    memory = run_target(target, "free -m")
    root_disk = run_target(target, "df -P -h /")
    root_disk_bytes = run_target(target, "df -P -B1 /")
    refresh_disk = run_target(target, "if [ -d /refresh ]; then df -P -h /refresh; fi")
    refresh_disk_bytes = run_target(target, "if [ -d /refresh ]; then df -P -B1 /refresh; fi")
    cpu_stat_first = run_target(target, "cat /proc/stat 2>/dev/null || echo ''")
    time.sleep(0.35)
    cpu_stat_second = run_target(target, "cat /proc/stat 2>/dev/null || echo ''")

    cpu_count = int(cpu["output"]) if re.match(r"^\d+$", cpu["output"] or "") else 0
    memory_payload = parse_meminfo_proc(meminfo.get("output")) or parse_memory(memory["output"])
    root_disk_payload = parse_disk_bytes(root_disk_bytes.get("output")) or parse_disk(root_disk["output"])
    refresh_disk_payload = parse_disk_bytes(refresh_disk_bytes.get("output")) or parse_disk(refresh_disk["output"])
    uptime_payload = parse_uptime((uptime["output"] or "").strip(), cpu_count)
    try:
        uptime_payload["uptimeSeconds"] = int(float((uptime_seconds.get("output") or "0").strip()))
    except (TypeError, ValueError):
        uptime_payload["uptimeSeconds"] = 0

    load_parts = re.split(r"\s+", (load_average.get("output") or "").strip())
    if len(load_parts) >= 3:
        try:
            uptime_payload["load1"] = round(float(load_parts[0]), 2)
            uptime_payload["load5"] = round(float(load_parts[1]), 2)
            uptime_payload["load15"] = round(float(load_parts[2]), 2)
            uptime_payload["cpuPressure"] = percent(uptime_payload["load1"], cpu_count) if cpu_count else 0
        except (TypeError, ValueError):
            pass
    uptime_payload["cpuBreakdown"] = cpu_breakdown(
        read_cpu_stat_snapshot(cpu_stat_first.get("output")),
        read_cpu_stat_snapshot(cpu_stat_second.get("output")),
    )

    scripts = []
    if script_directory:
        script_result = run_target(
            target,
            "if [ -d {0} ]; then ls {0} | head -n 12; fi".format(shlex.quote(script_directory)),
        )
        scripts = lines(script_result["output"])

    processes = []
    if process_matchers:
        pattern = "|".join([item for item in process_matchers if item])
        process_result = run_target(
            target,
            "ps -eo user=,pid=,comm=,args= --sort=user | egrep -i {0} | grep -v egrep | head -n 12".format(
                shlex.quote(pattern)
            ),
        )
        processes = lines(process_result["output"])

    return {
        "reachable": True,
        "status": "healthy",
        "actualHostname": (hostname["output"] or "").strip(),
        "kernel": (kernel["output"] or "").strip(),
        "os": (os_name["output"] or "").strip(),
        "uptime": uptime_payload,
        "memory": memory_payload,
        "rootDisk": root_disk_payload,
        "refreshDisk": refresh_disk_payload,
        "scriptDirectory": script_directory,
        "scripts": scripts,
        "processes": processes,
    }


def get_app_check(target, check):
    result = run_target(
        target,
        "if command -v curl >/dev/null 2>&1; then curl -k -L -s -o /dev/null -w '%{{http_code}} %{{time_total}}' {0}; else echo NO_CURL; fi".format(
            shlex.quote(check.get("url"))
        ),
    )

    output = (result.get("output") or "").strip()
    match = re.match(r"^(\d{3})\s+([0-9.]+)$", output)
    status = "down"
    status_text = "No response"
    http_code = None
    response_time_ms = None

    if match:
        http_code = int(match.group(1))
        response_time_ms = int(round(float(match.group(2)) * 1000))
        if 200 <= http_code < 400:
            status = "healthy"
            status_text = "Reachable"
        elif http_code in (401, 403):
            status = "warning"
            status_text = "Responding with authentication gate"
        elif http_code == 0:
            status_text = "Connection failed or service is not listening"
        else:
            status_text = "HTTP {0}".format(http_code)
    elif output == "NO_CURL":
        status_text = "curl not available on target"
    elif output:
        status_text = output

    return {
        "product": check.get("product") or "generic",
        "name": check.get("name"),
        "url": check.get("url"),
        "status": status,
        "statusText": status_text,
        "httpCode": http_code,
        "responseTimeMs": response_time_ms,
    }


def collect_app_checks(target, environment):
    checks = []
    if (environment.get("products") or {}).get("oam"):
        checks.extend((environment.get("oam") or {}).get("checks") or [])
    if (environment.get("products") or {}).get("oud"):
        checks.extend((environment.get("oud") or {}).get("checks") or [])
    if (environment.get("products") or {}).get("oig"):
        checks.extend((environment.get("oig") or {}).get("checks") or [])
    return [get_app_check(target, check) for check in checks]


def get_oud_root_dse(target, settings, fallback_password):
    ldap_url = settings.get("ldapUrl")
    bind_dn = settings.get("bindDn")
    bind_password = settings.get("bindPassword") or fallback_password
    if not ldap_url or not bind_dn or not bind_password:
        return {}

    command = (
        "ldapsearch -x -H {0} -D {1} -w {2} -b \"\" -s base "
        "\"(objectClass=*)\" namingContexts vendorName vendorVersion"
    ).format(
        shlex.quote(ldap_url),
        shlex.quote(bind_dn),
        shlex.quote(bind_password),
    )
    result = run_target(target, command)
    root = {
        "namingContexts": [],
        "vendorName": None,
        "vendorVersion": None,
    }

    for raw_line in lines(result.get("output")):
        if raw_line.startswith("namingContexts:"):
            root["namingContexts"].append(raw_line.split(":", 1)[1].strip())
        elif raw_line.startswith("vendorName:"):
            root["vendorName"] = raw_line.split(":", 1)[1].strip()
        elif raw_line.startswith("vendorVersion:"):
            root["vendorVersion"] = raw_line.split(":", 1)[1].strip()

    return root


def python_string_literal(value):
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def normalize_weblogic_connect_url(admin_url):
    text = str(admin_url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else "t3://{0}".format(text))
    scheme = str(parsed.scheme or "t3").lower()
    host = parsed.hostname or parsed.path
    port = parsed.port
    if not host:
        return ""
    if scheme in ("http", "https"):
        scheme = "t3s" if scheme == "https" else "t3"
    elif scheme not in ("t3", "t3s"):
        scheme = "t3"
    return "{0}://{1}{2}".format(scheme, host, ":{0}".format(port) if port else "")


def parse_weblogic_deployments(text):
    deployments = []
    for raw_line in lines(text):
        match = re.match(r"^(.*?)\s*:\s*(STATE_[A-Z_]+)\s*$", raw_line)
        if not match:
            continue
        deployments.append({
            "name": match.group(1).strip(),
            "state": match.group(2).strip(),
        })
    return deployments


def parse_weblogic_server_inventory(text):
    rows = []
    header_seen = False
    for raw_line in lines(text):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("TYPE | SERVER | STATE | MACHINE | LISTEN_ADDRESS | IP | PORT | SSL_PORT | CLUSTER"):
            header_seen = True
            continue
        if header_seen and (
            stripped.startswith("SERVER | STUCK_THREADS | HOGGING_THREADS")
            or stripped.startswith("SERVER | DATASOURCE | ACTIVE | WAITING")
            or stripped.startswith("SECTION | SERVER | NAME | METRIC | VALUE")
            or stripped.startswith("===== Application Deployment Status")
        ):
            break
        if not header_seen or "|" not in stripped:
            continue
        parts = [part.strip() for part in stripped.split("|")]
        if len(parts) < 9:
            continue
        rows.append({
            "type": parts[0],
            "name": parts[1],
            "state": parts[2],
            "machine": parts[3],
            "listenAddress": parts[4],
            "ip": parts[5],
            "port": parts[6],
            "sslPort": parts[7],
            "cluster": parts[8],
        })
    return rows


def parse_weblogic_stuck_threads(text):
    rows = []
    header_seen = False

    def maybe_int(value):
        text_value = str(value or "").strip()
        if re.fullmatch(r"-?\d+", text_value):
            return int(text_value)
        return None

    for raw_line in lines(text):
        stripped = raw_line.strip()
        if not stripped:
            continue
        arrow_match = re.match(
            r"^(?P<server>.+?)\s*->\s*StuckThreads:\s*(?P<stuck>[^|]+)\|\s*HoggingThreads:\s*(?P<hogging>.+?)$",
            stripped,
        )
        if arrow_match:
            stuck_threads = maybe_int(arrow_match.group("stuck"))
            hogging_threads = maybe_int(arrow_match.group("hogging"))
            rows.append({
                "server": arrow_match.group("server").strip(),
                "stuckThreads": stuck_threads,
                "hoggingThreads": hogging_threads,
                "error": "" if stuck_threads is not None and hogging_threads is not None else "Unable to fetch thread info",
            })
            continue
        if stripped.startswith("SERVER | STUCK_THREADS | HOGGING_THREADS"):
            header_seen = True
            continue
        if header_seen and (
            stripped.startswith("SERVER | DATASOURCE | ACTIVE | WAITING")
            or stripped.startswith("SECTION | SERVER | NAME | METRIC | VALUE")
            or stripped.startswith("TYPE | SERVER | STATE | MACHINE | LISTEN_ADDRESS | IP | PORT | SSL_PORT | CLUSTER")
        ):
            break
        if not header_seen or "|" not in stripped:
            continue
        parts = [part.strip() for part in stripped.split("|")]
        if len(parts) < 3:
            continue
        stuck_threads = maybe_int(parts[1])
        hogging_threads = maybe_int(parts[2])
        rows.append({
            "server": parts[0],
            "stuckThreads": stuck_threads,
            "hoggingThreads": hogging_threads,
            "error": "" if stuck_threads is not None and hogging_threads is not None else "Unable to fetch thread info",
        })
    return rows


def parse_weblogic_jdbc_pools(text):
    rows = []
    header_seen = False

    def maybe_int(value):
        text_value = str(value or "").strip()
        if re.fullmatch(r"-?\d+", text_value):
            return int(text_value)
        return None

    for raw_line in lines(text):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("SERVER | DATASOURCE | ACTIVE | WAITING"):
            header_seen = True
            continue
        if header_seen and (
            stripped.startswith("SECTION | SERVER | NAME | METRIC | VALUE")
            or stripped.startswith("TYPE | SERVER | STATE | MACHINE | LISTEN_ADDRESS | IP | PORT | SSL_PORT | CLUSTER")
            or stripped.startswith("SERVER | STUCK_THREADS | HOGGING_THREADS")
        ):
            break
        if not header_seen or "|" not in stripped:
            continue
        parts = [part.strip() for part in stripped.split("|")]
        if len(parts) < 4:
            continue
        active_connections = maybe_int(parts[2])
        waiting_connections = maybe_int(parts[3])
        if active_connections is None or waiting_connections is None:
            continue
        rows.append({
            "server": parts[0],
            "dataSource": parts[1],
            "activeConnections": active_connections,
            "waitingConnections": waiting_connections,
        })
    return rows


def parse_weblogic_runtime_value(value):
    text_value = str(value or "").strip()
    if re.fullmatch(r"-?\d+", text_value):
        return int(text_value)
    if re.fullmatch(r"-?\d+\.\d+", text_value):
        return float(text_value)
    return text_value


def parse_weblogic_runtime_groups(text):
    section_map = {
        "jta": "jtaTransactions",
        "jms": "jmsDestinations",
        "jvm": "jvmRuntime",
        "threadPool": "threadPoolRuntime",
        "jdbcHealth": "jdbcHealth",
        "socket": "socketRuntime",
        "workManager": "workManagers",
    }
    grouped = {value: {} for value in section_map.values()}
    header_seen = False
    for raw_line in lines(text):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("SECTION | SERVER | NAME | METRIC | VALUE"):
            header_seen = True
            continue
        if not header_seen or "|" not in stripped:
            continue
        parts = [part.strip() for part in stripped.split("|", 4)]
        if len(parts) < 5:
            continue
        section, server, name, metric, value = parts
        target_key = section_map.get(section)
        if not target_key or not metric:
            continue
        row_key = "{0}\n{1}".format(server, name)
        row = grouped[target_key].setdefault(row_key, {"server": server, "name": name})
        row[metric] = parse_weblogic_runtime_value(value)

    result = {}
    for key, rows in grouped.items():
        result[key] = list(rows.values())
    for row in result.get("jmsDestinations", []):
        row["destination"] = row.pop("name", "")
    for row in result.get("jdbcHealth", []):
        row["dataSource"] = row.pop("name", "")
    for row in result.get("workManagers", []):
        row["workManager"] = row.pop("name", "")
    return result


def parse_opatch_lsinventory(text):
    versions = []
    products = []
    patches = []
    distributions = []
    seen_versions = set()
    seen_distributions = set()
    current_patch = None
    capture_products = False

    def add_version(key, value):
        clean_key = str(key or "").strip()
        clean_value = str(value or "").strip()
        if clean_key and clean_value and clean_key not in seen_versions:
            versions.append({"key": clean_key, "value": clean_value})
            seen_versions.add(clean_key)

    def add_distribution(value):
        clean_value = str(value or "").strip()
        if clean_value and clean_value not in seen_distributions:
            distributions.append(clean_value)
            seen_distributions.add(clean_value)

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        distribution_match = re.match(r"^(?:OUI_)?\s*Distribution:\s*(.+)$", line, re.I)
        if distribution_match:
            add_distribution(distribution_match.group(1))
            continue

        installer_match = re.match(r"^Oracle Interim Patch Installer version\s+(.+)$", line, re.I)
        if installer_match:
            add_version("OPatch Installer Version", installer_match.group(1))
            continue

        for label in ("Oracle Home", "Central Inventory", "OPatch version", "OUI version"):
            if line.lower().startswith(label.lower()) and ":" in line:
                add_version(label, line.split(":", 1)[1].strip())

        if line.startswith("Installed Top-level Products"):
            capture_products = True
            continue
        if capture_products:
            if line.startswith("There are ") or line.startswith("Interim patches") or line.startswith("---"):
                capture_products = False
            else:
                product_match = re.match(r"^(.+?)\s+(\d+(?:\.\d+){1,})$", line)
                if product_match:
                    products.append({
                        "name": product_match.group(1).strip(),
                        "version": product_match.group(2).strip(),
                    })
                    continue

        patch_match = re.match(r"^Patch\s+([0-9]+)\s*:\s*(.*)$", line, re.I)
        if patch_match:
            current_patch = {
                "patchId": patch_match.group(1).strip(),
                "description": "",
                "appliedOn": "",
            }
            applied_match = re.search(r"applied on\s+(.+)$", patch_match.group(2), re.I)
            if applied_match:
                current_patch["appliedOn"] = applied_match.group(1).strip()
            patches.append(current_patch)
            continue

        if current_patch:
            description_match = re.match(r"^Patch description:\s*\"?(.*?)\"?\s*$", line, re.I)
            if description_match:
                current_patch["description"] = description_match.group(1).strip()

    return {
        "versions": versions,
        "products": products,
        "patches": patches,
        "distributions": distributions,
    }


def parse_opatch_lspatches(text):
    patches = []
    seen = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("OPATCH_PATH="):
            continue
        if line.lower().startswith("opatch succeeded"):
            continue
        if line.lower().startswith("opatch failed"):
            continue
        semicolon_match = re.match(r"^([0-9]+)\s*;\s*(.+)$", line)
        patch_match = re.match(r"^Patch\s+([0-9]+)\s*:?\s*(.*)$", line, re.I)
        patch_id = ""
        description = ""
        if semicolon_match:
            patch_id = semicolon_match.group(1).strip()
            description = semicolon_match.group(2).strip()
        elif patch_match:
            patch_id = patch_match.group(1).strip()
            description = patch_match.group(2).strip()
        if not patch_id or patch_id in seen:
            continue
        seen.add(patch_id)
        patches.append({
            "patchId": patch_id,
            "description": description or "OPatch lspatches row",
            "appliedOn": "",
            "source": "opatch_lspatches",
        })
    return {
        "versions": [],
        "products": [],
        "patches": patches,
    }


def merge_opatch_patch_rows(primary_rows, fallback_rows):
    rows = []
    seen = set()
    for row in list(primary_rows or []) + list(fallback_rows or []):
        patch_id = str((row or {}).get("patchId") or "").strip()
        if not patch_id or patch_id in seen:
            continue
        seen.add(patch_id)
        rows.append(row)
    return rows


def _patch_row(description, patch_id, applicability):
    return {
        "description": description,
        "patchId": str(patch_id),
        "applicability": list(applicability),
    }


ALL_IDM_COMPONENTS = ["OAM", "OIG", "OUD", "OID"]
DEFAULT_FMW_PATCH_BASELINES = {
    "12c": {
        "family": "12c",
        "version": "12.2.1.4.0",
        "latestRelease": "June 2026 12.2.1.4.260609 SPB",
        "latestPatch": _patch_row("IDM STACK PATCH BUNDLE 12.2.1.4.260609", "39525460", ALL_IDM_COMPONENTS),
        "patches": [
            _patch_row("MERGE REQUEST ON TOP OF 12.2.1.4.0 FOR BUGS 34065178 34113169", "36649916", ALL_IDM_COMPONENTS),
            _patch_row("OIM 12CPS3 UPGRADE STUCK AT SCHEMAREADINESSQUERIES QUERYINDEXNAMES", "32999272", ALL_IDM_COMPONENTS),
            _patch_row("FMWCONTROL BUNDLE PATCH 12.2.1.4.260120", "38633461", ALL_IDM_COMPONENTS),
            _patch_row("OID BUNDLE PATCH 12.2.1.4.221222", "34947852", ["OID"]),
            _patch_row("DATABASE RELEASE UPDATE 19.30.0.0.0 FOR FMW DBCLIENT", "38879115", ["OID"]),
            _patch_row("PERL SECURITY PATCH UPDATE 12.2.1.4.260120", "38590502", ["OID"]),
            _patch_row("ONE OFF PATCH TO RELINK OID WITH DBCLIENT - APR'26 BP", "39107027", ["OID"]),
            _patch_row("OINAV PATCH 12.2.1.4.250715", "38009014", ["OAM", "OIG"]),
            _patch_row("MERGE REQUEST ON TOP OF 12.2.1.4.0 FOR BUGS 20623024 29790738", "31676526", ["OAM", "OIG"]),
            _patch_row("OAS 12.2.1.4.5 - RCU CREATION WITH RAC DB SHOWS INCORRECT PORT WARNING", "30540494", ["OAM", "OIG"]),
            _patch_row("LIBDMS2 SECURITY PATCH UPDATE 12.2.1.4.260120", "38098858", ["OID"]),
            _patch_row("LIBIAU SECURITY PATCH UPDATE 12.2.1.4.260120", "38832179", ["OID"]),
            _patch_row("RDA release 26.2.2026421 for FMW 12.2.1.4.0", "38887908", ALL_IDM_COMPONENTS),
            _patch_row("ADR FOR WEBLOGIC SERVER 12.2.1.4.0 - SIZE OPTIMIZED FOR JAN 2024", "35965629", ALL_IDM_COMPONENTS),
            _patch_row("Coherence 12.2.1.4 Cumulative Patch 29 (12.2.1.4.29)", "39051716", ALL_IDM_COMPONENTS),
            _patch_row("WEBLOGIC SAMPLES SPU 12.2.1.4.240416", "36426672", ALL_IDM_COMPONENTS),
            _patch_row("FMW Thirdparty Bundle Patch 12.2.1.4.260317", "39096983", ALL_IDM_COMPONENTS),
            _patch_row("JDBC19.30 BUNDLE PATCH 12.2.1.4.260216", "38970574", ALL_IDM_COMPONENTS),
            _patch_row("OSS 19C BUNDLE PATCH 12.2.1.4.260228", "39024181", ALL_IDM_COMPONENTS),
            _patch_row("OCT 2024 CLONING SPU FOR FMW 12.2.1.4.0", "37056593", ALL_IDM_COMPONENTS),
            _patch_row("WLS PATCH SET UPDATE 12.2.1.4.260403", "39163907", ALL_IDM_COMPONENTS),
            _patch_row("FMW PLATFORM BUNDLE PATCH 12.2.1.4.240812", "36789759", ALL_IDM_COMPONENTS),
            _patch_row("OPSS BUNDLE PATCH 12.2.1.4.240220", "36316422", ALL_IDM_COMPONENTS),
            _patch_row("OWSM BUNDLE PATCH 12.2.1.4.260304", "39039951", ALL_IDM_COMPONENTS),
            _patch_row("OIM BUNDLE PATCH 12.2.1.4.260318", "39099526", ["OAM", "OIG"]),
            _patch_row("OUD BUNDLE PATCH 12.2.1.4.251205", "38729679", ["OUD"]),
            _patch_row("ADF BUNDLE PATCH 12.2.1.4.250822", "38348152", ALL_IDM_COMPONENTS),
            _patch_row("SOA Bundle Patch 12.2.1.4.260416", "39220089", ["OIG"]),
            _patch_row("WEBCENTER CORE BUNDLE PATCH 12.2.1.4.250904", "38400138", ALL_IDM_COMPONENTS),
            _patch_row("OAM BUNDLE PATCH 12.2.1.4.260228", "39023724", ["OAM", "OIG"]),
        ],
    },
    "14c": {
        "family": "14c",
        "version": "14.1.2.1",
        "latestRelease": "June 2026 14.1.2.1.260609 SPB",
        "latestPatch": _patch_row("IDM STACK PATCH BUNDLE 14.1.2.1.260609", "39526335", ALL_IDM_COMPONENTS),
        "patches": [
            _patch_row("MERGE REQUEST ON TOP OF 14.1.2.0.0 FOR BUGS 37571450 37359866", "37632501", ALL_IDM_COMPONENTS),
            _patch_row("Unable to Retrieve Request Details by REST Client With End User", "37512243", ["OIG"]),
            _patch_row("OINAV PATCH 14.1.2.0.260421", "39019346", ["OAM", "OIG"]),
            _patch_row("PATCHING OF LIB_IDM_OAM_THIRDPARTY.JAR FOR UPDATING THIRD PARTY LIBRARY", "38181053", ["OAM", "OIG"]),
            _patch_row("FMWCONTROL BUNDLE PATCH 14.1.2.0.260120", "38637356", ALL_IDM_COMPONENTS),
            _patch_row("PERL SECURITY PATCH UPDATE 14.1.2.0.260120", "38590500", ["OAM", "OIG", "OID"]),
            _patch_row("STACK:FMW:RCU prereq steps fails while trying to validate the DB connection", "38391376", ALL_IDM_COMPONENTS),
            _patch_row("Coherence 14.1.2.0 Cumulative Patch 6 (14.1.2.0.6)", "39051778", ALL_IDM_COMPONENTS),
            _patch_row("FMW Thirdparty Bundle Patch 14.1.2.0.260318", "39096850", ALL_IDM_COMPONENTS),
            _patch_row("RDA release 26.2.2026421 for FMW 14.1.2.0.0", "38887889", ALL_IDM_COMPONENTS),
            _patch_row("DATABASE RELEASE UPDATE 23.26.1.0.0 FOR FMW DBCLIENT", "38879142", ["OAM", "OIG", "OID"]),
            _patch_row("JDBC23.26.1 BUNDLE PATCH 14.1.2.0.260217", "38974223", ALL_IDM_COMPONENTS),
            _patch_row("WEBLOGIC SAMPLES PATCH 14.1.2.0.250715", "38063775", ALL_IDM_COMPONENTS),
            _patch_row("WLS PATCH SET UPDATE 14.1.2.0.260403", "39164253", ALL_IDM_COMPONENTS),
            _patch_row("OWSM BUNDLE PATCH 14.1.2.0.260304", "39043853", ALL_IDM_COMPONENTS),
            _patch_row("OAM BUNDLE PATCH 14.1.2.1.260228", "39023565", ["OAM", "OIG"]),
            _patch_row("OIM BUNDLE PATCH 14.1.2.1.260316", "39084797", ["OAM", "OIG"]),
            _patch_row("SOA Bundle Patch 14.1.2.0.260406", "39175119", ["OIG"]),
            _patch_row("OUD BUNDLE PATCH 14.1.2.1.251202", "38716493", ["OUD"]),
            _patch_row("ADF BUNDLE PATCH 14.1.2.0.260217", "38976275", ALL_IDM_COMPONENTS),
        ],
    },
}


def _copy_patch_baselines(value):
    return json.loads(json.dumps(value))


def _normalize_patch_catalog_components(value):
    if isinstance(value, str):
        return [item.strip().upper() for item in re.split(r"[,/|\s]+", value) if item.strip()]
    if isinstance(value, list):
        return [str(item or "").strip().upper() for item in value if str(item or "").strip()]
    return []


def _normalize_patch_catalog_row(value):
    row = value or {}
    return _patch_row(
        row.get("description") or row.get("name") or row.get("label") or "",
        row.get("patchId") or row.get("patch") or row.get("patchNumber") or "",
        _normalize_patch_catalog_components(row.get("applicability") or row.get("components") or row.get("appliesTo") or []),
    )


def _patch_catalog_paths():
    app_root = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.environ.get("IAM_MONITORING_PATCH_CATALOG") or "",
        os.path.join(app_root, "patch_catalog.json"),
        os.path.join(os.path.dirname(app_root), "config", "patch_catalog.json"),
    ]
    return [path for path in paths if path]


def load_fmw_patch_baselines():
    baselines = _copy_patch_baselines(DEFAULT_FMW_PATCH_BASELINES)
    for path in _patch_catalog_paths():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue
        families = payload.get("families") if isinstance(payload, dict) else None
        if not isinstance(families, dict):
            families = payload if isinstance(payload, dict) else {}
        for family, raw_baseline in families.items():
            if family not in baselines or not isinstance(raw_baseline, dict):
                continue
            baseline = baselines[family]
            for key in ("family", "version", "latestRelease"):
                if raw_baseline.get(key):
                    baseline[key] = str(raw_baseline.get(key))
            if raw_baseline.get("latestPatch"):
                baseline["latestPatch"] = _normalize_patch_catalog_row(raw_baseline.get("latestPatch"))
            if isinstance(raw_baseline.get("patches"), list):
                baseline["patches"] = [_normalize_patch_catalog_row(item) for item in raw_baseline.get("patches")]
            baseline["catalogSource"] = path
    return baselines


FMW_PATCH_BASELINES = load_fmw_patch_baselines()


def detect_fmw_patch_family(opatch, oracle_home):
    patches = opatch.get("patches") or []
    installed_ids = {str(item.get("patchId") or "").strip() for item in patches if str(item.get("patchId") or "").strip()}
    scores = {}
    for family, baseline in FMW_PATCH_BASELINES.items():
        known_ids = {baseline["latestPatch"]["patchId"]} | {item["patchId"] for item in baseline.get("patches") or []}
        scores[family] = len(installed_ids & known_ids)
    if scores.get("14c", 0) > scores.get("12c", 0):
        return "14c"
    if scores.get("12c", 0) > scores.get("14c", 0):
        return "12c"
    haystack = " ".join([
        str(oracle_home or ""),
        " ".join(str(item.get("description") or "") for item in patches),
        " ".join(str(item.get("value") or "") for item in opatch.get("versions") or []),
        " ".join(str(item.get("name") or "") + " " + str(item.get("version") or "") for item in opatch.get("products") or []),
        " ".join(str(item or "") for item in opatch.get("distributions") or []),
    ]).lower()
    if "14.1.2" in haystack or "14c" in haystack:
        return "14c"
    if "12.2.1.4" in haystack or "12c" in haystack:
        return "12c"
    return ""


def environment_idm_components(environment):
    product_map = {"oam": "OAM", "oig": "OIG", "oud": "OUD", "oid": "OID"}
    products = environment.get("products") or {}
    components = [component for key, component in product_map.items() if products.get(key)]
    environment_type = str(environment.get("environmentType") or "").strip().lower()
    if not components and environment_type in product_map:
        components.append(product_map[environment_type])
    return components


def patch_item_numbers(item):
    text = "{0} {1}".format(item.get("patchId") or "", item.get("description") or "")
    return {match for match in re.findall(r"\b\d{6,9}\b", text)}


def patch_category(description):
    text = str(description or "").upper()
    if "STACK PATCH BUNDLE" in text or " SPB" in text:
        return "SPB"
    if "COHERENCE" in text:
        return "COHERENCE"
    if "WEBLOGIC SAMPLES" in text:
        return "WEBLOGIC SAMPLES"
    if "ADR FOR WEBLOGIC" in text:
        return "ADR"
    if "WLS PATCH SET UPDATE" in text or "WEBLOGIC SERVER" in text:
        return "WLS"
    if "FMWCONTROL" in text:
        return "FMWCONTROL"
    if "OIM BUNDLE" in text:
        return "OIM"
    if "OAM BUNDLE" in text:
        return "OAM"
    if "SOA BUNDLE" in text:
        return "SOA"
    if "OUD BUNDLE" in text:
        return "OUD"
    if "OID BUNDLE" in text:
        return "OID"
    if "OPSS" in text:
        return "OPSS"
    if "OWSM" in text:
        return "OWSM"
    if "ADF" in text:
        return "ADF"
    if "WEBCENTER" in text:
        return "WEBCENTER"
    if "THIRDPARTY" in text:
        return "FMW THIRDPARTY"
    if "JDBC" in text:
        return "JDBC"
    if "OSS" in text:
        return "OSS"
    if "OINAV" in text:
        return "OINAV"
    if "RDA" in text:
        return "RDA"
    if "FMW PLATFORM" in text:
        return "FMW PLATFORM"
    if "CLONING" in text:
        return "CLONING"
    if "PERL" in text:
        return "PERL"
    if "LIBDMS2" in text:
        return "LIBDMS2"
    if "LIBIAU" in text:
        return "LIBIAU"
    if "DBCLIENT" in text or "DATABASE RELEASE UPDATE" in text:
        return "DBCLIENT"
    if "RELINK OID" in text:
        return "OID RELINK"
    if "MERGE REQUEST" in text:
        bugs = " ".join(re.findall(r"\b\d{7,8}\b", text))
        return "MERGE {0}".format(bugs).strip()
    return ""


def patch_version_tokens(description):
    text = str(description or "").upper()
    tokens = set(re.findall(r"\b\d{2}\.\d(?:\.\d+){2,4}\b", text))
    tokens.update(re.findall(r"\b(?:24|25|26)\d{4}\b", text))
    cp_match = re.search(r"CUMULATIVE PATCH\s+(\d+)", text)
    if cp_match:
        tokens.add("CP{0}".format(cp_match.group(1)))
    paren_match = re.search(r"\((\d{2}\.\d(?:\.\d+){2,4})\)", text)
    if paren_match:
        tokens.add(paren_match.group(1))
    return tokens


def baseline_items_for_components(baseline, components):
    relevant = [baseline["latestPatch"]]
    for item in baseline.get("patches") or []:
        if set(item.get("applicability") or []) & set(components):
            relevant.append(item)
    return relevant


def patch_matches_baseline(patch, baseline_item):
    if baseline_item["patchId"] in patch_item_numbers(patch):
        return True
    current_key = patch_category(patch.get("description"))
    baseline_key = patch_category(baseline_item.get("description"))
    if not current_key or current_key != baseline_key:
        return False
    current_tokens = patch_version_tokens(patch.get("description"))
    baseline_tokens = patch_version_tokens(baseline_item.get("description"))
    return bool(current_tokens and baseline_tokens and current_tokens & baseline_tokens)


def recommendation_text(item):
    if patch_category(item.get("description")) == "COHERENCE":
        return str(item.get("description") or "-")
    return "{0} - {1}".format(item.get("patchId") or "-", item.get("description") or "-")


def patch_presentation_classification(value):
    text = str(value or "").upper()
    if re.search(r"\bSECURITY\b|\bCVE[- ]?\d+", text):
        return {"patchGroup": "security", "patchGroupLabel": "Security Update"}
    if re.search(r"\b(?:WLS|WEBLOGIC)\b.*\b(?:PSU|PATCH SET UPDATE)\b|\bPSU\b", text):
        return {"patchGroup": "security", "patchGroupLabel": "WebLogic PSU"}
    if re.search(r"\bSPU\b|SECURITY PATCH UPDATE", text):
        return {"patchGroup": "security", "patchGroupLabel": "Security Patch Update"}
    if re.search(r"\bCPU\b|CRITICAL PATCH UPDATE", text):
        return {"patchGroup": "security", "patchGroupLabel": "Critical Patch Update"}
    if re.search(r"\b(?:JDK|JRE|JAVA)\b.*\b(?:SECURITY|PATCH|UPDATE)\b", text):
        return {"patchGroup": "security", "patchGroupLabel": "Java Security Update"}
    if re.search(r"FMW\s+THIRDPARTY|THIRD[- ]PARTY\s+(?:LIBRARY|BUNDLE)|\bOPSS\s+BUNDLE\b", text):
        return {"patchGroup": "security", "patchGroupLabel": "Security Platform Update"}
    if re.search(r"\bCOHERENCE\b.*\bCUMULATIVE PATCH\b|\bFMW PLATFORM\s+BUNDLE\b", text):
        return {"patchGroup": "security", "patchGroupLabel": "Platform Update"}
    return {"patchGroup": "product", "patchGroupLabel": "Product / Component"}


def build_fmw_patch_recommendation(opatch, environment, oracle_home):
    opatch = opatch or {}
    family = detect_fmw_patch_family(opatch, oracle_home)
    if not family:
        return {
            "status": "unknown",
            "message": "Unable to determine whether this ORACLE_HOME is 12c or 14c from OPatch lsinventory output.",
            "missingPatches": [],
            "comparisonRows": [],
        }
    baseline = FMW_PATCH_BASELINES[family]
    components = environment_idm_components(environment) or ALL_IDM_COMPONENTS
    patches = []
    for item in opatch.get("patches") or []:
        patch = dict(item)
        patch.update(patch_presentation_classification(patch.get("description")))
        patches.append(patch)
    installed_ids = set()
    for item in patches:
        installed_ids.update(patch_item_numbers(item))
    latest_patch = baseline["latestPatch"]
    if not installed_ids:
        latest_missing = dict(latest_patch)
        latest_missing["components"] = [component for component in latest_patch.get("applicability") or [] if component in components]
        return {
            "family": baseline["family"],
            "version": baseline["version"],
            "latestRelease": baseline["latestRelease"],
            "latestPatchId": latest_patch["patchId"],
            "catalogSource": baseline.get("catalogSource", "bundled"),
            "components": components,
            "status": "updates_recommended",
            "baseInstall": True,
            "message": "OPatch lsinventory did not report any installed patches. This ORACLE_HOME appears to be a base {0} installation. Please review Oracle KM document KA754 and apply the latest available {0} Stack Patch Bundle, currently {1} Patch {2}.".format(
                baseline["version"],
                baseline["latestRelease"],
                latest_patch["patchId"],
            ),
            "missingPatches": [latest_missing],
            "comparisonRows": [{
                "patchId": "",
                "description": "No bundle patches or Stack Patch Bundle detected",
                "appliedOn": "",
                "recommendation": recommendation_text(latest_missing),
                "recommendationStatus": "missing",
            }],
        }
    required = baseline_items_for_components(baseline, components)
    matched_required = set()
    comparison_rows = []
    by_category = {}
    for item in required:
        by_category.setdefault(patch_category(item.get("description")), item)

    for patch in patches:
        matched_item = next((item for item in required if patch_matches_baseline(patch, item)), None)
        if matched_item:
            matched_required.add(matched_item["patchId"])
            recommendation = "Latest"
            recommendation_status = "latest"
        else:
            same_category = by_category.get(patch_category(patch.get("description")))
            recommendation = recommendation_text(same_category) if same_category else "-"
            recommendation_status = "recommended" if same_category else "none"
        row = dict(patch)
        row["recommendation"] = recommendation
        row["recommendationStatus"] = recommendation_status
        row.update(patch_presentation_classification("{0} {1}".format(row.get("description") or "", recommendation)))
        comparison_rows.append(row)

    missing = []
    installed_categories = {
        patch_category(patch.get("description"))
        for patch in patches
        if patch_category(patch.get("description"))
    }
    for item in required:
        if item["patchId"] in matched_required or item["patchId"] in installed_ids:
            continue
        row = dict(item)
        row["components"] = [component for component in item.get("applicability") or [] if component in components]
        missing.append(row)
        missing_category = patch_category(row.get("description"))
        if missing_category and missing_category in installed_categories:
            continue
        comparison_row = {
            "patchId": row.get("patchId") or "",
            "description": row.get("description") or "Recommended patch",
            "appliedOn": "Not installed",
            "recommendation": recommendation_text(row),
            "recommendationStatus": "missing",
            "isMissingRecommendation": True,
        }
        comparison_row.update(patch_presentation_classification(comparison_row["recommendation"]))
        comparison_rows.append(comparison_row)

    status = "updates_recommended" if missing else "latest"
    message = (
        "Recommended latest patch level is {0} Patch {1}. Missing relevant patch count: {2}.".format(
            baseline["latestRelease"],
            latest_patch["patchId"],
            len(missing),
        )
        if missing
        else "This environment is on the latest {0} patch level: {1} Patch {2}.".format(
            baseline["family"],
            baseline["latestRelease"],
            latest_patch["patchId"],
        )
    )
    return {
        "family": baseline["family"],
        "version": baseline["version"],
        "latestRelease": baseline["latestRelease"],
        "latestPatchId": latest_patch["patchId"],
        "catalogSource": baseline.get("catalogSource", "bundled"),
        "components": components,
        "status": status,
        "message": message,
        "missingPatches": missing,
        "comparisonRows": comparison_rows,
    }


def collect_opatch_inventory(target, oracle_home, progress=None, label="OIG/OIM"):
    home = str(oracle_home or "").strip()
    if not home:
        return {
            "error": "ORACLE_HOME Path is not configured.",
            "versions": [],
            "products": [],
            "patches": [],
        }

    command = (
        "ORACLE_HOME={home}; export ORACLE_HOME; "
        "view_inventory() {{ "
        "for inv in \"$ORACLE_HOME/oui/bin/viewInventory.sh\" \"$(dirname \"$ORACLE_HOME\")/oui/bin/viewInventory.sh\"; do "
        "if [ -x \"$inv\" ]; then printf 'VIEW_INVENTORY_PATH=%s\\n' \"$inv\"; \"$inv\" 2>/dev/null | grep 'Distribution' | sed 's/^/OUI_/'; return 0; fi; "
        "done; return 1; "
        "}}; "
        "run_lspatches() {{ "
        "printf 'OPATCH_LSPATCHES_BEGIN\\n'; \"$1\" lspatches 2>&1; lsp_rc=$?; printf 'OPATCH_LSPATCHES_END=%s\\n' \"$lsp_rc\"; return 0; "
        "}}; "
        "for opatch in \"$(dirname \"$ORACLE_HOME\")/OPatch/opatch\" \"$ORACLE_HOME/OPatch/opatch\"; do "
        "if [ -x \"$opatch\" ]; then printf 'OPATCH_PATH=%s\\n' \"$opatch\"; "
        "start=$(date +%s); \"$opatch\" lsinventory; rc=$?; end=$(date +%s); "
        "printf 'OPATCH_DURATION_SECONDS=%s\\n' \"$((end - start))\"; "
        "run_lspatches \"$opatch\"; "
        "view_inventory || true; "
        "exit \"$rc\"; fi; "
        "done; "
        "view_inventory; "
        "echo \"OPatch executable not found under $(dirname \"$ORACLE_HOME\")/OPatch or $ORACLE_HOME/OPatch.\" >&2; exit 1"
    ).format(home=shlex.quote(home))
    if callable(progress):
        progress("Starting {0} OPatch lsinventory as the configured SSH user without sudo.".format(label))
        progress("If OPatch takes longer than expected on this machine, it may be scanning inactive patches.")
    opatch_target = dict(target or {})
    opatch_target["sudoRequired"] = False
    result = run_target(opatch_target, command, timeout=None)
    output = str(result.get("output") or "")
    parsed = parse_opatch_lsinventory(output)
    lspatches_match = re.search(r"OPATCH_LSPATCHES_BEGIN\n([\s\S]*?)\nOPATCH_LSPATCHES_END=([0-9]+)", output)
    lspatches_output = lspatches_match.group(1) if lspatches_match else ""
    lspatches_exit_code = int(lspatches_match.group(2)) if lspatches_match else None
    lspatches_parsed = parse_opatch_lspatches(lspatches_output)
    lsinventory_patch_count = len(parsed.get("patches") or [])
    parsed["patches"] = merge_opatch_patch_rows(parsed.get("patches"), lspatches_parsed.get("patches"))
    duration_match = re.search(r"^OPATCH_DURATION_SECONDS=([0-9]+)$", output, re.M)
    duration_seconds = int(duration_match.group(1)) if duration_match else None
    path_match = re.search(r"^OPATCH_PATH=(.+)$", output, re.M)
    opatch_path = path_match.group(1).strip() if path_match else ""
    parsed.update({
        "oracleHome": home,
        "opatchPath": opatch_path,
        "durationSeconds": duration_seconds,
        "command": "{0} lsinventory".format(opatch_path or "$(dirname ORACLE_HOME)/OPatch/opatch"),
        "lspatchesCommand": "{0} lspatches".format(opatch_path or "$(dirname ORACLE_HOME)/OPatch/opatch"),
        "lspatchesExitCode": lspatches_exit_code,
        "distributionCommand": "{0}/oui/bin/viewInventory.sh | grep Distribution".format(home),
        "error": "",
    })
    if not lsinventory_patch_count and parsed.get("patches"):
        parsed["parserNote"] = "OPatch lsinventory did not expose detailed patch rows, so installed patch IDs were loaded from OPatch lspatches."
    if duration_seconds is not None and duration_seconds > 60:
        parsed["warning"] = "OPatch lsinventory took {0} seconds on this machine. This can happen when OPatch scans inactive patches.".format(duration_seconds)
        if callable(progress):
            progress(parsed["warning"])
    if result.get("exit_code") != 0 and not parsed.get("patches") and not parsed.get("versions") and not parsed.get("distributions"):
        clean_error = "\n".join(
            line for line in output.splitlines()
            if not line.startswith("OPATCH_PATH=") and not line.startswith("OPATCH_DURATION_SECONDS=")
        ).strip()
        parsed["error"] = clean_error or "OPatch lsinventory failed."
    return parsed


def normalize_ssl_port(value):
    text = str(value or "").strip()
    if not text or text.lower() in ("none", "false", "disabled", "0"):
        return ""
    return text if re.fullmatch(r"\d+", text) else ""


def weblogic_cert_host(row, target):
    for value in (row.get("listenAddress"), row.get("ip"), (target or {}).get("host")):
        text = str(value or "").strip()
        if text and text.lower() not in ("none", "0.0.0.0", "::", "localhost"):
            return text
    return str((target or {}).get("host") or "localhost").strip() or "localhost"


def parse_openssl_certificate_output(text):
    result = {"subject": "", "issuer": "", "expiresAt": ""}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if lower.startswith("subject="):
            result["subject"] = line.split("=", 1)[1].strip()
        elif lower.startswith("issuer="):
            result["issuer"] = line.split("=", 1)[1].strip()
        elif lower.startswith("notafter="):
            result["expiresAt"] = line.split("=", 1)[1].strip()
    return result


def build_openssl_endpoint_command(host, port, sni_name=""):
    connect_target = "{0}:{1}".format(str(host or "").strip(), str(port or "").strip())
    return (
        "CONNECT_TARGET={connect_target}; SNI_NAME={sni_name}; "
        "TMP=\"/tmp/iam-monitoring-openssl-$$.out\"; CERT=\"/tmp/iam-monitoring-openssl-$$.crt\"; ERR=\"/tmp/iam-monitoring-openssl-$$.err\"; "
        "cleanup() {{ rm -f \"$TMP\" \"$CERT\" \"$ERR\"; }}; trap cleanup EXIT; "
        "probe_cert() {{ server_name=\"$1\"; sni_args=''; [ -n \"$server_name\" ] && sni_args=\"-servername $server_name\"; "
        "if command -v timeout >/dev/null 2>&1; then timeout 18 openssl s_client -showcerts $sni_args -connect \"$CONNECT_TARGET\" </dev/null >\"$TMP\" 2>\"$ERR\"; "
        "else openssl s_client -showcerts $sni_args -connect \"$CONNECT_TARGET\" </dev/null >\"$TMP\" 2>\"$ERR\"; fi; }}; "
        "extract_leaf() {{ awk 'BEGIN{{p=0}} /-----BEGIN CERTIFICATE-----/{{p=1}} p{{print}} /-----END CERTIFICATE-----/{{exit}}' \"$TMP\" >\"$CERT\"; }}; "
        "probe_cert \"$SNI_NAME\"; rc=$?; extract_leaf; "
        "if [ ! -s \"$CERT\" ] && [ -n \"$SNI_NAME\" ]; then probe_cert ''; rc=$?; extract_leaf; fi; "
        "if [ -s \"$CERT\" ]; then openssl x509 -noout -subject -issuer -enddate <\"$CERT\" 2>/dev/null; exit $?; fi; "
        "echo \"Certificate not returned by openssl for $CONNECT_TARGET\"; head -n 3 \"$ERR\" 2>/dev/null || true; exit ${rc:-1}"
    ).format(
        connect_target=shlex.quote(connect_target),
        sni_name=shlex.quote(str(sni_name or "").strip()),
    )


def certificate_status(expires_at):
    text = " ".join(str(expires_at or "").replace("GMT", "").split())
    text = re.sub(r"\s+[A-Z]{2,5}\s+(\d{4})$", r" \1", text)
    if not text:
        return "warning", "Unknown", None
    expiry = None
    for date_format in ("%b %d %H:%M:%S %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            expiry = datetime.strptime(text, date_format).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if expiry is None:
        return "warning", "Unknown", None
    days_remaining = int((expiry - datetime.now(timezone.utc)).total_seconds() // 86400)
    if days_remaining < 0:
        return "expired", "Expired", days_remaining
    if days_remaining <= 30:
        return "warning", "{0} days".format(days_remaining), days_remaining
    return "healthy", "{0} days".format(days_remaining), days_remaining


def parse_keytool_certificates(text, keystore_name, keystore_path, command):
    rows = []
    current = None

    def finish_current():
        if not current:
            return
        status, label, days_remaining = certificate_status(current.get("expiresAt"))
        current.update({
            "status": status,
            "statusLabel": label,
            "daysRemaining": days_remaining,
            "command": command,
        })
        rows.append(current)

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("alias name:"):
            finish_current()
            current = {
                "source": "KeyStore",
                "keystore": keystore_name,
                "keystorePath": keystore_path,
                "alias": line.split(":", 1)[1].strip(),
                "subject": "",
                "issuer": "",
                "expiresAt": "",
            }
            continue
        if current is None:
            continue
        if lower.startswith("owner:"):
            current["subject"] = line.split(":", 1)[1].strip()
        elif lower.startswith("issuer:"):
            current["issuer"] = line.split(":", 1)[1].strip()
        elif lower.startswith("valid from:"):
            match = re.search(r"\buntil:\s*(.+)$", line, re.I)
            if match:
                current["expiresAt"] = match.group(1).strip()
        elif lower.startswith("entry type:"):
            current["entryType"] = line.split(":", 1)[1].strip()

    finish_current()
    return rows


def collect_keystore_certificates(target, oracle_home, domain_home, progress=None):
    oracle_home = str(oracle_home or "").strip()
    domain_home = str(domain_home or "").strip()
    if not oracle_home and not domain_home:
        return [], "ORACLE_HOME and WebLogic DOMAIN_HOME are not configured for keystore certificate collection."

    if callable(progress):
        progress("Starting WebLogic keystore certificate collection for DemoTrust.jks and DemoIdentity.jks.")

    command = (
        "ORACLE_HOME={oracle_home}; DOMAIN_HOME={domain_home}; export ORACLE_HOME DOMAIN_HOME; "
        "find_keytool() {{ "
        "for candidate in \"$ORACLE_HOME/oracle_common/jdk/bin/keytool\" \"$ORACLE_HOME/jdk/bin/keytool\" \"$(dirname \"$ORACLE_HOME\")/jdk/bin/keytool\"; do "
        "if [ -x \"$candidate\" ]; then printf '%s\\n' \"$candidate\"; return 0; fi; "
        "done; "
        "parent=$(dirname \"$ORACLE_HOME\"); "
        "if [ -d \"$parent\" ]; then found=$(find \"$parent\" -maxdepth 6 -type f -path '*/bin/keytool' 2>/dev/null | head -n 1); "
        "if [ -n \"$found\" ]; then printf '%s\\n' \"$found\"; return 0; fi; fi; "
        "command -v keytool 2>/dev/null || true; "
        "}}; "
        "find_keystore() {{ name=\"$1\"; "
        "for candidate in \"$DOMAIN_HOME/security/$name\" \"$DOMAIN_HOME/$name\" \"$ORACLE_HOME/wlserver/server/lib/$name\" \"$ORACLE_HOME/oracle_common/common/lib/$name\"; do "
        "if [ -f \"$candidate\" ]; then printf '%s\\n' \"$candidate\"; return 0; fi; "
        "done; "
        "for root in \"$DOMAIN_HOME\" \"$ORACLE_HOME\"; do "
        "if [ -d \"$root\" ]; then found=$(find \"$root\" -maxdepth 8 -type f -name \"$name\" 2>/dev/null | head -n 1); "
        "if [ -n \"$found\" ]; then printf '%s\\n' \"$found\"; return 0; fi; fi; "
        "done; return 1; "
        "}}; "
        "KEYTOOL=$(find_keytool); "
        "if [ -z \"$KEYTOOL\" ]; then echo 'KEYTOOL_ERROR=keytool executable not found'; exit 0; fi; "
        "run_store() {{ name=\"$1\"; shift; path=$(find_keystore \"$name\" || true); "
        "if [ -z \"$path\" ]; then echo \"KEYSTORE_ERROR|$name|not found\"; return 0; fi; "
        "echo \"KEYSTORE_BEGIN|$name|$path\"; "
        "out=\"/tmp/iam-monitoring-keytool-$$-$(printf '%s' \"$name\" | tr -cd 'A-Za-z0-9_.-').out\"; "
        "rc=1; "
        "for pass in \"$@\"; do "
        "\"$KEYTOOL\" -list -v -keystore \"$path\" -storepass \"$pass\" >\"$out\" 2>&1; rc=$?; "
        "if [ \"$rc\" -eq 0 ]; then break; fi; "
        "done; "
        "cat \"$out\" 2>/dev/null || true; rm -f \"$out\"; "
        "echo \"KEYSTORE_END|$name|$rc\"; "
        "}}; "
        "run_store DemoTrust.jks DemoIdentityKeyStorePassPhrase DemoTrustKeyStorePassPhrase; "
        "run_store DemoIdentity.jks DemoIdentityPassPhrase DemoIdentityKeyStorePassPhrase"
    ).format(
        oracle_home=shlex.quote(oracle_home),
        domain_home=shlex.quote(domain_home),
    )
    result = run_target(target, command, timeout=75)
    output = str(result.get("output") or "")
    errors = []
    sections = []
    current = None
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if line.startswith("KEYTOOL_ERROR="):
            errors.append(line.split("=", 1)[1].strip())
            continue
        if line.startswith("KEYSTORE_ERROR|"):
            parts = line.split("|", 2)
            if len(parts) >= 3:
                errors.append("{0}: {1}".format(parts[1], parts[2]))
            continue
        if line.startswith("KEYSTORE_BEGIN|"):
            parts = line.split("|", 2)
            current = {
                "name": parts[1] if len(parts) > 1 else "Keystore",
                "path": parts[2] if len(parts) > 2 else "",
                "output": [],
                "exitCode": 0,
            }
            continue
        if line.startswith("KEYSTORE_END|"):
            parts = line.split("|")
            if current is not None:
                try:
                    current["exitCode"] = int(parts[2]) if len(parts) > 2 else 0
                except (TypeError, ValueError):
                    current["exitCode"] = 1
                sections.append(current)
                current = None
            continue
        if current is not None:
            current["output"].append(line)

    certificates = []
    for section in sections:
        section_command = "keytool -list -v -keystore {0}".format(section.get("path") or section.get("name"))
        rows = parse_keytool_certificates(
            "\n".join(section.get("output") or []),
            section.get("name") or "Keystore",
            section.get("path") or "",
            section_command,
        )
        certificates.extend(rows)
        if section.get("exitCode") != 0 and not rows:
            first_error = next((line.strip() for line in section.get("output") or [] if line.strip()), "")
            if first_error:
                errors.append("{0}: {1}".format(section.get("name") or "Keystore", first_error))
            else:
                errors.append("{0}: keytool failed.".format(section.get("name") or "Keystore"))

    if result.get("exit_code") != 0 and not certificates:
        errors.append(output.strip() or "keytool certificate collection failed.")
    return certificates, "; ".join([item for item in errors if item])


def collect_ssl_endpoint_certificates(target, server_inventory, progress=None):
    endpoints = []
    seen = set()
    for row in server_inventory or []:
        port = normalize_ssl_port(row.get("sslPort"))
        if not port:
            continue
        host = weblogic_cert_host(row, target)
        server = str(row.get("name") or row.get("server") or host).strip()
        key = (server, host, port)
        if key in seen:
            continue
        seen.add(key)
        endpoints.append({"server": server, "host": host, "port": port})

    errors = []
    if not endpoints:
        errors.append("No SSL ports were found in the WebLogic server inventory.")
    elif callable(progress):
        progress("Starting WebLogic SSL certificate expiry collection for {0} endpoint(s).".format(len(endpoints)))

    def collect_endpoint_certificate(endpoint):
        sni_name = endpoint["host"]
        command = build_openssl_endpoint_command(endpoint["host"], endpoint["port"], sni_name)
        result = run_target(target, command, timeout=20)
        parsed = parse_openssl_certificate_output(result.get("output"))
        status, label, days_remaining = certificate_status(parsed.get("expiresAt"))
        if result.get("exit_code") != 0 or not parsed.get("expiresAt"):
            status = "warning"
            label = "Unavailable"
            parsed["subject"] = parsed.get("subject") or (str(result.get("output") or "").strip() or "Certificate not returned by openssl.")
        return {
            "source": "SSL Port",
            "server": endpoint["server"],
            "host": endpoint["host"],
            "port": endpoint["port"],
            "subject": parsed.get("subject") or "",
            "issuer": parsed.get("issuer") or "",
            "expiresAt": parsed.get("expiresAt") or "",
            "status": status,
            "statusLabel": label,
            "daysRemaining": days_remaining,
            "command": command,
        }

    certificates = []
    selected_endpoints = endpoints[:20]
    if selected_endpoints:
        workers = max(1, min(8, len(selected_endpoints)))
        with ThreadPoolExecutor(max_workers=workers) as ssl_executor:
            futures = [ssl_executor.submit(collect_endpoint_certificate, endpoint) for endpoint in selected_endpoints]
            for future in futures:
                try:
                    certificates.append(future.result())
                except Exception as exc:
                    certificates.append({
                        "source": "SSL Port",
                        "server": "Unknown",
                        "host": "",
                        "port": "",
                        "subject": str(exc),
                        "issuer": "",
                        "expiresAt": "",
                        "status": "warning",
                        "statusLabel": "Unavailable",
                        "daysRemaining": None,
                        "command": "",
                    })

    return certificates, "; ".join([item for item in errors if item])


def collect_ssl_certificates(target, server_inventory, oracle_home="", domain_home="", progress=None, keystore_future=None):
    certificates, endpoint_error = collect_ssl_endpoint_certificates(target, server_inventory, progress=progress)
    errors = []
    if endpoint_error:
        errors.append(endpoint_error)

    if keystore_future is not None:
        try:
            keystore_certificates, keystore_error = keystore_future.result()
        except Exception as exc:
            keystore_certificates, keystore_error = [], str(exc)
    else:
        keystore_certificates, keystore_error = collect_keystore_certificates(
            target,
            oracle_home,
            domain_home,
            progress=progress,
        )
    certificates.extend(keystore_certificates)
    if keystore_error:
        errors.append(keystore_error)

    if certificates:
        errors = [item for item in errors if not item.startswith("No SSL ports were found")]
    return certificates, "; ".join([item for item in errors if item])


def parse_listener_address_port(value):
    text = str(value or "").strip()
    if not text:
        return "", ""
    if text.startswith("[") and "]" in text:
        host, remainder = text[1:].split("]", 1)
        port = remainder[1:] if remainder.startswith(":") else ""
        return host.strip(), port.strip()
    if ":" in text:
        host, port = text.rsplit(":", 1)
        return host.strip(), port.strip()
    if re.fullmatch(r"\d+", text):
        return "", text
    return text, ""


def collect_oud_ssl_certificates(target, oud_metrics, progress=None):
    metrics = oud_metrics or {}
    listeners = metrics.get("listeners") or []
    endpoints = []
    seen = set()
    for listener in listeners:
        row = listener or {}
        protocol = str(row.get("protocol") or "").strip().upper()
        if not any(token in protocol for token in ("LDAPS", "SSL", "ADMIN")):
            continue
        host, port = parse_listener_address_port(row.get("addressPort") or row.get("port"))
        port = normalize_ssl_port(port)
        if not port:
            continue
        if not host or host.lower() in ("0.0.0.0", "::", "localhost", "*"):
            host = str((target or {}).get("host") or metrics.get("hostName") or "localhost").strip() or "localhost"
        instance_name = str(row.get("instanceName") or metrics.get("instanceName") or "OUD Instance").strip()
        key = (instance_name, host, port)
        if key in seen:
            continue
        seen.add(key)
        endpoints.append({
            "instanceName": instance_name,
            "host": host,
            "port": port,
            "protocol": protocol,
        })

    errors = []
    if not endpoints:
        errors.append("No OUD SSL listener ports were found in OUD status output.")
    elif callable(progress):
        progress("Starting OUD SSL certificate expiry collection for {0} endpoint(s).".format(len(endpoints)))

    cert_target = dict(target or {})
    cert_target["sudoRequired"] = False
    certificates = []
    for endpoint in endpoints[:30]:
        command = (
            "printf '' | openssl s_client -servername {server_name} -connect {connect_target} 2>/dev/null "
            "| openssl x509 -noout -subject -issuer -enddate 2>/dev/null"
        ).format(
            server_name=shlex.quote(endpoint["host"]),
            connect_target=shlex.quote("{0}:{1}".format(endpoint["host"], endpoint["port"])),
        )
        result = run_target(cert_target, command, timeout=20)
        parsed = parse_openssl_certificate_output(result.get("output"))
        status, label, days_remaining = certificate_status(parsed.get("expiresAt"))
        if result.get("exit_code") != 0 or not parsed.get("expiresAt"):
            status = "warning"
            label = "Unavailable"
            parsed["subject"] = parsed.get("subject") or (str(result.get("output") or "").strip() or "Certificate not returned by openssl.")
        certificates.append({
            "source": "OUD SSL Port",
            "instanceName": endpoint["instanceName"],
            "server": endpoint["instanceName"],
            "host": endpoint["host"],
            "port": endpoint["port"],
            "protocol": endpoint["protocol"],
            "subject": parsed.get("subject") or "",
            "issuer": parsed.get("issuer") or "",
            "expiresAt": parsed.get("expiresAt") or "",
            "status": status,
            "statusLabel": label,
            "daysRemaining": days_remaining,
            "command": command,
        })

    if certificates:
        errors = []
    return certificates, "; ".join([item for item in errors if item])


def enrich_oud_shared_metrics(target, environment, metrics, progress=None):
    result = metrics or {}
    settings = environment.get("oud") or {}
    oracle_home = str(settings.get("oracleHome") or "").strip()
    tasks = [
        ("certificates", lambda: collect_oud_ssl_certificates(target, result, progress=progress)),
    ]
    if oracle_home:
        tasks.append(("opatch", lambda: collect_opatch_inventory(target, oracle_home, progress=progress, label="OUD")))
    task_results = run_parallel_tasks(tasks, max_workers=2)
    result["opatch"] = task_result(task_results, "opatch") or {
        "error": "OUD ORACLE_HOME Path is not configured.",
        "versions": [],
        "products": [],
        "patches": [],
    }
    certificates, certificate_error = task_result(task_results, "certificates", ([], "OUD certificate collection failed."))
    result["certificates"] = certificates
    result["certificateError"] = certificate_error
    return result


OID_LDAP_QUERY_SPECS = [
    {
        "id": "configset",
        "label": "OID Instance Configset",
        "baseDn": "cn=osdldapd,cn=subconfigsubentry",
        "scope": "sub",
        "filter": "objectclass=*",
    },
    {"id": "monitor", "label": "cn=monitor Categories", "baseDn": "cn=monitor", "scope": "sub", "filter": "objectclass=*"},
    {"id": "system", "label": "System Information", "baseDn": "cn=system information,cn=monitor", "scope": "base", "filter": "objectclass=*"},
    {"id": "workQueue", "label": "Work Queue", "baseDn": "cn=work queue,cn=monitor", "scope": "base", "filter": "objectclass=*"},
    {"id": "clientConnections", "label": "Client Connections", "baseDn": "cn=client connections,cn=monitor", "scope": "base", "filter": "objectclass=*"},
    {"id": "version", "label": "Version", "baseDn": "cn=version,cn=monitor", "scope": "base", "filter": "objectclass=*"},
    {
        "id": "dipProfiles",
        "label": "DIP Server Profiles",
        "baseDn": "cn=subscriber profile,cn=changelog subscriber,cn=oracle internet directory",
        "scope": "sub",
        "filter": "objectclass=*",
    },
    {"id": "schema", "label": "Schema", "baseDn": "cn=subschemasubentry", "scope": "base", "filter": "objectclass=*", "verbose": True},
    {
        "id": "defaultSubscriber",
        "label": "Default Subscriber",
        "baseDn": "cn=common,cn=products,cn=oraclecontext",
        "scope": "base",
        "filter": "objectclass=*",
        "attrs": ["orcldefaultsubscriber"],
    },
    {
        "id": "passwordPolicies",
        "label": "Password Policies",
        "baseDn": "cn=pwdPolicies,cn=common,cn=products,cn=OracleContext",
        "scope": "sub",
        "filter": "(objectclass=pwdpolicy)",
        "appendDefaultSubscriber": True,
    },
    {
        "id": "externalPlugins",
        "label": "External Authentication Plugins",
        "baseDn": "cn=plugin,cn=subconfigsubentry",
        "scope": "sub",
        "filter": "objectclass=*",
    },
    {"id": "orclaci", "label": "orclaci", "baseDn": "", "scope": "sub", "filter": "orclaci=*", "attrs": ["orclaci"]},
    {"id": "catalogs", "label": "Indexed Attributes", "baseDn": "cn=catalogs", "scope": "base", "filter": "objectclass=*", "verbose": True},
    {"id": "replicationConfig", "label": "Replication Configset", "baseDn": "cn=osdrepld,cn=subconfigsubentry", "scope": "base", "filter": "objectclass=*"},
]


def parse_oid_processes(text):
    rows = []
    ldap_port = None
    ldaps_port = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or "oidldapd" not in line or "grep" in line:
            continue
        parts = line.split(None, 7)
        pid = parts[1] if len(parts) > 1 else ""
        args = parts[7] if len(parts) > 7 else line

        def first_match(patterns):
            for pattern in patterns:
                match = re.search(pattern, line, re.I)
                if match:
                    return match.group(1)
            return ""

        row_ldap = first_match([
            r"(?:^|\s)(?:-p|-port|-ldapport|--port)\s+([0-9]+)",
            r"(?:LDAP_PORT|OIDLDAPD_PORT|PORT)=([0-9]+)",
            r"(?:^|\s)-D(?:oracle\.)?ldap(?:\.server)?\.port=([0-9]+)",
        ])
        row_ldaps = first_match([
            r"(?:^|\s)(?:-sp|-sslport|-ldapsport|-secureport|--ssl-port)\s+([0-9]+)",
            r"(?:^|\s)sport=([0-9]+)",
            r"(?:LDAPS_PORT|SSL_PORT)=([0-9]+)",
            r"(?:^|\s)-D(?:oracle\.)?(?:ldaps|ssl)(?:\.server)?\.port=([0-9]+)",
        ])
        row_host = first_match([r"(?:^|\s)host=([^\s]+)"])
        row_instance = first_match([r"(?:^|\s)inst=([^\s]+)", r"(?:^|\s)instance=([^\s]+)", r"(?:^|\s)configset=([^\s]+)"])
        if row_ldap and ldap_port is None:
            ldap_port = as_int_safe(row_ldap)
        if row_ldaps and ldaps_port is None:
            ldaps_port = as_int_safe(row_ldaps)
        rows.append({
            "pid": pid,
            "instanceName": "OID Instance {0}".format(row_instance) if row_instance else "",
            "host": row_host,
            "ldapPort": row_ldap,
            "ldapsPort": row_ldaps,
            "command": args[:500],
        })
    return rows, ldap_port, ldaps_port


def oid_ldap_command(oracle_home, host, port, bind_dn, bind_password, spec):
    attrs = " ".join(shlex.quote(attr) for attr in (spec.get("attrs") or []))
    verbose = "-v " if spec.get("verbose") else ""
    base_dn = spec.get("baseDn", "")
    scope = spec.get("scope") or "base"
    filter_expr = spec.get("filter") or "objectclass=*"
    return (
        "ORACLE_HOME={home}; export ORACLE_HOME; "
        "LDAPSEARCH=\"$ORACLE_HOME/bin/ldapsearch\"; [ -x \"$LDAPSEARCH\" ] || LDAPSEARCH=ldapsearch; "
        "pwfile=$(mktemp /tmp/oidpwd.XXXXXX); trap 'rm -f \"$pwfile\"' EXIT; "
        "printf %s {password} > \"$pwfile\"; chmod 600 \"$pwfile\"; OIDPWD=$(cat \"$pwfile\"); "
        "\"$LDAPSEARCH\" -h {host} -p {port} -D {bind_dn} -w \"$OIDPWD\" -b {base_dn} -s {scope} {verbose}{filter_expr} {attrs}"
    ).format(
        home=shlex.quote(str(oracle_home or "")),
        password=shlex.quote(str(bind_password or "")),
        host=shlex.quote(str(host or "localhost")),
        port=shlex.quote(str(port or "")),
        bind_dn=shlex.quote(str(bind_dn or "cn=orcladmin")),
        base_dn=shlex.quote(base_dn),
        scope=shlex.quote(scope),
        verbose=verbose,
        filter_expr=shlex.quote(filter_expr),
        attrs=attrs,
    )


def oid_display_command(oracle_home, host, port, bind_dn, spec):
    attrs = " ".join(spec.get("attrs") or [])
    verbose = "-v " if spec.get("verbose") else ""
    return "{ldapsearch} -h {host} -p {port} -D {bind_dn} -w <password> -b {base_dn} -s {scope} {verbose}{filter_expr}{attrs}".format(
        ldapsearch="{0}/bin/ldapsearch".format(oracle_home).rstrip("/") if oracle_home else "ldapsearch",
        host=host or "localhost",
        port=port or "",
        bind_dn=bind_dn or "cn=orcladmin",
        base_dn=spec.get("baseDn", ""),
        scope=spec.get("scope") or "base",
        verbose=verbose,
        filter_expr=spec.get("filter") or "objectclass=*",
        attrs=(" " + attrs) if attrs else "",
    )


def flatten_oid_ldif(entries, label, ignored_attrs=None, enabled_attr=None):
    rows = []
    ignored = {"objectclass"}
    ignored.update(str(item or "").strip().lower() for item in (ignored_attrs or []))
    enabled_attr_name = str(enabled_attr or "").strip().lower()
    for entry in entries or []:
        entry_name = ", ".join(entry.get("dn") or entry.get("cn") or []) or label
        entry_enabled = False
        if enabled_attr_name:
            for key, values in entry.items():
                if str(key or "").strip().lower() != enabled_attr_name:
                    continue
                entry_enabled = any(str(value or "").strip().lower() in ("1", "true", "yes", "enabled") for value in values or [])
                break
        for key, values in entry.items():
            if str(key).lower() in ignored or key == "dn":
                continue
            for value in values[:20]:
                rows.append({
                    "entry": entry_name,
                    "name": key,
                    "value": value,
                    "enabled": entry_enabled,
                })
    return rows[:300]


def oid_entry_value(entry, *names):
    wanted = {str(name or "").strip().lower() for name in names if str(name or "").strip()}
    for key, values in (entry or {}).items():
        if str(key or "").strip().lower() not in wanted:
            continue
        for value in values or []:
            text = str(value or "").strip()
            if text:
                return text
    return ""


def compact_unique_values(values):
    result = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text.lower() in ("none", "null", "-"):
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def oid_entry_dn(entry):
    return ", ".join((entry or {}).get("dn") or []).strip()


def oid_instance_name(entry, index):
    dn = oid_entry_dn(entry)
    match = re.match(r"\s*cn\s*=\s*([^,]+)", dn, re.I)
    if match:
        name = match.group(1).strip()
        if name and name.lower() not in ("osdldapd", "subconfigsubentry"):
            return name
    for key in ("orcloidcomponentname", "orclcomponentname", "cn"):
        value = oid_entry_value(entry, key)
        if value and value.lower() not in ("osdldapd", "subconfigsubentry"):
            return value
    return "OID Instance {0}".format(index + 1)


def build_oid_instances(configset_entries, processes, fallback_host):
    candidates = []
    for entry in configset_entries or []:
        dn = oid_entry_dn(entry)
        lower_dn = dn.lower()
        if "cn=osdldapd" not in lower_dn and "cn=osdldapd" not in lower_dn.replace(" ", ""):
            continue
        attrs = {str(key or "").strip().lower() for key in entry.keys()}
        interesting = {
            "orclnonsslport",
            "orclsslport",
            "orcloidcomponentname",
            "orclservermode",
            "orclsslenable",
            "orclserverprocs",
            "orcldispthreads",
            "orclmaxldapconns",
            "orclhostname",
        }
        if not attrs.intersection(interesting):
            continue
        candidates.append(entry)

    named_candidates = []
    for entry in candidates:
        match = re.match(r"\s*cn\s*=\s*([^,]+)", oid_entry_dn(entry), re.I)
        name = match.group(1).strip().lower() if match else ""
        if name and name not in ("osdldapd", "subconfigsubentry"):
            named_candidates.append(entry)

    instances = []
    for entry in (named_candidates or candidates):
        index = len(instances)
        dn = oid_entry_dn(entry)
        instance = {
            "name": oid_instance_name(entry, index),
            "entryDn": dn,
            "componentName": oid_entry_value(entry, "orcloidcomponentname", "cn"),
            "host": oid_entry_value(entry, "orclhostname") or fallback_host or "localhost",
            "ldapPort": oid_entry_value(entry, "orclnonsslport"),
            "ldapsPort": oid_entry_value(entry, "orclsslport"),
            "serverMode": oid_entry_value(entry, "orclservermode"),
            "sslEnabled": oid_entry_value(entry, "orclsslenable"),
            "serverProcesses": oid_entry_value(entry, "orclserverprocs"),
            "dispatcherThreads": oid_entry_value(entry, "orcldispthreads"),
            "maxConnections": oid_entry_value(entry, "orclmaxldapconns"),
            "statsFlag": oid_entry_value(entry, "orclstatsflag"),
            "statsLevel": oid_entry_value(entry, "orclstatslevel"),
            "debugFlag": oid_entry_value(entry, "orcldebugflag"),
        }
        instances.append(instance)

    if instances:
        return instances

    for row in processes or []:
        if not row.get("ldapPort") and not row.get("ldapsPort"):
            continue
        instances.append({
            "name": row.get("instanceName") or "OID Process {0}".format(row.get("pid") or len(instances) + 1),
            "entryDn": "",
            "componentName": "",
            "host": row.get("host") or fallback_host or "localhost",
            "ldapPort": row.get("ldapPort") or "",
            "ldapsPort": row.get("ldapsPort") or "",
            "serverMode": "",
            "sslEnabled": "",
            "serverProcesses": "",
            "dispatcherThreads": "",
            "maxConnections": "",
            "statsFlag": "",
            "statsLevel": "",
            "debugFlag": "",
        })
    return instances


def oid_process_host_for_port(processes, port, fallback_host):
    wanted = str(port or "").strip()
    for row in processes or []:
        if wanted and str(row.get("ldapsPort") or "").strip() == wanted:
            host = str(row.get("host") or "").strip()
            if host and host.lower() not in ("localhost", "127.0.0.1"):
                return host
    return fallback_host or "localhost"


def oid_certificate_endpoints(instances, processes, fallback_host):
    endpoints = []
    seen_ports = set()
    for instance in instances or []:
        port = normalize_ssl_port(instance.get("ldapsPort"))
        if not port:
            continue
        connect_host = oid_process_host_for_port(processes, port, instance.get("host") or fallback_host)
        key = (connect_host, port)
        if key in seen_ports:
            continue
        seen_ports.add(key)
        endpoints.append({
            "instanceName": instance.get("name") or "OID Instance",
            "host": connect_host or fallback_host or "localhost",
            "port": port,
            "source": "OID LDAPS Port",
        })
    for row in processes or []:
        port = normalize_ssl_port(row.get("ldapsPort"))
        if not port:
            continue
        connect_host = str(row.get("host") or fallback_host or "localhost").strip() or "localhost"
        key = (connect_host, port)
        if key in seen_ports:
            continue
        seen_ports.add(key)
        endpoints.append({
            "instanceName": row.get("instanceName") or "OID Process {0}".format(row.get("pid") or ""),
            "host": connect_host,
            "port": port,
            "source": "OID LDAPS Port",
        })
    return endpoints


def oid_group_attr_value(ldap_groups, group_id, attribute_name):
    target_name = str(attribute_name or "").strip().lower()
    rows = ((ldap_groups or {}).get(group_id) or {}).get("rows") or []
    for row in rows:
        if str(row.get("name") or "").strip().lower() == target_name:
            value = str(row.get("value") or "").strip()
            if value:
                return value
    return ""


def oid_query_candidates(spec, ldap_groups):
    if not spec.get("appendDefaultSubscriber"):
        return [spec]
    base_dn = str(spec.get("baseDn") or "").strip()
    default_subscriber = oid_group_attr_value(ldap_groups, "defaultSubscriber", "orcldefaultsubscriber")
    candidates = []
    if base_dn and default_subscriber:
        candidate = dict(spec)
        candidate["baseDn"] = "{0},{1}".format(base_dn, default_subscriber)
        candidates.append(candidate)
    candidates.append(spec)
    return candidates


def oid_server_status(processes, ldap_groups):
    if not processes:
        return "Down", "down", "No oidldapd process was discovered on the configured OID host."
    configset = (ldap_groups or {}).get("configset") or {}
    if configset.get("rows"):
        return "Running", "healthy", "oidldapd is running and OID LDAP configset collection succeeded."
    if configset.get("error"):
        return "Running, LDAP Warning", "warning", configset.get("error")
    return "Running", "healthy", "oidldapd process is running."


def collect_oid_certificates(target, endpoints, progress=None):
    normalized_endpoints = []
    seen = set()
    for endpoint in endpoints or []:
        port = normalize_ssl_port((endpoint or {}).get("port"))
        if not port:
            continue
        host = str((endpoint or {}).get("host") or "localhost").strip() or "localhost"
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        normalized_endpoints.append({
            "instanceName": (endpoint or {}).get("instanceName") or "OID Instance",
            "host": host,
            "port": port,
            "source": (endpoint or {}).get("source") or "OID LDAPS Port",
        })
    if not normalized_endpoints:
        return [], "No OID LDAPS listener port was discovered from oidldapd or OID configset."
    if callable(progress):
        progress("Starting OID LDAPS certificate expiry collection for {0} endpoint(s).".format(len(normalized_endpoints)))
    cert_target = dict(target or {})
    cert_target["sudoRequired"] = False
    certificates = []
    for endpoint in normalized_endpoints[:20]:
        command = (
            "printf '' | openssl s_client -servername {server_name} -connect {connect_target} 2>/dev/null "
            "| openssl x509 -noout -subject -issuer -enddate 2>/dev/null"
        ).format(
            server_name=shlex.quote(endpoint["host"]),
            connect_target=shlex.quote("{0}:{1}".format(endpoint["host"], endpoint["port"])),
        )
        result = run_target(cert_target, command, timeout=20)
        parsed = parse_openssl_certificate_output(result.get("output"))
        status, label, days_remaining = certificate_status(parsed.get("expiresAt"))
        if result.get("exit_code") != 0 or not parsed.get("expiresAt"):
            status = "warning"
            label = "Unavailable"
            parsed["subject"] = parsed.get("subject") or (str(result.get("output") or "").strip() or "Certificate not returned by openssl.")
        certificates.append({
            "source": endpoint.get("source") or "OID LDAPS Port",
            "instanceName": endpoint.get("instanceName") or "OID Instance",
            "server": endpoint.get("instanceName") or "OID Instance",
            "host": endpoint["host"],
            "port": endpoint["port"],
            "subject": parsed.get("subject") or "",
            "issuer": parsed.get("issuer") or "",
            "expiresAt": parsed.get("expiresAt") or "",
            "status": status,
            "statusLabel": label,
            "daysRemaining": days_remaining,
            "command": command,
        })
    return certificates, ""


def get_oid_metrics(target, environment, progress=None):
    products = environment.get("products") or {}
    if not products.get("oid"):
        return None
    settings = environment.get("oid") or {}
    oid_target = target
    host = str(settings.get("host") or (oid_target or {}).get("host") or "localhost").strip() or "localhost"
    ldap_host = str(settings.get("ldapHost") or "localhost").strip() or "localhost"
    oracle_home = str(settings.get("oracleHome") or "").strip()
    bind_dn = str(settings.get("adminUsername") or "cn=orcladmin").strip() or "cn=orcladmin"
    bind_password = str(settings.get("adminPassword") or "").strip()
    if callable(progress):
        progress("Starting OID process, LDAP monitoring, OPatch, and certificate collection.")

    opatch_executor = None
    opatch_future = None
    if oracle_home:
        opatch_executor = ThreadPoolExecutor(max_workers=1)
        opatch_future = opatch_executor.submit(collect_opatch_inventory, oid_target, oracle_home, progress, "OID")

    process_command = "ps -ef | grep '[o]idldapd' || true"
    process_result = run_target(oid_target, process_command, timeout=30)
    processes, discovered_ldap_port, discovered_ldaps_port = parse_oid_processes(process_result.get("output"))
    ldap_port = settings.get("ldapPort") or discovered_ldap_port or 3060
    ldaps_port = settings.get("ldapsPort") or discovered_ldaps_port

    ldap_groups = {}
    configset_entries = []
    for spec in OID_LDAP_QUERY_SPECS:
        query_candidates = oid_query_candidates(spec, ldap_groups)
        display_spec = query_candidates[0]
        group = {
            "label": spec.get("label"),
            "baseDn": display_spec.get("baseDn", ""),
            "command": oid_display_command(oracle_home, ldap_host, ldap_port, bind_dn, display_spec),
            "rows": [],
            "error": "",
        }
        if not oracle_home:
            group["error"] = "OID ORACLE_HOME Path is not configured."
        elif not bind_password:
            group["error"] = "OID administrator password is not configured."
        else:
            for candidate_index, query_spec in enumerate(query_candidates):
                group["baseDn"] = query_spec.get("baseDn", "")
                group["command"] = oid_display_command(oracle_home, ldap_host, ldap_port, bind_dn, query_spec)
                result = run_target(
                    oid_target,
                    oid_ldap_command(oracle_home, ldap_host, ldap_port, bind_dn, bind_password, query_spec),
                    timeout=60,
                )
                if result.get("exit_code") == 0:
                    entries = parse_ldif_entries(result.get("output"))
                    if spec["id"] == "configset":
                        configset_entries = entries
                        group["rows"] = flatten_oid_ldif(entries, spec.get("label"), ignored_attrs=["orclaci"])
                    elif spec["id"] == "externalPlugins":
                        group["rows"] = flatten_oid_ldif(entries, spec.get("label"), enabled_attr="orclpluginenable")
                        group["rows"].sort(key=lambda row: (not bool(row.get("enabled")), str(row.get("entry") or ""), str(row.get("name") or "")))
                    else:
                        group["rows"] = flatten_oid_ldif(entries, spec.get("label"))
                    group["error"] = ""
                    break
                else:
                    group["error"] = result.get("output") or "ldapsearch failed."
                    if candidate_index == len(query_candidates) - 1:
                        break
        ldap_groups[spec["id"]] = group

    oid_instances = build_oid_instances(configset_entries, processes, ldap_host)
    config_ldap_port = as_int_safe(oid_group_attr_value(ldap_groups, "configset", "orclnonsslport"))
    config_ldaps_port = as_int_safe(oid_group_attr_value(ldap_groups, "configset", "orclsslport"))
    if config_ldap_port:
        ldap_port = config_ldap_port
    if config_ldaps_port:
        ldaps_port = config_ldaps_port
    ldap_ports = compact_unique_values([settings.get("ldapPort"), discovered_ldap_port, config_ldap_port] + [item.get("ldapPort") for item in oid_instances] + [row.get("ldapPort") for row in processes])
    ldaps_ports = compact_unique_values([settings.get("ldapsPort"), discovered_ldaps_port, config_ldaps_port] + [item.get("ldapsPort") for item in oid_instances] + [row.get("ldapsPort") for row in processes])

    if opatch_future is not None:
        try:
            opatch = opatch_future.result()
        except Exception as exc:
            opatch = {"error": str(exc), "versions": [], "products": [], "patches": []}
        opatch["recommendation"] = build_fmw_patch_recommendation(opatch, environment, oracle_home)
        opatch["patchComparisonRows"] = opatch["recommendation"].get("comparisonRows", [])
    else:
        opatch = {
            "error": "OID ORACLE_HOME Path is not configured.",
            "versions": [],
            "products": [],
            "patches": [],
        }
    if opatch_executor is not None:
        opatch_executor.shutdown(wait=False)
    server_status, server_status_severity, server_status_reason = oid_server_status(processes, ldap_groups)
    certificate_endpoints = oid_certificate_endpoints(oid_instances, processes, ldap_host)
    certificates, certificate_error = collect_oid_certificates(oid_target, certificate_endpoints, progress=progress)
    return {
        "enabled": True,
        "host": host,
        "ldapSearchHost": ldap_host,
        "oracleHome": oracle_home,
        "adminUsername": bind_dn,
        "ldapPort": ldap_port,
        "ldapsPort": ldaps_port,
        "ldapPorts": ldap_ports,
        "ldapsPorts": ldaps_ports,
        "instances": oid_instances,
        "instanceCount": len(oid_instances),
        "serverStatus": server_status,
        "serverStatusSeverity": server_status_severity,
        "serverStatusReason": server_status_reason,
        "processes": processes,
        "processCount": len(processes),
        "processCommand": process_command,
        "processError": "" if process_result.get("exit_code") == 0 else (process_result.get("output") or "ps command failed."),
        "ldapGroups": ldap_groups,
        "opatch": opatch,
        "certificates": certificates,
        "certificateError": certificate_error,
    }


def run_wlst_script(target, wlst_path, script_body, timeout=180):
    command = (
        "scriptfile=$(mktemp /tmp/iam-monitoring-wlst.XXXXXX.py) && "
        "trap 'rm -f \"$scriptfile\"' EXIT && "
        "cat > \"$scriptfile\" <<'PY'\n"
        "{0}\n"
        "PY\n"
        "WLST={1}; "
        "if [ -x \"$WLST\" ]; then \"$WLST\" \"$scriptfile\"; else sh \"$WLST\" \"$scriptfile\"; fi"
    ).format(
        script_body.rstrip(),
        shlex.quote(wlst_path),
    )
    return command, run_target(target, command, timeout=timeout)


def build_dms_wlst_script(admin_username, admin_password, deployment_connect_url):
    return (
        "import os\n"
        "import sys\n"
        "import time\n"
        "connect('" + python_string_literal(admin_username) + "','" + python_string_literal(admin_password) + "','" + python_string_literal(deployment_connect_url) + "')\n"
        "def clean_dms(value):\n"
        "    if value is None:\n"
        "        return ''\n"
        "    return str(value).replace('|', '/').replace('\\n', ' ').replace('\\r', ' ')\n"
        "def safe_dms_call(bean, method_name, default_value):\n"
        "    try:\n"
        "        if bean is None:\n"
        "            return default_value\n"
        "        value = getattr(bean, method_name)()\n"
        "        if value is None:\n"
        "            return default_value\n"
        "        return value\n"
        "    except:\n"
        "        return default_value\n"
        "def dms_list(value):\n"
        "    try:\n"
        "        return list(value or [])\n"
        "    except:\n"
        "        return []\n"
        "dms_servers = []\n"
        "dms_found = False\n"
        "try:\n"
        "    domainConfig()\n"
        "    deployments = []\n"
        "    for getter in ['getAppDeployments', 'getInternalAppDeployments']:\n"
        "        deployments.extend(dms_list(safe_dms_call(cmo, getter, [])))\n"
        "    for deployment in deployments:\n"
        "        app_name = clean_dms(safe_dms_call(deployment, 'getName', ''))\n"
        "        source_path = clean_dms(safe_dms_call(deployment, 'getSourcePath', ''))\n"
        "        if app_name.lower() != 'dms' and not source_path.lower().endswith('/dms.war'):\n"
        "            continue\n"
        "        dms_found = True\n"
        "        for target in dms_list(safe_dms_call(deployment, 'getTargets', [])):\n"
        "            target_name = clean_dms(safe_dms_call(target, 'getName', ''))\n"
        "            target_servers = dms_list(safe_dms_call(target, 'getServers', []))\n"
        "            if target_servers:\n"
        "                for server in target_servers:\n"
        "                    server_name = clean_dms(safe_dms_call(server, 'getName', ''))\n"
        "                    if server_name and server_name not in dms_servers:\n"
        "                        dms_servers.append(server_name)\n"
        "                    print('IAM_DMS_TARGET|' + app_name + '|' + target_name + '|' + server_name)\n"
        "            else:\n"
        "                if target_name and target_name not in dms_servers:\n"
        "                    dms_servers.append(target_name)\n"
        "                print('IAM_DMS_TARGET|' + app_name + '|' + target_name + '|' + target_name)\n"
        "except:\n"
        "    print('IAM_DMS_ERROR|Deployment discovery failed: ' + clean_dms(sys.exc_info()[1]))\n"
        "if not dms_found:\n"
        "    print('IAM_DMS_ERROR|The dms application was not found in WebLogic domain configuration.')\n"
        "elif not dms_servers:\n"
        "    print('IAM_DMS_ERROR|The dms deployment has no server or cluster targets.')\n"
        "else:\n"
        "    try:\n"
        "        table_names = dms_list(displayMetricTableNames(servers=dms_servers))\n"
        "        for table_name in table_names:\n"
        "            print('IAM_DMS_TABLE|' + clean_dms(table_name))\n"
        "        dms_output_file = '/tmp/iam-monitoring-dms-' + str(int(time.time() * 1000)) + '.xml'\n"
        "        if os.path.exists(dms_output_file):\n"
        "            os.remove(dms_output_file)\n"
        "        dumpMetrics(servers=dms_servers, format='xml', outputfile=dms_output_file)\n"
        "        print('IAM_DMS_XML_BEGIN')\n"
        "        metric_file = open(dms_output_file, 'r')\n"
        "        try:\n"
        "            for metric_line in metric_file:\n"
        "                sys.stdout.write(metric_line)\n"
        "        finally:\n"
        "            metric_file.close()\n"
        "            if os.path.exists(dms_output_file):\n"
        "                os.remove(dms_output_file)\n"
        "        print('')\n"
        "        print('IAM_DMS_XML_END')\n"
        "    except:\n"
        "        print('IAM_DMS_ERROR|Metric collection failed: ' + clean_dms(sys.exc_info()[1]))\n"
        "exit()\n"
    )


def dms_table_priority(name):
    lowered = str(name or "").lower()
    product_terms = ("oam", "oim", "oracle_security", "oracle.security", "accessmanager", "identity")
    runtime_terms = ("jvm", "jdbc", "servlet", "webapp", "thread", "workmanager", "j2ee", "datasource")
    if any(term in lowered for term in product_terms):
        return 100
    if any(term in lowered for term in runtime_terms):
        return 50
    return 0


def parse_dms_wlst_output(text, max_tables=24, max_rows_per_table=10, max_metrics=600):
    output = str(text or "")
    deployments = []
    errors = []
    reported_tables = []
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("IAM_DMS_TARGET|"):
            parts = stripped.split("|", 3)
            if len(parts) == 4:
                deployments.append({"application": parts[1], "target": parts[2], "server": parts[3]})
        elif stripped.startswith("IAM_DMS_TABLE|"):
            table_name = stripped.split("|", 1)[1].strip()
            if table_name and table_name not in reported_tables:
                reported_tables.append(table_name)
        elif stripped.startswith("IAM_DMS_ERROR|"):
            errors.append(stripped.split("|", 1)[1].strip())

    result = {
        "deployments": deployments,
        "servers": sorted(set(item.get("server") for item in deployments if item.get("server"))),
        "tableCount": len(reported_tables),
        "tableInventory": [{"name": name} for name in reported_tables[:500]],
        "tables": [],
        "metrics": [],
        "error": "; ".join(item for item in errors if item),
    }
    match = re.search(r"IAM_DMS_XML_BEGIN\s*(.*?)\s*IAM_DMS_XML_END", output, re.DOTALL)
    if not match:
        if deployments and not result["error"]:
            result["error"] = "DMS returned no XML metric document."
        return result

    xml_text = re.sub(r"<\?xml[^>]*\?>", "", match.group(1)).strip()
    xml_text = re.sub(r"<!DOCTYPE[^>]*>", "", xml_text).strip()
    try:
        root = ET.fromstring("<dmsMetrics>{0}</dmsMetrics>".format(xml_text))
    except ET.ParseError as exc:
        result["error"] = "; ".join(item for item in (result["error"], "Unable to parse DMS XML: {0}".format(exc)) if item)
        return result

    parsed_tables = []
    for index, table in enumerate(root.findall(".//table")):
        name = str(table.attrib.get("name") or "DMS Table").strip()
        server = str(table.attrib.get("componentId") or "").strip()
        rows = table.findall("./row")
        parsed_tables.append({
            "index": index,
            "name": name,
            "server": server,
            "rowCount": len(rows),
            "keys": str(table.attrib.get("keys") or "").split(),
            "element": table,
            "priority": dms_table_priority(name),
        })

    if reported_tables and not parsed_tables:
        result["error"] = "; ".join(item for item in (
            result["error"],
            "DMS reported metric table names but returned no XML table content.",
        ) if item)

    if not reported_tables:
        unique_names = []
        for table in parsed_tables:
            if table["name"] not in unique_names:
                unique_names.append(table["name"])
        result["tableCount"] = len(unique_names)
        result["tableInventory"] = [{"name": name} for name in unique_names[:500]]

    selected = [item for item in parsed_tables if item["priority"] > 0]
    selected.sort(key=lambda item: (-item["priority"], item["name"].lower(), item["index"]))
    if not selected:
        selected = parsed_tables[:max_tables]
    selected = selected[:max_tables]

    for table in selected:
        result["tables"].append({
            "name": table["name"],
            "server": table["server"],
            "rowCount": table["rowCount"],
        })
        identity_names = set(table["keys"])
        for row_index, row in enumerate(table["element"].findall("./row")[:max_rows_per_table], start=1):
            values = {}
            types = {}
            for column in row.findall("./column"):
                column_name = str(column.attrib.get("name") or "").strip()
                if not column_name:
                    continue
                values[column_name] = str(column.text or "").strip()
                types[column_name] = str(column.attrib.get("type") or "").strip()
            identity_values = [values.get(key) for key in table["keys"] if values.get(key)]
            instance = " / ".join(identity_values[:4]) or "Row {0}".format(row_index)
            for metric_name, metric_value in values.items():
                if metric_name in identity_names or metric_value == "":
                    continue
                result["metrics"].append({
                    "server": table["server"],
                    "table": table["name"],
                    "instance": instance,
                    "metric": metric_name,
                    "value": metric_value[:500],
                    "type": types.get(metric_name) or "",
                })
                if len(result["metrics"]) >= max_metrics:
                    return result
    return result


def collect_dms_metrics(target, wlst_path, admin_username, admin_password, deployment_connect_url, progress=None):
    if callable(progress):
        progress("Discovering DMS deployment targets from WebLogic domain config.xml and collecting DMS metrics through WLST.")
    command, run_result = run_wlst_script(
        target,
        wlst_path,
        build_dms_wlst_script(admin_username, admin_password, deployment_connect_url),
        timeout=300,
    )
    parsed = parse_dms_wlst_output(run_result.get("output"))
    parsed["command"] = "Use Oracle Common WLST DMS commands for config.xml-targeted servers."
    if run_result.get("exit_code") != 0 and not parsed.get("error"):
        parsed["error"] = str(run_result.get("output") or "DMS WLST collection failed.").strip()
    return parsed


def build_weblogic_runtime_script(admin_username, admin_password, deployment_connect_url):
    return (
        "from java.net import InetAddress\n"
        "connect('" + python_string_literal(admin_username) + "','" + python_string_literal(admin_password) + "','" + python_string_literal(deployment_connect_url) + "')\n"
        "def clean(value):\n"
        "    if value is None:\n"
        "        return ''\n"
        "    return str(value).replace('|', '/').replace('\\n', ' ').replace('\\r', ' ')\n"
        "def emit(section, server, name, metric, value):\n"
        "    print(section + ' | ' + clean(server) + ' | ' + clean(name) + ' | ' + clean(metric) + ' | ' + clean(value))\n"
        "def emit_attr(section, server, name, metric, bean, method_name):\n"
        "    try:\n"
        "        method = getattr(bean, method_name)\n"
        "        emit(section, server, name, metric, method())\n"
        "    except:\n"
        "        emit(section, server, name, metric, 'ERROR')\n"
        "print('TYPE | SERVER | STATE | MACHINE | LISTEN_ADDRESS | IP | PORT | SSL_PORT | CLUSTER')\n"
        "def safe_mbean_call(bean, method_name, default_value):\n"
        "    try:\n"
        "        if bean is None:\n"
        "            return default_value\n"
        "        method = getattr(bean, method_name)\n"
        "        value = method()\n"
        "        if value is None:\n"
        "            return default_value\n"
        "        return value\n"
        "    except:\n"
        "        return default_value\n"
        "def as_list(value):\n"
        "    if value is None:\n"
        "        return []\n"
        "    try:\n"
        "        return list(value)\n"
        "    except:\n"
        "        rows = []\n"
        "        try:\n"
        "            for item in value:\n"
        "                rows.append(item)\n"
        "        except:\n"
        "            pass\n"
        "        return rows\n"
        "def remember_server(order, name):\n"
        "    if name and name not in order:\n"
        "        order.append(name)\n"
        "def resolve_ip(listen_address, machine_obj):\n"
        "    ip_value = 'None'\n"
        "    try:\n"
        "        resolver = listen_address\n"
        "        if resolver in ('', '0.0.0.0', '::') and machine_obj:\n"
        "            node_manager = safe_mbean_call(machine_obj, 'getNodeManager', None)\n"
        "            if node_manager:\n"
        "                resolver = safe_mbean_call(node_manager, 'getListenAddress', resolver) or resolver\n"
        "        if resolver and resolver not in ('0.0.0.0', '::'):\n"
        "            ip_value = InetAddress.getByName(resolver).getHostAddress()\n"
        "    except:\n"
        "        pass\n"
        "    return ip_value\n"
        "def read_server_config(server):\n"
        "    name = clean(safe_mbean_call(server, 'getName', ''))\n"
        "    machine_obj = safe_mbean_call(server, 'getMachine', None)\n"
        "    machine = 'None'\n"
        "    if machine_obj:\n"
        "        machine = clean(safe_mbean_call(machine_obj, 'getName', 'None') or 'None')\n"
        "    listen_address = clean(safe_mbean_call(server, 'getListenAddress', '') or '0.0.0.0')\n"
        "    port = clean(safe_mbean_call(server, 'getListenPort', ''))\n"
        "    ssl_port = 'None'\n"
        "    ssl = safe_mbean_call(server, 'getSSL', None)\n"
        "    if ssl:\n"
        "        ssl_port = clean(safe_mbean_call(ssl, 'getListenPort', 'None') or 'None')\n"
        "    cluster = 'None'\n"
        "    cluster_obj = safe_mbean_call(server, 'getCluster', None)\n"
        "    if cluster_obj:\n"
        "        cluster = clean(safe_mbean_call(cluster_obj, 'getName', 'None') or 'None')\n"
        "    return {'name': name, 'machine': machine, 'listenAddress': listen_address, 'port': port, 'sslPort': ssl_port, 'cluster': cluster, 'ip': resolve_ip(listen_address, machine_obj)}\n"
        "def split_listen_value(value):\n"
        "    listen_address = clean(value)\n"
        "    ip_value = 'None'\n"
        "    try:\n"
        "        if '/' in listen_address:\n"
        "            pieces = listen_address.split('/', 1)\n"
        "            listen_address = pieces[0] or listen_address\n"
        "            ip_value = pieces[1] or 'None'\n"
        "    except:\n"
        "        pass\n"
        "    return listen_address, ip_value\n"
        "def host_port_from_url(value):\n"
        "    host = ''\n"
        "    port = ''\n"
        "    try:\n"
        "        text = clean(value)\n"
        "        if '://' in text:\n"
        "            text = text.split('://', 1)[1]\n"
        "        if '/' in text:\n"
        "            text = text.split('/', 1)[0]\n"
        "        if '@' in text:\n"
        "            text = text.split('@')[-1]\n"
        "        if ':' in text:\n"
        "            pieces = text.split(':')\n"
        "            port = pieces[-1]\n"
        "            host = ':'.join(pieces[:-1])\n"
        "        else:\n"
        "            host = text\n"
        "    except:\n"
        "        pass\n"
        "    return host, port\n"
        "def read_server_runtime(runtime):\n"
        "    name = clean(safe_mbean_call(runtime, 'getName', ''))\n"
        "    machine = 'None'\n"
        "    machine_obj = safe_mbean_call(runtime, 'getCurrentMachine', None)\n"
        "    if machine_obj:\n"
        "        machine_name = safe_mbean_call(machine_obj, 'getName', None)\n"
        "        if machine_name:\n"
        "            machine = clean(machine_name)\n"
        "        else:\n"
        "            machine = clean(machine_obj)\n"
        "    listen_address = clean(safe_mbean_call(runtime, 'getListenAddress', ''))\n"
        "    listen_address, ip_value = split_listen_value(listen_address)\n"
        "    port = clean(safe_mbean_call(runtime, 'getListenPort', ''))\n"
        "    ssl_port = clean(safe_mbean_call(runtime, 'getSSLListenPort', 'None') or 'None')\n"
        "    for method_name in ['getDefaultURL', 'getAdministrationURL']:\n"
        "        if listen_address and port:\n"
        "            break\n"
        "        url_host, url_port = host_port_from_url(safe_mbean_call(runtime, method_name, ''))\n"
        "        if not listen_address and url_host:\n"
        "            listen_address = url_host\n"
        "        if not port and url_port:\n"
        "            port = url_port\n"
        "    cluster = 'None'\n"
        "    cluster_obj = safe_mbean_call(runtime, 'getClusterRuntime', None)\n"
        "    if cluster_obj:\n"
        "        cluster = clean(safe_mbean_call(cluster_obj, 'getName', 'None') or 'None')\n"
        "    if not ip_value or ip_value == 'None':\n"
        "        ip_value = resolve_ip(listen_address, None)\n"
        "    if not listen_address:\n"
        "        listen_address = 'None'\n"
        "    if not port:\n"
        "        port = 'None'\n"
        "    if not ssl_port or ssl_port in ('-1', '0'):\n"
        "        ssl_port = 'None'\n"
        "    return {'name': name, 'machine': machine, 'listenAddress': listen_address, 'port': port, 'sslPort': ssl_port, 'cluster': cluster, 'ip': ip_value}\n"
        "def missing_value(value):\n"
        "    text = clean(value)\n"
        "    return text in ('', 'None', 'null', 'N/A')\n"
        "def merge_server_info(config_info, runtime_info):\n"
        "    info = {'machine': 'None', 'listenAddress': 'None', 'ip': 'None', 'port': 'None', 'sslPort': 'None', 'cluster': 'None'}\n"
        "    for source in [config_info, runtime_info]:\n"
        "        if not source:\n"
        "            continue\n"
        "        for key in ['machine', 'ip', 'port', 'sslPort', 'cluster']:\n"
        "            if missing_value(info.get(key)) and not missing_value(source.get(key)):\n"
        "                info[key] = clean(source.get(key))\n"
        "        source_listen = source.get('listenAddress')\n"
        "        if (missing_value(info.get('listenAddress')) or info.get('listenAddress') in ('0.0.0.0', '::')) and not missing_value(source_listen):\n"
        "            info['listenAddress'] = clean(source_listen)\n"
        "    return info\n"
        "runtime_map = {}\n"
        "lifecycle_map = {}\n"
        "runtime_info_map = {}\n"
        "config_map = {}\n"
        "server_order = []\n"
        "admin_server_name = 'AdminServer'\n"
        "try:\n"
        "    domainRuntime()\n"
        "    runtimes = []\n"
        "    try:\n"
        "        runtimes = as_list(cmo.getServerRuntimes())\n"
        "    except:\n"
        "        runtimes = as_list(domainRuntimeService.getServerRuntimes())\n"
        "    for runtime in runtimes:\n"
        "        name = clean(safe_mbean_call(runtime, 'getName', ''))\n"
        "        if name:\n"
        "            runtime_map[name] = clean(safe_mbean_call(runtime, 'getState', 'RUNNING') or 'RUNNING')\n"
        "            runtime_info_map[name] = read_server_runtime(runtime)\n"
        "            remember_server(server_order, name)\n"
        "    lifecycles = []\n"
        "    try:\n"
        "        lifecycles = as_list(cmo.getServerLifeCycleRuntimes())\n"
        "    except:\n"
        "        lifecycles = as_list(domainRuntimeService.getServerLifeCycleRuntimes())\n"
        "    for lifecycle in lifecycles:\n"
        "        name = clean(safe_mbean_call(lifecycle, 'getName', ''))\n"
        "        if name:\n"
        "            lifecycle_map[name] = clean(safe_mbean_call(lifecycle, 'getState', 'UNKNOWN') or 'UNKNOWN')\n"
        "            remember_server(server_order, name)\n"
        "except:\n"
        "    pass\n"
        "try:\n"
        "    domainConfig()\n"
        "    admin_server_name = clean(safe_mbean_call(cmo, 'getAdminServerName', admin_server_name) or admin_server_name)\n"
        "    for server in as_list(cmo.getServers()):\n"
        "        try:\n"
        "            info = read_server_config(server)\n"
        "            name = info.get('name')\n"
        "            if name:\n"
        "                config_map[name] = info\n"
        "                remember_server(server_order, name)\n"
        "        except:\n"
        "            pass\n"
        "except:\n"
        "    pass\n"
        "for server_name in server_order:\n"
        "    info = merge_server_info(config_map.get(server_name), runtime_info_map.get(server_name))\n"
        "    state = runtime_map.get(server_name, lifecycle_map.get(server_name, 'UNKNOWN'))\n"
        "    server_type = 'ADMIN'\n"
        "    if server_name != admin_server_name:\n"
        "        server_type = 'MANAGED'\n"
        "    print(server_type + ' | ' + server_name + ' | ' + state + ' | ' + info.get('machine', 'None') + ' | ' + info.get('listenAddress', 'None') + ' | ' + info.get('ip', 'None') + ' | ' + str(info.get('port', 'None')) + ' | ' + str(info.get('sslPort', 'None')) + ' | ' + info.get('cluster', 'None'))\n"
        "try:\n"
        "    domainRuntime()\n"
        "    cd('/AppRuntimeStateRuntime/AppRuntimeStateRuntime')\n"
        "    apps = cmo.getApplicationIds()\n"
        "    print('\\n===== Application Deployment Status =====\\n')\n"
        "    for app in apps:\n"
        "        state = 'UNKNOWN'\n"
        "        try:\n"
        "            state = cmo.getCurrentState(app, 'AdminServer')\n"
        "        except:\n"
        "            pass\n"
        "        print(str(app) + ' : ' + str(state))\n"
        "    print('\\n=========================================\\n')\n"
        "except:\n"
        "    pass\n"
        "try:\n"
        "    domainRuntime()\n"
        "    print('SERVER | STUCK_THREADS | HOGGING_THREADS')\n"
        "    cd('/ServerRuntimes')\n"
        "    for name in ls(returnMap='true'):\n"
        "        try:\n"
        "            cd('/ServerRuntimes/' + name + '/ThreadPoolRuntime/ThreadPoolRuntime')\n"
        "            stuck = cmo.getStuckThreadCount()\n"
        "            hogging = cmo.getHoggingThreadCount()\n"
        "            print(name + ' | ' + str(stuck) + ' | ' + str(hogging))\n"
        "        except:\n"
        "            print(name + ' | ERROR | ERROR')\n"
        "        cd('/ServerRuntimes')\n"
        "except:\n"
        "    pass\n"
        "try:\n"
        "    domainRuntime()\n"
        "    print('SERVER | DATASOURCE | ACTIVE | WAITING')\n"
        "    for runtime in domainRuntimeService.getServerRuntimes():\n"
        "        server_name = runtime.getName()\n"
        "        try:\n"
        "            jdbc_service = runtime.getJDBCServiceRuntime()\n"
        "            data_sources = []\n"
        "            if jdbc_service:\n"
        "                data_sources = jdbc_service.getJDBCDataSourceRuntimeMBeans()\n"
        "            for data_source in data_sources:\n"
        "                print(server_name + ' | ' + data_source.getName() + ' | ' + str(data_source.getActiveConnectionsCurrentCount()) + ' | ' + str(data_source.getWaitingForConnectionCurrentCount()))\n"
        "        except:\n"
        "            pass\n"
        "except:\n"
        "    pass\n"
        "try:\n"
        "    domainRuntime()\n"
        "    print('SECTION | SERVER | NAME | METRIC | VALUE')\n"
        "    for runtime in domainRuntimeService.getServerRuntimes():\n"
        "        server = runtime.getName()\n"
        "        try:\n"
        "            jta = runtime.getJTARuntime()\n"
        "            for metric, method_name in [('activeTransactionsTotalCount','getActiveTransactionsTotalCount'),('transactionTotalCount','getTransactionTotalCount'),('committedTotalCount','getCommittedTotalCount'),('rolledBackTotalCount','getRolledBackTotalCount'),('heuristicsTotalCount','getHeuristicsTotalCount'),('secondsActiveTotalCount','getSecondsActiveTotalCount')]:\n"
        "                emit_attr('jta', server, server, metric, jta, method_name)\n"
        "        except:\n"
        "            emit('jta', server, server, 'error', 'JTARuntime unavailable')\n"
        "        try:\n"
        "            jvm = runtime.getJVMRuntime()\n"
        "            for metric, method_name in [('heapFreeCurrent','getHeapFreeCurrent'),('heapSizeCurrent','getHeapSizeCurrent'),('heapFreePercent','getHeapFreePercent'),('javaVersion','getJavaVersion'),('uptime','getUptime')]:\n"
        "                emit_attr('jvm', server, server, metric, jvm, method_name)\n"
        "        except:\n"
        "            emit('jvm', server, server, 'error', 'JVMRuntime unavailable')\n"
        "        try:\n"
        "            thread_pool = runtime.getThreadPoolRuntime()\n"
        "            for metric, method_name in [('executeThreadTotalCount','getExecuteThreadTotalCount'),('executeThreadIdleCount','getExecuteThreadIdleCount'),('pendingUserRequestCount','getPendingUserRequestCount'),('queueLength','getQueueLength'),('hoggingThreadCount','getHoggingThreadCount'),('stuckThreadCount','getStuckThreadCount'),('throughput','getThroughput')]:\n"
        "                emit_attr('threadPool', server, server, metric, thread_pool, method_name)\n"
        "        except:\n"
        "            emit('threadPool', server, server, 'error', 'ThreadPoolRuntime unavailable')\n"
        "        try:\n"
        "            socket_runtime = runtime.getSocketRuntime()\n"
        "            for metric, method_name in [('socketsOpenedTotalCount','getSocketsOpenedTotalCount'),('socketsClosedTotalCount','getSocketsClosedTotalCount'),('currentOpenSocketCount','getCurrentOpenSocketCount')]:\n"
        "                emit_attr('socket', server, server, metric, socket_runtime, method_name)\n"
        "        except:\n"
        "            emit('socket', server, server, 'error', 'SocketRuntime unavailable')\n"
        "        try:\n"
        "            jdbc_service = runtime.getJDBCServiceRuntime()\n"
        "            data_sources = []\n"
        "            if jdbc_service:\n"
        "                data_sources = jdbc_service.getJDBCDataSourceRuntimeMBeans()\n"
        "            for data_source in data_sources:\n"
        "                name = data_source.getName()\n"
        "                for metric, method_name in [('failuresToReconnectCount','getFailuresToReconnectCount'),('leakedConnectionCount','getLeakedConnectionCount'),('currCapacity','getCurrCapacity'),('state','getState'),('activeConnectionsCurrentCount','getActiveConnectionsCurrentCount'),('waitingForConnectionCurrentCount','getWaitingForConnectionCurrentCount')]:\n"
        "                    emit_attr('jdbcHealth', server, name, metric, data_source, method_name)\n"
        "        except:\n"
        "            emit('jdbcHealth', server, server, 'error', 'JDBCServiceRuntime unavailable')\n"
        "        try:\n"
        "            jms_runtime = runtime.getJMSRuntime()\n"
        "            jms_servers = []\n"
        "            if jms_runtime:\n"
        "                jms_servers = jms_runtime.getJMSServers()\n"
        "            for jms_server in jms_servers:\n"
        "                destinations = []\n"
        "                if jms_server:\n"
        "                    destinations = jms_server.getDestinations()\n"
        "                for destination in destinations:\n"
        "                    name = jms_server.getName() + '/' + destination.getName()\n"
        "                    for metric, method_name in [('messagesCurrentCount','getMessagesCurrentCount'),('messagesPendingCount','getMessagesPendingCount'),('messagesReceivedCount','getMessagesReceivedCount'),('consumersCurrentCount','getConsumersCurrentCount'),('bytesCurrentCount','getBytesCurrentCount')]:\n"
        "                        emit_attr('jms', server, name, metric, destination, method_name)\n"
        "        except:\n"
        "            emit('jms', server, server, 'error', 'JMSRuntime unavailable')\n"
        "        try:\n"
        "            work_managers = runtime.getWorkManagerRuntimes()\n"
        "            for work_manager in work_managers:\n"
        "                name = work_manager.getName()\n"
        "                for metric, method_name in [('pendingRequests','getPendingRequests'),('completedRequests','getCompletedRequests'),('stuckThreadCount','getStuckThreadCount')]:\n"
        "                    emit_attr('workManager', server, name, metric, work_manager, method_name)\n"
        "        except:\n"
        "            emit('workManager', server, server, 'error', 'WorkManagerRuntimes unavailable')\n"
        "except:\n"
        "    pass\n"
        "exit()\n"
    )


def weblogic_profile_configured(environment):
    weblogic = effective_weblogic_settings(environment)
    admin_host = weblogic.get("adminHost") or {}
    products = environment.get("products") or {}
    return bool(
        products.get("weblogic")
        or products.get("oam")
        or products.get("oig")
        or products.get("soa")
        or weblogic.get("enabled")
        or str(weblogic.get("adminUrl") or "").strip()
        or str(weblogic.get("oracleHome") or "").strip()
        or str(admin_host.get("host") or "").strip()
    )


def collect_weblogic_server_processes(target, server_names, jstat_path=None, node_id="", node_name="", node_host=""):
    process_patterns = ["Dweblogic.Name={0}".format(name) for name in server_names] if server_names else []
    servers = []
    if not process_patterns:
        return servers

    process_result = run_target(
        target,
        "ps -eo pid=,nlwp=,rss=,args= | egrep {0} | grep -v grep".format(shlex.quote("|".join(process_patterns))),
    )

    for process_line in lines(process_result.get("output")):
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", process_line)
        if not match:
            continue

        pid = int(match.group(1))
        threads = int(match.group(2))
        rss_kb = int(match.group(3))
        arguments = match.group(4)
        name_match = re.search(r"-Dweblogic.Name=([^\s]+)", arguments)
        server_name = name_match.group(1) if name_match else "Unknown"

        heap = None
        if jstat_path:
            jstat_result = run_target(target, "{0} -gcutil {1} 2>/dev/null".format(shlex.quote(jstat_path), pid))
            if jstat_result.get("exit_code") == 0:
                heap = parse_jstat(jstat_result.get("output"))

        servers.append({
            "name": server_name,
            "nodeId": node_id or "",
            "nodeName": node_name or "",
            "nodeHost": node_host or (target or {}).get("host") or "",
            "pid": pid,
            "threads": threads,
            "rssMb": round(rss_kb / 1024.0, 1),
            "xmxMb": extract_xmx_mb(arguments),
            "heap": heap,
            "status": "running",
        })
    return servers


def get_weblogic_metrics(target, environment, opatch_future=None, keystore_future=None, progress=None):
    if not weblogic_profile_configured(environment):
        return None

    products = environment.get("products") or {}
    target = build_weblogic_target(environment, target)
    settings = effective_weblogic_settings(environment)
    jstat_path = settings.get("jstatPath")
    admin_url = str(settings.get("adminUrl") or "").strip()
    admin_username = str(settings.get("adminUsername") or "").strip()
    admin_password = str(settings.get("adminPassword") or "").strip()
    oracle_home = str(settings.get("oracleHome") or "").strip()
    domain_home = str(settings.get("domainHome") or "").strip()
    path_resolution = resolve_fmw_home_paths(target, oracle_home, domain_home, progress=progress)
    resolved_oracle_home = str(path_resolution.get("oracleHome") or "").strip()
    resolved_domain_home = str(path_resolution.get("domainHome") or "").strip()
    if resolved_oracle_home and resolved_oracle_home != oracle_home:
        oracle_home = resolved_oracle_home
        settings["oracleHome"] = oracle_home
    if resolved_domain_home and resolved_domain_home != domain_home:
        domain_home = resolved_domain_home
        settings["domainHome"] = domain_home
    domain_home_discovery = {
        "domainHome": domain_home,
        "source": path_resolution.get("domainHomeSource") if domain_home else "",
        "registryFile": "",
        "error": "",
        "command": "",
    }
    if oracle_home and (not domain_home or products.get("oam")):
        discovered_domain_home = discover_domain_home_from_oracle_home(target, oracle_home, progress=progress)
        discovered_value = str(discovered_domain_home.get("domainHome") or "").strip()
        if discovered_value:
            domain_home_discovery = discovered_domain_home
            domain_home = discovered_value
            settings["domainHome"] = domain_home
        elif not domain_home:
            domain_home_discovery = discovered_domain_home
    server_inventory = []
    server_inventory_error = None
    server_inventory_command = ""
    deployment_connect_url = normalize_weblogic_connect_url(admin_url)
    deployment_command = ""
    deployment_error = None
    deployments = []
    stuck_thread_command = ""
    stuck_thread_error = None
    stuck_threads = []
    jdbc_pool_command = ""
    jdbc_pool_error = None
    jdbc_pools = []
    runtime_metrics_command = ""
    runtime_metrics_error = None
    runtime_groups = {
        "jtaTransactions": [],
        "jmsDestinations": [],
        "jvmRuntime": [],
        "threadPoolRuntime": [],
        "jdbcHealth": [],
        "socketRuntime": [],
        "workManagers": [],
    }
    dms_metrics = {
        "deployments": [],
        "servers": [],
        "tableCount": 0,
        "tableInventory": [],
        "tables": [],
        "metrics": [],
        "error": "DMS collection has not run.",
    }
    configuration_error = None
    weblogic_ready = bool(oracle_home and admin_username and admin_password and deployment_connect_url)

    if weblogic_ready:
        wlst_path = "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home.rstrip("/"))
        if callable(progress):
            progress("Starting combined WLST runtime collection from ORACLE_HOME/oracle_common/common/bin/wlst.sh.")
        combined_script = build_weblogic_runtime_script(admin_username, admin_password, deployment_connect_url)
        combined_command, combined_result = run_wlst_script(target, wlst_path, combined_script, timeout=210)
        combined_output = str(combined_result.get("output") or "").strip()
        combined_error = combined_output or "Combined WLST runtime collection failed."
        server_inventory_command = combined_command
        deployment_command = combined_command
        stuck_thread_command = combined_command
        jdbc_pool_command = combined_command
        runtime_metrics_command = combined_command
        server_inventory = parse_weblogic_server_inventory(combined_output)
        deployments = parse_weblogic_deployments(combined_output)
        stuck_threads = parse_weblogic_stuck_threads(combined_output)
        jdbc_pools = parse_weblogic_jdbc_pools(combined_output)
        runtime_groups = parse_weblogic_runtime_groups(combined_output)
        runtime_row_count = sum(len(value) for value in runtime_groups.values())
        if server_inventory:
            if callable(progress):
                progress("WLST server inventory returned {0} server row(s).".format(len(server_inventory)))
        else:
            server_inventory_error = combined_error
            if callable(progress):
                progress("WLST server inventory returned no parsed rows.")
        if deployments:
            if callable(progress):
                progress("WLST deployment-state returned {0} deployment row(s).".format(len(deployments)))
        else:
            deployment_error = combined_error
            if callable(progress):
                progress("WLST deployment-state returned no parsed rows.")
        if stuck_threads:
            if callable(progress):
                progress("WLST stuck-thread collection returned {0} server row(s).".format(len(stuck_threads)))
        else:
            stuck_thread_error = combined_error
            if callable(progress):
                progress("WLST stuck-thread collection returned no parsed rows.")
        if jdbc_pools:
            if callable(progress):
                progress("WLST JDBC connection pool collection returned {0} data source row(s).".format(len(jdbc_pools)))
        else:
            jdbc_pool_error = combined_error
            if callable(progress):
                progress("WLST JDBC connection pool collection returned no parsed rows.")
        if runtime_row_count:
            if callable(progress):
                progress("WLST extended runtime collection returned {0} metric row group(s).".format(runtime_row_count))
        else:
            runtime_metrics_error = combined_error
            if callable(progress):
                progress("WLST extended runtime collection returned no parsed rows.")
        if combined_result.get("exit_code") != 0 and not (server_inventory or deployments or stuck_threads or jdbc_pools or runtime_row_count):
            configuration_error = combined_error

        dms_metrics = collect_dms_metrics(
            target,
            wlst_path,
            admin_username,
            admin_password,
            deployment_connect_url,
            progress=progress,
        )

    if False and weblogic_ready:
        wlst_path = "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home.rstrip("/"))
        if callable(progress):
            progress("Starting WLST server inventory collection from ORACLE_HOME/oracle_common/common/bin/wlst.sh.")
        inventory_script = (
            "from java.net import InetAddress\n"
            "connect('{0}','{1}','{2}')\n"
            "print('TYPE | SERVER | STATE | MACHINE | LISTEN_ADDRESS | IP | PORT | SSL_PORT | CLUSTER')\n"
            "domainRuntime()\n"
            "runtime_map = {{}}\n"
            "for r in domainRuntimeService.getServerRuntimes():\n"
            "    runtime_map[r.getName()] = r.getState()\n"
            "domainConfig()\n"
            "for server in cmo.getServers():\n"
            "    name = server.getName()\n"
            "    machine = 'None'\n"
            "    if server.getMachine():\n"
            "        machine = server.getMachine().getName()\n"
            "    listen_address = server.getListenAddress() or '0.0.0.0'\n"
            "    port = str(server.getListenPort())\n"
            "    ssl_port = 'None'\n"
            "    ssl = server.getSSL()\n"
            "    if ssl:\n"
            "        ssl_port = str(ssl.getListenPort())\n"
            "    cluster = 'None'\n"
            "    if server.getCluster():\n"
            "        cluster = server.getCluster().getName()\n"
            "    state = runtime_map.get(name, 'UNKNOWN')\n"
            "    server_type = 'MANAGED'\n"
            "    if name == 'AdminServer':\n"
            "        server_type = 'ADMIN'\n"
            "    ip_value = 'None'\n"
            "    try:\n"
            "        resolver = listen_address\n"
            "        if resolver in ('', '0.0.0.0', '::') and server.getMachine() and server.getMachine().getNodeManager():\n"
            "            resolver = server.getMachine().getNodeManager().getListenAddress() or resolver\n"
            "        if resolver and resolver not in ('0.0.0.0', '::'):\n"
            "            ip_value = InetAddress.getByName(resolver).getHostAddress()\n"
            "    except:\n"
            "        pass\n"
            "    print(server_type + ' | ' + name + ' | ' + state + ' | ' + machine + ' | ' + listen_address + ' | ' + ip_value + ' | ' + port + ' | ' + ssl_port + ' | ' + cluster)\n"
            "exit()\n"
        ).format(
            python_string_literal(admin_username),
            python_string_literal(admin_password),
            python_string_literal(deployment_connect_url),
        )
        server_inventory_command, inventory_result = run_wlst_script(target, wlst_path, inventory_script)
        server_inventory = parse_weblogic_server_inventory(inventory_result.get("output"))
        inventory_output = str(inventory_result.get("output") or "").strip()
        if server_inventory:
            if callable(progress):
                progress("WLST server inventory returned {0} server row(s).".format(len(server_inventory)))
        else:
            server_inventory_error = inventory_output or "WLST server inventory command failed."
            if callable(progress):
                progress("WLST server inventory returned no parsed rows.")

        if callable(progress):
            progress("Starting WLST deployment-state collection from ORACLE_HOME/oracle_common/common/bin/wlst.sh.")
        deployment_script = (
            "connect('{0}','{1}','{2}')\n"
            "domainRuntime()\n"
            "cd('/AppRuntimeStateRuntime/AppRuntimeStateRuntime')\n"
            "apps = cmo.getApplicationIds()\n"
            "print('\\n===== Application Deployment Status =====\\n')\n"
            "for app in apps:\n"
            "    print(app + ' : ' + cmo.getCurrentState(app, 'AdminServer'))\n"
            "print('\\n=========================================\\n')\n"
            "exit()\n"
        ).format(
            python_string_literal(admin_username),
            python_string_literal(admin_password),
            python_string_literal(deployment_connect_url),
        )
        deployment_command, deployment_result = run_wlst_script(target, wlst_path, deployment_script)
        deployments = parse_weblogic_deployments(deployment_result.get("output"))
        deployment_output = str(deployment_result.get("output") or "").strip()
        if deployments:
            if callable(progress):
                progress("WLST deployment-state returned {0} deployment row(s).".format(len(deployments)))
        else:
            deployment_error = deployment_output or "WLST deployment status command failed."
            if callable(progress):
                progress("WLST deployment-state returned no parsed rows.")

        if callable(progress):
            progress("Starting WLST stuck-thread collection from ORACLE_HOME/oracle_common/common/bin/wlst.sh.")
        stuck_thread_script = (
            "connect('{0}','{1}','{2}')\n"
            "domainRuntime()\n"
            "print('SERVER | STUCK_THREADS | HOGGING_THREADS')\n"
            "cd('/ServerRuntimes')\n"
            "for name in ls(returnMap='true'):\n"
            "    try:\n"
            "        cd('/ServerRuntimes/' + name + '/ThreadPoolRuntime/ThreadPoolRuntime')\n"
            "        stuck = cmo.getStuckThreadCount()\n"
            "        hogging = cmo.getHoggingThreadCount()\n"
            "        print(name + ' | ' + str(stuck) + ' | ' + str(hogging))\n"
            "    except:\n"
            "        print(name + ' | ERROR | ERROR')\n"
            "    cd('/ServerRuntimes')\n"
            "exit()\n"
        ).format(
            python_string_literal(admin_username),
            python_string_literal(admin_password),
            python_string_literal(deployment_connect_url),
        )
        stuck_thread_command, stuck_thread_result = run_wlst_script(target, wlst_path, stuck_thread_script)
        stuck_threads = parse_weblogic_stuck_threads(stuck_thread_result.get("output"))
        stuck_thread_output = str(stuck_thread_result.get("output") or "").strip()
        if stuck_threads:
            if callable(progress):
                progress("WLST stuck-thread collection returned {0} server row(s).".format(len(stuck_threads)))
        else:
            stuck_thread_error = stuck_thread_output or "WLST stuck-thread command failed."
            if callable(progress):
                progress("WLST stuck-thread collection returned no parsed rows.")

        if callable(progress):
            progress("Starting WLST JDBC connection pool collection from ORACLE_HOME/oracle_common/common/bin/wlst.sh.")
        jdbc_pool_script = (
            "connect('{0}','{1}','{2}')\n"
            "domainRuntime()\n"
            "print('SERVER | DATASOURCE | ACTIVE | WAITING')\n"
            "for runtime in domainRuntimeService.getServerRuntimes():\n"
            "    server_name = runtime.getName()\n"
            "    try:\n"
            "        jdbc_service = runtime.getJDBCServiceRuntime()\n"
            "        data_sources = jdbc_service.getJDBCDataSourceRuntimeMBeans() if jdbc_service else []\n"
            "        for data_source in data_sources:\n"
            "            print(server_name + ' | ' + data_source.getName() + ' | ' + str(data_source.getActiveConnectionsCurrentCount()) + ' | ' + str(data_source.getWaitingForConnectionCurrentCount()))\n"
            "    except:\n"
            "        pass\n"
            "exit()\n"
        ).format(
            python_string_literal(admin_username),
            python_string_literal(admin_password),
            python_string_literal(deployment_connect_url),
        )
        jdbc_pool_command, jdbc_pool_result = run_wlst_script(target, wlst_path, jdbc_pool_script)
        jdbc_pools = parse_weblogic_jdbc_pools(jdbc_pool_result.get("output"))
        jdbc_pool_output = str(jdbc_pool_result.get("output") or "").strip()
        if jdbc_pools:
            if callable(progress):
                progress("WLST JDBC connection pool collection returned {0} data source row(s).".format(len(jdbc_pools)))
        else:
            jdbc_pool_error = jdbc_pool_output or "WLST JDBC connection pool command failed."
            if callable(progress):
                progress("WLST JDBC connection pool collection returned no parsed rows.")

        if callable(progress):
            progress("Starting WLST extended runtime collection for JTA, JMS, JVM, thread pool, JDBC health, sockets, and WorkManagers.")
        runtime_metrics_script = (
            "connect('{0}','{1}','{2}')\n"
            "domainRuntime()\n"
            "print('SECTION | SERVER | NAME | METRIC | VALUE')\n"
            "def clean(value):\n"
            "    if value is None:\n"
            "        return ''\n"
            "    return str(value).replace('|', '/').replace('\\n', ' ').replace('\\r', ' ')\n"
            "def emit(section, server, name, metric, value):\n"
            "    print(section + ' | ' + clean(server) + ' | ' + clean(name) + ' | ' + clean(metric) + ' | ' + clean(value))\n"
            "def emit_attr(section, server, name, metric, bean, method_name):\n"
            "    try:\n"
            "        method = getattr(bean, method_name)\n"
            "        emit(section, server, name, metric, method())\n"
            "    except:\n"
            "        emit(section, server, name, metric, 'ERROR')\n"
            "for runtime in domainRuntimeService.getServerRuntimes():\n"
            "    server = runtime.getName()\n"
            "    try:\n"
            "        jta = runtime.getJTARuntime()\n"
            "        for metric, method_name in [('activeTransactionsTotalCount','getActiveTransactionsTotalCount'),('transactionTotalCount','getTransactionTotalCount'),('committedTotalCount','getCommittedTotalCount'),('rolledBackTotalCount','getRolledBackTotalCount'),('heuristicsTotalCount','getHeuristicsTotalCount'),('secondsActiveTotalCount','getSecondsActiveTotalCount')]:\n"
            "            emit_attr('jta', server, server, metric, jta, method_name)\n"
            "    except:\n"
            "        emit('jta', server, server, 'error', 'JTARuntime unavailable')\n"
            "    try:\n"
            "        jvm = runtime.getJVMRuntime()\n"
            "        for metric, method_name in [('heapFreeCurrent','getHeapFreeCurrent'),('heapSizeCurrent','getHeapSizeCurrent'),('heapFreePercent','getHeapFreePercent'),('javaVersion','getJavaVersion'),('uptime','getUptime')]:\n"
            "            emit_attr('jvm', server, server, metric, jvm, method_name)\n"
            "    except:\n"
            "        emit('jvm', server, server, 'error', 'JVMRuntime unavailable')\n"
            "    try:\n"
            "        thread_pool = runtime.getThreadPoolRuntime()\n"
            "        for metric, method_name in [('executeThreadTotalCount','getExecuteThreadTotalCount'),('executeThreadIdleCount','getExecuteThreadIdleCount'),('pendingUserRequestCount','getPendingUserRequestCount'),('queueLength','getQueueLength'),('hoggingThreadCount','getHoggingThreadCount'),('stuckThreadCount','getStuckThreadCount'),('throughput','getThroughput')]:\n"
            "            emit_attr('threadPool', server, server, metric, thread_pool, method_name)\n"
            "    except:\n"
            "        emit('threadPool', server, server, 'error', 'ThreadPoolRuntime unavailable')\n"
            "    try:\n"
            "        socket_runtime = runtime.getSocketRuntime()\n"
            "        for metric, method_name in [('socketsOpenedTotalCount','getSocketsOpenedTotalCount'),('socketsClosedTotalCount','getSocketsClosedTotalCount'),('currentOpenSocketCount','getCurrentOpenSocketCount')]:\n"
            "            emit_attr('socket', server, server, metric, socket_runtime, method_name)\n"
            "    except:\n"
            "        emit('socket', server, server, 'error', 'SocketRuntime unavailable')\n"
            "    try:\n"
            "        jdbc_service = runtime.getJDBCServiceRuntime()\n"
            "        data_sources = jdbc_service.getJDBCDataSourceRuntimeMBeans() if jdbc_service else []\n"
            "        for data_source in data_sources:\n"
            "            name = data_source.getName()\n"
            "            for metric, method_name in [('failuresToReconnectCount','getFailuresToReconnectCount'),('leakedConnectionCount','getLeakedConnectionCount'),('currCapacity','getCurrCapacity'),('state','getState'),('activeConnectionsCurrentCount','getActiveConnectionsCurrentCount'),('waitingForConnectionCurrentCount','getWaitingForConnectionCurrentCount')]:\n"
            "                emit_attr('jdbcHealth', server, name, metric, data_source, method_name)\n"
            "    except:\n"
            "        emit('jdbcHealth', server, server, 'error', 'JDBCServiceRuntime unavailable')\n"
            "    try:\n"
            "        jms_runtime = runtime.getJMSRuntime()\n"
            "        jms_servers = jms_runtime.getJMSServers() if jms_runtime else []\n"
            "        for jms_server in jms_servers:\n"
            "            destinations = jms_server.getDestinations() if jms_server else []\n"
            "            for destination in destinations:\n"
            "                name = jms_server.getName() + '/' + destination.getName()\n"
            "                for metric, method_name in [('messagesCurrentCount','getMessagesCurrentCount'),('messagesPendingCount','getMessagesPendingCount'),('messagesReceivedCount','getMessagesReceivedCount'),('consumersCurrentCount','getConsumersCurrentCount'),('bytesCurrentCount','getBytesCurrentCount')]:\n"
            "                    emit_attr('jms', server, name, metric, destination, method_name)\n"
            "    except:\n"
            "        emit('jms', server, server, 'error', 'JMSRuntime unavailable')\n"
            "    try:\n"
            "        work_managers = runtime.getWorkManagerRuntimes()\n"
            "        for work_manager in work_managers:\n"
            "            name = work_manager.getName()\n"
            "            for metric, method_name in [('pendingRequests','getPendingRequests'),('completedRequests','getCompletedRequests'),('stuckThreadCount','getStuckThreadCount')]:\n"
            "                emit_attr('workManager', server, name, metric, work_manager, method_name)\n"
            "    except:\n"
            "        emit('workManager', server, server, 'error', 'WorkManagerRuntimes unavailable')\n"
            "exit()\n"
        ).format(
            python_string_literal(admin_username),
            python_string_literal(admin_password),
            python_string_literal(deployment_connect_url),
        )
        runtime_metrics_command, runtime_metrics_result = run_wlst_script(target, wlst_path, runtime_metrics_script, timeout=240)
        runtime_groups = parse_weblogic_runtime_groups(runtime_metrics_result.get("output"))
        runtime_metrics_output = str(runtime_metrics_result.get("output") or "").strip()
        runtime_row_count = sum(len(value) for value in runtime_groups.values())
        if runtime_row_count:
            if callable(progress):
                progress("WLST extended runtime collection returned {0} metric row group(s).".format(runtime_row_count))
        else:
            runtime_metrics_error = runtime_metrics_output or "WLST extended runtime collection failed."
            if callable(progress):
                progress("WLST extended runtime collection returned no parsed rows.")
    if not weblogic_ready:
        missing = []
        if not oracle_home:
            missing.append("ORACLE_HOME")
        if not admin_url:
            missing.append("WebLogic Admin URL")
        if not admin_username:
            missing.append("WebLogic Admin Username")
        if not admin_password:
            missing.append("WebLogic Admin Password")
        if missing:
            missing_text = "Missing WebLogic deployment settings: {0}.".format(", ".join(missing))
            server_inventory_error = missing_text
            deployment_error = missing_text
            stuck_thread_error = missing_text
            jdbc_pool_error = missing_text
            runtime_metrics_error = missing_text
            configuration_error = missing_text

    server_names = [item.get("name") for item in server_inventory if item.get("name")]
    servers = []
    weblogic_cluster_targets = build_discovered_weblogic_cluster_targets(
        environment,
        target,
        {"serverInventory": server_inventory},
    )
    if weblogic_cluster_targets:
        process_workers = max(1, min(6, len(weblogic_cluster_targets)))
        with ThreadPoolExecutor(max_workers=process_workers) as process_executor:
            process_futures = [
                process_executor.submit(
                    collect_weblogic_server_processes,
                    node.get("target") or target,
                    server_names,
                    jstat_path,
                    node.get("id") or "",
                    node.get("name") or "",
                    node.get("host") or "",
                )
                for node in weblogic_cluster_targets
            ]
            for future in process_futures:
                try:
                    servers.extend(future.result())
                except Exception:
                    pass

    order_map = dict((name, index) for index, name in enumerate(server_names))
    servers.sort(key=lambda item: order_map.get(item.get("name"), 999))

    found_names = [item.get("name") for item in servers]
    missing_servers = [name for name in server_names if name not in found_names]
    active_deployments = [item for item in deployments if item.get("state") == "STATE_ACTIVE"]
    inactive_deployments = [item for item in deployments if item.get("state") != "STATE_ACTIVE"]
    total_stuck_threads = sum(item.get("stuckThreads") or 0 for item in stuck_threads)
    total_hogging_threads = sum(item.get("hoggingThreads") or 0 for item in stuck_threads)
    servers_with_stuck_threads = [item.get("server") for item in stuck_threads if (item.get("stuckThreads") or 0) > 0]
    servers_with_hogging_threads = [item.get("server") for item in stuck_threads if (item.get("hoggingThreads") or 0) > 0]
    total_active_connections = sum(item.get("activeConnections") or 0 for item in jdbc_pools)
    total_waiting_connections = sum(item.get("waitingConnections") or 0 for item in jdbc_pools)

    def metric_number(row, key):
        value = (row or {}).get(key)
        if isinstance(value, (int, float)):
            return value
        text_value = str(value or "").strip()
        if re.fullmatch(r"-?\d+", text_value):
            return int(text_value)
        if re.fullmatch(r"-?\d+\.\d+", text_value):
            return float(text_value)
        return 0

    jta_transactions = runtime_groups.get("jtaTransactions") or []
    jms_destinations = runtime_groups.get("jmsDestinations") or []
    jvm_runtime = runtime_groups.get("jvmRuntime") or []
    thread_pool_runtime = runtime_groups.get("threadPoolRuntime") or []
    jdbc_health = runtime_groups.get("jdbcHealth") or []
    socket_runtime = runtime_groups.get("socketRuntime") or []
    work_managers = runtime_groups.get("workManagers") or []
    heap_used_percent_values = [
        max(0, 100 - metric_number(item, "heapFreePercent"))
        for item in jvm_runtime
        if "heapFreePercent" in item
    ]
    critical_widgets = {
        "heapUtilizationPercent": round(max(heap_used_percent_values), 1) if heap_used_percent_values else None,
        "pendingExecuteRequests": sum(metric_number(item, "pendingUserRequestCount") for item in thread_pool_runtime),
        "activeJtaTransactions": sum(metric_number(item, "activeTransactionsTotalCount") for item in jta_transactions),
        "jmsPendingMessages": sum(metric_number(item, "messagesPendingCount") for item in jms_destinations),
        "jdbcWaiters": sum(metric_number(item, "waitingForConnectionCurrentCount") for item in jdbc_health) or total_waiting_connections,
        "inactiveDeployments": len(inactive_deployments),
        "currentOpenSockets": sum(metric_number(item, "currentOpenSocketCount") for item in socket_runtime),
        "workManagerPendingRequests": sum(metric_number(item, "pendingRequests") for item in work_managers),
        "executeQueueLength": sum(metric_number(item, "queueLength") for item in thread_pool_runtime),
        "jvmUptime": max([metric_number(item, "uptime") for item in jvm_runtime] or [0]),
    }

    if opatch_future is not None:
        try:
            opatch = opatch_future.result()
        except Exception as exc:
            opatch = {"error": str(exc), "versions": [], "products": [], "patches": []}
    else:
        opatch = collect_opatch_inventory(target, oracle_home, progress=progress, label="WebLogic")
    opatch["recommendation"] = build_fmw_patch_recommendation(opatch, environment, oracle_home)
    opatch["patchComparisonRows"] = opatch["recommendation"].get("comparisonRows", [])
    certificates, certificate_error = collect_ssl_certificates(
        target,
        server_inventory,
        oracle_home=oracle_home,
        domain_home=domain_home,
        progress=progress,
        keystore_future=keystore_future,
    )
    oracle_home_nodes = [{
        "id": "node1",
        "name": "Node 1",
        "role": "AdminServer host",
        "host": target.get("host") or "",
        "port": target.get("port") or 22,
        "username": target.get("username") or "",
        "sshMode": target.get("sshMode") or "user_password",
        "oracleHome": oracle_home,
        "domainHome": domain_home,
        "opatch": opatch,
        "certificates": certificates,
        "certificateError": certificate_error,
    }]

    def collect_node_oracle_home_details(node):
        node_oracle_home = str(node.get("oracleHome") or oracle_home or "").strip()
        node_domain_home = str(node.get("domainHome") or domain_home or "").strip()
        node_target = node.get("target") or target
        with ThreadPoolExecutor(max_workers=2) as node_executor:
            node_opatch_future = node_executor.submit(
                collect_opatch_inventory,
                node_target,
                node_oracle_home,
                progress,
                "WebLogic {0}".format(node.get("name") or "cluster node"),
            )
            node_cert_future = node_executor.submit(
                collect_ssl_certificates,
                node_target,
                server_inventory,
                node_oracle_home,
                node_domain_home,
                progress,
            )
            try:
                node_opatch = node_opatch_future.result()
            except Exception as exc:
                node_opatch = {"error": str(exc), "versions": [], "products": [], "patches": []}
            try:
                node_certificates, node_certificate_error = node_cert_future.result()
            except Exception as exc:
                node_certificates, node_certificate_error = [], str(exc)
        node_opatch["recommendation"] = build_fmw_patch_recommendation(node_opatch, environment, node_oracle_home)
        node_opatch["patchComparisonRows"] = node_opatch["recommendation"].get("comparisonRows", [])
        return {
            "id": node.get("id") or "",
            "name": node.get("name") or "",
            "role": node.get("role") or "",
            "host": node.get("host") or "",
            "port": node.get("port") or (node_target or {}).get("port") or 22,
            "username": node.get("username") or (node_target or {}).get("username") or "",
            "sshMode": node.get("sshMode") or (node_target or {}).get("sshMode") or "user_password",
            "oracleHome": node_oracle_home,
            "domainHome": node_domain_home,
            "opatch": node_opatch,
            "certificates": node_certificates,
            "certificateError": node_certificate_error,
        }

    extra_oracle_home_nodes = weblogic_cluster_targets[1:]
    if extra_oracle_home_nodes:
        node_workers = max(1, min(4, len(extra_oracle_home_nodes)))
        with ThreadPoolExecutor(max_workers=node_workers) as node_executor:
            node_futures = [node_executor.submit(collect_node_oracle_home_details, node) for node in extra_oracle_home_nodes]
            for future in node_futures:
                try:
                    oracle_home_nodes.append(future.result())
                except Exception:
                    pass

    return {
        "error": configuration_error,
        "oracleHome": oracle_home,
        "oracleHomeSource": path_resolution.get("oracleHomeSource") or ("saved profile" if oracle_home else ""),
        "pathResolutionWarning": path_resolution.get("warning") or "",
        "pathResolutionCommand": path_resolution.get("command") or "",
        "domainHome": domain_home,
        "domainHomeSource": domain_home_discovery.get("source") or ("saved profile" if domain_home else ""),
        "domainRegistryFile": domain_home_discovery.get("registryFile") or "",
        "domainHomeDiscoveryError": domain_home_discovery.get("error") or "",
        "domainHomeDiscoveryCommand": domain_home_discovery.get("command") or "",
        "expectedServers": server_names,
        "runningServers": len(servers),
        "missingServers": missing_servers,
        "servers": servers,
        "serverInventory": server_inventory,
        "serverInventoryCount": len(server_inventory),
        "serverInventoryCommand": "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home.rstrip("/")) if oracle_home else "",
        "serverInventoryError": server_inventory_error,
        "deployments": deployments,
        "deploymentCommand": "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home.rstrip("/")) if oracle_home else "",
        "deploymentConnectUrl": deployment_connect_url,
        "deploymentError": deployment_error,
        "deploymentCount": len(deployments),
        "activeDeploymentCount": len(active_deployments),
        "inactiveDeploymentCount": len(inactive_deployments),
        "stuckThreads": stuck_threads,
        "stuckThreadCommand": "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home.rstrip("/")) if oracle_home else "",
        "stuckThreadError": stuck_thread_error,
        "stuckThreadCount": total_stuck_threads,
        "hoggingThreadCount": total_hogging_threads,
        "serversWithStuckThreads": servers_with_stuck_threads,
        "serversWithHoggingThreads": servers_with_hogging_threads,
        "jdbcPools": jdbc_pools,
        "jdbcPoolCommand": "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home.rstrip("/")) if oracle_home else "",
        "jdbcPoolError": jdbc_pool_error,
        "jdbcPoolCount": len(jdbc_pools),
        "jdbcActiveConnectionCount": total_active_connections,
        "jdbcWaitingConnectionCount": total_waiting_connections,
        "runtimeMetricsCommand": "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home.rstrip("/")) if oracle_home else "",
        "runtimeMetricsError": runtime_metrics_error,
        "jtaTransactions": jta_transactions,
        "jmsDestinations": jms_destinations,
        "jvmRuntime": jvm_runtime,
        "threadPoolRuntime": thread_pool_runtime,
        "jdbcHealth": jdbc_health,
        "socketRuntime": socket_runtime,
        "workManagers": work_managers,
        "dms": dms_metrics,
        "criticalWidgets": critical_widgets,
        "opatch": opatch,
        "certificates": certificates,
        "certificateError": certificate_error,
        "oracleHomeNodes": oracle_home_nodes,
    }


def normalize_oud_instance_profiles(settings):
    settings = settings or {}
    profiles = []
    for index, item in enumerate(settings.get("instances") or []):
        row = item or {}
        instance_home = str(row.get("instanceHome") or row.get("path") or "").strip()
        name = str(row.get("name") or row.get("label") or "OUD Instance {0}".format(index + 1)).strip()
        if not instance_home:
            continue
        profiles.append({
            "name": name or "OUD Instance {0}".format(index + 1),
            "instanceHome": instance_home,
            "bindDn": row.get("bindDn") or settings.get("bindDn") or "cn=Directory Manager",
            "bindPassword": row.get("bindPassword") or settings.get("bindPassword") or "",
            "ldapPort": row.get("ldapPort") or settings.get("ldapPort") or 1389,
            "adminPort": row.get("adminPort") or settings.get("adminPort") or 4444,
        })
    if not profiles and str(settings.get("instanceHome") or "").strip():
        profiles.append({
            "name": "OUD Instance 1",
            "instanceHome": str(settings.get("instanceHome") or "").strip(),
            "bindDn": settings.get("bindDn") or "cn=Directory Manager",
            "bindPassword": settings.get("bindPassword") or "",
            "ldapPort": settings.get("ldapPort") or 1389,
            "adminPort": settings.get("adminPort") or 4444,
        })
    return profiles


def collect_single_oud_metrics(target, environment, instance_profile=None):
    if not (environment.get("products") or {}).get("oud"):
        return None

    settings = dict(environment.get("oud") or {})
    if instance_profile:
        settings["instanceHome"] = instance_profile.get("instanceHome") or settings.get("instanceHome")
        settings["bindDn"] = instance_profile.get("bindDn") or settings.get("bindDn")
        settings["bindPassword"] = instance_profile.get("bindPassword") or settings.get("bindPassword")
        settings["ldapPort"] = instance_profile.get("ldapPort") or settings.get("ldapPort")
        settings["adminPort"] = instance_profile.get("adminPort") or settings.get("adminPort")
    instance_home = str(settings.get("instanceHome") or "").strip()
    instance_name = str((instance_profile or {}).get("name") or settings.get("name") or "OUD Instance").strip()
    bind_dn = settings.get("bindDn")
    bind_password = settings.get("bindPassword")
    status_path = str(settings.get("statusPath") or "").strip()
    bin_directory = "{0}/bin".format(instance_home.rstrip("/")) if instance_home else ""
    if not bin_directory and status_path and "/" in status_path:
        bin_directory = status_path.rsplit("/", 1)[0]
    if not bin_directory:
        return {
            "error": "OUD_INSTANCE_HOME Path is not configured.",
            "instanceName": instance_name,
            "instanceHome": instance_home,
            "ldapPort": settings.get("ldapPort") or 1389,
            "adminPort": settings.get("adminPort") or 4444,
            "listeners": [],
            "backends": [],
            "commands": [],
        }

    status_command = 'cd {0} && pwfile=$(mktemp ./pwd.XXXXXX.txt) && trap \'rm -f "$pwfile"\' EXIT && printf %s {1} > "$pwfile" && chmod 600 "$pwfile" && ./status -D {2} -j "$pwfile"'.format(
        shlex.quote(bin_directory),
        shlex.quote(str(bind_password or "")),
        shlex.quote(str(bind_dn or "")),
    )
    replication_command = 'cd {0} && pwfile=$(mktemp ./pwd.XXXXXX.txt) && trap \'rm -f "$pwfile"\' EXIT && printf %s {1} > "$pwfile" && chmod 600 "$pwfile" && export COLUMNS=240 && ./dsreplication status -D {2} -j "$pwfile" -X --Advanced -n'.format(
        shlex.quote(bin_directory),
        shlex.quote(str(bind_password or "")),
        shlex.quote(str(bind_dn or "")),
    )
    build_command = "cd {0} && ./start-ds -F".format(shlex.quote(bin_directory))
    system_command = "cd {0} && ./start-ds -s".format(shlex.quote(bin_directory))

    errors = []
    status_output = ""
    replication_output = ""
    if bind_dn and bind_password:
        status_result = run_target(target, status_command)
        status_output = status_result.get("output")
        if status_result.get("exit_code") != 0:
            errors.append("status command failed: {0}".format(status_output or "unknown error"))
        replication_result = run_target(target, replication_command)
        replication_output = replication_result.get("output")
        if replication_result.get("exit_code") != 0:
            errors.append("dsreplication status failed: {0}".format(replication_output or "unknown error"))
    else:
        errors.append("OUD root user or password is not configured.")

    build_result = run_target(target, build_command)
    if build_result.get("exit_code") != 0:
        errors.append("start-ds -F failed: {0}".format(build_result.get("output") or "unknown error"))

    system_result = run_target(target, system_command)
    if system_result.get("exit_code") != 0:
        errors.append("start-ds -s failed: {0}".format(system_result.get("output") or "unknown error"))
    parsed = parse_oud_status(status_output)
    replication_sections = parse_oud_replication(replication_output)
    build_info = parse_key_value_banner_output(build_result.get("output"))
    system_info = parse_key_value_banner_output(system_result.get("output"))
    summary = parsed.get("summary") or {}
    backends = parsed.get("backends") or []
    admin_connector = summary.get("Administration Connector") or summary.get("Administration Connector Port")
    _, inferred_admin_port = parse_listener_address_port(admin_connector)
    inferred_admin_port = normalize_ssl_port(inferred_admin_port)
    if not inferred_admin_port:
        for listener in parsed.get("listeners") or []:
            protocol = str((listener or {}).get("protocol") or "").strip().upper()
            address = str((listener or {}).get("addressPort") or "")
            if "ADMIN" not in protocol and "ADMIN" not in address.upper():
                continue
            _, inferred_admin_port = parse_listener_address_port(address)
            inferred_admin_port = normalize_ssl_port(inferred_admin_port)
            if inferred_admin_port:
                break
    if inferred_admin_port:
        settings["adminPort"] = inferred_admin_port
    monitor_payload = collect_oud_monitoring_and_policies(
        target,
        settings,
        bin_directory,
        instance_name,
        bind_dn,
        bind_password,
    )
    open_connections = summary.get("Open Connections")
    system_values = system_info.get("values") or {}
    build_values = build_info.get("values") or {}
    db_directory = resolve_existing_directory(target, [
        "{0}/db".format(instance_home.rstrip("/")) if instance_home else "",
    ])
    changelog_db_directory = resolve_existing_directory(target, [
        "{0}/changelogDB".format(instance_home.rstrip("/")) if instance_home else "",
        "{0}/changelogDb".format(instance_home.rstrip("/")) if instance_home else "",
    ])
    schema_directory = resolve_existing_directory(target, [
        "{0}/config/schema".format(instance_home.rstrip("/")) if instance_home else "",
        "{0}/cnfig/schema".format(instance_home.rstrip("/")) if instance_home else "",
    ])
    db_size = directory_size_label(target, db_directory)
    changelog_db_size = directory_size_label(target, changelog_db_directory)
    custom_schema_files = schema_match_files(target, schema_directory)
    replication_nodes = []
    for section in replication_sections:
        for server_row in section.get("servers") or []:
            row = dict(server_row)
            row["baseDn"] = section.get("baseDn")
            row["replicationEnabled"] = bool(section.get("enabled"))
            replication_nodes.append(row)
    unique_replication_servers = sorted({
        str(row.get("server") or "").strip()
        for row in replication_nodes
        if str(row.get("server") or "").strip()
    })
    replication_enabled_sections = [section for section in replication_sections if section.get("enabled")]
    replication_disabled_sections = [section for section in replication_sections if not section.get("enabled")]

    def pretty_memory(key):
        raw_value = system_values.get(key)
        numeric = threshold_value(raw_value)
        return humanize_bytes(numeric) if numeric is not None else raw_value

    return {
        "error": "; ".join(errors) if errors else None,
        "instanceName": instance_name,
        "instanceHome": instance_home,
        "ldapPort": settings.get("ldapPort") or 1389,
        "adminPort": settings.get("adminPort") or 4444,
        "binDirectory": bin_directory,
        "commands": [
            {"label": "OUD Status", "command": 'cd {0} && ./status -D "{1}" -j <temporary password file>'.format(bin_directory, bind_dn or "cn=Directory Manager")},
            {"label": "Replication Status", "command": 'cd {0} && ./dsreplication status -D "{1}" -j <temporary password file> -X --Advanced -n'.format(bin_directory, bind_dn or "cn=Directory Manager")},
            {"label": "Build Version", "command": "cd {0} && ./start-ds -F".format(bin_directory)},
            {"label": "Runtime System", "command": "cd {0} && ./start-ds -s".format(bin_directory)},
        ] + (monitor_payload.get("commands") or []),
        "serverRunStatus": summary.get("Server Run Status"),
        "openConnections": int(open_connections) if open_connections and str(open_connections).isdigit() else open_connections,
        "hostName": summary.get("Host Name") or system_values.get("System Name"),
        "administrativeUsers": summary.get("Administrative Users") or bind_dn,
        "installationPath": summary.get("Installation Path") or system_values.get("Installation Directory"),
        "instancePath": summary.get("Instance Path") or system_values.get("Instance Directory") or instance_home,
        "dbSize": db_size,
        "changelogDbSize": changelog_db_size,
        "customSchemaPresent": bool(custom_schema_files),
        "customSchemaFiles": custom_schema_files,
        "customSchemaCount": len(custom_schema_files),
        "customSchemaDirectory": schema_directory or None,
        "version": summary.get("Version") or build_info.get("banner") or system_info.get("banner"),
        "javaVersion": summary.get("Java Version") or system_values.get("JAVA Version") or build_values.get("Build Java Version"),
        "administrationConnector": summary.get("Administration Connector"),
        "listeners": parsed.get("listeners") or [],
        "backends": backends,
        "namingContexts": [item.get("baseDn") for item in backends if item.get("baseDn")],
        "replicationSections": replication_sections,
        "replicationNodes": replication_nodes,
        "replicationEnabledCount": len(replication_enabled_sections),
        "replicationDisabledCount": len(replication_disabled_sections),
        "replicationNodeCount": len(unique_replication_servers),
        "replicationNodeNames": unique_replication_servers,
        "replicationConflictCount": sum(
            int(row.get("conflicts") or 0)
            for row in replication_nodes
            if str(row.get("conflicts") or "").isdigit()
        ),
        "replicationNormalCount": len([
            row for row in replication_nodes
            if str(row.get("status") or "").strip().lower() == "normal"
        ]),
        "buildId": build_values.get("Build ID"),
        "majorVersion": build_values.get("Major Version"),
        "maintenanceVersion": build_values.get("Maintenance Version"),
        "releaseVersion": build_values.get("Release Version"),
        "componentVersion": build_values.get("Component Version"),
        "platformVersion": build_values.get("Platform Version"),
        "patchVersion": build_values.get("Patch Version"),
        "labelIdentifier": build_values.get("Label Identifier"),
        "debugBuild": build_values.get("Debug Build"),
        "buildOs": build_values.get("Build OS"),
        "buildUser": build_values.get("Build User"),
        "buildJavaVersion": build_values.get("Build Java Version"),
        "buildJavaVendor": build_values.get("Build Java Vendor"),
        "buildJvmVersion": build_values.get("Build JVM Version"),
        "buildJvmVendor": build_values.get("Build JVM Vendor"),
        "jeVersion": system_values.get("JE Version"),
        "javaHome": system_values.get("JAVA Home"),
        "classPath": system_values.get("Class Path"),
        "operatingSystem": system_values.get("Operating System"),
        "jvmArchitecture": system_values.get("JVM Architecture"),
        "systemName": system_values.get("System Name"),
        "availableProcessors": system_values.get("Available Processors"),
        "maxAvailableMemory": pretty_memory("Max Available Memory"),
        "currentlyUsedMemory": pretty_memory("Currently Used Memory"),
        "currentlyFreeMemory": pretty_memory("Currently Free Memory"),
        "monitorEntries": monitor_payload.get("monitorEntries") or [],
        "monitorError": monitor_payload.get("monitorError"),
        "passwordPolicies": monitor_payload.get("passwordPolicies") or [],
        "passwordPolicyError": monitor_payload.get("passwordPolicyError"),
    }


def get_oud_metrics(target, environment, progress=None):
    if not (environment.get("products") or {}).get("oud"):
        return None

    settings = environment.get("oud") or {}
    profiles = normalize_oud_instance_profiles(settings)
    if len(profiles) <= 1:
        result = collect_single_oud_metrics(target, environment, profiles[0] if profiles else None)
        return enrich_oud_shared_metrics(target, environment, result, progress=progress)

    instance_results = []
    for index, profile in enumerate(profiles):
        scoped_environment = dict(environment)
        scoped_settings = dict(settings)
        scoped_settings.update({
            "instanceHome": profile.get("instanceHome") or "",
            "bindDn": profile.get("bindDn") or settings.get("bindDn") or "cn=Directory Manager",
            "bindPassword": profile.get("bindPassword") or settings.get("bindPassword") or "",
            "ldapPort": profile.get("ldapPort") or settings.get("ldapPort") or 1389,
            "adminPort": profile.get("adminPort") or settings.get("adminPort") or 4444,
            "instances": [],
        })
        scoped_environment["oud"] = scoped_settings
        result = collect_single_oud_metrics(target, scoped_environment, profile) or {}
        result["instanceName"] = profile.get("name") or "OUD Instance {0}".format(index + 1)
        result["instanceHome"] = profile.get("instanceHome") or result.get("instanceHome") or ""
        result["ldapPort"] = profile.get("ldapPort") or result.get("ldapPort") or 1389
        result["adminPort"] = profile.get("adminPort") or result.get("adminPort") or 4444
        instance_results.append(result)

    if not instance_results:
        result = collect_single_oud_metrics(target, environment, None)
        return enrich_oud_shared_metrics(target, environment, result, progress=progress)

    aggregate = dict(instance_results[0])
    aggregate["instances"] = instance_results
    aggregate["instanceCount"] = len(instance_results)
    aggregate["commands"] = []
    aggregate["listeners"] = []
    aggregate["backends"] = []
    aggregate["replicationSections"] = []
    aggregate["replicationNodes"] = []
    aggregate["namingContexts"] = []
    aggregate["monitorEntries"] = []
    aggregate["passwordPolicies"] = []

    errors = []
    statuses = []
    open_connections = 0
    open_connection_values = []
    for result in instance_results:
        instance_name = result.get("instanceName") or "OUD Instance"
        if result.get("error"):
            errors.append("{0}: {1}".format(instance_name, result.get("error")))
        if result.get("serverRunStatus"):
            statuses.append(str(result.get("serverRunStatus")))
        value = result.get("openConnections")
        if isinstance(value, int):
            open_connections += value
            open_connection_values.append(value)
        for command in result.get("commands") or []:
            item = dict(command)
            item["label"] = "{0} - {1}".format(instance_name, item.get("label") or "Command")
            aggregate["commands"].append(item)
        for key in ("listeners", "backends", "replicationNodes", "monitorEntries", "passwordPolicies"):
            for row in result.get(key) or []:
                item = dict(row)
                item["instanceName"] = instance_name
                aggregate[key].append(item)
        for section in result.get("replicationSections") or []:
            item = dict(section)
            item["instanceName"] = instance_name
            aggregate["replicationSections"].append(item)
        for context in result.get("namingContexts") or []:
            if context:
                aggregate["namingContexts"].append(context)

    if open_connection_values:
        aggregate["openConnections"] = open_connections
    if statuses:
        normalized_statuses = {status.strip().lower() for status in statuses if status.strip()}
        if len(normalized_statuses) == 1:
            aggregate["serverRunStatus"] = statuses[0]
        elif all(status in ("started", "running") for status in normalized_statuses):
            aggregate["serverRunStatus"] = "Started"
        else:
            aggregate["serverRunStatus"] = "Mixed"
    aggregate["error"] = "; ".join(errors) if errors else None
    aggregate["namingContexts"] = list(dict.fromkeys(aggregate["namingContexts"]))
    aggregate["replicationNodeNames"] = sorted({
        str(row.get("server") or "").strip()
        for row in aggregate["replicationNodes"]
        if str(row.get("server") or "").strip()
    })
    aggregate["replicationNodeCount"] = len(aggregate["replicationNodeNames"])
    aggregate["replicationEnabledCount"] = len([
        section for section in aggregate["replicationSections"] if section.get("enabled")
    ])
    aggregate["replicationDisabledCount"] = len([
        section for section in aggregate["replicationSections"] if not section.get("enabled")
    ])
    aggregate["replicationConflictCount"] = sum(
        int(row.get("conflicts") or 0)
        for row in aggregate["replicationNodes"]
        if str(row.get("conflicts") or "").isdigit()
    )
    aggregate["replicationNormalCount"] = len([
        row for row in aggregate["replicationNodes"]
        if str(row.get("status") or "").strip().lower() == "normal"
    ])
    return enrich_oud_shared_metrics(target, environment, aggregate, progress=progress)


def parse_json_payload(text):
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return None


def parse_kubernetes_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        candidates = [text[:-1], text]
    else:
        candidates = [re.sub(r"([+-]\d\d):?(\d\d)$", "", text)]
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        if "." in candidate:
            base, fraction = candidate.split(".", 1)
            fraction = re.sub(r"\D.*$", "", fraction)[:6]
            candidate = "{0}.{1}".format(base, fraction)
            formats = ["%Y-%m-%dT%H:%M:%S.%f"]
        else:
            formats = ["%Y-%m-%dT%H:%M:%S"]
        for date_format in formats:
            try:
                return datetime.strptime(candidate, date_format).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def kubernetes_age(created_at):
    created = parse_kubernetes_timestamp(created_at)
    if not created:
        return ""
    delta = datetime.now(timezone.utc) - created
    seconds = max(0, int(delta.total_seconds()))
    days = seconds // 86400
    if days:
        return "{0}d".format(days)
    hours = seconds // 3600
    if hours:
        return "{0}h".format(hours)
    minutes = seconds // 60
    if minutes:
        return "{0}m".format(minutes)
    return "{0}s".format(seconds)


def parse_kubernetes_pods(payload):
    rows = []
    for pod in (payload or {}).get("items") or []:
        metadata = pod.get("metadata") or {}
        status = pod.get("status") or {}
        spec = pod.get("spec") or {}
        containers = status.get("containerStatuses") or []
        total_containers = len(containers) or len(spec.get("containers") or [])
        ready_containers = len([item for item in containers if item.get("ready")])
        restarts = sum(int(item.get("restartCount") or 0) for item in containers)
        reason = status.get("reason") or status.get("phase") or ""
        waiting_reasons = [
            (((item.get("state") or {}).get("waiting") or {}).get("reason") or "")
            for item in containers
        ]
        waiting_reasons = [item for item in waiting_reasons if item]
        if waiting_reasons:
            reason = ", ".join(waiting_reasons)
        rows.append({
            "name": metadata.get("name") or "",
            "namespace": metadata.get("namespace") or "",
            "ready": "{0}/{1}".format(ready_containers, total_containers or ready_containers),
            "readyContainers": ready_containers,
            "totalContainers": total_containers,
            "status": reason,
            "restarts": restarts,
            "age": kubernetes_age(metadata.get("creationTimestamp")),
            "podIp": status.get("podIP") or "",
            "node": spec.get("nodeName") or "",
        })
    return rows


def parse_kubernetes_nodes(payload):
    rows = []
    for node in (payload or {}).get("items") or []:
        metadata = node.get("metadata") or {}
        labels = metadata.get("labels") or {}
        status = node.get("status") or {}
        addresses = status.get("addresses") or []
        node_info = status.get("nodeInfo") or {}
        capacity = status.get("capacity") or {}
        allocatable = status.get("allocatable") or {}
        conditions = status.get("conditions") or []
        ready_condition = next((item for item in conditions if item.get("type") == "Ready"), {}) or {}
        role_labels = [
            key.split("/", 1)[-1]
            for key in labels
            if key.startswith("node-role.kubernetes.io/")
        ]
        roles = ", ".join([role for role in role_labels if role]) or "worker"
        rows.append({
            "name": metadata.get("name") or "",
            "status": "Ready" if ready_condition.get("status") == "True" else "NotReady",
            "roles": roles,
            "internalIp": next((item.get("address") for item in addresses if item.get("type") == "InternalIP"), ""),
            "externalIp": next((item.get("address") for item in addresses if item.get("type") == "ExternalIP"), ""),
            "hostname": next((item.get("address") for item in addresses if item.get("type") == "Hostname"), ""),
            "kubeletVersion": node_info.get("kubeletVersion") or "",
            "kernelVersion": node_info.get("kernelVersion") or "",
            "containerRuntime": node_info.get("containerRuntimeVersion") or "",
            "osImage": node_info.get("osImage") or "",
            "cpu": capacity.get("cpu") or "",
            "memory": capacity.get("memory") or "",
            "allocatableCpu": allocatable.get("cpu") or "",
            "allocatableMemory": allocatable.get("memory") or "",
            "age": kubernetes_age(metadata.get("creationTimestamp")),
        })
    return rows


def parse_kubectl_top_rows(text, resource_type):
    rows = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.upper().startswith("NAME "):
            continue
        parts = re.split(r"\s+", line)
        if resource_type == "nodes":
            if len(parts) < 5:
                continue
            rows.append({
                "name": parts[0],
                "cpu": parts[1],
                "cpuPercent": parts[2],
                "memory": parts[3],
                "memoryPercent": parts[4],
            })
        else:
            if len(parts) < 3:
                continue
            rows.append({
                "name": parts[0],
                "cpu": parts[1],
                "memory": parts[2],
            })
    return rows


def parse_kubernetes_events(payload, limit=30):
    rows = []
    for item in (payload or {}).get("items") or []:
        involved = item.get("involvedObject") or item.get("regarding") or {}
        event_type = item.get("type") or item.get("deprecatedType") or ""
        reason = item.get("reason") or item.get("deprecatedReason") or ""
        message = item.get("message") or item.get("note") or ""
        timestamp = (
            item.get("lastTimestamp")
            or item.get("eventTime")
            or (item.get("series") or {}).get("lastObservedTime")
            or item.get("deprecatedLastTimestamp")
            or (item.get("metadata") or {}).get("creationTimestamp")
        )
        count = item.get("count")
        if count is None:
            count = item.get("deprecatedCount")
        rows.append({
            "type": event_type,
            "reason": reason,
            "object": "{0}/{1}".format(involved.get("kind") or "", involved.get("name") or "").strip("/"),
            "message": message,
            "count": count if count is not None else "",
            "age": kubernetes_age(timestamp),
            "_timestamp": timestamp or "",
        })
    warning_rows = [
        row for row in rows
        if str(row.get("type") or "").lower() == "warning"
        or str(row.get("reason") or "").lower() in ("failed", "unhealthy", "backoff", "failedscheduling")
    ]
    warning_rows = sorted(warning_rows, key=lambda row: row.get("_timestamp") or "", reverse=True)
    return [{key: value for key, value in row.items() if key != "_timestamp"} for row in warning_rows[:limit]]


def format_kubernetes_ports(ports):
    formatted = []
    for port in ports or []:
        parts = []
        if port.get("port") is not None:
            parts.append(str(port.get("port")))
        if port.get("nodePort") is not None:
            parts.append(str(port.get("nodePort")))
        protocol = port.get("protocol") or ""
        if protocol:
            parts.append(protocol)
        if parts:
            formatted.append(":".join(parts))
    return ", ".join(formatted)


def parse_kubernetes_resources(payload):
    rows = []
    for item in (payload or {}).get("items") or []:
        metadata = item.get("metadata") or {}
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        kind = item.get("kind") or ""
        name = metadata.get("name") or ""
        detail = ""
        endpoint = ""
        ready = ""
        if kind == "Service":
            detail = spec.get("type") or ""
            endpoint = "{0} {1}".format(spec.get("clusterIP") or "", format_kubernetes_ports(spec.get("ports") or [])).strip()
        elif kind == "Deployment":
            replicas = spec.get("replicas")
            ready_replicas = status.get("readyReplicas") or 0
            updated = status.get("updatedReplicas") or 0
            available = status.get("availableReplicas") or 0
            ready = "{0}/{1}".format(ready_replicas, replicas if replicas is not None else 0)
            detail = "updated {0}, available {1}".format(updated, available)
        elif kind == "ReplicaSet":
            ready = "{0}/{1}".format(status.get("readyReplicas") or 0, spec.get("replicas") or 0)
            detail = "current {0}".format(status.get("replicas") or 0)
        elif kind == "Pod":
            pod_rows = parse_kubernetes_pods({"items": [item]})
            if pod_rows:
                ready = pod_rows[0].get("ready") or ""
                detail = pod_rows[0].get("status") or ""
                endpoint = pod_rows[0].get("podIp") or ""
        elif kind == "Ingress":
            hosts = []
            for rule in spec.get("rules") or []:
                if rule.get("host"):
                    hosts.append(rule.get("host"))
            detail = ", ".join(hosts)
            load_balancers = status.get("loadBalancer", {}).get("ingress") or []
            endpoint = ", ".join([item.get("hostname") or item.get("ip") or "" for item in load_balancers if item])
        elif kind == "PersistentVolumeClaim":
            phase = status.get("phase") or ""
            capacity = (status.get("capacity") or {}).get("storage") or ""
            access_modes = ", ".join(status.get("accessModes") or spec.get("accessModes") or [])
            detail = phase
            endpoint = "{0} {1}".format(capacity, access_modes).strip()
            ready = spec.get("volumeName") or ""
        rows.append({
            "kind": kind,
            "name": name,
            "namespace": metadata.get("namespace") or "",
            "ready": ready,
            "detail": detail,
            "endpoint": endpoint,
            "age": kubernetes_age(metadata.get("creationTimestamp")),
        })
    return rows


def first_matching_pod(pods, release_name, purpose):
    names = [pod.get("name") for pod in pods if pod.get("name")]
    release = str(release_name or "").strip()
    preferred = []
    if purpose == "mgmt":
        preferred = ["{0}-oaa-mgmt".format(release), "oaa-mgmt", "oaamgmt"]
    elif purpose == "runtime":
        preferred = ["{0}-oaa-".format(release), "-oaa-"]
    for token in preferred:
        for name in names:
            if token and token in name:
                return name
    return names[0] if names else ""


def normalize_oaa_runtime_base_url(value):
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    marker = "/config/property/v1"
    if marker in text:
        text = text.split(marker, 1)[0]
    return text.rstrip("/")


def mask_oaa_property_value(name, value):
    text_name = str(name or "").lower()
    if any(token in text_name for token in ("password", "secret", "credential", "apikey", "api.key", "token")):
        return "******" if str(value or "").strip() else ""
    return value


def redact_oaa_sensitive_text(text):
    redacted = []
    sensitive = re.compile(r"(password|passphrase|secret|credential|apikey|api[_\.-]?key|token)", re.I)
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        if sensitive.search(line):
            if "=" in line:
                key, _value = line.split("=", 1)
                line = "{0}=******".format(key.rstrip())
            elif ":" in line:
                key, _value = line.split(":", 1)
                line = "{0}: ******".format(key.rstrip())
            else:
                line = "******"
        redacted.append(line)
    return "\n".join(redacted)


def property_rows_from_json(value, parent_key=""):
    rows = []
    if isinstance(value, list):
        for item in value:
            rows.extend(property_rows_from_json(item, parent_key))
        return rows
    if isinstance(value, dict):
        if "items" in value:
            return property_rows_from_json(value.get("items"), parent_key)
        if "properties" in value:
            return property_rows_from_json(value.get("properties"), parent_key)
        name = value.get("propertyName") or value.get("name") or value.get("key")
        property_value = value.get("propertyValue")
        if property_value is None:
            property_value = value.get("value")
        if name:
            rows.append({"name": str(name), "value": mask_oaa_property_value(name, property_value)})
            return rows
        for key, item in value.items():
            child_name = "{0}.{1}".format(parent_key, key) if parent_key else str(key)
            if isinstance(item, (dict, list)):
                rows.extend(property_rows_from_json(item, child_name))
            else:
                rows.append({"name": child_name, "value": mask_oaa_property_value(child_name, item)})
    return rows


def parse_oaa_properties(text):
    payload = parse_json_payload(text)
    if payload is None:
        output = str(text or "").strip()
        return [{"name": "Raw Output", "value": output[:4000]}] if output else []
    return property_rows_from_json(payload)


def parse_oaa_deployment_details(text):
    details = []
    for raw_line in lines(text):
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        value = mask_oaa_property_value(key, value)
        details.append({"name": key, "value": value})
    return details


OAA_INSTALL_CONFIG_GUIDE = [
    ("Deployment", "Deployment Name", "common.deployment.name"),
    ("Deployment", "Kubernetes Namespace", "common.kube.namespace"),
    ("Deployment", "Deployment Mode", "common.deployment.mode"),
    ("Deployment", "Image Repository", "install.global.repo"),
    ("Deployment", "Image Tag", "install.global.image.tag"),
    ("Deployment", "Image Pull Secret", "install.global.imagePullSecrets[0].name"),
    ("Database", "Create Schema", "database.createschema"),
    ("Database", "Database Name", "database.name"),
    ("Database", "Database Host", "database.host"),
    ("Database", "Database Port", "database.port"),
    ("Database", "Database Schema", "database.schema"),
    ("Database", "Database Tablespace", "database.tablespace"),
    ("Database", "Database Service", "database.svc"),
    ("Database", "Database Protocol", "database.protocol"),
    ("Security", "Runtime OAuth Enabled", "install.global.service.security.oauth.enabled"),
    ("Security", "Runtime Basic Auth Enabled", "install.global.service.security.basic.enabled"),
    ("OAuth", "UI OAuth Enabled", "oauth.enabled"),
    ("OAuth", "OAM Admin URL", "oauth.adminurl"),
    ("OAuth", "OAM Runtime URL", "oauth.identityuri"),
    ("OAuth", "OAuth Domain", "oauth.domainname"),
    ("OAuth", "OAuth Application ID", "oauth.applicationid"),
    ("OAuth", "UI Token Expiry", "oauth.tokenexpiry"),
    ("OAuth", "API Token Expiry", "api.oauth.tokenexpiry"),
    ("Ingress", "Ingress Enabled", "install.global.ingress.enabled"),
    ("Ingress", "Runtime Host", "install.global.ingress.runtime.host"),
    ("Ingress", "Admin Host", "install.global.ingress.admin.host"),
    ("Ingress", "Global Service URL", "install.global.serviceurl"),
    ("Ingress", "Service Type", "install.service.type"),
    ("Ingress", "Install Ingress Controller", "ingress.install"),
    ("Ingress", "Ingress Namespace", "ingress.namespace"),
    ("Ingress", "Ingress Class", "ingress.class.name"),
    ("Ingress", "Ingress Service Type", "ingress.service.type"),
    ("Vault", "Vault Provider", "vault.provider"),
    ("Vault", "Vault Deployment", "vault.deploy.name"),
    ("Vault", "File Keystore Server", "vault.fks.server"),
    ("Vault", "File Keystore Path", "vault.fks.path"),
    ("Vault", "File Keystore Mount Path", "vault.fks.mountpath"),
    ("Management", "Management Release", "install.mgmt.release.name"),
    ("Management", "Config Mount Server", "install.mount.config.server"),
    ("Management", "Config Mount Path", "install.mount.config.path"),
    ("Management", "Logs Mount Server", "install.mount.logs.server"),
    ("Management", "Logs Mount Path", "install.mount.logs.path"),
    ("LDAP", "LDAP Server", "ldap.server"),
    ("LDAP", "LDAP Username", "ldap.username"),
    ("LDAP", "OAA Admin User", "ldap.oaaAdminUser"),
    ("LDAP", "OAA Admin Role", "ldap.adminRole"),
    ("LDAP", "OAA User Role", "ldap.userRole"),
    ("TAP", "OAA TAP Agent Name", "oaa.tapAgentName"),
    ("TAP", "OAA TAP JKS Path", "oaa.tapAgentFileLocation"),
    ("TAP", "OUA TAP Agent Name", "oua.tapAgentName"),
    ("TAP", "OUA TAP JKS Path", "oua.tapAgentFileLocation"),
    ("Factors", "Configured Factors", "oaa.authFactors"),
    ("OUA", "OUA OAM Runtime Endpoint", "oua.oamRuntimeEndpoint"),
]


def parse_oaa_property_file_values(text):
    values = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip()
    return values


def parse_oaa_install_properties(text):
    values = parse_oaa_property_file_values(text)

    rows = []
    for category, label, key in OAA_INSTALL_CONFIG_GUIDE:
        if key not in values:
            continue
        rows.append({
            "category": category,
            "label": label,
            "name": key,
            "value": mask_oaa_property_value(key, values.get(key)),
        })
    return rows


def oaa_install_value(values, *keys):
    for key in keys:
        value = str((values or {}).get(key) or "").strip()
        if value:
            return value
    return ""


def derive_oaa_runtime_base_url(values):
    base_url = oaa_install_value(
        values,
        "install.global.serviceurl",
        "oaa.service.url",
        "oaa.runtime.baseurl",
        "runtime.baseurl",
    )
    base_url = normalize_oaa_runtime_base_url(base_url)
    if not base_url:
        return ""
    if base_url.endswith("/oaa/runtime"):
        return base_url
    return "{0}/oaa/runtime".format(base_url.rstrip("/"))


def build_oaa_database_summary(values):
    values = values or {}
    return {
        "host": oaa_install_value(values, "database.host"),
        "port": oaa_install_value(values, "database.port"),
        "schema": oaa_install_value(values, "database.schema"),
        "service": oaa_install_value(values, "database.svc", "database.service"),
        "name": oaa_install_value(values, "database.name"),
        "tablespace": oaa_install_value(values, "database.tablespace"),
        "protocol": oaa_install_value(values, "database.protocol"),
        "createSchema": oaa_install_value(values, "database.createschema"),
        "hasPassword": bool(oaa_install_value(values, "database.password", "database.schema.password", "database.schemapassword")),
    }


OAA_INSTALL_PATH_MARKER = "__IAM_MONITORING_OAA_INSTALL_PATH__="

OAA_SCHEMA_TABLE_GUIDE = [
    {
        "name": "VCRYPT_USERS",
        "area": "Users",
        "purpose": "User records correlated with the external identity store.",
    },
    {
        "name": "VCRYPT_USER_GROUPS",
        "area": "Users",
        "purpose": "OAA/OARM user groups and their status.",
    },
    {
        "name": "VCRYPT_TRACKER_USERNODE_LOGS",
        "area": "Activity",
        "purpose": "User/device/node activity used for investigation reports.",
    },
    {
        "name": "VCRYPT_TRACKER_NODE",
        "area": "Activity",
        "purpose": "Tracked client or device nodes.",
    },
    {
        "name": "VT_USER_DEVICE_MAP",
        "area": "Devices",
        "purpose": "User to device/fingerprint relationships.",
    },
    {
        "name": "VT_SESSION_ACTION_MAP",
        "area": "Sessions",
        "purpose": "Session action correlation used by challenge and risk flows.",
    },
    {
        "name": "VT_TRX_LOGS",
        "area": "Transactions",
        "purpose": "Custom transaction/activity log rows.",
    },
    {
        "name": "VT_TRX_DATA",
        "area": "Transactions",
        "purpose": "Transaction payload data used by rules and analytics.",
    },
    {
        "name": "VR_RULE_LOGS",
        "area": "Rules",
        "purpose": "Rule execution log rows.",
    },
    {
        "name": "VCRYPT_ALERT",
        "area": "Alerts",
        "purpose": "Generated risk or policy alert records.",
    },
    {
        "name": "VCRYPT_COUNTRY",
        "area": "Geo",
        "purpose": "Country lookup rows for IP geolocation reporting.",
    },
    {
        "name": "VCRYPT_STATE",
        "area": "Geo",
        "purpose": "State lookup rows for IP geolocation reporting.",
    },
    {
        "name": "VCRYPT_CITY",
        "area": "Geo",
        "purpose": "City lookup rows for IP geolocation reporting.",
    },
    {
        "name": "VCRYPT_IP_LOCATION_MAP",
        "area": "Geo",
        "purpose": "IP range to location map for custom reports.",
    },
]


def merge_oaa_database_settings(*sources):
    merged = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in (
            "host",
            "port",
            "service",
            "name",
            "schema",
            "username",
            "protocol",
            "connectString",
            "tablespace",
            "createSchema",
        ):
            value = str(source.get(key) or "").strip()
            if value:
                merged[key] = value
        if source.get("hasPassword"):
            merged["hasPassword"] = True
    if not merged.get("username") and merged.get("schema"):
        merged["username"] = merged.get("schema")
    return merged


def oaa_database_password_from_install(values):
    return oaa_install_value(values, "database.password", "database.schema.password", "database.schemapassword")


def split_oaa_install_properties_output(output):
    found_path = ""
    lines = []
    for raw_line in str(output or "").splitlines():
        if raw_line.startswith(OAA_INSTALL_PATH_MARKER):
            found_path = raw_line[len(OAA_INSTALL_PATH_MARKER):].strip()
            continue
        lines.append(raw_line)
    return "\n".join(lines), found_path


def collect_oaa_install_properties_from_host(target, settings_path, progress=None):
    base_path = str(settings_path or "").strip()
    if base_path:
        candidates = []
        if base_path.endswith(".properties"):
            candidates.append(base_path)
        else:
            clean_base = base_path.rstrip("/")
            candidates.append("{0}/installOAA.properties".format(clean_base))
            if not clean_base.endswith("/settings"):
                candidates.append("{0}/settings/installOAA.properties".format(clean_base))
        unique_candidates = []
        for candidate in candidates:
            if candidate and candidate not in unique_candidates:
                unique_candidates.append(candidate)
        candidate_args = " ".join(shlex.quote(candidate) for candidate in unique_candidates)
        command = (
            "for f in {candidates}; do "
            "if [ -f \"$f\" ]; then printf '{marker}%s\\n' \"$f\"; cat \"$f\"; exit 0; fi; "
            "done; echo 'installOAA.properties was not found under {base_path}.'; exit 1"
        ).format(candidates=candidate_args, marker=OAA_INSTALL_PATH_MARKER, base_path=base_path.replace("'", ""))
        progress_target = base_path
    else:
        command = (
            "for root in /refresh /refresh/home /u01 /opt /home /scratch /mnt /nfs-share; do "
            "[ -d \"$root\" ] || continue; "
            "found=$(find \"$root\" -maxdepth 8 -type f -name installOAA.properties -print -quit 2>/dev/null); "
            "if [ -n \"$found\" ]; then printf '{marker}%s\\n' \"$found\"; cat \"$found\"; exit 0; fi; "
            "done; echo 'installOAA.properties was not found under the common OAA shared filesystem paths.'; exit 1"
        ).format(marker=OAA_INSTALL_PATH_MARKER)
        progress_target = "the OAA host shared filesystem"
    if callable(progress):
        progress("Reading OAA installOAA.properties from {0}.".format(progress_target))
    result = run_target(target, command, timeout=90)
    output, found_path = split_oaa_install_properties_output(result.get("output"))
    if result.get("exit_code") != 0:
        return [], {}, command, result.get("output") or "installOAA.properties collection failed.", found_path
    values = parse_oaa_property_file_values(output)
    rows = parse_oaa_install_properties(output)
    if not rows:
        return rows, values, command, "installOAA.properties was found, but no dashboard-visible configuration keys were parsed.", found_path
    return rows, values, command, None, found_path


def normalize_oaa_database_details(database):
    database = database or {}
    return merge_oaa_database_settings(database)


def oaa_database_missing_fields(database):
    database = normalize_oaa_database_details(database)
    missing = []
    if not database.get("connectString"):
        for key, label in (
            ("host", "Database Host"),
            ("port", "Database Port"),
            ("service", "Database Service"),
            ("username", "Database Schema/User"),
        ):
            if not str(database.get(key) or "").strip():
                missing.append(label)
    return missing


def oaa_database_connect_string(database):
    database = normalize_oaa_database_details(database)
    explicit = str(database.get("connectString") or "").strip()
    if explicit:
        return explicit
    host = str(database.get("host") or "").strip()
    port = str(database.get("port") or "1521").strip() or "1521"
    service = str(database.get("service") or database.get("name") or "").strip()
    if not host or not service:
        return ""
    return "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST={0})(PORT={1}))(CONNECT_DATA=(SERVICE_NAME={2})))".format(
        host,
        port,
        service,
    )


def parse_oaa_schema_table_output(text):
    guide = {item["name"]: item for item in OAA_SCHEMA_TABLE_GUIDE}
    rows = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        table_name = parts[0].strip().upper()
        if table_name not in guide:
            continue
        count_text = parts[1].strip()
        status_text = parts[2].strip()
        row_count = None
        try:
            row_count = int(count_text)
        except (TypeError, ValueError):
            row_count = None
        info = guide[table_name]
        rows.append({
            "name": table_name,
            "area": info.get("area") or "",
            "purpose": info.get("purpose") or "",
            "rowCount": row_count,
            "status": "Available" if row_count is not None else "Unavailable",
            "message": "OK" if row_count is not None else (status_text or "Table unavailable"),
        })
    seen = {row["name"] for row in rows}
    for item in OAA_SCHEMA_TABLE_GUIDE:
        if item["name"] not in seen:
            rows.append({
                "name": item["name"],
                "area": item.get("area") or "",
                "purpose": item.get("purpose") or "",
                "rowCount": None,
                "status": "Not checked",
                "message": "No row returned by schema query.",
            })
    return rows


def build_oaa_schema_host_sqlplus_command(sql_arg, sql_text, oracle_home_hint=""):
    template = (
        "set +e\n"
        "export ORACLE_HOME=__ORACLE_HOME_HINT__\n"
        "find_host_sqlplus() {\n"
        "  if command -v sqlplus >/dev/null 2>&1; then command -v sqlplus; return 0; fi\n"
        "  for candidate in \"$ORACLE_HOME/bin/sqlplus\" /usr/bin/sqlplus /usr/local/bin/sqlplus /opt/oracle/instantclient*/sqlplus /usr/lib/oracle/*/client64/bin/sqlplus /u01/app/oracle/product/*/bin/sqlplus /u01/oracle/product/*/bin/sqlplus; do\n"
        "    [ -x \"$candidate\" ] && { printf '%s\\n' \"$candidate\"; return 0; }\n"
        "  done\n"
        "  find /opt/oracle /u01 /refresh/home /home \\( -path '*/containers/storage/*' -o -path '*/.local/share/containers/*' \\) -prune -o -path '*sqlplus' -type f -perm -111 -print -quit 2>/dev/null\n"
        "}\n"
        "sqlplus_bin=$(find_host_sqlplus | head -1)\n"
        "if [ -n \"$sqlplus_bin\" ]; then\n"
        "  sqlplus_dir=$(dirname \"$sqlplus_bin\")\n"
        "  if [ -n \"$LD_LIBRARY_PATH\" ]; then\n"
        "    export LD_LIBRARY_PATH=\"$sqlplus_dir:$LD_LIBRARY_PATH\"\n"
        "  else\n"
        "    export LD_LIBRARY_PATH=\"$sqlplus_dir\"\n"
        "  fi\n"
        "  cat <<'IAM_MONITORING_SQL' | \"$sqlplus_bin\" -L -S __SQL_ARG__\n"
        "__SQL_TEXT__"
        "IAM_MONITORING_SQL\n"
        "  exit $?\n"
        "fi\n"
        "echo \"sqlplus was not found on this fallback host.\"\n"
        "exit 127\n"
    )
    return (
        template
        .replace("__ORACLE_HOME_HINT__", shlex.quote(str(oracle_home_hint or "").strip()))
        .replace("__SQL_ARG__", shlex.quote(sql_arg))
        .replace("__SQL_TEXT__", sql_text)
    )


def oaa_jdbc_url_from_connect_string(connect_string):
    text = str(connect_string or "").strip()
    if not text:
        return ""
    if text.lower().startswith("jdbc:"):
        return text
    if text.startswith("("):
        return "jdbc:oracle:thin:@" + text
    return "jdbc:oracle:thin:@" + text


def build_oaa_schema_host_jdbc_command(jdbc_url, username, password, table_names, oracle_home_hint=""):
    java_source = r"""
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

public class IamOaaSchemaCheck {
    public static void main(String[] args) throws Exception {
        String url = args[0];
        String user = args[1];
        String password = args[2];
        Class.forName("oracle.jdbc.OracleDriver");
        try (Connection connection = DriverManager.getConnection(url, user, password)) {
            for (int i = 3; i < args.length; i++) {
                String table = args[i];
                try (Statement statement = connection.createStatement();
                     ResultSet rows = statement.executeQuery("select count(*) from " + table)) {
                    rows.next();
                    System.out.println(table + "|" + rows.getLong(1) + "|OK");
                } catch (Exception exc) {
                    String message = exc.getMessage();
                    if (message == null || message.trim().isEmpty()) {
                        message = exc.getClass().getName();
                    }
                    System.out.println(table + "|-|" + message.replace('|', ' '));
                }
            }
        }
    }
}
"""
    jdbc_args = [jdbc_url, username, password] + list(table_names or [])
    template = (
        "set +e\n"
        "export ORACLE_HOME=__ORACLE_HOME_HINT__\n"
        "find_java_bin() {\n"
        "  for candidate in \"$JAVA_HOME/bin/java\" \"$ORACLE_HOME/jdk/bin/java\" \"$ORACLE_HOME/jdk/jre/bin/java\" \"$ORACLE_HOME/jdk/jre/bin/java\" /usr/java*/bin/java /usr/lib/jvm/*/bin/java; do\n"
        "    [ -x \"$candidate\" ] && { printf '%s\\n' \"$candidate\"; return 0; }\n"
        "  done\n"
        "  command -v java 2>/dev/null\n"
        "}\n"
        "find_javac_bin() {\n"
        "  java_bin=\"$1\"\n"
        "  java_dir=$(dirname \"$java_bin\" 2>/dev/null)\n"
        "  for candidate in \"$java_dir/javac\" \"$JAVA_HOME/bin/javac\" \"$ORACLE_HOME/jdk/bin/javac\" \"$ORACLE_HOME/jdk/jre/../bin/javac\" /usr/java*/bin/javac /usr/lib/jvm/*/bin/javac; do\n"
        "    [ -x \"$candidate\" ] && { printf '%s\\n' \"$candidate\"; return 0; }\n"
        "  done\n"
        "  command -v javac 2>/dev/null\n"
        "}\n"
        "find_ojdbc_jar() {\n"
        "  for candidate in \"$ORACLE_HOME/oracle_common/modules/oracle.jdbc/ojdbc\"*.jar \"$ORACLE_HOME/oracle_common/modules/\"*/ojdbc*.jar \"$ORACLE_HOME/wlserver/server/lib/ojdbc\"*.jar \"$ORACLE_HOME/jdbc/lib/ojdbc\"*.jar \"$ORACLE_HOME/lib/ojdbc\"*.jar; do\n"
        "    [ -f \"$candidate\" ] && { printf '%s\\n' \"$candidate\"; return 0; }\n"
        "  done\n"
        "  find /opt/oracle /u01 /refresh/home /home \\( -path '*/containers/storage/*' -o -path '*/.local/share/containers/*' \\) -prune -o -name 'ojdbc*.jar' -type f -print -quit 2>/dev/null\n"
        "}\n"
        "java_bin=$(find_java_bin | head -1)\n"
        "if [ -z \"$java_bin\" ]; then echo \"Java runtime was not found for JDBC Thin schema query.\"; exit 127; fi\n"
        "ojdbc_jar=$(find_ojdbc_jar | head -1)\n"
        "if [ -z \"$ojdbc_jar\" ]; then echo \"Oracle JDBC driver ojdbc*.jar was not found for JDBC Thin schema query.\"; exit 127; fi\n"
        "classdir=$(mktemp -d /tmp/iam-oaa-jdbc.XXXXXX)\n"
        "src=\"$classdir/IamOaaSchemaCheck.java\"\n"
        "trap 'rm -rf \"$classdir\"' EXIT\n"
        "cat > \"$src\" <<'IAM_MONITORING_JAVA'\n"
        "__JAVA_SOURCE__"
        "IAM_MONITORING_JAVA\n"
        "javac_bin=$(find_javac_bin \"$java_bin\" | head -1)\n"
        "if [ -n \"$javac_bin\" ]; then\n"
        "  \"$javac_bin\" -cp \"$ojdbc_jar\" -d \"$classdir\" \"$src\" && \"$java_bin\" -cp \"$ojdbc_jar:$classdir\" IamOaaSchemaCheck __JDBC_ARGS__\n"
        "  exit $?\n"
        "fi\n"
        "\"$java_bin\" -cp \"$ojdbc_jar\" \"$src\" __JDBC_ARGS__\n"
        "exit $?\n"
    )
    return (
        template
        .replace("__ORACLE_HOME_HINT__", shlex.quote(str(oracle_home_hint or "").strip()))
        .replace("__JAVA_SOURCE__", java_source)
        .replace("__JDBC_ARGS__", " ".join(shlex.quote(str(value)) for value in jdbc_args))
    )


def collect_oaa_schema_table_metrics(target, database, password, progress=None, oaa_settings=None, fallback_sqlplus_targets=None):
    database = normalize_oaa_database_details(database)
    missing = oaa_database_missing_fields(database)
    if missing:
        raise ValueError("Missing OAA database details: {0}.".format(", ".join(missing)))
    username = str(database.get("username") or database.get("schema") or "").strip()
    db_password = str(password or "").strip()
    if not db_password:
        raise ValueError("Missing OAA database password.")
    connect_string = oaa_database_connect_string(database)
    if not connect_string:
        raise ValueError("Missing OAA database connect string.")

    oaa_settings = oaa_settings or {}
    namespace = str(oaa_settings.get("namespace") or "oaans").strip() or "oaans"
    release_name = str(oaa_settings.get("releaseName") or "oaainstall").strip() or "oaainstall"
    kubectl_path = str(oaa_settings.get("kubectlPath") or "kubectl").strip() or "kubectl"
    table_names = [item["name"] for item in OAA_SCHEMA_TABLE_GUIDE]
    sql_text = (
        "set heading off feedback off verify off echo off pagesize 0 linesize 32767 trimspool on serveroutput on size unlimited\n"
        "whenever sqlerror exit sql.sqlcode\n"
        "declare\n"
        "  procedure check_table(p_table in varchar2) is\n"
        "  l_count number;\n"
        "  begin\n"
        "    execute immediate 'select count(*) from ' || p_table into l_count;\n"
        "    dbms_output.put_line(p_table || '|' || l_count || '|OK');\n"
        "  exception when others then\n"
        "    dbms_output.put_line(p_table || '|-|' || replace(sqlerrm, '|', ' '));\n"
        "  end;\n"
        "begin\n"
        "{checks}\n"
        "end;\n"
        "/\n"
        "exit\n"
    ).format(checks="\n".join("  check_table('{0}');".format(name) for name in table_names))
    sql_arg = "{0}/{1}@{2}".format(username, db_password, connect_string)
    command = (
        "set +e\n"
        "find_host_sqlplus() {{\n"
        "  if command -v sqlplus >/dev/null 2>&1; then command -v sqlplus; return 0; fi\n"
        "  for candidate in \"$ORACLE_HOME/bin/sqlplus\" /usr/bin/sqlplus /usr/local/bin/sqlplus /opt/oracle/instantclient*/sqlplus /usr/lib/oracle/*/client64/bin/sqlplus /u01/app/oracle/product/*/bin/sqlplus /u01/oracle/product/*/bin/sqlplus; do\n"
        "    [ -x \"$candidate\" ] && {{ printf '%s\\n' \"$candidate\"; return 0; }}\n"
        "  done\n"
        "  find /opt/oracle /u01 /refresh/home /home \\( -path '*/containers/storage/*' -o -path '*/.local/share/containers/*' \\) -prune -o -path '*sqlplus' -type f -perm -111 -print -quit 2>/dev/null\n"
        "}}\n"
        "sqlplus_bin=$(find_host_sqlplus | head -1)\n"
        "if [ -n \"$sqlplus_bin\" ]; then\n"
        "sqlplus_dir=$(dirname \"$sqlplus_bin\")\n"
        "if [ -n \"$LD_LIBRARY_PATH\" ]; then\n"
        "  export LD_LIBRARY_PATH=\"$sqlplus_dir:$LD_LIBRARY_PATH\"\n"
        "else\n"
        "  export LD_LIBRARY_PATH=\"$sqlplus_dir\"\n"
        "fi\n"
        "cat <<'IAM_MONITORING_SQL' | \"$sqlplus_bin\" -L -S {sql_arg}\n"
        "{sql_text}"
        "IAM_MONITORING_SQL\n"
        "exit $?\n"
        "fi\n"
        "kubectl_bin={kubectl_path}\n"
        "namespace={namespace}\n"
        "release_name={release_name}\n"
        "if command -v \"$kubectl_bin\" >/dev/null 2>&1 || [ -x \"$kubectl_bin\" ]; then\n"
        "  pod=$(\"$kubectl_bin\" get pods -n \"$namespace\" -o jsonpath='{{range .items[*]}}{{.metadata.name}}{{\"\\n\"}}{{end}}' 2>/dev/null | awk -v rel=\"$release_name\" 'index($0, rel) && $0 ~ /(mgmt|oaa|admin)/ {{print; exit}} /oaamgmt|oaa-mgmt/ {{print; exit}}')\n"
        "  if [ -n \"$pod\" ]; then\n"
        "    pod_sqlplus=$(\"$kubectl_bin\" exec -n \"$namespace\" \"$pod\" -- sh -lc 'if command -v sqlplus >/dev/null 2>&1; then command -v sqlplus; exit 0; fi; for c in /usr/bin/sqlplus /usr/local/bin/sqlplus /opt/oracle/instantclient*/sqlplus /usr/lib/oracle/*/client64/bin/sqlplus; do [ -x \"$c\" ] && {{ echo \"$c\"; exit 0; }}; done; exit 1' 2>/dev/null | head -1)\n"
        "    if [ -n \"$pod_sqlplus\" ]; then\n"
        "      pod_sqlplus_dir=$(dirname \"$pod_sqlplus\")\n"
        "      pod_command=\"if [ -n \\\"\\$LD_LIBRARY_PATH\\\" ]; then export LD_LIBRARY_PATH=\\\"$pod_sqlplus_dir:\\$LD_LIBRARY_PATH\\\"; else export LD_LIBRARY_PATH=\\\"$pod_sqlplus_dir\\\"; fi; \\\"$pod_sqlplus\\\" -L -S {sql_arg}\"\n"
        "      cat <<'IAM_MONITORING_SQL' | \"$kubectl_bin\" exec -i -n \"$namespace\" \"$pod\" -- sh -lc \"$pod_command\"\n"
        "{sql_text}"
        "IAM_MONITORING_SQL\n"
        "      exit $?\n"
        "    fi\n"
        "  fi\n"
        "fi\n"
        "echo \"sqlplus was not found on the OAA control host or in OAA Kubernetes pods. Checked PATH, common Oracle client paths, and Kubernetes namespace $namespace. Install Oracle Instant Client SQL*Plus on the OAA control host, or make SQL*Plus available in an OAA management pod, then reload Schema Tables.\"\n"
        "exit 127\n"
    ).format(
        sql_arg=shlex.quote(sql_arg),
        sql_text=sql_text,
        kubectl_path=shlex.quote(kubectl_path),
        namespace=shlex.quote(namespace),
        release_name=shlex.quote(release_name),
    )
    if callable(progress):
        progress("Collecting OAA schema table counts from the configured database.")
    result = run_target(target, command, timeout=180)
    attempts = [{
        "label": "OAA control host and OAA Kubernetes pods",
        "exitCode": result.get("exit_code"),
        "output": str(result.get("output") or "").strip(),
    }]
    fallback_used = None
    jdbc_used = None
    if result.get("exit_code") != 0 and fallback_sqlplus_targets:
        output_text = str(result.get("output") or "")
        should_try_fallback = (
            result.get("exit_code") == 127
            or ("libsqlplus.so" in output_text and "cannot open shared object file" in output_text)
            or "sqlplus was not found" in output_text.lower()
        )
        if should_try_fallback:
            for fallback in fallback_sqlplus_targets:
                fallback_target = (fallback or {}).get("target") or {}
                fallback_label = (fallback or {}).get("label") or fallback_target.get("host") or "fallback host"
                fallback_oracle_home = (fallback or {}).get("oracleHome") or ""
                if callable(progress):
                    progress("Retrying OAA schema table query with SQL*Plus from {0}.".format(fallback_label))
                fallback_command = build_oaa_schema_host_sqlplus_command(sql_arg, sql_text, fallback_oracle_home)
                fallback_result = run_target(fallback_target, fallback_command, timeout=180)
                attempts.append({
                    "label": fallback_label,
                    "exitCode": fallback_result.get("exit_code"),
                    "output": str(fallback_result.get("output") or "").strip(),
                })
                if fallback_result.get("exit_code") == 0:
                    result = fallback_result
                    fallback_used = fallback_label
                    break

    if result.get("exit_code") != 0:
        output_text = str(result.get("output") or "")
        should_try_jdbc = (
            result.get("exit_code") == 127
            or "sqlplus was not found" in output_text.lower()
            or "libsqlplus.so" in output_text
            or "sp2-0750" in output_text.lower()
        )
        if should_try_jdbc:
            jdbc_url = oaa_jdbc_url_from_connect_string(connect_string)
            if callable(progress):
                progress("Retrying OAA schema table query with JDBC Thin from the OAA control host.")
            jdbc_command = build_oaa_schema_host_jdbc_command(jdbc_url, username, db_password, table_names, "")
            jdbc_result = run_target(target, jdbc_command, timeout=180)
            attempts.append({
                "label": "JDBC Thin on OAA control host",
                "exitCode": jdbc_result.get("exit_code"),
                "output": str(jdbc_result.get("output") or "").strip(),
            })
            if jdbc_result.get("exit_code") == 0:
                result = jdbc_result
                jdbc_used = "OAA control host"
            elif fallback_sqlplus_targets:
                for fallback in fallback_sqlplus_targets:
                    fallback_target = (fallback or {}).get("target") or {}
                    fallback_label = (fallback or {}).get("label") or fallback_target.get("host") or "fallback host"
                    fallback_oracle_home = (fallback or {}).get("oracleHome") or ""
                    if callable(progress):
                        progress("Retrying OAA schema table query with JDBC Thin from {0}.".format(fallback_label))
                    fallback_jdbc_command = build_oaa_schema_host_jdbc_command(jdbc_url, username, db_password, table_names, fallback_oracle_home)
                    fallback_jdbc_result = run_target(fallback_target, fallback_jdbc_command, timeout=180)
                    attempts.append({
                        "label": "JDBC Thin on {0}".format(fallback_label),
                        "exitCode": fallback_jdbc_result.get("exit_code"),
                        "output": str(fallback_jdbc_result.get("output") or "").strip(),
                    })
                    if fallback_jdbc_result.get("exit_code") == 0:
                        result = fallback_jdbc_result
                        jdbc_used = fallback_label
                        break

    rows = parse_oaa_schema_table_output(result.get("output"))
    available = [row for row in rows if row.get("rowCount") is not None]
    total_rows = sum(int(row.get("rowCount") or 0) for row in available)
    summary = {
        "checkedTables": len(rows),
        "availableTables": len(available),
        "totalRows": total_rows,
        "databaseUser": username,
        "connectTarget": "{0}:{1}/{2}".format(
            database.get("host") or "-",
            database.get("port") or "1521",
            database.get("service") or database.get("name") or "-",
        ),
    }
    error = None
    result_output = result.get("output") or ""
    if result.get("exit_code") != 0:
        if "libsqlplus.so" in result_output and "cannot open shared object file" in result_output:
            error = (
                "SQL*Plus was found, but its Instant Client libraries are not complete or not loadable. "
                "The collector sets LD_LIBRARY_PATH to the SQL*Plus directory automatically and skips "
                "container overlay SQL*Plus paths; verify that libsqlplus.so exists next to sqlplus, or "
                "make SQL*Plus available in an OAA management pod."
            )
        else:
            attempted = "; ".join(
                "{0}: {1}".format(item.get("label"), (item.get("output") or "").splitlines()[-1] if item.get("output") else "no output")
                for item in attempts
            )
            error = attempted or result_output or "OAA schema table query failed."
    method = "JDBC Thin" if jdbc_used else "SQL*Plus"
    command_summary = "OAA schema table query via {0} against {1} as {2}. SQL text hidden by dashboard.".format(
        method,
        summary.get("connectTarget") or "-",
        username,
    )
    if fallback_used:
        command_summary = "{0} SQL*Plus fallback used: {1}.".format(command_summary, fallback_used)
    if jdbc_used:
        command_summary = "{0} JDBC Thin fallback used: {1}.".format(command_summary, jdbc_used)
    if result.get("exit_code") == 127:
        command_summary = "SQL*Plus and JDBC Thin discovery checked the OAA control host, OAA namespace pods, and configured WebLogic/OAM fallback hosts."
    return {
        "tables": rows,
        "summary": summary,
        "command": command_summary,
        "error": error,
    }


def parse_oaa_helm_releases(text):
    payload = parse_json_payload(text)
    rows = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("releases") or []
    else:
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append({
            "name": item.get("name") or "",
            "namespace": item.get("namespace") or "",
            "revision": item.get("revision") or "",
            "updated": item.get("updated") or "",
            "status": item.get("status") or "",
            "chart": item.get("chart") or "",
            "appVersion": item.get("app_version") or item.get("appVersion") or "",
        })
    return rows


def parse_oaa_helm_version(text):
    lines = []
    warnings = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("WARNING"):
            warnings.append(line)
        else:
            lines.append(line)
    cleaned = "\n".join(lines).strip()
    payload = parse_json_payload(cleaned)
    info = {
        "version": "",
        "gitCommit": "",
        "gitTreeState": "",
        "goVersion": "",
        "warnings": warnings,
        "raw": cleaned,
    }
    if isinstance(payload, dict):
        info["version"] = payload.get("version") or payload.get("Version") or ""
        info["gitCommit"] = payload.get("git_commit") or payload.get("GitCommit") or ""
        info["gitTreeState"] = payload.get("git_tree_state") or payload.get("GitTreeState") or ""
        info["goVersion"] = payload.get("go_version") or payload.get("GoVersion") or ""
        return info
    for key, field in (
        ("Version", "version"),
        ("GitCommit", "gitCommit"),
        ("GitTreeState", "gitTreeState"),
        ("GoVersion", "goVersion"),
    ):
        match = re.search(r'{0}:"([^"]+)"'.format(key), cleaned)
        if match:
            info[field] = match.group(1)
    if not info["version"]:
        match = re.search(r"\bv\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?", cleaned)
        if match:
            info["version"] = match.group(0)
    return info


def parse_oaa_helm_history(text, release_name):
    lines = []
    warnings = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("WARNING"):
            warnings.append(line)
        else:
            lines.append(line)

    rows = []
    status_pattern = r"(superseded|deployed|failed|pending-install|pending-upgrade|pending-rollback|uninstalled|uninstalling|unknown)"
    for line in lines:
        if line.upper().startswith("REVISION"):
            continue
        match = re.match(r"^(\d+)\s+(.+?)\s+{0}\s+(\S+)\s+(\S+)\s+(.*)$".format(status_pattern), line, re.IGNORECASE)
        if match:
            rows.append({
                "release": release_name,
                "revision": match.group(1).strip(),
                "updated": match.group(2).strip(),
                "status": match.group(3).strip(),
                "chart": match.group(4).strip(),
                "appVersion": match.group(5).strip(),
                "description": match.group(6).strip(),
            })
            continue
        parts = re.split(r"\t+|\s{2,}", line.strip())
        if len(parts) < 6 or not str(parts[0]).strip().isdigit():
            continue
        rows.append({
            "release": release_name,
            "revision": parts[0].strip(),
            "updated": parts[1].strip(),
            "status": parts[2].strip(),
            "chart": parts[3].strip(),
            "appVersion": parts[4].strip(),
            "description": " ".join(parts[5:]).strip(),
        })
    return rows, warnings


def oaa_certificate_endpoints(runtime_base_url, deployment_details):
    endpoints = []
    seen = set()

    def add_url(label, value):
        text = str(value or "").strip()
        if not text:
            return
        parsed = urlparse(text)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return
        port = parsed.port or 443
        key = (parsed.hostname, str(port))
        if key in seen:
            return
        seen.add(key)
        endpoints.append({
            "label": label,
            "host": parsed.hostname,
            "port": str(port),
            "url": text,
        })

    add_url("Runtime Base URL", runtime_base_url)
    for item in deployment_details or []:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            add_url(name, value)
    return endpoints


def collect_oaa_endpoint_certificates(target, endpoints, progress=None):
    endpoint_rows = endpoints or []
    if endpoint_rows and callable(progress):
        progress("Starting OAA HTTPS certificate expiry collection for {0} endpoint(s).".format(len(endpoint_rows)))
    certificates = []
    errors = []
    if not endpoint_rows:
        return [], "No HTTPS OAA endpoints were available for certificate collection."
    for endpoint in endpoint_rows[:20]:
        host = str(endpoint.get("host") or "").strip()
        port = str(endpoint.get("port") or "443").strip() or "443"
        label = str(endpoint.get("label") or "OAA Endpoint").strip() or "OAA Endpoint"
        if not host:
            continue
        command = (
            "printf '' | openssl s_client -servername {server_name} -connect {connect_target} 2>/dev/null "
            "| openssl x509 -noout -subject -issuer -enddate 2>/dev/null"
        ).format(
            server_name=shlex.quote(host),
            connect_target=shlex.quote("{0}:{1}".format(host, port)),
        )
        result = run_target(target, command, timeout=20)
        parsed = parse_openssl_certificate_output(result.get("output"))
        status, status_label, days_remaining = certificate_status(parsed.get("expiresAt"))
        if result.get("exit_code") != 0 or not parsed.get("expiresAt"):
            status = "warning"
            status_label = "Unavailable"
            parsed["subject"] = parsed.get("subject") or (str(result.get("output") or "").strip() or "Certificate not returned by openssl.")
            errors.append("{0}: certificate not returned by openssl.".format(label))
        certificates.append({
            "source": "OAA HTTPS Endpoint",
            "server": label,
            "host": host,
            "port": port,
            "url": endpoint.get("url") or "",
            "subject": parsed.get("subject") or "",
            "issuer": parsed.get("issuer") or "",
            "expiresAt": parsed.get("expiresAt") or "",
            "status": status,
            "statusLabel": status_label,
            "daysRemaining": days_remaining,
            "command": command,
        })
    return certificates, "; ".join(errors)


def get_oaa_metrics(target, environment, progress=None):
    if not (environment.get("products") or {}).get("oaa"):
        return None

    settings = environment.get("oaa") or {}
    target = build_oaa_target(environment, target)
    install_settings_path = str(settings.get("installSettingsPath") or "").strip()
    host_install_config = []
    host_install_values = {}
    host_install_command = ""
    host_install_error = None
    host_install_path = ""
    host_install_config, host_install_values, host_install_command, host_install_error, host_install_path = collect_oaa_install_properties_from_host(
        target,
        install_settings_path,
        progress=progress,
    )
    namespace = str(settings.get("namespace") or oaa_install_value(host_install_values, "common.kube.namespace") or "oaans").strip() or "oaans"
    ingress_namespace = str(settings.get("ingressNamespace") or oaa_install_value(host_install_values, "ingress.namespace") or "ingressns").strip() or "ingressns"
    release_name = str(settings.get("releaseName") or oaa_install_value(host_install_values, "common.deployment.name") or "oaainstall").strip() or "oaainstall"
    kubectl = str(settings.get("kubectlPath") or "kubectl").strip() or "kubectl"
    try:
        log_tail_lines = max(10, min(500, int(settings.get("logTailLines") or 80)))
    except (TypeError, ValueError):
        log_tail_lines = 80
    runtime_base_url = normalize_oaa_runtime_base_url(settings.get("runtimeBaseUrl") or derive_oaa_runtime_base_url(host_install_values))
    runtime_username = str(settings.get("runtimeUsername") or "{0}-oaa".format(release_name)).strip()
    runtime_password = str(settings.get("runtimePassword") or "").strip()
    property_names = settings.get("propertyNames") or []
    errors = []

    if callable(progress):
        progress("Starting OAA Kubernetes collection for namespace {0}.".format(namespace))

    pods_json_command = "{0} get pods -n {1} -o json".format(shlex.quote(kubectl), shlex.quote(namespace))
    pods_command = "{0} get pods -n {1} -o wide".format(kubectl, namespace)
    ingress_command = "{0} get all,ing -n {1} -o json".format(shlex.quote(kubectl), shlex.quote(ingress_namespace))
    namespace_resources_command = "{0} get svc,deploy,rs,ing,pvc -n {1} -o json".format(
        shlex.quote(kubectl),
        shlex.quote(namespace),
    )
    nodes_json_command = "{0} get nodes -o json".format(shlex.quote(kubectl))
    nodes_command = "{0} get nodes -o wide".format(kubectl)
    helm_version_command = "helm version"
    pod_usage_command = "{0} top pods -n {1} --no-headers".format(shlex.quote(kubectl), shlex.quote(namespace))
    node_usage_command = "{0} top nodes --no-headers".format(shlex.quote(kubectl))
    events_command = "{0} get events -n {1} --sort-by=.lastTimestamp -o json".format(
        shlex.quote(kubectl),
        shlex.quote(namespace),
    )

    helm_history = []
    helm_history_errors = []
    helm_history_warnings = []
    helm_history_commands = []
    history_releases = []
    for item in (release_name, "oaamgmt"):
        name = str(item or "").strip()
        if name and name not in history_releases:
            history_releases.append(name)
    helm_history_tasks = []
    for history_release in history_releases:
        history_command = "helm history {0} -n {1}".format(
            shlex.quote(history_release),
            shlex.quote(namespace),
        )
        helm_history_commands.append(history_command)
        helm_history_tasks.append((
            "helm_history_{0}".format(history_release),
            lambda history_release=history_release, history_command=history_command: run_target(target, history_command, timeout=60),
        ))

    if callable(progress):
        progress("Running OAA kubectl, helm, usage, and event probes in parallel.")
    initial_tasks = [
        ("pods", lambda: run_target(target, pods_json_command, timeout=60)),
        ("ingress", lambda: run_target(target, ingress_command, timeout=60)),
        ("namespace_resources", lambda: run_target(target, namespace_resources_command, timeout=60)),
        ("nodes", lambda: run_target(target, nodes_json_command, timeout=60)),
        ("helm_version", lambda: run_target(target, helm_version_command, timeout=45)),
        ("pod_usage", lambda: run_target(target, pod_usage_command, timeout=45)),
        ("node_usage", lambda: run_target(target, node_usage_command, timeout=45)),
        ("events", lambda: run_target(target, events_command, timeout=60)),
    ] + helm_history_tasks
    initial_results = run_parallel_tasks(initial_tasks, max_workers=8)

    pods_result = task_result(initial_results, "pods", {"exit_code": 1, "output": "kubectl get pods task failed."})
    pods_payload = parse_json_payload(pods_result.get("output"))
    pods = parse_kubernetes_pods(pods_payload) if pods_payload else []
    pods_error = None
    if pods_result.get("exit_code") != 0:
        pods_error = pods_result.get("output") or "kubectl get pods failed."
        errors.append(pods_error)
    elif pods_payload is None:
        pods_error = "kubectl get pods did not return JSON."
        errors.append(pods_error)

    ingress_result = task_result(initial_results, "ingress", {"exit_code": 1, "output": "kubectl get all,ing task failed."})
    ingress_payload = parse_json_payload(ingress_result.get("output"))
    ingress_resources = parse_kubernetes_resources(ingress_payload) if ingress_payload else []
    ingress_error = None
    if ingress_result.get("exit_code") != 0:
        ingress_error = ingress_result.get("output") or "kubectl get all,ing failed."
        errors.append(ingress_error)
    elif ingress_payload is None:
        ingress_error = "kubectl get all,ing did not return JSON."
        errors.append(ingress_error)

    namespace_resources_result = task_result(initial_results, "namespace_resources", {"exit_code": 1, "output": "kubectl get svc,deploy,rs,ing,pvc task failed."})
    namespace_resources_payload = parse_json_payload(namespace_resources_result.get("output"))
    namespace_resources = parse_kubernetes_resources(namespace_resources_payload) if namespace_resources_payload else []
    namespace_resources_error = None
    if namespace_resources_result.get("exit_code") != 0:
        namespace_resources_error = namespace_resources_result.get("output") or "kubectl get svc,deploy,rs,ing,pvc failed."
    elif namespace_resources_payload is None:
        namespace_resources_error = "kubectl get svc,deploy,rs,ing,pvc did not return JSON."

    if callable(progress):
        progress("Collecting OAA Kubernetes node, pod usage, and warning event metrics.")
    nodes_result = task_result(initial_results, "nodes", {"exit_code": 1, "output": "kubectl get nodes task failed."})
    nodes_payload = parse_json_payload(nodes_result.get("output"))
    node_inventory = parse_kubernetes_nodes(nodes_payload) if nodes_payload else []
    nodes_error = None
    if nodes_result.get("exit_code") != 0:
        nodes_error = nodes_result.get("output") or "kubectl get nodes failed."
    elif nodes_payload is None:
        nodes_error = "kubectl get nodes did not return JSON."

    helm_version_result = task_result(initial_results, "helm_version", {"exit_code": 1, "output": "helm version task failed."})
    helm_version = parse_oaa_helm_version(helm_version_result.get("output")) if helm_version_result.get("exit_code") == 0 else {}
    helm_version_error = None
    if helm_version_result.get("exit_code") != 0:
        helm_version_error = helm_version_result.get("output") or "helm version failed."
    elif not (helm_version.get("version") or helm_version.get("raw")):
        helm_version_error = "helm version returned no parseable version details."

    for history_release in history_releases:
        history_result = task_result(
            initial_results,
            "helm_history_{0}".format(history_release),
            {"exit_code": 1, "output": "helm history task failed for {0}.".format(history_release)},
        )
        if history_result.get("exit_code") != 0:
            helm_history_errors.append(history_result.get("output") or "helm history failed for {0}.".format(history_release))
            continue
        rows, warnings = parse_oaa_helm_history(history_result.get("output"), history_release)
        helm_history.extend(rows)
        helm_history_warnings.extend(warnings)
        if not rows:
            helm_history_errors.append("helm history returned no rows for {0}.".format(history_release))

    pod_usage_result = task_result(initial_results, "pod_usage", {"exit_code": 1, "output": "kubectl top pods task failed."})
    pod_resource_usage = parse_kubectl_top_rows(pod_usage_result.get("output"), "pods") if pod_usage_result.get("exit_code") == 0 else []
    pod_usage_error = None
    if pod_usage_result.get("exit_code") != 0:
        pod_usage_error = pod_usage_result.get("output") or "kubectl top pods failed. Kubernetes metrics-server may not be installed."

    node_usage_result = task_result(initial_results, "node_usage", {"exit_code": 1, "output": "kubectl top nodes task failed."})
    node_resource_usage = parse_kubectl_top_rows(node_usage_result.get("output"), "nodes") if node_usage_result.get("exit_code") == 0 else []
    node_usage_error = None
    if node_usage_result.get("exit_code") != 0:
        node_usage_error = node_usage_result.get("output") or "kubectl top nodes failed. Kubernetes metrics-server may not be installed."

    events_result = task_result(initial_results, "events", {"exit_code": 1, "output": "kubectl get events task failed."})
    events_payload = parse_json_payload(events_result.get("output"))
    warning_events = parse_kubernetes_events(events_payload) if events_payload else []
    events_error = None
    if events_result.get("exit_code") != 0:
        events_error = events_result.get("output") or "kubectl get events failed."
    elif events_payload is None:
        events_error = "kubectl get events did not return JSON."

    describe_pod_name = first_matching_pod(pods, release_name, "runtime")
    describe_command = ""
    describe_output = ""
    describe_error = None
    describe_result = None
    if describe_pod_name:
        describe_command = "{0} describe pod {1} -n {2}".format(
            shlex.quote(kubectl),
            shlex.quote(describe_pod_name),
            shlex.quote(namespace),
        )

    log_command = ""
    log_output = ""
    log_error = None
    log_pod_name = describe_pod_name
    log_result = None
    if log_pod_name:
        log_command = "{0} logs {1} -n {2} --tail={3}".format(
            shlex.quote(kubectl),
            shlex.quote(log_pod_name),
            shlex.quote(namespace),
            int(log_tail_lines),
        )
    pod_detail_tasks = []
    if describe_command:
        pod_detail_tasks.append(("describe", lambda: run_target(target, describe_command, timeout=60)))
    if log_command:
        pod_detail_tasks.append(("log", lambda: run_target(target, log_command, timeout=60)))
    pod_detail_results = run_parallel_tasks(pod_detail_tasks, max_workers=2)
    if describe_command:
        describe_result = task_result(pod_detail_results, "describe", {"exit_code": 1, "output": "kubectl describe pod task failed."})
        describe_output = redact_oaa_sensitive_text(describe_result.get("output") or "")
        if describe_result.get("exit_code") != 0:
            describe_error = describe_output or "kubectl describe pod failed."
    if log_command:
        log_result = task_result(pod_detail_results, "log", {"exit_code": 1, "output": "kubectl logs task failed."})
        log_output = redact_oaa_sensitive_text(log_result.get("output") or "")
        if log_result.get("exit_code") != 0:
            log_error = log_output or "kubectl logs failed."

    mgmt_pod_name = first_matching_pod(pods, release_name, "mgmt")
    deployment_details_command = ""
    deployment_details = []
    deployment_details_error = None
    install_config_command = host_install_command
    install_config = host_install_config
    install_config_values = dict(host_install_values)
    install_config_error = host_install_error
    deployment_result = None
    config_result = None
    if mgmt_pod_name:
        script_command = "cd ~/scripts && ./printOAADetails.sh -f settings/installOAA.properties"
        deployment_details_command = "{0} exec -n {1} {2} -- /bin/bash -lc {3}".format(
            shlex.quote(kubectl),
            shlex.quote(namespace),
            shlex.quote(mgmt_pod_name),
            shlex.quote(script_command),
        )

        if not install_config:
            config_script = (
                "for f in ~/scripts/settings/installOAA.properties settings/installOAA.properties "
                "/u01/oracle/scripts/settings/installOAA.properties; do "
                "if [ -f \"$f\" ]; then cat \"$f\"; exit 0; fi; "
                "done; echo 'installOAA.properties was not found in the OAA management pod.'; exit 1"
            )
            install_config_command = "{0} exec -n {1} {2} -- /bin/bash -lc {3}".format(
                shlex.quote(kubectl),
                shlex.quote(namespace),
                shlex.quote(mgmt_pod_name),
                shlex.quote(config_script),
            )
    elif pods:
        deployment_details_error = "OAA management pod was not found in namespace {0}.".format(namespace)
        if not install_config:
            install_config_error = deployment_details_error

    installation_status_command = ""
    installation_status = []
    installation_status_error = None
    management_log_command = ""
    management_log_output = ""
    management_log_error = None
    helm_command = ""
    helm_releases = []
    helm_error = None
    mgmt_tasks = []
    if mgmt_pod_name:
        status_script = "cat /u01/oracle/logs/status.info 2>/dev/null || true"
        installation_status_command = "{0} exec -n {1} {2} -- /bin/bash -lc {3}".format(
            shlex.quote(kubectl),
            shlex.quote(namespace),
            shlex.quote(mgmt_pod_name),
            shlex.quote(status_script),
        )
        mgmt_tasks.append(("deployment", lambda: run_target(target, deployment_details_command, timeout=90)))
        if install_config_command and not install_config:
            mgmt_tasks.append(("install_config", lambda: run_target(target, install_config_command, timeout=45)))
        mgmt_tasks.append(("status", lambda: run_target(target, installation_status_command, timeout=45)))

        log_script = (
            "for f in /u01/oracle/logs/install.log /u01/oracle/logs/status.info; do "
            "if [ -f \"$f\" ]; then echo \"===== $f =====\"; tail -n {lines} \"$f\"; fi; "
            "done"
        ).format(lines=int(log_tail_lines))
        management_log_command = "{0} exec -n {1} {2} -- /bin/bash -lc {3}".format(
            shlex.quote(kubectl),
            shlex.quote(namespace),
            shlex.quote(mgmt_pod_name),
            shlex.quote(log_script),
        )
        mgmt_tasks.append(("management_log", lambda: run_target(target, management_log_command, timeout=45)))

        helm_script = "helm list -n {0} -o json".format(shlex.quote(namespace))
        helm_command = "{0} exec -n {1} {2} -- /bin/bash -lc {3}".format(
            shlex.quote(kubectl),
            shlex.quote(namespace),
            shlex.quote(mgmt_pod_name),
            shlex.quote(helm_script),
        )
        mgmt_tasks.append(("helm", lambda: run_target(target, helm_command, timeout=60)))
        mgmt_results = run_parallel_tasks(mgmt_tasks, max_workers=5)

        deployment_result = task_result(mgmt_results, "deployment", {"exit_code": 1, "output": "printOAADetails.sh task failed."})
        deployment_details = parse_oaa_deployment_details(deployment_result.get("output"))
        if deployment_result.get("exit_code") != 0:
            deployment_details_error = deployment_result.get("output") or "printOAADetails.sh failed."

        if install_config_command and not install_config:
            config_result = task_result(mgmt_results, "install_config", {"exit_code": 1, "output": "installOAA.properties task failed."})
            install_config = parse_oaa_install_properties(config_result.get("output"))
            install_config_values = parse_oaa_property_file_values(config_result.get("output"))
            if config_result.get("exit_code") != 0:
                install_config_error = config_result.get("output") or "installOAA.properties collection failed."
            elif not install_config:
                install_config_error = "installOAA.properties was found, but no dashboard-visible configuration keys were parsed."
            else:
                install_config_error = None

        status_result = task_result(mgmt_results, "status", {"exit_code": 1, "output": "status.info task failed."})
        installation_status = parse_oaa_deployment_details(status_result.get("output"))
        if status_result.get("exit_code") != 0:
            installation_status_error = status_result.get("output") or "status.info collection failed."

        management_log_result = task_result(mgmt_results, "management_log", {"exit_code": 1, "output": "OAA management log tail task failed."})
        management_log_output = redact_oaa_sensitive_text(management_log_result.get("output") or "")
        if management_log_result.get("exit_code") != 0:
            management_log_error = management_log_output or "OAA management log tail failed."

        helm_result = task_result(mgmt_results, "helm", {"exit_code": 1, "output": "helm list task failed."})
        helm_releases = parse_oaa_helm_releases(helm_result.get("output"))
        if helm_result.get("exit_code") != 0:
            helm_error = helm_result.get("output") or "helm list failed in the OAA management pod."
        elif not helm_releases and str(helm_result.get("output") or "").strip() not in ("", "[]"):
            helm_error = "helm list did not return parsed release rows."
    elif pods:
        installation_status_error = "OAA management pod was not found in namespace {0}.".format(namespace)
        management_log_error = installation_status_error
        helm_error = installation_status_error

    if not runtime_base_url:
        runtime_base_url = normalize_oaa_runtime_base_url(derive_oaa_runtime_base_url(install_config_values))
    install_database = merge_oaa_database_settings(
        build_oaa_database_summary(install_config_values),
        settings.get("database") or {},
    )
    certificate_endpoints = oaa_certificate_endpoints(runtime_base_url, deployment_details)
    certificates, certificate_error = collect_oaa_endpoint_certificates(target, certificate_endpoints, progress=progress)

    property_command = ""
    properties = []
    properties_error = None
    specific_properties = []
    if runtime_base_url and runtime_username and runtime_password:
        all_property_url = "{0}/config/property/v1?propertyName=*".format(runtime_base_url)
        property_command = "curl -k --noproxy '*' -sS -u {0}:<password> -X GET {1}".format(
            shlex.quote(runtime_username),
            shlex.quote(all_property_url),
        )
        actual_property_command = "curl -k --noproxy '*' -sS -u {0}:{1} -X GET {2}".format(
            shlex.quote(runtime_username),
            shlex.quote(runtime_password),
            shlex.quote(all_property_url),
        )
        property_result = run_target(target, actual_property_command, timeout=60)
        property_output = property_result.get("output")
        property_payload = parse_json_payload(property_output)
        properties = property_rows_from_json(property_payload) if property_payload is not None else []
        if property_result.get("exit_code") != 0:
            properties_error = property_result.get("output") or "OAA property API request failed."
            errors.append(properties_error)
        elif property_payload is None:
            preview = (str(property_output or "").strip().splitlines() or [""])[0][:180]
            properties_error = "OAA property API did not return JSON."
            if preview:
                properties_error = "{0} First line: {1}".format(properties_error, preview)
            errors.append(properties_error)
        for property_name in property_names:
            name = str(property_name or "").strip()
            if not name:
                continue
            property_url = "{0}/config/property/v1?propertyName={1}".format(runtime_base_url, quote(name, safe=""))
            actual_specific_command = "curl -k --noproxy '*' -sS -u {0}:{1} -X GET {2}".format(
                shlex.quote(runtime_username),
                shlex.quote(runtime_password),
                shlex.quote(property_url),
            )
            specific_result = run_target(target, actual_specific_command, timeout=45)
            specific_output = specific_result.get("output")
            specific_payload = parse_json_payload(specific_output)
            rows = property_rows_from_json(specific_payload) if specific_payload is not None else []
            if rows:
                for row in rows:
                    item = dict(row)
                    item["requestedName"] = name
                    specific_properties.append(item)
            else:
                value = specific_output or ""
                if specific_payload is None and specific_result.get("exit_code") == 0:
                    value = "OAA property API did not return JSON."
                specific_properties.append({
                    "requestedName": name,
                    "name": name,
                    "value": value,
                    "error": specific_result.get("exit_code") != 0 or specific_payload is None,
                })
    else:
        missing = []
        if not runtime_base_url:
            missing.append("OAA Runtime Base URL")
        if not runtime_username:
            missing.append("OAA Runtime API Username")
        if not runtime_password:
            missing.append("OAA Runtime API Password")
        properties_error = "Missing OAA runtime property settings: {0}.".format(", ".join(missing))

    ready_pods = [pod for pod in pods if pod.get("readyContainers") == pod.get("totalContainers") and pod.get("status") == "Running"]
    running_pods = [pod for pod in pods if pod.get("status") == "Running"]
    restart_count = sum(int(pod.get("restarts") or 0) for pod in pods)

    return {
        "enabled": True,
        "namespace": namespace,
        "ingressNamespace": ingress_namespace,
        "releaseName": release_name,
        "installSettingsPath": install_settings_path or host_install_path,
        "installConfigSourcePath": host_install_path,
        "error": "; ".join(errors) if errors else None,
        "commands": [
            {"label": "OAA Pods", "command": "{0} get pods -n {1} -o wide".format(kubectl, namespace)},
            {"label": "OAA Namespace Resources", "command": "{0} get svc,deploy,rs,ing,pvc -n {1}".format(kubectl, namespace)},
            {"label": "Ingress Resources", "command": "{0} get all,ing -n {1}".format(kubectl, ingress_namespace)},
            {"label": "Kubernetes Nodes", "command": "{0} get nodes -o wide".format(kubectl)},
            {"label": "Helm Version", "command": helm_version_command},
            {"label": "Helm History", "command": "; ".join(helm_history_commands)},
            {"label": "Pod Resource Usage", "command": "{0} top pods -n {1}".format(kubectl, namespace)},
            {"label": "Node Resource Usage", "command": "{0} top nodes".format(kubectl)},
            {"label": "OAA Warning Events", "command": "{0} get events -n {1}".format(kubectl, namespace)},
            {"label": "OAA Helm Releases", "command": helm_command or "{0} exec -n {1} <mgmt-pod> -- helm list -n {1}".format(kubectl, namespace)},
            {"label": "OAA Management Status", "command": installation_status_command or "{0} exec -n {1} <mgmt-pod> -- cat /u01/oracle/logs/status.info".format(kubectl, namespace)},
            {"label": "OAA Install Configuration", "command": install_config_command or "{0} exec -n {1} <mgmt-pod> -- cat ~/scripts/settings/installOAA.properties".format(kubectl, namespace)},
            {"label": "OAA Properties", "command": property_command or "curl -k -u <runtime-user>:<password> <OAAService>/config/property/v1?propertyName=*"},
        ],
        "pods": pods,
        "podCount": len(pods),
        "readyPodCount": len(ready_pods),
        "runningPodCount": len(running_pods),
        "restartCount": restart_count,
        "podsCommand": pods_command,
        "podsJsonCommand": pods_json_command,
        "podsError": pods_error,
        "ingressResources": ingress_resources,
        "ingressResourceCount": len(ingress_resources),
        "ingressCommand": ingress_command,
        "ingressError": ingress_error,
        "namespaceResources": namespace_resources,
        "namespaceResourceCount": len(namespace_resources),
        "namespaceResourcesCommand": namespace_resources_command,
        "namespaceResourcesError": namespace_resources_error,
        "nodeInventory": node_inventory,
        "nodeCount": len(node_inventory),
        "nodesCommand": nodes_command,
        "nodesJsonCommand": nodes_json_command,
        "nodesError": nodes_error,
        "helmVersion": helm_version,
        "helmVersionCommand": helm_version_command,
        "helmVersionError": helm_version_error,
        "helmHistory": helm_history,
        "helmHistoryCommand": "; ".join(helm_history_commands),
        "helmHistoryError": "; ".join(helm_history_errors),
        "helmHistoryWarnings": helm_history_warnings,
        "podResourceUsage": pod_resource_usage,
        "podUsageCommand": pod_usage_command,
        "podUsageError": pod_usage_error,
        "nodeResourceUsage": node_resource_usage,
        "nodeUsageCommand": node_usage_command,
        "nodeUsageError": node_usage_error,
        "warningEvents": warning_events,
        "warningEventCount": len(warning_events),
        "eventsCommand": events_command,
        "eventsError": events_error,
        "describePodName": describe_pod_name,
        "describeCommand": describe_command,
        "describeOutput": describe_output,
        "describeError": describe_error,
        "logPodName": log_pod_name,
        "logCommand": log_command,
        "logOutput": log_output,
        "logError": log_error,
        "managementPodName": mgmt_pod_name,
        "deploymentDetailsCommand": deployment_details_command,
        "deploymentDetails": deployment_details,
        "deploymentDetailsError": deployment_details_error,
        "installConfigCommand": install_config_command,
        "installConfig": install_config,
        "installConfigCount": len(install_config),
        "installConfigError": install_config_error,
        "installConfigSource": "host" if install_config and host_install_config else ("managementPod" if install_config else ""),
        "database": install_database,
        "schemaTables": [],
        "schemaTableSummary": {},
        "schemaTablesError": "",
        "installationStatusCommand": installation_status_command,
        "installationStatus": installation_status,
        "installationStatusError": installation_status_error,
        "managementLogCommand": management_log_command,
        "managementLogOutput": management_log_output,
        "managementLogError": management_log_error,
        "helmCommand": helm_command,
        "helmReleases": helm_releases,
        "helmError": helm_error,
        "certificates": certificates,
        "certificateCount": len(certificates),
        "certificateError": certificate_error,
        "runtimeBaseUrl": runtime_base_url,
        "runtimeUsername": runtime_username,
        "propertyCommand": property_command,
        "properties": properties,
        "propertyCount": len(properties),
        "propertiesError": properties_error,
        "specificProperties": specific_properties,
        "specificPropertyNames": [str(item or "").strip() for item in property_names if str(item or "").strip()],
    }


def strip_xml_namespace(tag):
    text = str(tag or "")
    if "}" in text:
        text = text.rsplit("}", 1)[-1]
    return text


OAM_CONFIG_GROUP_KEYS = [
    "webgates",
    "ldapStores",
    "sessionManagement",
    "policies",
    "passwordPolicy",
    "commonSettings",
    "plugins",
    "certificateValidation",
    "wlstCommands",
]


def oam_config_category(text):
    lowered = str(text or "").lower()
    categories = []
    if any(token in lowered for token in ("webgate", "web gate", "accessclient", "access client", "sso agent", "ssoagent", "agent", "hostidentifier", "host identifier", "iamsuiteagent", "ohs")):
        categories.append("webgates")
    if any(token in lowered for token in ("ldap", "identitystore", "identity store", "useridentifystore", "user identity store", "ids profile", "idsprofile", "directory store", "datastore", "repository", "bind dn", "search base")):
        categories.append("ldapStores")
    if any(token in lowered for token in ("session", "timeout", "cookie", "sso", "token", "idle", "lifetime")):
        categories.append("sessionManagement")
    if any(token in lowered for token in ("applicationdomain", "application domain", "resource", "authentication scheme", "authorization", "authn", "authz", "protected resource")):
        categories.append("policies")
    if ("password" in lowered or "pwd" in lowered) and any(token in lowered for token in ("policy", "validation", "lockout", "attempt", "expire", "uppercase", "lowercase", "alphanumeric", "special", "unicode", "history", "minimum", "maximum", "length", "characters")):
        categories.append("passwordPolicy")
    if any(token in lowered for token in ("common settings", "commonsetting", "audit", "filter preset", "maximum search", "default store", "system store", "common service")):
        categories.append("commonSettings")
    if any(token in lowered for token in ("plugin", "plug-in", "authentication module", "onfailure", "onsuccess", "stepname", "module")):
        categories.append("plugins")
    if any(token in lowered for token in ("certificate", "cert", "keystore", "trust", "x509", "ocsp", "crl", "validation")):
        categories.append("certificateValidation")
    return categories


OAM_GENERIC_ENTITY_TOKENS = {
    "",
    "accessmanager",
    "access manager",
    "agent",
    "agents",
    "common",
    "configuration",
    "config",
    "deployedcomponent",
    "deployed component",
    "global",
    "instance",
    "map",
    "name",
    "ngamconfiguration",
    "profile",
    "profiles",
    "property",
    "setting",
    "settings",
    "string",
    "type",
    "value",
}


def oam_path_tokens(path):
    return [token.strip() for token in re.findall(r"\[([^\]]+)\]", str(path or "")) if token.strip()]


def oam_clean_entity_token(token):
    text = str(token or "").strip().strip("'\"")
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    lowered = text.lower()
    if lowered in OAM_GENERIC_ENTITY_TOKENS:
        return ""
    if lowered.startswith(("configuration/", "setting/")):
        return ""
    if any(secret in lowered for secret in ("password", "passwd", "credential", "secret", "keypass", "trustpass")):
        return ""
    return text[:120]


def oam_preferred_entity(category, tokens, fallback):
    cleaned = []
    for token in tokens:
        item = oam_clean_entity_token(token)
        if item and item not in cleaned:
            cleaned.append(item)
    if not cleaned:
        return oam_clean_entity_token(fallback) or "OAM Setting"

    lowered_pairs = [(item, item.lower()) for item in cleaned]
    if category == "ldapStores":
        for item, lowered in lowered_pairs:
            if (
                "identitystore" in lowered
                or "identity store" in lowered
                or lowered.startswith(("oid", "ldap"))
                or lowered.endswith("store")
            ):
                return item
    if category == "webgates":
        for item, lowered in lowered_pairs:
            if lowered not in ("opensso", "osso", "otherpartners") and any(token in lowered for token in ("webgate", "agent", "accessgate", "ohs", "iam")):
                return item
        for item, lowered in lowered_pairs:
            if lowered not in ("opensso", "osso", "otherpartners"):
                return item
    if category == "sessionManagement":
        for item, lowered in lowered_pairs:
            if any(token in lowered for token in ("session", "cookie", "timeout", "token", "sso")):
                return item
        return "Session Management"
    if category == "passwordPolicy":
        for item, lowered in lowered_pairs:
            if "password" in lowered or "pwd" in lowered:
                return item
        return "Password Policy"
    if category == "commonSettings":
        for item, lowered in lowered_pairs:
            if any(token in lowered for token in ("common", "audit", "store", "session")):
                return item
        return "Common Settings"
    if category == "plugins":
        for item, lowered in lowered_pairs:
            if any(token in lowered for token in ("plugin", "module", "step", "success", "failure")):
                return item
        return cleaned[-1]
    if category == "certificateValidation":
        for item, lowered in lowered_pairs:
            if any(token in lowered for token in ("cert", "keystore", "trust", "ocsp", "crl")):
                return item
        return "Certificate Validation"
    if category == "policies":
        for item, lowered in lowered_pairs:
            if any(token in lowered for token in ("domain", "resource", "policy", "scheme", "authorization", "authentication")):
                return item
        return cleaned[-1]
    return cleaned[-1]


def oam_config_entity(category, path, label_stack, logical_name, attribute):
    tokens = []
    tokens.extend(label_stack or [])
    tokens.extend(oam_path_tokens(path))
    tokens.extend([part.strip() for part in str(logical_name or "").split("/") if part.strip()])
    tokens.append(attribute)
    return oam_preferred_entity(category, tokens, logical_name)


def oam_config_section(path, label_stack, entity):
    entity_lower = str(entity or "").strip().lower()
    tokens = []
    tokens.extend(label_stack or [])
    tokens.extend(oam_path_tokens(path))
    for token in reversed(tokens):
        cleaned = oam_clean_entity_token(token)
        if cleaned and cleaned.lower() != entity_lower:
            return cleaned
    return entity or "OAM Setting"


def parse_oam_config_xml(text, config_file=""):
    groups = {key: [] for key in OAM_CONFIG_GROUP_KEYS}
    summary = {
        "webgateRows": 0,
        "ldapStoreRows": 0,
        "identityStoreRows": 0,
        "sessionRows": 0,
        "policyRows": 0,
        "passwordPolicyRows": 0,
        "commonSettingRows": 0,
        "pluginRows": 0,
        "certificateValidationRows": 0,
        "wlstRows": 0,
    }
    xml_text = str(text or "").strip()
    if not xml_text:
        return {"configFile": config_file, "groups": groups, "summary": summary, "error": "oam-config.xml output was empty."}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {"configFile": config_file, "groups": groups, "summary": summary, "error": "Unable to parse oam-config.xml: {0}".format(exc)}

    def normalized_value(value):
        value = str(value or "").strip()
        value = " ".join(value.split())
        return value

    def display_attribute(attribute):
        text = str(attribute or "").strip()
        if not text:
            return "Value"
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).replace("_", " ").replace("-", " ")
        return " ".join(part.capitalize() if part.isupper() else part for part in text.split())

    def sensitive_field(path, label, attribute):
        lowered = " ".join([str(path or ""), str(label or ""), str(attribute or "")]).lower()
        return any(token in lowered for token in ("ldappassword", "password", "credential", "secret", "keypass", "trustpass", "wallet password"))

    def meaningful_label(label, tag):
        text = normalized_value(label)
        if not text:
            return ""
        if text.lower() in ("setting", "settings", "configuration", "config", "property", "properties", "string", "integer", "boolean", str(tag or "").lower()):
            return ""
        return text[:120]

    def add_row(category, path, label, attribute, value, label_stack=None):
        value = normalized_value(value)
        if not value:
            return
        if sensitive_field(path, label, attribute) and category != "passwordPolicy":
            return
        if len(value) > 1200:
            value = value[:1200] + "..."
        if not label:
            label = path.rsplit("/", 1)[-1] or "-"
        displayed_attribute = display_attribute(attribute)
        entity = oam_config_entity(category, path, label_stack or [], label, attribute)
        section = oam_config_section(path, label_stack or [], entity)
        row_key = (path, entity, displayed_attribute, value)
        existing = groups.get(category) or []
        if any((item.get("path"), item.get("entity") or item.get("name"), item.get("attribute"), item.get("value")) == row_key for item in existing[-25:]):
            return
        if len(groups[category]) >= 250:
            return
        groups[category].append({
            "path": path,
            "name": label or path.rsplit("/", 1)[-1] or "-",
            "entity": entity,
            "section": section,
            "attribute": displayed_attribute,
            "value": value,
        })

    def walk(element, path, label_stack):
        tag = strip_xml_namespace(element.tag)
        attrs = dict(element.attrib or {})
        raw_label = (
            attrs.get("Name")
            or attrs.get("name")
            or attrs.get("DisplayName")
            or attrs.get("displayName")
            or attrs.get("id")
            or attrs.get("ID")
            or attrs.get("ref")
            or attrs.get("Reference")
            or tag
        )
        friendly_label = meaningful_label(raw_label, tag)
        path_part = tag if not friendly_label else "{0}[{1}]".format(tag, friendly_label)
        current_path = "{0}/{1}".format(path, path_part) if path else path_part
        attrs = dict(element.attrib or {})
        element_text = normalized_value(element.text)
        current_stack = list(label_stack)
        if friendly_label:
            current_stack.append(friendly_label)
        logical_name = " / ".join(current_stack[-3:]) or friendly_label or tag
        base_haystack = " ".join([current_path, tag, logical_name, element_text] + ["{0} {1}".format(k, v) for k, v in attrs.items()])
        categories = oam_config_category(base_haystack)
        for category in categories:
            for key, value in attrs.items():
                if str(key or "").lower() in ("xmlns", "schemalocation"):
                    continue
                if str(key or "").lower() == "type" and str(value or "").lower() in ("string", "integer", "boolean", "long", "list", "map"):
                    continue
                add_row(category, current_path, logical_name, key, value, current_stack)
            if element_text and len(element_text) < 500:
                add_row(category, current_path, logical_name, "Value", element_text, current_stack)
        for child in list(element):
            walk(child, current_path, current_stack)

    walk(root, "", [])
    summary["webgateRows"] = len(groups["webgates"])
    summary["ldapStoreRows"] = len(groups["ldapStores"])
    summary["identityStoreRows"] = len(groups["ldapStores"])
    summary["sessionRows"] = len(groups["sessionManagement"])
    summary["policyRows"] = len(groups["policies"])
    summary["passwordPolicyRows"] = len(groups["passwordPolicy"])
    summary["commonSettingRows"] = len(groups["commonSettings"])
    summary["pluginRows"] = len(groups["plugins"])
    summary["certificateValidationRows"] = len(groups["certificateValidation"])
    summary["wlstRows"] = len(groups["wlstCommands"])
    return {"configFile": config_file, "groups": groups, "summary": summary, "error": ""}


def oam_sanitized_value(path, name, value):
    text = str(value or "").strip()
    lowered = " ".join([str(path or ""), str(name or "")]).lower()
    safe_password_setting = any(token in lowered for token in (
        "credentialvalidityinterval",
        "validate_password",
        "passwordpolicy",
        "password policy",
        "password validation",
        "lockout",
        "attempt",
        "expiry",
        "expire",
        "minimum",
        "maximum",
        "/pwd",
    ))
    sensitive_password = "password" in lowered and not safe_password_setting
    sensitive_credential = "credential" in lowered and "credentialvalidityinterval" not in lowered
    if any(token in lowered for token in ("ldappassword", "secret", "keypass", "trustpass", "apikey", "tokenkey")) or sensitive_password or sensitive_credential:
        return "******" if text else ""
    return " ".join(text.split())


def parse_oam_setting_paths(text, config_file=""):
    xml_text = str(text or "").strip()
    result = {"configFile": config_file, "rows": [], "pathValues": {}, "error": ""}
    if not xml_text:
        result["error"] = "oam-config.xml output was empty."
        return result
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        result["error"] = "Unable to parse oam-config.xml paths: {0}".format(exc)
        return result

    def is_setting(element):
        return strip_xml_namespace(element.tag).lower() == "setting"

    def setting_name(element):
        attrs = element.attrib or {}
        name = attrs.get("Name") or attrs.get("name") or attrs.get("DisplayName") or attrs.get("displayName")
        if name:
            return str(name).strip()
        return strip_xml_namespace(element.tag)

    def add_value(path, name, value):
        value = oam_sanitized_value(path, name, value)
        if not value:
            return
        result["pathValues"].setdefault(path, []).append(value)
        if len(result["rows"]) < 5000:
            result["rows"].append({"path": path, "name": name, "value": value})

    def walk(element, tokens):
        name = setting_name(element) if is_setting(element) else ""
        next_tokens = list(tokens)
        if name and name.lower() != "ngamconfiguration":
            next_tokens.append(name)
        path = "/" + "/".join(next_tokens) if next_tokens else "/"
        text_value = " ".join(str(element.text or "").split())
        if text_value:
            add_value(path, name or path.rsplit("/", 1)[-1], text_value)
        for attr_name, attr_value in (element.attrib or {}).items():
            if str(attr_name).lower() in ("name", "displayname", "xmlns", "schemalocation"):
                continue
            add_value("{0}/@{1}".format(path, attr_name), attr_name, attr_value)
        for child in list(element):
            walk(child, next_tokens)

    walk(root, [])
    return result


def oam_path_first(path_values, exact_path):
    values = path_values.get(exact_path) or []
    for value in values:
        if str(value or "").strip():
            return str(value).strip()
    return ""


def oam_path_first_suffix(path_values, suffix):
    suffix = str(suffix or "").strip().lower()
    for path, values in path_values.items():
        if str(path or "").lower().endswith(suffix):
            for value in values or []:
                if str(value or "").strip():
                    return str(value).strip()
    return ""


def oam_group_paths(path_values, marker):
    marker = str(marker or "").strip("/")
    grouped = {}
    marker_text = "/" + marker + "/"
    for path, values in path_values.items():
        if marker_text not in path:
            continue
        remainder = path.split(marker_text, 1)[1].strip("/")
        if not remainder or "/" not in remainder:
            continue
        object_name, attr_path = remainder.split("/", 1)
        value = next((str(value).strip() for value in values or [] if str(value or "").strip()), "")
        if not value:
            continue
        grouped.setdefault(object_name, {})[attr_path] = value
    return grouped


def oam_object_value(data, *names):
    data = data or {}
    lowered = {str(key).lower(): value for key, value in data.items()}
    for name in names:
        key = str(name or "").strip()
        if not key:
            continue
        if key in data and str(data.get(key) or "").strip():
            return str(data.get(key)).strip()
        lower_key = key.lower()
        if lower_key in lowered and str(lowered.get(lower_key) or "").strip():
            return str(lowered.get(lower_key)).strip()
        for candidate, value in data.items():
            if str(candidate or "").lower().endswith("/" + lower_key) and str(value or "").strip():
                return str(value).strip()
    return ""


def parse_oam_duration(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+", text):
        return "{0} M".format(text)
    return text


def oam_compact_config_source(path):
    text = str(path or "").strip().strip("/")
    if not text:
        return "OAM config"
    text = unquote(text)
    parts = [part for part in text.split("/") if part]
    ignored = {
        "Configuration",
        "Setting",
        "Settings",
        "DeployedComponent",
        "Server",
        "NGAMServer",
        "Profile",
        "Resource",
        "Value",
    }
    interesting = [part for part in parts if part not in ignored]
    compact = " / ".join(interesting[-4:] or parts[-4:])
    if len(compact) > 80:
        compact = compact[:77].rstrip() + "..."
    return compact if compact else "OAM config"


def parse_oam_ldap_endpoint(value):
    text = str(value or "").strip()
    if not text:
        return {}
    probe = text if "://" in text else ("ldap://" + text if ":" in text else text)
    try:
        parsed = urlparse(probe)
    except Exception:
        parsed = None
    if parsed and parsed.hostname:
        scheme = parsed.scheme or ""
        default_port = "636" if scheme.lower() in ("ldaps", "ssl") else "389"
        return {
            "ldapUrl": text,
            "host": parsed.hostname,
            "port": str(parsed.port or default_port),
            "protocol": scheme,
        }
    match = re.match(r"^([^:/\s]+):(\d{2,5})", text)
    if match:
        return {"ldapUrl": text, "host": match.group(1), "port": match.group(2), "protocol": ""}
    return {"ldapUrl": text}


def oam_useful_config_value(path, value):
    attr = str(path or "").rsplit("/", 1)[-1].strip()
    attr_lower = attr.lower()
    value_text = str(value or "").strip()
    value_lower = value_text.lower()
    if not value_text:
        return False
    if attr_lower in ("@type", "type"):
        return False
    if value_lower in ("htf:map", "htf:list", "xsd:string", "xsd:boolean", "xsd:int", "xsd:integer"):
        return False
    return True


def oam_password_policy_area(path):
    path = str(path or "")
    if "/UserProfile/UserProfileInstance/" in path:
        match = re.search(r"/UserProfile/UserProfileInstance/([^/]+)/PasswordPolicyAttributes/", path)
        if match:
            return "{0} password attributes".format(match.group(1))
    if "/UserProfile/PasswordPolicyAttributes/" in path:
        return "Global password attributes"
    if "/IdentityServiceProviderConfiguration/" in path:
        return "Identity service lockout"
    match = re.search(r"/CompositeModules/([^/]+)/Steps/\d+/Parameters/", path)
    if match:
        return match.group(1)
    match = re.search(r"/RestServices/(PasswordPolicyMgmt[^/]+)/", path)
    if match:
        return match.group(1)
    return "Password policy"


def oam_password_policy_label(attribute):
    attr = str(attribute or "").strip()
    labels = {
        "POLICY_SCHEMA": "Policy schema",
        "NEW_USERPSWD_BEHAVIOR": "New user password behavior",
        "NEW_USERCHALLENGES_BEHAVIOR": "New user challenge behavior",
        "KEY_IDENTITY_STORE_REF": "Identity store reference",
        "KEY_SEARCH_BASE_URL": "Search base",
        "KEY_LDAP_FILTER": "LDAP filter",
        "PLUGIN_EXECUTION_MODE": "Plug-in execution mode",
        "DISABLED_STATUS_SUPPORT": "Disabled account status support",
        "CHALLENGES_SUPPORTED": "Challenge support",
        "OBJECTCLASS_EXTENSION_SUPPORTED": "Object class extension support",
        "URL_ACTION": "Password flow action",
        "URL_REDIRECT": "Password redirect URL",
        "USER_ACCOUNT_LOCKED": "User account locked flag",
        "PASSWORD_EXPIRED": "Password expired flag",
        "USER_ACCOUNT_DISABLED": "User account disabled flag",
        "FORCED_PASSWORD_CHANGE": "Forced password change flag",
        "TENANT_DISABLED": "Tenant disabled flag",
        "LockoutAttempts": "Lockout attempts",
        "LockoutDurationSeconds": "Lockout duration seconds",
        "Enabled": "Enabled",
        "RequireAuthorizationHeader": "Require authorization header",
        "ServerType": "Server type",
        "ServiceContext": "Service context",
        "UserStore": "User store",
    }
    return labels.get(attr, attr.replace("_", " ").strip().title() if attr.isupper() else attr)


def oam_password_policy_row(path, value):
    path = str(path or "")
    value_text = str(value or "").strip()
    if not value_text:
        return None
    attr = path.rsplit("/", 1)[-1]
    if not oam_useful_config_value(attr, value_text):
        return None
    lowered = path.lower()
    if any(token in lowered for token in ("ldappassword", "passwordsecret", "credential", "trustpass", "keypass")):
        return None
    module_match = re.search(r"/CompositeModules/([^/]+)/", path)
    module_name = module_match.group(1) if module_match else ""
    module_lower = module_name.lower()
    allowed_parameter_attrs = {
        "POLICY_SCHEMA",
        "NEW_USERPSWD_BEHAVIOR",
        "NEW_USERCHALLENGES_BEHAVIOR",
        "KEY_IDENTITY_STORE_REF",
        "KEY_SEARCH_BASE_URL",
        "KEY_LDAP_FILTER",
        "PLUGIN_EXECUTION_MODE",
        "DISABLED_STATUS_SUPPORT",
        "CHALLENGES_SUPPORTED",
        "OBJECTCLASS_EXTENSION_SUPPORTED",
        "URL_ACTION",
        "URL_REDIRECT",
    }
    allowed_rest_attrs = {"Enabled", "RequireAuthorizationHeader", "ServerType", "ServiceContext", "UserStore"}
    include = False
    if "/passwordpolicyattributes/" in lowered:
        include = "/userprofile/userprofileinstance/" in lowered and value_text.lower() not in ("boolean", "xsd:boolean")
    elif "/identityserviceconfiguration/identityserviceproviderconfiguration/" in lowered and attr in ("LockoutAttempts", "LockoutDurationSeconds"):
        include = True
    elif (
        "/compositemodules/" in lowered
        and "/parameters/" in lowered
        and attr in allowed_parameter_attrs
        and ("password policy" in module_lower or "passwordpolicy" in module_lower)
    ):
        include = True
    elif "/restservices/passwordpolicymgmt" in lowered and attr in allowed_rest_attrs:
        include = True
    if not include:
        return None
    display_value = oam_sanitized_value(path, attr, value_text)
    if not display_value or str(display_value).lower() in ("htf:map", "htf:list", "xsd:string", "xsd:boolean", "xsd:int", "xsd:integer"):
        return None
    return {
        "name": oam_password_policy_area(path),
        "attribute": oam_password_policy_label(attr),
        "value": display_value,
    }


def extract_oam_ports(path_values):
    ports = {}
    skip_context_tokens = (
        "creationdate",
        "createdate",
        "modifieddate",
        "updatedate",
        "timestamp",
        "expiry",
        "expiration",
        "timeout",
        "validityinterval",
    )
    for path, values in path_values.items():
        lowered = str(path or "").lower()
        if any(token in lowered for token in skip_context_tokens):
            continue
        field_name = lowered.rsplit("/", 1)[-1]
        explicit_port_field = "port" in lowered
        url_like_field = any(token in field_name for token in ("url", "uri", "endpoint", "location", "server", "host", "provider"))
        for value in values or []:
            text = str(value or "").strip()
            candidates = []
            if re.fullmatch(r"\d{1,5}", text) and explicit_port_field:
                candidates.append(text)
            if url_like_field or explicit_port_field:
                candidates.extend(re.findall(r":(\d{2,5})(?:/|$|[,\"'\]\s])", text))
            for candidate in candidates:
                try:
                    number = int(candidate)
                except (TypeError, ValueError):
                    continue
                if number <= 0 or number > 65535:
                    continue
                ports.setdefault(str(number), set()).add(str(path or "OAM config").strip())
    rows = []
    for port in sorted(ports, key=lambda item: int(item)):
        sources = sorted(ports[port])
        compact_sources = []
        for source in sources:
            compact = oam_compact_config_source(source)
            if compact and compact not in compact_sources:
                compact_sources.append(compact)
        first_source = compact_sources[0] if compact_sources else "OAM config"
        if len(compact_sources) > 1:
            source_summary = "{0} + {1} more".format(first_source, len(compact_sources) - 1)
        else:
            source_summary = first_source
        rows.append({
            "port": port,
            "source": first_source,
            "sourceSummary": source_summary,
            "sourceCount": len(sources),
            "sources": sources[:50],
        })
    return rows


def merge_oam_identity_stores(identity_stores):
    merged = []
    index = {}

    def identity_key(store):
        endpoint = str(store.get("ldapUrl") or "").strip().lower()
        if not endpoint:
            endpoint = "{0}:{1}".format(store.get("host") or "", store.get("port") or "").strip(":").lower()
        bind_dn = str(store.get("bindDn") or "").strip().lower()
        name = str(store.get("name") or store.get("module") or "").strip().lower()
        if endpoint:
            return (endpoint, bind_dn)
        return (name, bind_dn)

    def merge_list(existing, incoming, field):
        seen = {
            tuple(sorted((item or {}).items())) if isinstance(item, dict) else str(item)
            for item in existing.get(field) or []
        }
        combined = list(existing.get(field) or [])
        for item in incoming.get(field) or []:
            key = tuple(sorted((item or {}).items())) if isinstance(item, dict) else str(item)
            if key in seen:
                continue
            seen.add(key)
            combined.append(item)
        existing[field] = combined

    for store in identity_stores or []:
        if not store:
            continue
        key = identity_key(store)
        if key not in index:
            copy = dict(store)
            copy["rawFields"] = list(store.get("rawFields") or [])
            copy["references"] = list(store.get("references") or [])
            index[key] = copy
            merged.append(copy)
            continue
        existing = index[key]
        for field, value in store.items():
            if field in ("rawFields", "references"):
                continue
            if field == "module" and value and value != existing.get("module"):
                modules = [part.strip() for part in str(existing.get("module") or "").split(",") if part.strip()]
                if str(value).strip() not in modules:
                    modules.append(str(value).strip())
                existing["module"] = ", ".join(modules)
                continue
            if not str(existing.get(field) or "").strip() and str(value or "").strip():
                existing[field] = value
        merge_list(existing, store, "rawFields")
        merge_list(existing, store, "references")
    return merged


def extract_oam_curated_config(flat_paths):
    path_values = (flat_paths or {}).get("pathValues") or {}
    summary = {}

    session_base = "/DeployedComponent/Server/NGAMServer/Profile/Sme/SessionConfigurations"
    session_fields = [
        ("Timeout", "Idle Timeout"),
        ("MaxSessionsPerUser", "Maximum Sessions Per User"),
        ("Expiry", "Session Lifetime"),
        ("CredentialValidityInterval", "Credential Validity Interval"),
    ]
    session_config = []
    for key, label in session_fields:
        value = oam_path_first(path_values, "{0}/{1}".format(session_base, key)) or oam_path_first_suffix(path_values, "/SessionConfigurations/{0}".format(key))
        if value:
            display_value = value if key == "MaxSessionsPerUser" else parse_oam_duration(value)
            session_config.append({"name": label, "attribute": key, "value": display_value})

    server_objects = oam_group_paths(path_values, "DeployedComponent/Server/NGAMServer/Instance")
    servers = []
    for name, data in sorted(server_objects.items()):
        if not name:
            continue
        servers.append({
            "name": name,
            "host": oam_object_value(data, "host", "Host"),
            "port": oam_object_value(data, "port", "Port"),
            "accessPort": oam_object_value(data, "AccessServerConfig/Port", "AccessServerConfig/port"),
            "siteName": oam_object_value(data, "SiteName", "siteName"),
            "coherencePort": oam_object_value(data, "CoherenceConfiguration/LocalPort/Value", "LocalPort/Value"),
        })

    webgate_objects = oam_group_paths(path_values, "DeployedComponent/Agent/WebGate/Instance")
    webgates = []
    for name, data in sorted(webgate_objects.items()):
        primary_servers = []
        for key, value in data.items():
            match = re.match(r"PrimaryServerList/([^/]+)/(.+)$", key)
            if not match:
                continue
            index, attr = match.groups()
            while len(primary_servers) <= int(index) if str(index).isdigit() else False:
                primary_servers.append({})
            if str(index).isdigit():
                primary_servers[int(index)][attr] = value
        webgates.append({
            "name": name,
            "state": oam_object_value(data, "state"),
            "preferredHost": oam_object_value(data, "preferredHost", "PreferredHost"),
            "security": oam_object_value(data, "security"),
            "version": oam_object_value(data, "version"),
            "idleTimeout": parse_oam_duration(oam_object_value(data, "idleSessionTimeout", "IdleSessionTimeout")),
            "maxSessionTime": parse_oam_duration(oam_object_value(data, "maxSessionTime", "MaxSessionTime")),
            "cookieSessionTime": parse_oam_duration(oam_object_value(data, "cookieSessionTime", "CookieSessionTime")),
            "primaryCookieDomain": oam_object_value(data, "primaryCookieDomain"),
            "maxConnections": oam_object_value(data, "maxConnections"),
            "configurationProfile": oam_object_value(data, "ConfigurationProfile"),
            "logoutRedirectUrl": oam_object_value(data, "UserDefinedParameters/logoutRedirectUrl"),
            "restEndpointHost": oam_object_value(data, "UserDefinedParameters/OAMRestEndPointHostName"),
            "restEndpointPort": oam_object_value(data, "UserDefinedParameters/OAMRestEndPointPort"),
            "primaryServers": primary_servers,
        })

    identity_store_objects = {}
    identity_store_sources = {}

    def add_identity_store_field(store_name, attr_path, value, source_path):
        store_name = str(store_name or "").strip()
        attr_path = str(attr_path or "").strip("/")
        value = str(value or "").strip()
        if not (store_name and attr_path and value):
            return
        identity_store_objects.setdefault(store_name, {})[attr_path] = value
        identity_store_sources.setdefault(store_name, set()).add(str(source_path or "").strip())

    for path, values in path_values.items():
        value = next((str(value).strip() for value in values if str(value or "").strip()), "")
        if not value:
            continue
        if "/LDAPModules/" in path:
            remainder = path.split("/LDAPModules/", 1)[1].strip("/")
            if remainder and "/" in remainder:
                name, attr_path = remainder.split("/", 1)
                add_identity_store_field(name, attr_path, value, path)
        if "/Resource/LDAP/" in path:
            remainder = path.split("/Resource/LDAP/", 1)[1].strip("/")
            if remainder and "/" in remainder:
                name, attr_path = remainder.split("/", 1)
                add_identity_store_field(name, attr_path, value, path)
    identity_refs = []
    for path, values in path_values.items():
        lower_path = path.lower()
        if lower_path.endswith("/key_identity_store_ref") or lower_path.endswith("/identitystoreref") or lower_path.endswith("/userstore"):
            for value in values or []:
                if str(value or "").strip():
                    identity_refs.append({"source": oam_compact_config_source(path.rsplit("/", 1)[0]), "store": str(value).strip()})
    identity_stores = []
    resource_keys = {key for key in identity_store_objects if oam_object_value(identity_store_objects.get(key), "LDAP_URL", "LDAP_PROVIDER", "SECURITY_PRINCIPAL")}
    used_resource_keys = set()
    for name, data in sorted(identity_store_objects.items()):
        if name in resource_keys:
            continue
        ldap_id = oam_object_value(data, "ldapid")
        resource_data = identity_store_objects.get(ldap_id) if ldap_id else None
        if resource_data:
            merged = {}
            merged.update(resource_data)
            merged.update(data)
            data = merged
            used_resource_keys.add(ldap_id)
        endpoint = (
            oam_object_value(data, "LDAP_URL", "ldapUrl", "Location", "location")
            or oam_object_value(data, "serverName", "ServerName")
        )
        endpoint_parts = parse_oam_ldap_endpoint(endpoint)
        provider = oam_object_value(data, "LDAP_PROVIDER", "provider", "Type", "StoreType", "UserIdentityProviderType")
        protocol = endpoint_parts.get("protocol") or oam_object_value(data, "ldapProtocol", "protocol")
        port = endpoint_parts.get("port") or oam_object_value(data, "port", "Port")
        if not port:
            port_match = re.search(r":(\d{2,5})", endpoint or "")
            if port_match:
                port = port_match.group(1)
        host = endpoint_parts.get("host") or oam_object_value(data, "serverName", "ServerName")
        credential = oam_object_value(data, "SECURITY_CREDENTIAL", "SecurityCredential", "credential", "password")
        references = [
            item for item in identity_refs
            if item.get("store") in (
                name,
                ldap_id,
                oam_object_value(data, "Name"),
                oam_object_value(data, "name"),
            )
        ]
        raw_fields = []
        for key, value in sorted(data.items()):
            if key.startswith("_"):
                continue
            attr = key.rsplit("/", 1)[-1]
            if not oam_useful_config_value(attr, value):
                continue
            if any(token in attr.lower() for token in ("credential", "password", "secret")):
                display_value = "Configured" if str(value or "").strip() else ""
            else:
                display_value = oam_sanitized_value(key, attr, value)
            if display_value:
                raw_fields.append({"attribute": attr, "value": display_value})
            if len(raw_fields) >= 30:
                break
        identity_stores.append({
            "name": oam_object_value(data, "Name", "name") or ldap_id or name,
            "module": name,
            "ldapId": ldap_id,
            "storeType": provider,
            "ldapUrl": endpoint_parts.get("ldapUrl") or endpoint,
            "host": host,
            "port": port,
            "serverName": oam_object_value(data, "serverName", "ServerName") or host,
            "protocol": protocol,
            "sslEnabled": oam_object_value(data, "sslEnabled", "EnableSSL") or ("true" if str(protocol).lower() in ("ldaps", "ssl", "sslv3") or str(port) == "636" else ""),
            "bindDn": oam_object_value(data, "SECURITY_PRINCIPAL", "principal", "BindDN", "bindDN"),
            "passwordConfigured": "Yes" if credential else "",
            "rootDirectory": oam_object_value(data, "rootDirectory", "RootDirectory"),
            "userSearchBase": oam_object_value(data, "USER_SEARCH_BASE", "UserSearchBase", "KEY_SEARCH_BASE_URL"),
            "groupSearchBase": oam_object_value(data, "GROUP_SEARCH_BASE", "GroupSearchBase"),
            "groupBaseDn": oam_object_value(data, "groupBaseDN"),
            "loginIdAttribute": oam_object_value(data, "USER_NAME_ATTRIBUTE", "LoginIDAttribute"),
            "userPasswordAttribute": oam_object_value(data, "USER_PASSWORD_ATTRIBUTE", "UserPasswordAttribute"),
            "groupNameAttribute": oam_object_value(data, "GROUP_NAME_ATTR", "GroupNameAttribute"),
            "validatePassword": oam_object_value(data, "VALIDATE_PASSWORD"),
            "domainRealmName": oam_object_value(data, "domainRealmName"),
            "jaasControlFlag": oam_object_value(data, "jaasControlFlag"),
            "isPrimary": oam_object_value(data, "IsPrimary"),
            "isSystem": oam_object_value(data, "IsSystem"),
            "minConnections": oam_object_value(data, "MIN_CONNECTIONS"),
            "maxConnections": oam_object_value(data, "MAX_CONNECTIONS"),
            "rawFields": raw_fields,
            "references": references,
        })
    for resource_key in sorted(resource_keys - used_resource_keys):
        if any(item.get("module") == resource_key for item in identity_stores):
            continue
        data = identity_store_objects.get(resource_key) or {}
        endpoint = oam_object_value(data, "LDAP_URL", "ldapUrl")
        endpoint_parts = parse_oam_ldap_endpoint(endpoint)
        credential = oam_object_value(data, "SECURITY_CREDENTIAL", "credential", "password")
        provider = oam_object_value(data, "LDAP_PROVIDER", "Type", "UserIdentityProviderType")
        identity_stores.append({
            "name": oam_object_value(data, "Name", "name") or resource_key,
            "module": resource_key,
            "storeType": provider,
            "ldapUrl": endpoint_parts.get("ldapUrl") or endpoint,
            "host": endpoint_parts.get("host") or "",
            "port": endpoint_parts.get("port") or "",
            "protocol": endpoint_parts.get("protocol") or "",
            "bindDn": oam_object_value(data, "SECURITY_PRINCIPAL"),
            "passwordConfigured": "Yes" if credential else "",
            "userSearchBase": oam_object_value(data, "USER_SEARCH_BASE"),
            "groupSearchBase": oam_object_value(data, "GROUP_SEARCH_BASE"),
            "loginIdAttribute": oam_object_value(data, "USER_NAME_ATTRIBUTE"),
            "userPasswordAttribute": oam_object_value(data, "USER_PASSWORD_ATTRIBUTE"),
            "groupNameAttribute": oam_object_value(data, "GROUP_NAME_ATTR"),
            "isPrimary": oam_object_value(data, "IsPrimary"),
            "isSystem": oam_object_value(data, "IsSystem"),
            "minConnections": oam_object_value(data, "MIN_CONNECTIONS"),
            "maxConnections": oam_object_value(data, "MAX_CONNECTIONS"),
            "references": [item for item in identity_refs if item.get("store") in (resource_key, oam_object_value(data, "Name", "name"))],
        })
    if not identity_stores and identity_refs:
        for ref in identity_refs:
            identity_stores.append({"name": ref.get("store"), "module": "Reference", "references": [ref]})
    identity_stores = merge_oam_identity_stores(identity_stores)

    password_policy = []
    seen_password_policy = set()
    for path, values in path_values.items():
        value = next((str(value).strip() for value in values if str(value or "").strip()), "")
        row = oam_password_policy_row(path, value)
        if not row:
            continue
        key = (row.get("name"), row.get("attribute"), row.get("value"))
        if key in seen_password_policy:
            continue
        seen_password_policy.add(key)
        password_policy.append(row)
        if len(password_policy) >= 80:
            break

    oauth_services = []
    for path, values in path_values.items():
        if "oauth" not in path.lower():
            continue
        value = next((str(value).strip() for value in values if str(value or "").strip()), "")
        if not value:
            continue
        if any(token in path.lower() for token in ("servicestatus", "name", "domain", "client", "resource", "endpoint", "url")):
            oauth_services.append({
                "name": path.rsplit("/", 2)[-2] if "/" in path else "OAuth",
                "attribute": path.rsplit("/", 1)[-1],
                "value": oam_sanitized_value(path, path.rsplit("/", 1)[-1], value),
            })
            if len(oauth_services) >= 80:
                break

    ports = extract_oam_ports(path_values)
    summary.update({
        "sessionConfigurations": len(session_config),
        "serverInstances": len(servers),
        "webGateItems": len(webgates),
        "identityStores": len(identity_stores),
        "passwordPolicyItems": len(password_policy),
        "oauthConfigRows": len(oauth_services),
        "ports": len(ports),
    })
    return {
        "summary": summary,
        "sessionConfigurations": session_config,
        "servers": servers,
        "webgates": webgates,
        "identityStores": identity_stores,
        "passwordPolicy": password_policy,
        "oauthServices": oauth_services,
        "ports": ports,
    }


def collect_oam_config_metrics(target, oracle_home, domain_home, progress=None):
    oracle_home = str(oracle_home or "").strip().rstrip("/")
    domain_home = str(domain_home or "").strip().rstrip("/")
    if not domain_home:
        return {
            "configFile": "",
            "groups": {},
            "summary": {},
            "error": "OAM DOMAIN_HOME could not be derived, so oam-config.xml was not collected.",
            "command": "Read $DOMAIN_HOME/config/fmwconfig/oam-config.xml",
        }
    command = (
        "set +e\n"
        "OH=__ORACLE_HOME__\n"
        "DH=__DOMAIN_HOME__\n"
        "for candidate in \"$DH/config/fmwconfig/oam-config.xml\" \"$DH/config/fmwconfig/oam-config.xml.bak\"; do\n"
        "  if [ -r \"$candidate\" ]; then\n"
        "    printf 'IAM_MONITORING_OAM_CONFIG=%s\\n' \"$candidate\"\n"
        "    cat \"$candidate\"\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "found=$(find \"$DH\" \"$OH\" -maxdepth 6 -name oam-config.xml -type f -readable -print -quit 2>/dev/null)\n"
        "if [ -n \"$found\" ]; then\n"
        "  printf 'IAM_MONITORING_OAM_CONFIG=%s\\n' \"$found\"\n"
        "  cat \"$found\"\n"
        "  exit 0\n"
        "fi\n"
        "echo \"oam-config.xml was not found under DOMAIN_HOME or ORACLE_HOME.\"\n"
        "exit 1\n"
    ).replace("__ORACLE_HOME__", shlex.quote(oracle_home)).replace("__DOMAIN_HOME__", shlex.quote(domain_home))
    if callable(progress):
        progress("Collecting OAM webgate, LDAP store, session, policy, and certificate-validation settings from oam-config.xml.")
    result = run_target(target, command, timeout=60)
    output = str(result.get("output") or "")
    marker_match = re.search(r"^IAM_MONITORING_OAM_CONFIG=(.+)$", output, re.MULTILINE)
    if not marker_match:
        return {
            "configFile": "",
            "groups": {},
            "summary": {},
            "error": output.strip() or "oam-config.xml collection failed.",
            "command": "Read $DOMAIN_HOME/config/fmwconfig/oam-config.xml",
        }
    config_file = marker_match.group(1).strip()
    xml_text = output[marker_match.end():].strip()
    parsed = parse_oam_config_xml(xml_text, config_file=config_file)
    flat_paths = parse_oam_setting_paths(xml_text, config_file=config_file)
    curated = extract_oam_curated_config(flat_paths)
    parsed["flatPathsError"] = flat_paths.get("error") or ""
    parsed["curated"] = curated
    parsed.setdefault("summary", {}).update(curated.get("summary") or {})
    parsed["command"] = "cat {0}".format(config_file)
    return parsed


def build_oam_wlst_summary_script(admin_username, admin_password, deployment_connect_url):
    commands = [
        ("OAM Servers", "displayOAMServer()"),
        ("OAM Server Configuration", "displayOAMServerConfig()"),
        ("WebGate Agents", "listWebGateAgents()"),
        ("Access Gates", "listAccessGates()"),
        ("User Identity Stores", "displayUserIdentityStore()"),
        ("Authentication Modules", "listAuthenticationModules()"),
        ("Authentication Schemes", "listAuthenticationSchemes()"),
        ("Application Domains", "listApplicationDomains()"),
    ]
    body = (
        "connect('" + python_string_literal(admin_username) + "','" + python_string_literal(admin_password) + "','" + python_string_literal(deployment_connect_url) + "')\n"
        "def safe(value):\n"
        "    if value is None:\n"
        "        return ''\n"
        "    return str(value).replace('\\r', ' ').replace('\\n', ' ').strip()\n"
        "def run_oam_command(label, expression):\n"
        "    print('IAM_MONITORING_OAM_WLST_BEGIN|' + label)\n"
        "    try:\n"
        "        result = eval(expression)\n"
        "        if result is not None:\n"
        "            print(safe(result))\n"
        "    except:\n"
        "        import sys\n"
        "        print('IAM_MONITORING_OAM_WLST_ERROR|' + label + '|' + safe(sys.exc_info()[1]))\n"
        "    print('IAM_MONITORING_OAM_WLST_END|' + label)\n"
    )
    for label, expression in commands:
        body += "run_oam_command('{0}', '{1}')\n".format(python_string_literal(label), python_string_literal(expression))
    body += "disconnect()\n"
    return body


def parse_oam_wlst_sections(text):
    sections = []
    current = None
    for raw_line in lines(text):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("IAM_MONITORING_OAM_WLST_BEGIN|"):
            current = {"title": stripped.split("|", 1)[1].strip(), "lines": [], "errors": []}
            continue
        if stripped.startswith("IAM_MONITORING_OAM_WLST_END|"):
            if current:
                useful = [line for line in current.get("lines", []) if line and not line.startswith("Location changed to")]
                if useful:
                    sections.append({
                        "title": current.get("title") or "OAM WLST",
                        "lines": useful,
                        "errors": current.get("errors") or [],
                    })
            current = None
            continue
        if current is None:
            continue
        if stripped.startswith("IAM_MONITORING_OAM_WLST_ERROR|"):
            parts = stripped.split("|", 2)
            if len(parts) == 3:
                current.setdefault("errors", []).append(parts[2].strip())
            continue
        if stripped.startswith(("Initializing WebLogic", "Welcome to WebLogic", "Connecting to", "Successfully connected", "Disconnected from")):
            continue
        current.setdefault("lines", []).append(stripped)
    return sections


def oam_wlst_sections_to_rows(sections):
    rows = []
    for section in sections or []:
        title = section.get("title") or "OAM WLST"
        for index, line in enumerate(section.get("lines") or [], start=1):
            attribute = "Output"
            value = line
            if "=" in line and len(line.split("=", 1)[0]) < 80:
                attribute, value = [part.strip() for part in line.split("=", 1)]
            elif ":" in line and len(line.split(":", 1)[0]) < 80:
                attribute, value = [part.strip() for part in line.split(":", 1)]
            rows.append({
                "path": "WLST/{0}".format(title),
                "name": title,
                "entity": title,
                "section": "WLST",
                "attribute": attribute or "Line {0}".format(index),
                "value": value or line,
            })
            if len(rows) >= 250:
                return rows
    return rows


def collect_oam_wlst_metrics(target, oracle_home, admin_username, admin_password, admin_url, progress=None):
    oracle_home = str(oracle_home or "").strip().rstrip("/")
    admin_username = str(admin_username or "").strip()
    admin_password = str(admin_password or "").strip()
    deployment_connect_url = normalize_weblogic_connect_url(admin_url)
    if not (oracle_home and admin_username and admin_password and deployment_connect_url):
        return {"sections": [], "rows": [], "error": "OAM WLST collection needs ORACLE_HOME, WebLogic Admin URL, and WebLogic credentials."}
    wlst_path = "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home)
    if callable(progress):
        progress("Collecting a compact OAM WLST summary for servers, agents, identity stores, sessions, and policies.")
    command, result = run_wlst_script(
        target,
        wlst_path,
        build_oam_wlst_summary_script(admin_username, admin_password, deployment_connect_url),
        timeout=180,
    )
    sections = parse_oam_wlst_sections(result.get("output"))
    rows = oam_wlst_sections_to_rows(sections)
    error = ""
    if result.get("exit_code") != 0 and not rows:
        error = str(result.get("output") or "").strip() or "OAM WLST summary did not return usable output."
    return {
        "sections": sections,
        "rows": rows,
        "error": error,
        "command": "Run selected Oracle Access Manager WLST commands through {0}".format(wlst_path),
    }


def collect_oam_port_connections(target, ports, progress=None):
    unique_ports = []
    seen = set()
    for row in ports or []:
        port = str((row or {}).get("port") or "").strip()
        if not port or port in seen:
            continue
        seen.add(port)
        unique_ports.append(port)
    if not unique_ports:
        return {"rows": [], "error": "", "command": "No OAM ports discovered in oam-config.xml."}
    if callable(progress):
        progress("Counting open TCP connections for OAM ports discovered from oam-config.xml.")
    command = (
        "ports=__PORTS__; "
        "out=$(ss -tan 2>/dev/null || netstat -an 2>/dev/null || true); "
        "for p in $ports; do "
        "count=$(printf '%s\n' \"$out\" | awk -v pat=\":$p\" '$0 ~ pat {c++} END{print c+0}'); "
        "listen=$(printf '%s\n' \"$out\" | awk -v pat=\":$p\" '$0 ~ pat && $0 ~ /LISTEN/ {c++} END{print c+0}'); "
        "established=$(printf '%s\n' \"$out\" | awk -v pat=\":$p\" '$0 ~ pat && $0 ~ /(ESTAB|ESTABLISHED)/ {c++} END{print c+0}'); "
        "echo \"IAM_MONITORING_OAM_PORT|$p|$count|$listen|$established\"; "
        "done"
    ).replace("__PORTS__", shlex.quote(" ".join(unique_ports)))
    result = run_target(target, command, timeout=30)
    rows_by_port = {}
    for raw_line in str(result.get("output") or "").splitlines():
        if not raw_line.startswith("IAM_MONITORING_OAM_PORT|"):
            continue
        parts = raw_line.split("|")
        if len(parts) < 5:
            continue
        rows_by_port[parts[1]] = {
            "port": parts[1],
            "openConnections": as_int_safe(parts[2]) or 0,
            "listeningSockets": as_int_safe(parts[3]) or 0,
            "establishedConnections": as_int_safe(parts[4]) or 0,
        }
    rows = []
    metadata_by_port = {str((row or {}).get("port") or ""): (row or {}) for row in ports or []}
    for port in unique_ports:
        row = rows_by_port.get(port, {"port": port, "openConnections": 0, "listeningSockets": 0, "establishedConnections": 0})
        metadata = metadata_by_port.get(port, {})
        row["source"] = metadata.get("source") or ""
        row["sourceSummary"] = metadata.get("sourceSummary") or metadata.get("source") or "OAM config"
        row["sourceCount"] = metadata.get("sourceCount") or len(metadata.get("sources") or [])
        row["sources"] = metadata.get("sources") or []
        rows.append(row)
    error = ""
    if result.get("exit_code") != 0 and not rows_by_port:
        error = str(result.get("output") or "").strip() or "Open connection count failed."
    return {"rows": rows, "error": error, "command": "ss -tan or netstat -an filtered by OAM config ports"}


def oam_admin_base_url(admin_url):
    text = str(admin_url or "").strip()
    if not text:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return "{0}://{1}".format(parsed.scheme or "http", parsed.netloc)


def parse_jsonish_list(text):
    text = str(text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except Exception:
        return [item.strip() for item in re.split(r",\s*", text.strip("[]")) if item.strip()]


def oam_regex_value(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, str(text or ""), re.I | re.S)
        if match:
            return " ".join(str(match.group(1) or "").strip().strip('"').split())
    return ""


def parse_oam_oauth_identity_domain(text):
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    token_settings_text = oam_regex_value(raw, [r"TokenSettings\s*-\s*(\[[^\n]+?\])\s*,\s*ConsentPageURL", r"TokenSettings\s*=\s*(\[[^\n]+?\])"])
    return {
        "name": oam_regex_value(raw, [r"Name\s*-\s*([^,]+)", r"name\s*=\s*([^,]+)"]),
        "id": oam_regex_value(raw, [r"Id\s*-\s*([^,]+)", r"\bid\s*=\s*([^,]+)"]),
        "description": oam_regex_value(raw, [r"Description\s*-\s*([^,]+)", r"description\s*=\s*([^,]+)"]),
        "identityProvider": oam_regex_value(raw, [r"Identity Provider\s*-\s*([^,]+)", r"identityProvider\s*=\s*([^,]+)"]),
        "trustStores": parse_jsonish_list(oam_regex_value(raw, [r"TrustStore Identifiers\s*-\s*(\[[^\]]*\])"])),
        "tokenSettings": parse_jsonish_list(token_settings_text),
        "consentPageUrl": oam_regex_value(raw, [r"ConsentPageURL\s*-\s*([^,]+)", r"consentPageURL\s*=\s*([^,]+)"]),
        "errorPageUrl": oam_regex_value(raw, [r"ErrorPageURL\s*-\s*([^,]+)", r"errorPageURL\s*=\s*([^,]+)"]),
    }


def parse_oam_oauth_resource(text):
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    scopes_text = oam_regex_value(raw, [r"Scopes=\"(\[[^\n]+?\])\"", r"Scopes\s*=\s*(\[[^\n]+?\])"])
    attrs_text = oam_regex_value(raw, [r"tokenAttributes=\"(\[[^\n]+?\])\"", r"tokenAttributes\s*=\s*(\[[^\n]+?\])"])
    return {
        "identityDomain": oam_regex_value(raw, [r"IdentityDomain=\"?([^,\"]+)", r"identityDomain\s*=\s*([^,]+)"]),
        "name": oam_regex_value(raw, [r"Name=\"?([^,\"]+)", r"\bname\s*=\s*([^,]+)"]),
        "description": oam_regex_value(raw, [r"Description=\"?([^,\"]+)", r"description\s*=\s*([^,]+)"]),
        "resourceServerId": oam_regex_value(raw, [r"resourceServerId=\"?([^,\"]+)", r"resourceServerId\s*=\s*([^,]+)"]),
        "namespacePrefix": oam_regex_value(raw, [r"resourceServerNameSpacePrefix=\"?([^,\"]+)", r"namespacePrefix\s*=\s*([^,]+)"]),
        "type": oam_regex_value(raw, [r"resServerType=\"?([^,\"]+)", r"resServerType\s*=\s*([^,]+)"]),
        "scopes": parse_jsonish_list(scopes_text),
        "tokenAttributes": parse_jsonish_list(attrs_text),
    }


def parse_oam_oauth_clients(text):
    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("items", "clients", "OAuthClient"):
                if isinstance(parsed.get(key), list):
                    return parsed.get(key)
            return [parsed]
    except Exception:
        pass
    clients = []
    for chunk in re.split(r"OAuth Client\s*-\s*", raw):
        if "uid" not in chunk and "name" not in chunk:
            continue
        clients.append({
            "uid": oam_regex_value(chunk, [r"uid\s*=\s*([^,]+)"]),
            "name": oam_regex_value(chunk, [r"name\s*=\s*([^,]+)"]),
            "id": oam_regex_value(chunk, [r"\bid\s*=\s*([^,]+)"]),
            "identityDomain": oam_regex_value(chunk, [r"identityDomain\s*=\s*([^,]+)"]),
            "description": oam_regex_value(chunk, [r"description\s*=\s*([^,]+)"]),
            "clientType": oam_regex_value(chunk, [r"clientType\s*=\s*([^,]+)"]),
            "grantTypes": parse_jsonish_list(oam_regex_value(chunk, [r"grantTypes\s*=\s*(\[[^\]]*\])"])),
            "scopes": parse_jsonish_list(oam_regex_value(chunk, [r"scopes\s*=\s*(\[[^\]]*\])"])),
            "defaultScope": oam_regex_value(chunk, [r"defaultScope\s*=\s*([^,]+)"]),
            "redirectUris": parse_jsonish_list(oam_regex_value(chunk, [r"redirectURIs\s*=\s*(\[[^\]]*\])"])),
        })
    return clients


def collect_oam_oauth_rest_metrics(target, weblogic, curated, progress=None):
    weblogic = weblogic or {}
    base_url = oam_admin_base_url(weblogic.get("adminUrl") or "")
    username = str(weblogic.get("adminUsername") or "weblogic").strip()
    password = str(weblogic.get("adminPassword") or "").strip()
    if not (base_url and username and password):
        return {"configured": False, "error": "OAuth REST collection needs WebLogic Admin URL and credentials.", "commands": []}
    if callable(progress):
        progress("Collecting OAM OAuth identity-domain, resource-server, and client details through the OAM REST API.")
    domain_candidates = []
    for row in (curated or {}).get("oauthServices") or []:
        value = str(row.get("value") or "").strip()
        if value and "domain" in str(row.get("attribute") or "").lower():
            domain_candidates.append(value)
    domain_candidates.extend(["OAADomain", "OAuthOBEDomain", "OAA"])
    domain_candidates = uniqueList(domain_candidates) if "uniqueList" in globals() else list(dict.fromkeys(domain_candidates))
    commands = []
    error = ""

    def call_endpoint(path, safe_label):
        url = "{0}{1}".format(base_url.rstrip("/"), path)
        command = "curl -sS --connect-timeout 10 --max-time 45 -X GET {url} -u {auth} -H 'content-type: application/json'".format(
            url=shlex.quote(url),
            auth=shlex.quote("{0}:{1}".format(username, password)),
        )
        commands.append("curl -X GET {0} -u {1}:******".format(url, username))
        result = run_target(target, command, timeout=60)
        return result

    selected_domain = ""
    identity_domain = {}
    for candidate in domain_candidates[:10]:
        result = call_endpoint(
            "/oam/services/rest/ssa/api/v1/oauthpolicyadmin/oauthidentitydomain?name={0}".format(quote(candidate, safe="")),
            "OAuth identity domain",
        )
        output = str(result.get("output") or "").strip()
        if result.get("exit_code") == 0 and output and not re.search(r"(not found|error|exception|failed)", output, re.I):
            identity_domain = parse_oam_oauth_identity_domain(output)
            selected_domain = candidate
            break
        error = output or error

    resource = {}
    for resource_name in ("OAAResource", "OAAService", "OAA", "OAuthResource"):
        if not selected_domain:
            break
        result = call_endpoint(
            "/oam/services/rest/ssa/api/v1/oauthpolicyadmin/application?identityDomainName={0}&name={1}".format(
                quote(selected_domain, safe=""),
                quote(resource_name, safe=""),
            ),
            "OAuth resource server",
        )
        output = str(result.get("output") or "").strip()
        if result.get("exit_code") == 0 and output and not re.search(r"(not found|error|exception|failed)", output, re.I):
            resource = parse_oam_oauth_resource(output)
            break

    clients = []
    if selected_domain:
        result = call_endpoint(
            "/oam/services/rest/ssa/api/v1/oauthpolicyadmin/client?identityDomainName={0}".format(quote(selected_domain, safe="")),
            "OAuth clients",
        )
        output = str(result.get("output") or "").strip()
        if result.get("exit_code") == 0 and output:
            clients = parse_oam_oauth_clients(output)

    return {
        "configured": True,
        "baseUrl": base_url,
        "domainName": selected_domain,
        "identityDomain": identity_domain,
        "resourceServer": resource,
        "clients": clients,
        "commands": commands,
        "error": "" if selected_domain else (error or "No OAuth identity domain was returned by the OAM REST API."),
        "docUrl": "https://docs.oracle.com/en/middleware/idm/access-manager/12.2.1.4/oroau/op-oam-services-rest-ssa-api-v1-oauthpolicyadmin-oauthidentitydomain-get.html",
    }


def build_oam_keystore_password_script(admin_username, admin_password, deployment_connect_url):
    return (
        "connect('" + python_string_literal(admin_username) + "','" + python_string_literal(admin_password) + "','" + python_string_literal(deployment_connect_url) + "')\n"
        "print('IAM_MONITORING_OAM_KEYPASS_BEGIN')\n"
        "try:\n"
        "    domainRuntime()\n"
        "except:\n"
        "    import sys\n"
        "    print('IAM_MONITORING_OAM_KEYPASS_ERROR|domainRuntime: ' + str(sys.exc_info()[1]))\n"
        "def emit_password(map_name, key_name, value):\n"
        "    try:\n"
        "        if value is None:\n"
        "            return\n"
        "        print('Password|' + str(map_name) + '|' + str(key_name) + '=' + ''.join([str(ch) for ch in value]))\n"
        "    except:\n"
        "        print('Password|' + str(map_name) + '|' + str(key_name) + '=' + str(value))\n"
        "credential_maps = ['OAM_STORE', 'oracle.oam.OAMStore', 'OAMStore', 'oracle.oam', 'oracle.oam.security', 'oam']\n"
        "credential_keys = ['JKS', 'jks', 'amtruststore', 'AMTrustStore', 'amtruststore.password', 'amtruststore.passphrase', '.oamkeystore', 'oamkeystore', 'OAMKeyStore', 'oamkeystore.password', 'oamkeystore.passphrase', 'OAM_KEYSTORE', 'defaulttrustcastore', 'defaulttrustcastorepassword', 'stskeystorepassword', 'stskeystore', 'osts_signing_template', 'osts_encryption_template', 'osts_signing', 'osts_encryption', 'wlsclientsslkeystorepwd', 'clientsslkeystorepwd', 'wlsclientsslkeystore', 'clientsslkeystore', 'jcesignkeystore', 'jceenckeystore', 'jceSignKeyStore', 'jceEncKeyStore', 'keystore', 'KEYSTORE', 'storepass', 'password']\n"
        "for map_name in credential_maps:\n"
        "    for key_name in credential_keys:\n"
        "        try:\n"
        "            print('IAM_MONITORING_OAM_KEYPASS_MAP|' + map_name + '|' + key_name)\n"
        "            listCred(map=map_name, key=key_name)\n"
        "        except:\n"
        "            import sys\n"
        "            print('IAM_MONITORING_OAM_KEYPASS_ERROR|listCred ' + map_name + '/' + key_name + ': ' + str(sys.exc_info()[1]))\n"
        "        try:\n"
        "            cred = getCred(map=map_name, key=key_name)\n"
        "            password = None\n"
        "            try:\n"
        "                password = cred.getPassword()\n"
        "            except:\n"
        "                password = cred\n"
        "            if password is not None:\n"
        "                emit_password(map_name, key_name, password)\n"
        "        except Exception, exc:\n"
            "            import sys\n"
            "            print('IAM_MONITORING_OAM_KEYPASS_ERROR|getCred ' + map_name + '/' + key_name + ': ' + str(sys.exc_info()[1]))\n"
        "print('IAM_MONITORING_OAM_KEYPASS_END')\n"
        "try:\n"
        "    disconnect()\n"
        "except:\n"
        "    pass\n"
    )


def normalize_oam_keystore_password_candidate(value):
    value = str(value or "").strip().strip("\"'")
    if not value:
        return ""
    if set(value) <= {"*"}:
        return ""
    lower = value.lower()
    rejected_exact = {
        "none",
        "null",
        "undefined",
        "false",
        "true",
        "not",
        "password",
        "<password>",
        "{password}",
    }
    if lower in rejected_exact:
        return ""
    rejected_fragments = (
        "not found",
        "not configured",
        "does not exist",
        "exception",
        "weblogicscriptingexception",
        "iam_monitoring_oam_keypass",
    )
    if any(fragment in lower for fragment in rejected_fragments):
        return ""
    return value


def parse_oam_keystore_password_candidates(text):
    capture = False
    captured = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line == "IAM_MONITORING_OAM_KEYPASS_BEGIN":
            capture = True
            continue
        if line == "IAM_MONITORING_OAM_KEYPASS_END":
            capture = False
            continue
        if capture:
            captured.append(line)
    probe = "\n".join(captured)
    candidates = []
    seen = set()
    for pattern in (
        r"(?im)^\s*Password\|[^=]+=\s*(.+?)\s*$",
        r"(?im)^\s*Password\s*[:=]\s*(.+?)\s*$",
        r"(?im)^\s*password\s*[:=]\s*(.+?)\s*$",
        r"(?im)^\s*passphrase\s*[:=]\s*(.+?)\s*$",
    ):
        for match in re.finditer(pattern, probe):
            value = normalize_oam_keystore_password_candidate(match.group(1))
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                candidates.append(value)
    return candidates


def parse_oam_keystore_password_output(text):
    candidates = parse_oam_keystore_password_candidates(text)
    if candidates:
        return candidates[0]
    return ""


def collect_oam_keystore_password(target, oracle_home, admin_username, admin_password, admin_url, progress=None):
    oracle_home = str(oracle_home or "").strip().rstrip("/")
    admin_username = str(admin_username or "").strip()
    admin_password = str(admin_password or "").strip()
    deployment_connect_url = normalize_weblogic_connect_url(admin_url)
    if not (oracle_home and admin_username and admin_password and deployment_connect_url):
        return "", ""
    wlst_path = "{0}/oracle_common/common/bin/wlst.sh".format(oracle_home)
    if callable(progress):
        progress("Reading OAM keystore password from WLST credential store.")
    command, result = run_wlst_script(
        target,
        wlst_path,
        build_oam_keystore_password_script(admin_username, admin_password, deployment_connect_url),
        timeout=120,
    )
    passwords = parse_oam_keystore_password_candidates(result.get("output"))
    if passwords:
        return "\n".join(passwords), ""
    output = str(result.get("output") or "").strip()
    if result.get("exit_code") != 0:
        return "", output or "WLST credential-store lookup failed."
    errors = [line for line in output.splitlines() if "IAM_MONITORING_OAM_KEYPASS_ERROR|" in line]
    if errors:
        return "", "; ".join(line.split("|", 1)[1] for line in errors)
    return "", "WLST credential-store lookup did not return a usable OAM keystore password."


def summarize_oam_keystore_error(name, output_lines):
    name = str(name or "OAM keystore").strip()
    first_error = next((item.strip() for item in output_lines or [] if str(item or "").strip()), "")
    lower = first_error.lower()
    if "invalid keystore format" in lower:
        return "{0} was found, but it is not readable by keytool with the tested store types.".format(name), first_error
    if "keystore was tampered" in lower or "password was incorrect" in lower or "keystore password was incorrect" in lower:
        return "{0} was found, but the OAM keystore password was not resolved from the WebLogic credential store or the resolved password was not accepted by keytool.".format(name), first_error
    if "not found" in lower:
        return "{0} was not found under the OAM domain or ORACLE_HOME.".format(name), first_error
    if first_error:
        return first_error[:220], first_error
    return "Certificate entries could not be read from {0}. Keystore password or store type may be required.".format(name), ""


def collect_oam_keystore_certificates(target, oracle_home, domain_home, admin_username="", admin_password="", admin_url="", progress=None):
    oracle_home = str(oracle_home or "").strip().rstrip("/")
    domain_home = str(domain_home or "").strip().rstrip("/")
    if not (oracle_home or domain_home):
        return [], "OAM ORACLE_HOME or DOMAIN_HOME is required for amtruststore and .oamkeystore discovery."
    if callable(progress):
        progress("Discovering OAM amtruststore and .oamkeystore certificate entries.")
    credential_store_passwords, credential_store_error = collect_oam_keystore_password(
        target,
        oracle_home,
        admin_username,
        admin_password,
        admin_url,
        progress=progress,
    )
    command = (
        "OH=__ORACLE_HOME__; DH=__DOMAIN_HOME__; WLST_OAM_STOREPASS_FILE=/tmp/iam-monitoring-oam-storepass-$$.txt; cat >\"$WLST_OAM_STOREPASS_FILE\" <<'IAMOAMSTOREPASS'\n__OAM_STOREPASS__\nIAMOAMSTOREPASS\n"
        "find_keytool(){ for candidate in \"$OH/oracle_common/jdk/bin/keytool\" \"$OH/jdk/bin/keytool\" \"$(dirname \"$OH\")/jdk/bin/keytool\"; do "
        "if [ -x \"$candidate\" ]; then printf '%s\n' \"$candidate\"; return 0; fi; done; command -v keytool 2>/dev/null || true; }; "
        "KEYTOOL=$(find_keytool); "
        "if [ -z \"$KEYTOOL\" ]; then echo 'OAM_KEYSTORE_ERROR|keytool executable not found'; exit 0; fi; "
        "for name in amtruststore .oamkeystore; do "
        "path=''; "
        "for candidate in \"$DH/config/fmwconfig/$name\" \"$DH/config/fmwconfig/components/OAM/$name\" \"$DH/$name\" \"$OH/$name\"; do "
        "if [ -f \"$candidate\" ]; then path=\"$candidate\"; break; fi; done; "
        "if [ -z \"$path\" ]; then path=$(find \"$DH\" \"$OH\" -maxdepth 8 -type f -name \"$name\" 2>/dev/null | head -n 1); fi; "
        "if [ -z \"$path\" ]; then echo \"OAM_KEYSTORE_MISSING|$name\"; continue; fi; "
        "success=0; last_error=''; "
        "type_tokens='JKS JCEKS PKCS12 NONE'; [ \"$name\" = '.oamkeystore' ] && type_tokens='JCEKS JKS PKCS12 NONE'; "
        "for type_token in $type_tokens; do "
        "for pass_token in WLST_FILE EMPTY changeit welcome1 Welcome1 password DemoIdentityKeyStorePassPhrase DemoTrustKeyStorePassPhrase DemoIdentityPassPhrase; do "
        "storetype_args=''; [ \"$type_token\" != 'NONE' ] && storetype_args=\"-storetype $type_token\"; "
        "if [ \"$pass_token\" = 'WLST_FILE' ]; then [ -s \"$WLST_OAM_STOREPASS_FILE\" ] || continue; "
        "while IFS= read -r storepass || [ -n \"$storepass\" ]; do "
        "[ -n \"$storepass\" ] || continue; "
        "out=\"/tmp/iam-monitoring-oam-keytool-$$-$(printf '%s' \"$name-$type_token-wlst\" | tr -cd 'A-Za-z0-9_.-').out\"; "
        "timeout 25 \"$KEYTOOL\" -list -v -keystore \"$path\" $storetype_args -storepass \"$storepass\" >\"$out\" 2>&1; rc=$?; "
        "if [ \"$rc\" -eq 0 ]; then echo \"OAM_KEYSTORE_BEGIN|$name|$path\"; cat \"$out\" 2>/dev/null || true; echo \"OAM_KEYSTORE_END|$name|0\"; rm -f \"$out\"; success=1; break; fi; "
        "last_error=$(head -n 4 \"$out\" 2>/dev/null | tr '\n' ' '); rm -f \"$out\"; "
        "done <\"$WLST_OAM_STOREPASS_FILE\"; [ \"$success\" -eq 1 ] && break 2; continue; "
        "elif [ \"$pass_token\" = 'EMPTY' ]; then storepass=''; "
        "else storepass=\"$pass_token\"; fi; "
        "out=\"/tmp/iam-monitoring-oam-keytool-$$-$(printf '%s' \"$name-$type_token-$pass_token\" | tr -cd 'A-Za-z0-9_.-').out\"; "
        "timeout 25 \"$KEYTOOL\" -list -v -keystore \"$path\" $storetype_args -storepass \"$storepass\" >\"$out\" 2>&1; rc=$?; "
        "if [ \"$rc\" -eq 0 ]; then "
        "echo \"OAM_KEYSTORE_BEGIN|$name|$path\"; cat \"$out\" 2>/dev/null || true; echo \"OAM_KEYSTORE_END|$name|0\"; rm -f \"$out\"; success=1; break 2; "
        "fi; "
        "last_error=$(head -n 4 \"$out\" 2>/dev/null | tr '\n' ' '); rm -f \"$out\"; "
        "done; done; "
        "if [ \"$success\" -ne 1 ]; then "
        "echo \"OAM_KEYSTORE_BEGIN|$name|$path\"; "
        "echo \"keytool could not read $name with the tested store types/passwords. $last_error\"; "
        "echo \"OAM_KEYSTORE_END|$name|1\"; "
        "fi; "
        "done"
        "; rm -f \"$WLST_OAM_STOREPASS_FILE\""
    ).replace("__ORACLE_HOME__", shlex.quote(oracle_home)).replace("__DOMAIN_HOME__", shlex.quote(domain_home)).replace("__OAM_STOREPASS__", credential_store_passwords)
    result = run_target(target, command, timeout=75)
    rows = []
    errors = []
    current = None
    for raw_line in str(result.get("output") or "").splitlines():
        line = raw_line.rstrip()
        if line.startswith("OAM_KEYSTORE_ERROR|"):
            errors.append(line.split("|", 1)[1])
            continue
        if line.startswith("OAM_KEYSTORE_MISSING|"):
            errors.append("{0}: not found".format(line.split("|", 1)[1]))
            continue
        if line.startswith("OAM_KEYSTORE_BEGIN|"):
            parts = line.split("|", 2)
            current = {"name": parts[1] if len(parts) > 1 else "OAM Keystore", "path": parts[2] if len(parts) > 2 else "", "output": [], "exitCode": 0}
            continue
        if line.startswith("OAM_KEYSTORE_END|"):
            parts = line.split("|")
            if current:
                current["exitCode"] = as_int_safe(parts[2] if len(parts) > 2 else "") or 1
                parsed = parse_keytool_certificates(
                    "\n".join(current.get("output") or []),
                    current.get("name") or "OAM Keystore",
                    current.get("path") or "",
                    "keytool -list -v -keystore {0}".format(current.get("path") or current.get("name") or ""),
                )
                if parsed:
                    for item in parsed:
                        item["source"] = "OAM Keystore"
                    rows.extend(parsed)
                else:
                    message, full_error = summarize_oam_keystore_error(current.get("name"), current.get("output") or [])
                    rows.append({
                        "source": "OAM Keystore",
                        "keystore": current.get("name") or "OAM Keystore",
                        "keystorePath": current.get("path") or "",
                        "alias": "-",
                        "subject": message,
                        "error": full_error,
                        "issuer": "-",
                        "expiresAt": "",
                        "status": "warning",
                        "statusLabel": "Unavailable",
                        "command": "keytool -list -v -keystore {0}".format(current.get("path") or ""),
                    })
                current = None
            continue
        if current is not None:
            current.setdefault("output", []).append(line)
    if credential_store_error:
        errors.append("OAM credential-store password lookup did not return a usable password: {0}".format(credential_store_error))
    if result.get("exit_code") != 0 and not rows:
        errors.append(str(result.get("output") or "").strip() or "OAM keystore certificate collection failed.")
    return rows, "; ".join([item for item in errors if item])


def get_oam_metrics(target, environment, app_checks, weblogic_metrics=None, opatch_future=None, keystore_future=None, progress=None):
    if not (environment.get("products") or {}).get("oam"):
        return None

    oam_checks = [check for check in app_checks if check.get("product") == "oam"]
    weblogic_metrics = weblogic_metrics or {}
    weblogic = effective_weblogic_settings(environment)
    oam = environment.get("oam") or {}
    oracle_home = str(weblogic_metrics.get("oracleHome") or weblogic.get("oracleHome") or oam.get("oracleHome") or "").strip()
    domain_home = str(weblogic_metrics.get("domainHome") or weblogic.get("domainHome") or oam.get("domainHome") or "").strip()
    oam_target = build_weblogic_target(environment, target)
    if callable(progress):
        progress("Running OAM OPatch, certificate, and configuration probes in parallel.")

    def read_opatch():
        if opatch_future is not None:
            try:
                return opatch_future.result()
            except Exception as exc:
                return {"error": str(exc), "versions": [], "products": [], "patches": []}
        return collect_opatch_inventory(oam_target, oracle_home, progress=progress, label="OAM")

    initial_results = run_parallel_tasks([
        ("opatch", read_opatch),
        ("certificates", lambda: collect_ssl_certificates(
            oam_target,
            weblogic_metrics.get("serverInventory") or [],
            oracle_home=oracle_home,
            domain_home=domain_home,
            progress=progress,
            keystore_future=keystore_future,
        )),
        ("config", lambda: collect_oam_config_metrics(oam_target, oracle_home, domain_home, progress=progress)),
    ], max_workers=3)

    opatch = task_result(initial_results, "opatch", {"versions": [], "products": [], "patches": []})
    opatch["recommendation"] = build_fmw_patch_recommendation(opatch, environment, oracle_home)
    opatch["patchComparisonRows"] = opatch["recommendation"].get("comparisonRows", [])
    certificates, certificate_error = task_result(initial_results, "certificates", ([], "OAM certificate collection task failed."))
    config = task_result(initial_results, "config", {"groups": {}, "summary": {}, "curated": {}, "error": "OAM config collection task failed."})
    curated_config = config.get("curated") or {}

    if callable(progress):
        progress("Running OAM port, OAuth REST, WLST, and keystore probes in parallel.")

    secondary_results = run_parallel_tasks([
        ("oam_keystore", lambda: collect_oam_keystore_certificates(
            oam_target,
            oracle_home,
            domain_home,
            admin_username=weblogic.get("adminUsername") or "weblogic",
            admin_password=weblogic.get("adminPassword") or "",
            admin_url=weblogic.get("adminUrl") or "",
            progress=progress,
        )),
        ("ports", lambda: collect_oam_port_connections(
            oam_target,
            curated_config.get("ports") or [],
            progress=progress,
        )),
        ("oauth", lambda: collect_oam_oauth_rest_metrics(
            oam_target,
            weblogic,
            curated_config,
            progress=progress,
        )),
        ("wlst", lambda: collect_oam_wlst_metrics(
            oam_target,
            oracle_home,
            weblogic.get("adminUsername") or "weblogic",
            weblogic.get("adminPassword") or "",
            weblogic.get("adminUrl") or "",
            progress=progress,
        )),
    ], max_workers=4)

    oam_keystore_certificates, oam_keystore_error = task_result(
        secondary_results,
        "oam_keystore",
        ([], "OAM keystore certificate collection task failed."),
    )
    if oam_keystore_certificates:
        certificates.extend(oam_keystore_certificates)
    if oam_keystore_error:
        certificate_error = "; ".join([item for item in (certificate_error, oam_keystore_error) if item])
    config["portConnections"] = task_result(secondary_results, "ports", [])
    config["oauth"] = task_result(secondary_results, "oauth", {"rows": [], "clients": [], "error": "OAM OAuth REST task failed."})
    wlst_summary = task_result(secondary_results, "wlst", {"sections": [], "rows": [], "error": "OAM WLST task failed."})
    if wlst_summary.get("rows"):
        config.setdefault("groups", {})
        config.setdefault("summary", {})
        config["groups"]["wlstCommands"] = wlst_summary.get("rows") or []
        config["summary"]["wlstRows"] = len(wlst_summary.get("rows") or [])
    config["wlst"] = wlst_summary

    return {
        "configured": True,
        "message": "OAM uses the shared WebLogic AdminServer profile. DOMAIN_HOME is derived from domain-registry.xml under ORACLE_HOME when it is not saved.",
        "checks": oam_checks,
        "runtime": weblogic_metrics,
        "oracleHome": oracle_home,
        "domainHome": domain_home,
        "domainHomeSource": weblogic_metrics.get("domainHomeSource") or "",
        "domainRegistryFile": weblogic_metrics.get("domainRegistryFile") or "",
        "domainHomeDiscoveryError": weblogic_metrics.get("domainHomeDiscoveryError") or "",
        "opatch": opatch,
        "certificates": certificates,
        "certificateError": certificate_error,
        "config": config,
    }


def get_oig_metrics(target, environment, app_checks, weblogic_metrics=None, opatch_future=None, keystore_future=None, progress=None):
    if not (environment.get("products") or {}).get("oig"):
        return None

    oig_checks = [check for check in app_checks if check.get("product") == "oig"]
    weblogic_metrics = weblogic_metrics or {}
    weblogic = effective_weblogic_settings(environment)
    oig = environment.get("oig") or {}
    oracle_home = str(weblogic.get("oracleHome") or oig.get("oracleHome") or "").strip()
    domain_home = str(weblogic.get("domainHome") or oig.get("domainHome") or "").strip()
    oig_target = build_weblogic_target(environment, target)
    if opatch_future is not None:
        try:
            opatch = opatch_future.result()
        except Exception as exc:
            opatch = {"error": str(exc), "versions": [], "products": [], "patches": []}
    else:
        opatch = collect_opatch_inventory(oig_target, oracle_home, progress=progress)
    opatch["recommendation"] = build_fmw_patch_recommendation(opatch, environment, oracle_home)
    opatch["patchComparisonRows"] = opatch["recommendation"].get("comparisonRows", [])
    certificates, certificate_error = collect_ssl_certificates(
        oig_target,
        weblogic_metrics.get("serverInventory") or [],
        oracle_home=oracle_home,
        domain_home=domain_home,
        progress=progress,
        keystore_future=keystore_future,
    )

    return {
        "configured": True,
        "message": "OIG/OIM uses the shared WebLogic ORACLE_HOME and DOMAIN_HOME for AdminServer and OIM.",
        "checks": oig_checks,
        "runtime": weblogic_metrics,
        "opatch": opatch,
        "certificates": certificates,
        "certificateError": certificate_error,
        "oimProfile": {
            "adminUsername": oig.get("adminUsername") or "xelsysadm",
            "configured": bool(oig.get("adminUsername") and oig.get("adminPassword")),
            "hasPassword": bool(oig.get("adminPassword")),
        },
    }


def get_product_metrics(target, environment, app_checks, progress=None):
    weblogic_metrics = None
    background_executor = None
    opatch_future = None
    keystore_future = None
    products = environment.get("products") or {}
    if weblogic_profile_configured(environment):
        weblogic = effective_weblogic_settings(environment)
        oig = environment.get("oig") or {}
        oam = environment.get("oam") or {}
        oracle_home = str(weblogic.get("oracleHome") or oig.get("oracleHome") or oam.get("oracleHome") or "").strip()
        domain_home = str(weblogic.get("domainHome") or oig.get("domainHome") or oam.get("domainHome") or "").strip()
        oig_target = build_weblogic_target(environment, target)
        if oracle_home:
            background_executor = ThreadPoolExecutor(max_workers=4)
            opatch_label = "OIG/OIM" if products.get("oig") else ("OAM" if products.get("oam") else "WebLogic")

            path_resolution = resolve_fmw_home_paths(oig_target, oracle_home, domain_home, progress=progress)
            resolved_oracle_home = str(path_resolution.get("oracleHome") or oracle_home).strip()
            resolved_domain_home = str(path_resolution.get("domainHome") or domain_home).strip()
            if path_resolution.get("warning") and callable(progress):
                progress(path_resolution.get("warning"))

            opatch_future = background_executor.submit(
                collect_opatch_inventory,
                oig_target,
                resolved_oracle_home,
                progress,
                opatch_label,
            )
            if resolved_domain_home:
                keystore_target = dict(oig_target)
                keystore_target["sudoRequired"] = False
                keystore_future = background_executor.submit(
                    collect_keystore_certificates,
                    keystore_target,
                    resolved_oracle_home,
                    resolved_domain_home,
                    progress,
                )
    try:
        weblogic_metrics = get_weblogic_metrics(
            target,
            environment,
            opatch_future=opatch_future,
            keystore_future=keystore_future,
            progress=progress,
        )
    except Exception as exc:
        weblogic_metrics = {
            "error": str(exc),
            "expectedServers": (environment.get("weblogic") or {}).get("serverNames") or [],
            "runningServers": 0,
            "missingServers": (environment.get("weblogic") or {}).get("serverNames") or [],
            "servers": [],
        }

    metrics = {
        "weblogic": weblogic_metrics,
        "oam": None,
        "oud": None,
        "oid": None,
        "oaa": None,
        "oig": None,
    }

    def run_product_collector(product_name):
        if product_name == "oam":
            return get_oam_metrics(
                target,
                environment,
                app_checks,
                weblogic_metrics=weblogic_metrics,
                opatch_future=opatch_future,
                keystore_future=keystore_future,
                progress=progress,
            )
        if product_name == "oud":
            return get_oud_metrics(target, environment, progress=progress)
        if product_name == "oid":
            return get_oid_metrics(target, environment, progress=progress)
        if product_name == "oaa":
            return get_oaa_metrics(target, environment, progress=progress)
        if product_name == "oig":
            return get_oig_metrics(
                target,
                environment,
                app_checks,
                weblogic_metrics=weblogic_metrics,
                opatch_future=opatch_future,
                keystore_future=keystore_future,
                progress=progress,
            )
        return None

    error_defaults = {
        "oam": {"runtime": weblogic_metrics, "opatch": {"patches": []}, "certificates": []},
        "oud": {"listeners": [], "backends": []},
        "oid": {"processes": [], "ldapGroups": {}, "opatch": {"patches": []}, "certificates": []},
        "oaa": {"pods": [], "ingressResources": [], "properties": []},
        "oig": {"checks": [], "opatch": {"patches": []}, "certificates": []},
    }
    product_executor = ThreadPoolExecutor(max_workers=5)
    product_futures = {}
    try:
        for product_name in ("oam", "oud", "oid", "oaa", "oig"):
            if products.get(product_name):
                product_futures[product_name] = product_executor.submit(run_product_collector, product_name)
        for product_name, future in product_futures.items():
            try:
                metrics[product_name] = future.result()
            except Exception as exc:
                fallback = dict(error_defaults.get(product_name) or {})
                fallback["error"] = str(exc)
                metrics[product_name] = fallback
    finally:
        product_executor.shutdown(wait=False)
    if background_executor is not None:
        background_executor.shutdown(wait=False)
    return metrics


def product_metrics_status(product_metrics):
    if not product_metrics:
        return "healthy"

    weblogic = product_metrics.get("weblogic") or {}
    if weblogic.get("error"):
        return "warning"
    if weblogic.get("deploymentError"):
        return "warning"

    expected_servers = weblogic.get("expectedServers") or []
    running_servers = weblogic.get("servers") or []
    missing_servers = weblogic.get("missingServers") or []
    if expected_servers and not running_servers:
        return "down"
    if missing_servers:
        return "warning"
    if (weblogic.get("inactiveDeploymentCount") or 0) > 0:
        return "warning"

    oud = product_metrics.get("oud") or {}
    if oud.get("error"):
        return "warning"

    if oud.get("serverRunStatus") and str(oud.get("serverRunStatus")).lower() != "started":
        return "down"

    listeners = oud.get("listeners") or []
    ldap_listeners = [listener for listener in listeners if listener.get("protocol") in ("LDAP", "LDAPS")]
    if ldap_listeners and any(listener.get("state") != "Enabled" for listener in ldap_listeners):
        return "warning"
    if (oud.get("opatch") or {}).get("error"):
        return "warning"
    oud_certificate_statuses = [str(item.get("status") or "").lower() for item in (oud.get("certificates") or [])]
    if "expired" in oud_certificate_statuses:
        return "down"
    if "warning" in oud_certificate_statuses:
        return "warning"

    oid = product_metrics.get("oid") or {}
    if oid.get("error"):
        return "warning"
    if oid.get("enabled") and not oid.get("processCount"):
        return "down"
    if str(oid.get("serverStatusSeverity") or "").lower() == "warning":
        return "warning"
    if (oid.get("opatch") or {}).get("error"):
        return "warning"
    oid_certificate_rows = oid.get("certificates") or []
    oid_certificate_statuses = [str(item.get("status") or "").lower() for item in oid_certificate_rows]
    if "expired" in oid_certificate_statuses:
        return "down"
    if any(
        str(item.get("status") or "").lower() == "warning"
        and str(item.get("statusLabel") or "").lower() != "unavailable"
        for item in oid_certificate_rows
    ):
        return "warning"

    oaa = product_metrics.get("oaa") or {}
    if oaa.get("error"):
        return "warning"
    if oaa.get("podCount") and oaa.get("readyPodCount") != oaa.get("podCount"):
        return "warning"
    if oaa and oaa.get("podCount") == 0:
        return "down"

    oig = product_metrics.get("oig") or {}
    if (oig.get("opatch") or {}).get("error"):
        return "warning"
    certificate_statuses = [str(item.get("status") or "").lower() for item in (oig.get("certificates") or [])]
    if "expired" in certificate_statuses:
        return "down"
    if "warning" in certificate_statuses:
        return "warning"

    return "healthy"


def combine_statuses(*statuses):
    normalized = [status for status in statuses if status]
    if "down" in normalized:
        return "down"
    if "warning" in normalized:
        return "warning"
    return "healthy"


def app_checks_status(app_checks):
    if not app_checks:
        return "healthy"
    healthy = len([item for item in app_checks if item["status"] == "healthy"])
    warning = len([item for item in app_checks if item["status"] == "warning"])
    if healthy == len(app_checks):
        return "healthy"
    if healthy > 0 or warning > 0:
        return "warning"
    return "warning"


def target_status(server, app_checks, product_metrics=None):
    if not server.get("reachable"):
        return "down"
    app_status = app_checks_status(app_checks)
    server_health_status = ((server.get("health") or {}).get("status")) or "healthy"
    if server_health_status == "critical":
        server_health_status = "warning"
    return combine_statuses(app_status, product_metrics_status(product_metrics), server_health_status)


def refresh_saved_opatch_recommendations(dashboard_payload):
    dashboard_payload = dict(dashboard_payload or {})
    environment = dashboard_payload.get("environment") or {}
    product_metrics = dict(dashboard_payload.get("productMetrics") or {})

    def oracle_home_for(product_key, metrics):
        metrics = metrics or {}
        product_settings = environment.get(product_key) or {}
        weblogic_settings = environment.get("weblogic") or {}
        if product_key in ("oam", "weblogic"):
            return (
                product_settings.get("oracleHome")
                or weblogic_settings.get("oracleHome")
                or metrics.get("oracleHome")
                or ""
            )
        return (
            product_settings.get("oracleHome")
            or metrics.get("oracleHome")
            or weblogic_settings.get("oracleHome")
            or ""
        )

    def refresh_opatch(opatch, oracle_home):
        if not isinstance(opatch, dict):
            return opatch
        if not (
            opatch.get("patches")
            or opatch.get("versions")
            or opatch.get("products")
            or opatch.get("distributions")
        ):
            return opatch
        opatch = dict(opatch)
        recommendation = build_fmw_patch_recommendation(opatch, environment, oracle_home)
        opatch["recommendation"] = recommendation
        opatch["patchComparisonRows"] = recommendation.get("comparisonRows", [])
        return opatch

    for product_key in ("weblogic", "oam", "oig", "oud", "oid"):
        metrics = product_metrics.get(product_key)
        if not isinstance(metrics, dict):
            continue
        metrics = dict(metrics)
        oracle_home = oracle_home_for(product_key, metrics)
        metrics["opatch"] = refresh_opatch(metrics.get("opatch"), oracle_home)
        if product_key == "weblogic":
            cluster_nodes = []
            changed_nodes = False
            for node in metrics.get("clusterNodes") or []:
                if not isinstance(node, dict):
                    cluster_nodes.append(node)
                    continue
                node = dict(node)
                node_home = node.get("oracleHome") or oracle_home
                refreshed = refresh_opatch(node.get("opatch"), node_home)
                if refreshed is not node.get("opatch"):
                    changed_nodes = True
                node["opatch"] = refreshed
                cluster_nodes.append(node)
            if changed_nodes:
                metrics["clusterNodes"] = cluster_nodes
        product_metrics[product_key] = metrics

    dashboard_payload["productMetrics"] = product_metrics
    return dashboard_payload


def hydrate_dashboard_payload(dashboard_payload):
    dashboard_payload = dict(dashboard_payload or {})
    dashboard_payload = refresh_saved_opatch_recommendations(dashboard_payload)
    server = dict(dashboard_payload.get("server") or {})
    if server and not server.get("health"):
        server["health"] = build_server_health(server)
    dashboard_payload["server"] = server
    if server.get("reachable"):
        status = target_status(
            server,
            dashboard_payload.get("appChecks") or [],
            dashboard_payload.get("productMetrics") or {},
        )
        dashboard_payload["status"] = status
        server["status"] = status
    return dashboard_payload


def collect_monitoring_server(monitoring_config):
    target = build_monitoring_target(monitoring_config)
    snapshot = get_server_snapshot(
        target,
        script_directory=None,
        process_matchers=monitoring_config.get("processMatchers") or [],
    )
    status = "healthy" if snapshot.get("reachable") else "down"
    if snapshot.get("reachable"):
        snapshot["status"] = status
        snapshot["health"] = build_server_health(snapshot)

    return {
        "name": monitoring_config.get("name") or "IAM Monitoring Server",
        "host": monitoring_config.get("host") or "localhost",
        "description": monitoring_config.get("description") or "",
        "servicePort": monitoring_config.get("servicePort") or 8081,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "generatedAtLocal": time.strftime("%A, %B %d %Y %H:%M:%S %Z"),
        "status": status,
        "server": snapshot,
    }


def collect_weblogic_cluster_host_snapshots(environment, primary_target, primary_server, server_metrics, weblogic_metrics=None):
    nodes = build_discovered_weblogic_cluster_targets(environment, primary_target, weblogic_metrics)
    if len(nodes) <= 1:
        return [], {}
    snapshots = []
    needs_credentials = []
    for index, node in enumerate(nodes):
        if index == 0:
            snapshot = dict(primary_server or {})
        else:
            snapshot = get_server_snapshot(
                node.get("target") or primary_target,
                script_directory=server_metrics.get("scriptDirectory"),
                process_matchers=server_metrics.get("processMatchers") or [],
            )
            snapshot["health"] = build_server_health(snapshot)
        snapshot["nodeId"] = node.get("id") or "node{0}".format(index + 1)
        snapshot["nodeName"] = node.get("name") or "Node {0}".format(index + 1)
        snapshot["nodeRole"] = node.get("role") or ""
        snapshot["configuredHost"] = node.get("host") or ""
        node_target = node.get("target") or {}
        snapshot["configuredPort"] = node.get("port") or node_target.get("port") or 22
        snapshot["configuredUsername"] = node.get("username") or node_target.get("username") or ""
        snapshot["configuredSshMode"] = node.get("sshMode") or node_target.get("sshMode") or "user_password"
        snapshot["discoveredFromWlst"] = bool(node.get("discovered") or node.get("source") == "wlst")
        snapshot["sourceServers"] = node.get("sourceServers") or []
        snapshot["listenAddress"] = node.get("listenAddress") or ""
        snapshot["ip"] = node.get("ip") or ""
        snapshot["machine"] = node.get("machine") or ""
        snapshot["usesInheritedCredentials"] = bool(node.get("usesInheritedCredentials"))
        if index > 0 and snapshot.get("discoveredFromWlst") and not snapshot.get("reachable"):
            snapshot["needsCredentials"] = True
            snapshot["credentialHint"] = (
                "Multiple WebLogic nodes were detected from the AdminServer inventory. "
                "This node did not accept the primary SSH credentials."
            )
            needs_credentials.append({
                "nodeId": snapshot["nodeId"],
                "nodeName": snapshot["nodeName"],
                "host": snapshot["configuredHost"],
                "listenAddress": snapshot.get("listenAddress") or "",
                "ip": snapshot.get("ip") or "",
                "sourceServers": snapshot.get("sourceServers") or [],
                "error": snapshot.get("error") or "SSH connection failed.",
            })
        snapshots.append(snapshot)
    discovery = {
        "mode": "wlst-discovered" if any(node.get("source") == "wlst" for node in nodes) else "configured",
        "detected": len(nodes),
        "reachable": len([snapshot for snapshot in snapshots if snapshot.get("reachable")]),
        "unreachable": len([snapshot for snapshot in snapshots if not snapshot.get("reachable")]),
        "needsCredentialNodes": needs_credentials,
        "message": (
            "WebLogic cluster hosts were discovered from WLST server inventory and collected with the primary SSH credentials."
            if any(node.get("source") == "wlst" for node in nodes)
            else "WebLogic cluster hosts came from saved node-specific SSH profiles."
        ),
    }
    return snapshots, discovery


def collect_product_host_snapshots(environment, primary_target, primary_server, server_metrics, base_snapshots=None, base_discovery=None):
    products = environment.get("products") or {}
    snapshots = [dict(item or {}) for item in (base_snapshots or [])]
    base_discovery = dict(base_discovery or {})

    def snapshot_key(snapshot):
        return weblogic_host_key(
            snapshot.get("configuredHost"),
            snapshot.get("actualHostname"),
            ((snapshot.get("target") or {}) if isinstance(snapshot.get("target"), dict) else {}).get("host"),
        )

    def annotate_snapshot(snapshot, node_id, node_name, role, source_servers, target, product_host=False):
        snapshot["nodeId"] = snapshot.get("nodeId") or node_id
        snapshot["nodeName"] = snapshot.get("nodeName") or node_name
        snapshot["nodeRole"] = snapshot.get("nodeRole") or role
        snapshot["configuredHost"] = snapshot.get("configuredHost") or (target or {}).get("host") or ""
        snapshot["configuredPort"] = snapshot.get("configuredPort") or (target or {}).get("port") or 22
        snapshot["configuredUsername"] = snapshot.get("configuredUsername") or (target or {}).get("username") or ""
        snapshot["configuredSshMode"] = snapshot.get("configuredSshMode") or (target or {}).get("sshMode") or "user_password"
        snapshot["sourceServers"] = snapshot.get("sourceServers") or source_servers or []
        snapshot["productHost"] = bool(snapshot.get("productHost") or product_host)
        return snapshot

    if not snapshots:
        source_servers = []
        if products.get("oam"):
            source_servers.append("OAM")
        if products.get("weblogic"):
            source_servers.append("WebLogic")
        if products.get("oig"):
            source_servers.append("OIG/OIM")
        if products.get("oid"):
            source_servers.append("OID")
        if products.get("oud"):
            source_servers.append("OUD")
        if products.get("oaa"):
            source_servers.append("OAA")
        primary_name = "Primary Server Host"
        if products.get("oam") or products.get("weblogic") or products.get("oig"):
            primary_name = "OAM / WebLogic Host" if products.get("oam") else "WebLogic Admin Host"
        snapshots.append(annotate_snapshot(
            dict(primary_server or {}),
            "primary",
            primary_name,
            "Primary environment host",
            source_servers,
            primary_target,
            product_host=True,
        ))

    seen_keys = set(key for key in (snapshot_key(item) for item in snapshots) if key)

    def add_target_snapshot(node_id, node_name, role, source_servers, product_target):
        host_key = weblogic_host_key((product_target or {}).get("host"))
        if not host_key:
            return False
        if host_key in seen_keys:
            for snapshot in snapshots:
                if host_key in (
                    weblogic_host_key(snapshot.get("configuredHost")),
                    weblogic_host_key(snapshot.get("actualHostname")),
                ):
                    merged = list(snapshot.get("sourceServers") or [])
                    for source in source_servers or []:
                        if source and source not in merged:
                            merged.append(source)
                    snapshot["sourceServers"] = merged
                    snapshot["productHost"] = True
                    if node_name and node_name not in (snapshot.get("nodeName") or ""):
                        snapshot["nodeName"] = "{0} / {1}".format(snapshot.get("nodeName") or "Host", node_name)
                    break
            return False
        snapshot = get_server_snapshot(
            product_target,
            script_directory=server_metrics.get("scriptDirectory"),
            process_matchers=server_metrics.get("processMatchers") or [],
        )
        snapshot["health"] = build_server_health(snapshot)
        annotate_snapshot(snapshot, node_id, node_name, role, source_servers, product_target, product_host=True)
        snapshots.append(snapshot)
        seen_keys.add(host_key)
        actual_key = weblogic_host_key(snapshot.get("actualHostname"))
        if actual_key:
            seen_keys.add(actual_key)
        return True

    if products.get("weblogic") or products.get("oam") or products.get("oig"):
        weblogic_target = build_weblogic_target(environment, primary_target)
        source_servers = []
        if products.get("oam"):
            source_servers.append("OAM")
        if products.get("weblogic"):
            source_servers.append("WebLogic")
        if products.get("oig"):
            source_servers.append("OIG/OIM")
        add_target_snapshot(
            "weblogic-admin",
            "OAM / WebLogic Host" if products.get("oam") else "WebLogic Admin Host",
            "WebLogic AdminServer host",
            source_servers,
            weblogic_target,
        )

    if products.get("oaa"):
        oaa_target = build_oaa_target(environment, primary_target)
        add_target_snapshot(
            "oaa-control",
            "OAA Kubernetes Host",
            "OAA Kubernetes/control host",
            ["OAA"],
            oaa_target,
        )

    has_product_host_context = any(
        item.get("productHost") and item.get("sourceServers")
        for item in snapshots
    )
    if len(snapshots) <= 1 and not has_product_host_context:
        return [], {}

    discovery_mode = "mixed-product-hosts" if base_snapshots else "product-hosts"
    discovery = {
        "mode": discovery_mode,
        "detected": len(snapshots),
        "reachable": len([snapshot for snapshot in snapshots if snapshot.get("reachable")]),
        "unreachable": len([snapshot for snapshot in snapshots if not snapshot.get("reachable")]),
        "needsCredentialNodes": base_discovery.get("needsCredentialNodes") or [],
        "message": (
            "Infrastructure hosts include WebLogic/OAM and OAA product hosts collected from their saved SSH profiles."
            if products.get("oaa")
            else base_discovery.get("message") or "Infrastructure hosts were collected from product SSH profiles."
        ),
    }
    return snapshots, discovery


def collect_environment_dashboard(environment, progress=None):
    target = build_environment_target(environment)
    server_metrics = environment.get("serverMetrics") or {}
    server = get_server_snapshot(
        target,
        script_directory=server_metrics.get("scriptDirectory"),
        process_matchers=server_metrics.get("processMatchers") or [],
    )
    server["health"] = build_server_health(server)
    app_checks = collect_app_checks(target, environment)
    product_metrics = get_product_metrics(target, environment, app_checks, progress=progress)
    cluster_host_snapshots, cluster_discovery = collect_weblogic_cluster_host_snapshots(
        environment,
        target,
        server,
        server_metrics,
        product_metrics.get("weblogic") if product_metrics else None,
    )
    host_snapshots, host_discovery = collect_product_host_snapshots(
        environment,
        target,
        server,
        server_metrics,
        base_snapshots=cluster_host_snapshots,
        base_discovery=cluster_discovery,
    )
    if host_snapshots:
        server["clusterNodes"] = host_snapshots
        server["clusterNodeDiscovery"] = host_discovery
        if product_metrics and product_metrics.get("weblogic"):
            product_metrics["weblogic"]["clusterNodes"] = host_snapshots
            product_metrics["weblogic"]["clusterNodeDiscovery"] = host_discovery
    status = target_status(server, app_checks, product_metrics)
    if server.get("reachable"):
        server["status"] = status

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
                "host": (environment.get("server") or {}).get("host"),
                "port": (environment.get("server") or {}).get("port"),
                "username": (environment.get("server") or {}).get("username"),
                "sshMode": (environment.get("server") or {}).get("sshMode"),
                "authType": (environment.get("server") or {}).get("authType"),
                "sudoRequired": (environment.get("server") or {}).get("sudoRequired"),
            },
        },
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "generatedAtLocal": time.strftime("%A, %B %d %Y %H:%M:%S %Z"),
        "status": status,
        "server": server,
        "appChecks": app_checks,
        "productMetrics": product_metrics,
        "summary": {
            "totalApps": len(app_checks),
            "healthyApps": len([item for item in app_checks if item["status"] == "healthy"]),
            "warningApps": len([item for item in app_checks if item["status"] == "warning"]),
            "downApps": len([item for item in app_checks if item["status"] == "down"]),
        },
        "notes": [
            "This environment uses the same SSH definition for monitoring, install, and upgrade workflows.",
            "Bootstrap uses the initial SSH access one time and then switches the environment to the installed runtime key for ongoing collection.",
            "Use Run Jobs Now when you want a fresh environment snapshot before the next scheduled collector window.",
        ],
    }


def build_environment_error_dashboard(environment, error_message):
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
                "host": (environment.get("server") or {}).get("host"),
                "port": (environment.get("server") or {}).get("port"),
                "username": (environment.get("server") or {}).get("username"),
                "sshMode": (environment.get("server") or {}).get("sshMode"),
                "authType": (environment.get("server") or {}).get("authType"),
                "sudoRequired": (environment.get("server") or {}).get("sudoRequired"),
            },
        },
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "generatedAtLocal": time.strftime("%A, %B %d %Y %H:%M:%S %Z"),
        "status": "down",
        "server": {
            "reachable": False,
            "status": "down",
            "actualHostname": None,
            "error": error_message,
            "health": {
                "status": "down",
                "checks": {},
                "thresholds": DEFAULT_SERVER_HEALTH_THRESHOLDS,
            },
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
            "The environment could not be collected with the current connection settings.",
        ],
        "collectorError": error_message,
    }


def extract_environment_overview(dashboard_payload):
    dashboard_payload = hydrate_dashboard_payload(dashboard_payload)
    environment = dashboard_payload.get("environment") or {}
    server = dashboard_payload.get("server") or {}
    summary = dashboard_payload.get("summary") or {}
    return {
        "id": environment.get("id"),
        "name": environment.get("name"),
        "description": environment.get("description"),
        "host": (environment.get("server") or {}).get("host"),
        "status": dashboard_payload.get("status"),
        "generatedAt": dashboard_payload.get("generatedAt"),
        "generatedAtLocal": dashboard_payload.get("generatedAtLocal"),
        "generatedAtEpoch": dashboard_payload.get("generatedAtEpoch"),
        "actualHostname": server.get("actualHostname"),
        "environmentType": environment.get("environmentType"),
        "products": environment.get("products") or {},
        "sshMode": (environment.get("server") or {}).get("sshMode"),
        "authType": (environment.get("server") or {}).get("authType"),
        "summary": summary,
    }
