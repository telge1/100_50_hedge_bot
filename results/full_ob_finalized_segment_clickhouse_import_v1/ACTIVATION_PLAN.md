# ACTIVATION_PLAN

Status: **READY_ACTIVATION_REQUIRED** — no watch/service installed.

## Preconditions before any live activation

1. Explicit human approval for watch-mode or timer
2. Confirm collector PID still healthy
3. Confirm target DB remains `research_full_ob_import_pilot_v1` (or successor research DB)
4. Confirm `--require-finalized --verify-replay`
5. Resource caps: `nice 10`, batch ≤ 50, single-process, poll ≥ 30s

## Suggested first activation (manual, not done)

```bash
export PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse/src
/home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python \
  /home/telgenbuescher/projects/orderbook_analyse/scripts/import_finalized_full_ob_segments.py \
  --source-root /home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_edge_flight_recorder \
  --database research_full_ob_import_pilot_v1 \
  --symbols BTCUSDT,DOGEUSDT \
  --once --require-finalized --verify-replay
```

## Explicitly NOT done

- systemd install/enable
- `--watch` loop
- production table creation
- collector / OI restart
- mutating `orderbook_analysis` / smoke / prior BTC analysis DBs

## Rollback

Stop importer process only. Sources untouched. Pilot DB may be dropped if needed without affecting production.
