# PHASE BT-D50 — MANUAL REFERENCE UPPER-POOL ZONE FORENSICS

**Date:** 2026-10-26  
**Verdict:** `MANUAL_UPPER_ZONE_FORENSICS_PASS`

**Scope:** Read-only. No strategy, BT-D46/D47, dashboard, density SSOT, or PnL changes.

**Binding reference:** DOGEUSDT · 5m · PD1 · Depth 2 · 2026-09-11 UTC  
**Manual upper zone:** 0.08410 – 0.08436 (entry 0.08410, SL 0.08436 × 1.002 = **0.08452872**)  
**User signal times:** 12:25 UTC, 12:35 UTC

**Artifacts:** `results/phase_bt_d50_manual_upper_zone_v1/`  
**Tool:** `tools/phase_bt_d50_manual_upper_zone_v1/run_forensics.py` (replay via `density_bridge_v1`, same structural eligibility as D26/D46)

---

## Executive answer

BT-D46 did **not** “skip” the user’s 0.08410–0.08436 zone in favor of an arbitrary smaller pool inside it at those clock times. Instead:

1. **12:25 UTC** is a **fill/TP bar** for a pending created at **12:05** on the **lowest eligible** pool **0.08384–0.083844**, which lies **below** the manual zone. At 12:25 the manual interval is already visible in density as a **27-band cluster** (envelope **0.08409–0.084362**), but the open order predates that focus and used the then-lowest band.

2. **12:35 UTC** is a **fill/SL bar** for a pending from **12:30** on **0.08408–0.08409** — the **lowest eligible** band at 12:30, **0.00002 below** user entry 0.08410 and **not overlapping** the closed manual interval. The user’s broader zone is a **chart composite** of many canonical bands; the selector always picks **one atomic** band by minimum `pool_lower`.

---

## A. Exact signal identification

### 12:25 UTC — position `78cd59a534af80d5`

| Field | Value |
|-------|--------|
| **User timestamp meaning** | Simulated **entry + TP** same bar (`chronological_events.csv` rows 9–10) |
| **Decision close (pool choice)** | **2026-09-11T12:05:00Z** |
| **Pending order** | `57c97bbd342e9817bcf7` (created 12:05, active from **12:10**, kept until fill) |
| **Prior pending** | `3d12593059144e5ea010` cancelled 12:05 (`POOL_NOT_ACTIVE`) |
| **Position ID** | `78cd59a534af80d5` |
| **Modeled entry bar** | **12:25** (touch fill `CAUSAL_V2_LIMIT_TOUCH_INTRABAR`) |
| **Upper pool** | **0.08384 – 0.083844** |
| **Origin IDs** | `lldv2:DOGEUSDT:5m:upper:1789120800`, `lldv2:DOGEUSDT:5m:upper:1789128000` |
| **Entry / SL / TP** | **0.08384 / 0.084011688 / 0.08327** |
| **Selection** | `RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST` |
| **Exit** | TP @ 0.08327 same bar |

At **12:25 decision close**, scanner state in `setups.json` still showed **TRACKED_CANDIDATE_UNCHANGED** on the same 0.08384 pool (`emit_setup: false`) — no new signal at 12:25.

### 12:35 UTC — position `aa1a8a9c1bc83efc`

| Field | Value |
|-------|--------|
| **User timestamp meaning** | Simulated **entry + SL** same bar (rows 13–14) |
| **Decision close (pool choice)** | **2026-09-11T12:30:00Z** |
| **Pending order** | `98356165f844199aa699` (created 12:30, active **12:35**) |
| **Position ID** | `aa1a8a9c1bc83efc` |
| **Modeled entry bar** | **12:35** |
| **Upper pool** | **0.08408 – 0.08409** |
| **Origin IDs** | `1789100400`, `1789116300`, `1789116600`, `1789129500` (lldv2 upper origins) |
| **Entry / SL / TP** | **0.08408 / 0.08425818 / 0.08293** |
| **Selection** | `RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST` (`emit_setup: true` at 12:30) |
| **Exit** | SL @ 0.08425818 same bar |

