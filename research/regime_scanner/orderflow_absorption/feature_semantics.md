# Orderflow Absorption — Feature Semantics

## Join
- `market_candles` 5m ⋈ `research_orderflow_5m` ⋈ `research_open_interest_5m` via `load_joined_5m`
- Keys: `symbol`, `open_time = bucket_start`, `import_version = derivatives_5m_v1`
- Only `data_available = true`

## Orderflow columns used
- `buy_volume`, `sell_volume`, `total_volume`, `delta`, `delta_ratio`
- `sequence_id`, `bucket_start`
- `spread_mean` / `spread_max` present but **not** used as primary features

## Lookback
- Bars `[t - L, t)` — anchor bar `t` excluded
- L ∈ {6, 12, 24}
- Contiguous same `sequence_id`, 300s spacing through `t`

## Delta Ratio
```text
delta_ratio = sum(delta) / sum(total_volume)
```
over the lookback (fallback: `delta = buy_volume - sell_volume`).

## Flow strength
- **F1:** `|delta_ratio| >= 0.10`
- **F2:** `|delta_ratio| >= 0.05`
- **F3:** `|delta_ratio| >= causal prior 90th percentile of |bar_delta_ratio|` (288 bars, current excluded)

## Price reaction (positive flow)
- normal progress: `price_return >= +0.25%`
- weak progress: `0 < price_return < +0.10%`
- counter: `price_return <= 0`

Negative flow mirrored.

## Close location
```text
(close_end - range_low) / (range_high - range_low)
```
Weak (bearish absorb): `<= 0.50` (stronger `<= 0.35`).
Strong (bullish absorb): `>= 0.50` (stronger `>= 0.65`).

## Patterns
- **A1:** F+ & weak progress → expect down
- **A2:** F+ & (counter OR weak close) → expect down
- **A3:** F− & weak progress → expect up
- **A4:** F− & (counter OR strong close) → expect up
- **C1:** F+ & normal up progress
- **C2:** F− & normal down progress
- **C3:** all strong + flow
- **C4:** all strong − flow
- **C5:** all valid anchors

## Outcomes
- From `close[t]`, path bars `t+1 … t+H`
- Same-bar → adverse-first in side-aware metrics
- Bearish: fav=down; Bullish: fav=up

## OI
Diagnostic only (`oi_up` / `oi_down` / `oi_flat`); not a hard filter.
