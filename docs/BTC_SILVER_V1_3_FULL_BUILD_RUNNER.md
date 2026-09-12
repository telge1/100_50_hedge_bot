# BTC Silver v1.3 Full-Build Runner

Status: prepared but not started. Silver DML requires an explicit `--run` flag,
a fully verified Bronze input, and no active Bronze import process.

## Immutable contract

- Symbol: `BTCUSDT`
- Input database: `research_full_ob_continuous_v1_3`
- Output database: `research_full_ob_silver_v1_3`
- Expected Bronze records: `2,638,997`
- Segments: `164`
- Chain indices: `0–163`
- Chain version:
  `canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459`
- Chain hash:
  `f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333`
- Proven chunk size: `15` market minutes
- Proven warmup: `5` minutes inside the same epoch

Do not start Silver while the Bronze full import is still running.

## Schema initialization

Creates only the new productive Silver v1.3 tables. No Bronze reads beyond
branch/resource checks and no Silver DML.

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 \
  --symbol BTCUSDT \
  --input-database research_full_ob_continuous_v1_3 \
  --output-database research_full_ob_silver_v1_3 \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --expected-bronze-records 2638997 \
  --init-schema \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --min-available-memory-mib 4096 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/build.lock
```

## Read-only preflight

Requires Bronze to be complete, verified, and idle.

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 \
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
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/build.lock
```

## Deliberate future nohup start

Do not run this command until Bronze import is complete and `--check-only` PASS.

```bash
mkdir -p /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3
nohup nice -n 19 ionice -c3 \
  env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 \
  --symbol BTCUSDT \
  --input-database research_full_ob_continuous_v1_3 \
  --output-database research_full_ob_silver_v1_3 \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --expected-bronze-records 2638997 \
  --start-chain-index 0 \
  --end-chain-index 163 \
  --chunk-market-minutes 15 \
  --warmup-minutes 5 \
  --run --resume \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --min-available-memory-mib 4096 \
  --progress-every-chunks 1 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/build.lock \
  >> /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/nohup.log 2>&1 &
echo $! > /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/runner.pid
```

The same command is the resume command. Completed chunks are verified and skipped;
an incomplete chunk is safely rebuilt from its epoch anchor.

## Observe and stop safely

```bash
tail -F /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/nohup.log
cat /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/report.json
ps -fp "$(cat /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/runner.pid)"
kill -TERM "$(cat /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/runner.pid)"
```

Do not use `kill -9` for a normal stop. SIGINT/SIGTERM records the active chunk
as `INTERRUPTED`; a later `--run --resume` safely repeats it.

## Read-only final verification

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 \
  --symbol BTCUSDT \
  --input-database research_full_ob_continuous_v1_3 \
  --output-database research_full_ob_silver_v1_3 \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --expected-bronze-records 2638997 \
  --verify-only \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --min-available-memory-mib 4096 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/build.lock
```

## Runtime expectation

The pre-run estimate from the proven epoch-aware pilot is approximately
10.2 hours for the full 164-hour market span. This is a forecast, not a
guarantee. Per-chunk unbuffered output becomes the authoritative throughput
and ETA after the first chunks complete.
