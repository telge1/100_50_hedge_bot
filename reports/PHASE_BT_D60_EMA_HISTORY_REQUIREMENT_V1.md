# BT-D60 — EMA History Requirement V1

**VERDICT:** `EMA_HISTORY_VALIDATED`
**Regime gate:** `READY_FOR_REGIME_GEGENPROBE` (no short approval in this phase)

## Separation A / B
- **A — Pool context:** ≥168h ACTIVE origins (unchanged; not re-validated here).
- **B — OHLC EMA warmup:** ClickHouse 5m load from `2026-08-04T12:00:00Z` to `2026-09-11T17:00:00Z` (reference slice 720h vs candidates [72, 168, 336, 720]).

## EMA definition (binding for D60+)
- **Primary:** TRP indicators.ema.ema: SMA of first `period` closes seeds EMA at index period-1; k=2/(period+1); ema[i]=(value[i]-ema[i-1])*k+ema[i-1]; earlier indices None. Source: trading_research_platform/indicators/ema.py (Research Charts server path).
- **BT-D59 (preliminary):** BT-D54/D59: recursive α=2/(n+1) from bar 0 with first close as seed (no SMA warmup). Source: tools/phase_bt_d59_market_regime_pool_evolution_v1/ema_research_v1.py.
- **Liquidity context:** liquidity_context_engine_v1.ema_series: identical SMA-seed semantics to TRP. Source: research_charts/liquidity_context_engine_v1.py.

## Convergence tolerance (EMA200 / EMA59)
- `max(abs_diff, rel_tol * |ref|)` with abs=1e-08, rel=1e-05
- Reference EMA at probe = TRP path over **all loaded bars** from series start through probe.

## Checks
- Bar integrity: `True` (gaps=0, dups=0)
- Warmup convergence (72/168/336h vs full): `True`
- Slope warmth (EMA59/200 @ 6/12/24b): `True`
- SSOT `price_feed_v1` ≤72h EMA200 vs CH full history: `True`

## BT-D59 status
SUPERSEDED_BY_D60_TRP_PATH

## Outputs
- `results/phase_bt_d60_ema_history_v1/ema_history_manifest.json`
- `results/phase_bt_d60_ema_history_v1/convergence_by_probe.csv`

Regime Gegenprobe executed: **340** rows → `results/phase_bt_d60_ema_history_v1/regime_gegenprobe_asof.csv`, report `reports/PHASE_BT_D60_REGIME_GEGENPROBE_V1.md`.