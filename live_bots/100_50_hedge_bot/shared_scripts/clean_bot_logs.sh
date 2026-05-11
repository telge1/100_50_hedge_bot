#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ${BASH_SOURCE[0]} long_bot_<number>" >&2
  exit 1
}

if [[ $# -ne 1 ]]; then
  usage
fi

BOT_NAME="$1"
if [[ ! "${BOT_NAME}" =~ ^long_bot_[0-9]+$ ]]; then
  usage
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_GROUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOT_DIR="${BOT_GROUP_DIR}/${BOT_NAME}"

if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: bot directory not found: ${BOT_DIR}" >&2
  exit 1
fi

LOG_DIR="${BOT_DIR}/logs"

rm -vf \
  "${LOG_DIR}/fixed_cycle_hedge_runtime.log" \
  "${LOG_DIR}/generic_hedge_runtime_audit.jsonl" \
  "${LOG_DIR}/fixed_cycle_calc_audit.log" \
  "${LOG_DIR}/fixed_cycle_runner.stdout.log"
echo "[${BOT_NAME}] logs cleaned"
