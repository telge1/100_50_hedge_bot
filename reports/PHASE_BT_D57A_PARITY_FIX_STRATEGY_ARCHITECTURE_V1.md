# PHASE BT-D57A — D8 Parity Test Repair + Strategy V1 Architecture V1

**Date:** 2026-10-26  
**Repo A:** `/home/telgenbuescher/projects/pools+ob+delta_bot`  
**Repo B:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev`  
**Prior:** BT-D57 `D8_PARITY_ROOT_CAUSE_CONFIRMED`  
**Baseline tags unchanged:** `pre_downtrend_strategy_v1_20261026` → A `d5661662`, B `94c596fe`

---

## PART A — D8 test correction

### Root cause (BT-D57 recap)

Parity failed on **full `signals` dict equality** while **bands, orders, trades, and PnL were already identical**. Causes: (1) raw vs enriched snapshot metadata; (2) `cfg_cache` mutating `config_content_hash` / `signal_id`.

### Exact fix

File: `tools/phase_bt_d8_canonical_density_cache_v1/tests/test_bt_d7_fixture_cache_parity_v1.py`

1. Build cache from **`_enrich_snapshot`** payloads (unchanged).
2. Run **both** backtests with the **same** `cfg` from `synthetic_fixture_v1.json`.
3. **Fixture arm:** `FixtureDensitySessionV1(enriched_by_close)` — not raw `fix["snapshots"]`.
4. **Cache arm:** `ValidatedDensityCacheSessionV1(bundle_dir, strategy_config=cfg)` — session injection only; **no** `density_source` / `validated_cache` keys added to the hashed strategy document.
5. Retain full assertions on `signals`, `orders`, `trades`, `pnl` (including deterministic `signal_id`).

No production density, strategy, or golden fixture changes.

---

## PART A — Test outcomes

| Suite | Location | Result |
|-------|----------|--------|
| D8 parity | `test_bt_d7_fixture_cache_parity_v1.py` | **1 passed** |
| D8 integrity | `test_cache_integrity_v1.py` | **6 passed** |
| D6 foundation | `phase_bt_d6_.../tests/` | **28 passed** |
| D7 integration | `phase_bt_d7_.../tests/` | **26 passed** |
| D46 integrated | `phase_bt_d46_.../tests/` | **4 passed** |
| D52 true price level SL | `phase_bt_d52_.../tests/` | **4 passed** |
| D53 touch connected SL | `phase_bt_d53_.../tests/` | **4 passed** |
| BT-D48/D49 (Repo B) | `test_bt_d48*`, `test_bt_d49*` | **14 passed** |

**Skipped / not run:** `test_live_parity_optional_v1.py` (requires `RUN_BT_D8_LIVE=1`).

**Note:** Running D6 and D7 test trees in one pytest invocation can hit `test_regression_v1.py` import name collision; suites were executed **per module** (standard for this repo).

**Repo A subtotal (this phase):** 73 passed  
**Repo B subtotal:** 14 passed

---

## PART A — Changed files

| File | Change |
|------|--------|
| `tools/phase_bt_d8_canonical_density_cache_v1/tests/test_bt_d7_fixture_cache_parity_v1.py` | Parity harness aligned per BT-D57 |
| `reports/PHASE_BT_D57A_PARITY_FIX_STRATEGY_ARCHITECTURE_V1.md` | This document |
| `reports/PHASE_BT_D57_D8_CACHE_PARITY_ROOT_CAUSE_V1.md` | Uncommitted from BT-D57 (reference only) |

---

## PART B — Architecture: `downtrend_isolated_upper_short_v1`

**Immutable baseline:** `tools/phase_bt_d46_integrated_short_backtester_v1/` + D47 SSOT artifacts (not edited).

**Strategy intent:** Documented in `New_downtrend_logik.md` — SHORT only in confirmed downtrend, isolated upper entry pool, gap/stack checks, existing entry/SL/TP contracts, `SHORT_PAUSED` after SL with causal requalification (no fixed timer).

### Reusable components (existing)

| Capability | Primary modules | Notes |
|------------|-----------------|-------|
| **1. Canonical 5m PD1 Depth2 exact origin density** | `phase_bt_d4_enforced_depth2_backtester_v1/research_backtester_v1.py`, `research_density_gateway_v1.py`; live: Repo B PD1 viewer / `density_bridge_v1.ResearchDensitySessionV1` | SSOT path; do not fork math |
| **2. ≥168h warmup / older ACTIVE origins** | D50/D54 forensics windows; D23 `setup_scanner_v1` + `price_feed_v1`; backtest `start_utc` must precede trade window | Warmup is **window config**, not a separate engine |
| **3. Causal 5m decision-close** | D41 `next_5m_open_after_close`, D45 `execution_contract_v1`, D46 integrated loop | Activation **after** close |
| **4. Nearest eligible upper pool** | D7 `pool_first_signal_v1.detect_short_signal_pool_first`; D26 `dynamic_selector_v1` (lowest eligible edge); config `nearest_upper_proximity_v1` vs pool-first | V1 should **pin** pilot YAML mode explicitly |
| **5. Upper gap / stack research** | D23 `measure_upper_gap_pct`, `gap_detection_v1`; D51 composite zone prototype; D50 manual zone forensics | Stack = research; no production thresholds yet |
| **6–7. EMA 9/20/59/200, slope, sideways** | D54 `run_forensics.py` (inline EMA); Repo B `ema_pool_trend_flip_v1/ema_regime.py` (EMA9/20 + ATR); `ema_signale_v2_api.py` | **No** unified 4-EMA regime module in Repo A; D54 is research script |
| **8. Entry / SL / TP** | `entry_price_v1`, `phase4d.../strategy_rules.short_sl`, `select_short_tp_depth2` | SL = upper top × (1 + `sl_above_upper_top_pct`/100) → **×1.002 at 0.2%** |
| **9. Min TP distance 0.5%** | `take_profit.min_distance_pct` in BT-D6 YAML schema | Already in pilot configs |
| **10. Pending CANCEL/REPLACE** | D41 `pending_lifecycle_v1.review_pending_at_close`; wired in D46 | Reuse verbatim for V1 runner |
| **11. Single pending / single position** | D7 `max_concurrent_positions`; D46 state machine | Enforce in V1 state |
| **12. Modeled execution / exit chronology** | D45 `model_entry_decision`; D42 fill chronology; D7 `sell_limit_fill_v1`, `execution_v1` | D46 = chronological no-PnL; D7 = PnL research |
| **13. SHORT_PAUSED after SL** | **Not implemented** in D46/D7 | Spec only in `New_downtrend_logik.md` §7–8 |

**Explicit non-reuse for default SL:** BT-D52/D53 composite-wide / touch-connected components — **research only**; must not override local upper-pool `short_sl()` without explicit opt-in flag.

### Market states (planned)

| State | Authorizes new short intent? |
|-------|------------------------------|
| `DOWNTREND` | Yes (subject to pool rules) |
| `SIDEWAYS` | No |
| `UNCERTAIN` | No |
| `SHORT_PAUSED` | No (until causal requalification) |

`EMA9 < EMA20 < EMA59 < EMA200` remains a **research candidate** (D54 documents; not hard-coded).

### Layering (reuse over rewrite)

```text
┌─────────────────────────────────────────────────────────┐
│ downtrend_isolated_upper_short_v1 (NEW — not created)   │
│  regime_gate_v1  pool_eligibility_v1  pause_state_v1    │
└────────────┬────────────────────────────────────────────┘
             │ calls
