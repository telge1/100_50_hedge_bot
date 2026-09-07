# Footprint Candles (MVP)

Read-only research overlay for the Market Profile page.

## Supported (V1)

| Setting | Value |
|---------|-------|
| Symbol | `BTCUSDT` only |
| Candle TF | `5m` only |
| Mode | `DISPLAY` only |
| `bucket_step` | `5.0` (fixed; zoom never changes buckets) |

## API time limits

| Limit | Value | Meaning |
|-------|-------|---------|
| `MAX_RANGE_SECONDS` | **21600** (6h) | Hard cap on `to - from` |
| Closed 5m intervals in 6h | **72** | `21600 / 300` |
| `MAX_CANDLES` | **73** | 72 closed + at most 1 forming |
| Client visible buffer | **±900s** (3×5m) | Applied around visible range, then clamped to 6h |

Exact 6h (`to - from == 21600`) is allowed. Anything above 6h is rejected.

## Data sources

- **OHLC SoT:** `signal_generator.candles_1m` aggregated to 5m
- **Footprint volume:** `orderbook_analysis.public_trades_canonical` (executed public trades only)
- **Not** order-book resting liquidity

## Side semantics

Bybit / canonical `side` is the **taker aggressor**:

- `Buy` → ask volume (aggressive buy)
- `Sell` → bid volume (aggressive sell)

Both `size` and `notional` are aggregated. UI shows compact **notional**. Imbalances use **size**.

## Bucket rule

```text
bucket_index = floor(price / 5.0)   # Decimal-stable
price_low  = bucket_index * 5.0
price_high = price_low + 5.0
```

Global tick-aligned grid. Never anchored to candle low or zoom.

## DISPLAY mode

```text
candle_start <= trade_ts < candle_end
```

API field `mode` is always `DISPLAY` in V1. `CAUSAL_RESEARCH` is not exposed in the UI.

## Deduplication

Logical key `(symbol, trade_id)` via:

```sql
GROUP BY symbol, trade_id
argMax(..., ingest_timestamp)
```

No raw summing of physical duplicates; `FINAL` is not the request path.

## Coverage

`COMPLETE` | `PARTIAL` | `MISSING` | `UNKNOWN`

- `COMPLETE` only with positive completeness proof (not density). V1 live path does not claim COMPLETE without that flag.
- `PARTIAL` / `UNKNOWN`: show bid/ask/delta/vPOC with yellow “not confirmed”; suppress imbalance highlights.
- `MISSING`: no footprint levels; red “Footprint-Daten fehlen”.

## Imbalance

- Ratio `3.0`, min size `0.01` BTC, stacked ≥ 3 consecutive (ask and bid runs separate)
- Ask imbalance at `i` vs bid at `i-1`
- Bid imbalance at `i` vs ask at `i+1`
- vPOC: max total size; tie → lower `bucket_index`

## Zoom gates

| Gate | Condition | Draw |
|------|-----------|------|
| Full | barSpacing ≥ 60 and levelHeight ≥ 11 | Bid×Ask text, delta, vPOC, imbalances if COMPLETE |
| Delta | ≥ 40 bar or ≥ 3 level px | Delta tint + vPOC |
| Fallback | below that | Empty overlay + zoom hint |

## Module boundary

Logic lives under `dashboard/footprint_candles/`. Market Profile only hosts `#fpOverlay`, a toggle, and thin hooks in `app.js`.

## Later extensions

- More symbols / timeframes
- Manual `bucket_step`
- `CAUSAL_RESEARCH` mode
- ClickHouse trade heartbeats → real `COMPLETE`
- Custom series / OB overlays (clearly labeled, not footprint volume)
