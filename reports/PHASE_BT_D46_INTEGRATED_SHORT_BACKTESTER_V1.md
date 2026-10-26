# PHASE BT-D46 — Integrated Causal Short Backtester V1

**Project:** `pools+ob+delta_bot`  
**Baselines:** BT-D40 … BT-D45 **PASS**  
**Pilot:** DOGEUSDT Sep-11–12 2026 (24h)  
**Date:** 2026-10-26  

---

## VERDICT

**`INTEGRATED_BACKTESTER_PASS`**

Single chronological engine: landscape → nearest upper → pending lifecycle → **Model 1** simulated fill (`SIMULATED_TOUCH_FILL_NOT_EXCHANGE_CONFIRMED`) → frozen SL/TP position → rescan after close. **No PnL**, no live orders, no dashboard/density/strategy changes.

---

## 1. Execution (A)

| Item | Value |
|------|--------|
| Primary model | `model_1_touch_hypothetical_active_v1` (BT-D45) |
| Fill label | **`SIMULATED_TOUCH_FILL_NOT_EXCHANGE_CONFIRMED`** |
| Model 2 | Documented in `comparison_bt_d45.md` only (not merged into optimistic path) |

---

## 2. Engine order per 5m close (B–D)

1. Density snapshot + pool age (causal at close **T**).  
2. If **simulated position** open → evaluate SL/TP on bar **T** (ambiguous → `UNRESOLVED`, no favorable pick).  
3. If **pending** and no position → Model 1 fill attempt on bar **T** (if `T ≥ active_from`).  
4. **Review** pending (cancel/replace rules = BT-D41).  
5. **Dynamic scanner** state advance; on emit:  
   - if **position open** → `SCANNER_SUPPRESSED` (no new pending);  
   - else → CREATE / REPLACE as in D41.

Activation: BT-D41/BT-D42 close-key semantics (`comparison_bt_d45.md`).

---

## 3. Sep-11 integrated results (E–F)

| Metric | BT-D46 integrated | Notes |
|--------|------------------:|-------|
| Scanner candidate emits | **102** | Same as BT-D26/D41 signal count |
| Pending **CREATE** | **36** | 66 emits while position open → suppressed |
| Pending KEEP / CANCEL / REPLACE | 53 / 15 / 2 | Lifecycle events |
| Lineage links | 2 | Bound-drift replacements |
| Model 1 simulated entries | **19** | Matches **BT-D45 M1** |
| Closed SL / TP | 14 / 4 | No PnL |
| Open at pilot end | 1 | |
| Suppressed scanner (position open) | **66** | |
| Unresolved exit | 0 | |

**102 scanner emits are not 102 trades.** Independent count: **`NOT_EXCHANGE_CONFIRMED`**.

### vs BT-D41 / BT-D45

| | D41 | D45 M1 (segment) | D46 |
|--|-----|------------------|-----|
| CREATE | 102 | — | 36 (position gating) |
| Simulated entries | — | 19 | **19** |
| Blocked emits | — | 4 | **66** |

D45 replays order timelines **sequentially** without interleaved live scanner state; D46 **interleaves** fills/exits with scanner → **more** suppressed emits (66 vs 4) but **same** entry count (19).

---

## 4. Reference 15:35 UTC (G)

Pool 0.08681–0.086838, replace **15:40**, no touch before replace — **no** `SIMULATED_FILL` on that lineage in integrated run (consistent with D42/D45).

---

## 5. Tests

```bash
python3 tools/phase_bt_d46_integrated_short_backtester_v1/run_integrated_backtest.py
python3 -m pytest tools/phase_bt_d46_integrated_short_backtester_v1/tests/test_integrated_backtester_v1.py
```

**4 passed** — one position max, 102 scanner emits, deterministic summary, outputs present.

---

## 6. Artifacts

| Path | Role |
|------|------|
| `tools/phase_bt_d46_integrated_short_backtester_v1/integrated_backtester_v1.py` | Engine |
| `tools/phase_bt_d46_integrated_short_backtester_v1/run_integrated_backtest.py` | Driver |
| `results/phase_bt_d46_integrated_short_backtester_v1/chronological_events.csv` | Event log |
| `results/phase_bt_d46_integrated_short_backtester_v1/pending_order_lineage.csv` | Parent/child |
| `results/phase_bt_d46_integrated_short_backtester_v1/simulated_positions.csv` | Positions |
| `results/phase_bt_d46_integrated_short_backtester_v1/execution_ambiguities.csv` | Ambiguities |
| `results/phase_bt_d46_integrated_short_backtester_v1/summary.json` | Counts |
| `results/phase_bt_d46_integrated_short_backtester_v1/comparison_bt_d45.md` | D45 baseline |

---

**End of BT-D46.**
