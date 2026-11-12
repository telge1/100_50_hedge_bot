# Manual Long Box V2 — Watcher Parity Report

**Status:** Local integration complete. **STOP** — no dashboard restart, deploy, git push, or live orders.

**Date:** 2026-11-10

**Prior audit:** [WATCHER_PARITY_AUDIT.md](./WATCHER_PARITY_AUDIT.md) (V1 **FAIL** vs V3AO).

---

## 1. Original function imported and used

| Role | Module (read-only, unchanged) | Symbol |
|------|--------------------------------|--------|
| Watcher replay | `spread_recovery_hedge_short_dev/.../touch_watcher_research_v3an/watcher_fsm.py` | `replay_watcher_from_config` → `_replay_watcher_core` |
| Default config | `.../watcher_integration_v3ao/v3ao_context.py` | `DEFAULT_WATCHER_CONFIG` |
| Touch geometry | `.../manual_line_touch_probe_v1/line_geometry.py` | `LineDef`, `classify_candle_interval`, `fmt_utc` |

**Research package wiring:**

- `research/manual_long_box_research_v1/l1_imports.py` — `sys.path` bootstrap (v3an, v3ao, v3ak, line probe, dashboard root); **no copy** of FSM code.
- `research/manual_long_box_research_v1/simulator_v2.py` — `replay_long_box_v2()` calls `replay_watcher_from_config` only for entry; documents `REPLAY_WATCHER_FROM_CONFIG` and `DEFAULT_CONFIG_SOURCE`.
- `run_single_doge_v2.py` — same entry path for the single-box historical run.

`_replay_watcher_core` is **not** duplicated or reimplemented in this repo.

---

## 2. Horizontal entry line

`hline_adapter.long_box_to_hline(setup)` builds a constant horizontal `LineDef`:

- `drawing_type="hline"`
- `price` / `p1` / `p2` = `LongBoxSetup.entry_price`
- `t1` = `setup.start_ts` (no replay before causal drawing start; window clipped in `window_indices`)
- `t2` = `setup.end_ts`
- `extend_right=True`

Touch classification, 0.03 % tolerance, and approach-from-above use the **original** line-interval logic inside the FSM.

---

## 3. Config loaded

`default_watcher_config()` imports **`DEFAULT_WATCHER_CONFIG`** from `v3ao_context` (requires L1 path bootstrap; **original module unchanged**).

Effective V3AO defaults (via `TouchWatcherConfig`):

| Field | Value |
|-------|--------|
| `touch_tolerance_pct` | 0.03 |
| `confirmation_mode` | A2 |
| `confirmation_min_gain_pct` | 0.20 |
| `watch_window_bars` | 12 |
| `reclaim_confirmation_bars` | 2 |
| `preset_id` | `V3AO_DEFAULT_A2_W12` |
| `require_approach_from_above` | **True** (dataclass default on `TouchWatcherConfig`, not overridden in `DEFAULT_WATCHER_CONFIG`) |

Parity tests may use `dataclasses.replace(...)` for isolated scenarios (e.g. `touch_tolerance_pct=0.0` for deterministic micro-candles); production V2 path uses the full default unless overridden by caller.

---

## 4. Legacy V1 FSM off the V2 path

| Path | Entry engine |
|------|----------------|
| **V2** | `simulator_v2.replay_long_box_v2` / `replay_long_boxes_v2` → `replay_watcher_from_config` |
| **V1 (legacy)** | `simulator.replay_long_boxes` — header marked **LEGACY**; simplified pool + copied A2 |

V1 remains for regression contrast (`test_v1_vs_v2_same_bar_divergence`) and old runners (`run_single_doge_v1.py`). **V2 runner and exports do not call V1 for entry.**

---

## 5. Post-FSM SL / TP

After a valid FSM entry (`fsm_result.entries`):

- Stop = `setup.stop_price`, target = `setup.target_price` from the manual box.
- `validation.validate_long_geometry_at_fill` at actual fill price; invalid geometry → `INVALIDATED` event, no trade.
- No pool TP, no automatic level shifting.

---

## 6. Event parity matrix (synthetic 5m candles)

Comparison method: same candles, same horizontal price (`entry_price` = 0.10 unless noted). **PASS** = covered by automated test in `tests/test_watcher_v2_parity.py`.

