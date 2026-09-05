# STATE_MACHINE

## States

| State | Meaning |
|-------|---------|
| DISCOVERED | Path found via manifest walk |
| VALIDATING | Readiness gates running |
| VALIDATED | Eligible for import |
| IMPORTING | Inserts in progress |
| IMPORTED | Insert finished, not yet parity-checked |
| VERIFYING | Source↔DB parity running |
| VERIFIED | Parity passed; research-usable |
| QUARANTINED | Parity failed; research_eligible=false |
| FAILED_RETRYABLE | Transient (CH down, IO); retry OK |
| FAILED_PERMANENT | SHA/manifest/schema; do not auto-retry blindly |
| OPEN_NOT_ELIGIBLE | tmp/partial/open writers |

## Persistence

1. Local JSON store (`import_state.json`) — atomic replace via `.tmp`
2. ClickHouse `full_ob_import_state` (`ReplacingMergeTree(updated_at)`)

## Resume rules

- `VERIFIED` + `--resume` → skip
- `IMPORTING` / `IMPORTED` / `FAILED_RETRYABLE` → re-import + re-verify
- Physical rows may increase; logical counts must stay stable

## Fields per segment

source_path, source_sha256, file_size, symbol, topic, fight_event_id, segment/continuation index, contract_version, first/last ts/u/seq, record_count, checkpoint_count, continuity_epochs, import_attempts, last_error, import_time, verify_time, db_rows_physical/logical, replay_status
