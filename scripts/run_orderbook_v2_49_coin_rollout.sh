#!/usr/bin/env bash
# Sequential, lock-protected, resumable Orderbook V2 import for 49 remaining coins.
# Does not enable OPTIMIZE FINAL. Never issues destructive SQL.
# ADAUSDT and BTCUSDT are excluded by construction.

set -u
set -o pipefail

PROJECT_ROOT="/home/telgenbuescher/projects/orderbook_analyse"
EXPECTED_HEAD="21e001c89811d04e8bf28871ed53ccaa62cfafd3"
COLLECTOR_PID="147111"
DATA_ROOT_BASE="/home/telgenbuescher/projects/data/orderbook_raw_v2/bybit/linear/ob200"
RESULT_DIR="${PROJECT_ROOT}/results/orderbook_v2_manual_rollout"
PROGRESS_FILE="${RESULT_DIR}/progress.json"
LOCK_FILE="${RESULT_DIR}/rollout.lock"
LIB="${PROJECT_ROOT}/scripts/orderbook_v2_49_rollout_lib.py"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"

cd "${PROJECT_ROOT}" || exit 1
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/.venv/bin/activate"
export PYTHONPATH=src
EXPECTED_PARSER="$("${PYTHON}" -c 'from orderbook_analyse.orderbook_v2 import PARSER_VERSION; print(PARSER_VERSION)')"

mkdir -p "${RESULT_DIR}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "STOP: another rollout holds ${LOCK_FILE}"
  exit 1
fi

log() {
  printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

write_progress() {
  local status="$1"
  local current="${2:-}"
  local failed="${3:-}"
  "${PYTHON}" - "${PROGRESS_FILE}" "${status}" "${current}" "${failed}" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path("/home/telgenbuescher/projects/orderbook_analyse/scripts")))
from orderbook_v2_49_rollout_lib import SYMBOLS_49, EXPECTED_HEAD, EXPECTED_PARSER, write_progress_atomic

path, status, current, failed = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
prev = {}
p = Path(path)
if p.is_file():
    prev = json.loads(p.read_text(encoding="utf-8"))
completed = list(prev.get("completed_symbols") or [])
skipped = list(prev.get("skipped_complete_symbols") or [])
done = set(completed) | set(skipped)
remaining = [s for s in SYMBOLS_49 if s not in done]
payload = {
    "run_started_at": prev.get("run_started_at") or datetime.now(timezone.utc).isoformat(),
    "current_symbol": current or None,
    "completed_symbols": completed,
    "skipped_complete_symbols": skipped,
    "failed_symbol": failed or None,
    "remaining_symbols": remaining,
    "last_completed_at": prev.get("last_completed_at"),
    "rollout_status": status,
    "expected_window": ["2026-08-11", "2026-08-17"],
    "parser_version": EXPECTED_PARSER,
    "HEAD": EXPECTED_HEAD,
}
if status == "RUNNING" and current:
    payload["remaining_symbols"] = [s for s in remaining if s != current]
write_progress_atomic(p, payload)
PY
}

mark_completed() {
  local kind="$1"
  local symbol="$2"
  "${PYTHON}" - "${PROGRESS_FILE}" "${kind}" "${symbol}" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path("/home/telgenbuescher/projects/orderbook_analyse/scripts")))
from orderbook_v2_49_rollout_lib import SYMBOLS_49, EXPECTED_HEAD, EXPECTED_PARSER, write_progress_atomic

path, kind, symbol = sys.argv[1], sys.argv[2], sys.argv[3]
prev = json.loads(Path(path).read_text(encoding="utf-8"))
completed = list(prev.get("completed_symbols") or [])
skipped = list(prev.get("skipped_complete_symbols") or [])
if kind == "completed" and symbol not in completed:
    completed.append(symbol)
if kind == "skipped" and symbol not in skipped:
    skipped.append(symbol)
