#!/usr/bin/env bash
set -euo pipefail
RUN_ID="${PHASE4Y_RUN_ID:-phase4y_backup_restore}"
REPO_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"
LOG_DIR="${REPO_ROOT}/logs/phase4y"
SCRIPT_DIR="${REPO_ROOT}/backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase4y"
STATUS_FILE="${LOG_DIR}/${RUN_ID}_all.exit"
exec > >(tee -a "${LOG_DIR}/${RUN_ID}_all.log") 2>&1
trap 'ec=$?; echo "${ec}" >"${STATUS_FILE}"; exit "${ec}"' EXIT

echo "PHASE4Y_ALL_START $(date -u +%Y-%m-%dT%H:%M:%SZ)"
export PHASE4Y_RUN_ID="${RUN_ID}"
bash "${SCRIPT_DIR}/phase4y_backup_restore_runner.sh"
DASH_ROOT="${REPO_ROOT}/backtester/short/dashboard"
export PYTHONPATH="${DASH_ROOT}:${REPO_ROOT}/backtester/short"
cd "${DASH_ROOT}"
python3 "${SCRIPT_DIR}/phase4y_prepare_june.py"
echo "PHASE4Y_ALL_OK $(date -u +%Y-%m-%dT%H:%M:%SZ)"
