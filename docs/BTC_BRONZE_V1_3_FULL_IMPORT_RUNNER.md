# BTC Bronze v1.3 Full-Import Runner

Status: prepared but not started. The runner imports no events unless `--run`
is explicitly present.

## Immutable contract

- Symbol: `BTCUSDT`
- Segments: `164`
- Expected logical records: `2,638,997`
- Chain version:
  `canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459`
- Chain hash:
  `f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333`
- Target database: `research_full_ob_continuous_v1_3`

The preflight and schema commands are preparation steps. The separately marked
nohup command starts the full import and must not be executed without explicit
authorization. None of these commands was executed while creating this checkpoint.

## Read-only preflight

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.bronze_full_import_v1_3 \
  --symbol BTCUSDT \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --archive-root /home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_v1 \
  --database research_full_ob_continuous_v1_3 \
  --check-only \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/import.lock
```

## Schema initialization

This creates only the three new productive v1.3 tables. It does not register
segments or import events.

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.bronze_full_import_v1_3 \
  --symbol BTCUSDT \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --archive-root /home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_v1 \
  --database research_full_ob_continuous_v1_3 \
  --init-schema \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/import.lock
```

## Deliberate future nohup start

Do not run this command until the full import is separately authorized.

```bash
mkdir -p /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3
nohup nice -n 19 ionice -c3 \
  env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.bronze_full_import_v1_3 \
  --symbol BTCUSDT \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --archive-root /home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_v1 \
  --database research_full_ob_continuous_v1_3 \
  --run --resume \
  --start-chain-index 0 \
  --end-chain-index 163 \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --progress-every-segments 1 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/import.lock \
  > /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/nohup.log 2>&1 &
echo $! > /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/runner.pid
```

The exact same command is the resume command. Completed segments are verified
and skipped; an incomplete segment is replayed from its start with deterministic
row IDs.

## Observe and stop safely

```bash
tail -F /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/nohup.log
cat /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/report.json
ps -fp "$(cat /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/runner.pid)"
kill -TERM "$(cat /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/runner.pid)"
```

Do not use `kill -9` for a normal stop. SIGINT/SIGTERM records the active
segment as `INTERRUPTED`; a later `--run --resume` safely repeats it.

## Read-only final verification

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.bronze_full_import_v1_3 \
  --symbol BTCUSDT \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --archive-root /home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_v1 \
  --database research_full_ob_continuous_v1_3 \
  --verify-only \
  --max-rss-mib 1536 \
  --min-free-disk-gib 200 \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/bronze_full_import_v1_3/import.lock
```

## Runtime expectation

The pre-run estimate is deliberately broad: approximately 15–45 minutes for
2,638,997 records, including all 164 archive SHA checks. It is a forecast, not
a guarantee. The unbuffered per-segment output becomes the authoritative
throughput and ETA after the first segments.
