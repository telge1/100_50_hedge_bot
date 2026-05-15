#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOT_GROUP_DIR="$(cd "${BOT_DIR}/.." && pwd)"
exec "${BOT_GROUP_DIR}/shared_scripts/clean_bot_logs.sh" "long_bot_4"
