# Active Research Pipeline — MP / QDH / First-Touch Freeze

**Status:** Research baseline freeze (`mp-qdh-first-touch-freeze-v1-20260918`)  
**Strategy phase:** Causal feature + outcome dataset (research only). **Not** live / Signal V2.

## 1. Active entry-point

```bash
PYTHONPATH=obfull_research_engine/src:/path/to/orderbook_analyse/src \
  python -m obfull_research_engine.mp_qdh_first_touch_study_v1 \
    --source-run-dir /absolute/path/to/mp_edge_event_batch_v1_20260916
```

Or via environment:

```bash
export OBFULL_RESEARCH_SOURCE_RUN_DIR=/absolute/path/to/mp_edge_event_batch_v1_20260916
python -m obfull_research_engine.mp_qdh_first_touch_study_v1
```

**Source-run priority:** `--source-run-dir` > `OBFULL_RESEARCH_SOURCE_RUN_DIR` > repo-local `obfull_research_engine/runs/mp_edge_event_batch_v1_20260916` only if that directory exists.

`runs/` stays gitignored. Do not copy or commit historical batch CSVs into the worktree. Unit tests are self-contained; the historical 282→120→115 check is an optional `@pytest.mark.integration` test.

Call graph:

`run_cli.main` → `run_study.run_study` → `analyze_first_touch_event` → `analyze_one_event_v2` → `audit_one_event` + `build_flow_100ms_v2`

Smoke only:

```bash
python -m obfull_research_engine.mp_qdh_first_touch_study_v1 --smoke-only \
  --source-run-dir "$OBFULL_RESEARCH_SOURCE_RUN_DIR" \
  --smoke-out-dir obfull_research_engine/runs/<smoke_dir>
```

## 2. Data flow

MP batch episodes → first-touch universe freeze (115) → Silver L2 + public trades → wall linkage → attribution intervals → 100ms flow (fill/pull/UNKNOWN/QDH/IE/depth) → typed timeline view + FootprintClusterEvent view → confidence split → 1m candle MFE/MAE outcomes → reports.

## 3. Active direct modules

- `mp_qdh_first_touch_study_v1` (entry, universe, confidence, outcomes, typed timeline, footprint, contract)
- `mp_qdh_30event_case_control_v2` (analyze_one, flow_v2, features_v2, coverage)

## 4. Active transitive modules

- `mp_qdh_wall_linkage_audit_v1`
- `mp_qdh_canonical_integration_v1` (event_load, near_zero)
- `mp_qdh_30event_case_control_v1` (wall_movement, paired/bootstrap)
- `level_first_episode1_wall_flow_qdh_base_v1` (attribution, aggressor, QDH, mass, price_response, timeline_100ms)
- `mp_price_path_4h_v1` (candles, geometry, path_engine)
- `mp_wall_flow_qdh_silver_v1` (silver adapters)
- `mp_big_move_case_control_v1.stats`
- `timeparse`, `drilldown/aggregation_100ms`
- ClickHouse helpers (read-only)

## 5. Legacy / reproducibility modules

See `DEPRECATED_RESEARCH_MODULES.md`. Kept for historical runs and regression tests; not the active strategy entry.

## 6. Not implemented / not active

- WallStateClassifier
- Signal V2
- normalized Impact Efficiency (status `NOT_CALIBRATED`)
- Absorption Ratio (`NOT_CALIBRATED`)
- Vacuum Score (`NOT_CALIBRATED`)
- OI / Liquidations enrichment (`M_OI=M_Liq=1`)
- Walk-forward validation

## 7. Data sources

- Silver: `research_full_ob_silver_v1_3` (read-only)
- Public trades: mandatory for fills
- Candles: `signal_generator.candles_1m` for outcomes only
- Batch: `runs/mp_edge_event_batch_v1_20260916`

## 8–10. Contract / checkpoint / universe

- Contract hashes **parameter body + SHA256 of listed transitive sources** (byte hash; comments count).
- Legacy param-only hash `2bbd0ec0…` is **rejected**.
- Checkpoints require matching `contract_hash` + `universe_hash`.
- Universe: first-touch only, exclude UNRESOLVED, require trade side; **no outcome/feature filter**.
- Expected universe hash (historical FT run): `c76eac60…`

## 11–12. MFE/MAE

Percent geometry; decision- and touch-relative; target 0.41%; TP/SL grids; costs 0.08/0.12; TRUE_BREAK uses break side.

## 13. Known warnings (allowed)

- Receive-time missing → no live latency claim
- High UNKNOWN (excluded from QDH)
- Abs/Vac/normalized IE NOT_CALIBRATED
- No WallStateClassifier / Signal V2 / walk-forward
- NO_CONFIRMED_EDGE on FT study

## 14. Tests

```bash
PYTHONPATH=obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src \
  python -m pytest obfull_research_engine/tests/test_mp_qdh_*.py \
    obfull_research_engine/tests/test_mp_price_path_4h_v1_offline.py \
    obfull_research_engine/tests/test_mp_edge_event_*offline.py \
    obfull_research_engine/tests/test_mp_big_move_case_control_v1_offline.py \
    obfull_research_engine/tests/test_mp_ob_feature_enrichment_v1_offline.py \
    obfull_research_engine/tests/test_level_first_episode1_wall_flow_qdh_base_v1.py \
    obfull_research_engine/tests/test_mp_qdh_first_touch_freeze_v1_offline.py -q
```

## 15. Resume

Same `out_dir`, identical contract + universe hashes; stale checkpoints rejected.

## 16. Typed timeline & footprint

- `typed_timeline.py`: masterplan event-type view over attribution + wall moves (no second replay).
- `footprint_cluster.py`: cluster from **same** attributed trade IDs as QDH hits; `adds_to_qdh_hits=False`.
