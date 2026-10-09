#!/usr/bin/env bash
# Start three separate OB200 import jobs for DOGE, XRP, and ADA.
# Each job gets its own nohup log and progress directory.
set -euo pipefail

PROJECT_ROOT="/home/telgenbuescher/projects/pools+ob+delta_bot"
ORDERBOOK_ANALYSE_ROOT="/home/telgenbuescher/projects/orderbook_analyse"
IMPORT_SCRIPT="${ORDERBOOK_ANALYSE_ROOT}/scripts/run_orderbook_v3_30d_import.py"
PYTHON_BIN="${ORDERBOOK_ANALYSE_ROOT}/.venv/bin/python"

START_DAY="${START_DAY:-2026-06-26}"
END_DAY="${END_DAY:-2026-09-25}"

if [[ ! -f "${IMPORT_SCRIPT}" ]]; then
  echo "Missing import script: ${IMPORT_SCRIPT}" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "No python interpreter found" >&2
  exit 1
fi

mkdir -p "${PROJECT_ROOT}/results/ob200_3coins/logs"

symbols=(DOGEUSDT XRPUSDT ADAUSDT)

echo "Starting OB200 imports for: ${symbols[*]}"
echo "Window: ${START_DAY} .. ${END_DAY}"
echo "Logs: ${PROJECT_ROOT}/results/ob200_3coins/logs"
echo "Status: ${PROJECT_ROOT}/results/ob200_3coins/<SYMBOL>/progress.json"

for symbol in "${symbols[@]}"; do
  job_dir="${PROJECT_ROOT}/results/ob200_3coins/${symbol}"
  log_file="${PROJECT_ROOT}/results/ob200_3coins/logs/${symbol}.nohup.log"
  pid_file="${job_dir}/import.pid"
  mkdir -p "${job_dir}"

  if [[ -f "${pid_file}" ]] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
    echo "SKIP ${symbol}: already running (pid $(cat "${pid_file}"))"
    continue
  fi

  nohup "${PYTHON_BIN}" "${IMPORT_SCRIPT}" \
    --start-day "${START_DAY}" \
    --end-day "${END_DAY}" \
    --symbols "${symbol}" \
    --result-dir "${job_dir}" \
    >"${log_file}" 2>&1 &

  echo $! >"${pid_file}"
  echo "STARTED ${symbol}: pid=$! log=${log_file} status=${job_dir}/progress.json"
done

echo
echo "Use these commands to check status:"
echo "  cat ${PROJECT_ROOT}/results/ob200_3coins/DOGEUSDT/progress.json"
echo "  cat ${PROJECT_ROOT}/results/ob200_3coins/XRPUSDT/progress.json"
echo "  cat ${PROJECT_ROOT}/results/ob200_3coins/ADAUSDT/progress.json"
