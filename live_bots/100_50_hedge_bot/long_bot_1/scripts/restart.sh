#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${BOT_DIR}/../../.." && pwd)"
cd "${PROJECT_ROOT}" || exit 1

STOP_SCRIPT="${BOT_DIR}/scripts/stop.sh"
START_SCRIPT="${BOT_DIR}/scripts/start.sh"

if [[ ! -x "${STOP_SCRIPT}" ]]; then
  echo "Stop script missing or not executable: ${STOP_SCRIPT}" >&2
  exit 1
fi
if [[ ! -x "${START_SCRIPT}" ]]; then
  echo "Start script missing or not executable: ${START_SCRIPT}" >&2
  exit 1
fi

echo "[long_bot_1] restarting..."
"${STOP_SCRIPT}"
echo "[long_bot_1] stopped"
sleep 1
"${START_SCRIPT}"
echo "[long_bot_1] started"
