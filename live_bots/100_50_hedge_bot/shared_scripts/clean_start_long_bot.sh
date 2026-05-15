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
PROJECT_ROOT="$(cd "${BOT_GROUP_DIR}/../.." && pwd)"
BOT_DIR="${BOT_GROUP_DIR}/${BOT_NAME}"

if [[ ! -d "${BOT_DIR}" ]]; then
  echo "ERROR: bot directory not found: ${BOT_DIR}" >&2
  exit 1
fi

"${SCRIPT_DIR}/stop_with_cleanup.sh" "${BOT_NAME}"

STATE_DIR="${BOT_DIR}/state"
if [[ -d "${STATE_DIR}" ]]; then
  rm -f "${STATE_DIR}"/*.json
fi

RUN_SCRIPT="${SCRIPT_DIR}/start_long_bot.sh"

"${RUN_SCRIPT}" "${BOT_NAME}"
