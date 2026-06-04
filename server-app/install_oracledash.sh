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
DASHBOARD_PORT="8081"
DASHBOARD_PORT_SET=0
COLLECTOR_MINUTES="60"
COLLECTOR_MINUTES_SET=0
SKIP_OS_PACKAGES="${SKIP_OS_PACKAGES:-0}"
PKG_MGR=""
CRON_SERVICE_NAME="cron"
ARCHIVE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR=""

usage() {
  cat <<'EOF'
Usage:
  sudo bash ./install_oracledash.sh [options]

Options:
  --install-dir /opt/iam-monitoring
  --config-file /etc/iam-monitoring.env
  --state-dir /var/lib/iam-monitoring/state
  --log-dir /var/log/iam-monitoring
  --user iam-monitoring
  --port 8081
  --collector-minutes 60
  --archive /tmp/iam-monitoring.tar.gz
  --skip-os-packages

This installer sets up the IAM dashboard service, Python virtualenv, runtime state,
the admin-editable environment file used by systemd, and the host scheduler used
for per-environment collectors. It can install from the current bundle directory
or from a tar/zip archive.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    --config-file) CONFIG_FILE="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --log-dir) LOG_DIR="${2:-}"; shift 2 ;;
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --port) DASHBOARD_PORT="${2:-}"; DASHBOARD_PORT_SET=1; shift 2 ;;
    --collector-minutes) COLLECTOR_MINUTES="${2:-}"; COLLECTOR_MINUTES_SET=1; shift 2 ;;
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --skip-os-packages) SKIP_OS_PACKAGES=1; shift ;;
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

prompt_for_port() {
  if [[ "${DASHBOARD_PORT_SET}" -eq 1 ]]; then
    return
  fi

  if [[ -t 0 ]]; then
    local reply=""
    read -r -p "Dashboard port [8081]: " reply
    reply="${reply:-8081}"
    DASHBOARD_PORT="${reply}"
  fi

  if [[ -z "${DASHBOARD_PORT}" ]]; then
    DASHBOARD_PORT="8081"
  fi

  if ! [[ "${DASHBOARD_PORT}" =~ ^[0-9]+$ ]] || (( DASHBOARD_PORT < 1 || DASHBOARD_PORT > 65535 )); then
    echo "Invalid port: ${DASHBOARD_PORT}. Use a number between 1 and 65535." >&2
    exit 1
  fi
}

prompt_for_collector_minutes() {
  if [[ "${COLLECTOR_MINUTES_SET}" -eq 1 ]]; then
    :
  elif [[ -t 0 ]]; then
    local reply=""
    read -r -p "Default collector interval in minutes [60]: " reply
    COLLECTOR_MINUTES="${reply:-60}"
  fi

  if [[ -z "${COLLECTOR_MINUTES}" ]]; then
    COLLECTOR_MINUTES="60"
  fi

  if ! [[ "${COLLECTOR_MINUTES}" =~ ^[0-9]+$ ]] || (( COLLECTOR_MINUTES < 5 )); then
    echo "Invalid collector interval: ${COLLECTOR_MINUTES}. Use a whole number of minutes, 5 or greater." >&2
    exit 1
  fi
}

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

install_os_packages() {
  case "${PKG_MGR}" in
    apt)
      apt update
      apt install -y "$@"
      ;;
    dnf)
      dnf install -y "$@"
      ;;
    yum)
      yum install -y "$@"
      ;;
  esac
}

install_base_packages() {
  case "${PKG_MGR}" in
    apt)
      install_os_packages python3 python3-venv python3-pip openssh-client sshpass tar unzip cron
      ;;
    dnf|yum)
      install_os_packages python3 python3-pip openssh-clients sshpass tar unzip cronie
      ;;
  esac
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
  echo "Run this installer as root, for example: sudo bash ./install_oracledash.sh" >&2
  exit 1
fi

detect_platform_tools
SOURCE_DIR="$(resolve_source_dir)"
validate_source_dir "${SOURCE_DIR}"
prompt_for_port
prompt_for_collector_minutes

if [[ "${SKIP_OS_PACKAGES}" == "1" || "${SKIP_OS_PACKAGES,,}" == "true" || "${SKIP_OS_PACKAGES,,}" == "yes" ]]; then
  section "Skipping operating system packages"
  echo "Skipping OS package installation because --skip-os-packages or SKIP_OS_PACKAGES was set."
else
  section "Installing operating system packages"
  install_base_packages
fi

