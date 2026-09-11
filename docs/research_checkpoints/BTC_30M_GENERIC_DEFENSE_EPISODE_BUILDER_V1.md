# BTC_30M_GENERIC_DEFENSE_EPISODE_BUILDER_V1

Audit ID: `BTC_30M_GENERIC_DEFENSE_EPISODE_BUILDER_V1`
Schema: `btc_30m_generic_defense_episode_builder_v1`
Run prefix: `gdeb1_`
Book source: `ORDERBOOK_FULL` only

## Purpose

Generic multi-episode defense episode builder for **CLOSED 30m MP zones** on BTCUSDT.

- Discovers zones, chronological visits, and attack clusters automatically
- Selects past-only ask/bid wall candidates at zone touch
- Builds handoff → wall-flow (ask only) → price response → decision snapshots → outcomes
- **No** Episode-1 special branch, **no** `research_visit_count` selector, **no** known touch ISOs / episode IDs as calc inputs

## Outcome contract

Pinned hash of `wall_defense_outcome_contract_v1`:

`efd620a305e8252e61f03c516c69de364049153e6fb02c091caf6137e4e7e46a`

Uses `compute_outcome_facts` + `label_from_facts` + `outcome_contract_hash`.
Does **not** call `apply_episode1_mechanics_test`.

Features and outcomes are physically separated:

- `.../<run_key>/features/`
- `.../<run_key>/outcomes/`

## Absolute read-only data paths

Documented in `config/btc_30m_generic_defense_episode_builder_v1.json` (do not copy raw data):

| Input | Path |
|---|---|
| MP events | `/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/bounded_level_first_analyzer_pilot_v1/BTCUSDT/lf1_69e21d12d280596e/mp_level_events.csv` |
| Clusters | `.../level_clusters.csv` |
| Trades | `.../level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/episode1_independent_derivation_inputs_v1/public_trades_zone_window.jsonl` |
| Candles | `.../candles_1m_zone_window.jsonl` |
| Full OB archive | `/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_v1` |
| Optional Episode-1 persist reuse | `.../level_first_episode1_wall_flow_qdh_base_v1/BTCUSDT/wfq1_58db1918314881b6a` |

Results (worktree only):

`obfull_research_engine/results/btc_30m_generic_defense_episode_builder_v1/BTCUSDT/<run_key>/`

## Reused frozen packages (call, do not edit)

- `bounded_level_first_analyzer_pilot_v1`: `detect_visits_for_cluster`, `episode_id`, `classify_reaction`
- `level_first_episode1_touch_detection_independent_v1`: `build_wall_generations`, `derive_wall_first_touch` (parameterized); do **not** call `derive_zone_first_touch` with `research_visit_count`
- `level_first_episode1_detection_to_wall_flow_integration_v1`: `Episode1Handoff` / `validate_handoff` / `wall_flow_inputs_from_handoff` (generic note: visit_count not a selector)
- `level_first_episode1_wall_flow_qdh_base_v1`: `compute_wall_flow_bundle` + `write_wall_flow_outputs` (**ask-side only**)
- Bid walls: skip wall-flow with exclusion `WALL_FLOW_FROZEN_ASK_ONLY_V1`; still do discovery/handoff/price/outcomes
- `level_first_episode1_price_response_reclaim_v1`: `run_price_response`
- `level_first_episode1_defense_chain_v1`: `build_ask_defense_chain` / `score_generations_past_only` when feasible; else fail closed with exclusion
- `wall_defense_outcome_contract_v1`: facts + labels + hash
- Persist: `replay_window` + `build_states_100ms` + `write_book_tables`

## Snapshots

Anchors: `ZONE_FIRST_TOUCH`, `WALL_FIRST_TOUCH`, `FIRST_JOINT_BREACH`, `DETECTION` (if available)
Offsets (seconds): `0,1,3,5,10,15,30,60,120`
Causality: `event_available_at <= decision_time` for features

## Headroom

Schema keys prepared (`executable_best_bid/ask`, `wall_side`, `decision_time`, `replay_epoch`, `wall_candidates_reference`, `defense_chain_id`, `book_checkpoint_id`, `source_manifest_hash`).
**No trading rule activation** (including no 0.41% rule).

