# Orderbook / Volatility / Flow — Research Signal Inventory

**Created (UTC):** 2026-08-20  
**Repo:** `orderbook_analyse`  
**Branch (at write):** `research/confirmed-orderbook-entries`  
**Purpose:** Keep every tested candidate findable. Rejected ≠ deleted.

---

## Hard rules

1. **Rejected does not mean discarded.**  
   Standalone-rejected features may later reappear as **context** inside a new named confluence (`…_V2`, etc.).
2. **No silent retuning.** Changing quantiles/gates/horizons requires a **new candidate name**.
3. **Primary clean OB window** for multi-coin work: `2026-08-11` … `2026-08-17` (51 coins, `ob200_v3`).  
   OI/Liq not used there (no clean overlap until ~2026-08-18 16:00Z).
4. **Standard path metrics** (where applied): entry = next 1m open after signal close; measure fixed horizon MFE / MAE / return / hits vs matched controls.

### Status vocabulary (this inventory)

| Status | Meaning |
|---|---|
| `CONFIRMED` | Passed acceptance including robustness/holdout where required |
| `CONFIRMED_WATCHLIST` | Passed research acceptance for **watchlist/alert research**; **not** live auto-trade |
| `PARTIAL` | Interesting in-sample or discovery, but incomplete / fragile |
| `PARTIAL_WATCHLIST` | Filter/trigger interesting enough to freeze for retest; **not** live-confirm / **not** live-alert |
| `WATCHLIST_ONLY` | Interesting path/exit research; fees/stability block tradeable claim |
| `PAPER_HYPOTHESIS` / `STRONG_PRESSURE_PARTIAL` | Small-n or fragile improvement; paper only |
| `REJECTED_AS_STANDALONE` | Failed as a solo trigger; still usable as context |
| `CONTEXT_ONLY` | Marks regime/activity/risk, not a directional edge |
| `REJECTED_FULLY` | Do not reuse even as context without a new thesis |

---

## Quick matrix

| Candidate | Window | Horizons | Standalone status | Best reuse |
|---|---|---|---|---|
| R≥5 Continuation | 11–17 / 51c | 1h | `REJECTED_AS_STANDALONE` | Volatility / range context |
| R≥5 Reversal | 11–17 / 51c | 1h | `REJECTED_AS_STANDALONE` | Weak mean-reversion hint only |
| FLOW_ALIGNED_TPS3 | 11–17 / 51c | 1h (MFE≥0.75) | `CONTEXT_ONLY` | Activity / flow intensity |
| EXHAUSTION_FLIP_TPS3 | 11–17 / 51c | 1h (MFE≥0.75) | `CONTEXT_ONLY` | Activity + asymmetric flow |
| LLD Touch/Break/Struct/Surge family | 11–17 (+ contaminated 18–19 in MULTI) | 1h | `REJECTED_AS_STANDALONE` | Level / LLD context |
| Big-Move Precondition Discovery | 11–17 / 51c | labels 1h MFE | `PARTIAL` | Feature ranking source |
| Rare Confluence Discovery | 11–17 / 51c | labels 1h MFE | `CONFIRMED` (discovery only) | Source of frozen V1s |
| SHORT_RARE_IMB_DELTA_TPS_V1 | 11–17 / 51c | 1h + 4h | `REJECTED_AS_STANDALONE` | OB+flow extreme confluence |
| LONG_RARE_IMB_OFI_DELTA_V1 | 11–17 / 51c | 1h + 4h | `REJECTED_AS_STANDALONE` | OB+OFI+flow confluence |
| RARE_CONFLUENCE_QUIET_ENTRY_V2 | 11–17 / 51c | 1h + 4h | `PARTIAL` / `CONTEXT_ONLY` | Quiet-entry risk gate (not alert) |
| TIERA_LONG_ONLY_IN_HIGH_VOL_V1 | 11–17 Tier-A | TRADE / TRADE_NO_BE50 | `PARTIAL_WATCHLIST` | Filter existing Tier-A LONGs; SHORT passthrough |
| RANGE60_BREAKOUT_OB_V1 | 11–17 + **30d** / 51c | 1h + 2h | `REJECTED_AS_STANDALONE` / `CONTEXT_ONLY` | 30d standalone fail; building block for Regime V2 |
| RANGE60_BREAKOUT_OB_REGIME_V2 (C) | 30d / 51c | 1h + 2h | `CONFIRMED_WATCHLIST` | V1 + vol/continuation regime gate; **no live trade** |
| SHORT-C trade management | 30d Regime-C SHORT | TP/SL path | `WATCHLIST_ONLY` | Fees/weeks block tradeable |
| SHORT_C_STRONG_PRESSURE_V2 (A) | 30d SHORT-C | TP0.75/SL1.0 | `STRONG_PRESSURE_PARTIAL` / `PAPER_HYPOTHESIS` | Stronger flow/break pressure; n small |

