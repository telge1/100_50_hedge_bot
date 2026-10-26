# PHASE BT-D33 — 5m PD1 Historical Density Performance Forensics

**Date:** 2026-10-26  
**Mode:** READ-ONLY — no code changes, no restarts, no new scans/backtests  
**Artifacts:** `results/phase_bt_d33_density_performance_forensics_v1/workload_model_sep11_8h.json`

---

## VERDICT

**`DENSITY_PERFORMANCE_ROOT_CAUSE_MAPPED`**

The ~**162–172 s** window latency is dominated by **server-side cold recomputation** of `build_landscape_window()` over a **176 h** effective timeline (**168 h warmup + 8 h visible**), walking **2113** five-minute decision closes for **5m PD1** membership, after a batched ClickHouse prefill. It is **not** primarily network or JSON size. The B-layer client has **in-memory chunk cache** and **inflight dedup**, but **no cross-request server cache**; pan/zoom can repeat nearly the same warmup work. **Middleware timeout (150 s)** can fire before the handler finishes (~162 s measured in BT-D32).

---

## A. Timing profile (bounded Sep-11 case)

### Measured end-to-end (existing evidence, no new full-window run)

| Stage | Evidence | Duration / scale |
|--------|-----------|------------------|
| HTTP / in-process total | BT-D32 pass2 + in-process probe | **161.9 s** HTTP, **172.1 s** Python |
| Pools returned | Same probes | **262** |
| Request ingress → handler | FastAPI + `asyncio.to_thread` (BT-D32) | Negligible vs total |
| Response serialization | `json.dumps(payload)` timed in builder | Included in `timing_ms.json_serialize` (not captured this run) |

### Structural breakdown (code instrumentation + workload model)

Builder: `pd1_landscape_viewer_v1/historical_window_v1.py` — emits `timing_ms`:

| Sub-phase | What it does |
|-----------|----------------|
| `clickhouse_candles` | `ClickHouseResearchCandleSource.get_candles` over **context→vis_to** (~2113 bars) |
| Phase 5S prefill | `prefill_member_cache_phase5s` → `build_episode_walk_batch_cache` for **all** decision closes in range |
| `build_lifetimes` | `_build_lifetimes` on full **5m grid** (2113 steps); ACTIVE segment merge per pool |
| `build_clusters_by_dc` | Per decision close: `analyze_side` cluster pass (not used by B-layer JS) |
| `json_serialize` | Full payload dump |

**Workload model (no CH calls):** `context_hours=176`, `grid_5m_steps=2113`, `unique_decision_closes=2113` for visible `2026-09-11T07:34:51Z` … `15:34:51Z`, pipeline `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`. See workload JSON artifact.

**ClickHouse vs Python (inference, not a fresh profile run):**

- Phase **4X-AK** / 5S batch: **120** closes prefetched in **~0.22 s**, **9** CH queries (`results/phase4xak_density_readback_optimization_v1/ch_query_analysis.md`).
- Scaling **2113** closes linearly in *prefetch work* is still **orders of magnitude smaller** than **~172 s** total → **Python lifetime + cluster passes** are the main cost; CH prefill is necessary but not the bulk at batch scale.
- **Cold vs warm:** Per-request `cache: dict` inside `build_landscape_window` is **request-scoped only** — repeat pan/zoom hits **client** chunk cache (`exactOriginWindowStores`, max **32** chunks) but **misses server** unless the same chunk key is reused.

**Profiling hook (not executed in D33):** `PD1_LANDSCAPE_WINDOW_PROFILE=1` adds `profile` + `get_last_window_profile()` with `load_members_hit/miss`, grid counts.

---

## B. Cache inventory

