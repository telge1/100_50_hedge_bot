# Dedup query contract (smoke DB only)

## Problem
`ReplacingMergeTree` retains physical duplicates until merges. After two identical imports **without** `OPTIMIZE FINAL`:

| Metric | Value |
|---|---|
| physical rows | 406 |
| GROUP BY packet_sha256 | 203 |
| SELECT FINAL | 203 |
| dedup VIEW | 203 |
| naive sum(bid_n) | 24954 |
| argMax dedup sum(bid_n) | 12477 |
| naive/dedup ratio | 2.0 |

## Stable identity
`packet_sha256 = sha256(source_file \| source_line_number \| canonical_record_json)`

## Canonical read paths (no OPTIMIZE required)

1. **GROUP BY / argMax** (preferred analytics) — 0.0578s on smoke
2. **FINAL** — 0.0579s on smoke
3. **View** `research_full_ob_smoke.v_full_ob_gap_audit_packets_dedup`

## Rules
1. Always report physical vs logical counts separately.
2. Never aggregate volumes from the raw table without dedup.
3. Prefer GROUP BY/argMax or the view; FINAL is fine for small reads.
4. OPTIMIZE FINAL is optional compaction only.
5. No production migration in this audit.

## Result
`passed=True`