ADAUSDT-only detector / continuation studies (Jul–Aug) are listed under **Related ADAUSDT studies** — not multi-coin production candidates.

Tier-A **filter** candidates (existing `signal_generator` Tier-A signals, not new entries) are detailed in §11 and `docs/research/FROZEN_TIERA_FILTER_WATCHLIST.md`.

Range-breakout / regime watchlist: §12–§15 and `docs/research/FROZEN_RANGE_BREAKOUT_WATCHLIST.md`.

---

## 1) R≥5 Volatility Continuation

| Field | Value |
|---|---|
| **Name** | `VOL_EVENT_R5_CONTINUATION` (family: vol_event flow / cont-vs-rev) |
| **Window** | 2026-08-11 … 2026-08-17, 51 coins |
| **Sources** | candles_1m, public_trades_canonical, OB `ob200_v3` |
| **Horizons** | **1h** (60m after entry) |
| **Definition** | Episode first minute with `vol_ratio_max ≥ 5`; PUMP→LONG / DUMP→SHORT; cooldown 30m; OB available |
| **Direction** | Continuation = with event; also measured vs matched controls |
| **Key metrics (1h)** | n≈5684; Hit≈**47.3%**; Ø MFE≈0.48%; Ø MAE≈0.45%; vs control Hit **−4.4 pp** |
| **Control** | Worse than matched controls on hit/return; MAE higher |
| **Holdout** | Not separately promoted; full clean window already rejects |
| **Status** | **`REJECTED_AS_STANDALONE`** / effectively **`CONTEXT_ONLY`** (range expansion) |
| **Still useful** | **Volatility Context**, **Risk Warning** (two-way range after spike) |
| **Retest when** | More OB days; combine with *rare* directional confluence under a **new name**; not alone |

**Artifacts (note):** deep-dive dirs under `results/condition_1h_moves/vol_event_*` were removed after reject; numbers retained from run logs / prior reports. Detector family still under `results/volatility_event_detector/`.

---

## 2) R≥5 Volatility Reversal

| Field | Value |
|---|---|
| **Name** | `VOL_EVENT_R5_REVERSAL` |
| **Window** | same 11–17 / 51c |
| **Sources** | same |
| **Horizons** | **1h** |
| **Definition** | Same R≥5 trigger; trade **against** event (PUMP→SHORT, DUMP→LONG) |
| **Direction** | Reversal / Gegenrichtung |
| **Key metrics (1h)** | Hit≈**51.0%**; vs control Hit ~**+5 pp** but largely **mirror artifact** of control labeling; absolute ≈ coin-flip; MAE not better |
| **Control** | Lift vs “reversal control” misleading; no clean MAE win |
| **Holdout** | N/A as standalone winner |
| **Status** | **`REJECTED_AS_STANDALONE`** |
| **Still useful** | Mild **Risk Warning** that post-spike path is two-sided; not a short/long alert |
| **Retest when** | Only inside new confluence with MAE discipline + longer holdout |

---

## 3) TPS3 Flow Aligned — `FLOW_ALIGNED_TPS3`

| Field | Value |
|---|---|
| **Name** | `FLOW_ALIGNED_TPS3` |
| **Window** | 2026-08-11 … 2026-08-17, 51 coins |
| **Sources** | candles_1m, trades, OB (availability) |
| **Horizons** | **1h**; success = Hit MFE≥0.75% in labeled direction |
| **Definition** | Frozen: `tps_ratio ≥ 3` + `delta_5m` aligned (LONG>0 / SHORT<0); entry next open; cooldown 60m |
| **Direction** | With delta sign |
| **Key metrics** | n≈10.8k; ~**1500 signals/day**; Hit075≈15.8% vs ctrl≈5.3% (**+10.5 pp**); Ø MFE≈0.42% vs 0.27%; Ø MAE≈0.42% vs 0.26% (**MAE worse**) |
| **Control** | Hit/MFE lift, **fails MAE / MFE-MAE acceptance** |
| **Holdout** | Not required — rejected on MAE in full window |
| **Status** | **`CONTEXT_ONLY`** (activity / range expansion) |
| **Still useful** | **Activity Context**, **Flow Context** (intensity), **Risk Warning** |
| **Retest when** | Only as one leg of a **rare** confluence (e.g. top1% TPS), new candidate name |

**Artifacts:** `results/frozen_precondition_hard_test/flow_tps3_20260811_17/`

---

## 4) TPS3 Exhaustion Flip — `EXHAUSTION_FLIP_TPS3`

