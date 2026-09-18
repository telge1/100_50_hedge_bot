# Historical run inputs (read-only)

## Why `runs/` is not versioned

Historical batch CSVs and checkpoints are large, regenerable research artifacts. They remain gitignored so freeze tags and clean worktrees stay small and secret-free.

## How to reference them

1. CLI: `--source-run-dir /abs/path/to/mp_edge_event_batch_v1_YYYYMMDD`
2. Env: `OBFULL_RESEARCH_SOURCE_RUN_DIR=/abs/path/...`
3. Fallback: repo-local `obfull_research_engine/runs/mp_edge_event_batch_v1_20260916` only if present

Required files in that directory:

- `events_all.csv`
- `episodes.csv`
- `batch_windows.csv`

## Input hashes

Resolver records SHA256 of each required CSV and a combined `source_content_id_sha256`. Identical file bytes at different absolute paths share the same content id. Absolute paths are run metadata only and are **not** part of the research contract hash.

## Integration tests

```bash
export OBFULL_RESEARCH_SOURCE_RUN_DIR=/path/to/mp_edge_event_batch_v1_20260916
pytest obfull_research_engine/tests/test_mp_qdh_first_touch_historical_integration.py -q
```

Without the env var the integration test is **SKIPPED**, not failed.

## Credentials

Never put tokens/passwords in path strings or manifests. Paths should point at already-materialized research CSVs only.
