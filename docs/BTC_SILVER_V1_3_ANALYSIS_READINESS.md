# BTC Silver v1.3 Analysis Readiness

Fail-closed contract for **parallel read-only analysis** while the Silver full
build is still running.

## Rule

A time window `[start_ns, end_ns)` is **READY** only when all of the following
hold:

1. Every covering chunk is `COMPLETE` in `silver_build_chunks_v1_3 FINAL`
2. Stored `level_change_count` / `state_count` match ClickHouse outputs exactly
3. `output_hash` is a non-zero 64-hex digest
4. No `RUNNING` / `INTERRUPTED` / `FAILED` row is logically current for those chunks
5. The window lies entirely inside one replay `epoch_id`
6. The window does not cross a gap (`silver_epoch_gaps_v1_3`) or blind hole
7. Start/end lie within COMPLETE chunk bounds

Otherwise the window is **NOT_READY** and every analysis entry point must hard-stop
via `assert_analysis_window_ready(...)`.

## Parallel analysis constraints

- `SELECT` only
- `max_threads=1`
- `max_memory_usage=536870912` (512 MiB)
- `max_execution_time=60`
- no unbounded full-table scans (require `chunk_key` / `epoch_id` / time bounds)
- never create/modify the builder lock
- builder keeps resource priority
- if available RAM `< 6 GiB`, swap grows above the analysis baseline, or ClickHouse
  raises a memory limit: **analysis STOP**, builder untouched

## CLI (read-only)

```bash
env PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/src \
  /home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -u \
  -m obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 \
  --symbol BTCUSDT \
  --input-database research_full_ob_continuous_v1_3 \
  --output-database research_full_ob_silver_v1_3 \
  --chain-version canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459 \
  --expected-chain-hash f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333 \
  --report-only \
  --report-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_analysis_readiness_v1_3/report.json \
  --lock-path /home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/obfull_research_engine/runs/silver_full_build_v1_3/build.lock
```

Assert a concrete window:

```bash
... analysis_readiness_v1_3 \
  --assert-window \
  --start-ns <NS> \
  --end-ns <NS> \
  --report-path .../assert_window.json \
  --lock-path .../build.lock
```

## Library gate

```python
from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
    assert_analysis_window_ready,
    gated_select,
)

assert_analysis_window_ready(client, config, start_ns=..., end_ns=...)
gated_select(client, config, "SELECT ... WHERE chunk_key = {chunk_key:String}", start_ns=..., end_ns=...)
```

## Report fields

- analyzable `[start_ns, end_ns)` windows
- `epoch_id`, `chunk_key`(s)
- level-change / state counts
- output hash(es)
- `READY` / `NOT_READY` + reason
- contiguous analysis watermark
- COMPLETE / RUNNING / INTERRUPTED / FAILED / missing chunk counts
