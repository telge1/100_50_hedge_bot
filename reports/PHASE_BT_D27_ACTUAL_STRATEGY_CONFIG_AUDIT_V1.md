# PHASE BT-D27 — Actual Strategy Code & YAML Config Effectiveness Audit

**Project:** `pools+ob+delta_bot`  
**Date:** 2026-10-25  
**Mode:** Read-only — no code/config changes, no new scanner runs, no PnL simulation  
**Verdict:** **`STRATEGY_CONFIG_AUDIT_VERIFIED`**

**Artifacts:** `results/phase_bt_d27_actual_strategy_audit_v1/`

---

## Executive summary

Three **distinct** executables share YAML **shape** but **not** identical selection semantics:

| Mode | Role | Pool selection | Orders/PnL |
|------|------|----------------|------------|
| **BT-D21** | Research engine | One **lowest** eligible upper / bar + `pool_signal_hold` | Yes |
| **BT-D23** | Setup scanner | **Every** structurally eligible upper (dedup per origin episode) | PLANNED only |
| **BT-D26** | Dynamic selector | One **lowest** eligible upper / bar + causal candidate state | PLANNED only |

**Pilot YAML** (`configs/pilot_bt_d21_pool_first_24h.yaml`) normalized hash **`4822db2515778cb9d108186d5bc8274631d95a4d0786f27167b2eadc91407d9e`** matches stored hashes in `bt_d23_pilot_sep11_24h_v1` and `bt_d26_dynamic_referenz_sep11_24h_v1` `setups.json` (re-loaded YAML today → identical hash).  
**`configs/backtester.yaml`** is the **baseline** (`nearest_upper_proximity_v1`) — **not** used by D23/D26 pilot runs.

---

## 1. Actual code paths (executable proof)

### BT-D23

| Step | Location |
|------|----------|
| CLI | `tools/phase_bt_d23_pool_first_setup_scanner_v1/run_setup_scanner.py` — `main()` L44–70 |
| YAML | `load_yaml_and_validate(args.config)` L51 → `config_yaml_v1.py` |
| Validate | `validate_config_dict` inside `scan_pool_first_setups` L140 |
| Main scan | `setup_scanner_v1.scan_pool_first_setups` L134–309 |
| Per-bar loop | L166–289 |
| **Pool iteration** | **L182–283:** `for band in uppers` — **not** `detect_short_signal_pool_first` |
| Structural gate | `pool_first_signal_v1._structural_eligible_upper` L183–191 |
| Entry | `compute_short_entry_level` L209 |
| SL | `short_sl` L210 |
| TP | `select_short_tp_depth2(entry_level, …)` L212–213 |
| Dedup | `setup_dedup_v1` L214–288 |

Pilot path recorded: `results/setup_scanner_runs/bt_d23_pilot_sep11_24h_v1/config_path.txt` → `configs/pilot_bt_d21_pool_first_24h.yaml`.

### BT-D26

| Step | Location |
|------|----------|
| CLI | `tools/phase_bt_d26_dynamic_upper_pool_selector_v1/run_dynamic_selector.py` — `main()` |
| Validate | `dynamic_selector_v1.scan_dynamic_upper_pool_setups` L99 |
| **Selection** | `detect_short_signal_pool_first` L148–156 → `pool_first_signal_v1` L117–118 **sort by `pool_lo`, take first** |
| State / emit | `candidate_state_v1.DynamicCandidateStateV1.process_bar` L154 |
| Entry/SL/TP | L159–228 (same functions as D23) |

### BT-D21 (trading research)

| Step | Location |
|------|----------|
| Engine | `short_research_engine_v1.run_short_research_backtest` L58+ |
| Signal | `detect_short_signal` L237 → `pool_first_structural_v1` branch `signal_engine_v1` L41–50 |
| Blocked pools | `blocked_pool_keys=pool_signal_hold` L245 |
| Pending / cancel | L167–212, `pool_still_active_for_pending` |
| TP at fill | `select_short_tp_depth2(fill_p, …)` L98–99 |

Evidence run: `results/phase_bt_d22_pool_first_trade_audit_v1/` (same pilot config hash in `config_provenance.json`).

Full trace JSON: `results/phase_bt_d27_actual_strategy_audit_v1/codepath_trace.json`.

---

## 2. Active configuration (pilot vs backtester.yaml)

See **`active_config_map.csv`** and **`config_effectiveness.json`**.

### Pilot normalized highlights (D21/D23/D26)

