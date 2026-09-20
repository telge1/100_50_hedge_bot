# Upstream Causality Audit — LLD Targets

**Phase:** LIQUIDITY_DESTINATION_BIAS Phase 2B  
**Generated (UTC):** 2026-09-05T13:59:26Z  
**Scope:** read-only code + artifact verification (no re-materialization)

## Verdict for Audit A

**PASS** for the Phase-1/1D episode target pipeline, with one **residual mid-bar HTF tip caveat** on pool *invalidation timing* (~1 minute on 5m), which does **not** use future lifetime/max size/touch for ranking and is therefore **not** classified as classic target lookahead.

---

## End-to-end chain

| Step | Repository | File | Function/Class | Input | Event-Time | Available-Time / Knowledge-Cutoff | Output | Causality proof | Lookahead risk |
|---|---|---|---|---|---|---|---|---|---|
| 1 Pool detection | trading_research_platform | `indicators/liquidity_location/engine.py` | `_run_pools` / `run_liquidity_location` | 1m/HTF candles | source bar i-1 OHLC; confirm bar i | confirmation bar close | `LiquidityPool` geometry + metadata | Engine docstring: pool from candle i-1, confirmed on i; only info through i | Display `amount` newest-N prune within as-of pack |
| 2 Availability stamp | trading_research_platform | `indicators/liquidity_location/availability.py` | `attach_availability_metadata`, `confirmation_bar_end` | confirmation open + TF | bar open | **`available_at` = confirmation bar end** | metadata timestamps | Explicit: earliest usable moment = closed confirmation bar | None if callers honor `available_at` |
| 3 As-of snapshot | orderbook_analyse | `liquidity_pool_signal/chart_pool_adapter.py` | `export_snapshot`, `pool_row_from_engine` | candles lookback ending at `as_of` | candle opens ≤ as_of | `available_at`; `active_as_of` | active canonical pools | Re-runs engine on `[pack_start, as_of]`; `window_start` ignored | HTF tip: bar may be usable ~1m before true close for OHLC invalidation |
| 4 Canonical rows | orderbook_analyse | `liquidity_pool_signal/canonical.py` | `canonical_pool_record` | engine rows | — | `available_at` | lower/upper/side/id | Passthrough of as-of fields | None |
| 5 Provider | spread_recovery | `research/liquidity_destination_bias/targets.py` | `CanonicalLldPoolProvider.snapshot` | symbol, T0 | T0 | as_of=T0 | snapshot dict | Calls `export_snapshot(..., as_of=t0)` | Inherits tip caveat |
| 6 Target freeze | spread_recovery | `targets.py` | `select_frozen_targets` | snapshot + `price_t0` + T0 | — | `available_at <= T0-60s` and `active_as_of` | `FrozenTarget` pair | Nearest ASK above / BID below only; no strength/lifetime/touch | Ruled out: future pools filtered by available_at |
| 7 Episode | spread_recovery | `builder.py` | `_process_grid` | targets + trades | T0 | `knowledge_cutoff_utc=T0` | frozen episode | Targets copied before path label | Outcome cannot mutate targets |
| 8 Label | spread_recovery | `path_label.py` | `label_first_touch` | future 1s path | post-T0 | n/a (label only) | outcome | Docstring: path only for this function | Future prices OK for labels only |

---

## Explicit answers (Audit A)

1. **How does an LLD pool arise?**  
   Candle swing + volume on source bar `i-1`, confirmed on bar `i` in TRP `run_liquidity_location`. Geometry = source high/low ± half-size. OA does not re-detect; it binds the same engine.

2. **Which raw data set price, side, size, bounds?**  
   Closed candle OHLC/volume (1m aggregated to TF). Side ASK/BID from swing high/low. Bounds `top_price`/`bottom_price` from source bar ± half. Strength may exist on the engine object but is **not** used by `select_frozen_targets`.

3. **Does detection use only data through that time?**  
   Engine claims yes for candle index `i`. Snapshot path rebuilds from lookback candles with `end=as_of`. Residual: HTF aggregation can expose a forming bar tip slightly before true close for invalidation OHLC.

4. **Does `available_at` mean “recognizable then” or “later stored”?**  
   **Recognizable/usable then:** close of the confirmation TF bar (`confirmation_bar_end`). Not export/ingest time.

5. **How is 60s minimum persistence proven?**  
   `MIN_TARGET_PERSISTENCE_SECONDS=60` and `available > T0-60s → skip` in `select_frozen_targets`. Eligibility also fails if `available_at > T0`.

6. **Past elapsed vs future lifetime?**  
   **Past elapsed only.** No remaining-life or total-lifetime filter.

7. **Can later resolution change the T0 snapshot?**  
   Not in builder: targets are frozen into immutable fields. Re-running `export_snapshot(as_of=T0)` reconstructs historically; outcome does not rewrite targets. Later invalidation after T0 does not alter stored episode geometry.

8. **Historical reconstruction vs end-state backprojection?**  
   **Historical reconstruction:** lookback pack ending at `as_of`, engine run, as-of filters. Not “today’s final pool list projected back.”

9. **Later max size / lifetime / touches for prioritization?**  
   **No.** Selection keys: side, edges vs `price_t0`, nearest edge, pool_id tie-break.

10. **Prefix identity?**  
    Builder unit tests + Phase-1D prefix parity (`prefix_parity.json` passed) show completed semantic rows identical for a shorter prefix vs longer run. Episode-level provider mock tests prove future pools cannot change T0 choice. Live dual `export_snapshot` under extending candle history is covered by OA as-of/causality tests; residual tip caveat remains for invalidation mid-bar.

---

## Classic lookahead checklist

| Pattern | Present? |
|---|---|
| Future lifetime at T0 | No |
| Future max size | No |
| Future resolution used to select | No |
| Future touch used to select | No |
| Completed track backprojected | No |
| Full-window ranking after T0 | No |
| Residual HTF tip invalidation | **Yes (~1m)** — early deactivation possible; conservative, not accuracy-inflating ranking |

---

## Evidence tests / artifacts

- `tests/research/test_liquidity_destination_episodes.py` — future pool ignored; frozen targets; path only changes label; prefix target parity  
- `tests/research/test_liquidity_destination_multiday.py` — prefix parity; `knowledge_cutoff == t0`; available_at ≤ t0  
- `results/.../phase_1d.../prefix_parity.json` — passed  
- OA: `test_liquidity_pool_signal_foundation_v1.py`, `test_canonical_chart_pool_asof_restore_v1.py`, `test_liquidity_location_pool_causality_*`
