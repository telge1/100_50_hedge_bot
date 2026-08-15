# Feature & Join Semantics — Liquidation Exhaustion Reversal v1

## Join

```text
regime_scanner_research.market_candles
  exchange = bybit
  timeframe = 5m
  open_time (UTC)

INNER JOIN

research_open_interest_5m ⋈ research_liquidations_5m ⋈ research_orderflow_5m
  ON (symbol, bucket_start, import_version)

JOIN KEYS:
  symbol
  market_candles.open_time = derivatives.bucket_start
  import_version = derivatives_5m_v1
```

Filters:

- `data_available = true` only
- exact one row per `(symbol, bucket_start, import_version)` (dedupe keep last)
- no forward-fill of missing derivative buckets
- unavailable symbols ENA/ARB/OP rejected at CLI

## Gaps / sequences

- OI change / rolling windows reset when:
  - `sequence_id` changes
  - bucket spacing ≠ 300 seconds
- Known outage `2026-03-25 18:13`–`2026-03-27 16:46` already absent from imported data
- No events spanning sequence boundaries (reclaim/fill require same sequence + contiguous 5m)

## OI change

Recomputed from `open_interest` levels:

- `oi_chg_Nm = OI_t - OI_{t-N}` only if all bars in range share `sequence_id` and are finite
- Never import legacy source `oi_change`
- No forward-fill

## Burst thresholds

Causal lookback 288 **prior** valid buckets (current bar excluded from percentile/MAD/median buffer).

## Reclaim / fill

- Reclaim confirmed on **close** of a bar **after** burst anchor
- Hypothetical fill at **next 5m open** after reclaim close
- No same-candle fill

## Outcomes

- MFE/MAE via shared `path_arrays`
- First-touch via `first_touch_level`
- Same-bar TP+SL → adverse-first conservative
