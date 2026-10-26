#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/results/phase_bt_d9_first_24h_short_pnl_v1"
mkdir -p "$OUT"
export HISTORICAL_LMV2_BATCH_READER=1
cd "$ROOT"
if [[ -f "$OUT/pipeline.pid" ]]; then
  pid="$(cat "$OUT/pipeline.pid")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "BT-D9 pipeline already running (pid $pid)" >&2
    exit 2
  fi
fi
nohup python tools/phase_bt_d9_first_24h_short_pnl_v1/run_bt_d9.py --mode pipeline \
  >>"$OUT/pipeline.log" 2>&1 &
echo $! >"$OUT/pipeline.nohup.pid"
echo "started nohup pid $(cat "$OUT/pipeline.nohup.pid") log $OUT/pipeline.log"
