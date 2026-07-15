# RESEARCH_PERFORMANCE_AND_METRICS.md

Performance, state-metric and cache root-fix for the regime-scanner research
system. No scanner domain logic is changed by this work.

## 1. Proven starting point

Pilot variants over five market windows produced transition-bucket shares of
~97–100 % for every expected character (uptrend/downtrend/range/transition/
mixed). The `trend_up_late_feb` window scored **+6.5** despite

```
transition_share = 1.0
state_changes = 1
median_state_duration ≈ 1008.5
```

1-week runs took ~200 s; the 6-week run ~4950 s — clearly super-linear.

## 2. Interrupted multi-window run (Phase 1)

* No research process was running at pivot start.
* The earlier `resume-window-set` (pid 539515) had already been SIGTERM’d
  (exit 143).
* One leftover MySQL run was marked `interrupted`:

  * `aead9137-3252-4856-ac3a-008226428f7e`
  * window `mixed_feb_mar_six_weeks` / variant `faster_confirmation`
  * zero trend/structure rows persisted (atomic write)
* No completed run was modified or deleted.

Completed combinations at pivot:

| Window | Variants completed |
|---|---|
| `transition_march_week` | all 5 |
| `trend_up_late_feb` | all 5 |
| `trend_down_early_jun` | all 5 |
| `range_late_may` | all 5 |
| `mixed_feb_mar_six_weeks` | baseline, slower_confirmation |

Missing (never built): three mixed-window variants.

## 3. Raw-state distribution (Phase 2)

Actual stored raw states for `baseline` (counts → share):

```
trend_up_late_feb:
  bearish_weakening 1737  86.12%
  bottoming          280  13.88%

trend_down_early_jun:
  bottoming         1396  69.21%
  bullish_weakening  412  20.43%
  bearish_weakening  141   6.99%
  early_bearish / warning / strong_*/early_bullish / neutral  < 3.4%

range_late_may:
  topping           1089  53.99%
  bullish_weakening  644  31.93%
  bearish_weakening  227  11.25%
  early_*/strong_*   < 2.9%

transition_march_week:
  bullish_weakening 1404  69.61%
  bearish_weakening  437  21.67%
  topping / bottoming / early_* / strong_*  < 9%

mixed_feb_mar_six_weeks:
  bullish_weakening 4660  38.52%
  bearish_weakening 3722  30.77%
  topping           1762  14.57%
  bottoming         1714  14.17%
  early_*/strong_*  < 2%
```

Distinct raw states observed: `bearish_warning`, `bearish_weakening`,
`bottoming`, `bullish_weakening`, `early_bearish`, `early_bullish`, `neutral`,
`strong_bearish`, `strong_bullish`, `topping`. (`unavailable` / `bullish_warning`
did not appear in these windows.)

Artefacts live under `results_state_metric_audit/`.

## 4. Bucket mapping (Phase 3–4)

Canonical research function:

```python
classify_research_state_bucket(snapshot) -> str
```

in `research_variants/state_buckets.py`. Mapping (research-only; never
influences the scanner):

| Raw state | Bucket | Reason |
|---|---|---|
| `strong_bullish` | uptrend | confirmed bullish trend |
| `early_bullish` | uptrend | emerging bullish trend |
| `bullish_warning` | uptrend | bullish with early warning |
| `strong_bearish` | downtrend | confirmed bearish trend |
| `early_bearish` | downtrend | emerging bearish trend |
| `bearish_warning` | downtrend | bearish with early warning |
| `neutral` | range | explicit no-trend / range |
| `bullish_weakening` | transition | post-bullish turning state |
| `bearish_weakening` | transition | post-bearish turning state |
| `topping` | transition | top-formation turning state |
| `bottoming` | transition | bottom-formation turning state |
| `unavailable` | unknown | warmup / no data |

Parity: for every baseline window `sum(bucket_counts) == row_count`
(`audit_summary.json`). No state is dropped or double-counted. Mapping is
identical to the previous `stability.py` taxonomy (purely relocated).

**Root cause of the “100 % transition” observation is NOT a mapping bug.** The
scanner genuinely produces ~97–100 % transition-bucket states across every
pilot window. Weakening / topping / bottoming are the state machine’s
legitimate turning states.

## 5. Score-component root cause (Phase 5–6)

`trend_up_late_feb` / baseline score components (exact sum = +6.5):

