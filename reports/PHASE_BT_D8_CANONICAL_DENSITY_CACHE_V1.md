# PHASE BT-D8 — Canonical Depth-2 Historical Density Cache V1

**Date:** 2026-10-25  
**Project:** `/home/telgenbuescher/projects/pools+ob+delta_bot`  
**Prior:** BT-D7 `SHORT_PNL_INTEGRATION_PARTIAL` (24h pilot incomplete; 2h smoke OK)

**Scope:** New isolated cache layer + BT-D7 `density_source` switch. No new density math, no strategy/PnL logic changes, no SSOT/chart/freeze/Original Short V2 edits.

---

## VERDICT

### `DENSITY_CACHE_PARITY_PASS`

| Layer | Status |
|-------|--------|
| Cache design (keys, checksums, atomic bundle) | **PASS** |
| Fail-closed read (missing / corrupt / hash mismatch) | **PASS** |
| Provider vs cache readback (6 BT-D3/D4 reference closes) | **PASS** |
| Golden chart parity `2026-09-25T11:00:00Z` (215 bands) | **PASS** |
| 2h predeclared pilot bundle + BT-D7 provider vs cache | **PASS** |
| BT-D6/D7 regression tests | **PASS** |

---

## 1. Artefakte

| Path | Role |
|------|------|
| `tools/phase_bt_d8_canonical_density_cache_v1/cache_contract_v1.py` | Contract version, structured cache keys, checksums |
| `tools/phase_bt_d8_canonical_density_cache_v1/cache_store_v1.py` | Atomic entry/bundle I/O, integrity checks |
| `tools/phase_bt_d8_canonical_density_cache_v1/cache_builder_v1.py` | Build from `ResearchDensityRunV1.collect_at_close` only |
| `tools/phase_bt_d8_canonical_density_cache_v1/cache_session_v1.py` | `ValidatedDensityCacheSessionV1` (no recompute) |
| `tools/phase_bt_d8_canonical_density_cache_v1/parity_runner_v1.py` | Provider ↔ cache + golden band parity |
| `tools/phase_bt_d8_canonical_density_cache_v1/pilot_compare_v1.py` | BT-D7 full run equality |
| `tools/phase_bt_d8_canonical_density_cache_v1/run_bt_d8.py` | CLI: `build-pilot`, `parity-suite`, `benchmark-read`, `pilot-compare` |
| `tools/phase_bt_d8_canonical_density_cache_v1/configs/pilot_2h_build_v1.json` | Predeclared 2h window |
| `results/phase_bt_d8_canonical_density_cache_v1/bundles/dogeusdt_5m_pd1_depth2_pilot_2h_20260911T1200_1400Z/` | Pilot cache bundle (25 closes) |

**BT-D7 integration:** `density_bridge_v1.create_density_session()` — `density.density_source`: `canonical_provider` (default) \| `validated_cache` with `validated_cache.bundle_path`.

**BT-D6 validator:** optional `density_source` / `validated_cache`; fail-closed when cache mode without `bundle_path`.

---

## 2. Cache design (A)

Each entry stores the **exact** BT-D4 `snapshot_payload` shape:

- `as_of_utc` (= decision close)
- `upper_bands` / `lower_bands` with string `lower_price` / `upper_price`, `unique_origin_count`, `origin_ids`
- `pipeline_line_id`, `config_hash`, `data_provenance`, `provider_manifest`

**Cache key** (structured, not timestamp-only): symbol, timeframe, PD, depth, decision close, pipeline line id, provider config hash, cache contract version, adapter contract version → SHA256 suffix.

**Bundle manifest:** immutable publish via staging dir + `os.replace`; entries versioned `bt_d8_cache_entry_v1` with `content_sha256`.

---

## 3. Kausalität & Integrität (B–C)

- One row per historical decision close; `as_of_utc` must equal close (enforced on read).
- Build path: only `ResearchDensityRunV1.collect_at_close` → gateway → BT-D3 provider (same as BT-D4).
- Missing entry → `CACHE_ENTRY_MISSING` (fail-closed).
- Checksum mismatch → `CACHE_CHECKSUM_FAILED`.
- Pipeline / config hash mismatch vs bundle → fail-closed.
- `validated_cache` mode in BT-D7: density errors **not** swallowed (no silent fallback).

