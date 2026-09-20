# CLI Architecture Plan — run_current_direction_forecast.py

**Generated (UTC):** `2026-09-05T14:33:50Z`  
**Implement:** NOT in Phase 0

## Planned invocation

```bash
python scripts/run_current_direction_forecast.py \
  --symbol BTCUSDT \
  --now \
  --horizons 5,15,30,60 \
  --primary-horizon 15 \
  --require-full-ob \
  --observe-outcome \
  --output-dir results/current_direction_forecast_runs/
```

## Hard sequence

1. Resolve `T0` (`--now` wall clock or `--timestamp`)  
2. Close data cutoff (`feature_cutoff_utc = T0`)  
3. Load Full-OB pre-roll (**requires bridge — blocked today**) + live book + trades + optional context  
4. Compute features → hash  
5. Emit prediction → write **immutable** prediction artifact  
6. Only then start outcome observer  
7. Write outcomes at 5/15/30/60 without mutating prediction  

## Status codes

```text
DATA_COMPLETE
FULL_OB_CACHE_INCOMPLETE
FULL_OB_STALE
FULL_OB_SEQUENCE_GAP
PUBLIC_TRADES_STALE
OI_NOT_AVAILABLE
OUTCOME_PENDING
OUTCOME_COMPLETE
INSUFFICIENT_DATA
```

## Failure / edge behavior

| Situation | Behavior |
|-----------|----------|
| Pre-roll < required seconds | `FULL_OB_CACHE_INCOMPLETE` / abort if `--require-full-ob` |
| Reconnect cleared buffer | same; do not invent history |
| Sequence gap open | `FULL_OB_SEQUENCE_GAP`; fail-closed |
| OI missing | `OI_NOT_AVAILABLE`; continue if optional |
| Liq missing | continue (optional) |
| Public trades stale | `PUBLIC_TRADES_STALE`; abort path observer / mark incomplete |
| CLI abort before 60m | prediction remains; outcomes partial; resume by `forecast_id` |
| Restart observer | load immutable prediction; append missing horizons only |
| Existing `forecast_id` | refuse overwrite of prediction; allow outcome fill-only |
| Parallel forecasts | unique ids; no shared mutable feature store |

## Immutable prediction schema

```text
forecast_id
symbol
t0_utc
feature_cutoff_utc
feature_contract_version
feature_hash
forecast_contract_version
prediction
confidence_status   # UNCALIBRATED | INSUFFICIENT_DATA | …
created_at
immutable_prediction_payload
```

## Separate outcome schema

```text
forecast_id
horizon_minutes
outcome_available_at
future_price
return_bps
realized_high_bps
realized_low_bps
mfe_bps
mae_bps
outcome_status
```

## Lookahead / mutation guards

- Write prediction file with `O_EXCL` / exists-check; never rewrite.  
- Feature hash over canonical JSON of T0 inputs only.  
- Observer process reads prediction; cannot open feature recompute path.  
- Trade SoT query for features uses `event_time < T0`; outcome uses `T0 <= event_time < T0+H`.  
- OI: reject any bucket with `bucket_end > T0` or not yet received at create time.
