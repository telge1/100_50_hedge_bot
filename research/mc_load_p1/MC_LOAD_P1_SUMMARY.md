# MC-LOAD-P1 Summary

**Status:** `MC_LOAD_P1_PARTIAL`

- **Instrumentation:** delivered (`mc_load_diag_v1.js`, hooks in `research_charts.js`, optional `_diag` on pane POST).
- **Browser E2E A–F:** `NOT_RUN` (no auth browser session).
- **Server benchmarks:** `MEASURED` (see `MC_LOAD_P1_E2E_RUNS.json`).

## Deliverables

| File | Path |
|------|------|
| Architecture | `research/mc_load_p1/MC_LOAD_P1_ARCHITECTURE.md` |
| Schema | `research/mc_load_p1/MC_LOAD_P1_TRACE_SCHEMA.md` |
| E2E + server runs | `research/mc_load_p1/MC_LOAD_P1_E2E_RUNS.json` |
| Timing CSV | `research/mc_load_p1/MC_LOAD_P1_TIMING_COMPARISON.csv` |
| Root cause | `research/mc_load_p1/MC_LOAD_P1_ROOT_CAUSE.md` |
| Diag module | `spread_recovery_hedge_short_dev/dashboard/static/js/research/mc_load_diag_v1.js` |

## Browser export (Sie)

1. Hard-refresh Research (`research-mc-load-p1-v1` script tag).
2. Konsole: `__MC_LOAD_DIAG__.start({ symbol: 'BNBUSDT', timeframe: '5m', view: 'single' });`
3. Symbol laden / wechseln wie im Screenshot-Setup.
4. `__MC_LOAD_DIAG__.stop(); __MC_LOAD_DIAG__.exportJSON();`

Optional: `localStorage.setItem('mc.load.diag','1')` für Dauer-Events ohne `start`.

## Regression

Keine Performance-Fixes. Nur Diagnose-Header `X-MC-Load-Diag` und Trace-Emits (inaktiv ohne `__MC_LOAD_DIAG__`).

**STOP**