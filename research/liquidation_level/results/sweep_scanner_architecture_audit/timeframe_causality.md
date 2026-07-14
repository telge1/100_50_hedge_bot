# Multi-Timeframe Causality Audit

## Verdict

Scanner HTF aggregation (`research/regime_scanner/timeframes.aggregate_candles`) is **causally strict**:
only complete contiguous 5m groups, `close_time <= decision_time`, no forward-fill, no backfill of partial buckets.
Indicators are **recomputed on each TF frame**, never resampled from 5m indicators.

Liquidation research uses a **parallel** HTF mapper in `short_squeeze_continuation_audit.aggregate_closed_htf_local` / `map_5m_to_latest_closed_htf` for trend tags T1–T3. Same intent (last fully closed HTF), different call site. Phase A must pin one aggregation API and verify identical bucket membership.

## Decision clock

- Regime pipeline (`pipeline_audit`): `decision_time` is the **open of the next 5m bar** after the closed candle under review (candle-open style).
- Closed bar with open `T` covering `[T, T+5m)` becomes usable at `decision_time = T+5m`.
- For liquidation sweeps: sweep facts (high/low crossed level; reclaim via close) are knowable **at sweep-bar close** (`T+5m` wall clock). Path-audit `entry_index = signal_index + 1` uses **next open** — this is a legacy measurement convention, **not** the proposed analysis entry.

## Aggregation rules (`aggregate_candles`)

| Rule | Behavior |
|------|----------|
| 5m universe | `timestamp < decision_time` |
| Complete group | Exact expected opens (no gaps) |
| 15m | 3 × 5m; close_time = open+15m ≤ decision_time |
| 30m | 6 × 5m; close_time = open+30m ≤ decision_time |
| Partial / gapped | dropped |
| ffill / bfill | none |
| Output timestamp | HTF **open** time |

## Warm-up / replay before audit window

- Per-TF indicator warm-up ≈ `ema200 + slope144 + pivot_right` (~347 TF bars).
- 5m history expansion: `required_5m_history_candles = history × max(1,3,6)` so 30m warm-up dominates.
- Pivots: confirmation lag `pivot_right` (5m=3; 15m/30m default 2) — structure labels lag closes.
- Liquidation: volume SMA needs 13 bars before ratios/creates are defined.
- Trend state machine (if used): `min_warmup_5m_bars=220` and HTF structure updates only when a **new** 15m/30m bucket closes.

## Example timeline (UTC)

Assume contiguous 5m data. Candle label = **open** time.

| Wall / decision | 5m bar just closed | Available 5m state | Available 15m | Available 30m |
|-----------------|--------------------|--------------------|---------------|---------------|
| 10:00 open = decision after 09:55 | 09:55 | indicators/structure as of 09:55 close | last 15m with close≤10:00 → e.g. 09:45–10:00 **not** closed yet; last closed often **09:30–09:45** | last 30m with close≤10:00 → e.g. **09:30–10:00** not closed; last closed **09:00–09:30** |
| 10:05 | 10:00 | 10:00 5m features | still no 10:00–10:15 | still no 10:00–10:30 |
| 10:10 | 10:05 | 10:05 5m features | still incomplete 10:00–10:15 | same |
| 10:15 | 10:10 | 10:10 5m features | **10:00–10:15** becomes available (close_time=10:15) | 10:00–10:30 still open |
| 10:30 | 10:25 | 10:25 5m features | 10:15–10:30 available | **10:00–10:30** available |

### Sweep on 5m open 10:00 (covers 10:00–10:05)

At sweep **close** (wall 10:05 / decision_time for that bar = 10:05):

- **5m**: full sweep candle OHLCV + liquidation sweep flags + any 5m indicators ending on this close.
- **15m**: still **previous** closed 15m (latest close ≤ 10:05) — typically `09:45–10:00` if that bucket completed at 10:00; the in-progress `10:00–10:15` is **not** visible.
- **30m**: still previous closed 30m (latest close ≤ 10:05) — typically `09:30–10:00` only if it closed at 10:00; else `09:00–09:30`. The in-progress `10:00–10:30` is **not** visible.

Frozen “HTF context at sweep” must use **as-of sweep decision_time**, never the HTF bucket that is still forming when the sweep wick prints.

## Lookahead risks to watch in Phase A

1. Joining HTF **indicators of the forming bucket** into the sweep row.
2. Using pivot labels before `confirmation_timestamp`.
3. Treating `trend_structure.liquidity_sweep_*` as LuxAlgo liquidation sweeps.
4. Mixing `timeframes.aggregate_candles` with liquidation local HTF aggregation without equality checks.
5. Using ValidationEvent `entry_*` as proposed research entry (it is path-audit next-open).

## Forward-fill / backfill

| Mechanism | Present? |
|-----------|----------|
| Incomplete HTF fill | No |
| Explicit asof last closed HTF onto 5m | Yes (`map_5m_to_latest_closed_htf` / decision-time aggregation) — this is **causal last-closed**, not peeking |
| Backfill missing 5m into HTF | No (gapped groups dropped) |
