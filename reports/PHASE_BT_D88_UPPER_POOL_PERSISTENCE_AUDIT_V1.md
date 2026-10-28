# PHASE BT-D88 — Upper Pool Persistence and Origin Evolution Audit

**Baseline:** BT-D87 `WEAK_UPPER_POOL_ANALYSIS_PASS` · `031ea67e` · BT-D85 July research  
**Mode:** Read-only · no strategy, filter, CH write, or backtest changes  
**Date:** 2026-10-28  

## Verdict

**`POOL_PERSISTENCE_AUDIT_PASS`**

All 25 trades analyzed with past-only July D8 cache walks. Historical separation from user visual labels is **partial** — some persistence features align with weak picks, but **winning trades also show short-lived and sudden bands**. Pre–July-1 evolution remains **unobservable** in this bundle.

---

## Sources and limits

| Source | Use | Provenance |
|--------|-----|------------|
| `trade_ledger_no_pause.json` | 13 + 12 cohort, signal closes | BT-D85 |
| `cache_bundle/` (8928 × 5m closes, Jul 2026) | ACTIVE upper bands at each `decision_close_utc` | BT-D8 / BT-D85 build |
| `origin_ids` epoch suffix | Segment open time (5m grid), past-only age proxy | lldv2 naming (see BT-D3/4X-AA) |
| ClickHouse | **Not queried** | Candles-only adapter; no band timeline table used |

**Window:** `max(signal − 168h, 2026-07-01T00:00:00Z)` — per research 168h context rule, capped by bundle start.

**Blind spot:** No snapshots before **2026-07-01**; long-lived pools that existed only in June are invisible. Early-July signals have truncated in-bundle history.

**Tool:** `tools/phase_bt_d88_upper_pool_persistence_audit_v1/run_bt_d88.py`  
**Artifacts:** `results/phase_bt_d88_upper_pool_persistence_audit_v1/`

---

## Band matching methodology (past-only)

1. **Reference** at `signal_close_utc`: ledger `pool_key` (lower|upper) on the ACTIVE upper band (same as BT-D87).
2. **Historical track** (each prior 5m close, newest → oldest):
   - Prefer **exact** `pool_key`;
   - else best ACTIVE upper with **≥50% price overlap** with reference band (merge/split tolerant).
3. **Stop** at window start; never use closes **after** signal.
4. **Confidence:** all 25 references matched with `exact_pool_key` at signal.

Metrics are computed on the **price-tracked** chain unless noted as `exact_pool_key` bars.

**Not claimed:** stable band identity across merges — `boundary_pool_key_changes_while_tracked` counts key changes along the overlap chain.

---

## Group summary (medians)

| Metric | User-selected 13 | Other 12 | Notes |
|--------|------------------|----------|-------|
| Price-tracked persistence (5m bars) | **10** | 28 | Selected tend **shorter** in-window overlap chains |
| Exact `pool_key` bars | 1 | 1 | Ledger key often **new at signal** for both groups |
| Hours signal − first tracked in window | 47.1 | 103.5 | Selected bands **re-enter** overlap window closer to signal |
| Sudden-appearance heuristic* | 4 trades | 2 trades | Not exclusive to user picks |
| Origin membership change bars (median) | 3 | 3.5 | Similar churn |
| Mean origin segment age at signal (h) | 0.13 | 0.23 | All **very fresh segments** (5–35 min); weak separation |
| Nearby uppers (50 bp mean while tracked) | 1.95 | 1.79 | Similar local upper density |

\*Sudden = ≥12 consecutive 5m bars without tracked band immediately before signal, and ≤3 tracked bars total (descriptive only; **not** a filter).

**Large gap to next upper (BT-D87):** Not used as a negative signal per existing strategy rule. This audit does not re-score gap.

---

## Per-trade evidence (user-selected 13)