| Field | Value |
|---|---|
| **Name** | `EXHAUSTION_FLIP_TPS3` |
| **Window** | 11–17 / 51c |
| **Sources** | same as TPS3 aligned |
| **Horizons** | **1h** (MFE≥0.75%) |
| **Definition** | Frozen: `tps_ratio ≥ 3` + persistent **opposite** pressure (`delta_5m` & `delta_1m` against flip side) |
| **Direction** | Flip vs prior pressure (LONG after sells, SHORT after buys) |
| **Key metrics** | n≈10.3k; ~**1480/day**; Hit075≈15.5% vs ctrl≈4.9% (**+10.7 pp**); MAE still higher vs control |
| **Control** | Same failure mode as aligned: range↑ both ways |
| **Holdout** | N/A |
| **Status** | **`CONTEXT_ONLY`** |
| **Still useful** | **Flow Context** (exhaustion asymmetry idea), **Activity Context** |
| **Retest when** | Extreme quantiles + OB imbalance (see rare V1s), not raw TPS3 |

**Artifacts:** same `flow_tps3_20260811_17/`

---

## 5) LLD Touch / Break / Struct / Surge family

| Field | Value |
|---|---|
| **Names** | `S_lld_touch_ob`, `L_lld_touch_ob`, `*_lld_break_ob`, `*_struct_*`, `*_surge_delta_ob` (MULTI condition study) |
| **Window** | OB era 11–17 primary; MULTI also scanned through 19 (Aug19 contaminated) |
| **Sources** | candles, trades, OB `ob200_v3`, LLD (TRP Liquidity Location); OI/Liq only when present |
| **Horizons** | **1h** directional ret/MFE/MAE |
| **Definition (example SHORT touch)** | Upper LLD touch + OB short context (≥2/3) + `tps≥3`; cooldown 30m |
| **Direction** | Long/short per condition |
| **Key metrics** | Early MULTI clean cell for `S_lld_touch_ob`: n=34, Hit≈71% (**Aug17-only artifact**). Deep dive full clean sample: n=350, Hit≈**47.7%**, ≈ control, **REJECTED** |
| **Control** | Matched coin/hour/vol — no durable edge |
| **Holdout** | Expanding sample killed the edge; Aug19 contamination previously inflated totals |
| **Status** | **`REJECTED_AS_STANDALONE`** |
| **Still useful** | **Level Context** (pool distance/touch/break), **OB Context** (imbalance/OFI/removals) |
| **Retest when** | Longer LLD warmup + OB holdout; only inside new named confluence |

**Artifacts:** MULTI / LLD deep-dive dirs under `results/condition_1h_moves/` and `MULTI_20260811_19_*` were **deleted after reject**; keep this inventory as the memory. LLD still used as **context** in big-move discovery feature ranks (`lld_dist_*`).

---

## 6) Big-Move Precondition Discovery

| Field | Value |
|---|---|
| **Name** | `BIG_MOVE_PRECONDITION_DISCOVERY` |
| **Window** | 11–17 / 51c |
| **Sources** | candles, trades, OB, LLD features |
| **Horizons** | Labels: 1h MFE_up/down ≥0.75% / 1.0% (clustered, cooldown 60m) |
| **Definition** | Find large 1h moves first; rank causal preconditions vs matched non-move controls |
| **Direction** | LONG / SHORT big-move labels separately |
| **Key metrics** | Clustered ~2464 LONG / 2523 SHORT at 0.75%; strongest separators: pre-range, LLD distance, TPS/vol_ratio, asymmetric delta; combos only moderate until rare quantiles |
| **Control** | Matched non-big-move minutes |
| **Holdout** | Discovery phase only |
| **Status** | **`PARTIAL`** (method confirmed useful; no single frozen alert) |
| **Still useful** | Entire **feature menu** for confluence; methodology template |
| **Retest when** | More OB days; OI/Liq overlap era; sub-minute lead |

**Artifacts:** `results/big_move_precondition_discovery/20260811_17_51coins/`

---

## 7) Rare Confluence Discovery

| Field | Value |
|---|---|
| **Name** | `RARE_CONFLUENCE_DISCOVERY` |
| **Window** | 11–17 / 51c |
| **Sources** | candles, trades, OB; LLD optional in atom pool |
| **Horizons** | Label Hit MFE≥0.75% (path after next open) |
| **Definition** | Causal trailing-24h top1/2% flags; ≤3-condition ANDs; frequency gate ≤10/day across 51 coins; `ob_ok`+`spread_ok` |
| **Direction** | LONG and SHORT separately |
| **Key metrics** | Verdict **`RARE_CONFLUENCE_CONFIRMED`** (discovery); 10 interesting combos; best SHORT ~1.7/day Hit075 +25 pp & MAE↓; best LONG ~3/day Hit075 +22 pp & MAE↓ |
| **Control** | Matched controls; MAE often truly lower (unlike TPS3) |
| **Holdout** | Not yet — handed to frozen V1 hard-tests |
| **Status** | **`CONFIRMED`** as *discovery process*; candidates themselves still need freeze/holdout |
| **Still useful** | Blueprint for rare OB+flow extremes |
| **Retest when** | Always with frozen names + walk-forward |

