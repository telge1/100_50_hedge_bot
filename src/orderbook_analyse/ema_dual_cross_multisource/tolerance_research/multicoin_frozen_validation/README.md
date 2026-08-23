# Multi-Coin Frozen Validation (Research-Only)

Validates XRP-frozen EDC strategies across `config/universe_tradeable_51.json`.

## CLI defaults

| Flag | Default |
|------|---------|
| `--symbols-file` | `config/universe_tradeable_51.json` |
| `--start` | `2026-07-24T00:00:00Z` |
| `--end` | `2026-08-23T00:00:00Z` (exclusive) |
| `--output-dir` | `results/edc_sync_tolerance/multicoin_30d_frozen_validation` |
| `--max-workers` | `1` |
| `--checkpoint-every` | `1` |

## Modes

| Flag | Behavior |
|------|----------|
| `--dry-run` | Config/plan only; **no** ClickHouse |
| `--preflight-only` | Coverage classification for all symbols |
| `--run` | Backtest `ELIGIBLE_CORE_30D` only |
| `--resume` | Skip `COMPLETE` checkpoints with matching `entry_rule` |
| `--report-only` | Rebuild reports from checkpoints; no market reload |

`--run` and `--resume` are mutually exclusive. A mode flag is required.

Resume **rejects** checkpoints written under the legacy rule
`FIRST_1M_OPEN_STRICTLY_AFTER_DECISION_AT` (or missing `entry_rule`). No auto-migration.

## Coverage semantics

Preflight class `ELIGIBLE_CORE_30D` means **threshold pass**, not complete coverage
(`eligibility_means_threshold_pass_not_complete_coverage=true`).

| Source | Preflight minimum |
|--------|-------------------|
| Candles | ratio ≥ **0.95** of expected window minutes |
| Public trades | ratio ≥ **0.50** of candle minutes |
| Orderbook ob200_v3 | ratio ≥ **0.85** of expected minutes |
| Outcome 1m (proxy) | ratio ≥ **0.90** |
| Warm-up | ≥ `ema_slow + 20` signal-TF bars before `start` |

OI / liquidations are **not** eligibility gates. Window status is `FULL` / `PARTIAL` /
`MISSING`. Per candidate at `decision_at`: before feed start → `MISSING`; liq feed ok
with zero events → `VALID_EMPTY`. Local OB/trades gaps → `CORE_RESEARCH_INSUFFICIENT`.
Incomplete 6h/8h outcome path → `INCOMPLETE_OUTCOME_HORIZON` (excluded from primary PnL).

Listing: unbounded earliest candle; if unreliable → `listing_status=UNKNOWN`
(never falsely assert `listing_limited=false`).

## Entry / Exit

- `decision_at` = close of the completed signal bar
- Entry: first 1m open with `open_time >= decision_at`
  (`entry_rule = FIRST_1M_OPEN_AT_OR_AFTER_DECISION_AT`)
- Exact minute at `decision_at` is allowed; earlier minutes are not
- Signal-bar open price is never used
- Same rule for Long and Short
- Same-bar TP+SL → `SL_FIRST`
- Horizon end → `TIME_EXIT` at last 1m close
- Roundtrip costs applied once
- Funding: `FUNDING_NOT_INCLUDED_DATA_UNAVAILABLE` without a causal payment ledger

## XRP parity

On `--run` / `--resume`, XRPUSDT candidates are compared (local export CSV only) on
`candidate_id`, `decision_at`, `entry_at`, `entry_price`, `direction`, `mode_id`,
`core_research_verdict`. Mismatch → checkpoint status `FAILED_PARITY` (no silent continue).

## Research vs Production

Research labels (`CORE_RESEARCH_*`) are never renamed to Production `ALLOW`.
Missing OI/Liq stays `MISSING` and does not block core research.

## Manual run (later)

```bash
cd /home/telgenbuescher/projects/orderbook_analyse
PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --preflight-only --max-workers 1
PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --run --max-workers 1
PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --resume --max-workers 1
PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --report-only
```

No production defaults or live collectors are modified by this package.
