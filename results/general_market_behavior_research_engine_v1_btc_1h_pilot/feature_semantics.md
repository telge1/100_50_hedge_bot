# Feature semantics — mb_state_1s_v1

## Time
- `state_ts`: UTC second start
- Bucket: `[state_ts, state_ts+1s)`
- Causal cut: `event_time < state_ts + 1s`
- Past returns use only earlier `state_ts` mids

## Units
{
  "price": "USDT_per_coin",
  "qty": "coin_base_qty",
  "notional": "USDT_notional = price * coin_base_qty",
  "bps": "basis_points = 1e4 * fraction_of_mid",
  "count": "non_negative_integer",
  "age_ms": "milliseconds",
  "timestamp": "UTC DateTime64 / pandas datetime64[ns, UTC]"
}

## BPS bands
0.0-2.0, 2.0-5.0, 5.0-10.0, 10.0-25.0, 25.0-50.0 relative to mid; depths are **USDT notional** (`price * qty`).

## Large wall
Descriptive: level USDT notional >= 500000 (BTC). Not outcome-tuned.

## Attribution
Execute vs cancel not exact → removals in `mixed_or_unknown_removed_usdt` with `attribution_confidence=MIXED_OR_UNKNOWN`.

## OI
Causal asof on `source_event_time`; valid if age <= 120000 ms.
