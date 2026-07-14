#!/usr/bin/env bash
# Server-safe batched resume for liquidation optimizer.
# - nice priority
# - small batches
# - abort if load or free memory look dangerous
set -euo pipefail
ROOT="/home/telgenbuescher/projects/spread_recovery_hedge_short_dev"
OUT="$ROOT/research/liquidation_level/results/APTUSDT_5m_optimizer_v1"
LOG="$OUT/safe_batch_run.log"
BATCH_SIZE="${BATCH_SIZE:-10}"
MAX_LOAD="${MAX_LOAD:-3.5}"
MIN_AVAIL_MIB="${MIN_AVAIL_MIB:-1800}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-20}"
TARGET=200

cd "$ROOT"
mkdir -p "$OUT"
exec >>"$LOG" 2>&1

echo "===== safe batch start $(date -Is) batch=$BATCH_SIZE ====="

avail_mib() {
  awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo
}

load1() {
  cut -d' ' -f1 /proc/loadavg
}

completed_n() {
  if [[ -f "$OUT/completed_configurations.jsonl" ]]; then
    wc -l <"$OUT/completed_configurations.jsonl"
  else
    echo 0
  fi
}

while true; do
  DONE=$(completed_n)
  echo "$(date -Is) completed=$DONE / $TARGET load=$(load1) avail_mib=$(avail_mib)"
  if (( DONE >= TARGET )); then
    echo "all $TARGET configs reached"
    break
  fi

  LOAD=$(load1)
  AVAIL=$(avail_mib)
  # bash float compare via awk
  if awk -v l="$LOAD" -v m="$MAX_LOAD" 'BEGIN{exit !(l>m)}'; then
    echo "abort: load $LOAD > $MAX_LOAD — waiting ${SLEEP_BETWEEN}s"
    sleep "$SLEEP_BETWEEN"
    continue
  fi
  if (( AVAIL < MIN_AVAIL_MIB )); then
    echo "abort: MemAvailable ${AVAIL}MiB < ${MIN_AVAIL_MIB}MiB — waiting ${SLEEP_BETWEEN}s"
    sleep "$SLEEP_BETWEEN"
    continue
  fi

  echo "running batch_size=$BATCH_SIZE ..."
  nice -n 19 ionice -c3 env PYTHONPATH=. python3 -m research.liquidation_level.run_liquidation_optimizer \
    --grid-config research/liquidation_level/configs/liquidation_optimizer_grid.json \
    --feather-file /home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather \
    --symbol APTUSDT \
    --output-dir "$OUT" \
    --max-configs 200 \
    --batch-size "$BATCH_SIZE" \
    --max-mem-cache 1 \
    --skip-controls \
    --workers 1 \
    --seed 42 \
    --resume \
    --progress-every 2

  sleep "$SLEEP_BETWEEN"
done

echo "final ranking pass with controls $(date -Is)"
nice -n 19 ionice -c3 env PYTHONPATH=. python3 -m research.liquidation_level.run_liquidation_optimizer \
  --grid-config research/liquidation_level/configs/liquidation_optimizer_grid.json \
  --feather-file /home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather \
  --symbol APTUSDT \
  --output-dir "$OUT" \
  --max-configs 200 \
  --batch-size 1 \
  --max-mem-cache 1 \
  --with-controls \
  --workers 1 \
  --seed 42 \
  --resume \
  --progress-every 1

echo "===== safe batch done $(date -Is) ====="
