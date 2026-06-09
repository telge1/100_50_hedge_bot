#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LONG_SCRIPT="${PROJECT_ROOT}/live_bots/100_50_hedge_bot/shared_scripts/stop_hedge_guard_watchers.sh"
SHORT_SCRIPT="${PROJECT_ROOT}/live_bots/short_hedge_bot/shared_scripts/stop_wallet_refill_watchdog.sh"
SHORT_PROFILE="${SHORT_PROFILE:-short_bot_1}"

errors=0

if [[ -x "${LONG_SCRIPT}" ]]; then
  if ! "${LONG_SCRIPT}"; then
    echo "[stop_all] long script failed" >&2
    errors=1
  fi
else
  echo "[stop_all] missing: ${LONG_SCRIPT}" >&2
  errors=1
fi

if [[ -x "${SHORT_SCRIPT}" ]]; then
  echo "[INFO] Stopping Short wallet refill watchdog for ${SHORT_PROFILE}..."
  if ! bash "${SHORT_SCRIPT}" "${SHORT_PROFILE}"; then
    echo "[stop_all] short script failed" >&2
    errors=1
  fi
else
  echo "[stop_all] missing: ${SHORT_SCRIPT}" >&2
  errors=1
fi

if [[ ${errors} -ne 0 ]]; then
  exit 1
fi
