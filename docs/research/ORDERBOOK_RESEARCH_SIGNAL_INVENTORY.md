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
| `PARTIAL` | Interesting in-sample or discovery, but incomplete / fragile |
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

ADAUSDT-only detector / continuation studies (Jul–Aug) are listed under **Related ADAUSDT studies** — not multi-coin production candidates.

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
| **Volatility** | `vol_ratio_*`, R≥5 episode flag, pre-range 5m/15m | Yes — range both ways |
| **Activity** | `tps_ratio`, `tc_5m`, TPS top1/2% | Yes — unless rarified + OB |
| **Flow** | `delta_5m`, delta extremes, buy/sell share, exhaustion asymmetry | Yes — TPS3-scale spam |
| **OB** | `imb50_5m` extremes, `ofi_5m` extremes, depth asymmetry, `ob_ok` | Partial — need confluence + holdout |
| **Level / LLD** | `lld_dist_*`, near/touch/break | Yes — standalone touch failed |
| **Risk warning** | High MAE after R≥5 / high TPS; early adverse time-to-MAE | Use as caution, not entry |

---

## Suggested next tests (no retune of V1)

1. **More OB days** after 2026-08-20: replay **exact** `SHORT_…_V1` and `LONG_…_V1` (new artifact folder; same names).
2. **OI/Liq overlap window** (from ~2026-08-18 16:00Z): new candidates only (`…_OI_V1`), never silently add OI to V1.
3. **Horizons:** keep reporting **1h and 4h** for any new freeze; 4h MAE tails matter.
4. **Combinations:** any merge of rejected legs → **new candidate name** + discovery → frozen hard-test → holdout.

---

## Artifact index

| Path | Contents |
|---|---|
| `docs/research/ORDERBOOK_RESEARCH_SIGNAL_INVENTORY.md` | This inventory |
| `docs/research/FROZEN_RARE_CONFLUENCE_WATCHLIST.md` | Frozen V1 definitions |
| `results/frozen_precondition_hard_test/flow_tps3_20260811_17/` | TPS3 A/B reject |
| `results/big_move_precondition_discovery/20260811_17_51coins/` | Big-move discovery |
| `results/rare_confluence_discovery/20260811_17_51coins/` | Rare confluence discovery |
| `results/frozen_hard_tests/SHORT_RARE_IMB_DELTA_TPS_V1_20260811_17/` | SHORT V1 1h hard-test |
| `results/frozen_rare_confluence_watchlist/20260811_17/` | SHORT+LONG 1h/4h freeze cycle |
| `results/volatility_event_detector/` | ADA detector / continuation lineage |
| *(deleted after reject)* | `results/condition_1h_moves/*`, `MULTI_20260811_19_*` — see sections 1–2 & 5 |

---

## Changelog

| Date (UTC) | Note |
|---|---|
| 2026-08-20 | Initial inventory compiled from remaining artifacts + documented deleted rejects |
