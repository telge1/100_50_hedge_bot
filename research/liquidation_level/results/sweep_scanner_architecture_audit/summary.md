# Sweep ↔ Scanner Architecture Audit — Summary

**Date:** 2026-07-14  
**Branch:** `research/liquidation-level-backtest`  
**Scope:** Read-only architecture & data-flow audit only  
**Winner config:** `2eab613f172d928e` (upper 50x immediate reclaim; OOS confirmed vs matched control)

## Bottom line

**Ready for Phase A** (read-only event-join + timeline causality audit).  
No hard blockers that require code changes before Phase A. Remaining gaps are **join/freeze design**, not missing core feature engines.

## Scanner modules found (core path)

5m feather → `data_loader` → `timeframes.aggregate_candles` {5m,15m,30m} → `indicators.compute_indicator_frame` (per TF) → `point_audit` / `classifier` → `regime_snapshot` + `evaluate_setup_activation` → optional `price_action` → `momentum`, orchestrated by `pipeline_audit`.

Parallel research stacks (do not silently mix): `trend_structure`+`trend_state_machine`/`trend_state_policy`, `luxalgo_structure_reference`, `multilevel_market_structure`, `market_regime` (K2_H4).

## Features by TF (availability)

| Bucket | 5m | 15m | 30m |
|--------|----|-----|-----|
| Trend EMA/ADX/DI/regime | Yes | Yes (if bucket closed) | Yes (if bucket closed) |
| ATR / ATR% | Yes | Yes | Yes |
| Volume SMA/spikes | Liquidation-side yes; scanner mostly raw volume only | via agg sum | via agg sum |
| Structure HH/BOS/CHoCH/… | Yes (pivot lag) | Yes | Yes |
| PA + momentum SMs | Yes (5m only) | No | No |
| LuxAlgo liq sweep | Yes (trigger) | n/a | n/a |
| Bollinger / 1m | Missing | Missing | Missing |

## Causality assessment

**Sound** for scanner aggregation: complete buckets only; no ffill/bfill; indicators recomputed per TF.  
At sweep close, HTF = **last closed** 15m/30m only. Forming buckets are invisible. Pivot confirmation lags. Decision clock is candle-open after close.

## Sweep interface

Producer chain: `replay_liquidation_levels` → lite/upper builders → `build_winner_events`.  
Immediate reclaim = sweep close already below upper level. Proposed `SweepTriggerEvent` drops entry fields and freezes `source_config_id`.

## Proposed analysis SM

`IDLE → SWEEP_DETECTED → ANALYSIS_ACTIVE → {SHORT_CONFIRMED | LONG_CONTINUATION_CONFIRMED | INVALIDATED | EXPIRED}`  
Windows 3/6/12 closed 5m candles after sweep — variants only, no pick. Sweep ≠ entry.

## Main gaps

1. No sweep analysis SM yet  
2. Level-anchored post-sweep classification not standardized  
3. Semantic collision: structure `liquidity_sweep_*` vs liquidation sweep  
4. Dual HTF aggregators need equality pin  
5. Volume feature asymmetry liquidation vs scanner  
6. No 1m data  
7. Existing `entry_*` on winner events are path-audit only  

## Implementation order

Phase A join/timeline → B window → C snapshots → D reverse/breakout → E momentum bridge → F next-open entry audit → G economics → H possible scanner integration.

## Artifacts in this folder

- `module_inventory.csv`
- `feature_inventory.csv`
- `timeframe_causality.md`
- `sweep_event_interface.md`
- `proposed_state_machine.md`
- `data_gaps.md`
- `implementation_plan.md`
- `summary.md`