done = set(completed) | set(skipped)
remaining = [s for s in SYMBOLS_49 if s not in done]
payload = {
    "run_started_at": prev.get("run_started_at"),
    "current_symbol": None,
    "completed_symbols": completed,
    "skipped_complete_symbols": skipped,
    "failed_symbol": None,
    "remaining_symbols": remaining,
    "last_completed_at": datetime.now(timezone.utc).isoformat(),
    "rollout_status": "RUNNING" if remaining else "COMPLETED",
    "expected_window": ["2026-08-11", "2026-08-17"],
    "parser_version": EXPECTED_PARSER,
    "HEAD": EXPECTED_HEAD,
}
write_progress_atomic(Path(path), payload)
print(payload["rollout_status"])
PY
}

stop_with() {
  local status="$1"
  local msg="$2"
  local failed="${3:-}"
  log "${msg}"
  write_progress "${status}" "" "${failed}"
  exit 1
}

HEAD_NOW="$(git rev-parse HEAD)"
if [ "${HEAD_NOW}" != "${EXPECTED_HEAD}" ]; then
  stop_with "STOPPED_INCONSISTENT" "STOP: HEAD ${HEAD_NOW} != ${EXPECTED_HEAD}"
fi

PARSER_NOW="$("${PYTHON}" -c 'from orderbook_analyse.orderbook_v2 import PARSER_VERSION; print(PARSER_VERSION)')"
if [ "${PARSER_NOW}" != "${EXPECTED_PARSER}" ]; then
  stop_with "STOPPED_INCONSISTENT" "STOP: parser ${PARSER_NOW} != ${EXPECTED_PARSER}"
fi

mapfile -t SYMBOLS < <("${PYTHON}" "${LIB}" list-symbols)
if [ "${#SYMBOLS[@]}" -ne 49 ]; then
  stop_with "STOPPED_INCONSISTENT" "STOP: symbol count ${#SYMBOLS[@]} != 49"
fi

if printf '%s\n' "${SYMBOLS[@]}" | grep -Exq 'ADAUSDT|BTCUSDT|XAUUSDT'; then
  stop_with "STOPPED_INCONSISTENT" "STOP: forbidden symbol in list"
fi

if ! "${PYTHON}" "${LIB}" check-window; then
  stop_with "STOPPED_WINDOW_CHANGED" "STOP: UTC window is not 2026-08-11..2026-08-17"
fi

if ! "${PYTHON}" "${LIB}" check-env; then
  stop_with "STOPPED_INCONSISTENT" "STOP: ClickHouse env incomplete (names only, no values)"
fi

if ! "${PYTHON}" "${LIB}" check-clickhouse; then
  stop_with "STOPPED_INCONSISTENT" "STOP: ClickHouse SELECT failed"
fi

if ! "${PYTHON}" "${LIB}" check-collector; then
  stop_with "STOPPED_COLLECTOR_GUARD" "STOP: collector PID ${COLLECTOR_PID} not healthy"
fi

if pgrep -af '[o]rderbook_analyse.orderbook_v2.pilot' >"${RESULT_DIR}/pilot_pgrep.txt" 2>/dev/null; then
  if [ -s "${RESULT_DIR}/pilot_pgrep.txt" ]; then
    stop_with "STOPPED_INCONSISTENT" "STOP: another orderbook_v2.pilot is running"
  fi
fi

ps -p "${COLLECTOR_PID}" -o pid,pcpu,pmem,etime,cmd | tee -a "${RESULT_DIR}/resource_samples.log" >/dev/null || true

write_progress "RUNNING" "" ""
log "rollout start n=${#SYMBOLS[@]}"

