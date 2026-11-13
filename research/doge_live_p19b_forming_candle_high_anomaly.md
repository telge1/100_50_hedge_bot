# DOGE-LIVE-P19B – Forming 5m candle high anomaly (0.094940)

**Date:** 2026-11-13  
**Screenshot:** ~09:41 UTC · Legend O/H/L/C ≈ 0.064730 / **0.094940** / 0.064560 / 0.064700  
**Verdict:** **CONFIRMED frontend merge + backend forming-tip path** (not live CH/collector at probe time)  
**Browser:** **UNVERIFIED** after fix (no commit/push)

## 1. Exact bucket (inferred)

| Field | Value |
|-------|--------|
| **5m open (UTC)** | 2026-11-13 **09:40:00** (`time = 1794562800`) |
| Alt. prior bucket | 09:35 (`1794562500`) — legend matches **09:40** forming tip (O≈0.06473) |

## 2. Forensic OHLC comparison (probe @ ~12:45 UTC)

| Source | Open | High | Low | Close | Note |
|--------|------|------|-----|-------|------|
| **CH 1m aggregate → 5m partial** (09:40–09:45) | 0.06473 | **0.06473** | 0.06466 | 0.06470 | Canonical |
| **Pane `resolve_candle_pack` 5m** (09:35–09:45) | 0.06473–0.06474 | **0.06473–0.06484** | 0.06466–0.06469 | 0.06470–0.06473 | No `0.094940` |
| **`fetch_forming_candle` (collector)** | 0.06470 | **0.06470** | 0.06470 | 0.06470 | Live probe |
| **CH fallback 15m** | 0.06458 | 0.06484 | 0.06456 | 0.06470 | Not used when collector OK |
| **CH fallback 5m** | 0.06473 | 0.06473 | 0.06466 | 0.06470 | — |
| **Screenshot / user** | 0.064730 | **0.094940** | 0.064560 | 0.064700 | **Anomaly** |
| **Chart renderer** | — | — | — | — | Same object as frontend `pendingData` |

**Search:** No `0.09494` in DOGE CH 1m (Nov 6–14 window). Value **≈0.09496** appears in **September frozen** research/backtest artifacts (~DOGE price in that sim segment).

## 3. Confirmed cause

### A) `mergePollCandlesIntoPane` (primary — P18 interaction)

When the open forming bucket on the client overlapped an incoming API row for the same `time`, merge did:

```javascript
high: Math.max(cur.high, oldTip.high)
```

After P18 gap-backfill, **`oldTip.high`** could still carry a **frozen September wick** (~0.095) while **O/C** already matched November live (~0.0647). API `cur` was correct; **client max pinned the phantom wick**.

### B) `apply_live_forming_tip` else branch

When `last_t > forming_bucket`, backend **still** widened the **last** bar with `max(last_h, hi, px)` — same cross-bucket wick bleed if forming carried a stale high.

### C) HTF forming `high` from 1m/collector tick

`formingBarForTf` and `apply_live_forming_tip` used `forming.high` on **5m+** buckets. Stale/wrong `forming.high` (~0.095) widened the live 5m bar while O/C stayed ~0.0647.

**Fix:** On HTF (`tfSec > 60` / `step > 60`), forming updates **close** and only `max(bar.high, px)` — not raw `forming.high`.

### D) `formingBarForTf` desync branch

`lastT > bucket`: close-only (no cross-bucket wick merge).

**Not proven:** Collector/CH publishing `0.094940` at probe time. **Not required:** DB correction.

## 4. Minimal fix (uncommitted)

| File | Change |
|------|--------|
| `static/js/research/research_charts.js` | Forming merge: **`byTime[t] = Object.assign({}, cur)`** — API wins for O/H/L |
| same | Desync forming: **close only**, preserve H/L |
| `research_charts/service.py` | `last_t > bucket`: **no** wick paint on tip |
| `templates/research_charts.html` | `?v=pd1-p19b-forming-wick-merge-v1` |
| Mirror | `backtester/short/dashboard/...` |
| Tests | `test_apply_live_forming_does_not_widen_when_last_ahead_of_bucket`; host asserts no `oldTip.high` max |

No artificial price clamps. No publisher/collector changes.

## 5. Tests

| Test | Result |
|------|--------|
| `test_apply_live_forming_does_not_widen_when_last_ahead_of_bucket` | **PASS** |
| `test_host_coalesce_...` (merge asserts) | **PASS** |
| `node --check research_charts.js` | **PASS** |
| Browser regression matrix | **UNVERIFIED** |

## 6. Regression intent

- Normal / forming / roll / backfill / poll: OHLC from API + TF-correct forming only.  
- Real spikes (in CH) still flow through API rows unchanged.  
- P18 viewport, P19 EMA, pools, drawings: untouched logic except merge path.

**STOP** — no commit/push.
