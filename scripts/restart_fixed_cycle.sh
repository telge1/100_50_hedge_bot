#!/bin/bash

set -u

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"

echo "Running hard reset..."
cd "${PROJECT_ROOT}" || exit 1

BOT_CONTROL_PYTHON="${PYTHON}" ./bot_control.sh hard-reset
RESET_CODE=$?

echo "hard-reset exit code: ${RESET_CODE}"

if [[ "${RESET_CODE}" -ne 0 ]]; then
  echo "[ERROR] hard-reset failed (exit_code=${RESET_CODE}); aborting restart." >&2
  exit 1
fi

echo "Cleaning log files..."
rm -vf \
  "${PROJECT_ROOT}/logs/fixed_cycle_hedge_runtime.log" \
  "${PROJECT_ROOT}/logs/generic_hedge_runtime_audit.jsonl" \
  "${PROJECT_ROOT}/logs/fixed_cycle_calc_audit.log" \
  "${PROJECT_ROOT}/logs/fixed_cycle_runner.stdout.log"

echo "After cleanup:"
ls -lah "${PROJECT_ROOT}/logs" | grep -E "fixed_cycle|generic_hedge" || true

echo "Starting fixed cycle bot..."
PYTHON="${PYTHON}" "${PROJECT_ROOT}/start_fixed_cycle.sh"