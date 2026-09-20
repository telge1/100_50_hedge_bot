# Feature Catalog V1 (causal, versioned) — draft

Version tag: `mb_features_v1`. Thresholds are **provisional**; freeze only after discovery period, never on test set.

## A. Book Shape (from reconstructed book ≤ t)
- Mid, microprice, spread abs/bps
- Bid/ask notional & qty in bps bands from mid: propose calibrate on BTC tick (0.1) & empirical depth mass — start candidates `0-2,2-5,5-10,10-25,25-50,50-100` bps then refit on train-only quantiles
- Imbalance per band and cumulative
- Slope / concentration (Herfindahl on top N notionals)
- Distance to nearest large wall (threshold = train quantile of level notional)
- Depth to fixed notional (e.g. $100k/$1M) in bps
- Liquidity gaps: empty/thin ticks between mid and X bps

**Measurable:** exact given book snapshot. **Proxy:** “wall” definition.

## B. Book Flow (Δ book over window ending at t)
- Added / removed notional by side & band
- Net liquidity change; quote churn; add/cancel rates
- Wall persistence, migration, pull
- Consumption / refill sequences (reuse OB Fight metrics)
- Attribution labels: EXECUTED_LIKELY / CANCEL_LIKELY / MIXED_OR_UNKNOWN via trade overlap

**Measurable:** raw Δ size. **Proxy:** attribution class.

## C. Aggressive volume / footprint
- Taker buy/sell base & quote, delta, cum delta (rolling causal windows)
- Trade count, mean/max size, size buckets, trade intensity, bursts
- Volume at price (from trades); absorption proxies (high opposite taker + small mid move)

## D. Price Response (core)
Define over window W ending at t (and separately as **outcome** after t — never mix):
- `mid_return_bps / taker_buy_quote` (and sell, and |delta|)
- `mid_return_bps / EXECUTED_LIKELY_notional`
- `mid_return_bps / removed_liquidity_notional`
- Quadrants: high vol+move, high vol+no move, low vol+move, strong book-flow+no progress

## E. OI & liquidations
- ΔOI abs/% over 5s–5m causal windows; price↑/↓ × OI↑/↓ quadrants (**interpretive, not exact long/short open**)
- Liq notional by forced side; cluster rate; squeeze-context **proxies**

## F. Regime (causal lookbacks only)
- Trend/range via returns & efficiency ratio
- Compression/expansion (realized vol ratio)
- Liquidity regime (spread + near-touch depth quantiles)
- Volume regime vs rolling baseline

## G. Data quality flags (required on every row)
coverage_status, replay_status, ages per source, gap/resync flags, carried_forward flags, feature_valid, exclude_reason
