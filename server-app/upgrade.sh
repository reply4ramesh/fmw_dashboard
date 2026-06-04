#!/usr/bin/env bash
set -euo pipefail

PRODUCT_NAME="FMW Monitoring Dashboard"
PRODUCT_SLUG="iam-monitoring"
SERVICE_NAME="iam-monitoring"
UPGRADE_SERVICE_NAME="iam-monitoring-upgrader"
INSTALL_DIR="/opt/iam-monitoring"
CONFIG_FILE="/etc/iam-monitoring.env"
STATE_DIR="/var/lib/iam-monitoring/state"
LOG_DIR="/var/log/iam-monitoring"
SERVICE_USER="iam-monitoring"
SKIP_RESTART=0
BACKUP_ROOT="/opt/iam-monitoring-backup"
HEALTH_PORT="8081"
PKG_MGR=""
CRON_SERVICE_NAME="cron"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR=""
ARCHIVE=""

usage() {
  cat <<'EOF'
Usage:
  sudo bash ./upgrade.sh [options]

Options:
  --install-dir /opt/iam-monitoring
  --config-file /etc/iam-monitoring.env
  --state-dir /var/lib/iam-monitoring/state
  --log-dir /var/log/iam-monitoring
  --user iam-monitoring
  --backup-root /opt/iam-monitoring-backup
  --archive /tmp/iam-monitoring.tar.gz
  --skip-restart

This preserves the installed environment data and runtime config, backs up the current
application tree, stages the new bundle, refreshes the virtualenv, validates the app
modules, and restarts the service.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    --config-file) CONFIG_FILE="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --log-dir) LOG_DIR="${2:-}"; shift 2 ;;
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --backup-root) BACKUP_ROOT="${2:-}"; shift 2 ;;
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --skip-restart) SKIP_RESTART=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

cleanup() {
  if [[ -n "${TEMP_DIR}" && -d "${TEMP_DIR}" ]]; then
    rm -rf "${TEMP_DIR}"
  fi
}
trap cleanup EXIT

section() {
  echo
  echo "==> $1"
}

detect_platform_tools() {
  if command -v apt >/dev/null 2>&1; then
    PKG_MGR="apt"
    CRON_SERVICE_NAME="cron"
  elif command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
    CRON_SERVICE_NAME="crond"
  elif command -v yum >/dev/null 2>&1; then
    PKG_MGR="yum"
    CRON_SERVICE_NAME="crond"
  else
    echo "Unsupported system: no apt, dnf, or yum package manager found." >&2
    exit 1
  fi
}

ensure_config_setting() {
  local key="$1"
  local value="$2"
  if grep -Eq "^${key}=" "${CONFIG_FILE}" 2>/dev/null; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "${CONFIG_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${CONFIG_FILE}"
  fi
}

read_version_file() {
  local version_file="$1"
  if [[ -f "${version_file}" ]]; then
    tr -d '\r' < "${version_file}" | head -n 1
  else
    printf 'unknown'
  fi
}

copy_dir_contents() {
  local src_dir="$1"
  local dst_dir="$2"
  mkdir -p "${dst_dir}"
  if [[ -d "${src_dir}" ]]; then
    cp -a "${src_dir}/." "${dst_dir}/"
  fi
}

resolve_source_dir() {
  if [[ -n "${ARCHIVE}" ]]; then
    if [[ ! -f "${ARCHIVE}" ]]; then
      echo "Archive not found: ${ARCHIVE}" >&2
      exit 1
    fi
    TEMP_DIR="$(mktemp -d)"
    case "${ARCHIVE}" in
      *.zip)
        unzip -q "${ARCHIVE}" -d "${TEMP_DIR}"
        ;;
      *.tar.gz|*.tgz)
        tar -xzf "${ARCHIVE}" -C "${TEMP_DIR}"
        ;;
      *.tar)
        tar -xf "${ARCHIVE}" -C "${TEMP_DIR}"
        ;;
      *)
        echo "Unsupported archive format: ${ARCHIVE}" >&2
        exit 1
        ;;
    esac
    if [[ -f "${TEMP_DIR}/app.py" ]]; then
      printf '%s' "${TEMP_DIR}"
      return
    fi
    while IFS= read -r candidate; do
      if [[ -f "${candidate}/app.py" && -f "${candidate}/requirements.txt" ]]; then
        printf '%s' "${candidate}"
        return 0
      fi
    done < <(find "${TEMP_DIR}" -mindepth 1 -maxdepth 2 -type d)
    echo "Could not locate the IAM dashboard bundle inside ${ARCHIVE}" >&2
    exit 1
  fi
  printf '%s' "${SCRIPT_DIR}"
}

