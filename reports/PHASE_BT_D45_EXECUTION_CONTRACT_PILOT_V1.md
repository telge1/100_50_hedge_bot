# PHASE BT-D45 — Deterministic Short Execution Contract Pilot

**Project:** `pools+ob+delta_bot`  
**Baselines:** BT-D41 … BT-D44 **PASS**  
**Pilot:** DOGEUSDT Sep-11–12 2026 (24h)  
**Date:** 2026-10-26  

---

## VERDICT

**`EXECUTION_CONTRACT_RESEARCH_PASS`**

Two **explicitly labeled** execution models compared under single-pending / single-position chronology. No PnL, no real orders, no changes to BT-D26/D41–D44 artifacts or density math.

---

## 1. Preserved rules (A)

Canonical 5m PD1 exact-origin Depth2, nearest eligible upper only, existing Entry/SL/TP/Gap, T+5m close-key activation, no retroactive fill/cancel.

---

## 2. Activation chronology (B)

SSOT **close-key** = 5m bucket **ending** at that timestamp. Pending active when `bar_utc >= pending_active_from_utc`. Fills evaluated only on bars **strictly before** cancel-effective close when a cancel exists. Documented in `execution_evidence.csv` per order/bar.

---

## 3. Execution models (D) — neither is “the truth”

| ID | Definition |
|----|------------|
| **model_1_touch_hypothetical_active_v1** | Full `causal_sell_limit_v2` while pending active and before cancel |
| **model_2_conservative_open_cross_v1** | **Only** `CAUSAL_V2_MARKETABLE_AT_OPEN` (`open >= limit`); `CAUSAL_V2_LIMIT_TOUCH_INTRABAR` → **UNRESOLVED** (no invented tick threshold) |

Touch alone is **not** a proven exchange fill on either model.

---

## 4. Position lifecycle (E)

After modeled entry: one position, existing SL/TP, `simulate_short_exit` with `no_trade_ambiguous`, block new shorts until close or pilot end. No PnL.

---

## 5. Pilot comparison (F)

| Metric | Model 1 | Model 2 |
|--------|--------:|--------:|
| Pending created | 102 | 102 |
| Cancelled / replaced | 89 / 12 | 89 / 12 |
| **Modeled entries** | **19** | **0** |
| Closed positions | 17 | 0 |
| Still open | 1 | 0 |
| Unresolved entry | 44 | 66 |
| Unresolved exit | 1 | 0 |
| Blocked scanner emits | 4 | 0 |

**Why counts differ:** All **19** Model-1 entries use **`CAUSAL_V2_LIMIT_TOUCH_INTRABAR`** (open below limit, high reaches limit). Model 2 **does not** treat those as entries → **0** modeled trades on this window. Model 1 aligns with BT-D44 Path B entry count (**19**). Model 2 is deliberately stricter; **no silent optimism**.

**Confirmed trade count / profitability:** not stated.

---

## 6. Reference 15:35 UTC (G)

Pool 0.08681–0.086838, entry 0.08681, replace **15:40**, `CANCELLED_BEFORE_TOUCH`. **Model 1 & 2: no modeled fill** (`ref_ok` in pilot run). 15:40 bar high **0.08671 < entry**.

---

## 7. Evidence classes (C)

Per-bar in `execution_evidence.csv`: `no_touch`, `touch_while_active`, `gap_through_order_level_at_open`, `BEFORE_ACTIVATION`, cancel-close ambiguity flags.

1m/trades: 5m SSOT primary for contract; finer data not required to pass this pilot (D42 1m used in prior phases for ambiguity notes only).

---

## 8. Tests

```bash
python3 tools/phase_bt_d45_execution_contract_v1/run_contract_pilot.py
python3 -m pytest tools/phase_bt_d45_execution_contract_v1/tests/test_execution_contract_v1.py
```

**5 passed** — model discrimination, pilot outputs, reference 1535, determinism.

---

## 9. Deliverables

| Path | Role |
|------|------|
| `tools/phase_bt_d45_execution_contract_v1/` | Implementation |
| `results/phase_bt_d45_execution_contract_v1/execution_evidence.csv` | Per-bar evidence |
| `results/phase_bt_d45_execution_contract_v1/execution_model_comparison.csv` | Model totals + delta row |
| `results/phase_bt_d45_execution_contract_v1/position_timelines.csv` | Modeled positions (M1) |
| `results/phase_bt_d45_execution_contract_v1/ambiguity_cases.csv` | UNRESOLVED entry/exit |
| `results/phase_bt_d45_execution_contract_v1/summary.md` | Machine summary |

---

**End of BT-D45.**
