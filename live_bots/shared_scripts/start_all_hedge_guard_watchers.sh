#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LONG_SCRIPT="${PROJECT_ROOT}/live_bots/100_50_hedge_bot/shared_scripts/start_hedge_guard_watchers.sh"
SHORT_SAFETY_SCRIPT="${PROJECT_ROOT}/live_bots/short_hedge_bot/shared_scripts/start_safety_order_watchdog.sh"
SHORT_SCRIPT="${PROJECT_ROOT}/live_bots/short_hedge_bot/shared_scripts/start_wallet_refill_watchdog.sh"
# Einheitliche Credential-/Transfer-Config: immer die 100_50_hedge_bot-Config.
LONG_TRANSFER_CONFIG="${PROJECT_ROOT}/live_bots/100_50_hedge_bot/config/config.yaml"
SHORT_TRANSFER_CONFIG="${PROJECT_ROOT}/live_bots/100_50_hedge_bot/config/config.yaml"
SHORT_PROFILE="${SHORT_PROFILE:-short_bot_1}"

if [[ ! -x "${LONG_SCRIPT}" ]]; then
  echo "[start_all_hedge_guard_watchers] missing: ${LONG_SCRIPT}" >&2
  exit 1
fi
if [[ ! -x "${SHORT_SAFETY_SCRIPT}" ]]; then
  echo "[start_all_hedge_guard_watchers] missing: ${SHORT_SAFETY_SCRIPT}" >&2
  exit 1
fi
if [[ ! -x "${SHORT_SCRIPT}" ]]; then
  echo "[start_all_hedge_guard_watchers] missing: ${SHORT_SCRIPT}" >&2
  exit 1
fi
if [[ ! -f "${LONG_TRANSFER_CONFIG}" ]]; then
  echo "[ERROR] Long transfer config missing: ${LONG_TRANSFER_CONFIG}" >&2
  exit 1
fi
if [[ ! -f "${SHORT_TRANSFER_CONFIG}" ]]; then
  echo "[ERROR] Short transfer config missing: ${SHORT_TRANSFER_CONFIG}" >&2
  exit 1
fi

echo "Starting Long hedge guard viewers..."
"${LONG_SCRIPT}"

echo "[INFO] Starting Short safety watchdog for ${SHORT_PROFILE}..."
bash "${SHORT_SAFETY_SCRIPT}" "${SHORT_PROFILE}"

echo "[INFO] Starting Short wallet refill watchdog for ${SHORT_PROFILE}..."
bash "${SHORT_SCRIPT}" "${SHORT_PROFILE}"
