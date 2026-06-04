import copy
import json
import os
import re
from urllib.parse import urlparse


DEFAULT_OAM_CHECKS = [
    {"product": "oam", "name": "OAM Console", "url": "http://localhost:7001/oamconsole"},
    {"product": "oam", "name": "OAM Access", "url": "http://localhost:14150/access"},
    {"product": "oam", "name": "Fusion Middleware EM", "url": "http://localhost:7001/em"},
]

DEFAULT_OUD_CHECKS = [
    {"product": "oud", "name": "OUDSM", "url": "http://localhost:7101/oudsm"},
]

DEFAULT_OIG_CHECKS = []

DEFAULT_OID_QUERY_GROUPS = [
    "configset",
    "monitor",
    "system",
    "workQueue",
    "clientConnections",
    "version",
    "opatch",
    "certificates",
]

DEFAULT_OAA_PROPERTY_NAMES = [
    "bharosa.uio.default.challenge.type.enum.ChallengeEmail.fromAddress",
]

VALID_ENVIRONMENT_TYPES = ("oam", "oig", "oid", "oud", "oaa", "soa", "weblogic")
PRODUCT_KEYS = ("oam", "oud", "oig", "oid", "oaa", "soa", "weblogic")
WEBLOGIC_REQUIRED_PRODUCTS = ("oam", "oig", "soa")


def deep_copy(value):
    return copy.deepcopy(value)


def admin_local_url(admin_url, path, default_port=7001):
    scheme = "http"
    port = default_port
    text = str(admin_url or "").strip()
    if text:
        candidate = text if "://" in text else "http://{0}".format(text)
        try:
            parsed = urlparse(candidate)
            scheme = parsed.scheme or scheme
            port = parsed.port or (443 if scheme == "https" else default_port)
        except ValueError:
            pass
    suffix = path if str(path or "").startswith("/") else "/{0}".format(path)
    return "{0}://localhost:{1}{2}".format(scheme, port, suffix)


def default_oam_checks(admin_url=""):
    checks = deep_copy(DEFAULT_OAM_CHECKS)
    for check in checks:
        if check.get("name") == "OAM Console":
            check["url"] = admin_local_url(admin_url, "/oamconsole")
        elif check.get("name") == "Fusion Middleware EM":
            check["url"] = admin_local_url(admin_url, "/em")
    return checks


def slugify(value):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "")).strip("-").lower()
    return text or "environment"


def coerce_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_optional_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def default_collection_minutes():
    return max(
        5,
        as_int(
            os.environ.get(
                "IAM_MONITORING_DEFAULT_COLLECTION_MINUTES",
                os.environ.get("IAM_DASHBOARD_DEFAULT_COLLECTION_MINUTES", "60"),
            ),
            60,
        ),
    )


def sanitize_schedule_minutes(value, default):
    return max(5, as_int(value, default))


def preserve_secret(new_value, existing_value, allow_blank=False):
    if new_value is None:
        return existing_value or ""
    if allow_blank and new_value == "":
        return ""
    value = str(new_value).strip()
    if not value and existing_value:
        return existing_value
    return value


def parse_csv_or_list(value, fallback=None):
    fallback = fallback or []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return list(fallback)
    text = str(value).strip()
    if not text:
        return list(fallback)
    return [item.strip() for item in text.split(",") if item.strip()]


def normalize_oud_instances(payload_oud, existing_oud, instance_home="", ldap_port=1389, admin_port=4444):
    payload_oud = payload_oud or {}
    existing_oud = existing_oud or {}
    existing_instances = existing_oud.get("instances") if isinstance(existing_oud.get("instances"), list) else []

    def matching_existing(row, index):
        if index < len(existing_instances):
            return existing_instances[index] or {}
        row_home = str((row or {}).get("instanceHome") or (row or {}).get("path") or "").strip()
        row_name = str((row or {}).get("name") or (row or {}).get("label") or "").strip()
        for existing_row in existing_instances:
            candidate = existing_row or {}
            if row_home and row_home == str(candidate.get("instanceHome") or candidate.get("path") or "").strip():
                return candidate
            if row_name and row_name == str(candidate.get("name") or candidate.get("label") or "").strip():
                return candidate
        return {}

    source = []
    if isinstance(payload_oud.get("instances"), list):
        source = payload_oud.get("instances") or []
    elif isinstance(existing_oud.get("instances"), list) and "instances" not in payload_oud and not payload_oud.get("instanceHome"):
        source = existing_oud.get("instances") or []

    rows = []
    for index, item in enumerate(source):
        row = item or {}
        existing_row = matching_existing(row, index)
        home = str(row.get("instanceHome") or row.get("path") or "").strip()
        name = str(row.get("name") or row.get("label") or "OUD Instance {0}".format(index + 1)).strip()
        if not home:
            continue
        bind_dn = str(
            row.get("bindDn")
            or existing_row.get("bindDn")
            or existing_oud.get("bindDn")
            or payload_oud.get("bindDn")
            or "cn=Directory Manager"
        ).strip() or "cn=Directory Manager"
        rows.append({
            "name": name or "OUD Instance {0}".format(index + 1),
            "instanceHome": home,
            "bindDn": bind_dn,
            "bindPassword": preserve_secret(
                row.get("bindPassword"),
                existing_row.get("bindPassword") or existing_oud.get("bindPassword"),
                allow_blank=coerce_bool(row.get("clearBindPassword"), False),
            ),
            "ldapPort": as_int(row.get("ldapPort") or ldap_port or 1389, 1389),
            "adminPort": as_int(row.get("adminPort") or admin_port or 4444, 4444),
        })

    fallback_home = str(payload_oud.get("instanceHome") or instance_home or existing_oud.get("instanceHome") or "").strip()
    if not rows and fallback_home:
        bind_dn = str(payload_oud.get("bindDn") or existing_oud.get("bindDn") or "cn=Directory Manager").strip() or "cn=Directory Manager"
        rows.append({
            "name": "OUD Instance 1",
            "instanceHome": fallback_home,
            "bindDn": bind_dn,
            "bindPassword": preserve_secret(
                payload_oud.get("bindPassword"),
                existing_oud.get("bindPassword"),
                allow_blank=coerce_bool(payload_oud.get("clearBindPassword"), False),
            ),
            "ldapPort": as_int(payload_oud.get("ldapPort") or existing_oud.get("ldapPort") or ldap_port or 1389, 1389),
            "adminPort": as_int(payload_oud.get("adminPort") or existing_oud.get("adminPort") or admin_port or 4444, 4444),
        })
    return rows


def normalize_checks(checks, default_checks):
    source = checks if checks is not None else default_checks
    normalized = []
    for check in source:
        item = check or {}
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        product = str(item.get("product") or "generic").strip().lower() or "generic"
        if name and url:
            normalized.append({
                "product": product,
                "name": name,
                "url": url,
            })
    return normalized


def is_local_default_url(url, expected_path, ports):
    text = str(url or "").strip()
    if not text:
        return False
    candidate = text if "://" in text else "http://{0}".format(text)
    try:
        parsed = urlparse(candidate)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    host = str(parsed.hostname or "").lower()
    path = str(parsed.path or "").rstrip("/") or "/"
    return host in ("localhost", "127.0.0.1") and path == expected_path and port in ports


def normalize_oam_checks(checks, admin_url=""):
    defaults = default_oam_checks(admin_url)
    normalized = normalize_checks(checks, defaults)
    if not normalized:
        normalized = defaults
    default_by_name = {item["name"].lower(): item["url"] for item in defaults}
    for item in normalized:
        if str(item.get("product") or "").lower() != "oam":
            continue
        name = str(item.get("name") or "").strip().lower()
        if name == "oam console" and is_local_default_url(item.get("url"), "/oamconsole", {7001}):
            item["url"] = default_by_name.get(name, admin_local_url(admin_url, "/oamconsole"))
        elif name == "fusion middleware em" and is_local_default_url(item.get("url"), "/em", {7001, 7201}):
            item["url"] = default_by_name.get(name, admin_local_url(admin_url, "/em"))
    return normalized


