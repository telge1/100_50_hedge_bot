#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CYCLE_STATE_FILE="$SCRIPT_DIR/fixed_cycle_hedge_bot/state.json"
# allow overriding the strategy state path if the runtime was started with a non-default file
STRATEGY_STATE_FILE="${BOT_CONTROL_STRATEGY_STATE_FILE:-$SCRIPT_DIR/logs/fixed_cycle_state.json}"
RUN_PATTERN="fixed_cycle_hedge_bot\.runner"

function log_err() {
  echo "[ERROR]" "$@" >&2
}

function log_info() {
  echo "[INFO]" "$@"
}

function find_bot_pids() {
  mapfile -t pids < <(pgrep -f "$RUN_PATTERN" || true)
  echo "${pids[@]}"
}

function stop_bot() {
  local pids
  read -r -a pids <<< "$(find_bot_pids)"
  if [[ ${#pids[@]} -eq 0 ]]; then
    log_info "No running bot process found."
    return 0
  fi

  log_info "Stopping bot (pids=${pids[*]})..."
  kill -TERM "${pids[@]}"

  local wait_seconds=0
  while [[ $wait_seconds -lt 30 ]]; do
    local alive=0
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" >/dev/null 2>&1; then
        alive=1
        break
      fi
    done
    if [[ $alive -eq 0 ]]; then
      log_info "Bot stopped cleanly."
      return 0
    fi
    sleep 1
    wait_seconds=$((wait_seconds + 1))
  done

  log_err "Bot did not exit within 30 seconds (still running: ${pids[*]})."
  return 1
}

function delete_state_files() {
  local file
  for file in "$CYCLE_STATE_FILE" "$STRATEGY_STATE_FILE"; do
    if [[ -f "$file" ]]; then
      rm -f "$file"
      log_info "Removed state file: $file"
    else
      log_info "State file already absent: $file"
    fi
  done
}

function cleanup_exchange() {
  local prev_dir
  local exit_code=0
  local python_bin
  local cleanup_cmd

  prev_dir="$(pwd)"
  cd "$SCRIPT_DIR" || return 1
  log_info "Running exchange cleanup (cancel-open orders)..."

  python_bin="${BOT_CONTROL_PYTHON:-python}"
  cleanup_cmd=("$python_bin" -m fixed_cycle_hedge_bot.cleanup --config-file "$SCRIPT_DIR/fixed_cycle_hedge_bot/config/fixed_cycle_config.json")
  if [[ -n "${BOT_CONTROL_CLEANUP_SYMBOL:-}" ]]; then
    log_info "Cleanup symbol override: ${BOT_CONTROL_CLEANUP_SYMBOL}"
    cleanup_cmd+=(--symbol "${BOT_CONTROL_CLEANUP_SYMBOL}")
  fi

  if ! "${cleanup_cmd[@]}"; then
    log_err "Exchange cleanup failed"
    exit_code=1
  fi

  cd "$prev_dir" >/dev/null || true
  return $exit_code
}

if [[ $# -lt 1 ]]; then
  log_err "Usage: $0 {soft-stop|hard-reset}"
  exit 1
fi

cmd="$1"

case "$cmd" in
  soft-stop)
    stop_bot
    exit $?
    ;;

  hard-reset)
    if ! stop_bot; then
      log_err "Failed to stop bot; aborting hard reset."
      exit 1
    fi
  rm -f "$SCRIPT_DIR/logs/fixed_cycle_bot.pid"
    cleanup_exchange
    delete_state_files
    exit 0
    ;;

  *)
    log_err "Unknown command: $cmd"
    exit 1
    ;;
esac
