# Phase C2A — Topping / Bottoming sticky-state root-cause audit

Read-only diagnosis under **C1-C strict** research replay.

## Central question
Why does the SM often fail to leave `topping` → `early_bearish`/`neutral` and
`bottoming` → `early_bullish`/`neutral` after C1 weakening exits?

## Code answers (see code_audit.json)
- **No** `topping→neutral` or `bottoming→neutral` branch exists.
- Productive exits require **same-bar** BOS/CHoCH (LH/HL labels may be persisted).
- `neutral` is only reached from warning invalidation — practically rare after warmup.

## Headline stats
- Topping runs: {'n_runs': 45, 'median': 179.0, 'p75': 321.0, 'p90': 472.20000000000016, 'maximum': 765, 'ge24': 45, 'ge48': 40, 'ge96': 35, 'ge288': 12}
- Bottoming runs: {'n_runs': 41, 'median': 206.0, 'p75': 322.0, 'p90': 472.0, 'maximum': 925, 'ge24': 38, 'ge48': 35, 'ge96': 29, 'ge288': 13}
- Neutral bars: 0
- CF unlock bars (diagnostic): {'cf3': 15642, 'cf1': 5938, 'cf2': 3008}

## C2B proposal
Allow topping→early_bearish / bottoming→early_bullish when BOS/CHoCH evidence is persisted or multi-bar (mirror C1), still requiring LH/HL + impulse; separately evaluate optional age→neutral for chop without direction.

Default production `weakening_multi_bar_mode=off` unchanged. No transitions changed in C2A.