section "Creating service user and directories"
id -u "${SERVICE_USER}" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
mkdir -p "${INSTALL_DIR}" "${STATE_DIR}" "${LOG_DIR}"
mkdir -p "${STATE_DIR}/upgrade"

section "Staging application bundle"
copy_bundle_contents "${SOURCE_DIR}" "${INSTALL_DIR}"
chmod +x \
  "${INSTALL_DIR}/collect_environment.py" \
  "${INSTALL_DIR}/install.sh" \
  "${INSTALL_DIR}/install_oracledash.sh" \
  "${INSTALL_DIR}/scheduler_jobs.sh" \
  "${INSTALL_DIR}/upgrade_watcher.py" \
  "${INSTALL_DIR}/upgrade.sh"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" "${STATE_DIR}" "${LOG_DIR}"
chmod 775 "${STATE_DIR}" "${STATE_DIR}/upgrade" "${LOG_DIR}" || true
find "${STATE_DIR}/upgrade" -maxdepth 1 -type f -exec chmod 664 {} \; 2>/dev/null || true

section "Creating Python virtual environment"
python3 -m venv "${INSTALL_DIR}/venv"
install_python_requirements "${INSTALL_DIR}/requirements.txt"

section "Writing environment file"
cat > "${CONFIG_FILE}" <<EOF
IAM_MONITORING_HOST=0.0.0.0
IAM_MONITORING_PORT=${DASHBOARD_PORT}
IAM_MONITORING_CACHE_SECONDS=60
IAM_MONITORING_DB_PATH=${STATE_DIR}/iam-monitoring.sqlite
IAM_MONITORING_LOG_DIR=${LOG_DIR}
IAM_MONITORING_SERVICE_USER=${SERVICE_USER}
IAM_MONITORING_DEFAULT_COLLECTION_MINUTES=${COLLECTOR_MINUTES}
IAM_MONITORING_SCHEDULER_MINUTES=5
# Optional outbound proxy for GitHub update checks
# IAM_MONITORING_HTTP_PROXY=http://proxy.example.com:80
# IAM_MONITORING_HTTPS_PROXY=http://proxy.example.com:80
# IAM_MONITORING_NO_PROXY=127.0.0.1,localhost
EOF
chmod 640 "${CONFIG_FILE}"
chown root:"${SERVICE_USER}" "${CONFIG_FILE}"

section "Installing systemd service"
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

section "Installing collector scheduler"
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
chown "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}/scheduler.log"

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

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
systemctl is-active "${SERVICE_NAME}"
systemctl enable "${UPGRADE_SERVICE_NAME}"
systemctl restart "${UPGRADE_SERVICE_NAME}"
systemctl is-active "${UPGRADE_SERVICE_NAME}"
systemctl enable --now "${CRON_SERVICE_NAME}"

section "Install complete"
echo "${PRODUCT_NAME} install complete."
echo
echo "============================================================"
echo " DASHBOARD URL"
echo " http://<server-ip>:${DASHBOARD_PORT}/"
echo "============================================================"
echo
echo "Health check: http://<server-ip>:${DASHBOARD_PORT}/healthz"
echo "After install, use Administration to add one or more IAM environments."
echo "Use Save And Bootstrap when adding an environment so the dashboard can switch"
echo "from the initial SSH login to its installed runtime key for ongoing collection."
echo
echo "Installed service: ${SERVICE_NAME}"
echo "Installed upgrade helper: ${UPGRADE_SERVICE_NAME}"
echo "Cron service: ${CRON_SERVICE_NAME}"
echo "Scheduler wake interval: every 5 minutes"
echo "Default per-environment collector interval: ${COLLECTOR_MINUTES} minutes"
echo "Useful checks:"
echo "  sudo systemctl status ${SERVICE_NAME} --no-pager"
echo "  sudo systemctl status ${UPGRADE_SERVICE_NAME} --no-pager"
echo "  curl -I http://127.0.0.1:${DASHBOARD_PORT}/healthz"
echo "  curl http://127.0.0.1:${DASHBOARD_PORT}/healthz"
echo "  sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
echo "  sudo tail -F ${LOG_DIR}/scheduler.log"
echo
echo "GitHub update proxy:"
echo "  Preferred: open Administration -> Help -> GitHub Update Proxy in the dashboard."
echo "  Alternative: add these to ${CONFIG_FILE} and restart the service:"
echo "  IAM_MONITORING_HTTP_PROXY=http://proxy.example.com:80"
echo "  IAM_MONITORING_HTTPS_PROXY=http://proxy.example.com:80"
echo "  IAM_MONITORING_NO_PROXY=127.0.0.1,localhost"
