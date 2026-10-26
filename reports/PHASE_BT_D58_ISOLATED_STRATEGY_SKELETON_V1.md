# PHASE BT-D58 — Isolated Downtrend Short Strategy Skeleton V1

**Date:** 2026-10-26  
**Repo:** `/home/telgenbuescher/projects/pools+ob+delta_bot`  
**Prior:** BT-D57A `PARITY_FIXED_STRATEGY_ARCH_READY` (`46695f29`)  
**Baseline tag unchanged:** `pre_downtrend_strategy_v1_20261026` → `d5661662`

---

## Summary

Implemented **fail-closed research skeleton** for `downtrend_isolated_upper_short_v1`: typed gates, YAML config with provenance from `configs/backtester.yaml`, causal replay runner, D46 read-only adapter, and deterministic tests. **No trading rules, no PnL, no dashboard changes, no D46 code edits.**

---

## Created files

| Path | Role |
|------|------|
| `tools/phase_downtrend_isolated_upper_short_v1/contracts_v1.py` | Shared datatypes / contract ids |
| `tools/phase_downtrend_isolated_upper_short_v1/strategy_config_v1.yaml` | Strategy + isolated metadata; BT-D6 baseline fields |
| `tools/phase_downtrend_isolated_upper_short_v1/config_loader_v1.py` | Split `isolated_strategy_v1` vs validated BT-D6 JSON |
| `tools/phase_downtrend_isolated_upper_short_v1/regime_classifier_v1.py` | Always `UNCERTAIN` + evidence |
| `tools/phase_downtrend_isolated_upper_short_v1/isolated_upper_eligibility_v1.py` | `eligible=False`, `NOT_YET_VALIDATED` |
| `tools/phase_downtrend_isolated_upper_short_v1/short_pause_state_v1.py` | Default `SHORT_PAUSED`; SL latch; no auto requalify |
| `tools/phase_downtrend_isolated_upper_short_v1/entry_gate_v1.py` | Fail-closed AND gate |
| `tools/phase_downtrend_isolated_upper_short_v1/isolated_research_runner_v1.py` | Causal bar loop; **0 intents default** |
| `tools/phase_downtrend_isolated_upper_short_v1/d46_adapter_v1.py` | `run_integrated_backtest` delegate only |
| `tools/phase_downtrend_isolated_upper_short_v1/tests/*` | 18 unit/integration tests |

---

## Interfaces and contracts

- **Regime:** `RegimeClassifierV1.regime_at_close(RegimeContextV1) -> RegimeClassificationV1`
- **Eligibility:** `evaluate_isolated_upper_eligibility(...) -> IsolatedEligibilityResultV1`
- **Pause:** `ShortPauseControllerV1` — `READY` / `SHORT_PAUSED`; `try_requalify` always false in skeleton
- **Entry gate:** requires `DOWNTREND` + eligible + `READY` + baseline signal
- **Runner:** `run_isolated_research_replay` — does **not** invoke D46 internal loop; no pending placement

**D46 integration:** `d46_adapter_v1.run_d46_baseline_reference` calls existing `run_integrated_backtest` without modification.

---

## Config provenance

| Parameter | Value | Source |
|-----------|-------|--------|
| Symbol / TF / PD / Depth2 | DOGEUSDT, 5m, PD1, min_origins=2 | `configs/backtester.yaml` |
| Entry selection | `nearest_upper_proximity_v1` | `configs/backtester.yaml` |
| Entry price | `pool_lower_edge` | `configs/backtester.yaml` |
| SL | `sl_above_upper_top_pct: 0.2` (×1.002) | `configs/backtester.yaml` |
| TP | `next_eligible_band`, `min_distance_pct: 0.5` | `configs/backtester.yaml` |
| Upper gap | `gap_min_pct: 0.5` | `configs/backtester.yaml` |
| Single position | `max_concurrent_positions: 1` | `configs/backtester.yaml` |
| Warmup | `minimum_warmup_hours: 168` | `isolated_strategy_v1` (documented) |
| EMA / stack / requalify thresholds | `UNSET` | `isolated_strategy_v1` |

Backtest window in skeleton YAML spans Sep-2–Sep-9 for **documentation**; replay tests use D7 synthetic fixture unless canonical CH is wired later.

---

## Default fail-closed evidence

- Regime skeleton → `UNCERTAIN` on all inputs
- Eligibility → `NOT_YET_VALIDATED`
- Pause → initial `SHORT_PAUSED`
- Combined gate → `allow_new_short_intent=False`
- Runner → `short_intent_count=0`, `pending_orders_created=0`, `modeled_fills=0` on synthetic replay

---

## Test results

| Suite | Result |
|-------|--------|
| `phase_downtrend_isolated_upper_short_v1/tests/` | **18 passed** |
| D8 parity + integrity | **7 passed** |
| D46 integrated tests | **4 passed** |

### Requirement mapping

| # | Requirement | Test coverage |
|---|-------------|---------------|
| 1 | Default regime UNCERTAIN | `test_regime_classifier_v1` |
| 2 | Missing data fail-closed | `test_regime_classifier_v1` |
| 3 | Eligibility false | `test_isolated_eligibility_v1` |
| 4 | SHORT_PAUSED blocks | `test_entry_gate_v1`, `test_short_pause_state_v1` |
| 5 | SL sets pause | `test_short_pause_state_v1` |
| 6 | No implicit reset | `test_short_pause_state_v1` |
| 7 | Baseline entry/SL/TP unchanged | `test_config_loader_v1` vs `backtester.yaml` |
| 8 | D46 path unchanged | `test_d46_baseline_unchanged_v1` |
| 9–10 | Sep-9 / Sep-11 fail closed | `test_reference_windows_fixtures_v1` (**fixture only**, not CH) |
| 11 | Provenance retained | `test_isolated_research_runner_v1` |
| 12 | No future data in regime | `test_causal_no_future_data_v1` |

---

## Unresolved — next phase (BT-D59+)

1. Implement validated `DOWNTREND` / `SIDEWAYS` rules (EMA + structure) without hard-coding unproven thresholds.
2. Isolated upper eligibility (stack/growth) using D50/D51 forensics.
3. Causal `SHORT_PAUSED` requalification after SL.
4. Optional: wire canonical density session for true Sep-9/11 replay (168h warmup).
5. Gate-enabled runner hook before D46 pending create (still without editing D46).

---

## VERDICT

**`ISOLATED_STRATEGY_SKELETON_PASS`**

---

*End of BT-D58 report.*
