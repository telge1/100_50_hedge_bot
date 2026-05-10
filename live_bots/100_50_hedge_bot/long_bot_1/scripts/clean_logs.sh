#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_DIR}/../../.." && pwd)"
cd "${PROJECT_ROOT}" || exit 1

LOG_DIR="${BOT_DIR}/logs"

rm -vf \
  "${LOG_DIR}/fixed_cycle_hedge_runtime.log" \
  "${LOG_DIR}/generic_hedge_runtime_audit.jsonl" \
  "${LOG_DIR}/fixed_cycle_calc_audit.log" \
  "${LOG_DIR}/fixed_cycle_runner.stdout.log"
echo "[long_bot_1] logs cleaned"
exit 0