**Artifacts:** `results/rare_confluence_discovery/20260811_17_51coins/`  
**Doc:** feeds `docs/research/FROZEN_RARE_CONFLUENCE_WATCHLIST.md`

---

## 8) `SHORT_RARE_IMB_DELTA_TPS_V1`

| Field | Value |
|---|---|
| **Name** | `SHORT_RARE_IMB_DELTA_TPS_V1` (**frozen**) |
| **Window** | 11–17 / 51c; discovery split 11–15; holdout 16–17 |
| **Sources** | candles, trades, OB; LLD not a filter |
| **Horizons** | **1h** hard-test; **1h + 4h** watchlist run |
| **Definition** | SHORT ∧ `imb50_lo_top1` ∧ `delta_lo_top2` ∧ `tps_top1` ∧ `ob_ok` ∧ `spread_ok`; causal trailing quantiles; entry next open; cooldown 60m |
| **Direction** | SHORT only; Gegenrichtung via MAE |
| **Key metrics** | n=12 (~2/day). **1h:** Hit075=25% vs ctrl (~+19–25 pp); Med MFE≈0.54%; Med MAE≈0.18%; MAE better than control. **4h:** Hit075≈58%; Ø MFE≈1.21%; Ø MAE≈0.56%; still better vs control in-sample |
| **Control** | Full window pass; **holdout 1h fail** (n=4, Hit075=0%) |
| **Holdout** | **FAIL** (1h). 4h holdout mixed / tiny n |
| **Status** | **`REJECTED_AS_STANDALONE`** (watchlist freeze cycle rejected; definition remains frozen for reuse) |
| **Still useful** | **OB Context** (extreme ask imbalance), **Flow Context**, **Activity Context** (tps top1) — as *legs*, not solo alert |
| **Retest when** | Longer OB holdout post-2026-08-20; same V1 definition only; or new `…_V2` if gates change |
| **Timing risk** | Signal minute often already down (`ret_1m` med ≈ −0.20%); MAE often before MFE |

**Artifacts:**  
- `results/frozen_hard_tests/SHORT_RARE_IMB_DELTA_TPS_V1_20260811_17/`  
- `results/frozen_rare_confluence_watchlist/20260811_17/`  
- `docs/research/FROZEN_RARE_CONFLUENCE_WATCHLIST.md`

---

## 9) `LONG_RARE_IMB_OFI_DELTA_V1`

| Field | Value |
|---|---|
| **Name** | `LONG_RARE_IMB_OFI_DELTA_V1` (**frozen**) |
| **Window** | 11–17 / 51c; 11–15 discovery; 16–17 holdout |
| **Sources** | candles, trades, OB |
| **Horizons** | **1h + 4h** |
| **Definition** | LONG ∧ `imb50_hi_top1` ∧ `ofi_hi_top2` ∧ `delta_hi_top2` ∧ `ob_ok` ∧ `spread_ok` |
| **Direction** | LONG; MAE = adverse down |
| **Key metrics** | n=21 (~3/day). **1h:** Hit075≈33% (+16 pp vs ctrl); Ø MFE≈0.86%; Ø MAE≈0.57% (ratio ok). **4h:** Hit075≈67%; Ø MFE≈1.42%; Ø MAE≈1.12% |
| **Control** | Full window better; discovery 11–15 strong |
| **Holdout** | **FAIL** (1h Hit075≈14%, lift negative; 4h also fails vs control) |
| **Status** | **`REJECTED_AS_STANDALONE`** |
| **Still useful** | **OB Context** (bid imbalance + OFI), **Flow Context** |
| **Retest when** | Longer holdout with frozen V1; entry-timing filters only via **new name** |

**Artifacts:** `results/frozen_rare_confluence_watchlist/20260811_17/` + frozen watchlist doc

---

## 10) `RARE_CONFLUENCE_QUIET_ENTRY_V2`

