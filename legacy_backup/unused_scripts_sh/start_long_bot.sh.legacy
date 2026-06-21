#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
exec "${PROJECT_ROOT}/live_bots/100_50_hedge_bot/shared_scripts/start_long_bot.sh" "$@"
