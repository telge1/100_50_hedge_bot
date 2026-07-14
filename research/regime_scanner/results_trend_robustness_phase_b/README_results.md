# Trend / Regime Robustness — Phase B Results

Read-only causal audit of the existing trend state machine vs transparent ground truth.
No live wiring. No threshold changes. No writes into `research/regime_scanner/results/`.

## Window
- Load/warmup from `2025-12-27T00:00:00+00:00`
- Analyze `2026-03-01T00:00:00+00:00` → `2026-05-31T23:59:59+00:00`
- Bars analyzed: **26497**
- March case: `2026-03-05T18:00:00+00:00` → `2026-03-10T00:00:00+00:00`

## Ground truth
- CLEAR_UPTREND / CLEAR_DOWNTREND / CLEAR_SIDEWAYS / AMBIGUOUS
- AMBIGUOUS is never counted as FP/FN

## Audit-class map
- UPTREND ← strong_bullish, early_bullish
- DOWNTREND ← strong_bearish, early_bearish
- SIDEWAYS ← neutral
- BOTTOMING / TOPPING ← same SM states
- UNCLEAR ← unavailable, warnings, weakenings, other

## Headline classification (clear GT only)
- Clear bars: 6961; ambiguous excluded: 19536
- Overall clear match rate: 0.01206723171958052

## Detection delays (candles)
- CLEAR_UPTREND first match: {'n_episodes': 306, 'n_with_detection': 4, 'n_missed': 302, 'median': 0.0, 'p75': 0.0, 'p90': 0.0, 'max': 0.0}
- CLEAR_DOWNTREND first match: {'n_episodes': 335, 'n_with_detection': 3, 'n_missed': 332, 'median': 0.0, 'p75': 0.0, 'p90': 0.0, 'max': 0.0}

## Files
- `summary.json`, `state_distribution.csv`, `transition_matrix.csv`, `transition_ping_pong.csv`
- `trend_detection_delays.csv`, `countertrend_exposure.csv`, `sideways_false_trends.csv`
- `monthly_summary.csv`, `march_case_study.csv`, `root_cause_findings.csv`
- `state_timeline_5m.csv` (streamed replay)