def normalize_environment_type(value, fallback=""):
    text = str(value or "").strip().lower()
    if text == "oim":
        return "oig"
    if text in VALID_ENVIRONMENT_TYPES:
        return text
    return fallback


def product_flags(**enabled):
    return {key: bool(enabled.get(key, False)) for key in PRODUCT_KEYS}


def apply_product_dependencies(products):
    flags = {key: coerce_bool((products or {}).get(key), False) for key in PRODUCT_KEYS}
    if flags.get("oig"):
        flags["soa"] = False
    if any(flags.get(key) for key in WEBLOGIC_REQUIRED_PRODUCTS):
        flags["weblogic"] = True
    return flags


def default_weblogic_server_names(products):
    flags = apply_product_dependencies(products)
    names = ["AdminServer"]
    if flags.get("oig"):
        names.append("oim_server1")
    else:
        if flags.get("oam"):
            names.append("oam_server1")
        if flags.get("soa"):
            names.append("soa_server1")
    if flags.get("oud"):
        names.append("OUD_Managed_server1")
    return list(dict.fromkeys(names))


def products_for_environment_type(value):
    text = normalize_environment_type(value, "")
    if text == "oam":
        return apply_product_dependencies(product_flags(oam=True))
    if text == "oig":
        return apply_product_dependencies(product_flags(oig=True))
    if text == "oud":
        return product_flags(oud=True)
    if text == "oid":
        return product_flags(oid=True)
    if text == "oaa":
        return product_flags(oaa=True)
    if text == "soa":
        return apply_product_dependencies(product_flags(soa=True))
    if text == "weblogic":
        return product_flags(weblogic=True)
    return product_flags()


def has_weblogic_profile(products=None, weblogic=None):
    product_flags = products or {}
    weblogic_config = weblogic or {}
    admin_host = weblogic_config.get("adminHost") or {}
    return bool(
        coerce_bool(product_flags.get("weblogic"), False)
        or coerce_bool(weblogic_config.get("enabled"), False)
        or str(weblogic_config.get("adminUrl") or "").strip()
        or str(weblogic_config.get("oracleHome") or "").strip()
        or str(admin_host.get("host") or "").strip()
    )


def normalize_ssh_mode(value, fallback="user_password_sudo"):
    text = str(value or "").strip().lower()
    if text in ("root_password", "user_password", "user_password_sudo", "root_key", "user_key", "user_key_sudo"):
        return text
    return fallback


def normalize_ssh_profile_payload(
    payload,
    existing=None,
    default_host="",
    default_username="oracle",
    default_port=22,
    default_mode="user_password_sudo",
):
    payload = payload or {}
    existing = existing or {}
    ssh_mode = normalize_ssh_mode(payload.get("sshMode") or existing.get("sshMode"), default_mode)
    profile = {
        "mode": "ssh",
        "host": str(payload.get("host") or existing.get("host") or default_host).strip(),
        "port": as_int(payload.get("port") or existing.get("port") or default_port, default_port),
        "username": str(payload.get("username") or existing.get("username") or default_username).strip() or default_username,
        "sshMode": ssh_mode,
        "authType": "private_key" if ssh_mode.endswith("_key") else "password",
        "sudoRequired": ssh_mode.endswith("_sudo"),
        "password": preserve_secret(
            payload.get("password"),
            existing.get("password"),
            allow_blank=coerce_bool(payload.get("clearPassword"), False),
        ),
        "privateKeyPath": str(payload.get("privateKeyPath") or existing.get("privateKeyPath") or "").strip(),
        "passphrase": preserve_secret(
            payload.get("passphrase"),
            existing.get("passphrase"),
            allow_blank=coerce_bool(payload.get("clearPassphrase"), False),
        ),
    }
    if ssh_mode.startswith("root_"):
        profile["username"] = "root"
        profile["sudoRequired"] = False
    return profile


def repair_bootstrap_server_profile(environment):
    environment = environment or {}
    server = environment.get("server") or {}
    bootstrap = environment.get("bootstrap") or {}
    initial_mode = normalize_ssh_mode(bootstrap.get("initialSshMode"), "")
    runtime_key_path = str(bootstrap.get("runtimeKeyPath") or "").strip()
    bootstrap_ready = str(bootstrap.get("status") or "").strip().lower() == "ready"
    current_mode = normalize_ssh_mode(server.get("sshMode"), "")
    current_key_path = str(server.get("privateKeyPath") or "").strip()

    if not initial_mode or not bootstrap_ready:
        return environment

    # Older builds overwrote the saved SSH profile with the runtime key profile.
    # Repair the user-facing server settings so the UI still reflects the original bootstrap login.
    if current_mode in ("root_key", "user_key", "user_key_sudo") or (runtime_key_path and current_key_path == runtime_key_path):
        server["sshMode"] = initial_mode
        server["authType"] = "private_key" if "_key" in initial_mode else "password"
        if server["authType"] == "password":
            server["privateKeyPath"] = ""
            server["passphrase"] = ""
        elif runtime_key_path and current_key_path == runtime_key_path:
            server["privateKeyPath"] = ""
        environment["server"] = server

    return environment


def default_process_matchers(products):
    matchers = []
    if products.get("weblogic") or products.get("oam") or products.get("oig") or products.get("soa"):
        matchers.extend(["AdminServer", "weblogic"])
    if products.get("oam"):
        matchers.extend(["oam", "ohs"])
    if products.get("oud"):
        matchers.extend(["oud"])
    if products.get("oig"):
        matchers.extend(["oig", "oim"])
    if products.get("oid"):
        matchers.extend(["oid", "ldap"])
    if products.get("oaa"):
        matchers.extend(["oaa"])
    if products.get("soa"):
        matchers.extend(["soa"])
    if not matchers:
        matchers = ["sshd", "python"]
    return list(dict.fromkeys(matchers))


def normalize_oaa_database_payload(payload, existing=None):
    payload = payload or {}
    existing = existing or {}

    def pick(*keys):
        for key in keys:
            value = payload.get(key)
            if value is None or value == "":
                value = existing.get(key)
            text = str(value or "").strip()
            if text:
                return text
        return ""

    schema = pick("schema", "user", "username")
    username = pick("username", "schema", "user") or schema
    return {
        "host": pick("host", "hostname"),
        "port": pick("port") or "1521",
        "service": pick("service", "svc", "serviceName"),
        "name": pick("name", "databaseName"),
        "schema": schema,
        "username": username,
        "protocol": pick("protocol"),
        "connectString": pick("connectString", "connectDescriptor"),
        "tablespace": pick("tablespace"),
        "createSchema": pick("createSchema"),
    }