| Component | raw | weight | weighted |
|---|---|---|---|
| structure_consistency | 0.50 (default) | +25 | **+12.5** |
| median_duration | 1.00 (saturated) | +20 | **+20.0** |
| transition_resolution | 0.00 | +15 | 0 |
| short_run_penalty | 0 | −2 | 0 |
| reversal_penalty | 0 | −3 | 0 |
| structure_conflict_penalty | 0 | −2 | 0 |
| excessive_transition_penalty | 0.65 | −40 | **−26.0** |
| **SUM** | | | **+6.5** |

Old degenerate rule checked *single raw-state* dominance (> 0.90). Here the
dominant raw state is `bearish_weakening` at **86 %** — below the threshold —
so the window was treated as healthy, and a long-duration *false* transition
state earned a positive bonus.

### Corrected rules (`score_version=2`)

* Degenerate when **any bucket share > 0.90**, or when meaningful
  (up+down+range) share < 0.10:

  * `excessive_transition`
  * `mostly_unknown`
  * `no_meaningful_regime`
  * (single-trend dominance without structure remains `single_state_dominant`)
* A degenerate window is **never** score-positive:
  `stability_score = min(raw_component_score, −50)`.
* `rankable = false` for degenerate windows.
* `window_character_fit` is computed separately and never mixed into the
  stability score.

## 6. Pilot re-evaluation without a scanner (Phase 7)

`evaluate-window-set-from-cache --rescore-only` recomputed all 22 existing
runs in **1.54 seconds**, `scanner_runs_started=0`, candles and validation
runs unchanged.

All 22 become:

* `degenerate=true`, `degenerate_reason=excessive_transition`
* `stability_score = −50`
* `rankable=false`
* character fit ≈ 0 for uptrend / downtrend / range / mixed windows;
  ≈ 0.49 for the transition window (still not rankable)

`trend_up_late_feb` is no longer a “good” result.

## 7. Performance profile (Phase 9–10)

Hotspot for a 2-day sample (~973 replay bars, 85.7 s total):

```
aggregate_candles   1510 calls   70.9 s cumtime   (~83%)
pandas __getitem__               26.7 s cumtime
pandas _get_item_cache           21.5 s cumtime
```

Code path: every bar of `run_trend_state_timeline` does
`candles_as_of = df.iloc[:i+1]` and then `step_trend_state` re-aggregates
that prefix into 15m/30m via `aggregate_candles` → **O(n²)**.

Complexity benchmark (same warm-up, growing analysis window):

| days | replay_bars | seconds | ms/bar | ratio_vs_prev |
|---|---|---|---|---|
| 1 | 685 | 17.7 | 25.9 | — |
| 2 | 973 | 35.3 | 36.3 | 1.99 |
| 4 | 1549 | 86.4 | 55.8 | 2.45 |

Per-bar cost grows with N. Runtime grows faster than bar count — consistent
with **O(n²)** aggregation work. Artefacts under `results_research_performance/`.

Scanner domain logic is **not** modified (it is a foreign dirty file). The
architectural fix replaces `variant × window × scanner` with one timeline
per parameter set.

## 8. Variant-independent vs variant-dependent

| Layer | Independent of `TrendStateConfig`? |
|---|---|
| Candle load + 5m/15m/30m aggregation for input hashes | Yes |
| Indicator frame (ATR / RSI / EMA on 5m) | Yes (scanner_cfg fixed) |
| Swing / pivot candidates | Yes |
| Structure events at a given decision time | Partially — structure CFG can vary via overrides, but baseline variants currently only change min_hold / strength thresholds |
| Trend-state decision / confirmation / hold / strength | **No — variant-dependent** |

Prepared context therefore covers candles + feature / scanner config. Timeline
covers the parameter-set-specific state machine output.

## 9. Architecture (Phase 8, 11–20)

```
candles (unchanged)
        ↓
PreparedContext        (feature_config_hash + candle hashes + code version)
        ↓
Timeline               (prepared_context_hash + parameter_hash + scanner_version
                        + warm-up / timeline bounds + decision-time semantics)
        ↓
WindowEvaluation       (timeline_id + window_hash + metric_version + score_version)
```

Reuse rules: every identity field must match exactly. Failures stay
`failed`/`interrupted` and are never reused. `--force-rebuild` creates a new
artifact without touching completed rows.

Hard protection rule — every scanner call logs:

```
[cache] lookup prepared_context_hash=…
[cache] completed prepared context found: yes/no
[cache] lookup timeline_fingerprint=…
[cache] completed timeline found: yes/no
```

Parallel build protection: unique key + atomic `building` status on
`research_prepared_contexts`. A racing claim returns `in_progress` and never
starts a second expensive build.

Version split:

* `scanner_version` / `prepared_context_version` — timeline identity
* `metric_version=2` — bucket counts (currently compatible with v1 counts)
* `score_version=2` — new degenerate + character-fit rules

Score / metric / window changes never trigger a scanner run.

## 10. Timeline vs single-window parity (Phase 21)

Using the tightest covering completed timeline (preferring the exact
per-window run when present):

| Window | trend count | trend hash | structure count | structure hash |
|---|---|---|---|---|
| `trend_up_late_feb` | 2017 = 2017 | equal | 1294 = 1294 | equal |
| `transition_march_week` | 2017 = 2017 | equal | 1518 = 1518 | equal |

Forced slice from the *mixed* 6-week timeline into `transition_march_week`
diverges at bar 0 (`bullish_weakening` vs `bearish_weakening`). Cause: the
per-window runner truncates replay to `start − 450 bars`
(`_indicator_window`), so March’s separate warm-up is shorter than the deep
mixed history. Canonical multi-window reuse therefore requires **one common
warm-up depth** (the window-set warm-up) for future timeline builds. Existing
tightest covering timelines remain byte-equivalent to their original
single-window runs.

## 11. New CLI (Phase 22–24)

```bash
PYTHONPATH=. python3 -m research.regime_scanner.research_cache \
  build-prepared-context --exchange bybit --symbol APTUSDT \
  --data-source mysql --warmup-start ... --end ...

PYTHONPATH=. python3 -m research.regime_scanner.research_cache \
  build-timeline --variant baseline --window-set regime_market_windows_v1
  # (--confirm-build required to actually launch a scanner)

PYTHONPATH=. python3 -m research.regime_scanner.research_variants \
  evaluate-window-set-from-cache \
  --variant-set simple_regime_stability_v1 \
  --window-set regime_market_windows_v1 [--rescore-only]

PYTHONPATH=. python3 -m research.regime_scanner.research_variants \
  audit-state-metrics \
  --variant-set simple_regime_stability_v1 \
  --window-set regime_market_windows_v1
```

Progress goes to stderr; final JSON to stdout. Resume of *half-built*
timelines is deliberately **not** implemented — the state-machine runtime is
not fully serialised. Interrupted builds are marked and a fresh build starts
from warm-up. Completed timelines are reused.

## 12. Measured times after the fix

| Operation | Time | Scanner runs |
|---|---|---|
| First expensive timeline build | unchanged O(n²) | 1 |
| Identical second `build-timeline` | seconds (lookup) | 0 |
| Five windows from cache | **1.54 s for 22 evals** | 0 |
| Score formula change (`--rescore-only`) | seconds | 0 |
| Metric version change | seconds (reuse timeline) | 0 |
| New window | only a new window evaluation | 0 |
| `--force-rebuild` | one deliberate rebuild | 1 |

Previous architecture: 5 variants × 5 windows = **25** scanner runs.
Target: **5** scanner runs (one per variant) + near-free evaluation.

## 13. Resume / interrupted semantics

* Completed timelines remain completed forever.
* Running → interrupted (or failed) on SIGTERM / crash; reason stored.
* Interrupted / failed timelines are never reused for evaluation.
* No partial-resume of a mid-timeline build (would require serialising the
  full `TrendRuntime`, which this root-fix deliberately avoids claiming).

## 14. What was deliberately NOT changed

Scanner state machine, structure, price action, momentum, regime thresholds,
variant / baseline parameters, candle data / schema, validation runs,
completed research runs, raw trend/structure outputs, live bots,
`fixed_cycle_config.json`, foreign dirty files, `.env.regime_db`. No new
variants, windows or profit scoring.

## 15. Tests

* `tests/test_research_root_fix.py` — 26 new tests covering bucket mapping,
  score components, degenerate rules, character fit, slice semantics,
  covering detection, fingerprint identity and rescoring purity.
* Existing `test_research_variants_multiwindow.py` + `test_research_variants.py`
  + `test_research_runs.py` (without the long MySQL baseline integration
  re-run) — all green after the refactor.
