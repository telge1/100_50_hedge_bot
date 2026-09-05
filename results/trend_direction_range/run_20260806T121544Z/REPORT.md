# Trend Direction Range Run

## Primärentscheidung

**TREND_DIRECTION_RANGE_RUNNER_READY**

- symbol: `APTUSDT`
- range: `2026-04-11T17:00:00Z` → `2026-04-12T04:00:00Z` (inclusive decision times)
- step: `5m`
- rows (output): 13
- direction_transitions: 8
- BULLISH/UNCLEAR/BEARISH: 30/11/92
- causality_failures: 0
- runtime_seconds: 18.8393
- direct BULL↔BEAR flips: 0

## Transition matrix

- BULLISH->UNCLEAR: 2
- UNCLEAR->BEARISH: 3
- BEARISH->UNCLEAR: 2
- UNCLEAR->BULLISH: 1
- BULLISH->BEARISH: 0
- BEARISH->BULLISH: 0

## Notes

- Decision window is inclusive: `start <= decision_time <= end`.
- Single C3.4B pass over candles through `end`; each T uses prefix `close_time <= T`.
- Forward returns not implemented in v1 (EX_POST_EVALUATION deferred).
- No MySQL writes; no HTF in default path.