def default_environment(name=None, host=None):
    schedule_minutes = default_collection_minutes()
    products = product_flags(oam=True, oud=True, weblogic=True)
    return {
        "id": slugify(name or host or "iam-environment"),
        "name": name or "IAM Environment",
        "description": "Starter Oracle IAM environment.",
        "environmentType": "",
        "server": {
            "mode": "ssh",
            "host": host or "",
            "port": 22,
            "username": "oracle",
            "sshMode": "user_password_sudo",
            "authType": "password",
            "sudoRequired": True,
            "password": "",
            "privateKeyPath": "",
            "passphrase": "",
        },
        "products": products,
        "serverMetrics": {
            "scriptDirectory": "/refresh/home/auto/bin",
            "processMatchers": default_process_matchers(products),
        },
        "weblogic": {
            "enabled": False,
            "adminUrl": "",
            "adminUsername": "weblogic",
            "adminPassword": "",
            "oracleHome": "",
            "domainHome": "",
            "adminHost": {
                "mode": "ssh",
                "host": "",
                "port": 22,
                "username": "oracle",
                "sshMode": "user_password",
                "authType": "password",
                "sudoRequired": False,
                "password": "",
                "privateKeyPath": "",
                "passphrase": "",
            },
            "cluster": {
                "enabled": False,
                "nodes": [],
                "node2": {
                    "mode": "ssh",
                    "host": "",
                    "port": 22,
                    "username": "oracle",
                    "sshMode": "user_password",
                    "authType": "password",
                    "sudoRequired": False,
                    "password": "",
                    "privateKeyPath": "",
                    "passphrase": "",
                    "oracleHome": "",
                    "domainHome": "",
                },
            },
            "jstatPath": "",
            "serverNames": [],
        },
        "oam": {
            "oracleHome": "",
            "domainHome": "",
            "checks": deep_copy(DEFAULT_OAM_CHECKS),
        },
        "oud": {
            "host": host or "",
            "port": None,
            "weblogicEnabled": False,
            "oracleHome": "",
            "domainHome": "",
            "instanceHome": "",
            "instances": [],
            "statusPath": "/refresh/home/Instances/oudinst/OUD/bin/status",
            "bindDn": "cn=Directory Manager",
            "bindPassword": "",
            "ldapPort": 1389,
            "adminPort": 4444,
            "ldapUrl": "ldap://localhost:1389",
            "checks": deep_copy(DEFAULT_OUD_CHECKS),
        },
        "oid": {
            "host": host or "",
            "weblogicEnabled": False,
            "oracleHome": "",
            "adminUsername": "cn=orcladmin",
            "adminPassword": "",
            "ldapPort": None,
            "ldapsPort": None,
            "queryGroups": deep_copy(DEFAULT_OID_QUERY_GROUPS),
            "checks": [],
        },
        "oig": {
            "oracleHome": "",
            "domainHome": "",
            "adminUrl": "",
            "adminUsername": "xelsysadm",
            "adminPassword": "",
            "checks": deep_copy(DEFAULT_OIG_CHECKS),
        },
        "oaa": {
            "namespace": "oaans",
            "ingressNamespace": "ingressns",
            "releaseName": "oaainstall",
            "installSettingsPath": "",
            "runtimeBaseUrl": "",
            "runtimeUsername": "oaainstall-oaa",
            "runtimePassword": "",
            "propertyNames": deep_copy(DEFAULT_OAA_PROPERTY_NAMES),
            "kubectlPath": "kubectl",
            "logTailLines": 80,
            "database": {
                "host": "",
                "port": "1521",
                "service": "",
                "name": "",
                "schema": "",
                "username": "",
                "protocol": "",
                "connectString": "",
                "tablespace": "",
                "createSchema": "",
            },
            "kubeHost": {
                "mode": "ssh",
                "host": host or "",
                "port": 22,
                "username": "root",
                "sshMode": "root_password",
                "authType": "password",
                "sudoRequired": False,
                "password": "",
                "privateKeyPath": "",
                "passphrase": "",
            },
        },
        "soa": {
            "oracleHome": "",
            "domainHome": "",
            "checks": [],
        },
        "collection": {
            "enabled": True,
            "scheduleMinutes": schedule_minutes,
        },
        "bootstrap": {
            "status": "pending",
            "strategy": "initial_ssh_then_runtime_key",
            "initialSshMode": "user_password_sudo",
            "runtimeKeyPath": "",
            "runtimeEnvPath": "",
            "lastBootstrappedAt": "",
            "message": "Bootstrap uses the initial SSH access one time and then switches the environment to the installed runtime key for ongoing collection.",
        },
        "operations": {
            "installMethod": "environment_ssh",
            "upgradeMethod": "environment_ssh",
        },
    }


def default_config():
    return {
        "dashboard_title": "FMW Monitoring Dashboard",
        "monitoring_server": {
            "name": "IAM Monitoring Server",
            "host": "localhost",
            "description": "Hosted dashboard server for Oracle IAM monitoring and administration.",
            "servicePort": 8081,
            "processMatchers": ["iam-monitoring", "python", "sshd"],
        },
        "operations": {
            "installMethod": "environment_ssh",
            "upgradeMethod": "environment_ssh",
        },
        "environments": [],
    }


def map_legacy_checks(checks):
    mapped = []
    for check in checks or []:
        item = check or {}
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        product = "oam"
        label = name.lower()
        if "oud" in label:
            product = "oud"
        elif "oig" in label:
            product = "oig"
        mapped.append({
            "product": product,
            "name": name,
            "url": url,
        })
    return mapped