| Field | Value |
|---|---|
| **Name** | `RARE_CONFLUENCE_QUIET_ENTRY_V2` (**named follow-up; does not overwrite V1**) |
| **Window** | 2026-08-11 … 2026-08-17 only, 51 coins, `ob200_v3` — **no post-Aug17, no OI/Liq** |
| **Sources** | candles_1m, public_trades_canonical, OB; LLD context only (not a filter) |
| **Horizons** | **1h + 4h** (same path contract as V1) |
| **Definition** | Exact frozen V1 legs (`SHORT_RARE_IMB_DELTA_TPS_V1` / `LONG_RARE_IMB_OFI_DELTA_V1`) after V1 cooldown, **plus** a-priori quiet-entry gate only. V2 ⊆ cooldowned V1. **No retuning** of V1 quantiles/gates; no extra filters beyond A/B/C. |
| **Quiet variants (a-priori)** | **A:** `abs(event_minute_return) ≤ 0.15%` (best). **B:** `≤ 0.10%` (similar, smaller n). **C:** `≤ 2 × trailing_24h_median(|ret_1m|)` causal — too aggressive / rejected. |
| **Direction** | SHORT and LONG separately + combined |
| **V1 baseline (1h, this run)** | SHORT n=12 Hit075=25% ØMFE=0.5722% ØMAE=0.3307% ratio=1.7304; LONG n=21 Hit075=33.33% ØMFE=0.8586% ØMAE=0.5721% ratio=1.5007 |
| **Key metrics — Variant A (best)** | SHORT n=4 (holdout n=0): Hit075=50%, ØMFE=0.7061%, ØMAE=0.1282%, ratio=5.5063. LONG n=14 (holdout n=5): Hit075=35.71%, ØMFE=0.6966%, ØMAE=0.3227%, ratio=2.1586. **COMBINED n=18:** Hit075=38.89%, ØMFE=0.6987%, ØMAE=0.2795%, ratio=**2.50** vs V1 combined ratio **1.5577** (MAE ~**−20.5%** vs V1). |
| **Control** | Full-window better than matched controls for A (in-sample) |
| **Holdout** | **FAIL** (`holdout_holds=False`). LONG A holdout Hit075 lift negative vs controls; SHORT A has **no** holdout events |
| **Verdict** | **`RARE_CONFLUENCE_QUIET_ENTRY_V2_PARTIAL`** — **not watchlist, no alert, not confirmed** |
| **Status** | **`PARTIAL` / `CONTEXT_ONLY`** |
| **Still useful** | **Risk Warning / Timing Context:** if signal minute already moved hard, entry is likely late. Reuse quiet-entry as context in a **new named** candidate later; do not silently patch V1. Any threshold change → **`…_V3`**. |
| **Retest when** | Longer clean OB holdout with **exact** Variant A definition; or new name if gates change |

**Artifacts:**  
- `results/frozen_hard_tests/RARE_CONFLUENCE_QUIET_ENTRY_V2_20260811_17/`  
- Runner: `scripts/run_frozen_quiet_entry_v2.py`

**Interpretation:** Quiet-Entry improves V1 **in-sample** MAE and MFE/MAE, but **does not fix** the Aug16–17 holdout failure. Keep as timing risk feature, not as a released trigger.

---

## 11) Tier-A LONG-only High-Vol filter (`TIERA_LONG_ONLY_IN_HIGH_VOL_V1`)

| Field | Value |
|---|---|
| **Name** | `TIERA_LONG_ONLY_IN_HIGH_VOL_V1` |
| **Type** | **Filter on existing Tier-A signals** — **no new entry**, no retune of strategy params |
| **Source of Truth** | ClickHouse `signal_generator.signals` (`tier_a=1`) + `signal_outcomes`; versions `wave_fade_no_be50_v1` / `wave_fade_shadow_pipeline_v1` |
| **Outcome horizons** | `TRADE` and `TRADE_NO_BE50` (identical in this window; no BE50 activations) |
| **Window** | 2026-08-11 … 2026-08-17; 51-coin OB universe context; evaluate OB-resolved subset separately; **no OI/Liq** |
| **Rule** | **SHORT always passthrough.** LONG keep only if **high_vol** active. No other conditions. |
| **Variant A (primary)** | `vol_ratio_15m >= 1.257911` (= regime-audit window **p66 / high_vol**) |
| **Variant B (parallel a-priori)** | `vol_ratio_15m >= 1.0` (practical comparison; not optimized) |
| **Key metrics — A** | ALL WR **64.2% → 73.5%** (+9.4 pp). LONG **55.9% → 71.4%** (+15.5 pp, n_after=21). LONG OB **47.9% → 64.3%** (+16.4 pp, n_after=**14**). SHORT **74.5%** unchanged. Removed all LONG **18W/20L**; OB LONG **14W/20L**. LOO LONG lifts positive; n thin. ACE/XAU **not** sole driver. |
| **Key metrics — B** | ALL **+10.5 pp**; LONG OB **+22.1 pp**; more keep, simpler absolute threshold |
| **Prior related** | `FROZEN_TIERA_LONG_FILTER_VOL_RATIO_15M_V1` (PARTIAL; LONG `vol>=1` + OB/vol required) aligned with Variant B story; universal vol filter on ALL sides was **REJECTED** (hurts SHORT) |
| **Verdict** | **`TIERA_LONG_ONLY_IN_HIGH_VOL_V1_PARTIAL`** → status **`PARTIAL_WATCHLIST`** |
| **Status** | **`PARTIAL_WATCHLIST`** — **no live-confirm, no live-alert** |
| **Interpretation** | LONGs need high-vol / movement; SHORTs in this window should stay unfiltered. Currently **best practical Tier-A filter signal**, but PARTIAL due to small n and 7-day OB window |
| **Retest when** | New OB holdout **after 2026-08-20** with **exact** Variant A (and report B). Threshold change → new name `…_V2`. OI/Liq only as `…_OI_V1` |

