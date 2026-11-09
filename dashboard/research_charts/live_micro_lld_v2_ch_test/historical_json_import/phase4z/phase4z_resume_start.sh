#!/usr/bin/env bash
# Phase 4Z resume: August PD10 batch02+ and September (existing plan/progress).
set -euo pipefail

REPO_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"
DASH_ROOT="${REPO_ROOT}/backtester/short/dashboard"
LOG_ROOT="${REPO_ROOT}/logs/historical_import/overnight_2026"
RECONCILE="${LOG_ROOT}/phase4z_resume_reconcile.json"
PLAN="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/historical_json_import/manifests/june_sep_2026_overnight_import_plan_v1.json"
RUNNER="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/historical_json_import/overnight_june_sep_runner.py"
CATALOG="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
PROGRESS="${CATALOG}/_controlled_import_progress_phase4z_overnight"
STATE_FILE="${LOG_ROOT}/overnight_state.json"
EXIT_FILE="${LOG_ROOT}/overnight.exit"
PREFLIGHT="${LOG_ROOT}/phase4z_preflight_all_months.json"

if [[ ! -f "${RECONCILE}" ]]; then
  echo "missing reconcile: ${RECONCILE}" >&2
  exit 2
fi
python3 -c "import json,sys; d=json.load(open('${RECONCILE}')); sys.exit(0 if d.get('ok') else 1)"

BACKUP_ID="$(python3 -c "import json; print(json.load(open('${PREFLIGHT}'))['inventory']['backup_recovery_id'])")"

export PYTHONPATH="${DASH_ROOT}:${REPO_ROOT}/backtester/short"
export HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1
export HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1
export HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID="${BACKUP_ID}"
export HISTORICAL_IMPORT_MIN_FREE_BYTES=53687091200
export HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS=0
export HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS=120
export HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS=60

mkdir -p "${LOG_ROOT}"

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

if [[ -f "${STATE_FILE}" ]] && ! grep -q '"next_month_index"' "${STATE_FILE}" 2>/dev/null; then
  cp -a "${STATE_FILE}" "${LOG_ROOT}/overnight_state_failed_run.json"
fi

python3 - <<PY
import json
from pathlib import Path
reconcile = json.loads(Path("${RECONCILE}").read_text())
resume = reconcile["overnight_resume_state"]
Path("${STATE_FILE}").write_text(json.dumps(resume, indent=2) + "\n", encoding="utf-8")
PY

rm -f "${EXIT_FILE}"
cd "${DASH_ROOT}"
python3 -u "${RUNNER}" --mode write_and_readback --plan "${PLAN}" --log-root "${LOG_ROOT}" \
  --state-file "${STATE_FILE}" --progress-root "${PROGRESS}"
ec=$?
echo "${ec}" > "${EXIT_FILE}"
exit "${ec}"
