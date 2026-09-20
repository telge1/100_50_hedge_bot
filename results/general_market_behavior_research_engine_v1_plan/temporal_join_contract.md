# Temporal Join Contract — V1

## Canonical clock
- **Decision clock = Event-Time (exchange/event timestamps), UTC.**
- Receive-Time is quality metadata (lag, late arrival), not the primary join key.
- Store both on every joined row when available.

## Base grid (recommended)
- **Primary continuous state grid: 1 second** (`bucket_time` = floor(event_time)).
- **Optional microstructure overlays:** event-exact book changes and 100 ms trade buckets for episode detectors — not as default CH row grain for all levels.
- Rationale: 6-core practicality; aligns with existing `research_*_1s` / OB1000 materializer; Full-OB event stream remains RAW for on-demand zoom.

## Join rules by source
| Source | Align to 1s | Forward-fill allowed? | Max age at decision_t | Missing behavior |
|--------|-------------|-----------------------|-----------------------|------------------|
| Full-OB / OB200 mid & shape | last state with event_ts ≤ t | within second only (hold last applied book) | book must be ready; else invalid | `book_valid=0`; exclude FULL claims |
| Public trades | sum/count in (t-1s,t] | **No** for volumes | n/a (empty window = zeros) | zeros + `trades_present` |
| Candles 1m | last **closed** candle with close_time ≤ t | closed only | 60s+ε | `candle_age_s`; do not use open candle |
| OI 5s | last observation ≤ t | **Yes** | 30s soft / 120s hard | if age>hard: `oi_valid=0` |
| Liquidations | sum in window | **No** | n/a | zeros |

## Forbidden
- Using a future OI/trade/candle to fill a past second.
- Stitching across Full-OB `gap_marker` / GAP segment as contiguous FULL.
- Random shuffle of adjacent seconds for ML splits.

## Reconnect / resync
- New checkpoint after gap starts new `book_epoch_id`.
- Features that need continuous flow reset at epoch boundaries (mark `resync_flag=1`).

## Dedup
- Trades: `symbol+trade_id` (ReplacingMergeTree).
- Book: ignore DUP/STALE `u` via existing DeltaOutcome.
- Liq/OI: existing `event_key`.

## Tolerances
- Trade↔book attribution window: **±50 ms** event-time (configurable; document in run).
- OI vs book: OI may lag; never invent OI path inside the second beyond last known.

## Multi-symbol
BTC and DOGE joined independently on UTC second; cross-market features are secondary and must not invent synchronized FULL coverage where one symbol is GAP.