**Artifacts:**  
- `results/frozen_signal_filter_tests/TIERA_LONG_ONLY_IN_HIGH_VOL_V1_20260811_17/`  
- Context: `results/frozen_signal_regime_audit/20260811_17/`, `results/frozen_signal_ob_feature_audit/20260811_17/`, `results/frozen_signal_filter_tests/FROZEN_TIERA_LONG_FILTER_VOL_RATIO_15M_V1_20260811_17/`  
- Watchlist doc: `docs/research/FROZEN_TIERA_FILTER_WATCHLIST.md`

---

## 12) `RANGE60_BREAKOUT_OB_V1` (from RANGE_CONSOLIDATION_BREAKOUT_V1 / A)

| Field | Value |
|---|---|
| **Name** | `RANGE60_BREAKOUT_OB_V1` (**frozen definition — do not silent-edit**) |
| **Source test (7d)** | `RANGE_CONSOLIDATION_BREAKOUT_V1` Variant **A RANGE60_PRIMARY** |
| **7d window** | 2026-08-11 … 2026-08-17, 51 coins — then PARTIAL_WATCHLIST freeze |
| **30d retest** | Exact V1 on `2026-07-19` … `2026-08-17` |
| **Definition** | Unchanged: 60m consolidation → clear break → aligned delta → TPS/spread/OB gates → 2/3 hold → next open. Exact text in watchlist doc. |
| **30d metrics** | n=**516**; ~**17.2**/day; Hit075 lift 60m **+5.44 pp**, 120m **+6.06 pp**; **MAE worse than control**; weeks **not stable** |
| **7d reference (historical)** | n=121; ~17.3/day; Hit075 +11.1 pp; MAE ok; holdout 16–17 only +0.2 pp |
| **Verdict (30d)** | **`REJECTED_AS_STANDALONE` / `CONTEXT_ONLY`** |
| **Status** | Building block for Regime V2 — **not** a standalone live/alert candidate |
| **Rule** | Any parameter change → **new name** (not an in-place V1 edit) |

**Artifacts:**  
- 7d: `results/frozen_hard_tests/RANGE_CONSOLIDATION_BREAKOUT_V1_20260811_17/`  
- 30d: `results/frozen_hard_tests/RANGE60_BREAKOUT_OB_V1_30D_20260719_0817/`  
- Watchlist doc: `docs/research/FROZEN_RANGE_BREAKOUT_WATCHLIST.md`

---

## 13) `RANGE60_BREAKOUT_OB_REGIME_V2`

| Field | Value |
|---|---|
| **Name** | `RANGE60_BREAKOUT_OB_REGIME_V2` (**new candidate** — V1 + a-priori regime gates) |
| **Best variant** | **C `VOL_PLUS_CONTINUATION`** |
| **Window** | 2026-07-19 … 2026-08-17, 51 coins |
| **Base** | Exact V1 30d signals; filter only |
| **Key metrics (C)** | ~**5.8**/day; Hit075 lift 120m **+10.15 pp**, 60m **+9.7 pp**; all **4 weeks** positive lift; W3 reduced; **SHORT > LONG** |
| **MAE** | Absolute MAE worse vs control; **MFE/MAE better** |
| **Verdict** | **`CONFIRMED_WATCHLIST`** |
| **Status** | Watchlist / research alert — **no live auto-trade** |

**Artifacts:** `results/frozen_hard_tests/RANGE60_BREAKOUT_OB_REGIME_V2_30D_20260719_0817/`

---

## 14) SHORT-C trade management (Regime V2 C SHORT)