At **12:35 close** (after rally to **0.08488**), a **new** lowest eligible pool would be **0.08542–0.08545** (`setups.json` 12:35) — but the fill is from the **12:30** pending, not a fresh 12:35 selection.

**Detail:** `signal_1225.json`, `signal_1235.json`, D47 `positions_audit.csv`, `chart_overlay_events.json`.

---

## B. Manual reference pool (density replay)

Warmup: full BT-D26 backtest window (≥168h before 2026-09-11). Causal `collect_at_close` only.

### Questions 1–8 (12:25 and 12:35 landscapes; 12:30 for 12:35 trade)

| # | 12:25 | 12:35 |
|---|-------|-------|
| **1. One contiguous canonical upper pool?** | **No** | **No** |
| **2. Union of neighboring bands?** | **Yes — 27** structurally eligible bands overlap [0.08410, 0.08436] | **Yes — 3** bands overlap interval but **none eligible** (price above) |
| **3. Overlap / density cluster?** | **Yes** — shared origin sets across 0.08409–0.084362 | Cluster **truncated**; close **0.08488** > zone |
| **4. Origin IDs** | See `origin_memberships.csv` (e.g. core quartet `1789100400`, `1789115100`, `1789116300`, `1789116600` on 0.08409–0.08411) | Bands use `1789048800`, `1789069200`, `1789071300`, … |
| **5. ACTIVE origin counts** | **4–10** per band in cluster | **3–5** per overlapping band |
| **6. Precise canonical edges in manual interval** | Envelope **0.08409 – 0.084362** (not 0.08410 – 0.08436) | Overlap envelope **0.08429 – 0.08436** only |
| **7. Change 12:25 → 12:35?** | Cluster **stable** at 12:25–12:30 (28 bands); **12:35** price invalidates sub-0.08488 bands | |
| **8. Structural eligibility (manual envelope as pool)?** | Individual bands **eligible** when close below them; **no** single band equals user box | At 12:35, overlapping bands **fail** `POOL_NOT_ABOVE_PRICE` |

**Canonical vs user boundaries (explicit):**

- User lower **0.08410** ≠ canonical cluster floor **0.08409** (band 0.08409–0.08411 straddles the edge).
- User upper **0.08436** ≈ canonical **0.084362** on band 0.084334–0.084362 (within float tick).

Full band lists: `manual_zone_08410_08436.json`.

---

## C. Small pool vs relevant zone

### 12:25 (fill bar context)

| | BT-D46 actual | User manual | Notes |
|---|---------------|-------------|--------|
| Lower / upper | 0.08384 / 0.083844 | 0.08410 / 0.08436 | **No overlap** |
| Width | ~0.000004 | 0.00026 | |
| Origins | 2 | Cluster: many | |
| Lowest eligible @ 12:25 | **0.08384** (selected earlier) | First in-zone band **0.08409** | Gap **~0.00025** below manual cluster |
| Reason | Tracked pending from 12:05 | N/A | Not a same-bar re-rank |

### 12:35 (fill bar context; decision **12:30**)

| | BT-D46 actual | User manual | Intermediate |
|---|---------------|-------------|----------------|
| Lower / upper | **0.08408 / 0.08409** | 0.08410 / 0.08436 | **0.08409 / 0.08411** first band inside manual overlap |
| Origins | 4 | Cluster 27 bands @ 12:30 | Same 4-origin set on 0.08409–0.08411 (+ `1789115100`) |
| Overlaps manual? | **No** (upper 0.08409 < 0.08410) | — | **Yes** (partial) |
| Selection | Lowest eligible **pool_lower** | Composite envelope | **0.00001** rank gap |

`selected_vs_manual.csv` summarizes counterfactual TPs at fill bars.

---

## D. Entry / SL / TP (counterfactual, existing TP rule)

TP: `select_short_tp_depth2` / `next_eligible_band` + `min_distance_pct` (unchanged).

