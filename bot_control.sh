#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CYCLE_STATE_FILE="$SCRIPT_DIR/fixed_cycle_hedge_bot/state.json"
# allow overriding the strategy state path if the runtime was started with a non-default file
STRATEGY_STATE_FILE="${BOT_CONTROL_STRATEGY_STATE_FILE:-$SCRIPT_DIR/logs/fixed_cycle_state.json}"
PID_FILE="$SCRIPT_DIR/logs/fixed_cycle_bot.pid"
# Match common runner invocations. pgrep -f uses regex.
RUN_PATTERN="${BOT_CONTROL_RUN_PATTERN:-fixed_cycle_hedge_bot(\.runner|/runner)|-m[[:space:]]+fixed_cycle_hedge_bot\.runner}"

STOP_TERM_TIMEOUT_SECONDS="${BOT_CONTROL_STOP_TERM_TIMEOUT_SECONDS:-30}"
STOP_KILL_TIMEOUT_SECONDS="${BOT_CONTROL_STOP_KILL_TIMEOUT_SECONDS:-10}"

function log_err() {
  echo "[ERROR]" "$@" >&2
}

function log_info() {
  echo "[INFO]" "$@"
}

function _pid_is_ours() {
  local pid="$1"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  if ! kill -0 "$pid" >/dev/null 2>&1; then
    return 1
  fi
  # only treat it as ours if the command line looks like the fixed cycle runner
  local cmdline=""
  cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ "$cmdline" =~ fixed_cycle_hedge_bot ]] && [[ "$cmdline" =~ runner ]]
}

function find_bot_pids() {
  local pids=()
  local pid_from_file=""

  if [[ -f "$PID_FILE" ]]; then
    pid_from_file="$(tr -cd '0-9' < "$PID_FILE" | head -c 20 || true)"
    if [[ -n "$pid_from_file" ]] && _pid_is_ours "$pid_from_file"; then
      pids+=("$pid_from_file")
    fi
  fi

  mapfile -t _pgrep_pids < <(pgrep -f "$RUN_PATTERN" 2>/dev/null || true)
  for pid in "${_pgrep_pids[@]:-}"; do
    if _pid_is_ours "$pid"; then
      pids+=("$pid")
    fi
  done

  # de-dup
  if [[ ${#pids[@]} -gt 0 ]]; then
    printf "%s\n" "${pids[@]}" | awk '!seen[$0]++' | tr '\n' ' '
  fi
}

function stop_bot() {
  local pids
  read -r -a pids <<< "$(find_bot_pids)"
  if [[ ${#pids[@]} -eq 0 ]]; then
    log_info "No running bot process found."
    return 0
  fi

  log_info "Stopping bot (pids=${pids[*]})..."
  kill -TERM "${pids[@]}" 2>/dev/null || true

  local wait_seconds=0
  while [[ $wait_seconds -lt "$STOP_TERM_TIMEOUT_SECONDS" ]]; do
    local alive=0
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" >/dev/null 2>&1; then alive=1; break; fi
    done
    if [[ $alive -eq 0 ]]; then log_info "Bot stopped cleanly."; return 0; fi
    sleep 1
    wait_seconds=$((wait_seconds + 1))
  done

  log_err "Bot did not exit within ${STOP_TERM_TIMEOUT_SECONDS}s; sending SIGKILL (pids=${pids[*]})..."
  kill -KILL "${pids[@]}" 2>/dev/null || true

  wait_seconds=0
  while [[ $wait_seconds -lt "$STOP_KILL_TIMEOUT_SECONDS" ]]; do
    local alive=0
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" >/dev/null 2>&1; then alive=1; break; fi
    done
    if [[ $alive -eq 0 ]]; then log_info "Bot stopped after SIGKILL."; return 0; fi
    sleep 1
    wait_seconds=$((wait_seconds + 1))
  done

  log_err "Bot still running after SIGKILL timeout (${STOP_KILL_TIMEOUT_SECONDS}s)."
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

function read_pending_dynamic_symbol() {
  local symbol=""
  for file in "$STRATEGY_STATE_FILE" "$CYCLE_STATE_FILE"; do
    if [[ ! -f "$file" ]]; then
      continue
    fi
    symbol=$(
      python - <<'PY'
import json, sys, pathlib
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
for candidate in ("pending_dynamic_symbol",):
    if candidate in data and data[candidate]:
        print(data[candidate])
        sys.exit(0)
state = data.get("strategy_state") or {}
symbol = state.get("pending_dynamic_symbol")
if symbol:
    print(symbol)
PY
      "$file"
    )
    symbol="${symbol//$'\n'/}"
    symbol="${symbol//[[:space:]]/}"
    if [[ -n "$symbol" ]]; then
      echo "$symbol"
      return 0
    fi
  done
  return 1
}

function write_pending_symbol_to_best_coin() {
  local symbol="$1"
  if [[ -z "$symbol" ]]; then
    return 0
  fi
  local best_coin_path="$SCRIPT_DIR/logs/best_coin.json"
  local tmp
  tmp="$(mktemp)"
  python - <<PY >"$tmp"
import json, datetime, sys
symbol = sys.argv[1]
payload = {
    "symbol": symbol,
    "score": 999999,
    "source": "pending_dynamic_symbol_restart",
    "reason": "captured pending dynamic symbol before hard-reset",
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
print(json.dumps(payload, indent=2, ensure_ascii=False))
PY
  mv "$tmp" "$best_coin_path"
  log_info "Wrote pending dynamic symbol to best_coin.json: $symbol"
}

function verify_state_deleted() {
  local file
  for file in "$CYCLE_STATE_FILE" "$STRATEGY_STATE_FILE"; do
    if [[ -f "$file" ]]; then
      log_err "State file still present after delete: $file"
      return 1
    fi
  done
  return 0
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
  if ! command -v "$python_bin" >/dev/null 2>&1; then
    # fallback to repo venv if available (common in this project)
    if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
      python_bin="$SCRIPT_DIR/.venv/bin/python"
      log_info "BOT_CONTROL_PYTHON not found; falling back to ${python_bin}"
    fi
  fi
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
    local pending_symbol
    if pending_symbol="$(read_pending_dynamic_symbol)"; then
      log_info "Captured pending dynamic symbol: $pending_symbol"
      write_pending_symbol_to_best_coin "$pending_symbol"
    else
      log_info "No pending dynamic symbol found; best_coin.json left untouched"
    fi
    if ! stop_bot; then
      log_err "Failed to stop bot; aborting hard reset."
      exit 1
    fi
    rm -f "$PID_FILE"
    if ! cleanup_exchange; then
      log_err "Hard reset failed: exchange cleanup failed."
      exit 1
    fi
    delete_state_files
    if ! verify_state_deleted; then
      log_err "Hard reset failed: state delete verification failed."
      exit 1
    fi
    log_info "hard_reset_success"
    exit 0
    ;;

  *)
    log_err "Unknown command: $cmd"
    exit 1
    ;;
esac
