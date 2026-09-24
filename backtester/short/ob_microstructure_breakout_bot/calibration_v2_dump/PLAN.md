# EMA Touch V2 Plan

Dieser Ordner ist die isolierte Arbeitsflaeche fuer die neue EMA59-Touch-Logik.

> Dump / Experiment only:
> Die echten Signale und die Baseline fuer Backtests kommen aus `calibration/`.
> `calibration_v2/` ist nur ein Testzweig und nicht die Source of Truth.

## Ziel

Wir bauen hier eine zweite Strategievariante auf, ohne die bestehende Calibration-Basis zu veraendern:

- strenger definierte EMA59-Touches
- Long und Short getrennt, aber klar spiegelbar
- EMA200 vorerst ignorieren, damit wir die EMA59-Structure sauber testen
- danach neue Kalibrierung und neuer Backtest

## Signal-Basis fuer den Backtest

Fuer den naechsten Backtest verwenden wir nur die bereits bestaetigten Signale
aus dem kalibrierten YAML-Run. Diese Signalmenge ist die Referenz-Input-Liste
und wird nicht aus dem V2-Scanner neu erzeugt.

### Longs

- `2026-09-07T08:45:00Z | tier2_strong`
- `2026-09-07T19:35:00Z | tier1_valid`
- `2026-09-08T20:25:00Z | tier2_strong`
- `2026-09-11T02:15:00Z | tier1_valid`
- `2026-09-13T14:25:00Z | tier2_strong`
- `2026-09-14T02:15:00Z | tier1_valid`
- `2026-09-15T17:50:00Z | tier2_strong`
- `2026-09-15T23:30:00Z | tier1_valid`
- `2026-09-16T18:15:00Z | tier2_strong`
- `2026-09-17T23:20:00Z | tier1_valid`

### Shorts

- `2026-09-07T20:25:00Z | tier1_valid`
- `2026-09-08T17:45:00Z | tier1_valid`
- `2026-09-11T17:30:00Z | tier1_valid`

## Backtest-Plan

1. Fuer jedes Signal den Entry-Zeitpunkt und die Richtung festlegen.
2. Den Stop-Loss nach der aktuellen V2-Regel ableiten.
3. Den Take-Profit aus der Liquidity-Pool-Regel ableiten.
4. Mit 5m-Bars forward-replayen, bis SL, TP oder ein definierter Exit greift.
5. Pro Signal die P/L in Prozent und das Exit-Ereignis speichern.
6. Danach die Gesamtstatistik ueber alle 13 Signale bilden.

## Messgroessen

- Trefferquote
- durchschnittlicher P/L pro Trade
- Summe aller P/L-Werte
- Winrate
- groesster Verlust und groesster Gewinn
- Vergleich Long vs Short
- Vergleich mit und ohne Ignore-Filter

## Ordnerstruktur

```text
calibration_v2/
  PLAN.md
  configs/
  events/
  reports/
  tests/
```

## Inhalt pro Bereich

- `configs/`: neue YAMLs oder Zwischenstufen fuer die V2-Strategie
- `events/`: gelabelte Touch-, Breakout- und Fakeout-Ergebnisse
- `reports/`: Vergleiche gegen Baseline, Walk-forward, Signalstatistiken
- `tests/`: zielgerichtete Tests fuer Touch-Definition und Kontextfilter

## Arbeitsregel

Alles Neue zur EMA-Touch-V2 kommt nur hier hinein.
Die bestehende `calibration/` bleibt die Baseline fuer den eingefrorenen Stand.
