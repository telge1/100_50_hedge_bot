# Bybit 100-Coin History Backfill

- Window: `2025-12-11T00:00:00+00:00` → `2026-08-15T09:42:00+00:00`
- Selection: `coin_scanner_crypto_tradeability_pass`
- Symbols in run: 1
- Unique FINAL candles: 356262
- Physical rows: 356262
- DB bytes (approx): 749933499

## Coverage

| Status | Count |
| ------ | ----: |
| COMPLETE_CLEAN | 1 |
| COMPLETE_WITH_INTERNAL_GAPS | 0 |
| PARTIAL | 0 |
| FAILED | 0 |
| NO_HISTORY | 0 |

## Quality

- Coins without internal gaps: 1
- Coins with internal gaps: 0
- Total internal missing candles: 0
- Largest internal gap (seconds): 0

- Resume: checkpoint cursor (`last_completed_timestamp`) continues aborted runs.
- Repair: `--repair-missing` fills leading/internal/trailing gaps from ClickHouse;
  COMPLETE is skipped only when resume without repair, or repair finds 0 missing ranges.
- Window extension merges checkpoint windows (no full store reset).

## FINAL query note

- This audit uses `FINAL` for correctness under `ReplacingMergeTree`.
- For frequent production chart queries across ~100 symbols × months,
  prefer ingestion-side idempotency + occasional `OPTIMIZE`, or
  `argMax`/`LIMIT 1 BY` reads / a cleaned projection — avoid relying on
  `FINAL` for every interactive dashboard query.
