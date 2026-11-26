# P4A Changelog (no commit)

## `research_charts/service.py`

- `_tail_limit_candles`
- `_packed_from_cached`: trim on cache hit
- `_load_candles_compute`: trim before `_cache_put`

## `static/js/research/research_charts.js`

- Asset `mc-load-p4a-v1`
- `abortStalePaneWork`, TF-change hook
- `ensurePaneCaughtUp` / `mergePollCandlesIntoPane` / `schedulePaneHistoryPhaseB` / EMA guards

## `templates/research_charts.html`, `mc_load_diag_v1.js`

- Query `p4a-v1`

## Tests

- `research_charts/tests/test_candle_cache_limit_hit.py`
- `test_mc_load_p3_fast_first_v1.py` (symbols + cache bust)