---

## 4. Chart / provider parity (D)

**Golden:** `2026-09-25T11:00:00Z` — **39 upper + 176 lower = 215** bands (all edges, origins, sides) match live provider and golden JSON.

**Reference closes** (BT-D4 `research_config_v1.json`):

- `2026-09-25T11:00:00Z`
- `2026-05-15T12:00:00Z`, `2026-06-20T08:00:00Z`, `2026-07-10T16:00:00Z`, `2026-08-05T10:00:00Z`, `2026-09-10T14:00:00Z`

**Gate:** band-level equality provider == cache readback; core snapshot fields (bands + provenance ids) byte-stable after cache roundtrip. Full JSON may differ only in non-trading `provider_manifest` noise when comparing two live collects — cache write uses the **same** provider snapshot object as readback reference.

```bash
python tools/phase_bt_d8_canonical_density_cache_v1/run_bt_d8.py --mode parity-suite
# verdict: DENSITY_CACHE_PARITY_PASS (see results/.../run_parity_suite.json)
```

---

## 5. Performance (E)

**2h pilot build** (`2026-09-11T12:00:00Z` … `14:00:00Z`, 25 closes):

| Metric | Value |
|--------|--------|
| Cold compute total | **153.1 s** |
| Cold compute mean / close | **6.12 s** |
| Mean entry size | **~138 KB** |
| Total bundle payload | **~3.45 MB** |
| Cache read (sample close) | **~0.001 s** |
| Extrapolation 44 064 closes (compute) | **~75 h** at observed mean |
| Extrapolation 44 064 closes (storage) | **~5.7 GB** at observed mean |

**Batch/window note (documentation only):** BT-D3 already uses `build_landscape_window` per close; batching multiple closes in one Node invocation could reduce overhead **only** if each close remains an independent as-of evaluation with identical semantics. Not implemented in BT-D8 (no parity risk).

---

## 6. BT-D7 pilot (F–H)

**Window:** same predeclared 2h as BT-D7 smoke (`historical_pilot_smoke_2h_v1.json`).

| Run | Signals | Trades | Snapshots |
|-----|---------|--------|-----------|
| `canonical_provider` | 1 | 0 | 25 |
| `validated_cache` | 1 | 0 | 25 |

**Equality:** signals, orders, trades, full PnL object — **identical** (`pilot-compare` OK).

```bash
python tools/phase_bt_d8_canonical_density_cache_v1/run_bt_d8.py --mode build-pilot
export HISTORICAL_LMV2_BATCH_READER=1
python tools/phase_bt_d8_canonical_density_cache_v1/run_bt_d8.py --mode pilot-compare
```

**Example cache config fragment:**

```json
"density": {
  "timeframe": "5m",
  "pool_detail": 1,
  "method": "exact_unique_origin",
  "min_unique_origins": 2,
  "density_source": "validated_cache",
  "validated_cache": {
    "bundle_path": "results/phase_bt_d8_canonical_density_cache_v1/bundles/dogeusdt_5m_pd1_depth2_pilot_2h_20260911T1200_1400Z"
  }
}
```

No automatic 24h or multi-month cache generation in this phase.

---

## 7. Tests (G)

```bash
python -m pytest tools/phase_bt_d8_canonical_density_cache_v1/tests -q
# 7 passed, 2 skipped (live optional: RUN_BT_D8_LIVE=1)

python -m pytest tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/tests -q
python -m pytest tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests -q
```

Coverage includes: cache key contract, golden roundtrip, missing/corrupt/hash failures, BT-D7 synthetic fixture provider vs cache backtest identity, optional live golden test.

---

## 8. STOP boundary

- No 24h+ cache builds, no parameter optimization, no frozen-strategy approval, no chart/SSOT/historical publication changes.
- Strategy semantics remain `FREEZE_CANDIDATE` / `NOT_FINAL_APPROVED`; cache is infrastructure only.

---

## Final verdict token

**`DENSITY_CACHE_PARITY_PASS`**
