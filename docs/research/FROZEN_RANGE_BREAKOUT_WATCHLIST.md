# Frozen Range Breakout Watchlist

**Status date (UTC):** 2026-08-21  
**Branch:** `research/confirmed-orderbook-entries`  
**Repo:** `orderbook_analyse`  
**Related inventory:** `docs/research/ORDERBOOK_RESEARCH_SIGNAL_INVENTORY.md` (§12–§15)

## Purpose

Document the consolidation → breakout research line after **30d OB** replay, regime gating, and SHORT-C trade-management diagnostics.

- **Not** live auto-trade  
- **No** silent retuning of frozen V1  
- Rejected ≠ deleted  

| Candidate | Status |
|---|---|
| `RANGE60_BREAKOUT_OB_V1` | **`REJECTED_AS_STANDALONE` / `CONTEXT_ONLY`** |
| `RANGE60_BREAKOUT_OB_REGIME_V2` (best: **C `VOL_PLUS_CONTINUATION`**) | **`CONFIRMED_WATCHLIST`** — watchlist/alert research only; **no live trade** |
| SHORT-C trade management (on Regime C SHORT) | **`WATCHLIST_ONLY`** |
| `SHORT_C_STRONG_PRESSURE_V2` (best pre-entry: **A `STRONG_FLOW`**) | **`STRONG_PRESSURE_PARTIAL` / `PAPER_HYPOTHESIS`** |

## Lineage

| Step | Path / note |
|---|---|
| Discovery | `results/level_break_reclaim_discovery/20260811_17_51coins/` |
| Broad breakout | `BREAKOUT_CONTINUATION_STRICT_V1` — **REJECTED** (frequency) |
| 7d freeze source | `results/frozen_hard_tests/RANGE_CONSOLIDATION_BREAKOUT_V1_20260811_17/` Variant **A** → name `RANGE60_BREAKOUT_OB_V1` |
| 30d V1 replay | `results/frozen_hard_tests/RANGE60_BREAKOUT_OB_V1_30D_20260719_0817/` |
| Regime V2 | `results/frozen_hard_tests/RANGE60_BREAKOUT_OB_REGIME_V2_30D_20260719_0817/` |
| SHORT-C TM | `…/short_c_trade_management/` |
| Strong pressure | `…/short_c_strong_pressure_v2/` |

---

## A) `RANGE60_BREAKOUT_OB_V1` — 30d standalone

| Field | Value |
|---|---|
| **Status** | **`REJECTED_AS_STANDALONE` / `CONTEXT_ONLY`** |
| **Artifact** | `results/frozen_hard_tests/RANGE60_BREAKOUT_OB_V1_30D_20260719_0817/` |
| **Window** | 2026-07-19 … 2026-08-17, 51 coins, `ob200_v3` |
| **n** | 516 |
| **Signals/day** | ~17.2 |
| **Hit075 lift** | 60m **+5.44 pp**; 120m **+6.06 pp** (both **&lt; +10 pp**) |
| **MAE** | **Worse than control** |
| **Weeks** | **Not stable** (W3 flat/negative on matched lifts) |

**Interpretation:** Alone not sufficient for CONFIRMED. Remains a **context / building block** for Regime V2. **Do not silently edit** this definition.

### Frozen definition (unchanged)

Causal rules; **do not edit in place**. Any gate change → new name (`…_V2` / sibling).

Shared:

- Range and features only from data **before** the break minute  
- Signal decision at **hold/confirm minute close** (break + 3 follow minutes)  
- **Entry** = next 1m **open** after confirmation  
- Cooldown in source tests: **60m** per coin per side  

**Range (60m):** prior 60m high/low; width in trailing 24h **p20–p70**; no strong trend; ≥2 spaced touches on breakout edge.

**Breakout + flow + OB:** clear break; aligned `delta_3m`/`delta_5m`; TPS ≥ trailing p90; spread ≤ trailing p75; OB not opposed (LONG: OFI≥0 or imb≥med; SHORT mirror); **2/3** holds outside range; confirm directed `ret_1m` ≤ **0.30%**.

Exact prose of the original freeze remains authoritative for replay; see also inventory §12.

---

## B) `RANGE60_BREAKOUT_OB_REGIME_V2`

