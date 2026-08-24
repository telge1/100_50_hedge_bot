# EDC Profitability Analysis v2 (P2E1)

Beschreibende Profitabilitätsdiagnose für den P2D4-Lauf `edc_m0_51coin_30d_v2`.

## Zweck

- Warum waren manche Coins netto profitabel, andere nicht?
- Welche **kausal verfügbaren** Features unterscheiden Winner und Loser?
- Wie stark ist die Kostenwirkung unter festen Szenarien?
- Welche Effekte wirken stabil, gemischt oder coin-mix-getrieben?

**Keine** ML-/Optimierungsphase. Keine automatischen Filterregeln. Keine ClickHouse-Abfragen.

## Feature-Kausalität

Features werden in genau vier Gruppen eingeteilt:

| Gruppe | Verwendung |
|--------|------------|
| `PREDICTOR_CAUSAL` | Einzige Gruppe für Winner-/Loser-Diagnose |
| `IDENTITY_CONTEXT` | Join-/Identitätskontext und Strategiekonstanten |
| `OUTCOME_FUTURE` | Labels/Exits/PnL/MFE/MAE — verboten als Predictor |
| `UNRESOLVED_AVAILABILITY` | Kausalität unklar/unproven — ausgeschlossen |

Unklare Features werden **nicht geraten**, sondern ausgeschlossen.

Zusätzliche Zensus-Kennzahlen (Manifest `feature_census`, Report, CLI):

| Kennzahl | Bedeutung |
|----------|-----------|
| `predictor_causal_total` | Alle Features mit Gruppe PREDICTOR_CAUSAL |
| `predictor_causal_analyzable` | Davon `usable=yes` (nicht voll-missing, nicht konstant) |
| `predictor_causal_numeric_analyzable` | Analyzable und numerisch |
| `predictor_causal_categorical_analyzable` | Analyzable und kategorial |
| `predictor_causal_excluded_missing` | Causal, aber vollständig fehlend |
| `predictor_causal_excluded_constant` | Causal, aber konstant |

Die frühere CLI-Bezeichnung `allowed=` war irreführend und wurde entfernt.


## Inputs

| Input | Pfad (Beispiel) |
|-------|-----------------|
| Trades | `results/strategy_lab/edc_m0_51coin_30d_v2/trades.csv` |
| Coin-Summary | `results/strategy_lab/edc_m0_51coin_30d_v2/coin_summary.csv` |
| Enrichment | `results/edc_sync_tolerance/multicoin_reference_enrichment_v2_shared_engine/` (`enriched_trades.csv`) |

Join ausschließlich über `(symbol, source_event_id)` ↔ Enrichment-`candidate_id` (gleiche Legacy-ID). Zusätzlich exakter Vergleich von `symbol`, `side`/`feature__direction`, `decision_time`/`feature__decision_at`. Bei Paritätsfehler bricht die Analyse vor jeder Statistik ab.

## Öffentliche API

```python
from pathlib import Path
from orderbook_analyse.strategy_lab.analysis import analyze_edc_profitability_v2

result = analyze_edc_profitability_v2(
    trades_path=Path(".../trades.csv"),
    coin_summary_path=Path(".../coin_summary.csv"),
    enrichment_path=Path(".../multicoin_reference_enrichment_v2_shared_engine"),
    output_dir=Path(".../analysis_p2e1"),
)
```

## CLI-Aufruf

```bash
PYTHONPATH=src python scripts/run_strategy_lab_edc_profitability_analysis.py \
  --trades results/strategy_lab/edc_m0_51coin_30d_v2/trades.csv \
  --coin-summary results/strategy_lab/edc_m0_51coin_30d_v2/coin_summary.csv \
  --enrichment results/edc_sync_tolerance/multicoin_reference_enrichment_v2_shared_engine \
  --output-dir results/strategy_lab/edc_m0_51coin_30d_v2/analysis_p2e1
```

### Empfohlener nohup-Aufruf

```bash
mkdir -p logs

nohup env PYTHONPATH=src \
  python scripts/run_strategy_lab_edc_profitability_analysis.py \
  --trades results/strategy_lab/edc_m0_51coin_30d_v2/trades.csv \
  --coin-summary results/strategy_lab/edc_m0_51coin_30d_v2/coin_summary.csv \
  --enrichment results/edc_sync_tolerance/multicoin_reference_enrichment_v2_shared_engine \
  --output-dir results/strategy_lab/edc_m0_51coin_30d_v2/analysis_p2e1 \
  > logs/strategy_lab_edc_p2e1.log 2>&1 &
```

Logdatei: `logs/strategy_lab_edc_p2e1.log`

### CLI-Abschlussmeldungen

| Fall | Ausgabe | Returncode |
|------|---------|------------|
| Erfolg | `P2E1_EDC_PROFITABILITY_DIAGNOSIS_COMPLETE` | 0 |
| Parität | `P2E1_EDC_PROFITABILITY_ANALYSIS_BLOCKED_BY_PARITY` | ≠0 |
| Sonst | `P2E1_EDC_PROFITABILITY_ANALYSIS_BLOCKED` | ≠0 |

## Analysen

- Trade-Ebene: Winner (`net_pnl_usdt > 0`) / Loser (`< 0`) / Zero (`== 0`); Mediane/Quartile; Long/Short/gepoolt
- Coin-Ebene (51 Coins): Candidates, Trades, Win-Rate, Wilson-Intervall, Gross/Costs/Net, Stichprobengruppen
- Kosten (Decimal): `scenario_net = gross − trade_count × notional × cost_pct / 100` für 0 / 0.055 / 0.11 / 0.15 / 0.20 %
- Quartile: maximal Quartile, keine Threshold-Optimierung
- Stabilität: gepoolt, Long/Short, pro Coin, Leave-one-coin-out

### Stabilitätsklassen

| Klasse | Bedeutung |
|--------|-----------|
| `STABLE_DIRECTION` | Richtung hält über Coins und Leave-one-coin-out |
| `MIXED_DIRECTION` | Richtungen widersprechen sich zwischen Coins (explizit **instabil**) |
| `SINGLE_COIN_DRIVEN` | Effekt hängt an einzelnen Coins |
| `INSUFFICIENT_DATA` | Zu wenig Daten für eine Richtung |
| `POSSIBLE_COIN_MIX_CONFOUNDING` | Gepoolter Effekt nicht innerhalb der Coins reproduzierbar |

Within-coin-Richtungen zählen nur bei `trade_count >= 10` und mindestens 2 Winners sowie 2 Losers mit nicht-fehlendem Featurewert. Sonst `INSUFFICIENT_DATA`, nicht automatisch `MIXED_DIRECTION`.

Der Abschnitt **Nächste Hypothesen (unbestätigt)** enthält bis zu fünf deterministisch gerankte explorative Beobachtungen (auch bei `MIXED_DIRECTION`, dann als UNSTABLE markiert). Keine Filterregel, kein Threshold-Tuning.

## Outputs

Unter `results/strategy_lab/edc_m0_51coin_30d_v2/analysis_p2e1/`:

- `analysis_manifest.json`
- `feature_availability.csv`
- `trade_feature_comparison.csv`
- `feature_quantiles.csv`
- `coin_analysis.csv`
- `cost_scenarios.csv`
- `stability_analysis.csv`
- `findings.json`
- `report.md`

Deterministische Reihenfolge, feste CSV-Spalten, JSON mit `sort_keys=True`, finanzielle Werte als Decimal-Strings, atomisches Schreiben. Inputs werden nicht verändert.

## Resultate nicht committen

Analyse-Artefakte unter `results/` und Logs gehören **nicht** ins Repository.