| Parameter | Effective value | D23/D26 |
|-----------|-----------------|---------|
| `entry_pool_selection_mode` | `pool_first_structural_v1` | **USED** (D23 requires it L142–143) |
| `entry_price_reference` | `upper_pool_lower_edge` | **USED** |
| `entry_offset_pct` | `null` → 0 | **USED** if mode `pool_lower_edge_offset` |
| `upper_gap_min_pct` | 0.5 | **USED** |
| `pool_min_age_bars` | 0 | **USED** (no age block) |
| `upper_pool_proximity_pct` | 0.05 | **IGNORED** (pool_first path) |
| `pending_limit_*` | enabled / causal_v2 | **IGNORED** in scanners |
| `pool_cancel_identity_mode` | `origin_set_v1` | **PARTIAL** D23 dedup end_bar; **USED** D26 |
| `sl_above_upper_top_pct` | 0.2 | **USED** |
| `take_profit.min_distance_pct` | 0.5 | **USED** (eligibility + TP) |
| `take_profit.selection_mode` | `next_eligible_band` | **IGNORED** in scanner code (always `select_short_tp_depth2`) |
| `dense_cluster_semantics_approved` | false | **NOT_IMPLEMENTED** |
| `pnl.*` | present | **IGNORED** in D23/D26 |

### backtester.yaml (baseline CLI / BT-D9)

- `entry_pool_selection_mode: nearest_upper_proximity_v1` → bearish + proximity + nearest high→`pool_lo` (`signal_engine_v1` L63–75).
- **Not** the config used for D23/D26 pilot artifacts.

---

## 3. Entry-pool selection

### Shared structural gates (`_structural_eligible_upper` L20–70)

| Gate | Rule |
|------|------|
| Price | `close < pool_lo` → `POOL_NOT_ABOVE_PRICE` |
| Age | `pool_age_bars[pk] >= pool_min_age_bars` if min > 0 |
| Gap | `has_upper_large_gap_above_pool(uppers, pool_hi, min_gap)` — union intervals, any large gap with `gap_bottom >= pool_upper` |
| Entry vs SL | `entry_level < sl` |
| TP exists | `select_short_tp_depth2` must return band at **planned entry** (scanners) |

**Depth:** PD1 snapshots; bands already depth≥2 from cache (`min_unique_origins: 2`, `exact_unique_origin`).  
**5m / 168h:** `create_density_session` → validated cache bundle (`density_bridge_v1`); warmup contract unchanged in scanner notes.  
**ACTIVE:** Origins/lifecycle in cache SSOT; cancel uses `origin_set_v1` ⊆ present origins in snapshot upper band.

**No bearish / proximity / rejection** on pool_first or scanners (confirmed: no calls in `pool_first_signal_v1` or scanner loops).

### BT-D23 — why many setups per close?

Loop **every** `upper_bands` entry; each passing gate can emit once per **origin-set episode** (`setup_dedup_v1`). Example **12:00 UTC:** **62** setups in artifact (62 distinct origin episodes / bands).

### BT-D26 — one pool how?

`detect_short_signal_pool_first`: collect all passing bands → **sort by `pool_lo` ascending** → **first only**. Tie-break: stable sort on float `pool_lo` only (no secondary key in code).

---

## 4. Entry price

Supported in code (`entry_price_v1.py` L15–26):

| YAML mode | Normalized reference | Active pilot |
|-----------|---------------------|--------------|
| `pool_lower_edge` | `upper_pool_lower_edge` | **Yes** |
| `pool_upper_edge` | `upper_pool_upper_edge` | Supported, not pilot |
| `pool_lower_edge_offset` | `upper_pool_lower_edge` + offset | Supported; pilot offset null |

Offset formula: `entry = pool_lo * (1 - entry_offset_pct/100)` — **wired** via `short_entry_offset_pct` L8–12, not schema-only.

---

## 5. Stop-loss

`short_sl(pool_hi, sl_pct)` → `pool_hi * (1 + sl_pct/100)` (`strategy_rules.py` L29–30).  
Pilot `sl_pct = 0.2` → **×1.002**. **Independent** of entry offset.  
**Identical** function for D23 and D26.

---

## 6. Take-profit (critical)

**Function actually executed:** `select_short_tp_depth2` only — **not** dense cluster, **not** a separate `next_eligible_band` implementation.

| User dense-cluster goal | Implementation |
|-------------------------|----------------|
| Large/dense lower **cluster** as TP | **Not implemented** (`dense_cluster_semantics_approved: false`) |
| Nearest lower with min distance | **Partial:** highest eligible lower **top** ≥0.5% below reference price |
| Skip nearer lowers? | Nearer lowers with **top** closer to entry may fail 0.5% or lose sort |
| TP price | Lower band **upper** edge (`planned_tp` = `top`) |
| Causal | Snapshot at **decision close** for scanners; fill-time snapshot for D21 fills |