| Field | Value |
|---|---|
| **Scope** | SHORT signals from Regime V2 variant C only |
| **Exit checked** | TP **0.75%** / SL **1.00%** / max **120m**, SL_FIRST |
| **n** | 76 (TP 40 / SL 19 / Timeout 17) |
| **Avg PnL @ 0.11% RT** | **+0.0132%** (thin); weeks **not stable** |
| **Higher TP** | `TP_NOT_THE_MAIN_PROBLEM` — higher TPs worse |
| **Verdict** | **`WATCHLIST_ONLY`** |

**Artifacts:** `…/RANGE60_BREAKOUT_OB_REGIME_V2_30D_20260719_0817/short_c_trade_management/`

---

## 15) `SHORT_C_STRONG_PRESSURE_V2`

| Field | Value |
|---|---|
| **Name** | `SHORT_C_STRONG_PRESSURE_V2` |
| **Best pre-entry** | **A `STRONG_FLOW`** (directed delta + tps_ratio + clear_break ≥ SHORT-C medians) |
| **A metrics** | n=**15**; SL **13.3%**; wr@0.11 **73%**; avg@0.11 **+0.191%**; tot/100 **+2.9**; removed worse than kept |
| **Weeks** | 4/4 positive @0.11% but **n too small** |
| **Variant C** | Better numerically but **`POST_ENTRY_CONFIRMATION`** — not a normal entry filter |
| **Verdict** | **`STRONG_PRESSURE_PARTIAL` / `PAPER_HYPOTHESIS`** |

**Artifacts:** `…/short_c_strong_pressure_v2/`

---

## Related ADAUSDT-only studies (context)

Not multi-coin frozen candidates; useful history for detector design.

| Study | Window | Notes | Status flavor |
|---|---|---|---|
| ADA smoke / 12–17 detector | Aug12–17 | Causal R≥2/3/5/10 episodes | Detector plumbing |
| ADA post-horizon | Aug12–17 | Larger horizon continuation candidate vs controls | `PARTIAL` / research |
| ADA continuation PnL | Aug12–17 & Jul–Aug | Live-like rules **fail OOS** | `REJECTED_AS_STANDALONE` |
| ADA 10x deepdive / Aug6 case | case studies | Chart alignment / late entry lessons | Case study only |

**Artifacts:** `results/volatility_event_detector/ADAUSDT_*`

---

## Feature / context cheat-sheet (reuse guide)

| Context bucket | Features that survived as useful | Do not use alone as alert |
|---|---|---|
| **Volatility** | `vol_ratio_*`, R≥5 episode flag, pre-range 5m/15m; **Tier-A LONG high_vol gate** (`PARTIAL_WATCHLIST`) | Yes as solo alert — OK as Tier-A LONG filter candidate only |
| **Activity** | `tps_ratio`, `tc_5m`, TPS top1/2% | Yes — unless rarified + OB |
| **Flow** | `delta_5m`, delta extremes, buy/sell share, exhaustion asymmetry | Yes — TPS3-scale spam |
| **OB** | `imb50_5m` extremes, `ofi_5m` extremes, depth asymmetry, `ob_ok`; range-breakout “OB not opposed”; **Regime V2 vol/continuation gates** (`CONFIRMED_WATCHLIST`) | Partial as solo — need confluence + regime |
| **Level / LLD** | `lld_dist_*`, near/touch/break; **60m consolidation range + touches** (`RANGE60_BREAKOUT_OB_V1` = context after 30d reject) | LLD-near alone failed; V1 standalone failed 30d |
| **Risk warning** | High MAE after R≥5 / high TPS; early adverse time-to-MAE; **`|ret_1m|` already large at signal close** (Quiet-Entry V2 lesson); SHORT-C fee fragility | Use as caution, not entry |

---

## Suggested next tests (no retune of V1 / no silent V2 retune)

1. **More OB days** after 2026-08-20: replay **exact** `SHORT_…_V1` and `LONG_…_V1` (new artifact folder; same names).
2. Optional: replay **exact Quiet-Entry Variant A** on longer holdout (`RARE_CONFLUENCE_QUIET_ENTRY_V2` unchanged); if gates change → **`…_V3`**.
3. **Tier-A filter holdout:** replay **exact** `TIERA_LONG_ONLY_IN_HIGH_VOL_V1` Variant A (`vol_ratio_15m >= 1.257911`) on OB days **after 2026-08-20**; report Variant B (`>= 1.0`) in parallel. No threshold edit without `…_V2`.
4. **Regime V2 / Strong Pressure holdout:** exact replay of `RANGE60_BREAKOUT_OB_REGIME_V2` C and `SHORT_C_STRONG_PRESSURE_V2` A on forward/holdout windows — **no** threshold edit without new names.
5. **`COIN_REGIME_SCANNER_V1`:** coin-level regime context (new candidate).
6. **OI/Liq overlap window** (from ~2026-08-18 16:00Z): new candidates only (`…_OI_V1`), never silently add OI to V1/V2.
7. **Horizons:** keep reporting **1h and 4h** (or 60m/120m/240m as used) for any new freeze; fee-aware exits where claiming tradeable.
8. **Combinations:** any merge of rejected/partial legs → **new candidate name** + discovery → frozen hard-test → holdout.