def normalize_environment(payload, existing=None):
    base = default_environment(
        (payload or {}).get("name") or (existing or {}).get("name"),
        ((payload or {}).get("server") or {}).get("host") or ((existing or {}).get("server") or {}).get("host"),
    )
    existing = existing or {}
    payload = payload or {}

    server_payload = payload.get("server") or {}
    existing_server = existing.get("server") or {}
    products_payload = payload.get("products") or {}
    existing_products = existing.get("products") or {}
    collection_payload = payload.get("collection") or {}
    existing_collection = existing.get("collection") or {}
    bootstrap_payload = payload.get("bootstrap") or {}
    existing_bootstrap = existing.get("bootstrap") or {}

    environment_type = normalize_environment_type(
        payload.get("environmentType"),
        normalize_environment_type((existing or {}).get("environmentType"), ""),
    )
    if products_payload:
        product_defaults = {}
    elif existing:
        product_defaults = existing_products
    elif environment_type:
        product_defaults = products_for_environment_type(environment_type)
    else:
        product_defaults = existing_products or base.get("products") or {}
    products = {
        key: coerce_bool(products_payload.get(key), product_defaults.get(key, False))
        for key in PRODUCT_KEYS
    }
    if "oim" in products_payload and "oig" not in products_payload:
        products["oig"] = coerce_bool(products_payload.get("oim"), product_defaults.get("oig", False))
    products = apply_product_dependencies(products)

    server_metrics_payload = payload.get("serverMetrics") or {}
    existing_server_metrics = existing.get("serverMetrics") or {}

    oam_payload = payload.get("oam") or {}
    oud_payload = payload.get("oud") or {}
    oid_payload = payload.get("oid") or {}
    oig_payload = payload.get("oig") or {}
    oaa_payload = payload.get("oaa") or {}
    soa_payload = payload.get("soa") or {}
    weblogic_payload = payload.get("weblogic") or {}
    existing_oam = existing.get("oam") or {}
    existing_oig = existing.get("oig") or {}
    existing_oaa = existing.get("oaa") or {}
    existing_soa = existing.get("soa") or {}
    existing_weblogic = existing.get("weblogic") or {}
    operations_payload = payload.get("operations") or {}
    existing_oud = existing.get("oud") or {}
    existing_oid = existing.get("oid") or {}
    oud_host = str(
        oud_payload.get("host")
        or existing_oud.get("host")
        or server_payload.get("host")
        or existing_server.get("host")
        or base["oud"].get("host")
        or ""
    ).strip()
    oid_host = str(
        oid_payload.get("host")
        or existing_oid.get("host")
        or server_payload.get("host")
        or existing_server.get("host")
        or base["oid"].get("host")
        or ""
    ).strip()
    oud_instance_home = str(
        oud_payload.get("instanceHome")
        or existing_oud.get("instanceHome")
        or base["oud"].get("instanceHome")
        or ""
    ).strip()
    derived_status_path = ""
    if oud_instance_home:
        derived_status_path = "{0}/bin/status".format(oud_instance_home.rstrip("/"))
    ldap_port = as_int(
        oud_payload.get("ldapPort")
        or existing_oud.get("ldapPort")
        or base["oud"].get("ldapPort")
        or 1389,
        1389,
    )
    admin_port = as_int(
        oud_payload.get("adminPort")
        or existing_oud.get("adminPort")
        or base["oud"].get("adminPort")
        or 4444,
        4444,
    )
    oud_instances = normalize_oud_instances(oud_payload, existing_oud, oud_instance_home, ldap_port, admin_port)
    if oud_instances:
        primary_oud_instance = oud_instances[0]
        oud_instance_home = str(primary_oud_instance.get("instanceHome") or oud_instance_home or "").strip()
        ldap_port = as_int(primary_oud_instance.get("ldapPort") or ldap_port, 1389)
        admin_port = as_int(primary_oud_instance.get("adminPort") or admin_port, 4444)
        derived_status_path = "{0}/bin/status".format(oud_instance_home.rstrip("/")) if oud_instance_home else ""
    primary_oud_bind_dn = str(
        (oud_instances[0].get("bindDn") if oud_instances else "")
        or oud_payload.get("bindDn")
        or existing_oud.get("bindDn")
        or base["oud"]["bindDn"]
    ).strip() or "cn=Directory Manager"
    primary_oud_bind_password = (
        (oud_instances[0].get("bindPassword") if oud_instances else "")
        or preserve_secret(
            oud_payload.get("bindPassword"),
            existing_oud.get("bindPassword") or environment_server_password(server_payload, existing_server),
            allow_blank=coerce_bool(oud_payload.get("clearBindPassword"), False),
        )
    )
    derived_ldap_url = "ldap://localhost:{0}".format(ldap_port)
    existing_weblogic_profile = has_weblogic_profile(existing_products, existing_weblogic)
    payload_weblogic_profile = has_weblogic_profile(products_payload, weblogic_payload)
    weblogic_enabled = coerce_bool(
        weblogic_payload.get("enabled"),
        existing_weblogic.get("enabled", products.get("weblogic")),
    )
    if environment_type == "oud":
        if existing_weblogic_profile or payload_weblogic_profile:
            weblogic_enabled = True
        products["weblogic"] = bool(weblogic_enabled)
    oid_weblogic_enabled = coerce_bool(
        oid_payload.get("weblogicEnabled"),
        coerce_bool(existing_oid.get("weblogicEnabled"), False),
    )
    if products.get("oid") and oid_weblogic_enabled:
        weblogic_enabled = True
        products["weblogic"] = True
    products = apply_product_dependencies(products)
    if products.get("weblogic"):
        weblogic_enabled = True
    oaa_release_name = str(
        oaa_payload.get("releaseName")
        or existing_oaa.get("releaseName")
        or base["oaa"].get("releaseName")
        or "oaainstall"
    ).strip() or "oaainstall"

    environment = {
        "id": str(payload.get("id") or existing.get("id") or base.get("id")).strip() or base.get("id"),
        "name": str(payload.get("name") or existing.get("name") or base.get("name")).strip() or base.get("name"),
        "description": str(payload.get("description") or existing.get("description") or base.get("description")).strip(),
        "environmentType": environment_type,
        "server": {
            "mode": "ssh",
            "host": str(server_payload.get("host") or existing_server.get("host") or oud_host or oid_host or base["server"]["host"]).strip(),
            "port": as_int(server_payload.get("port") or existing_server.get("port") or base["server"]["port"], 22),
            "username": str(server_payload.get("username") or existing_server.get("username") or base["server"]["username"]).strip() or "oracle",
            "sshMode": normalize_ssh_mode(
                server_payload.get("sshMode") or existing_server.get("sshMode"),
                "user_password_sudo",
            ),
            "authType": str(server_payload.get("authType") or existing_server.get("authType") or base["server"]["authType"]).strip() or "password",
            "sudoRequired": coerce_bool(server_payload.get("sudoRequired"), existing_server.get("sudoRequired", False)),
            "password": preserve_secret(
                server_payload.get("password"),
                existing_server.get("password"),
                allow_blank=coerce_bool(server_payload.get("clearPassword"), False),
            ),
            "privateKeyPath": str(server_payload.get("privateKeyPath") or existing_server.get("privateKeyPath") or "").strip(),
            "passphrase": preserve_secret(
                server_payload.get("passphrase"),
                existing_server.get("passphrase"),
                allow_blank=coerce_bool(server_payload.get("clearPassphrase"), False),
            ),
        },
        "products": products,
        "serverMetrics": {
            "scriptDirectory": str(
                server_metrics_payload.get("scriptDirectory")
                or existing_server_metrics.get("scriptDirectory")
                or base["serverMetrics"]["scriptDirectory"]
            ).strip(),
            "processMatchers": parse_csv_or_list(
                server_metrics_payload.get("processMatchers"),
                existing_server_metrics.get("processMatchers") or default_process_matchers(products),
            ),
        },
        "weblogic": {
            "enabled": weblogic_enabled,
            "adminUrl": str(
                weblogic_payload.get("adminUrl")
                or existing_weblogic.get("adminUrl")
                or ""
            ).strip(),
            "adminUsername": str(
                weblogic_payload.get("adminUsername")
                or existing_weblogic.get("adminUsername")
                or base["weblogic"].get("adminUsername")
                or "weblogic"
            ).strip() or "weblogic",
            "adminPassword": preserve_secret(
                weblogic_payload.get("adminPassword"),
                existing_weblogic.get("adminPassword"),
                allow_blank=coerce_bool(weblogic_payload.get("clearAdminPassword"), False),
            ),
            "oracleHome": str(
                weblogic_payload.get("oracleHome")
                or existing_weblogic.get("oracleHome")
                or (existing_oam.get("oracleHome") if products.get("oam") else "")
                or (existing_oig.get("oracleHome") if products.get("oig") else "")
                or base["weblogic"].get("oracleHome")
                or ""
            ).strip(),
            "domainHome": str(
                weblogic_payload.get("domainHome")
                or existing_weblogic.get("domainHome")
                or (existing_oam.get("domainHome") if products.get("oam") else "")
                or (existing_oig.get("domainHome") if products.get("oig") else "")
                or base["weblogic"].get("domainHome")
                or ""
            ).strip(),
            "adminHost": normalize_ssh_profile_payload(
                weblogic_payload.get("adminHost"),
                existing_weblogic.get("adminHost"),
                default_host="",
                default_username="oracle",
                default_port=22,
                default_mode="user_password",
            ),
            "cluster": deep_copy(existing_weblogic.get("cluster") or base["weblogic"].get("cluster") or {}),
            "jstatPath": str(
                weblogic_payload.get("jstatPath")
                or existing_weblogic.get("jstatPath")
                or base["weblogic"]["jstatPath"]
            ).strip(),
            "serverNames": parse_csv_or_list(
                weblogic_payload.get("serverNames"),
                existing_weblogic.get("serverNames") or default_weblogic_server_names(products) or base["weblogic"]["serverNames"],
            ),
        },
        "oam": {
            "oracleHome": str(
                weblogic_payload.get("oracleHome")
                or oam_payload.get("oracleHome")
                or existing_weblogic.get("oracleHome")
                or existing_oam.get("oracleHome")
                or base["oam"].get("oracleHome")
                or ""
            ).strip(),
            "domainHome": str(
                weblogic_payload.get("domainHome")
                or oam_payload.get("domainHome")
                or existing_weblogic.get("domainHome")
                or existing_oam.get("domainHome")
                or base["oam"].get("domainHome")
                or ""
            ).strip(),
            "checks": normalize_checks(
                oam_payload.get("checks") if "checks" in oam_payload else existing_oam.get("checks"),
                DEFAULT_OAM_CHECKS,
            ),
        },
        "oud": {
            "host": oud_host,
            "port": as_optional_int(
                oud_payload.get("port")
                if "port" in oud_payload
                else existing_oud.get("port")
            ),
            "weblogicEnabled": coerce_bool(
                oud_payload.get("weblogicEnabled"),
                coerce_bool(existing_oud.get("weblogicEnabled"), False),
            ),
            "oracleHome": str(
                oud_payload.get("oracleHome")
                or existing_oud.get("oracleHome")
                or base["oud"].get("oracleHome")
                or ""
            ).strip(),
            "domainHome": str(
                oud_payload.get("domainHome")
                or existing_oud.get("domainHome")
                or base["oud"].get("domainHome")
                or ""
            ).strip(),
            "instanceHome": oud_instance_home,
            "instances": deep_copy(oud_instances),
            "statusPath": str(
                derived_status_path
                or oud_payload.get("statusPath")
                or existing_oud.get("statusPath")
                or base["oud"]["statusPath"]
            ).strip(),
            "bindDn": str(
                primary_oud_bind_dn
            ).strip(),
            "bindPassword": primary_oud_bind_password,
            "ldapPort": ldap_port,
            "adminPort": admin_port,
            "ldapUrl": str(
                oud_payload.get("ldapUrl")
                or existing_oud.get("ldapUrl")
                or derived_ldap_url
                or base["oud"]["ldapUrl"]
            ).strip(),
            "checks": normalize_checks(
                oud_payload.get("checks") if "checks" in oud_payload else existing_oud.get("checks"),
                DEFAULT_OUD_CHECKS,
            ),
        },
        "oid": {
            "host": oid_host,
            "weblogicEnabled": oid_weblogic_enabled,
            "oracleHome": str(
                oid_payload.get("oracleHome")
                or existing_oid.get("oracleHome")
                or base["oid"].get("oracleHome")
                or ""
            ).strip(),
            "adminUsername": str(
                oid_payload.get("adminUsername")
                or existing_oid.get("adminUsername")
                or base["oid"].get("adminUsername")
                or "cn=orcladmin"
            ).strip() or "cn=orcladmin",
            "adminPassword": preserve_secret(
                oid_payload.get("adminPassword"),
                existing_oid.get("adminPassword"),
                allow_blank=coerce_bool(oid_payload.get("clearAdminPassword"), False),
            ),
            "ldapPort": as_optional_int(
                oid_payload.get("ldapPort")
                if "ldapPort" in oid_payload
                else existing_oid.get("ldapPort")
            ),
            "ldapsPort": as_optional_int(
                oid_payload.get("ldapsPort")
                if "ldapsPort" in oid_payload
                else existing_oid.get("ldapsPort")
            ),
            "queryGroups": parse_csv_or_list(
                oid_payload.get("queryGroups"),
                existing_oid.get("queryGroups") or DEFAULT_OID_QUERY_GROUPS,
            ),
            "checks": normalize_checks(
                oid_payload.get("checks") if "checks" in oid_payload else existing_oid.get("checks"),
                [],
            ),
        },
        "oig": {
            "oracleHome": str(
                weblogic_payload.get("oracleHome")
                or existing_weblogic.get("oracleHome")
                or oig_payload.get("oracleHome")
                or existing_oig.get("oracleHome")
                or base["oig"].get("oracleHome")
                or ""
            ).strip(),
            "domainHome": str(
                weblogic_payload.get("domainHome")
                or existing_weblogic.get("domainHome")
                or oig_payload.get("domainHome")
                or existing_oig.get("domainHome")
                or base["oig"].get("domainHome")
                or ""
            ).strip(),
            "adminUrl": "",
            "adminUsername": str(
                oig_payload.get("adminUsername")
                or existing_oig.get("adminUsername")
                or base["oig"].get("adminUsername")
                or "xelsysadm"
            ).strip() or "xelsysadm",
            "adminPassword": preserve_secret(
                oig_payload.get("adminPassword"),
                existing_oig.get("adminPassword"),
                allow_blank=coerce_bool(oig_payload.get("clearAdminPassword"), False),
            ),
            "checks": normalize_checks(
                oig_payload.get("checks") if "checks" in oig_payload else existing_oig.get("checks"),
                DEFAULT_OIG_CHECKS,
            ),
        },
        "oaa": {
            "namespace": str(
                oaa_payload.get("namespace")
                or existing_oaa.get("namespace")
                or base["oaa"].get("namespace")
                or "oaans"
            ).strip() or "oaans",
            "ingressNamespace": str(
                oaa_payload.get("ingressNamespace")
                or existing_oaa.get("ingressNamespace")
                or base["oaa"].get("ingressNamespace")
                or "ingressns"
            ).strip() or "ingressns",
            "releaseName": oaa_release_name,
            "installSettingsPath": str(
                oaa_payload.get("installSettingsPath")
                or existing_oaa.get("installSettingsPath")
                or base["oaa"].get("installSettingsPath")
                or ""
            ).strip(),
            "runtimeBaseUrl": str(
                oaa_payload.get("runtimeBaseUrl")
                or existing_oaa.get("runtimeBaseUrl")
                or base["oaa"].get("runtimeBaseUrl")
                or ""
            ).strip(),
            "runtimeUsername": str(
                oaa_payload.get("runtimeUsername")
                or existing_oaa.get("runtimeUsername")
                or "{0}-oaa".format(oaa_release_name)
            ).strip() or "{0}-oaa".format(oaa_release_name),
            "runtimePassword": preserve_secret(
                oaa_payload.get("runtimePassword"),
                existing_oaa.get("runtimePassword"),
                allow_blank=coerce_bool(oaa_payload.get("clearRuntimePassword"), False),
            ),
            "propertyNames": parse_csv_or_list(
                oaa_payload.get("propertyNames"),
                existing_oaa.get("propertyNames") or DEFAULT_OAA_PROPERTY_NAMES,
            ),
            "kubectlPath": str(
                oaa_payload.get("kubectlPath")
                or existing_oaa.get("kubectlPath")
                or base["oaa"].get("kubectlPath")
                or "kubectl"
            ).strip() or "kubectl",
            "logTailLines": max(10, min(500, as_int(
                oaa_payload.get("logTailLines")
                or existing_oaa.get("logTailLines")
                or base["oaa"].get("logTailLines")
                or 80,
                80,
            ))),
            "database": normalize_oaa_database_payload(
                oaa_payload.get("database"),
                existing_oaa.get("database") or base["oaa"].get("database"),
            ),
            "kubeHost": normalize_ssh_profile_payload(
                oaa_payload.get("kubeHost"),
                existing_oaa.get("kubeHost"),
                default_host=str(server_payload.get("host") or existing_server.get("host") or base["server"].get("host") or "").strip(),
                default_username="root",
                default_port=as_int(server_payload.get("port") or existing_server.get("port") or 22, 22),
                default_mode="root_password",
            ),
        },
        "soa": {
            "oracleHome": str(
                weblogic_payload.get("oracleHome")
                or existing_weblogic.get("oracleHome")
                or soa_payload.get("oracleHome")
                or existing_soa.get("oracleHome")
                or base["soa"].get("oracleHome")
                or ""
            ).strip(),
            "domainHome": str(
                soa_payload.get("domainHome")
                or existing_soa.get("domainHome")
                or base["soa"].get("domainHome")
                or ""
            ).strip(),
            "checks": normalize_checks(
                soa_payload.get("checks") if "checks" in soa_payload else existing_soa.get("checks"),
                [],
            ),
        },
        "collection": {
            "enabled": coerce_bool(
                collection_payload.get("enabled"),
                existing_collection.get("enabled", True),
            ),
            "scheduleMinutes": sanitize_schedule_minutes(
                collection_payload.get("scheduleMinutes")
                or collection_payload.get("intervalMinutes")
                or existing_collection.get("scheduleMinutes")
                or default_collection_minutes(),
                default_collection_minutes(),
            ),
        },
        "bootstrap": {
            "status": str(
                bootstrap_payload.get("status")
                or existing_bootstrap.get("status")
                or base["bootstrap"].get("status")
            ).strip() or "pending",
            "strategy": str(
                bootstrap_payload.get("strategy")
                or existing_bootstrap.get("strategy")
                or base["bootstrap"].get("strategy")
            ).strip() or "initial_ssh_then_runtime_key",
            "initialSshMode": str(
                bootstrap_payload.get("initialSshMode")
                or existing_bootstrap.get("initialSshMode")
                or server_payload.get("sshMode")
                or existing_server.get("sshMode")
                or base["bootstrap"].get("initialSshMode")
            ).strip() or "user_password_sudo",
            "runtimeKeyPath": str(
                bootstrap_payload.get("runtimeKeyPath")
                or existing_bootstrap.get("runtimeKeyPath")
                or base["bootstrap"].get("runtimeKeyPath")
                or ""
            ).strip(),
            "runtimeEnvPath": str(
                bootstrap_payload.get("runtimeEnvPath")
                or existing_bootstrap.get("runtimeEnvPath")
                or base["bootstrap"].get("runtimeEnvPath")
                or ""
            ).strip(),
            "lastBootstrappedAt": str(
                bootstrap_payload.get("lastBootstrappedAt")
                or existing_bootstrap.get("lastBootstrappedAt")
                or base["bootstrap"].get("lastBootstrappedAt")
                or ""
            ).strip(),
            "message": str(
                bootstrap_payload.get("message")
                or existing_bootstrap.get("message")
                or base["bootstrap"].get("message")
            ).strip(),
        },
        "operations": {
            "installMethod": str(
                operations_payload.get("installMethod")
                or (existing.get("operations") or {}).get("installMethod")
                or "environment_ssh"
            ).strip(),
            "upgradeMethod": str(
                operations_payload.get("upgradeMethod")
                or (existing.get("operations") or {}).get("upgradeMethod")
                or "environment_ssh"
            ).strip(),
        },
    }

    if not environment["serverMetrics"]["processMatchers"]:
        environment["serverMetrics"]["processMatchers"] = default_process_matchers(products)

    ssh_mode = normalize_ssh_mode(
        server_payload.get("sshMode") or existing_server.get("sshMode"),
        environment["server"].get("sshMode") or "user_password_sudo",
    )
    environment["server"]["sshMode"] = ssh_mode
    environment["server"]["authType"] = "private_key" if ssh_mode.endswith("_key") else "password"
    if ssh_mode.startswith("root_"):
        environment["server"]["username"] = "root"
        environment["server"]["sudoRequired"] = False
    else:
        environment["server"]["sudoRequired"] = ssh_mode.endswith("_sudo")

    cluster_payload = weblogic_payload.get("cluster") or {}
    existing_cluster = existing_weblogic.get("cluster") or {}
    admin_host_profile = environment["weblogic"].get("adminHost") or {}
    cluster_payload_nodes = cluster_payload.get("nodes") if isinstance(cluster_payload.get("nodes"), list) else []
    existing_cluster_nodes = existing_cluster.get("nodes") if isinstance(existing_cluster.get("nodes"), list) else []
    if not cluster_payload_nodes and cluster_payload.get("node2"):
        cluster_payload_nodes = [cluster_payload.get("node2") or {}]
    if not existing_cluster_nodes and existing_cluster.get("node2"):
        existing_cluster_nodes = [existing_cluster.get("node2") or {}]
    source_cluster_nodes = cluster_payload_nodes or existing_cluster_nodes
    cluster_nodes = []
    for index, node_payload in enumerate(source_cluster_nodes):
        node_payload = node_payload or {}
        existing_node = existing_cluster_nodes[index] if index < len(existing_cluster_nodes) else {}
        node_profile = normalize_ssh_profile_payload(
            node_payload,
            existing_node,
            default_host="",
            default_username=admin_host_profile.get("username") or "oracle",
            default_port=admin_host_profile.get("port") or 22,
            default_mode=admin_host_profile.get("sshMode") or "user_password",
        )
        node_profile["oracleHome"] = str(
            node_payload.get("oracleHome")
            or existing_node.get("oracleHome")
            or environment["weblogic"].get("oracleHome")
            or ""
        ).strip()
        node_profile["domainHome"] = str(
            node_payload.get("domainHome")
            or existing_node.get("domainHome")
            or environment["weblogic"].get("domainHome")
            or ""
        ).strip()
        if not node_profile.get("password") and admin_host_profile.get("password"):
            node_profile["password"] = admin_host_profile.get("password")
        if not node_profile.get("privateKeyPath") and admin_host_profile.get("privateKeyPath"):
            node_profile["privateKeyPath"] = admin_host_profile.get("privateKeyPath")
        if not node_profile.get("passphrase") and admin_host_profile.get("passphrase"):
            node_profile["passphrase"] = admin_host_profile.get("passphrase")
        if node_profile.get("host"):
            cluster_nodes.append(node_profile)
    environment["weblogic"]["cluster"] = {
        "enabled": coerce_bool(cluster_payload.get("enabled"), coerce_bool(existing_cluster.get("enabled"), False)),
        "nodes": cluster_nodes,
        "node2": cluster_nodes[0] if cluster_nodes else normalize_ssh_profile_payload(
            {},
            {},
            default_host="",
            default_username=admin_host_profile.get("username") or "oracle",
            default_port=admin_host_profile.get("port") or 22,
            default_mode=admin_host_profile.get("sshMode") or "user_password",
        ),
    }

    if products.get("oam"):
        environment["oam"]["checks"] = normalize_oam_checks(
            environment["oam"].get("checks"),
            environment["weblogic"].get("adminUrl"),
        )

    if not environment["oud"]["checks"] and products.get("oud"):
        environment["oud"]["checks"] = deep_copy(DEFAULT_OUD_CHECKS)

    environment["weblogic"]["enabled"] = bool(
        products.get("weblogic")
        or products.get("oam")
        or products.get("oig")
        or products.get("soa")
    )
    pure_weblogic = bool(products.get("weblogic")) and not any(
        products.get(key) for key in ("oam", "oud", "oig", "oid", "oaa", "soa")
    )
    if not pure_weblogic:
        environment["weblogic"]["cluster"]["enabled"] = False

    if products.get("oig"):
        environment["oig"]["oracleHome"] = environment["weblogic"].get("oracleHome") or environment["oig"].get("oracleHome") or ""
        environment["oig"]["domainHome"] = environment["weblogic"].get("domainHome") or environment["oig"].get("domainHome") or ""
        environment["weblogic"]["serverNames"] = [
            name for name in (environment["weblogic"].get("serverNames") or [])
            if "soa" not in str(name or "").lower()
        ] or default_weblogic_server_names(products)
    if products.get("soa"):
        environment["soa"]["oracleHome"] = environment["weblogic"].get("oracleHome") or environment["soa"].get("oracleHome") or ""

    return environment


