# Wave-Fade NO_BE50 active strategy

## Identity

- **Active strategy_version:** `wave_fade_no_be50_v1`
- **Frozen baseline (immutable):** `wave_fade_frozen_f16ae32`

## BASE

`wave_fade_frozen_f16ae32`

## CHANGE

BE50 disabled

## UNCHANGED

- Wave-Fade detection
- Tier-A (`TREND_ALIGNED ∧ Q4`)
- Q4 efficiency edges
- Entry (T0 = first 1m open after confirmation)
- Initial TP / SL levels (`tpsl_for_tf` / `trade_levels`)
- Intrabar policy `SL_FIRST`

## Exit logic (active)

```text
Entry
↓
original TP
original SL
↓
first terminal touch (SL_FIRST)
```

No 50%-TP trigger, no SL move to entry, no BE exit.

## Outcomes

| Horizon | Meaning |
| ------- | ------- |
| `TRADE` | Exit for the signal's own `strategy_version` (BE50 if frozen, NO_BE50 if v1) |
| `TRADE_NO_BE50` | NO_BE50 path for any Tier-A signal (comparison + dashboard default) |

Historical BE50 `TRADE` rows are never overwritten by NO_BE50 evaluation.
