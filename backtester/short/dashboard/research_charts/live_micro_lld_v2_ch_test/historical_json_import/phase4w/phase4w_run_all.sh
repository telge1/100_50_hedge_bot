#!/usr/bin/env bash
# Phase 4W orchestrator: backup/restore then May read-only preparation.
set -euo pipefail
RUN_ID="${PHASE4W_RUN_ID:-phase4w_backup_restore}"
REPO_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"
LOG_DIR="${REPO_ROOT}/logs/phase4w"
SCRIPT_DIR="${REPO_ROOT}/backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase4w"
STATUS_FILE="${LOG_DIR}/${RUN_ID}_all.exit"
MASTER_LOG="${LOG_DIR}/${RUN_ID}_all.log"

mkdir -p "${LOG_DIR}"
: >"${STATUS_FILE}"
exec > >(tee -a "${MASTER_LOG}") 2>&1
trap 'ec=$?; echo "${ec}" >"${STATUS_FILE}"; exit "${ec}"' EXIT

echo "PHASE4W_ALL_START $(date -u +%Y-%m-%dT%H:%M:%SZ)"
export PHASE4W_RUN_ID="${RUN_ID}"
bash "${SCRIPT_DIR}/phase4w_backup_restore_runner.sh"
DASH_ROOT="${REPO_ROOT}/backtester/short/dashboard"
export PYTHONPATH="${DASH_ROOT}:${REPO_ROOT}/backtester/short:${PYTHONPATH:-}"
cd "${DASH_ROOT}"
python3 "${SCRIPT_DIR}/phase4w_prepare_may.py"
echo "PHASE4W_ALL_OK $(date -u +%Y-%m-%dT%H:%M:%SZ)"