validate_source_dir() {
  local source_dir="$1"
  for required_path in \
    app.py \
    collect_environment.py \
    collector.py \
    config_store.py \
    environment_registry.py \
    job_runner.py \
    notification_store.py \
    support_store.py \
    upgrade.sh \
    upgrade_runtime.py \
    upgrade_watcher.py \
    requirements.txt \
    scheduler_jobs.sh \
    static \
    deploy \
    deploy/iam-monitoring-upgrader.service; do
    if [[ ! -e "${source_dir}/${required_path}" ]]; then
      echo "Bundle is missing ${required_path} in ${source_dir}" >&2
      exit 1
    fi
  done
}

handoff_to_bundle_upgrade_script() {
  local source_dir="$1"
  local bundle_upgrade_script="${source_dir}/upgrade.sh"
  local -a handoff_args=()

  if [[ -n "${IAM_MONITORING_UPGRADE_HANDOFF:-}" ]]; then
    return
  fi

  if [[ "${source_dir}" == "${SCRIPT_DIR}" ]]; then
    return
  fi

  if [[ ! -x "${bundle_upgrade_script}" && ! -f "${bundle_upgrade_script}" ]]; then
    return
  fi

  handoff_args+=(--install-dir "${INSTALL_DIR}")
  handoff_args+=(--config-file "${CONFIG_FILE}")
  handoff_args+=(--state-dir "${STATE_DIR}")
  handoff_args+=(--log-dir "${LOG_DIR}")
  handoff_args+=(--user "${SERVICE_USER}")
  handoff_args+=(--backup-root "${BACKUP_ROOT}")
  if [[ "${SKIP_RESTART}" -eq 1 ]]; then
    handoff_args+=(--skip-restart)
  fi

  section "Handing off to bundle upgrade script"
  echo "Using the bundle's own upgrade.sh from ${source_dir}"
  export IAM_MONITORING_UPGRADE_HANDOFF=1
  exec bash "${bundle_upgrade_script}" "${handoff_args[@]}"
}

copy_bundle_contents() {
  local source_dir="$1"
  local target_dir="$2"

  mkdir -p "${target_dir}"
  tar \
    --exclude='.git' \
    --exclude='venv' \
    --exclude='state' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    -C "${source_dir}" -cf - . | tar -C "${target_dir}" -xf -
}

install_python_requirements() {
  local requirements_file="$1"
  if grep -Eq '^[[:space:]]*[^#[:space:]]' "${requirements_file}"; then
    "${INSTALL_DIR}/venv/bin/pip" install --disable-pip-version-check -r "${requirements_file}"
  else
    echo "No external Python requirements declared."
  fi
}

service_template_path() {
  printf '%s' "${INSTALL_DIR}/deploy/oracledash.service"
}

