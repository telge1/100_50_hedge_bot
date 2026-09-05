# IMPORT_CONTRACT — full_ob_finalized_segment_clickhouse_import_v1

## Process isolation

Importer runs only as a separate CLI/process (`nice ≥ 10`). Never inside the WebSocket/collector event loop.

## Segment readiness (all required)

| Gate | Rule |
|------|------|
| extension | `.jsonl.zst` |
| open suffixes | `*.tmp` / `*.partial` / `*.open` → `OPEN_NOT_ELIGIBLE` |
| finalized | `segment_sha256` present in `event_manifest.json` |
| manifest | must exist; silent import without manifest forbidden |
| sha | `actual_sha256 == expected_sha256` |
| size | `file_size > 0` |
| writers | no process holds file open for write (`/proc/*/fd`) |
| identity | symbol, fight_event_id, continuation_index valid |

## Import states

`DISCOVERED → VALIDATING → VALIDATED → IMPORTING → IMPORTED → VERIFYING → VERIFIED`

Failure: `FAILED_RETRYABLE` | `FAILED_PERMANENT` | `QUARANTINED` | `OPEN_NOT_ELIGIBLE`

Crash between `IMPORTING` and `VERIFIED` is resume-safe (re-insert + ReplacingMergeTree + canonical views).

## Idempotency

- Same source record → same `record_id`
- Physical duplicates allowed; logical views count once
- No `OPTIMIZE FINAL` required for correct analysis counts

## Level storage default

Raw packet rows (exact String price/qty arrays) + `v_full_ob_level_changes` via `arrayJoin`. No global persistent level-change table in v1.

## Parity gates (mandatory)

```
source_logical_records == db_logical_records
parse_rejects == 0
source_book_hash == db_book_hash (checkpoints)
```

Mismatch → `QUARANTINED`, `research_eligible=false`. No auto-delete.

## CLI safety

- `--dry-run` or explicit `--database` required
- Production / protected DBs refused
- `--watch` refused until activation approval