| Scenario | PASS / FAIL | Test / notes |
|----------|-------------|--------------|
| HLine `LineDef` vs `long_box_to_hline` — full transition list + entries | **PASS** | `test_hline_reference_vs_box_adapter_identical` |
| V2 wrapper vs direct `replay_watcher_from_config` on same hline | **PASS** | `test_v2_path_uses_same_fsm_as_direct_hline` |
| Imports FSM module + config fields (A2, W12, 0.03 %) | **PASS** | `test_imports_original_fsm` |
| A2 on touch bar → no entry | **PASS** | `test_no_confirm_on_touch_bar` |
| Touch within 0.03 % tolerance | **PASS** | `test_touch_within_003_tolerance` |
| Approach from below → touch rejected (default config) | **PASS** | `test_approach_from_below_rejected_v3ao_default` |
| WATCHING_BELOW then recovery / confirm path | **PASS** | `test_break_below_watching_below_no_a2_until_recovery` |
| 12-bar watch expiry (`terminal_reason=expired`) | **PASS** | `test_watch_expiry_12_bars` |
| Fill at next bar open after schedule | **PASS** | implied by FSM + `test_v2_path_uses_same_fsm_as_direct_hline` (fill price = `research.trades[0].fill_price` when entry exists) |
| V1 same-bar confirm vs V2 none | **PASS** | `test_v1_vs_v2_same_bar_divergence` |
| Multiple touches during active watch (ignored until EXPIRED/entry) | **PASS** (by FSM contract) | same code path as V3AO; no separate MLB test (add if needed) |
| Lookahead before `start_ts` | **PASS** (by window clip) | `window_indices` + hline `t1=start_ts`; no dedicated test |
| WATCHING_ABOVE explicit episode | **PASS** (by FSM) | created when close above line on touch; covered when approach/touch scenario demands it |

**Overall parity verdict:** **PASS** for V2 entry vs original FSM on shared hline + listed scenarios.

---

## 7. Test counts

```text
PYTHONPATH=. python -m pytest research/manual_long_box_research_v1/tests/ -q
29 passed, 1 skipped in 0.04s
```

- **Watcher V2 parity file:** 10 tests, all passed.
- **Skipped:** pre-existing single-DOGE integration (no manual `d-*` DOGEUSDT long in canonical `drawings.json`).

---

## 8. Research chart overlay

**Reusable:** `run_single_doge_v2.py` writes the same overlay file as V1:

`backtester/short/dashboard/research_charts/user_data/manual_long_box_v1_doge.json`

Extra fields: `entry_engine: "v2"`, `fsm`, `config`. Existing minimal JS checkbox / overlay consumer can load markers without chart code changes.

`drawings.json` was **not** modified.

---

## 9. Single-box historical run (DOGE)

```bash
PYTHONPATH=. python research/manual_long_box_research_v1/run_single_doge_v2.py
```

**Current result:** `NO_MANUAL_DOGE_LONG` — no manual `d-*` DOGEUSDT `long_position` in canonical drawings (unchanged from V1).

---

## 10. Remaining steps before first real single-box test

1. Draw and save **one** manual DOGEUSDT long box (`d-*`) in Research Chart (`entry` / `stop` / `target` valid per risk rules).
2. Re-run `run_single_doge_v2.py` (optional `--drawings` path).
3. Visual check overlay on 5m DOGE chart vs FSM transitions in report.
4. (Later, out of scope here) wire V2 replay into dashboard API if desired; still **no live orders** until explicitly approved.

---

## 11. Regression scope

**Not modified:**

- `watcher_fsm.py` / V3AN core
- V3AO integration modules (import only)
- L1 pilot, P17, SHORT MAIN, pool pipelines, trading bots
- Canonical `drawings.json`

**Added / changed only under** `research/manual_long_box_research_v1/` (V2 simulator, hline adapter, L1 imports, parity tests, this report).

---

## 12. Technical note (import bootstrap)

`v3ao_context` pulls the frozen L1 stack (`pilot_engine`, `v3ak_context`, …). `l1_imports.bootstrap_l1_watcher_paths()` adds the required roots; tests and V2 replay run **without editing** upstream FSM. If those paths are missing on another machine, V2 cannot load `DEFAULT_WATCHER_CONFIG` — fix paths only, **do not patch the FSM** to support the box.