---

## Artifact index

| Path | Contents |
|---|---|
| `docs/research/ORDERBOOK_RESEARCH_SIGNAL_INVENTORY.md` | This inventory |
| `docs/research/FROZEN_RARE_CONFLUENCE_WATCHLIST.md` | Frozen rare-confluence V1 definitions |
| `docs/research/FROZEN_TIERA_FILTER_WATCHLIST.md` | Frozen Tier-A filter watchlist (LONG high-vol) |
| `docs/research/FROZEN_RANGE_BREAKOUT_WATCHLIST.md` | Range / regime / SHORT-C watchlist record |
| `results/frozen_hard_tests/RANGE_CONSOLIDATION_BREAKOUT_V1_20260811_17/` | 7d source PARTIAL (Variant A → V1 name) |
| `results/frozen_hard_tests/RANGE60_BREAKOUT_OB_V1_30D_20260719_0817/` | V1 30d standalone (**REJECTED_AS_STANDALONE**) |
| `results/frozen_hard_tests/RANGE60_BREAKOUT_OB_REGIME_V2_30D_20260719_0817/` | Regime V2 (**CONFIRMED_WATCHLIST**) |
| `…/short_c_trade_management/` | SHORT-C TP/SL diagnostics (**WATCHLIST_ONLY**) |
| `…/short_c_strong_pressure_v2/` | Strong pressure filter (**PAPER_HYPOTHESIS**) |
| `results/frozen_hard_tests/BREAKOUT_CONTINUATION_STRICT_V1_20260811_17/` | Prior broad breakout strict (REJECTED on frequency) |
| `results/level_break_reclaim_discovery/20260811_17_51coins/` | Level break/reclaim discovery lineage |
| `results/frozen_precondition_hard_test/flow_tps3_20260811_17/` | TPS3 A/B reject |
| `results/big_move_precondition_discovery/20260811_17_51coins/` | Big-move discovery |
| `results/rare_confluence_discovery/20260811_17_51coins/` | Rare confluence discovery |
| `results/frozen_hard_tests/SHORT_RARE_IMB_DELTA_TPS_V1_20260811_17/` | SHORT V1 1h hard-test |
| `results/frozen_rare_confluence_watchlist/20260811_17/` | SHORT+LONG 1h/4h freeze cycle |
| `results/frozen_hard_tests/RARE_CONFLUENCE_QUIET_ENTRY_V2_20260811_17/` | Quiet-Entry V2 hard-test (PARTIAL) |
| `results/frozen_signal_ob_feature_audit/20260811_17/` | Tier-A OB feature audit |
| `results/frozen_signal_regime_audit/20260811_17/` | Tier-A regime audit |
| `results/frozen_signal_filter_tests/FROZEN_TIERA_LONG_FILTER_VOL_RATIO_15M_V1_20260811_17/` | Prior LONG vol≥1 filter (PARTIAL) |
| `results/frozen_signal_filter_tests/TIERA_LONG_ONLY_IN_HIGH_VOL_V1_20260811_17/` | LONG high-vol filter (PARTIAL_WATCHLIST) |
| `results/volatility_event_detector/` | ADA detector / continuation lineage |
| *(deleted after reject)* | `results/condition_1h_moves/*`, `MULTI_20260811_19_*` — see sections 1–2 & 5 |

---

## Changelog

| Date (UTC) | Note |
|---|---|
| 2026-08-20 | Initial inventory compiled from remaining artifacts + documented deleted rejects |
| 2026-08-20 | Added `RARE_CONFLUENCE_QUIET_ENTRY_V2` (PARTIAL / CONTEXT_ONLY; Variant A best; holdout fail; no alert) |
| 2026-08-20 | Added `TIERA_LONG_ONLY_IN_HIGH_VOL_V1` (`PARTIAL_WATCHLIST`); new `FROZEN_TIERA_FILTER_WATCHLIST.md` |
| 2026-08-21 | Added `RANGE60_BREAKOUT_OB_V1` (`PARTIAL_WATCHLIST`); new `FROZEN_RANGE_BREAKOUT_WATCHLIST.md` |
| 2026-08-21 | 30d V1 → `REJECTED_AS_STANDALONE`/`CONTEXT_ONLY`; Regime V2 `CONFIRMED_WATCHLIST`; SHORT-C TM `WATCHLIST_ONLY`; Strong Pressure `PAPER_HYPOTHESIS` |