def environment_server_password(server_payload, existing_server):
    candidate = (server_payload or {}).get("password")
    if candidate:
        return str(candidate).strip()
    return str((existing_server or {}).get("password") or "").strip()


def normalize_config(config):
    config = config or {}
    defaults = default_config()
    monitoring_server = config.get("monitoring_server") or {}
    operations = config.get("operations") or {}
    environments = [normalize_environment(item) for item in config.get("environments", [])]

    return {
        "dashboard_title": str(config.get("dashboard_title") or defaults["dashboard_title"]).strip(),
        "monitoring_server": {
            "name": str(monitoring_server.get("name") or defaults["monitoring_server"]["name"]).strip(),
            "host": str(monitoring_server.get("host") or defaults["monitoring_server"]["host"]).strip(),
            "description": str(
                monitoring_server.get("description") or defaults["monitoring_server"]["description"]
            ).strip(),
            "servicePort": as_int(
                monitoring_server.get("servicePort") or defaults["monitoring_server"]["servicePort"],
                defaults["monitoring_server"]["servicePort"],
            ),
            "processMatchers": parse_csv_or_list(
                monitoring_server.get("processMatchers"),
                defaults["monitoring_server"]["processMatchers"],
            ),
        },
        "operations": {
            "installMethod": str(operations.get("installMethod") or defaults["operations"]["installMethod"]).strip(),
            "upgradeMethod": str(operations.get("upgradeMethod") or defaults["operations"]["upgradeMethod"]).strip(),
        },
        "environments": environments or defaults["environments"],
    }