## Attack cluster validity

A cluster is valid for enrichment if it has `zone_touch` and at least one past-only wall candidate that gets a `wall_touch` within continuous book coverage.
Order by `cluster_start` ascending. Never select by outcome/profit.

Enrichment ceiling:

- `--pilot` → apply `max_pilot_clusters` (default 20)
- full mode (no `--pilot`) → **no** implicit 20-cap; process all valid clusters
- `--max-clusters N` → explicit ceiling for either mode (`None` = mode default / unlimited in full)

## Defaults

- Window: `2026-09-06T19:00:00Z` .. `2026-09-06T23:00:00Z`
- tick=`0.1`, band_ticks=`5`, max_pilot_clusters=`20`
- attack_cluster_gap_ms=`60000` (from outcome contract)
- Default absolute paths are **pilot fixtures** (`input_contract=pilot_fixture_v1`). Full mode refuses them; use `config/btc_30m_generic_defense_episode_builder_v1_full_run.json` after regenerating generic inputs.

## Explicitly out of scope

No DOGE, no 1h/4h (except cluster metadata match), no LLD, no OI, no liq, no classifier, no trading signal, no 0.41% rule.

## CLI

```bash
export PYTHONPATH=/home/telgenbuescher/projects/orderbook_analyse_btc30m_v1/obfull_research_engine/src:/home/telgenbuescher/projects/orderbook_analyse/src
# Pilot (fixture paths; ceiling 20):
/home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -m \
  obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.e2e \
  --pilot --run-key gdeb1_pilot

# Full (requires full_run_generic_v1 config with regenerated inputs — not Episode-1 fixtures):
/home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python -m \
  obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.e2e \
  --config /home/telgenbuescher/projects/orderbook_analyse_btc30m_v1/config/btc_30m_generic_defense_episode_builder_v1_full_run.json \
  --run-key gdeb1_full_REPLACE
```

Import smoke:

```bash
... e2e --check-imports
```

## Worktree bootstrap note

Freeze-tag gap fill on this builder branch (not dirty symlinks):

- Added worktree-owned `coverage_catalog.py` (required by `interval_coverage`)
- Lazy `__getattr__` exports in `bounded_level_first_analyzer_pilot_v1`,
  `level_first_episode1_corrected_sms1_persist_v1`, and
  `level_first_window_native_full_ob_direction_v1` so importing episodes/persist
  does not pull ClickHouse precheck / analyze runners

Frozen Episode-1 package **logic** is called, not rewritten.

## CODE_READY status

- Unit tests: 11 passed
- Import smoke: all builder + reused research modules resolve under
  `/home/telgenbuescher/projects/orderbook_analyse_btc30m_v1`
- Contract hash unchanged: `efd620a305e8252e61f03c516c69de364049153e6fb02c091caf6137e4e7e46a`
- Intermediate verdict target: `BTC_30M_GENERIC_DEFENSE_EPISODE_BUILDER_V1_CODE_READY_PUSHED`

## Pilot blockers / caveats

1. **Book persist**: Fresh `replay_window` over `full_ob_v1` is required when the Episode-1 persist fallback does not cover `[zone_touch-5m, max(detection, zone_touch+3m)]`. Overlapping Episode-1 windows can reuse the fallback read-only; most other zones need archive replay (I/O heavy).
2. **Trade/candle window**: Default public trade/candle JSONL is the Episode-1 zone window freeze — visits outside that price/time coverage will be sparse until broader trade/candle inputs are wired.
3. **Ask-only wall-flow**: Bid walls skip `compute_wall_flow_bundle` with `WALL_FLOW_FROZEN_ASK_ONLY_V1`; discovery/handoff/price/outcomes still run.
4. **Defense chain**: Best-effort from `build_wall_generations` + `score_generations_past_only`; fails closed with `DEFENSE_CHAIN_INPUTS_MISSING` when generations/liquidity are insufficient.
5. **Unit tests** are synthetic/fast and do not exercise full archive replay.
