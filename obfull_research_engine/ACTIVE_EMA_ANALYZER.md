# Active EMA Analyzer Identity

## ACTIVE EMA ANALYZER

`obfull_research_engine.ema_trend_live_analyzer_v1`

## RETIRED

`obfull_research_engine.ema_trend_analyzer_v1` — `RETIRED_INVALID_PROTOTYPE`

- Forensic backup: `/home/telgenbuescher/backups/ema_trend_analyzer_v1_forensic_20260918T123443Z`
- Invalid run: `obfull_research_engine/runs/ema_trend_analyzer_single_signal_v1_20260918`
  - `INVALID_TECHNICAL_FAILURE`
  - `NOT_STRATEGY_VALID`
  - `DO_NOT_RESUME`
  - `DO_NOT_USE_FOR_RESULTS`

## Rules

- Allowed new run prefix: `ema_trend_live_*`
- Forbidden new run prefix: `ema_trend_analyzer_*`
- No alias / wrapper / fallback / CLI / resume / deploy entry for retired package
- Valid CLI: `python -m obfull_research_engine.ema_trend_live_analyzer_v1`
- Status/stop commands must use the live package name only

## Integration SHAs (filled after green gates)

- Research: `7668a57` (updated if identity docs committed)
- Full-OB integration branch: `integration/ema-full-ob-fanout-live-v1-20260918`
- Public-Trade integration branch: `integration/ema-public-trade-fanout-live-v1-20260918`
- Contract: see `CONTRACT_MANIFEST.json` in this run folder
