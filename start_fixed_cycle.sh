#!/usr/bin/env bash

# Run fixed-cycle strategy via the modular fixed_cycle runner.
# Make sure env keys are set (see /strategy/env or /env/local.env) before launching.
PROJECT_ROOT="$(dirname "$0")"
if [ -z "${PYTHON:-}" ]; then
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
fi
cd "${PROJECT_ROOT}"
nohup "${PYTHON}" -m fixed_cycle_hedge_bot.runner \
  --strategy fixed_cycle \
  --strategy-config-file fixed_cycle_hedge_bot/config/fixed_cycle_config.json \
  --audit-log-file logs/generic_hedge_runtime_audit.jsonl \
  > logs/fixed_cycle_runner.stdout.log 2>&1 &
PID=$!
echo "$PID" > logs/fixed_cycle_bot.pid
echo "Fixed cycle bot started via nohup (logs/fixed_cycle_runner.stdout.log)"
echo "Started fixed-cycle bot with PID=$PID"
