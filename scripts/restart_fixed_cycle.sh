#!/bin/bash

set -u

PROJECT_ROOT="/home/telgenbuescher/projects/spread_recovery_hedge"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"

echo "Running hard reset..."
cd "${PROJECT_ROOT}" || exit 1

./bot_control.sh hard-reset
RESET_CODE=$?

echo "hard-reset exit code: ${RESET_CODE}"

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