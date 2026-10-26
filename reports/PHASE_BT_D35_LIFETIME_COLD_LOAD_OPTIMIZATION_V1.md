# PHASE BT-D35 — Canonical 5m PD1 Lifetime Cold-Load Optimization

**Date:** 2026-10-26  
**Artifacts:** `results/phase_bt_d35_lifetime_cold_load_v1/`

---

## VERDICT

**`LIFETIME_OPTIMIZATION_PASS`**

Root cause was **not** Python `_build_lifetimes` algorithmic cost with a warm member cache, but **~2113 per-close ClickHouse publication loads** when Phase 5S batch prefill was **disabled by default**. Enabling validated batch prefill for the landscape window path reduces cold load **~167 s → ~4.8 s** with **identical `pools[]` SHA256** vs legacy per-close loader.

**Browser:** NOT_VERIFIED (separate).

---

## A. Profiling (Sep-11 8h reference)

| Metric | Legacy (`PD1_LANDSCAPE_BATCH_READER=0`) | Default batch (BT-D35) |
|--------|----------------------------------------|-------------------------|
| Total cold | **167.1 s** | **4.8 s** |
| `phase5s_prefill` | 0 ms | **~4300 ms** |
| `build_lifetimes` | **166880 ms** (CH inside `_load_members`) | **~280 ms** (cache hits) |
| `phase5s_batch_reader_active` | false | **true** |
| CH HTTP (cProfile, empty cache) | **~69729** requests | **~153** batched queries |
| Grid / closes | 2113 | 2113 |

`_build_lifetimes` inner loop is cheap when the member `dict` is prefilled; legacy path spent ~**60 ms × 2113** in `load_published` per close.

---

## B. Optimization (semantically equivalent)

1. **`_landscape_phase5s_prefill_enabled()`** — Phase 5S batch prefill **on by default** for `build_landscape_window` (opt-out: `PD1_LANDSCAPE_BATCH_READER=0` or `HISTORICAL_LMV2_BATCH_READER=0`).
2. **`prefill_member_cache_phase5s`** — removed redundant inner env gate (caller controls activation).
3. **`timing_ms.phase5s_prefill`** — explicit sub-timer (separate from `build_lifetimes`).
4. **`provenance.lifetime_builder_version`** — `pd1_landscape_lifetime_v1_phase5s_batch_default`.

No change to warmup hours, pool math, origin IDs, or depth semantics.

---

## C–D. Historical correctness and parity

- **Pools SHA256** (legacy per-close vs batch default): **`b14d4c3679c9a38026d3dd1fc4f53cc621a8295792641f971bec29826674cc2a`** — **match**.
- **262 pools** (visible clipped), 5m PD1, 168 h context unchanged.
- Full pytest: `BT_D35_SLOW_PARITY=1 pytest test_bt_d35_landscape_batch_default_v1.py`.

---

## E. Performance summary

| Scenario | Measured |
|----------|----------|
| Cold legacy per-close | **167.1 s** |
| Cold exact_origin_v1 + batch | **4.81 s** |
| Warm BT-D34 response cache | **~0.002 s** (in-process, prior BT-D34) |
| Memory | In-process LRU unchanged (16 entries) |

---

## F. Live integration

- BT-D34 response cache **unchanged**.
- Legacy path preserved via env opt-out.
- Dashboard restarted after deploy.
- No Dynamic Selector changes.

---

## G. Fallback

Set **`PD1_LANDSCAPE_BATCH_READER=0`** (or `HISTORICAL_LMV2_BATCH_READER=0`) to restore per-close `load_historical_pool_state_at_close` behavior.

---

## H. Remaining bottlenecks

- Cold **~4.3 s** Phase 5S CH batch + **~70 ms** candles + **~280 ms** lifetimes per uncached window.
- Overlapping windows still recompute context until BT-D34 cache hit.
- Browser canvas path not re-tested.

---

## Files

- `pd1_landscape_viewer_v1/historical_window_v1.py`
- `pd1_landscape_viewer_v1/historical_window_batch_v1.py`
- `test_bt_d35_landscape_batch_default_v1.py`
- `tools/phase_bt_d35_lifetime_cold_load_v1/run_measurements_v1.py`
