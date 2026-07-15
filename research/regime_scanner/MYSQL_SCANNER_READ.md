# MySQL as optional Regime-Scanner candle source

Stand: 2026-07-14

## Zweck

Der Regime Scanner kann 5m-Candles wahlweise aus Feather oder aus der Research-MySQL-DB laden.

```text
data_source=feather   # Default — bestehender Pfad
data_source=mysql     # read-only Research-DB
```

MySQL ist **nur** eine alternative Datenquelle. Scannerlogik, Aggregation, Structure, Policy, Price Action und Momentum bleiben unverändert.

## Architektur

```text
FeatherCandleSource ─┐
                     ├─> kanonisches 5m-DataFrame
MySQLCandleSource  ──┘
                           ↓
bestehender Scanner (aggregate_candles für 15m/30m)
```

Module:

* `research/regime_scanner/candle_sources.py` — Source-Abstraktion
* `research/regime_scanner/data_loader.py` — `load_symbol_candles(..., data_source=...)`
* `research/regime_scanner/mysql_feather_parity_audit.py` — Paritätsaudit

## Kanonisches DataFrame (Scanner)

```text
timestamp   datetime64[ns, UTC]   # Candle-Open
open/high/low/close/volume  float64
```

Optional in der Source-API zusätzlich:

```text
close_time = timestamp + timeframe
```

Hinweis: Feather-Dateien nutzen Spalte `date`; die Source normalisiert nach `timestamp`, weil der Scanner intern `timestamp` erwartet. Fachlich ist das dieselbe Open-Time.

## HTF-Semantik (unverändert)

Der Scanner **aggregiert** 15m/30m weiterhin aus 5m via `timeframes.aggregate_candles`.

Direct-15m/30m in MySQL sind für Candle-Level-Parität und spätere Nutzung vorhanden, werden aber **nicht** stillschweigend in den Scannerpfad eingespeist.

## Decision-Time

Zwei Ebenen:

1. **CandleSource API** (`close_time <= decision_time`) — closed-candle Semantik für Source-Vergleiche.
2. **Scanner-Pfad** (`timestamp < decision_time` in `load_closed_candles_as_of`) — bestehende candle-open Semantik, identisch für Feather und MySQL.

Für 5m auf TF-Grenzen (Decision = Close einer Candle) liefern beide dieselbe letzte Candle.

## CLI

```bash
PYTHONPATH=. python3 -m research.regime_scanner.point_audit \
  --decision-time 2026-03-05T17:30:00+00:00 \
  --timeframes 5m,15m,30m \
  --data-source feather

PYTHONPATH=. python3 -m research.regime_scanner.point_audit \
  --decision-time 2026-03-05T17:30:00+00:00 \
  --timeframes 5m,15m,30m \
  --data-source mysql
```

Auch: `pipeline_audit`, `signal_tp_audit`, `batch_audit` mit `--data-source`.

Default bleibt `feather`. Unbekannte Quelle → Fehler. Kein stiller Fallback MySQL→Feather.

## Environment

Nur `REGIME_DB_*` (lokal, gitignored):

```text
research/regime_scanner/.env.regime_db
```

```text
REGIME_DB_HOST
REGIME_DB_PORT
REGIME_DB_NAME=regime_scanner_research
REGIME_DB_USER=regime_scanner
REGIME_DB_PASSWORD
```

Keine `MYSQL_*` / Live-Credentials. Passwort niemals loggen.

## Read-only Garantie

MySQL-Source:

* kein Schema-Init
* kein Import
* keine Aggregation speichern
* keine Validation-Runs schreiben
* Verbindung schließen nach Load

## Paritätsaudit

```bash
set -a && source research/regime_scanner/.env.regime_db && set +a
PYTHONPATH=. python3 -m research.regime_scanner.mysql_feather_parity_audit \
  --exchange bybit \
  --symbol APTUSDT \
  --timeframes 5m,15m,30m
```

Artefakte unter:

```text
research/regime_scanner/results_mysql_feather_parity/
```

Vergleicht:

* Candle-Level 5m/15m/30m direct (Counts, Ranges, OHLC exakt, Volume, Hashes)
* Decision-Time-Fenster (`close_time <= decision_time`)
* Warm-up bis Analysefenster
* Point-Audits (Sample) inkl. Regime-Snapshot / Multi-TF
* Trend-State-Timeline + Structure-Events für die März-Woche (vollständig)
* Langfenster + volles 5m-Fenster: Input-Parität + deterministische Point-Audit-Samples
* State-Isolation (Feather↔MySQL Reihenfolge)
* DB-Write-Guard (Counts unverändert)

Nachgewiesen (2026-07-14): `parity_summary.json` → `ok=true`, 0 Differences.
Volume war bit-exakt (nicht nur tolerant). PA/Momentum laufen auf denselben 5m-Inputs
und denselben Point-Audit-/Regime-Zuständen; ein vollständiger Pipeline-Wochenlauf
ist optional und zeitintensiv, ändert bei identischem Input keine fachliche Erwartung.

## Performance (März-Fenster, nachgewiesen)

```text
Feather 5m load: ~0.19 s
MySQL 5m load:   ~0.41 s
Point-Audit Sample (≈25×2): ~128 s
Trend-Timeline Feather: ~201 s
Trend-Timeline MySQL:   ~202 s
```

Noch keine Query-/Index-Optimierung in diesem Schritt.

## Hashes

* Candles: `candles_export_hash` — sortiert nach timestamp, Felder `timestamp,open,high,low,close,volume`, Float `%.17g`, Timestamp ISO UTC
* Scanner-Outputs: `json_hash` über stabil bereinigte Dicts (`source_feather` / Runtime-Felder entfernt)

## Noch nicht umgesetzt

* Scanner-Ergebnisse in MySQL speichern
* Optimierer / Parameter-Sweeps
* Live-Scanner-Migration als Default
* Direct-HTF als Scanner-Input (bewusst zurückgestellt)

## Risiken

* Decision-Time-Semantik Source (`close_time`) vs Scanner (`open < decision`) unterscheiden sich abseits der TF-Grenzen — dokumentiert und in Tests abgedeckt.
* Float-Volume kann bitgleich oder nur innerhalb Toleranz sein; Preise müssen exakt sein.