┌────────────▼────────────────────────────────────────────┐
│ D46 integrated_backtester_v1 (frozen baseline)          │
│  OR thin wrapper: run_isolated_v1_research.py           │
└────────────┬────────────────────────────────────────────┘
             │ uses
┌────────────▼────────────────────────────────────────────┐
│ D41 pending │ D26/D7 signals │ D4 density session      │
│ D45 execution contract │ phase4d strategy_rules        │
└─────────────────────────────────────────────────────────┘
```

---

## PART C — Proposed file plan (create in next implementation phase only)

**Root:** `tools/phase_downtrend_isolated_upper_short_v1/` (name aligned with strategy id)

| File | Responsibility |
|------|----------------|
| `configs/downtrend_isolated_upper_short_v1.yaml` | Standalone strategy YAML extending BT-D6 schema: density locks, entry mode, risk/TP, **regime section (flags only, no magic thresholds)** |
| `regime_classifier_v1.py` | Interface: `classify_regime(bar_utc, ohlc_series, ema_features) -> DOWNTREND\|SIDEWAYS\|UNCERTAIN`; pluggable rules; default **UNCERTAIN** until research pins rules |
| `isolated_upper_eligibility_v1.py` | Wraps `detect_short_signal_pool_first` + gap/stack **research hooks** (D23/D51 helpers); returns eligibility + reasons |
| `short_pause_state_v1.py` | `SHORT_PAUSED` latch on modeled SL exit; `evaluate_requalification(...)` stub returning bool + audit trail (no timer) |
| `isolated_research_runner_v1.py` | Orchestrates: density session → regime → eligibility → **delegate** to D46 `run_integrated_backtest` path with injected gates (no copy of chronology loop) |
| `tests/test_regime_classifier_v1.py` | Deterministic fixtures from D54 reference bars (no live CH) |
| `tests/test_isolated_eligibility_v1.py` | Sep-9 positive / Sep-11 dense-zone negative fixtures |
| `tests/test_short_pause_state_v1.py` | SL → pause → no new intent until requalify mock |
| `tests/test_d46_baseline_unchanged_v1.py` | Assert D46 outputs unchanged when gates always pass |

**Interfaces (sketch):**

```python
class RegimeClassifierV1(Protocol):
    def regime_at_close(self, decision_close_utc: str, context: RegimeContextV1) -> str: ...