upgrade_service_template_path() {
  printf '%s' "${INSTALL_DIR}/deploy/iam-monitoring-upgrader.service"
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this upgrade as root, for example: sudo bash ./upgrade.sh" >&2
  exit 1
fi

if [[ ! -d "${INSTALL_DIR}" ]]; then
  echo "Install directory not found: ${INSTALL_DIR}" >&2
  exit 1
fi

if [[ ! -f "${INSTALL_DIR}/requirements.txt" ]]; then
  echo "requirements.txt not found in ${INSTALL_DIR}" >&2
  exit 1
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

detect_platform_tools
SOURCE_DIR="$(resolve_source_dir)"
validate_source_dir "${SOURCE_DIR}"
handoff_to_bundle_upgrade_script "${SOURCE_DIR}"
CURRENT_VERSION="$(read_version_file "${INSTALL_DIR}/VERSION")"
PACKAGE_VERSION="$(read_version_file "${SOURCE_DIR}/VERSION")"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="${BACKUP_ROOT}/${PRODUCT_SLUG}-${CURRENT_VERSION}-${TIMESTAMP}"

section "Preparing upgrade"
echo "Product:      ${PRODUCT_NAME}"
echo "Current ver:  ${CURRENT_VERSION}"
echo "Package ver:  ${PACKAGE_VERSION}"
echo "Package dir:  ${SOURCE_DIR}"
echo "Install dir:  ${INSTALL_DIR}"
echo "Config file:  ${CONFIG_FILE}"
echo "State dir:    ${STATE_DIR}"
echo "Log dir:      ${LOG_DIR}"
echo "Service user: ${SERVICE_USER}"
echo "Backup dir:   ${BACKUP_DIR}"

section "Creating backup"
mkdir -p "${BACKUP_DIR}/app" "${BACKUP_DIR}/service"
copy_dir_contents "${INSTALL_DIR}" "${BACKUP_DIR}/app"
if [[ -f "${CONFIG_FILE}" ]]; then
  mkdir -p "${BACKUP_DIR}/config"
  cp -a "${CONFIG_FILE}" "${BACKUP_DIR}/config/"
fi
if [[ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
  cp -a "/etc/systemd/system/${SERVICE_NAME}.service" "${BACKUP_DIR}/service/"
fi
if [[ -f "/etc/systemd/system/${UPGRADE_SERVICE_NAME}.service" ]]; then
  cp -a "/etc/systemd/system/${UPGRADE_SERVICE_NAME}.service" "${BACKUP_DIR}/service/"
fi

cat > "${BACKUP_DIR}/restore.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
if [[ "\${EUID}" -ne 0 ]]; then
  echo "Run this restore as root." >&2
  exit 1
fi
systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
rm -rf "${INSTALL_DIR}"
mkdir -p "$(dirname "${INSTALL_DIR}")"
cp -a "${BACKUP_DIR}/app/." "${INSTALL_DIR}/"
if [[ -f "${BACKUP_DIR}/config/$(basename "${CONFIG_FILE}")" ]]; then
  cp -a "${BACKUP_DIR}/config/$(basename "${CONFIG_FILE}")" "${CONFIG_FILE}"
fi
if [[ -f "${BACKUP_DIR}/service/${SERVICE_NAME}.service" ]]; then
  cp -a "${BACKUP_DIR}/service/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
fi
if [[ -f "${BACKUP_DIR}/service/${UPGRADE_SERVICE_NAME}.service" ]]; then
  cp -a "${BACKUP_DIR}/service/${UPGRADE_SERVICE_NAME}.service" "/etc/systemd/system/${UPGRADE_SERVICE_NAME}.service"
fi
systemctl daemon-reload
systemctl restart "${SERVICE_NAME}"
systemctl is-active "${SERVICE_NAME}"
systemctl start "${UPGRADE_SERVICE_NAME}" >/dev/null 2>&1 || true
EOF
chmod +x "${BACKUP_DIR}/restore.sh"

section "Ensuring runtime directories"
mkdir -p "${STATE_DIR}" "${STATE_DIR}/upgrade" "${LOG_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}" "${LOG_DIR}" || true
chmod 775 "${STATE_DIR}" "${STATE_DIR}/upgrade" "${LOG_DIR}" || true
find "${STATE_DIR}/upgrade" -maxdepth 1 -type f -exec chmod 664 {} \; 2>/dev/null || true

section "Staging updated application bundle"
copy_bundle_contents "${SOURCE_DIR}" "${INSTALL_DIR}"
chmod +x \
  "${INSTALL_DIR}/collect_environment.py" \
  "${INSTALL_DIR}/install.sh" \
  "${INSTALL_DIR}/install_oracledash.sh" \
  "${INSTALL_DIR}/scheduler_jobs.sh" \
  "${INSTALL_DIR}/upgrade_watcher.py" \
  "${INSTALL_DIR}/upgrade.sh"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" "${STATE_DIR}" "${LOG_DIR}" || true

section "Refreshing Python virtual environment"
if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then
  python3 -m venv "${INSTALL_DIR}/venv"
fi
install_python_requirements "${INSTALL_DIR}/requirements.txt"

section "Ensuring runtime environment file"
if [[ ! -f "${CONFIG_FILE}" ]]; then
  cat > "${CONFIG_FILE}" <<EOF
IAM_MONITORING_HOST=0.0.0.0
IAM_MONITORING_PORT=8081
IAM_MONITORING_CACHE_SECONDS=60
IAM_MONITORING_DB_PATH=${STATE_DIR}/iam-monitoring.sqlite
IAM_MONITORING_LOG_DIR=${LOG_DIR}
IAM_MONITORING_SERVICE_USER=${SERVICE_USER}
IAM_MONITORING_DEFAULT_COLLECTION_MINUTES=60
IAM_MONITORING_SCHEDULER_MINUTES=5
# Optional outbound proxy for GitHub update checks
# IAM_MONITORING_HTTP_PROXY=http://proxy.example.com:80
# IAM_MONITORING_HTTPS_PROXY=http://proxy.example.com:80
# IAM_MONITORING_NO_PROXY=127.0.0.1,localhost
EOF
  chmod 640 "${CONFIG_FILE}"
  chown root:"${SERVICE_USER}" "${CONFIG_FILE}" || true
fi

ensure_config_setting "IAM_MONITORING_LOG_DIR" "${LOG_DIR}"
ensure_config_setting "IAM_MONITORING_SERVICE_USER" "${SERVICE_USER}"
ensure_config_setting "IAM_MONITORING_DEFAULT_COLLECTION_MINUTES" "$(grep -E '^IAM_MONITORING_DEFAULT_COLLECTION_MINUTES=' "${CONFIG_FILE}" | tail -n 1 | cut -d= -f2- || echo 60)"
ensure_config_setting "IAM_MONITORING_SCHEDULER_MINUTES" "5"

if [[ -f "${CONFIG_FILE}" ]]; then
  HEALTH_PORT="$(grep -E '^IAM_MONITORING_PORT=' "${CONFIG_FILE}" | tail -n 1 | cut -d= -f2- || true)"
  HEALTH_PORT="${HEALTH_PORT:-8081}"
fi

section "Installing updated systemd service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_TEMPLATE="$(service_template_path)"
sed \
  -e "s|__PRODUCT_NAME__|${PRODUCT_NAME}|g" \
  -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
  -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
  -e "s|__CONFIG_FILE__|${CONFIG_FILE}|g" \
  "${SERVICE_TEMPLATE}" > "${SERVICE_FILE}"

UPGRADE_SERVICE_FILE="/etc/systemd/system/${UPGRADE_SERVICE_NAME}.service"
UPGRADE_SERVICE_TEMPLATE="$(upgrade_service_template_path)"
sed \
  -e "s|__PRODUCT_NAME__|${PRODUCT_NAME}|g" \
  -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
  -e "s|__CONFIG_FILE__|${CONFIG_FILE}|g" \
  "${UPGRADE_SERVICE_TEMPLATE}" > "${UPGRADE_SERVICE_FILE}"
systemctl daemon-reload

section "Installing updated collector scheduler"
CRON_FILE="/etc/cron.d/${PRODUCT_SLUG}"
CRON_TEMPLATE="${INSTALL_DIR}/deploy/crontab.iam-monitoring"
CRON_TMP="$(mktemp)"
cp "${CRON_TEMPLATE}" "${CRON_TMP}"
sed -i \
  -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
  -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
  -e "s|__LOG_DIR__|${LOG_DIR}|g" \
  -e "s|__CONFIG_FILE__|${CONFIG_FILE}|g" \
  "${CRON_TMP}"
cp "${CRON_TMP}" "${CRON_FILE}"
rm -f "${CRON_TMP}"
chmod 644 "${CRON_FILE}"
touch "${LOG_DIR}/scheduler.log"
chown "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}/scheduler.log" || true

section "Validating application modules"
"${INSTALL_DIR}/venv/bin/python" -m py_compile \
  "${INSTALL_DIR}/app.py" \
  "${INSTALL_DIR}/collect_environment.py" \
  "${INSTALL_DIR}/collector.py" \
  "${INSTALL_DIR}/config_store.py" \
  "${INSTALL_DIR}/environment_registry.py" \
  "${INSTALL_DIR}/job_runner.py" \
  "${INSTALL_DIR}/notification_store.py" \
  "${INSTALL_DIR}/support_store.py" \
  "${INSTALL_DIR}/upgrade_runtime.py" \
  "${INSTALL_DIR}/upgrade_watcher.py"

if [[ "${SKIP_RESTART}" -eq 0 ]]; then
  section "Restarting service"
  systemctl restart "${SERVICE_NAME}"
  systemctl is-active "${SERVICE_NAME}"
else
  section "Skipping service restart"
  echo "Upgrade validation completed without restarting ${SERVICE_NAME}."
fi
systemctl enable --now "${CRON_SERVICE_NAME}" || true
systemctl enable "${UPGRADE_SERVICE_NAME}" || true
systemctl start "${UPGRADE_SERVICE_NAME}" || true

section "Upgrade complete"
echo "${PRODUCT_NAME} is ready."
echo "Installed service: ${SERVICE_NAME}"
echo "Installed upgrade helper: ${UPGRADE_SERVICE_NAME}"
echo "Cron service: ${CRON_SERVICE_NAME}"
echo "Health check: http://<server-ip>:${HEALTH_PORT}/healthz"
echo "Useful checks:"
echo "  sudo systemctl status ${SERVICE_NAME} --no-pager"
echo "  sudo systemctl status ${UPGRADE_SERVICE_NAME} --no-pager"
echo "  curl -I http://127.0.0.1:${HEALTH_PORT}/healthz"
echo "  curl http://127.0.0.1:${HEALTH_PORT}/healthz"
echo "  sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
echo "  sudo tail -F ${LOG_DIR}/scheduler.log"
echo
echo "If GitHub update checks need a proxy, add these to ${CONFIG_FILE} and restart ${SERVICE_NAME}:"
echo "  IAM_MONITORING_HTTP_PROXY=http://proxy.example.com:80"
echo "  IAM_MONITORING_HTTPS_PROXY=http://proxy.example.com:80"
echo "  IAM_MONITORING_NO_PROXY=127.0.0.1,localhost"
