# Beste APTUSDT Resultate inklusive Realfees

Datum: 2026-07-10

## Git-Stand

- Branch: research/recovery-gap-reduction-baseline
- Commit: 7250b652a27c3ee89add3f32c1372c0fe4ead0fb
- Gebühren werden über resolve_simulated_fee_rate aus der Live-Konfiguration berücksichtigt.
- Short-Profit-Basis-Fix de4b214 ist enthalten.

## Gemeinsame Einstellungen

- Symbol: APTUSDT
- Candles: 50000
- Multi-Start: aktiv
- Start-Step: 250 Candles
- Fenster: 15000 Candles
- Starts: 120 pro Richtung
- Fill-Modell: conservative
- Config-Quelle: live
- TP-Profit-Ziel: 0,25 % aus der Live-Konfiguration

## Long-Ergebnis

- Runs: 120
- Geschlossen: 118
- Unfinished: 2
- Total PnL: 23.939484291557154 USDT
- Live Short-TP-Relief: aktiviert

Long erneut starten:

    bash research/backtests/beste_resultate_aptusdt_inklusive_realfees/run_long.sh

## Short-Ergebnis

- Runs: 120
- Geschlossen: 120
- Unfinished: 0
- Total PnL: 33.9853 USDT
- Kein zusätzlicher Stuck-Recovery-Reload in diesem Referenzlauf

Short erneut starten:

    bash research/backtests/beste_resultate_aptusdt_inklusive_realfees/run_short.sh

## Kombiniertes Ergebnis

- Long: 23.939484291557154 USDT
- Short: ungefähr 33.9853 USDT
- Gesamt: ungefähr 57.9248 USDT

## Gespeicherte Artefakte

- long_results.json
- long_aggregate.csv
- short_results.json
- short_aggregate.csv
- long_fixed_cycle_config.json
- short_fixed_cycle_config.json
- run_long.sh
- run_short.sh
- git_commit.txt
- git_branch.txt
