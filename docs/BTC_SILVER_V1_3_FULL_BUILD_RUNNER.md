# BTC Silver v1.3 Full-Build Runner

Status: builder stopped after `STOP_SILVER_MEMORY_LIMIT` on chunk
`4dd543b356b0dfe78a4deaa893c291bbf5acbae6c2991d3ce50d45527b9b4126`.
Before any productive `--run --resume`, complete the append-only bucket-boundary
repair (see [BTC_SILVER_V1_3_BUCKET_BOUNDARY_REPAIR.md](BTC_SILVER_V1_3_BUCKET_BOUNDARY_REPAIR.md)).
Full `--run` hard-stops with `STOP_SILVER_FULL_RESUME_UNTIL_REPAIR_VERIFIED`
while COMPLETE chunks still have missing terminal 100ms states.

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
- Full-build output warmup: `0` minutes (exchange snapshot is the book anchor)
- Optional replay warm-up prefix for bounded pilots: pass `--warmup-minutes` explicitly
- Bucket contract: `[bucket_start, bucket_start+100ms)`; state includes every
  delta with `event_time < bucket_end`. Planner and emitter share
  `ceil(start) while start < end`. Artificial chunk ends may extend the
  *read/apply* window to `evaluation_end_ns` within the same epoch; LCs stay
  inside `[analysis_start, analysis_end)`.
- Expected full plan bucket count remains `5,619,393`
- Bounded memory: LC and state inserts stream in batches during replay
  (no full chunk LC materialization). Python RSS cap stays `1536` MiB.
  Memory aborts mark the chunk `INTERRUPTED` (fail-closed).

Do not start Silver while the Bronze full import is still running.
Do not start Silver resume until bucket repair is `REPAIR_VERIFIED`.

## Parallel analysis while building

Completed Silver windows may be analysed read-only in parallel with the builder.
See [BTC_SILVER_V1_3_ANALYSIS_READINESS.md](BTC_SILVER_V1_3_ANALYSIS_READINESS.md).
Analysis must call `assert_analysis_window_ready` first and must never touch the
builder lock.

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

## Read-only epoch-plan proof

Streams Bronze per segment without delta payloads and without Silver DML.
Use this after `--check-only` PASS and before any `--run`.

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
  --epoch-plan-only \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --min-available-memory-mib 4096 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/epoch_plan_profile.json \
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

## ClickHouse session isolation

The full builder opens three dedicated ClickHouse HTTP clients:

- `read` – Bronze `query_row_block_stream` only
- `write` – Silver inserts and chunk-ledger updates
- `verify` – post-insert count checks and resource probes

Each client has its own `session_id`. `replay_epoch_window` may stop at
`analysis_end_ns` before the Bronze stream is exhausted; the runner always
closes the Bronze generator before any write/verify query. ClickHouse code
373 / `SESSION_IS_LOCKED` maps to `STOP_SILVER_CH_SESSION_LOCKED` and leaves
the chunk ledger in a resume-capable `INTERRUPTED` state.

## Runtime expectation

The pre-run estimate from the proven epoch-aware pilot is approximately
10.2 hours for the full 164-hour market span. This is a forecast, not a
guarantee. Per-chunk unbuffered output becomes the authoritative throughput
and ETA after the first chunks complete.
