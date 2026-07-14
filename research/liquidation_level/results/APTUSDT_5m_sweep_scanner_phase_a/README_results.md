# Phase A Results — Sweep ↔ Scanner Join

## What Phase A does

Phase A reproduces the frozen winner liquidation sweeps and freezes the scanner
5m / 15m / 30m state that is causally available at each sweep candle **close**.

## Sweep is not an entry

The validated upper 50x immediate-reclaim event only opens an analysis context.
No entry, TP, SL, or PnL is computed here.

## What was frozen

- Same closed 5m sweep candle features (EMAs, ADX/DI, ATR, regime label, structure)
- Last fully closed 15m bucket as-of decision_time = signal_timestamp + 5m
- Last fully closed 30m bucket with the same rule
- Liquidation volume_ratio on the sweep 5m bar (SMA13)

## Why 15m/30m can be older than the sweep

Higher-timeframe candles are only visible after their bucket close.
If a sweep closes while a 15m/30m bucket is still forming, Phase A uses the previous
closed bucket. That age is expected and not a join failure.

## Why forming HTF candles are excluded

Using a still-open bucket would be lookahead. Aggregation requires a complete contiguous
set of 5m bars and `close_time <= decision_time`.

## Missing / unavailable features

- Full trend state machine timeline is not forced on; Phase A exposes a structure-bias proxy only
- Price-action and momentum state machines are not armed by the sweep → marked unavailable
- Bollinger bands and 1m data remain absent

## Stale flags

`stale_15m` / `stale_30m` use diagnostic thresholds of 15 / 30
minutes. They are informational only.

## Event counts

Expected: {'full': 2696, 'in_sample': 1824, 'out_of_sample': 872}
Reproduced: {'full': 2696, 'in_sample': 1824, 'out_of_sample': 872}

## Phase B readiness

phase_b_ready = **True**

Deterministic hash: `89c3abfa95e8b640ab6354dcf5d5fb514a0ba572a271982d17f5a362377d7995`

