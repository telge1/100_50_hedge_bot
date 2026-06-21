#!/usr/bin/env bash
set -euo pipefail

# Determine the repo root (one level above this folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Ensure strategy package and helpers are importable
export PYTHONPATH="${REPO_ROOT}"

exec python "${REPO_ROOT}/emergency_100/final_hedge_strategy.py" "$@"
