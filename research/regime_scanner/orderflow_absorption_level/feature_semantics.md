# Feature Semantics — Level-Context × Orderflow Absorption V1

## Absorption (imported unchanged)

- Feature window: `[t-L, t)` with `L=24`
- Outcomes: from `entry_eligible_index + 1` (not from absorption anchor if R1/R2 later)
- Flow rule: `F1` (`delta_ratio <= -0.10` for A4; mirrored for A2)
- Patterns: `A4` bullish treatment; `A2` bearish treatment; `A1` diagnostic only
- Gap/sequence guards: contiguous `sequence_id` + 300s bars

## Level distance

```text
distance_atr = abs(close[t] - level_price) / atr_14[t-1]
max_distance_atr = 0.50
```

Buckets: touch ≤0.10; very_near ≤0.25; near ≤0.50; far >0.50; no_level

## Visibility (strict causal)

```text
confirmation_index < anchor_index
```

Same-bar confirmation is **not** visible. Future levels never assigned.

## Priority

`protected` before `external_swing`; then nearest distance. Confluence if other type within 0.25 ATR.

## Events

Consecutive same pattern × same level zone merged; cooldown 6 bars after end.
Event id: `sha1(symbol|pattern|flow|lookback|level_id|event_start_iso)[:20]`

## Confirmations

- R0: entry = event start
- R1: rejection wick + close on side
- R2: break + reclaim in 1–3 bars

## Imports

See `config.IMPORTED_ABSORPTION`, `config.IMPORTED_LEVELS`, `config.NEW_ADAPTERS`.
Existing `orderflow_absorption/` package is not modified.
