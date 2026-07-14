# Data Coverage and Gaps

## Already available

| Need | Source |
|------|--------|
| Causal LuxAlgo-style levels + upper sweeps | `replay_liquidation_levels` |
| Immediate reclaim + 50x winner events | `build_winner_events` / `_find_bearish_reclaim` |
| Sweep geometry (body/wicks/close location) | `liquidation_features.candle_geometry` / ShortSqueezeEvent |
| Cluster center / strength aggregations | ValidationEvent + cluster builders |
| Per-TF EMA/ADX/DI/ATR suite | `indicators.compute_indicator_frame` on 5/15/30 |
| Regime labels + setup snapshot | `classifier` + `regime_snapshot` |
| HH/HL/LH/LL + BOS/CHoCH/failed break/retest/protective | `swings` + `structure` + `trend_structure` |
| 5m PA + momentum confirmation SMs | `price_action` + `momentum` |
| Causal HTF aggregation | `timeframes.aggregate_candles` |
| Pipeline warm-up / decision_time discipline | `pipeline_audit` + `point_audit` |

## Must be joined / adapted (exist but not connected)

1. Winner `SweepTriggerEvent` ↔ `point_audit` / regime snapshot **as-of sweep decision_time**
2. Frozen HTF context row + rolling post-sweep 5m feature timeline (3/6/12)
3. Explicit analysis SM separate from setup→PA→momentum live path
4. Map structure/PA events **relative to sweep level / cluster center** (level-anchored semantics)
5. Decide single HTF aggregators for liquidation vs scanner equality checks
6. Relabel / isolate `trend_structure.liquidity_sweep_*` to avoid semantic collision

## Completely missing

| Gap | Impact |
|-----|--------|
| Dedicated sweep-analysis state machine | Must be new research module |
| Level-anchored acceptance/rejection feature set (beyond reclaim class) | Needed for reverse vs breakout |
| Bollinger / classic band-width suite | Not in indicators; use ATR%/EMA bands proxies or add later |
| Regime-scanner volume SMA / spike suite | Only raw volume in indicators; volume logic lives in liquidation |
| Unified “expansion candle” feature | Proxy via range/ATR% |
| 1m OHLCV and 1m microstructure | Entire 1m layer absent |
| Analysis-window expiry semantics (3/6/12) tied to sweep | Momentum’s 0..3 is PA-relative, different object |
| Non-overlapping trade / fee TP-SL layer for **this** trigger | Exists in liquidation backtests but not for SM entries |

## Existing features that are unsuitable as-is

| Feature | Why |
|---------|-----|
| ValidationEvent / ShortSqueezeEvent `entry_*` | Path-audit next-open after reclaim — **not** confirmation entry |
| `generate_signals` liquidation SignalEvent | Historical signal experiment; conflicts with “sweep ≠ entry” |
| `trend_structure.liquidity_sweep_*` | Different meaning than LuxAlgo liquidation sweep |
| `momentum` confirmation alone | Requires prior PA confirm from setup activation path |
| `evaluate_setup_activation` alone | Thin HTF alignment; explicitly not entry |
| K2_H4 / lux / multilevel dialects mixed without dialect flag | Risk of inconsistent votes |

## Semantic mismatches: liquidation_level vs regime_scanner

| Topic | Liquidation | Regime scanner |
|-------|-------------|----------------|
| Sweep | LuxAlgo estimated liq levels cross OHLC | Structure `liquidity_sweep_*` around pivots |
| HTF | Local aggregate helpers in squeeze audit | `timeframes.aggregate_candles` + point_audit |
| Volume | Core to level creation (SMA13, threshold) | Mostly ignored beyond OHLCV pass-through |
| Reclaim | Close relative to **liq level** | PA confirmation vs **swing levels** |
| Primary TF clock | 5m sweep close | 5m decision_time (next open) |
| LuxAlgo naming | Liquidation levels replication | Separate Lux structure engine |

## Too late at sweep close

- Forming 15m/30m bucket indicators
- Unconfirmed pivots (`confirmation_timestamp` in future)
- Post-sweep 5m reaction candles (by definition later)
- Momentum confirmation (needs later bars + PA arm)

## Warm-up risk

| Layer | Risk |
|-------|------|
| EMA200 / slope144 on 30m | Needs long history; early series regimes unreliable |
| Pivot R lag | Structure “false quiet” near start |
| Liquidation vol SMA13 | First 12 bars no volume-created levels |
| Trend SM 220 5m | If used, shorter windows incomplete |

## 1m still missing

No 1m loader, aggregation, structure, or entry timing in either stack. All planned phases remain **5m (and HTF from 5m)** until a separate 1m ingestion project exists.