for OB_SYMBOL in "${SYMBOLS[@]}"; do
  log "window recheck before ${OB_SYMBOL}"
  if ! "${PYTHON}" "${LIB}" check-window; then
    stop_with "STOPPED_WINDOW_CHANGED" "STOP: window changed before ${OB_SYMBOL}" "${OB_SYMBOL}"
  fi
  if ! "${PYTHON}" "${LIB}" check-collector; then
    stop_with "STOPPED_COLLECTOR_GUARD" "STOP: collector guard before ${OB_SYMBOL}" "${OB_SYMBOL}"
  fi
  ps -p "${COLLECTOR_PID}" -o pid,pcpu,pmem,etime,cmd | tee -a "${RESULT_DIR}/resource_samples.log" >/dev/null || true

  classify_out="$("${PYTHON}" "${LIB}" classify "${OB_SYMBOL}")"
  classify_rc=$?
  classify_class="${classify_out%% *}"
  log "classify ${OB_SYMBOL} ${classify_out} rc=${classify_rc}"

  if [ "${classify_class}" = "COMPLETE_V3" ]; then
    log "SKIP_COMPLETE ${OB_SYMBOL}"
    mark_completed skipped "${OB_SYMBOL}" >/dev/null
    continue
  fi
  if [ "${classify_class}" != "NOT_IMPORTED" ]; then
    stop_with "STOPPED_INCONSISTENT" "STOP_INCONSISTENT ${OB_SYMBOL} ${classify_out}" "${OB_SYMBOL}"
  fi

  write_progress "RUNNING" "${OB_SYMBOL}" ""
  coin_log="${RESULT_DIR}/${OB_SYMBOL}_20260811_20260817.log"
  log "IMPORT ${OB_SYMBOL} log=${coin_log}"
  set +e
  "${PYTHON}" -m orderbook_analyse.orderbook_v2.pilot \
    --symbol "${OB_SYMBOL}" \
    --days 7 \
    --data-root "${DATA_ROOT_BASE}/${OB_SYMBOL}" \
    >"${coin_log}" 2>&1
  import_rc=$?

  if [ "${import_rc}" -ne 0 ]; then
    stop_with "STOPPED_IMPORT_FAILED" "STOP: import exit ${import_rc} ${OB_SYMBOL}" "${OB_SYMBOL}"
  fi
  if [ ! -s "${coin_log}" ]; then
    stop_with "STOPPED_IMPORT_FAILED" "STOP: empty log ${OB_SYMBOL}" "${OB_SYMBOL}"
  fi
  if ! grep -q "^=== ORDERBOOK_V2 PILOT: ${OB_SYMBOL} 7d ===" "${coin_log}"; then
    stop_with "STOPPED_IMPORT_FAILED" "STOP: missing symbol header ${OB_SYMBOL}" "${OB_SYMBOL}"
  fi
  if grep -q "DECISION: ${OB_SYMBOL}_OB_V2_7D_PILOT_PASSED_WITH_WARNINGS" "${coin_log}"; then
    stop_with "STOPPED_IMPORT_FAILED" "STOP: PASSED_WITH_WARNINGS ${OB_SYMBOL}" "${OB_SYMBOL}"
  fi
  if ! grep -qx "DECISION: ${OB_SYMBOL}_OB_V2_7D_PILOT_PASSED" "${coin_log}"; then
    stop_with "STOPPED_IMPORT_FAILED" "STOP: missing exact PASSED decision ${OB_SYMBOL}" "${OB_SYMBOL}"
  fi

  audit_out="$("${PYTHON}" "${LIB}" audit "${OB_SYMBOL}")"
  audit_rc=$?
  log "${audit_out}"
  if [ "${audit_rc}" -ne 0 ]; then
    log "AUDIT_FAIL ${OB_SYMBOL}"
    stop_with "STOPPED_AUDIT_FAILED" "STOP: audit failed ${OB_SYMBOL} ${audit_out}" "${OB_SYMBOL}"
  fi

  if ! "${PYTHON}" "${LIB}" check-collector; then
    stop_with "STOPPED_COLLECTOR_GUARD" "STOP: collector guard after ${OB_SYMBOL}" "${OB_SYMBOL}"
  fi

  mark_completed completed "${OB_SYMBOL}" >/dev/null
  log "DONE ${OB_SYMBOL}"
done

write_progress "COMPLETED" "" ""
log "rollout COMPLETED"
exit 0