def migrate_legacy_config(config):
    if not config or "targets" not in config:
        return normalize_config(config)

    monitoring_target = None
    application_target = None
    for target in config.get("targets", []):
        role = str((target or {}).get("role") or "").lower()
        if monitoring_target is None and "monitoring" in role:
            monitoring_target = target
        elif application_target is None and ("oam" in str((target or {}).get("name") or "").lower() or target.get("oam") or target.get("oud")):
            application_target = target

    if monitoring_target is None and config.get("targets"):
        monitoring_target = config["targets"][0]
    if application_target is None and len(config.get("targets", [])) > 1:
        application_target = config["targets"][1]

    migrated = default_config()
    migrated["dashboard_title"] = config.get("dashboard_title") or migrated["dashboard_title"]

    if monitoring_target:
        migrated["monitoring_server"]["name"] = monitoring_target.get("name") or "IAM Monitoring Server"
        migrated["monitoring_server"]["host"] = monitoring_target.get("host") or migrated["monitoring_server"]["host"]
        migrated["monitoring_server"]["processMatchers"] = monitoring_target.get("process_matchers") or migrated["monitoring_server"]["processMatchers"]

    if application_target:
        legacy_checks = map_legacy_checks(application_target.get("app_checks") or [])
        seed = default_environment(
            application_target.get("name") or "Imported IAM Environment",
            application_target.get("host"),
        )
        seed["description"] = "Imported from the earlier dashboard target configuration."
        seed["server"]["host"] = application_target.get("host") or seed["server"]["host"]
        seed["server"]["username"] = application_target.get("username") or seed["server"]["username"]
        seed["server"]["password"] = application_target.get("password") or seed["server"]["password"]
        seed["serverMetrics"]["scriptDirectory"] = application_target.get("script_directory") or seed["serverMetrics"]["scriptDirectory"]
        seed["serverMetrics"]["processMatchers"] = application_target.get("process_matchers") or seed["serverMetrics"]["processMatchers"]
        if application_target.get("oam"):
            seed["weblogic"]["jstatPath"] = (application_target.get("oam") or {}).get("jstat_path") or seed["weblogic"]["jstatPath"]
            seed["weblogic"]["serverNames"] = (application_target.get("oam") or {}).get("server_names") or seed["weblogic"]["serverNames"]
        if application_target.get("oud"):
            legacy_oud = application_target.get("oud") or {}
            seed["oud"]["statusPath"] = legacy_oud.get("status_path") or seed["oud"]["statusPath"]
            seed["oud"]["bindDn"] = legacy_oud.get("bind_dn") or seed["oud"]["bindDn"]
            seed["oud"]["bindPassword"] = legacy_oud.get("bind_password") or seed["oud"]["bindPassword"]
            seed["oud"]["ldapUrl"] = legacy_oud.get("ldap_url") or seed["oud"]["ldapUrl"]
        if legacy_checks:
            seed["oam"]["checks"] = [item for item in legacy_checks if item["product"] == "oam"]
            seed["oud"]["checks"] = [item for item in legacy_checks if item["product"] == "oud"] or seed["oud"]["checks"]
            seed["oig"]["checks"] = [item for item in legacy_checks if item["product"] == "oig"]
        migrated["environments"] = [normalize_environment(seed)]

    return normalize_config(migrated)


