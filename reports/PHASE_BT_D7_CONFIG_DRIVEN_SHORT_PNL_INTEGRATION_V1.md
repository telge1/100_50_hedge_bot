# PHASE BT-D7 — Config-Driven Depth2 Short Backtester + Full PnL Integration V1

**Date:** 2026-10-25  
**Project:** `/home/telgenbuescher/projects/pools+ob+delta_bot`  
**Prior:** BT-D4 `BACKTESTER_DEPTH2_INTEGRATION_PASS`, BT-D5 `FROZEN_SHORT_SEMANTIC_APPROVAL_REQUIRED`, BT-D6 `CONFIG_PNL_FOUNDATION_PASS`

**Scope:** New isolated package only. No edits to Original Short V2, Phase 4D engine/signal CSV path, chart/SSOT, or `strategy_freeze/*` contents.

---

## VERDICT (summary)

| Layer | Status |
|-------|--------|
| **Overall** | **`SHORT_PNL_INTEGRATION_PARTIAL`** |
| Technical integration (density → signals → orders → fills → SL/TP → PnL) | **PASS** |
| PnL accounting on integrated trade ledger | **PASS** (synthetic fixture + BT-D6 aggregator) |
| Config-driven gap gate (`upper_gap_min_pct`) | **PASS** (demonstrated) |
| Predeclared **24h** DOGEUSDT historical pilot | **NOT COMPLETED** (runtime blocker) |
| **2h** historical smoke (subset of same window) | **PASS** (live density + SSOT OHLC) |
| Frozen strategy final approval | **NOT** — `FREEZE_CANDIDATE` / `NOT_FINAL_APPROVED` |
| TP mode in BT-D7 | **`next_eligible_band` only** — experimental / 4D-aligned, **not** user-approved `dense_cluster` |

---

## 1. Deliverable package

| Path | Role |
|------|------|
| `tools/phase_bt_d7_config_driven_short_pnl_integration_v1/run_bt_d7.py` | CLI: `synthetic`, `historical-pilot`, `gap-demo` |
| `density_bridge_v1.py` | Production: `ResearchDensitySessionV1` → `ResearchDensityRunV1.collect_at_close` only |
| `gap_detection_v1.py` | `adjacent_union_interval_gap_pct_v1` (Signal_definierung / `computeGaps` alignment) |
| `signal_engine_v1.py` | Config-driven short signals; **gap gate uses `upper_gap_min_pct`** |
| `short_research_engine_v1.py` | Bar walk: density → signals → pending limit → fill → SL/TP → `aggregate_pnl` |
| `execution_v1.py` | Pending-limit fill, intrabar exit, pool lifecycle |
| `bands_adapter_v1.py` | Snapshot → intervals for rules |
| `price_feed_v1.py` | SSOT OHLC for historical pilot (vis window capped for loader) |
| `configs/synthetic_fixture_v1.json` | Integration fixture config |
| `configs/historical_pilot_24h_v1.json` | Predeclared 24h window (not run to completion) |
| `configs/historical_pilot_smoke_2h_v1.json` | First 2h of same window (smoke) |
| `fixtures/synthetic_integration_v1.json` | Reproducible density + price fixture |
| `tests/test_bt_d7_integration_v1.py` | Gap, PnL fields, determinism |
| `tests/test_regression_v1.py` | Phase 4D strict unit subset unchanged |

**Experiment outputs:** `results/phase_bt_d6_config_experiments_v1/<run>_*` (immutable config copy, manifest, ledgers, PnL).  
**Runner summary:** `results/phase_bt_d7_config_driven_short_pnl_integration_v1/integration_report.json`

---

## 2. Density contract (A) — enforced

- All production density via **`ResearchDensityRunV1.collect_at_close`** (BT-D4 gateway → BT-D3 chart-path analyzer).
- Fixed: **5m**, **PD1**, **exact_unique_origin**, **min_unique_origins=2**.
- No per-pool fallback, no alternate density math, no lookahead in the engine loop (signals only after decision close snapshot).
- Tests use **`FixtureDensitySessionV1`** only for isolated integration; historical pilot uses live collection.

---

## 3. Config-driven strategy (B)

All tunables read from validated BT-D6 JSON (`load_and_validate_config` / `validate_config_dict`). Fail-closed on missing financial or density locks.

**Gap gate (binding):** `signal_engine_v1` calls `has_upper_large_gap_above_pool(..., upper_gap_min_pct, ...)` — changing config **changes signal count**, not manifest-only.

**Gap parameter demo (fixture):**

| `upper_gap_min_pct` | Signals |
|---------------------|---------|
| 0.5 | 2 |
| 50.0 | 0 |

`gap_gate_effective: true`

**TP:** Only `next_eligible_band` (reuses `strategy_rules.select_short_tp_depth2` read-only). `dense_cluster` remains blocked without `dense_cluster_semantics_approved: true` (BT-D6 validator).

---

## 4. Pipeline (C)

```text
collect_at_close (density)
  → upper/lower intervals
  → structure + gap gate + proximity
  → ShortSignalV1 @ decision close
  → pending limit (next bar+)
  → fill on touch
  → SL/TP from fill + bands (short_sl, select_short_tp_depth2)
  → ClosedTradeV1
  → SyntheticTrade → aggregate_pnl (BT-D6)
```

- No Phase 2A signal CSV as “live” signals.
- `max_concurrent_positions: 1` enforced in engine.
- Entry/SL/TP follow documented references where implemented (fill-based TP distance when configured).

---

## 5. Full PnL (D)

Uses **`pnl_aggregator_v1.aggregate_pnl`** on trades produced by the BT-D7 engine.

