#!/usr/bin/env bash
# Phase 4Y prep only — production June import (NOT executed in Phase 4Y).
# Use after explicit write approval, mirroring phase4x_start_may.sh.
set -euo pipefail

REPO_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"
DASH_ROOT="${REPO_ROOT}/backtester/short/dashboard"
LOG_ROOT="${REPO_ROOT}/logs/historical_import/june2026"
PLAN="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4y_june2026_monthly_run_plan_v1.json"
RUNNER="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/historical_json_import/june2026_monthly_runner.py"
CATALOG="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
PROGRESS="${CATALOG}/_controlled_import_progress_phase4y"
STATE_FILE="${LOG_ROOT}/phase4x_monthly_runner_state.json"
EXIT_FILE="${LOG_ROOT}/phase4x_monthly_runner.exit"
BACKUP_ID="${HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID:?set verified backup id}"

export PYTHONPATH="${DASH_ROOT}:${REPO_ROOT}/backtester/short"
export HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1
export HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1
export HISTORICAL_IMPORT_MIN_FREE_BYTES=53687091200
export HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS=0
export HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS=120
export HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS=60

mkdir -p "${LOG_ROOT}" "${PROGRESS}"
rm -f "${EXIT_FILE}"
cd "${DASH_ROOT}"
python3 -u "${RUNNER}" --mode write_and_readback --plan "${PLAN}" --log-root "${LOG_ROOT}" \
  --run-id phase4x_june2026 --state-file "${STATE_FILE}" --progress-dir "${PROGRESS}"
ec=$?
echo "${ec}" > "${EXIT_FILE}"
exit "${ec}"
