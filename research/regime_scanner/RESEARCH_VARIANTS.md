# Research Variants — Controlled Regime Stability Comparison

## Purpose

Run a **small, named set** of parameter variants through the existing
`research_runs` baseline runner, compute **stability metrics** on stored trend
states and structure events, and rank variants transparently.

This is **not** profit optimization, grid search, or live trading.

## First variant set: `simple_regime_stability_v1`

| Variant | Hypothesis | Overrides |
|---------|------------|-----------|
| `baseline` | Reference | none |
| `faster_confirmation` | Faster regime commits | `min_hold_bars` −1 for strong/early trends |
| `slower_confirmation` | Slower regime commits | `min_hold_bars` +1 for strong/early trends |
| `stricter_trend_strength` | Stricter ADX/DI confirmation | `adx_confirm` 22, `di_spread_confirm` 7 |
| `looser_trend_strength` | Looser ADX/DI confirmation | `adx_confirm` 15, `di_spread_confirm` 3 |

All paths are whitelisted under `trend_state.*` in `research_runs/parameters.py`.

## Baseline protection

Before any variant run:

```text
parameter_hash(baseline) == 46becb86a9e736ee07a1dab14df3a14a2f90d7fe600ec6d83df16e179556ea66
```

After the set baseline run completes, parity is checked against stored reference
run `64534bb1-3be8-4050-8f10-7fda99fc0de1`.

## Variant hash

```text
sha256(json({
  name, description, tags,
  parameter_overrides (sorted),
  resulting_parameter_hash,
  runner_version
}))
```

Excludes `run_id`, timestamps, duration.

## State taxonomy (actual TrendState names)

| Bucket | States |
|--------|--------|
| Uptrend | `strong_bullish`, `early_bullish`, `bullish_warning` |
| Downtrend | `strong_bearish`, `early_bearish`, `bearish_warning` |
| Range | `neutral` |
| Transition | `bullish_weakening`, `bearish_weakening`, `topping`, `bottoming` |
| Unknown | `unavailable` |

## Stability metrics

See `research_variants/stability.py` for full definitions.

Flip-flop threshold (analysis only): **short run &lt; 3 bars**.

Degenerate if:

- one state &gt; 90% of bars,
- mostly `unavailable`,
- or no state changes in a non-trivial window.

## Score (transparent, no profit)

```text
score =
  structure_consistency * 25
  + min(median_duration/6, 1) * 20
  + transition_resolution * 15
  - short_runs*2 - reversal_3*3
  - conflicts*2
  - max(0, transition_share-0.35)*40
```

Degenerate variants receive score **−50**.

## CLI

```bash
PYTHONPATH=. python3 -m research.regime_scanner.research_variants init-schema

PYTHONPATH=. python3 -m research.regime_scanner.research_variants list-variants \
  --variant-set simple_regime_stability_v1

PYTHONPATH=. python3 -m research.regime_scanner.research_variants run-set \
  --variant-set simple_regime_stability_v1 \
  --data-source mysql

PYTHONPATH=. python3 -m research.regime_scanner.research_variants compare-set \
  --variant-set simple_regime_stability_v1

PYTHONPATH=. python3 -m research.regime_scanner.research_variants repeat-best \
  --variant-set simple_regime_stability_v1
```

Default: pipeline skipped (`not_exported` signals unchanged from research runs).

## Artifacts

```text
research/regime_scanner/results_research_variants/
  simple_regime_stability_v1_summary.json
  simple_regime_stability_v1_ranking.csv
  simple_regime_stability_v1_metrics.csv
  simple_regime_stability_v1_parameter_diff.csv
```

## Known limitations

- No profit / entry quality evaluation.
- Structure-turn metrics depend on stored structure events and trend metadata.
- Full PA/Momentum pipeline not required for this step.

## Next step

After proven infrastructure: additional variant sets on other windows or
isolated structure parameters — still without automatic optimization.