**Synthetic integration example** (`synthetic_integration_34e43d69024a_20261025T100805Z`):

| Metric | Value |
|--------|-------|
| Signals | 2 |
| Trades | 1 (SL) |
| Gross PnL USDT | -7.04 |
| Fees USDT | 1.10 (0.11% roundtrip on 1000 USDT notional) |
| Net PnL USDT | -8.14 |
| Return % | -0.081% |
| Max drawdown % | 0.081% |
| Equity curve + trade_ledger | Present in `pnl_report.json` |

**Note:** Aggregator still sets `synthetic_only: true` in JSON (BT-D6 heritage); BT-D7 trades are engine-generated, not hand-authored `SyntheticTrade` stubs.

**Policies from config:** `intrabar_ambiguity_policy: no_trade_ambiguous`, fee/slippage/funding models explicit; no invented defaults.

---

## 6. Historical pilot (E)

### Predeclared 24h (primary config)

- **Symbol:** DOGEUSDT  
- **Window:** `2026-09-11T12:00:00Z` → `2026-09-12T12:00:00Z` (fixed in `historical_pilot_24h_v1.json`)  
- **Status:** **Not completed.** One run exceeded ~10 min wall without finishing (~289 bars × ~5–6 s/bar Node density ≈ **30+ minutes** expected). Process stopped to avoid unbounded wait.

**Blockers for full 24h claim:**

1. **PERFORMANCE** — no per-close density cache/chunking in BT-D7 (correctness-first, one `collect_at_close` per bar).
2. No multi-month or post-hoc window selection (not attempted).

### 2h smoke (allowable small pilot)

- **Config:** `historical_pilot_smoke_2h_v1.json` — same parameters, `end_utc` = `2026-09-11T14:00:00Z`  
- **Runtime:** ~163 s  
- **Result:** 1 signal, 0 trades, net PnL 0 (no fill/close)  
- **Verdict for smoke:** Live path **works**; **not** a substitute for full 24h PnL statement.

---

## 7. Parameter test (F)

Each `run_bt_d7.py` successful run writes under `phase_bt_d6_config_experiments_v1/`:

- `config.immutable.json`, `run_manifest.json` (config hash, density contract, gap %, TP mode)
- `signals.json`, `orders.json`, `trades.json`, `pnl_report.json`, `integration_result.json`

No optimization sweep — only demonstrated dual-config gap demo + distinct experiment dirs.

---

## 8. Tests (G)

```bash
python -m pytest tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests -q
# 5 passed

python tools/phase_bt_d7_config_driven_short_pnl_integration_v1/run_bt_d7.py --mode synthetic
python tools/phase_bt_d7_config_driven_short_pnl_integration_v1/run_bt_d7.py --mode historical-pilot \
  --config tools/phase_bt_d7_config_driven_short_pnl_integration_v1/configs/historical_pilot_smoke_2h_v1.json
```

| Test area | Coverage |
|-----------|----------|
| Depth-2 density path | Production session → BT-D4; fixture in unit tests |
| Gap change via config | `test_gap_config_changes_signal_count` + gap-demo |
| Short entry / SL / TP | Synthetic fixture (1 SL trade) |
| Pending limit fill | Synthetic path |
| No lookahead | Engine uses `collect_at_close` at decision close only |
| Max one position | Config + engine |
| Fees / net PnL | `test_synthetic_pnl_fields` |
| Equity / drawdown | PnL report keys |
| Determinism | `test_deterministic_repeat` |
| Phase 4D regression | `test_phase4d_unit_unchanged` |

Chart parity / full BT-D3 golden replay are **not** re-run inside BT-D7 (unchanged BT-D3 scope).

---

## 9. Strategy approval status (explicit)

- Source: `strategy_freeze/pool_landscape_v1_candidate/`, `Signal_definierung.md`
- BT-D7 is **research integration** only.
- **Unresolved vs user SSOT:** dense lower TP cluster (`dense_cluster`) — **not implemented** in BT-D7.
- **Implemented TP:** `next_eligible_band` — label as **experimental variant** pending BT-D5 semantic sign-off.

---

## 10. Commands (quick reference)

```bash
# Full integration smoke + gap demo
python tools/phase_bt_d7_config_driven_short_pnl_integration_v1/run_bt_d7.py --mode synthetic

# Historical smoke (2h)
export HISTORICAL_LMV2_BATCH_READER=1
python tools/phase_bt_d7_config_driven_short_pnl_integration_v1/run_bt_d7.py --mode historical-pilot \
  --config tools/phase_bt_d7_config_driven_short_pnl_integration_v1/configs/historical_pilot_smoke_2h_v1.json

# Full 24h (expect ~30+ min; not executed to completion in BT-D7 sign-off)
python tools/phase_bt_d7_config_driven_short_pnl_integration_v1/run_bt_d7.py --mode historical-pilot
```

---

## 11. STOP boundary

Per phase charter: **no** multi-month backtests, **no** freeze final approval, **no** mutation of Original Short V2 / 4D production path / chart SSOT.

**Next optional work (out of scope for BT-D7 sign-off):** density snapshot caching for 24h pilot completion; separate performance phase — not a strategy change.

---

## Final verdict token

**`SHORT_PNL_INTEGRATION_PARTIAL`**

- Use **`SHORT_PNL_INTEGRATION_PASS`** for the **synthetic + config-gap + end-to-end PnL** slice.  
- Use **blocked/incomplete** for **predeclared 24h historical PnL** until a full run finishes with artifacts under `historical_pilot_24h_*`.
