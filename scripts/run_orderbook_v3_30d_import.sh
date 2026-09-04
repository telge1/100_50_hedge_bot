#!/usr/bin/env bash
# Thin nohup wrapper for scripts/run_orderbook_v3_30d_import.py
# Does not start/stop collectors. No DELETE. No OPTIMIZE FINAL.
set -euo pipefail
PROJECT_ROOT="/home/telgenbuescher/projects/orderbook_analyse"
cd "${PROJECT_ROOT}"
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH=src
mkdir -p logs results/orderbook_v3_30d_import
exec .venv/bin/python scripts/run_orderbook_v3_30d_import.py "$@"
