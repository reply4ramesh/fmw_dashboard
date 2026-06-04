#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${IAM_MONITORING_CONFIG:-/etc/iam-monitoring.env}"

if [[ -f "${CONFIG_FILE}" ]]; then
  set -a
  . "${CONFIG_FILE}"
  set +a
fi

DB_PATH="${IAM_MONITORING_DB_PATH:-${SCRIPT_DIR}/state/iam-monitoring.sqlite}"

exec "${SCRIPT_DIR}/venv/bin/python" "${SCRIPT_DIR}/collect_environment.py" --db-path "${DB_PATH}" --scheduler
