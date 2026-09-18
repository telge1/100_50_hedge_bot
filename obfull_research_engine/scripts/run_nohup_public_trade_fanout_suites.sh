#!/usr/bin/env bash
# Nohup test runner — every suite gets .log/.pid/.exit under RUN/nohup/
set -euo pipefail

RUN_ROOT="${1:-/home/telgenbuescher/projects/orderbook_analyse_ch_research_mp_qdh_trigger_v1/obfull_research_engine/runs/ema_public_trade_fanout_v1_20260918}"
NOHUP_DIR="$RUN_ROOT/nohup"
mkdir -p "$NOHUP_DIR"

start_job() {
  local name="$1"
  shift
  local logfile="$NOHUP_DIR/${name}.log"
  local pidfile="$NOHUP_DIR/${name}.pid"
  local exitfile="$NOHUP_DIR/${name}.exit"
  local meta="$NOHUP_DIR/${name}.meta"
  local cwd="$1"
  shift
  {
    echo "name=$name"
    echo "start_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "cwd=$cwd"
    echo "cmd=$*"
    echo "worktree=$cwd"
    echo "git_head=$(git -C "$cwd" rev-parse HEAD 2>/dev/null || echo n/a)"
  } > "$meta"
  (
    cd "$cwd"
    # shellcheck disable=SC2068
    nohup bash -c 'set +e; "$@"; ec=$?; echo $ec > "'"$exitfile"'"; exit $ec' _ "$@" \
      >"$logfile" 2>&1 &
    echo $! >"$pidfile"
  )
  local pid
  pid=$(cat "$pidfile")
  # verify pid alive
  sleep 0.2
  if ! kill -0 "$pid" 2>/dev/null; then
    # may have finished instantly
    if [[ ! -f "$exitfile" ]]; then
      echo "FAIL: $name pid $pid not running and no exit file" >&2
      echo 1 >"$exitfile"
    fi
  fi
  echo "STARTED $name pid=$pid"
}

wait_job() {
  local name="$1"
  local pidfile="$NOHUP_DIR/${name}.pid"
  local exitfile="$NOHUP_DIR/${name}.exit"
  local meta="$NOHUP_DIR/${name}.meta"
  local pid
  pid=$(cat "$pidfile")
  while kill -0 "$pid" 2>/dev/null; do
    sleep 2
  done
  # wait for exit file flush
  for _ in $(seq 1 50); do
    [[ -f "$exitfile" ]] && break
    sleep 0.1
  done
  local ec
  ec=$(cat "$exitfile" 2>/dev/null || echo 99)
  echo "end_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$meta"
  echo "exit_code=$ec" >>"$meta"
  echo "FINISHED $name exit=$ec"
  return "$ec"
}

PT_WT=/home/telgenbuescher/projects/public_trades_live_fanout_v1
FO_WT=/home/telgenbuescher/projects/orderbook_analyse_ema_delta_fanout_v1
RES_WT=/home/telgenbuescher/projects/orderbook_analyse_ch_research_mp_qdh_trigger_v1/obfull_research_engine
OA_SRC=/home/telgenbuescher/projects/orderbook_analyse/src

# 1) trade fanout unit tests
start_job trade_fanout_tests "$PT_WT" \
  env PYTHONPATH=src python3 -m pytest tests/unit/test_public_trade_event_fanout_v1.py -q --tb=line

# 2) full-ob regression + 10k
start_job full_ob_tests "$FO_WT" \
  env PYTHONPATH=src python3 -m pytest \
    tests/test_full_ob_event_fanout_v1.py \
    tests/test_full_ob_case_archive_v1.py \
    tests/test_full_ob_continuous_raw_archive_a1_a2.py \
    tests/test_full_ob_10k_depth_v1.py \
    -q --tb=line

# 3) analyzer tests
start_job analyzer_tests "$RES_WT" \
  env PYTHONPATH="src:$OA_SRC" python3 -m pytest \
    tests/test_ema_trend_live_analyzer_v1.py \
    tests/test_ema_trend_live_analyzer_v1_expanded.py \
    tests/test_ema_live_trade_fanout_v1.py \
    -q --tb=line

wait_job trade_fanout_tests
TF_EC=$?
wait_job full_ob_tests
FO_EC=$?
wait_job analyzer_tests
AN_EC=$?

# leftover pids check
LEFTOVER=0
for name in trade_fanout_tests full_ob_tests analyzer_tests; do
  pid=$(cat "$NOHUP_DIR/${name}.pid" 2>/dev/null || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "LEFTOVER_PID $name $pid"
    LEFTOVER=1
  fi
done

{
  echo "# NOHUP_TEST_REPORT"
  echo
  echo "| suite | exit | head |"
  echo "|-------|------|------|"
  for name in trade_fanout_tests full_ob_tests analyzer_tests; do
    ec=$(cat "$NOHUP_DIR/${name}.exit" 2>/dev/null || echo missing)
    head=$(grep '^git_head=' "$NOHUP_DIR/${name}.meta" | cut -d= -f2)
    echo "| $name | $ec | \`$head\` |"
    echo
    echo "### $name log tail"
    echo '```'
    tail -30 "$NOHUP_DIR/${name}.log" 2>/dev/null || true
    echo '```'
  done
  echo
  echo "**LEFTOVER_PIDS:** $LEFTOVER"
  echo "**ALL_NOHUP_EXIT_CODES_ZERO:** $([ "$TF_EC$FO_EC$AN_EC$LEFTOVER" = "0000" ] && echo YES || echo NO)"
} > "$RUN_ROOT/NOHUP_TEST_REPORT.md"

echo "SUMMARY trade=$TF_EC fullob=$FO_EC analyzer=$AN_EC leftover=$LEFTOVER"
exit $(( TF_EC + FO_EC + AN_EC + LEFTOVER ))