| Cache | Location | Stored | TF / PD | Hash / SSOT | As-of | Compatible with live B density? |
|--------|-----------|--------|---------|-------------|--------|--------------------------------|
| **BT-D8 validated density** | `results/phase_bt_d8_.../bundles/...` | Per-close `upper_bands` / `lower_bands` (BT-D4 snapshot shape) | 5m PD1, depth 2 | `pipeline_line_id`, `config_hash`, structured cache key | `as_of_utc == decision_close` | **Not 1:1** — bands at a **single close**, not `pools[]` + `segments[]` over a window. Parity proven provider↔cache for **bands**; **adapter required** to landscape window contract. |
| **BT-D9 24h bundle** | `results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle` | Same BT-D8 family; 288 closes | 5m PD1 | Same | Causal close | Same as BT-D8 — **not** drop-in for `/viewer/api/window`. |
| **BT-D23 / BT-D26 runs** | `setups.json`, scanner outputs | Planned entries, transitions, boxes | N/A | Run `config_hash` | Setup / candidate as-of | **No** — chart overlays only, not membership SSOT. |
| **Phase 5S episode-walk batch** | In-request `dict` during `build_landscape_window` | ACTIVE members per decision close | Per `hcfg` | `pipeline_line_id` + `config_hash` | Causal decision close | **Yes** — this *is* the live path when `HISTORICAL_LMV2_BATCH_READER` enabled. |
| **`_PUBLISHED_CLOSES_CACHE`** | `historical_pool_state_v1.py` | Published close list | Per pipeline | Per line | N/A | Speeds publication listing; must be cleared after imports (4X-AK). |
| **6XT client EOD cache** | `pane.exactOriginWindowStores` | `pools` per planned window/tile | 5m/15m PD scopes | Scope key + window ms range | Viewport / replay gen | **Yes** for client reuse; **LRU 32** chunks; cleared on `replayGen` change. |
| **`exactOriginSegmentCache`** | Per pane | Merged segments for canvas | — | `viewportRenderKey` | Current view | **Yes** — avoids re-merge when chunks cached. |
| **`inflight` Map** | `pd1_exact_origin_density_research_v1.js` | Dedup concurrent identical window POST | — | scope+from+to | — | Reduces duplicate parallel fetches only. |

**Do not silently substitute BT-D8 band caches** for window API responses without a parity gate on `pools`/`segments` and provenance fields.

---

## C. Bottleneck summary

| Factor | Role |
|--------|------|
| **168 h warmup** | Fixes `context_from`; cost scales with **visible start**, not only visible width. |
| **5m PD1 slot** | **2113** membership evaluations for ~176 h (vs ~704 for 15m slots on same span). |
| **`_build_lifetimes`** | Full-grid episode walk + segment coalescing — **CPU-heavy** with warm member cache. |
| **`_build_clusters_by_dc`** | Extra per-close `analyze_side` — **not referenced** by B-layer JS (potential dead cost for Exact Origin canvas). |
| **ClickHouse** | Batched prefill (~9 queries per batch scale in 4X-AK); candle load for ~2k bars — smaller than Python phases at this scale. |
| **Serialization** | Large `pools` + optional `clusters_by_dc` — minor vs compute (measured via `timing_ms` in code). |
| **Pan/zoom** | Client may request **new 72 h chunks** (`EOD_MAX_5M_STEPS`); each server call rebuilds **168 h context** from chunk `from_utc` → **heavy overlap**, no server reuse. |
| **Cache hits** | Client: yes for unchanged chunk keys / fully cached plans. Server: **no** persistent window cache. |

**Cold calculation?** Yes — every uncached chunk is a full rebuild.  
**Repeated on pan?** Yes — overlapping warmup unless client hits an identical chunk key.

---

## D. Client timeout and races

| Mechanism | Behavior |
|-----------|----------|
| **Server middleware** | `asyncio.wait_for(call_next, 150.0)` for most routes including `/live-charts/viewer/api/window` (`app.py`). **162 s** measured window **can return 504** while thread work continues. |
| **B-layer `fetchWindow`** | Raw `fetch()` — **no** `AbortController`, **no** explicit client timeout. |
| **`sendJson` (research host)** | Optional `meta.signal` from `state.loadAbort` — **not** wired into `fetchWindow`. |
| **Concurrency** | `MAX_CONCURRENT_WINDOW_FETCH = 2`; per-key `inflight` dedup. |
| **Stale responses** | `loadSerial` + `replayGen` checks after fetch; aborted logical loads skip canvas update. **Risk:** slower response without serial mismatch could still apply if viewport moved but serial unchanged — mitigated partially by `renderKey` / segment cache. |
| **Failed fetch** | Hint: “prior data retained”; may repaint `exactOriginSegmentCache`. |
| **Planner** | Default **sliding** ≤72 h chunks; optional **fixed 48 h tiles** (`localStorage` flag, default off). Load span adds **36 h prefetch** + **6 h tail** + pad (`computeDensityLoadSpanMs`). |

