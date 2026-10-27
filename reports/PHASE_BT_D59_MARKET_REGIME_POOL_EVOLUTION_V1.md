# PHASE BT-D59 — Market Regime and Upper Pool Evolution V1

Generated: 2026-10-27T08:03:54Z

## Provenance
- EMA: EMA(9/20/59/200): standard recursive α=2/(n+1); first observed close in loaded price series seeds each EMA (same as BT-D54). Not TradingView seed.
- OHLC: OHLC via price_feed_v1 → pd1_landscape_viewer historical_window (≤72h export cap); bars before vis_from are unavailable — 168h warmup may be partial.
- Density canonical: ResearchDensityRunV1.collect_at_close / historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1
- Density cache: ValidatedDensityCacheSessionV1 / BT-D9 bundle (Sep-11–12 only)

## Row counts
```json
{
  "asof_regime_features": 76,
  "asof_upper_pool_evolution": 76,
  "reference_window_comparison": 2,
  "regime_variant_comparison": 76,
  "sl_requalification_research": 5
}
```

## Limitations
- OHLC export capped at ~72h before window end — full **168h EMA warmup not achieved** (Sep-9 EMA uses ~72h ending 2026-09-09T22:00Z)
- Sep-9 density: **BT-D54 canonical artifacts** (not live re-collect in this phase)
- Sep-11: **D9 validated_cache** only (2026-09-11T12:00Z – 2026-09-12T11:55Z)
- Sep-4/5: **30m stride** (`bar_stride=12`) + live canonical collect — bounded sample, not full 24h resolution
- D54 snapshots use `upper_bands_above_close` (nearest-upper research view, not full ladder export)

## User anchor checks (hypothesis)
- 2026-09-09T16:30:00Z: close 0.08924 vs hypothesis 0.08924; upper [0.089324, 0.089358]
- 2026-09-09T20:55:00Z: close 0.08698 vs hypothesis 0.08698; upper [0.08703, 0.087052]

## Findings (summary)

### 1. Active downtrend (research signals, not production gates)
At Sep-9 anchor bars (user hypothesis), with partial EMA warmup:
- `close_below_ema200=True`, `ema9_lt_20_lt_59_lt_200=True`
- Negative `net_move_24b_pct` (~−1.15% / −1.62%)
- Variants **A–D** true at 16:30 and 20:55

### 2. Sideways / chop
Sep-11 **12:25** shows `chop_ratio_24b≈15` with flat `net_move_24b_pct` — sideways compression vs Sep-9 directional bars.
Sep-4/5 strided sample: insufficient EMA history on many bars (`chop_ratio` often null) — see CSV.

### 3. Upper pool growth / stack (hypothesis under test)
| Bar | `local_stack_density_0p35pct` | `research_stack_growth_warning` | Variant E |
|-----|------------------------------|--------------------------------|-----------|
| 2026-09-09T16:30Z | 1 | false | **true** (gap ~1.17%) |
| 2026-09-09T20:55Z | 1 | false | **true** (gap ~1.73%) |
| 2026-09-11T12:25Z | **7** | **true** | false |
| 2026-09-11T12:35Z | elevated | **true** | false |

**Hypothesis:** rising local stack density + chop may warn of trend deterioration — **supported as research pattern**, not validated rule.

### 4. Sep-9 desired examples (user hypothesis)
User anchors match canonical D54-derived bands at 16:30 and 20:55. Research variants **E/F true** at anchors; would **not** auto-block on stack metric.

### 5. Sep-11 dense case (user hypothesis)
12:25–12:35: stack warning true, variant **E false** (isolation/gap semantics fail vs Sep-9). Aligns with “problematic dense upper” narrative — **research only**.

### 6. SL / SHORT_PAUSED requalification
Five Sep-11 SL episodes in `sl_requalification_research.csv`: regime before/at/after SL varies; **no single requalification rule** extracted. SL = re-evaluation trigger only.

### 7. Proposed measurable V1 gates (next implementation phase)
- Composite **research gate candidate:** variant D + E + not `research_stack_growth_warning` (thresholds UNSET)
- Require **full 168h OHLC** path before promoting EMA stack to classifier
- Keep skeleton classifier **UNCERTAIN** until validated on held-out windows

### 8. Next step
BT-D60: implement regime/eligibility **feature ingestion** from `asof_*.csv` contracts; still fail-closed in production until user sign-off.

## VERDICT
MARKET_REGIME_RESEARCH_PASS (bounded windows; limitations documented)