**Deviation:** `take_profit.distance_reference: actual_fill_to_lower_upper_edge` — **full** only in BT-D21 at fill; scanners use **planned entry** for both eligibility gate (in `_structural_eligible_upper`) and TP row.

---

## 7. Dynamics & identity

| | BT-D23 | BT-D26 |
|--|--------|--------|
| Dedup / track | `SetupDedupTrackerV1` per `origin_set_key`; clear when origins absent | Single **tracked** candidate; `pool_still_active_for_pending` + bound reconcile |
| New setup | First eligibility per origin episode | `CANDIDATE_SELECTED` / `NEXT_CANDIDATE_SELECTED` only |
| Configurable | `pool_cancel_identity_mode` for inactive detection | Same + **hardcoded** state machine |
| vs deactivation | Origin absent → episode ends | Invalidation + reselect lowest |

**Hardcoded (not YAML):** D23 all-band loop; D26 switch reasons; D21 `pool_signal_hold`, max 1 pending, max 1 position.

---

## 8. Historical evidence (artifacts only)

See **`reference_cases.md`**.

| UTC | D21 signal | D23 emits | D26 selected pool (low/high) |
|-----|------------|-----------|------------------------------|
| 12:00 | Yes 0.08365/0.08366 (then cancelled) | 62 | 0.08365/0.08366 (lowest) |
| 15:35 | **No** (pending #7) | 1 × 0.08681/0.086838 | 0.08681/0.086838 |
| 17:00 | **No** | 1 × 0.08593/0.085982 | 0.08593/0.085982 |

SL numeric check (15:35): `0.086838 * 1.002 = 0.087011676` matches `planned_sl` in both scanner JSONs.

---

## 9. Config change would matter?

| Parameter | D23/D26 effect if changed |
|-----------|---------------------------|
| `entry_price_mode` / offset | Yes → entry + TP distance |
| `gap_min_pct` / `upper_gap_min_pct` | Yes → structural pass/fail |
| `sl_above_upper_top_pct` | Yes → `planned_sl` |
| `min_distance_pct` | Yes → eligibility + TP band |
| `pool_min_age_bars` | Yes if > 0 |
| `signal_selection_mode` | Must stay `pool_first_structural_v1` or scanner **raises** |
| `pool_proximity_pct` | **No** on pool_first |
| `pending_limit_*` | **No** in scanners |
| `selection_mode` dense_cluster | **No** (not implemented) |
| `pool_cancel_identity_mode` | D23 dedup inactive; D26 invalidation |

---

## 10. Comparison table

See **`btd21_btd23_btd26_comparison.md`**.

---

## 11. Direct answers

1. **BT-D26 pool:** Lowest `pool_lower_edge` among structurally eligible uppers at that 5m close (`detect_short_signal_pool_first`).
2. **Conditions:** Same as `_structural_eligible_upper` (close below pool lo, gap, entry&lt;SL, TP exists at planned entry); **no** bearish/proximity.
3. **Entry:** `pool_lo` × (1 − offset/100); pilot offset 0 → `pool_lo`.
4. **SL:** `pool_upper * 1.002` (pilot).
5. **TP:** Highest lower **top** with ≥0.5% below entry reference (`select_short_tp_depth2`); not dense cluster.
6. **YAML works (pilot scanners):** gap, age, entry mode/offset, SL%, min TP%, density bundle path, pool_first mode.
7. **Ignored (scanners):** proximity, pending, pnl, `selection_mode` branch, dense cluster, `distance_reference` semantics at fill.
8. **Hardcoded:** D23 all-band loop; D26 state machine; D21 hold/pending/position caps.
9. **Concept gaps:** “Relevant” pool vs lowest edge; dense TP; fill-based TP distance in scanners; D23 ≠ single tracked pool.
10. **Change later:** Pool pick → `setup_scanner_v1` loop or `detect_short_signal_pool_first` / D26 state; TP → `strategy_rules.select_short_tp_depth2` or new function + engine L66–68; baseline proximity → `signal_engine_v1` nearest branch; identity → `pool_identity_v1` + dedup/state modules.

---

## VERDICT

**`STRATEGY_CONFIG_AUDIT_VERIFIED`** — Code paths, config hash, and artifact-backed reference closes align. Residual nuance (TP reference price scanners vs fills) documented as **TEILWEISE**, not blocking.

**STOP** — read-only audit complete.