def load_config(path):
    if not os.path.isfile(path):
        config = normalize_config(default_config())
        save_config(path, config)
        return config

    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if "targets" in raw and "environments" not in raw:
        raw = migrate_legacy_config(raw)
        save_config(path, raw)
        return raw

    config = normalize_config(raw)
    if config != raw:
        save_config(path, config)
    return config


def save_config(path, config):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temp_path = "{0}.tmp".format(path)
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(temp_path, path)


def find_environment(config, environment_id):
    for environment in config.get("environments", []):
        if environment.get("id") == environment_id:
            return environment
    return None


def unique_environment_id(config, base_id, exclude_id=None):
    existing_ids = [item.get("id") for item in config.get("environments", []) if item.get("id") != exclude_id]
    candidate = slugify(base_id)
    if candidate not in existing_ids:
        return candidate
    index = 2
    while True:
        trial = "{0}-{1}".format(candidate, index)
        if trial not in existing_ids:
            return trial
        index += 1


def upsert_environment(config, payload, environment_id=None):
    config.setdefault("environments", [])
    existing = find_environment(config, environment_id or payload.get("id"))
    normalized = normalize_environment(payload, existing)
    normalized["id"] = unique_environment_id(config, normalized.get("id") or normalized.get("name"), existing.get("id") if existing else None)

    if existing:
        for index, environment in enumerate(config["environments"]):
            if environment.get("id") == existing.get("id"):
                config["environments"][index] = normalized
                return normalized

    config["environments"].append(normalized)
    return normalized


def delete_environment(config, environment_id):
    environments = config.get("environments", [])
    updated = [environment for environment in environments if environment.get("id") != environment_id]
    deleted = len(updated) != len(environments)
    config["environments"] = updated
    return deleted


def serialize_oud_instances(instances, include_sensitive=False):
    rows = []
    for index, item in enumerate(instances or []):
        row = item or {}
        serialized = {
            "name": row.get("name") or "OUD Instance {0}".format(index + 1),
            "instanceHome": row.get("instanceHome") or "",
            "bindDn": row.get("bindDn") or "cn=Directory Manager",
            "bindPassword": (row.get("bindPassword") or "") if include_sensitive else "",
            "hasBindPassword": bool(row.get("bindPassword")),
            "ldapPort": row.get("ldapPort") or 1389,
            "adminPort": row.get("adminPort") or 4444,
        }
        rows.append(serialized)
    return rows


