# Timeframe aggregation

## Analysis (read-only)

### STRATEGY_TIMEFRAMES

From validated fractal wave-fade strategy (`COVERAGE_TFS` / `SIGNAL_TFS`):

- **1m** — canonical source (`candles_1m`)
- **15m, 30m, 1h, 4h** — signal + trend context TFs

`5m` is **not** a signal TF in the frozen strategy. The aggregation core may still
support `5m` for generality/tests, but strategy consumers should use
`STRATEGY_TIMEFRAMES`.

Signals schema already prepares: `trend_15m`, `trend_30m`, `trend_1h`, `trend_4h`.

### EXISTING_AGGREGATION

`src/signal_generator/timeframes.py` already implements:

- UTC bucket flooring
- closed-only HTF emission (`as_of >= close_time`)
- exact contiguous 1m membership requirement
- OHLCV + turnover sum

### REUSABLE_COMPONENTS

- `timeframes.py` (core)
- `Candle1m` / CH `candles_1m` as SoT
- docs in `DATABASE.md`

### NEEDED_CHANGES

- Explicit `STRATEGY_TIMEFRAMES`
- `available_at` / signal-availability helpers
- Bucket inspect API (COMPLETE vs INCOMPLETE with reason)
- Adapters from `Candle1m` / dict rows
- Single-bucket aggregate for live (no full-history re-scan API)
- Storage decision: **on-demand from 1m** (no new HTF tables)

### Storage decision: A — compute from `candles_1m`

No materialised HTF ClickHouse tables. Rationale: one SoT, recovery-compatible,
identical history/live semantics, avoids dual-write drift. Live path aggregates
only the current/lookback buckets.
