# current_direction_outcome_contract_v1_draft

**Status:** DRAFT  
**Generated (UTC):** `2026-09-05T14:32:25Z`

## Horizons

| Horizon | Role |
|---------|------|
| 5 min | secondary |
| **15 min** | **primary** |
| 30 min | secondary |
| 60 min | secondary / completeness |

## Per-horizon stored fields

```text
forecast_id
horizon_minutes
reference_price_t0
future_reference_price
return_bps
max_favorable_excursion_bps
max_adverse_excursion_bps
realized_high_bps
realized_low_bps
data_complete
outcome_available_at
outcome_status
```

## Recommended price source (V1)

**Primary:** **Last public trade price** (event-time), same SoT for T0 and future.

Rationale:

- Aligns with observable execution prints.
- Mid from Full OB is useful as **diagnostic parallel**, but Full-OB mid can move without trades.
- Mark/Index are exchange-derived and can diverge from trade path; keep optional diagnostics only.

**T0 reference:** last trade with `event_time < T0` (strict), mirroring Phase-2B trade bucket rule.  
**Future reference at horizon H:** last trade with `event_time < T0+H` (or ≤ boundary — freeze one rule in Phase 1).  
**Excursions:** high/low of trade prices (or 1s trade buckets) on `[T0, T0+H)`.

## Continuous vs class

```text
continuous outcome = return_bps
direction class = UP / DOWN / NEUTRAL
```

## Neutral rule candidates (pre-outcome; pick one before any evaluation)

1. Sign of return only (NEUTRAL iff return_bps == 0) — brittle.  
2. Fixed bps deadband (e.g. ±X bps) — simple but arbitrary.  
3. **Recommended V1:** deadband = `k * realized_vol_bps` computed **only from data ≤ T0** (e.g. 60s or 300s closed trade returns), with `k` frozen a priori (suggest k=0.5 or 1.0) and floor/ceil caps frozen a priori.

Do **not** tune deadband on observed forward outcomes in this research stream.

## Separation from prediction

Outcomes written only after horizon elapses; never mutate `immutable_prediction_payload`.