| Signal UTC | Exit | Tracked bars | Exact-key bars | First tracked in window | Sudden? | Mean origin age (h) |
|------------|------|--------------|----------------|-------------------------|---------|---------------------|
| 2026-07-08T07:15:00Z | SL | 1 | 1 | 07:15 (signal) | **Yes** | 0.25 |
| 2026-07-09T03:00:00Z | SL | 43 | 1 | 07-08 12:10 | No | 0.13 |
| 2026-07-12T01:10:00Z | SL | 21 | 1 | 07-09 05:55 | No | 0.13 |
| 2026-07-12T06:55:00Z | SL | 10 | 5 | 07-09 05:50 | No | 0.21 |
| 2026-07-13T04:20:00Z | SL | 44 | 1 | 07-08 12:25 | No | 0.13 |
| 2026-07-17T09:15:00Z | SL | 1 | 1 | 09:15 (signal) | **Yes** | 0.22 |
| 2026-07-24T00:50:00Z | SL | 1 | 1 | 00:50 (signal) | **Yes** | 0.13 |
| 2026-07-24T12:00:00Z | **TP** | 20 | 1 | 07-23 18:35 | No | 0.13 |
| 2026-07-24T17:10:00Z | SL | 4 | 1 | 07-24 15:05 | No | 0.17 |
| 2026-07-27T15:05:00Z | SL | 3 | 1 | 07-25 16:00 | **Yes** | 0.13 |
| 2026-07-28T04:55:00Z | SL | **337** | 1 | 07-24 09:50 | No | 0.13 |
| 2026-07-31T03:45:00Z | **TP** | **453** | 20 | 07-24 07:15 | No | 0.33 |
| 2026-07-31T10:55:00Z | SL | 10 | 7 | 07-24 12:10 | No | 0.67 |

Full CSV: `per_trade_persistence.csv`.

---

## Counterexamples (comparison group)

| Signal UTC | Exit | Tracked bars | Sudden? | Note |
|------------|------|--------------|---------|------|
| 2026-07-07T09:05:00Z | SL | **503** | No | Long overlap from Jul 1 — not user-flagged |
| 2026-07-23T14:30:00Z | **TP** | 1 | **Yes** | TP on **signal-only** band |
| 2026-07-23T16:30:00Z | **TP** | 1 | **Yes** | Same pattern |
| 2026-07-01T07:15:00Z | **TP** | 4 | No | Short tracked window but **TP** |

---

## Answers

### 1. Are user-marked weak bands younger or less persistent?

**Often shorter in-window overlap chains** (median 10 vs 28 price-tracked bars) and **more likely** to match the sudden-appearance heuristic (4/13 vs 2/12). **Not uniformly:** two user-selected **TP** trades show **453** and **337** tracked bars (Jul 31 and Jul 28). Exact ledger `pool_key` is usually **only present at the signal bar** (median 1 bar) for **both** groups — persistence shows up in **price-region** tracking, not stable keys.

### 2. Do good trades also use short-lived bands?

**Yes.** Comparison includes **two TP** trades with **1 bar** tracked and sudden-appearance **true** (Jul 23). User-selected **TP** trades include both short (Jul 24 12:00) and **very long** (Jul 31 03:45) overlap histories.

### 3. Does pool evolution add separation beyond width / origin count (BT-D87)?

**Moderately, not decisively.** Useful added signals:
- **In-window overlap duration** and **recency of first track** before signal;
- **Sudden re-appearance** (descriptive heuristic);
- **Boundary / origin churn** along overlap chain (similar medians across groups).

**Origin segment age** from `origin_ids` (5m epoch) stays **sub-hour** at signal for all trades — does **not** separate groups strongly. Width/origin-count from BT-D87 already overlapped; evolution explains **some** user picks (signal-only bands) but **not all**.

### 4. What remains unproven?

- Behavior **before 2026-07-01** (bundle floor).
- True origin **lifetime** vs segment **candle open** timestamp in IDs.
- Merge/split event log beyond inferred key changes.
- Whether **isolated large gap to next upper** reflects weak vs strong **when** overlap history is long (strategy already requires gap — not audited as negative).
- Any causal link between persistence metrics and SL rate.

---

## Performance

Single pass over **≤8928** cache entries (LRU, ~22s wall time for 25 × ~2017-bar windows). No month recomputation, no CH import.

---

## Safety

No entry/SL/TP/EMA/density/filter/dashboard changes · no CH writes · no new July backtest · **no git push**.

---

## Commits

| Artifact | Role |
|----------|------|
| `tools/phase_bt_d88_upper_pool_persistence_audit_v1/run_bt_d88.py` | Analyzer |
| `results/phase_bt_d88_upper_pool_persistence_audit_v1/*` | CSV + JSON |
| `reports/PHASE_BT_D88_UPPER_POOL_PERSISTENCE_AUDIT_V1.md` | This report |
