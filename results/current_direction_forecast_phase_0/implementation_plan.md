# Implementation Plan (post Phase 0)

**Generated (UTC):** `2026-09-05T14:33:50Z`  
**Phase-0 verdict:** `CURRENT_DIRECTION_FORECAST_PHASE_0_CACHE_BRIDGE_REQUIRED`

## Phase 0.5 — Cache bridge (separate approval)

- Add RO collector socket op to dump ringbuffer snapshot + metadata (no flush, no clear, no new WS).  
- Document size (~3k msgs / ~10 MiB BTC), lock hold budget, rate limit.  
- Health field expose for CLI preflight.  
- **Least live risk** among options needing pre-roll.

## Phase 1 — CLI pilot (State Analyzer)

- `scripts/run_current_direction_forecast.py` research-only.  
- Output: `BUY_PRESSURE | SELL_PRESSURE | MIXED | INSUFFICIENT_DATA` (no direction claim).  
- Freeze prediction then observe 5/15/30/60.  
- Gate: 10 complete runs.

## Phase 2 — Frozen uncalibrated heuristic (optional)

- `UP_LEAN | DOWN_LEAN | NEUTRAL_WAIT | INSUFFICIENT_DATA`  
- `CONFIDENCE=UNCALIBRATED` only.  
- Weights frozen a priori; shadow sampler ≥200.

## Phase 3 — Forward reservation

- Freeze contracts; run ≥14d / ≥1000 scheduled forecasts untouched.

## Explicitly deferred

- ML / calibrated probabilities  
- Dashboard buttons  
- Trading / orders  
- Coupling to LIQUIDITY_DESTINATION_BIAS  
- Second Full-OB websocket from CLI
