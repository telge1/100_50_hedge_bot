# BTC Silver v1.3 Bucket-Boundary Repair Runner

Append-only repair for COMPLETE chunks that are missing the terminal 100ms
state at off-grid chunk ends (ledger `8999` instead of planned `9000`).

## Contract

- Default is read-only (`--check-only` / `--verify-only`)
- Productive DML requires explicit `--run`
- Never deletes or rewrites Level-Changes
- Never deletes existing states; only inserts missing state row_ids
- Exclusive `repair.lock` (must not run while full builder holds `build.lock`)
- Full Silver `--run` hard-stops with
  `STOP_SILVER_FULL_RESUME_UNTIL_REPAIR_VERIFIED` while COMPLETE holes remain

## Ops order (mandatory)

```text
Repair check-only
→ Repair run
→ Repair verify-only
→ Silver check-only
→ Silver resume
→ Silver final verify
```

## Check-only

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.silver_bucket_boundary_repair_v1_3 \
  --symbol BTCUSDT \
  --input-database research_full_ob_continuous_v1_3 \
  --output-database research_full_ob_silver_v1_3 \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --expected-bronze-records 2638997 \
  --check-only \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --min-available-memory-mib 4096 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/check_report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/repair.lock
```

## Productive repair (do not run until authorized)

```bash
mkdir -p /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3
nohup nice -n 19 ionice -c3 \
  env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.silver_bucket_boundary_repair_v1_3 \
  --symbol BTCUSDT \
  --input-database research_full_ob_continuous_v1_3 \
  --output-database research_full_ob_silver_v1_3 \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --expected-bronze-records 2638997 \
  --run --resume \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --min-available-memory-mib 4096 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/run_report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/repair.lock \
  >> /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/nohup.log 2>&1 &
echo $! > /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/runner.pid
```

## Verify-only

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.silver_bucket_boundary_repair_v1_3 \
  --symbol BTCUSDT \
  --input-database research_full_ob_continuous_v1_3 \
  --output-database research_full_ob_silver_v1_3 \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --expected-bronze-records 2638997 \
  --verify-only \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/verify_report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/repair.lock
```
