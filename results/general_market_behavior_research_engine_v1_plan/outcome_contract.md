# Outcome Contract V1

## Horizons (event-time from decision_t)
5s, 15s, 30s, 1m, 5m, 15m, 30m.

## Labels (computed only on (t, t+H])
- mid_return_bps
- MFE_bps / MAE_bps (path from 1s mids; optional event path for short H)
- realized_vol_bps
- direction: UP / DOWN / NEUTRAL
- path_class: CONTINUATION / REVERSAL / RANGE (vs pre-t regime context — context features causal; class uses future path)
- time_to_threshold
- hit_up_before_down / hit_down_before_up

## Neutral threshold
Train-only: max(k * median(|1s return|), c * median_spread_bps, fee_buffer_bps if trade sim). Do not fit on test.

## Classes WAIT / UNCLEAR
Allowed when feature_valid=0 or pattern confidence low — **not** forced long/short every second.

## Separation
`outcomes_v1` tables/files never feed feature builder. Build order: states → episodes → outcomes → discovery.