---

## E. Optimization plan (2–3 variants)

### Variant 1 — Server-side causal membership / window cache (recommended direction)

- **Idea:** Persist or memoize Phase 5S batch results (or full `pools` payload) keyed by `(pipeline_line_id, config_hash, timeframe, pool_detail, context_from_utc, to_utc)` with TTL or LRU; pan shifts only extend `to_utc` or slide `context_from` incrementally.
- **Benefit:** Reuse validated SSOT data; largest reduction on overlapping pans and repeat GO TO.
- **Risks:** Stale cache after CH imports; memory; key must include config hash.
- **Parity:** Byte-compare `pools` + provenance vs uncached builder on golden windows (Phase 1b/1e style).
- **Changes:** Dashboard cache module + invalidation hook on import (reuse `clear_published_closes_cache` pattern).
- **Fallback:** Env flag disables cache → current builder only.

### Variant 2 — Align timeouts + optional slim window response for B-layer

- **Idea:** (a) Exempt window route from 150 s middleware cap or set ≥ observed P99; (b) API flag or route variant returning `pools` only (skip `clusters_by_dc`) for Exact Origin consumers.
- **Benefit:** Fewer false 504s; (b) cuts Python cluster phase if contract-approved.
- **Risks:** (b) breaks consumers that need clusters; must not change pool math.
- **Parity:** Diff pools only on golden cases; clusters route unchanged for legacy.
- **Changes:** Middleware + optional query param (separate phase).
- **Fallback:** Full payload default.

### Variant 3 — Client tile stability + BT-D8-assisted hydration (higher integration cost)

- **Idea:** Enable **fixed 48 h UTC tiles** by default for stable keys; for each missing tile, hydrate membership from **BT-D8 bundle** closes where bundle covers span, fall back to CH for gaps.
- **Benefit:** Fast path where bundles exist (D8 read ~ms/close); better pan reuse via tile keys.
- **Risks:** **Representation mismatch** (bands vs segments); incomplete bundle coverage; depth-2 filter still client-side.
- **Parity:** Mandatory gate: generated `pools` must match `build_landscape_window` on overlap closes before production use.
- **Changes:** Adapter layer + bundle manifest registry; no change to depth-2 math.
- **Fallback:** Current CH window builder only.

---

## F. As-of BT-D26 drawings (replay before first candidate)

**Problem (BT-D31):** GO TO **11:34:51 UTC** shows a box while earliest candidate is **12:00 UTC**.

**Mechanisms (read-only):**

1. **`load_bt_d26_dynamic_selector_catalog`** — calls `_clear_bt_d26_dynamic_selector_drawings` before loading catalog (`workspace_session.py`).
2. **`sync_bt_d26_dynamic_selector_viewport`** — in “Aktueller Kandidat” mode, builds specs from `d26_build_clean_view_draw_specs` at `as_of_unix`; removes drawing IDs not in `wanted_ids` (planned + markers).
3. **Persisted `drawings.json`** — survives reload; visible until (1) or (2) succeeds.
4. **Client** — `resolveBtD26AsOfUnix` uses `replayTargetTs` in replay; sync runs on viewport/backtester apply paths.

**To clear stale art before first valid candidate (future UX phase, not trading logic):** ensure **sync-viewport** (or hide) runs after **GO TO** with `as_of_unix = replayTargetTs` once catalog is loaded; failed HTTP left stale TRP drawings in D31/D32.

---

## G. Constraints honored

No productive code changes, no dashboard restart, no new scanner/backtests, no SSOT/density math changes.

---

## References

- BT-D32: `reports/PHASE_BT_D32_HTTP_STALL_RECOVERY_V1.md`
- Builder: `dashboard/pd1_landscape_viewer_v1/historical_window_v1.py`
- Client B-layer: `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js`, `pd1_exact_density_trp_adapter.js`
- BT-D8: `reports/PHASE_BT_D8_CANONICAL_DENSITY_CACHE_V1.md`