| Field | Value |
|---|---|
| **Status** | **`CONFIRMED_WATCHLIST`** — **no live trade** |
| **Best variant** | **C `VOL_PLUS_CONTINUATION`** (= vol regime **A** + continuation **B**) |
| **Artifact** | `results/frozen_hard_tests/RANGE60_BREAKOUT_OB_REGIME_V2_30D_20260719_0817/` |
| **Base** | Exact V1 signals + **a-priori regime gates** (not a silent V1 edit) |
| **Signals/day** | ~**5.8** |
| **Hit075 lift** | 120m **+10.15 pp**; 60m **+9.7 pp** |
| **Weeks** | All **4 weeks** positive lift; W3 problem **reduced** |
| **Side** | **SHORT stronger than LONG** |
| **MAE** | Absolute MAE **worse** than control; **MFE/MAE ratio better** |

**Gates (research):**  
- **A** `VOL_REGIME_PRIMARY`: `coin_rv_24h` ≥ cross-sectional universe median at t **and** `range_width` ≥ V1-30d median  
- **B** `CONTINUATION_REGIME`: directed 1h pre-return in signal direction **and** (BTC 1h aligned **or** BTC RV ≥ trailing median)  
- **C** = A ∧ B  
- **D** = V1 baseline (comparison only)

**Interpretation:** Regime-gating matters. Edge appears mainly in suitable vol/continuation conditions. Suitable for **watchlist / research alert**, **not** auto-trade.

---

## C) SHORT-C trade management

| Field | Value |
|---|---|
| **Status** | **`WATCHLIST_ONLY`** |
| **Artifact** | `…/RANGE60_BREAKOUT_OB_REGIME_V2_30D_20260719_0817/short_c_trade_management/` |
| **Universe** | Regime V2 variant **C**, **SHORT only** |
| **Exit check** | TP **0.75%** / SL **1.00%** / max **120m**, **SL_FIRST** |

| Metric (TP0.75/SL1.0/max120) | Value |
|---|---|
| n | 76 |
| TP / SL / Timeout | **40 / 19 / 17** |
| Avg PnL @ **0.11%** RT | **+0.0132%** (thin) |
| Weeks @ 0.11% | **Not stable** |
| Higher TP check | **`TP_NOT_THE_MAIN_PROBLEM`** — higher TPs **worse** |

**Interpretation:** Signal often sees movement, but **fees + week instability** block a tradeable claim. No live claim.

---

## D) `SHORT_C_STRONG_PRESSURE_V2`

| Field | Value |
|---|---|
| **Status** | **`STRONG_PRESSURE_PARTIAL` / `PAPER_HYPOTHESIS`** |
| **Artifact** | `…/short_c_strong_pressure_v2/` |
| **Best real pre-entry** | **A `STRONG_FLOW`** |
| **A n** | **15** (small) |
| **A SL share** | **13.3%** (baseline SHORT-C ~25%) |
| **A wr @ 0.11%** | **73%** |
| **A avg @ 0.11%** | **+0.191%**; tot/100 ≈ **+2.9** |
| **Removed vs kept** | Removed **worse** than kept |
| **Weeks** | 4/4 positive @ 0.11% but **n too small** for confirm |
| **Variant C** | Numerically better but **`POST_ENTRY_CONFIRMATION`** (no reclaim in first 5m) — **not** a normal entry filter |

**Interpretation:** Stronger breakout pressure helps. **n too small** for CONFIRMED. Paper / watchlist hypothesis only.

---

## Hard rules

1. **Do not silently change V1.** Exact replay only under the same name.  
2. **V2 is a new candidate** (regime gate on top of V1 signals).  
3. **Any new threshold / gate / horizon edit → new name.**  
4. **Rejected ≠ delete** — keep as context / lineage.  
5. **No live-trading claim** from this doc.  
6. Watchlist / paper ≠ auto-trade.  

## Next steps

1. **Holdout / forward paper** on Regime V2 C and Strong Pressure A (new artifact folders; same names if exact).  
2. **`COIN_REGIME_SCANNER_V1`** — coin-level regime context (new candidate).  
3. Do not promote SHORT-C exits to live without fee-aware, multi-week holdout.

## Changelog

| Date (UTC) | Note |
|---|---|
| 2026-08-21 | Initial freeze: `RANGE60_BREAKOUT_OB_V1` from consolidation Variant A (`PARTIAL_WATCHLIST`) |
| 2026-08-21 | 30d V1 → `REJECTED_AS_STANDALONE`/`CONTEXT_ONLY`; add Regime V2 `CONFIRMED_WATCHLIST`; SHORT-C TM `WATCHLIST_ONLY`; Strong Pressure `PAPER_HYPOTHESIS` |