class PoolEligibilityV1(Protocol):
    def evaluate(self, decision_close_utc: str, snapshot: dict, candle: dict, config: dict) -> EligibilityResultV1: ...

class ShortPauseControllerV1(Protocol):
    def on_stop_loss_exit(self, exit_utc: str) -> None: ...
    def blocks_new_short_intent(self, decision_close_utc: str) -> bool: ...
    def try_requalify(self, decision_close_utc: str, context: RegimeContextV1) -> bool: ...
```

**Repo B:** No new strategy files; optional future read-only overlay flag in chart (out of scope until V1 runner exists).

---

## PART D — Safety compliance

- Live `:3000` dashboard not restarted  
- Research chart / density SSOT untouched  
- No PnL simulation added in this phase  
- No live orders  
- LMV2 Short V2 not modified  
- Baseline git tags not moved  

---

## PART E — Unresolved requirements (for next phase)

1. **Regime:** Which EMA slopes + structure features promote DOWNTREND vs SIDEWAYS vs UNCERTAIN (D54 data, not closed).
2. **Isolated pool vs stack:** Operational definition beyond 0.5% gap (D51 composite, D50 zones).
3. **SHORT_PAUSED requalification:** Causal rule set after SL (explicitly **no** candle-count ban).
4. **Selection mode:** Confirm pilot uses pool-first vs `nearest_upper_proximity_v1` for V1 YAML.
5. **EMA59/200:** Compute path for offline runner (reuse D54 helper vs port `ema_regime` patterns).
6. **Validated cache:** Optional for long windows; parity test now certifies **transport** on synthetic fixture only.

---

## Recommendation — next phase

**BT-D58 (proposed):** Implement `phase_downtrend_isolated_upper_short_v1` **skeleton only** — YAML + regime/eligibility/pause modules with **UNCERTAIN-default regime** (no trades) + tests; wire runner to call existing D46 with gates disabled by default; run Sep-9 / Sep-11 replay **read-only** against D47 SSOT without mutating D46 results.

---

## VERDICT

**`PARITY_FIXED_STRATEGY_ARCH_READY`**

D8 parity test repaired; regression suites green; architecture and file plan documented; **no** strategy implementation in this phase.

---

*End of BT-D57A report.*
