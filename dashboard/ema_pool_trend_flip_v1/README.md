# EMA_POOL_TREND_FLIP_V1

Isolated research/backtest overlay. Default `/stoch-signale` strategy remains `wave_fade_no_be50_v1`.

Does not write collector/live tables. Does not place orders. Does not mutate `POOL_ORDER_PLAN_V1`.

## Pool thickness (BigBeluga, not a visual substitute)

From `pool_order_planer` pin `c6c960a`:

- **strength** of a pool = `norm_vol` of the source bar at confirmation (`created_at` = next bar). See `tests/test_bigbeluga_pools.py::test_strength_uses_norm_vol_prev_bar_and_created_at`.
- **Clusters** merge overlapping/adjacent same-side boxes. `pool_count` / overlap is cluster size.
- **Relevant / “thick” cluster** (`order_planner._is_relevant`): `pool_count >= 2` **or** `strength_max >= 2` **or** `strength_sum >= 3`.
- **Micro / too-thin box** (`_is_micro_pool`): `pool_count == 1` and height `< MICRO_HEIGHT_PCT` (0.05% of entry). Isolated micro-pools are not used as structure.
- **Thin protection pool** (this strategy): nearest **isolated** (`pool_count == 1`) **non-micro** active cluster on the protection side of entry. Thick clusters are not used as the structural SL.

## Pool bias score

For each active cluster on a side of the entry price:

```text
weight = exp(-abs(distance_from_entry_pct) / POOL_BIAS_DISTANCE_HALFLIFE_PCT)
score += strength_sum * weight * (1 + POOL_BIAS_CLUSTER_COUNT_WEIGHT * (pool_count - 1))
```

Defaults: `HALFLIFE = 1.0`, `CLUSTER_COUNT_WEIGHT = 0.25`, `MIN_RATIO = 1.0`.

Bullish context: `upper_pool_bias_score > lower_pool_bias_score * MIN_RATIO` **and** at least one active lower protection-side cluster. Bearish is mirrored.

## EMA confirmed strong cross

```text
EMA_CROSS_CONFIRMATION_BARS = 2
EMA_CROSS_MIN_SEPARATION_ATR = 0.05
```

Requires two consecutive closed signal-TF bars with EMA9 on the new side of EMA20, `|EMA9-EMA20|/ATR14 >= 0.05` on the second bar, EMA9 moving in the new direction, and close confirming. Touches / one-bar weak crosses do not flip the regime and do not exit.

## SL

Planner `SL_BUFFER = 0.002` beyond the thin protection cluster edge. Distance `> 1.5%` sets `SL_TOO_WIDE` but does not block.

- `STATIC`: freeze SL at entry.
- `RATCHET`: only move SL in the trade’s favor after a later closed 5m bar; never retroactive.

TP1/TP2 disabled.

## Batch

```bash
cd dashboard
PYTHONPATH=. python -m ema_pool_trend_flip_v1.batch --frozen-ace
```