| Reference time | Actual Entry / SL / TP | Manual ref Entry / SL / TP |
|----------------|------------------------|----------------------------|
| **12:25** (landscape @ fill) | 0.08384 / 0.084011688 / **0.08327** | 0.08410 / **0.08452872** / **0.08293** |
| **12:35** (landscape @ fill) | 0.08408 / 0.08425818 / **0.08293** | 0.08410 / **0.08452872** / **0.08223** |

Manual SL uses user formula on **0.08436**, not on canonical envelope top **0.084362**.

---

## E. Research question — why the user sees a broader zone

Supported explanations (with evidence):

1. **Dense overlap of origin bands** — 27–28 atomic upper bands share origins across 0.08409–0.084362 (`origin_memberships.csv`).
2. **Larger price-level envelope** — user box ≈ visual shelf; SSOT emits **micro-bands** per depth-2 unique-origin slice.
3. **Persistence** — cluster present from at least 12:05 through 12:30 with slowly growing origin counts (e.g. +`1789129500` at 12:30).
4. **Ranking by lowest eligible edge only** — **0.08408–0.08409** wins at 12:30 despite sitting **just under** the user’s mental floor 0.08410.
5. **Split representation** — not one merge ID; many `pool_key` rows.

**Not** supported: BT-D46 picking a random small pool **inside** the manual box at 12:25 (trade was **below** the box). At 12:35 the trade is **adjacent below**, not interior.

---

## F. Reference decision (side-by-side)

### 12:25 UTC

**USER_MANUAL_REFERENCE:** Upper **0.08410 – 0.08436** (composite).  
**BACKTESTER_ACTUAL_SELECTION:** **0.08384 – 0.083844** (`1789120800|1789128000`) — pending from **12:05**, filled **12:25**.

- Same broader structural zone? **No** for the executed trade; manual cluster exists **above** the fill pool.
- Manual zone continuously present? **Yes** in density at 12:25 (27-band cluster).
- User expecting composite vs atomic band? **Consistent with composite** interpretation.
- Conceptual change to match user zone? **Cluster/envelope selection** or **minimum lower ≥ user floor** — not lowest global eligible.

### 12:35 UTC

**USER_MANUAL_REFERENCE:** Upper **0.08410 – 0.08436**.  
**BACKTESTER_ACTUAL_SELECTION:** **0.08408 – 0.08409** (4 origins) — decided **12:30**, filled **12:35**.

- Same broader zone? **Adjacent below** the manual interval; inside the **visual** shelf but not overlapping canonical manual bounds.
- Manual zone continuously present? **Yes** at 12:30; **degraded** at 12:35 close (ineligible under price).
- Composite vs atomic? User box ≈ union; algorithm = **single lowest band**.
- Conceptual change? Same as above; additionally **pending lifecycle** decouples fill bar from latest scanner pick (12:35 would be 0.08542).

---

## G. Deliverables

| Path | Status |
|------|--------|
| `reports/PHASE_BT_D50_MANUAL_UPPER_ZONE_REFERENCE_V1.md` | This file |
| `results/phase_bt_d50_manual_upper_zone_v1/signal_1225.json` | ✓ |
| `results/phase_bt_d50_manual_upper_zone_v1/signal_1235.json` | ✓ |
| `results/phase_bt_d50_manual_upper_zone_v1/manual_zone_08410_08436.json` | ✓ |
| `results/phase_bt_d50_manual_upper_zone_v1/selected_vs_manual.csv` | ✓ |
| `results/phase_bt_d50_manual_upper_zone_v1/origin_memberships.csv` | ✓ |
| `results/phase_bt_d50_manual_upper_zone_v1/structural_comparison.md` | ✓ |

---

## Verdict

**`MANUAL_UPPER_ZONE_FORENSICS_PASS`** — Causal density replay matches Research Chart SSOT; BT-D46 trades identified; manual zone reconstructed as a **multi-band cluster** (not one pool); divergence explained by **lowest-eligible atomic band ranking**, **pending timing**, and **user composite vs canonical band granularity**. No implementation changes in BT-D50.
