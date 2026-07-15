# Multi-Window Variant Stability Evaluation

## Purpose

The single-window variant runner (`simple_regime_stability_v1` on the March week) showed a small score advantage for `slower_confirmation`, but that window was ~99% transition-dominated. This step runs the **same five variants** across **multiple pre-defined market windows** and compares stability **across windows**, not profit.

Goals:

- Find which variant reacts most consistently across different market conditions
- Avoid overfitting parameter choices to one transition-heavy week
- Reuse completed research runs when fingerprints match

Non-goals:

- No new parameters or variants
- No optimization loop
- No profit or live-deployment conclusions

## Why one window is not enough

A single dominated regime (e.g. transition) can rank variants by noise or transition-handling quirks. Multi-window evaluation exposes regime-dependent strengths and weaknesses before any parameter decision.

## Window selection (`regime_market_windows_v1`)

Windows were chosen **before** full variant ranking using **candle-only evidence** (`window_selection.py`):

| Window | Period | Character | Selection basis |
|--------|--------|-----------|-----------------|
| `transition_march_week` | 2026-03-01 → 2026-03-08 | transition | Retained from prior variant run |
| `trend_up_late_feb` | 2026-02-25 → 2026-03-04 | uptrend | Highest 7d return in scan |
| `trend_down_early_jun` | 2026-06-01 → 2026-06-08 | downtrend | Lowest 7d return in scan |
| `range_late_may` | 2026-05-23 → 2026-05-30 | range | Minimal net move |
| `mixed_feb_mar_six_weeks` | 2026-02-01 → 2026-03-15 | mixed | Multi-week span |

Warm-up for all windows: `2025-12-27T00:00:00Z` (canonical, unchanged scanner semantics).

Evidence per window is stored in `evidence` on each `ResearchWindow` and in result artifacts.

### Pilot finding (scanner vs price character)

Candle-based window selection correctly found divergent **price** regimes. Baseline TrendState
labels, however, were ~97–100% `transition` (`weakening`/`topping`/`bottoming`) in all five
windows. That is a **taxonomy/labeling property of the current scanner**, not evidence that
the windows are identical: stability scores still differed strongly (e.g. uptrend window score
`6.5` vs transition March `≈ -5.57`).

Plausibility therefore separates:

- hard failures (degenerate metrics, wrong directional share when directional bars exist)
- warnings (`scanner_price_mismatch`) when price character and scanner state labels diverge

No windows were swapped after seeing full five-variant rankings. Replacement would require a
new versioned set (`regime_market_windows_v2`) selected from baseline state evidence only.

## Hashes

- `window_hash`: SHA256 of canonical window JSON (no run IDs, runtimes, or secrets)
- `window_set_hash`: SHA256 of full window set JSON

List hashes:

```bash
PYTHONPATH=. python3 -m research.regime_scanner.research_variants \
  list-windows --window-set regime_market_windows_v1
```

## CLI

```bash
# Initialize schema (includes research_window_sets, research_variant_window_runs)
PYTHONPATH=. python3 -m research.regime_scanner.research_variants init-schema

# List windows
PYTHONPATH=. python3 -m research.regime_scanner.research_variants \
  list-windows --window-set regime_market_windows_v1

# Pilot: baseline + slower_confirmation across all windows
PYTHONPATH=. python3 -m research.regime_scanner.research_variants \
  run-window-set \
  --variant-set simple_regime_stability_v1 \
  --window-set regime_market_windows_v1 \
  --exchange bybit --symbol APTUSDT --data-source mysql \
  --pilot

# Full 5×5 matrix (after pilot is plausible)
PYTHONPATH=. python3 -m research.regime_scanner.research_variants \
  run-window-set \
  --variant-set simple_regime_stability_v1 \
  --window-set regime_market_windows_v1 \
  --exchange bybit --symbol APTUSDT --data-source mysql

# Compare / regenerate reports from DB
PYTHONPATH=. python3 -m research.regime_scanner.research_variants \
  compare-window-set \
  --variant-set simple_regime_stability_v1 \
  --window-set regime_market_windows_v1

# Resume incomplete matrix (reuses completed cells)
PYTHONPATH=. python3 -m research.regime_scanner.research_variants \
  resume-window-set \
  --variant-set simple_regime_stability_v1 \
  --window-set regime_market_windows_v1
```

Default: `reuse_completed=true`. Use `--no-reuse` to force new runs.

Runs execute **sequentially** (no parallelism in this step).

## Pilot stage

Before the full 5×5 matrix:

1. Run only `baseline` and `slower_confirmation` on all windows
2. Measure runtimes and metric plausibility
3. Reuse March week runs from `simple_regime_stability_v1` when fingerprints match

## Run reuse

Before starting a cell, the runner checks:

1. Existing `research_variant_window_runs` row (completed)
2. `research_runs` with matching `run_fingerprint` and `status=completed`
3. For `transition_march_week`, prior `research_variant_runs` from the same variant set

Failed runs are never reused. Reused runs are linked, not copied, and marked `reused=true`.

## Per-window metrics

Same stability metrics as the single-window variant runner (`stability.py`), plus window shares:

- `dominant_state`, `dominant_state_share`
- `uptrend_share`, `downtrend_share`, `range_share`, `transition_share`, `unknown_share`

Score formula is **unchanged**.

## Cross-window aggregation

Per variant:

- Score: `mean`, `median`, `min`, `max`, `stddev`
- Rank: `rank_per_window`, `mean_rank`, `median_rank`, `worst_rank`, `top_1_count`, `top_2_count`
- Coverage: non-degenerate / degenerate window counts, success by expected character

### Robustness score (transparent weights)

```
robustness_score =
    1.0 * median_score
  - 0.5 * score_stddev
  - 0.25 * abs(minimum_score - median_score)
  - 10.0 * degenerate_window_count
```

Raw values are always reported alongside. Rankings use `robustness_score` but conclusions are limited to **stability in this test**, not “best strategy”.

## Window character plausibility

After each window’s baseline run, `check_window_plausibility()` compares expected character vs state shares. Implausible windows are documented; they are not silently renamed or swapped after variant rankings.

## Result artifacts

`research/regime_scanner/results_research_variants_multiwindow/`:

- `regime_market_windows_v1_windows.json`
- `simple_regime_stability_v1_by_window.csv`
- `simple_regime_stability_v1_aggregate_ranking.csv`
- `simple_regime_stability_v1_metric_matrix.csv`
- `simple_regime_stability_v1_rank_matrix.csv`
- `simple_regime_stability_v1_baseline_deltas.csv`
- `simple_regime_stability_v1_summary.json`

## Database tables

- `research_window_sets`: versioned window definitions
- `research_variant_window_runs`: variant × window × run link with score and metadata

Existing research and candle tables are not modified.

## Limits

- Only describes behavior on **selected** APTUSDT windows
- Does not predict profitability or recommend live deployment
- Windows are fixed at `regime_market_windows_v1`; changes require a new version (e.g. `v2`)

## Next steps (optional)

- Add more windows or symbols with a new window-set version
- Chart-review plausibility for borderline windows
- Entry/exit research only after a stable variant choice under multi-window evidence
