# TEST_REPORT — full_ob_finalized_segment_clickhouse_import_v1

## Summary

**26/26 PASS** (`tests/test_full_ob_finalized_segment_clickhouse_import_v1.py`, 4.79s)

## Coverage map

| # | Case | Result |
|---|------|--------|
| 1 | Only finalized `.jsonl.zst` | PASS |
| 2 | `.tmp` always excluded | PASS |
| 3 | Manifest missing | PASS |
| 4 | SHA wrong | PASS |
| 5 | Initial checkpoint present | PASS |
| 6 | Resync checkpoint present | PASS |
| 7 | Multi-epoch event | PASS |
| 8 | Markers not in u-continuity kinds | PASS |
| 9 | Parent/nested signal load | PASS |
| 10 | Signal isolation schema | PASS |
| 11 | Overlap cluster field | PASS |
| 12 | Double import idempotency | PASS |
| 13 | Parallel ID stability | PASS |
| 14 | Process abort resume | PASS |
| 15 | CH unreachable → retryable | PASS |
| 16 | Partial batch logical dedup | PASS |
| 17 | Physical ≥ logical | PASS |
| 18 | Source/DB book hash | PASS |
| 19 | Checkpoint manipulation | PASS |
| 20 | Segment order | PASS |
| 21 | Predecessor / seg0 | PASS |
| 22 | Open event + finalized segs | PASS |
| 23 | Production DB refused | PASS |
| 24 | Smoke DB 1514 unchanged | PASS |
| 25 | Collector/OI PIDs alive | PASS |
| + | Local state atomic write | PASS |

## Pilot evidence files

`pilot_dry_run.json`, `pilot_import_result.json`, `pilot_parity.json`, `pilot_replay.json`, `pilot_idempotency.json`, `pilot_resume_test.json`, `pilot_quarantine_test.json`
