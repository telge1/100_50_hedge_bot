# PHASE BT-D42 — Causal Short Limit Fill vs Cancel Research

**Project:** `pools+ob+delta_bot`  
**Baseline:** BT-D41 `PENDING_LIFECYCLE_RESEARCH_PASS`  
**Pilot:** Sep-11–12 2026 DOGEUSDT 5m pool-first dynamic run  
**Date:** 2026-10-26  

---

## VERDICT

**`FILL_CANCEL_CHRONOLOGY_PASS`**

Evidence-based ordering of **price touch** vs **cancel/replace effective time** for all **102** pending segments. No orders, no PnL, no strategy or BT-D41 lifecycle changes.

---

## 1. Activation timing contract (B — verified, unchanged)

| Concept | Value |
|---------|--------|
| Order created at decision close **T** | e.g. `2026-09-11T15:35:00Z` |
| `pending_active_from_utc` / `earliest_eligible_execution_utc` | **T+5m** bar key (`2026-09-11T15:40:00Z`) |
| First OHLC evaluated | That **close-labelled** 5m candle — **not** the originating decision bar |

This matches BT-D41 and `evaluate_short_sell_limit_fill` (`bar_utc >= order_active_from_utc`). It denotes the **first eligible 5m decision-close bucket**, not “bar open instant” as a separate timestamp in SSOT.

---

## 2. Data sources (C)

| Layer | Coverage | Role |
|-------|----------|------|
| **5m OHLCV** | SSOT window (288 bars) | Primary touch + `causal_sell_limit_v2` evidence |
| **1m OHLCV** | ClickHouse `1681` closed 1m bars in pilot span | Intrabar ordering when 5m bar equals cancel close |
| **Public trades** | Not used | Coverage/validation not required for this window |

Touch ≠ guaranteed fill. Evidence uses `causal_sell_limit_v2` only as **EXECUTION_POSSIBLE** reference (open ≥ limit, or open &lt; limit ≤ high). No queue, liquidity, or slippage invented.

---

## 3. Status taxonomy (D)

| Status | Meaning |
|--------|---------|
| `NO_TOUCH` | No `high >= entry` while pending |
| `CANCELLED_BEFORE_TOUCH` | Ended (cancel/replace) with no prior touch |
| `TOUCHED_BEFORE_CANCEL` | Touch evidence before cancel close (fill not proven by v2) |
| `PRICE_TOUCHED_FILL_NOT_PROVEN` | High touch without v2 fill path |
| `EXECUTION_POSSIBLE` | v2 fill path on a bar **before** cancel effective close |
| `EXECUTION_UNRESOLVED` | Touch on **same** 5m bar as cancel/replace close |
| `STILL_PENDING_*` | Open at pilot end |

**Branches:** `UNFILLED_PENDING` vs `POSSIBLY_FILLED` (no fabricated fills).

**Priority:** If touch precedes cancel effective close → BT-D41 cancel/replace is **not** assumed conditional on unfilled (`bt41_cancel_conditional_on_unfilled=false`).

---

## 4. Pilot aggregates (F)

| Metric | Count |
|--------|------:|
| Order segments | 102 |
| BT-D41 `POTENTIAL_FILL_UNRESOLVED` | 82 |
| BT-D42 touch evidence rows (5m) | 82 |
| Cancel/replace **not** conditional on unfilled | **66** |
| `EXECUTION_POSSIBLE` | 19 |
| `EXECUTION_UNRESOLVED` | 47 |
| `CANCELLED_BEFORE_TOUCH` | 35 |
| `STILL_PENDING_NO_TOUCH` | 1 |

All **82** D41 unresolved intervals are confirmed in `touch_evidence.csv` (`high >= entry` on the flagged bar).

---

## 5. Reference 2026-09-11 15:35 UTC (E)

**Order** `80a38deab26b25f812ea` — entry **0.08681**, SL **0.087011676**, TP **0.08586**.

| Item | Result |
|------|--------|
| First eligible execution time | **15:40:00Z** (5m close key) |
| Replace effective | **15:40:00Z** (`REPLACE` / bound drift) |
| 5m bar 15:40 | o **0.08668**, h **0.08671**, l **0.08639**, c **0.08639** |
| Touch? | **No** — `high 0.08671 < entry 0.08681` |
| 1m touch before replace | **None** in validated CH window |
| Status | **`CANCELLED_BEFORE_TOUCH`** / `UNFILLED_PENDING` |
| Could old pending fill before replace? | **No** on available OHLC |
| Limitations | Order active only on the same 5m bucket as replace; no intrabar path to entry on this bar |

Full JSON: `results/phase_bt_d42_fill_cancel_chronology_v1/reference_1535.json`.

---

## 6. Example — touch before cancel (not 15:35)

Order `57c97bbd342e9817bcf7` (create 12:05, cancel 12:30): first touch **12:25** (`high 0.08399`), cancel **12:30** → **`EXECUTION_POSSIBLE`**, `POSSIBLY_FILLED`, cancel **not** conditional on unfilled.

---

## 7. Tests (G)

`python3 -m pytest tools/phase_bt_d42_fill_cancel_chronology_v1/tests/test_fill_chronology_v1.py`

Covers: no touch, touch before cancel, cancel before touch, no activation in decision candle, segment parsing, deterministic CSV replay.

---

## 8. Artifacts

| Path | Content |
|------|---------|
| `tools/phase_bt_d42_fill_cancel_chronology_v1/fill_chronology_v1.py` | Chronology engine |
| `tools/phase_bt_d42_fill_cancel_chronology_v1/run_chronology.py` | Pilot driver |
| `results/phase_bt_d42_fill_cancel_chronology_v1/order_timelines.csv` | Per-order summary |
| `results/phase_bt_d42_fill_cancel_chronology_v1/touch_evidence.csv` | All touch bars |
| `results/phase_bt_d42_fill_cancel_chronology_v1/cancel_fill_conflicts.csv` | 66 non-conditional ends |
| `results/phase_bt_d42_fill_cancel_chronology_v1/reference_1535.json` | 15:35 case |
| `results/phase_bt_d42_fill_cancel_chronology_v1/summary.md` | Machine summary |

**Re-run:** `python3 tools/phase_bt_d42_fill_cancel_chronology_v1/run_chronology.py`

---

## 9. Scope

- BT-D41 outputs preserved; no ranking, thresholds, dashboard, or LMV2 Short V2 changes  
- No SL/TP outcome simulation  
- `FILLED` remains a future integration state  

---

**End of BT-D42.**
