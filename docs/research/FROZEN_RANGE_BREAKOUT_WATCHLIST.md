# Frozen Range Breakout Watchlist

**Status date (UTC):** 2026-08-21  
**Branch:** `research/confirmed-orderbook-entries`  
**Repo:** `orderbook_analyse`  
**Related inventory:** `docs/research/ORDERBOOK_RESEARCH_SIGNAL_INVENTORY.md` (§12)

## Purpose

Freeze one consolidation → breakout continuation candidate as a **research watchlist**.

- **Not** live-confirm  
- **Not** live-alert  
- **Not** CONFIRMED  
- **No** silent retuning of thresholds  

| Candidate | Status |
|---|---|
| `RANGE60_BREAKOUT_OB_V1` | **`PARTIAL_WATCHLIST`** |

## Source artifact

| Kind | Path |
|---|---|
| Hard-test (source) | `results/frozen_hard_tests/RANGE_CONSOLIDATION_BREAKOUT_V1_20260811_17/` |
| Source verdict | `RANGE_CONSOLIDATION_BREAKOUT_V1_PARTIAL` |
| Best variant frozen | **A `RANGE60_PRIMARY`** → renamed for watchlist as **`RANGE60_BREAKOUT_OB_V1`** |
| Runner (research) | `research/frozen_hard_test_RANGE_CONSOLIDATION_BREAKOUT_V1.py` |

Lineage (do not revive as this freeze):

- `results/level_break_reclaim_discovery/20260811_17_51coins/` — discovery
- `results/frozen_hard_tests/BREAKOUT_CONTINUATION_STRICT_V1_20260811_17/` — broad breakout; **REJECTED** (frequency)

## Data window (primary)

- **Evaluation:** `2026-08-11T00:00:00Z` … `2026-08-17T23:59:59Z`
- **Universe:** 51 coins with full `ob200_v3` / `depth=200` coverage
- **Sources:** `candles_1m`, `public_trades_canonical`, `orderbook_features_1s_v2`
- **LLD / pools:** context / descriptive slice only — **not** part of the frozen rule
- **OI / liquidations:** **not used**

## Status and metrics (Variant A → `RANGE60_BREAKOUT_OB_V1`)

| Metric | Value |
|---|---|
| Status | **`PARTIAL_WATCHLIST`** |
| n | 121 |
| Signals/day | ~17.3 |
| Hit075 lift (1h vs control) | **+11.1 pp** |
| MAE vs control | **ok** (MAE ≤ control; MFE/MAE also better) |
| Holdout 16–17 Hit075 lift | **+0.2 pp** |
| Top2 coin share | ~10% |
| Without Top2 Hit075 lift | **+10.9 pp** |
| Side | **LONG better than SHORT** |
| OB vs NO_OB (sibling C) | OB helps lightly (higher lift, slightly rarer; C holdout negative) |

### Explicitly **not** frozen from the same hard-test

| Sibling | Why not watchlisted |
|---|---|
| B `RANGE120_STRICT` | Negative Hit075 lift / holdout fail |
| C `RANGE60_NO_OB` | Weaker lift; holdout negative |
| D `RANGE60_COMPRESSION` | Weaker lift; holdout fail |
| LLD-near as filter | Near-LLD slice worse than not-near |

## Frozen definition (`RANGE60_BREAKOUT_OB_V1`)

Causal rules; **do not edit in place**.

Shared:

- Range and features only from data **before** the break minute
- Signal decision at **hold/confirm minute close** (break + 3 after evaluating 3 follow minutes)
- **Entry** = next 1m **open** after confirmation
- Measure **60m and 240m** MFE / MAE / return / hits after entry
- Cooldown used in source test: **60m** per coin per side (keep on retest)

### Range / consolidation (60m)

1. `range_high` / `range_low` = max(high) / min(low) over the **prior 60 minutes** (exclusive of break minute)
2. `range_width` = `(range_high - range_low) / mid`
3. `range_width` in trailing 24h **p20–p70** for that symbol (causal trailing)
4. No strong trend in range: `abs(return_window) <= 0.40 * range_width`; close position not stuck at extremes
5. ≥ **2** touches/rejections on the breakout edge (upper for LONG, lower for SHORT), spaced ≥ **5** minutes; touch tolerance fixed (source: 7.5 bps or 10% of range width)

### Breakout + flow + OB

**LONG**

- Break-minute close **above** `range_high` with clear break ≥ **5 bps** or ≥ **10%** of `range_width`
- `delta_3m > 0` and `delta_5m > 0` (through confirm)
- Break/hold-zone `trade_count` ≥ trailing 24h **p90**
- `spread` ≤ trailing 24h **p75**
- OB not opposed: `OFI_5m >= 0` **or** `imbalance_l50 >=` trailing 24h median

**SHORT** (mirror)

- Close **below** `range_low` with same clear-break rule
- `delta_3m < 0` and `delta_5m < 0`
- Same TPS / spread gates
- OB not opposed: `OFI_5m <= 0` **or** `imbalance_l50 <=` trailing 24h median

### Hold + entry protection

- At least **2 of 3** follow minutes close **outside** the range (above high / below low)
- Confirm-minute directed `ret_1m` ≤ **0.30%** (late entries may be flagged for analysis; frozen primary is the on-time gate as in source A)

## Why PARTIAL, not CONFIRMED

1. Holdout Aug16–17 is only **barely** positive (+0.2 pp Hit075 lift) — not robust enough for CONFIRMED.
2. Primary clean OB sample is still **7 days / 51 coins**; n=121 is usable but small for promotion.
3. Sibling variants (120m, compression, NO_OB) did **not** confirm the edge — watchlist is specifically the **60m + OB** stack.

Acceptance that **did** pass in-sample for A: ≤20 signals/day, Hit075 lift ≥ +10 pp, MFE > control, MAE ok, not 1–2-coin driven.

## Retest rule

When the OB-V3 **30d import** for `2026-07-19` … `2026-08-17` is complete, retest **exactly** this definition:

- Same name: `RANGE60_BREAKOUT_OB_V1`
- Same gates / hold / entry / metrics / matched-control contract
- New artifact folder (do not overwrite 11–17 source)
- Report full / discovery-holdout split appropriate to the longer window
- **No** parameter changes on this name

## No-retune rule

| Change | Required action |
|---|---|
| Any threshold / window / OB gate / touch / hold edit | **New candidate name** (e.g. `RANGE60_BREAKOUT_OB_V2`) |
| Add LLD / OI / Liq / compression / 120m | **New name**, not an edit of V1 |
| Quiet-entry or late-entry filter as hard gate | **New name** |
| Watchlist → alert | Requires CONFIRMED-level acceptance on longer holdout with **exact** V1 |

## Hard rules

1. Watchlist ≠ live alert.  
2. Do not commit runners/results as the freeze — this doc + inventory are the freeze record.  
3. Do not touch OB-30d import / collectors for documentation commits.  

## Changelog

| Date (UTC) | Note |
|---|---|
| 2026-08-21 | Initial freeze: `RANGE60_BREAKOUT_OB_V1` from `RANGE_CONSOLIDATION_BREAKOUT_V1` Variant A (`PARTIAL_WATCHLIST`) |
