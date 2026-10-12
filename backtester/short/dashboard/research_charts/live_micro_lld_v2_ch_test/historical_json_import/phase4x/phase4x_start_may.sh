#!/usr/bin/env bash
# Phase 4X: production May 2026 monthly import (write_and_readback) via may2026_monthly_runner.py
set -euo pipefail

REPO_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"
DASH_ROOT="${REPO_ROOT}/backtester/short/dashboard"
LOG_ROOT="${REPO_ROOT}/logs/historical_import/may2026"
PLAN="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/historical_json_import/manifests/phase4w_may2026_monthly_run_plan_v1.json"
RUNNER="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/historical_json_import/may2026_monthly_runner.py"
CATALOG="${DASH_ROOT}/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
PROGRESS="${CATALOG}/_controlled_import_progress_phase4x"
STATE_FILE="${LOG_ROOT}/phase4x_monthly_runner_state.json"
EXIT_FILE="${LOG_ROOT}/phase4x_monthly_runner.exit"
META_FILE="${LOG_ROOT}/phase4x_start_meta.json"
BACKUP_ID="ebfdfb7d-ba12-4c2a-af6b-ff9c5ca57b02"

export PYTHONPATH="${DASH_ROOT}:${REPO_ROOT}/backtester/short"
export HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1
export HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1
export HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID="${BACKUP_ID}"
export HISTORICAL_IMPORT_MIN_FREE_BYTES=53687091200
export HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS=0
export HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS=120
export HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS=60

mkdir -p "${LOG_ROOT}" "${PROGRESS}"

for var in HISTORICAL_JSON_IMPORT_ALLOW_WRITE HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE \
  HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID HISTORICAL_IMPORT_MIN_FREE_BYTES \
  HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS \
  HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS; do
  if [[ -z "${!var:-}" ]]; then
    echo "missing env ${var}" >&2
    exit 10
  fi
done

if [[ -f "${LOG_ROOT}/monthly_runner.pid" ]]; then
  old=$(cat "${LOG_ROOT}/monthly_runner.pid")
  if [[ -n "${old}" && -d "/proc/${old}" ]]; then
    echo "monthly_runner.pid still active: ${old}" >&2
    exit 11
  fi
fi

rm -f "${EXIT_FILE}"
STARTED_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)

cd "${DASH_ROOT}"
CMD=(python3 -u "${RUNNER}" --mode write_and_readback --plan "${PLAN}" --log-root "${LOG_ROOT}"
  --run-id phase4x_may2026 --state-file "${STATE_FILE}" --progress-dir "${PROGRESS}")

python3 - "${META_FILE}" "${STARTED_UTC}" "${BACKUP_ID}" "${PLAN}" <<'PY'
import json, sys
from pathlib import Path
meta_path, started, backup_id, plan = sys.argv[1:5]
manifests = [
    "phase4w_may2026_15m_pd1_full_v1.json",
    "phase4w_may2026_15m_pd5_full_v1.json",
    "phase4w_may2026_15m_pd10_full_v1.json",
    "phase4w_may2026_4h_pd10_full_v1.json",
]
base = Path(plan).parent
rows = []
for name in manifests:
    data = json.loads((base / name).read_text(encoding="utf-8"))
    if data["backup_recovery_id"] != backup_id:
        raise SystemExit(f"backup mismatch in {name}")
    rows.append({
        "file": name,
        "run_id": data["run_id"],
        "scope_fingerprint": data["scope_fingerprint"],
        "backup_recovery_id": data["backup_recovery_id"],
        "max_snapshots": data["max_snapshots"],
        "max_batches": data["max_batches"],
    })
Path(meta_path).write_text(json.dumps({
    "phase": "4X",
    "started_at_utc": started,
    "backup_recovery_id": backup_id,
    "monthly_plan": plan,
    "manifests": rows,
}, indent=2), encoding="utf-8")
PY

"${CMD[@]}"
ec=$?
echo "${ec}" > "${EXIT_FILE}"
exit "${ec}"
