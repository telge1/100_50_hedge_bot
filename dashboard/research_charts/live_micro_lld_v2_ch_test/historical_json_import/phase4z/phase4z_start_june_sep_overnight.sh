#!/usr/bin/env bash
# Phase 4Z: production Jun–Sep 2026 overnight import (write_and_readback).
set -euo pipefail

REPO_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"
DASH_ROOT="${REPO_ROOT}/backtester/short/dashboard"
LOG_ROOT="${REPO_ROOT}/logs/historical_import/overnight_2026"
PLAN="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/historical_json_import/manifests/june_sep_2026_overnight_import_plan_v1.json"
RUNNER="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/historical_json_import/overnight_june_sep_runner.py"
CATALOG="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
PROGRESS="${CATALOG}/_controlled_import_progress_phase4z_overnight"
STATE_FILE="${LOG_ROOT}/overnight_state.json"
EXIT_FILE="${LOG_ROOT}/overnight.exit"
PREFLIGHT="${LOG_ROOT}/phase4z_preflight_all_months.json"

if [[ ! -f "${PREFLIGHT}" ]]; then
  echo "missing preflight: ${PREFLIGHT}" >&2
  exit 2
fi

BACKUP_ID="$(python3 -c "import json; print(json.load(open('${PREFLIGHT}'))['inventory']['backup_recovery_id'])")"

export PYTHONPATH="${DASH_ROOT}:${REPO_ROOT}/backtester/short"
export HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1
export HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1
export HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID="${BACKUP_ID}"
export HISTORICAL_IMPORT_MIN_FREE_BYTES=53687091200
export HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS=0
export HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS=120
export HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS=60

mkdir -p "${LOG_ROOT}" "${PROGRESS}/june" "${PROGRESS}/july" "${PROGRESS}/august" "${PROGRESS}/september"

for var in HISTORICAL_JSON_IMPORT_ALLOW_WRITE HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE \
  HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID HISTORICAL_IMPORT_MIN_FREE_BYTES \
  HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS \
  HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS; do
  if [[ -z "${!var:-}" ]]; then
    echo "missing env ${var}" >&2
    exit 10
  fi
done

if [[ -f "${LOG_ROOT}/overnight.pid" ]]; then
  old=$(cat "${LOG_ROOT}/overnight.pid")
  if [[ -n "${old}" && -d "/proc/${old}" ]]; then
    echo "overnight.pid still active: ${old}" >&2
    exit 11
  fi
fi

LOCK_FILE="/tmp/historical_lmv2_json_import.flock"
if command -v flock >/dev/null 2>&1 && [[ -f "${LOCK_FILE}" ]]; then
  if ! flock -n "${LOCK_FILE}" true 2>/dev/null; then
    echo "writer lock busy: ${LOCK_FILE}" >&2
    exit 12
  fi
fi

rm -f "${EXIT_FILE}"
STARTED_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)

cd "${DASH_ROOT}"
python3 -u "${RUNNER}" --mode write_and_readback --plan "${PLAN}" --log-root "${LOG_ROOT}" \
  --state-file "${STATE_FILE}" --progress-root "${PROGRESS}"
ec=$?
echo "${ec}" > "${EXIT_FILE}"
exit "${ec}"