def serialize_environment(environment, include_sensitive=False):
    environment = environment or {}
    server = environment.get("server") or {}
    oud = environment.get("oud") or {}
    oid = environment.get("oid") or {}
    oig = environment.get("oig") or {}
    oaa = environment.get("oaa") or {}
    oaa_kube_host = oaa.get("kubeHost") or {}
    weblogic = environment.get("weblogic") or {}
    weblogic_admin_host = weblogic.get("adminHost") or {}
    weblogic_cluster = weblogic.get("cluster") or {}
    weblogic_node2 = weblogic_cluster.get("node2") or {}
    weblogic_cluster_nodes = weblogic_cluster.get("nodes") if isinstance(weblogic_cluster.get("nodes"), list) else []
    if not weblogic_cluster_nodes and weblogic_node2.get("host"):
        weblogic_cluster_nodes = [weblogic_node2]

    def serialize_weblogic_cluster_node(node):
        node = node or {}
        return {
            "mode": node.get("mode") or "ssh",
            "host": node.get("host") or "",
            "port": node.get("port") or 22,
            "username": node.get("username") or "",
            "sshMode": normalize_ssh_mode(node.get("sshMode"), "user_password"),
            "authType": node.get("authType") or "password",
            "sudoRequired": bool(node.get("sudoRequired")),
            "privateKeyPath": node.get("privateKeyPath") or "",
            "hasPassword": bool(node.get("password")),
            "hasPassphrase": bool(node.get("passphrase")),
            "oracleHome": node.get("oracleHome") or weblogic.get("oracleHome") or "",
            "domainHome": node.get("domainHome") or weblogic.get("domainHome") or "",
        }

    payload = {
        "id": environment.get("id"),
        "name": environment.get("name"),
        "description": environment.get("description"),
        "environmentType": normalize_environment_type(environment.get("environmentType"), ""),
        "server": {
            "mode": server.get("mode") or "ssh",
            "host": server.get("host") or "",
            "port": server.get("port") or 22,
            "username": server.get("username") or "",
            "sshMode": normalize_ssh_mode(server.get("sshMode"), "user_password_sudo"),
            "authType": server.get("authType") or "password",
            "sudoRequired": bool(server.get("sudoRequired")),
            "privateKeyPath": server.get("privateKeyPath") or "",
            "hasPassword": bool(server.get("password")),
            "hasPassphrase": bool(server.get("passphrase")),
        },
        "products": deep_copy(environment.get("products") or {}),
        "serverMetrics": deep_copy(environment.get("serverMetrics") or {}),
        "weblogic": {
            "enabled": bool(weblogic.get("enabled")),
            "adminUrl": weblogic.get("adminUrl") or "",
            "adminUsername": weblogic.get("adminUsername") or "",
            "adminPassword": "",
            "hasAdminPassword": bool(weblogic.get("adminPassword")),
            "oracleHome": weblogic.get("oracleHome") or "",
            "domainHome": weblogic.get("domainHome") or "",
            "adminHost": {
                "mode": weblogic_admin_host.get("mode") or "ssh",
                "host": weblogic_admin_host.get("host") or "",
                "port": weblogic_admin_host.get("port") or 22,
                "username": weblogic_admin_host.get("username") or "",
                "sshMode": normalize_ssh_mode(weblogic_admin_host.get("sshMode"), "user_password"),
                "authType": weblogic_admin_host.get("authType") or "password",
                "sudoRequired": bool(weblogic_admin_host.get("sudoRequired")),
                "privateKeyPath": weblogic_admin_host.get("privateKeyPath") or "",
                "hasPassword": bool(weblogic_admin_host.get("password")),
                "hasPassphrase": bool(weblogic_admin_host.get("passphrase")),
            },
            "cluster": {
                "enabled": bool(weblogic_cluster.get("enabled")),
                "nodes": [serialize_weblogic_cluster_node(node) for node in weblogic_cluster_nodes],
                "node2": serialize_weblogic_cluster_node(weblogic_cluster_nodes[0] if weblogic_cluster_nodes else weblogic_node2),
            },
            "jstatPath": weblogic.get("jstatPath") or "",
            "serverNames": deep_copy(weblogic.get("serverNames") or []),
        },
        "oam": deep_copy(environment.get("oam") or {}),
        "collection": deep_copy(environment.get("collection") or {}),
        "bootstrap": deep_copy(environment.get("bootstrap") or {}),
        "oud": {
            "host": oud.get("host") or "",
            "port": oud.get("port"),
            "weblogicEnabled": coerce_bool(oud.get("weblogicEnabled"), False),
            "oracleHome": oud.get("oracleHome") or "",
            "domainHome": oud.get("domainHome") or "",
            "instanceHome": oud.get("instanceHome") or "",
            "instances": serialize_oud_instances(oud.get("instances") or [], include_sensitive=include_sensitive),
            "statusPath": oud.get("statusPath") or "",
            "bindDn": oud.get("bindDn") or "",
            "bindPassword": "",
            "hasBindPassword": bool(oud.get("bindPassword")),
            "ldapPort": oud.get("ldapPort") or 1389,
            "adminPort": oud.get("adminPort") or 4444,
            "ldapUrl": oud.get("ldapUrl") or "",
            "checks": deep_copy(oud.get("checks") or []),
        },
        "oid": {
            "host": oid.get("host") or "",
            "weblogicEnabled": coerce_bool(oid.get("weblogicEnabled"), False),
            "oracleHome": oid.get("oracleHome") or "",
            "adminUsername": oid.get("adminUsername") or "cn=orcladmin",
            "adminPassword": "",
            "hasAdminPassword": bool(oid.get("adminPassword")),
            "ldapPort": oid.get("ldapPort"),
            "ldapsPort": oid.get("ldapsPort"),
            "queryGroups": deep_copy(oid.get("queryGroups") or DEFAULT_OID_QUERY_GROUPS),
            "checks": deep_copy(oid.get("checks") or []),
        },
        "oig": {
            "oracleHome": oig.get("oracleHome") or "",
            "domainHome": oig.get("domainHome") or "",
            "adminUrl": "",
            "adminUsername": oig.get("adminUsername") or "xelsysadm",
            "adminPassword": "",
            "hasAdminPassword": bool(oig.get("adminPassword")),
            "checks": deep_copy(oig.get("checks") or []),
        },
        "oaa": {
            "namespace": oaa.get("namespace") or "oaans",
            "ingressNamespace": oaa.get("ingressNamespace") or "ingressns",
            "releaseName": oaa.get("releaseName") or "oaainstall",
            "installSettingsPath": oaa.get("installSettingsPath") or "",
            "runtimeBaseUrl": oaa.get("runtimeBaseUrl") or "",
            "runtimeUsername": oaa.get("runtimeUsername") or "",
            "runtimePassword": "",
            "hasRuntimePassword": bool(oaa.get("runtimePassword")),
            "propertyNames": deep_copy(oaa.get("propertyNames") or []),
            "kubectlPath": oaa.get("kubectlPath") or "kubectl",
            "logTailLines": oaa.get("logTailLines") or 80,
            "database": normalize_oaa_database_payload(oaa.get("database")),
            "kubeHost": {
                "mode": oaa_kube_host.get("mode") or "ssh",
                "host": oaa_kube_host.get("host") or "",
                "port": oaa_kube_host.get("port") or 22,
                "username": oaa_kube_host.get("username") or "",
                "sshMode": normalize_ssh_mode(oaa_kube_host.get("sshMode"), "root_password"),
                "authType": oaa_kube_host.get("authType") or "password",
                "sudoRequired": bool(oaa_kube_host.get("sudoRequired")),
                "privateKeyPath": oaa_kube_host.get("privateKeyPath") or "",
                "hasPassword": bool(oaa_kube_host.get("password")),
                "hasPassphrase": bool(oaa_kube_host.get("passphrase")),
            },
        },
        "soa": deep_copy(environment.get("soa") or {}),
        "operations": deep_copy(environment.get("operations") or {}),
    }

    if include_sensitive:
        payload["server"]["password"] = server.get("password") or ""
        payload["server"]["passphrase"] = server.get("passphrase") or ""
        payload["weblogic"]["adminPassword"] = weblogic.get("adminPassword") or ""
        payload["weblogic"]["adminHost"]["password"] = weblogic_admin_host.get("password") or ""
        payload["weblogic"]["adminHost"]["passphrase"] = weblogic_admin_host.get("passphrase") or ""
        for index, node in enumerate(weblogic_cluster_nodes):
            payload["weblogic"]["cluster"]["nodes"][index]["password"] = (node or {}).get("password") or ""
            payload["weblogic"]["cluster"]["nodes"][index]["passphrase"] = (node or {}).get("passphrase") or ""
        if weblogic_cluster_nodes:
            payload["weblogic"]["cluster"]["node2"]["password"] = (weblogic_cluster_nodes[0] or {}).get("password") or ""
            payload["weblogic"]["cluster"]["node2"]["passphrase"] = (weblogic_cluster_nodes[0] or {}).get("passphrase") or ""
        else:
            payload["weblogic"]["cluster"]["node2"]["password"] = weblogic_node2.get("password") or ""
            payload["weblogic"]["cluster"]["node2"]["passphrase"] = weblogic_node2.get("passphrase") or ""
        payload["oud"]["bindPassword"] = oud.get("bindPassword") or ""
        payload["oid"]["adminPassword"] = oid.get("adminPassword") or ""
        payload["oig"]["adminPassword"] = oig.get("adminPassword") or ""
        payload["oaa"]["runtimePassword"] = oaa.get("runtimePassword") or ""
        payload["oaa"]["kubeHost"]["password"] = oaa_kube_host.get("password") or ""
        payload["oaa"]["kubeHost"]["passphrase"] = oaa_kube_host.get("passphrase") or ""

    return payload
