# OI + Price + Orderflow Delta Pattern — Feature Semantics

## Join
- `market_candles` 5m ⋈ OI ⋈ orderflow via `load_joined_5m`
- Keys: `symbol`, `open_time = bucket_start`, `import_version = derivatives_5m_v1`
- Only `data_available = true`

## Columns used
- OHLCV: `open`, `high`, `low`, `close`, `volume`
- OI: `open_interest`
- Orderflow: `buy_volume`, `sell_volume`, `total_volume`, `delta`, `delta_ratio` (DB)
- `sequence_id`, `bucket_start`

## Lookback
- Bars `[t - L, t)` — **anchor bar t excluded from features**
- L ∈ {12, 24}
- Requires contiguous same `sequence_id` and 300s spacing through t

## Price
- `price_return = close[t-1] / open[t-L] - 1` over the past window
- States: up/down/flat at ±0.25%

## OI
- `oi_change_pct = oi_end / oi_start - 1` on past window
- States: up if >0, down if <0, flat if ==0; invalid if non-positive start

## Delta
- Prefer DB `delta`; else `buy_volume - sell_volume`
- `delta_ratio = sum(delta) / sum(total_volume)` over lookback
- States: positive/negative/neutral at ±0.05

## Outcomes
- Reference: `close[t]`
- Path: bars `t+1 … t+H`, H ∈ {3,6,12}
- Thresholds: 0.50% and 1.00%
- Same-bar up+down → conservative down-first

## Patterns
- P1: flat + oi_up + delta_positive
- P2: flat + oi_up + delta_negative
- P3: up + oi_up + delta_positive
- P4: down + oi_up + delta_negative
- P5: flat + oi_down
- P6: all valid anchors (control)
