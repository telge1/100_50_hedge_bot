# Frozen Wave-Fade BE50 baseline — provenance

## Source

- **Repository:** `/home/telgenbuescher/projects/orderbook_analyse`
- **Commit:** `f16ae32da38da86f39e75b09c63c31f62d11996b`
- **Tree:** `78633c9f001b5c3716fbae2bbbb53e9e0bca9a05`
- **Baseline label:** `fractal_wave_fade_be50_frozen_baseline_v1`
- **Port type:** `LOGIC_PRESERVING`

Note: branch tip `0e41184` is an amend of the same tree (identical content).

## Freeze dependency gap

`fractal_cycle_wave_analysis/indicators.py` in the freeze commit imports:

`orderbook_analyse.mtf_rsi_stoch_audit.indicators` (`wilder_rsi`, `stochastic_rsi`)

That package is **not** in the freeze git tree (never committed; untracked on disk).
The validated backtests ran against that local runtime dependency.
This port **inlines** those exact formulas into `indicators.py` so the package is
self-contained. Documented as `FREEZE_DEPENDENCY_GAP`.

## Ported source files (logic extracted via `git show f16ae32:<path>`)

| Freeze path | Port module |
|-------------|-------------|
| `fractal_cycle_wave_analysis/indicators.py` | `indicators.py` (`attach_indicators`) |
| `mtf_rsi_stoch_audit/indicators.py` (untracked runtime dep) | `indicators.py` (`wilder_rsi`, `stochastic_rsi`) |
| `fractal_cycle_wave_analysis/waves.py` | `waves.py` |
| `fractal_all_wave_fade_generalization/annotate.py` | `annotation.py` |
| `fractal_cycle_phase_failure/events.py` (`local_failure_mask`) | `annotation.py` |
| `fractal_wave_fade_trend_filter/analysis.py` (`assign_trend_bucket`) | `trend.py` |
| `fractal_signal_confluence_db/signals.py` | `signals.py` / `edges.py` |
| `fractal_signal_confluence_db/cluster.py` | `clusters.py` |
| `fractal_dynamic_cluster_upgrade_db/simulate.py` (tpsl/P5A) | `tpsl.py` |
| `fractal_wave_fade_strategy_backtest_db/engine.py` (`prepare_signal_events`) | `selection.py` |
| `fractal_wave_fade_strategy_backtest_db/engine.py` (`_scan_exit` SL_FIRST) | `exits.py` |
| `fractal_wave_fade_global_single_position_db/global_engine.py` (`event_sort_key`) | `selection.py` |
| `fractal_wave_fade_be50_july_2026/simulate.py` | `be50.py` |
| `fractal_wave_fade_be50_full_backtest/simulate_fast.py` | `be50.py` |
| constants from respective `__init__.py` | `parameters.py` |

## Trade management (ported rules vs full sequencer)

Available now (canonical rules, no live executor):

- `FIRST_CLUSTER_ENTRY` / `prepare_signal_events` / `event_sort_key`
- `P5A` via `apply_upgrade_plan`
- `SL_FIRST` via `scan_exit_sl_first`
- BE50 via `simulate_be50_trade` / `simulate_be50_trade_fast`
- Parameters: `POSITION_MODE=GLOBAL_SINGLE`, `CONFLICT_EXIT=True`

**Not** ported as a full chronological executor (deferred to shadow-mode wiring):

- `run_global_single_position` (full FLAT/OPEN loop with books)

Rules and tie-break for that sequencer are encoded in `selection.py` +
`parameters.py`; the live/shadow runner will call them next.

## Q4 / frozen efficiency edges

- Algorithm: APTUSDT waves with `end_available_at <= 2026-08-08T10:21:00Z`
- Quartiles 0.25/0.5/0.75 on `directional_efficiency` and `signed_price_move_pct`
  per `(tf, direction)`
- Shipped snapshot: `data/frozen_eff_edges_apt_is.json` (MySQL freeze path; matches
  APT wave-CSV recompute with same cutoff)
- **Not** refit live; **not** per-coin edges

## Explicitly not ported

Anti-repeat, collision ranker, pre-entry quality audit, MySQL loaders, backtest
CLIs, plotting, equity/cashout simulators, orderbook research packages.
