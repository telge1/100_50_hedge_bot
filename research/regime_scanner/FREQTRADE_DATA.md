# Freqtrade APTUSDT History Data

Stand: 2026-07-14 (Download- und Validierungslauf).

Dieses Dokument beschreibt, wie APTUSDT-OHLCV für Research mit Freqtrade erzeugt und wo die Dateien liegen. Keine API-Keys oder Config-Secrets.

---

## A. Freqtrade-Installation

```text
Projekt:
  /home/telgenbuescher/projects/freqtrade

Environment:
  /home/telgenbuescher/projects/freqtrade/.venv

Executable:
  /home/telgenbuescher/projects/freqtrade/.venv/bin/freqtrade

Version (2026-07-14):
  freqtrade 2025.4-dev-a1cecbae0
  Python 3.12.2
  CCXT 4.4.75
```

Aktivierung:

```bash
cd /home/telgenbuescher/projects/freqtrade
source .venv/bin/activate
freqtrade --version
```

Alternativ vorhanden (gleiche Freqtrade-Version): Conda-Environment `freq`.

---

## B. Config

```text
/home/telgenbuescher/projects/freqtrade/user_data/config.json
```

Nicht geheime Angaben:

```text
exchange: bybit
trading_mode: futures
margin_mode: isolated
pair: APT/USDT:USDT
data_format: feather   # CLI-Default / --data-format-ohlcv feather
```

API-Keys und Secrets gehören nicht in diese Dokumentation und wurden hier nicht kopiert.

---

## C. Kanonische Research-Datei (5m)

```text
/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather
```

| Feld | Wert |
|------|------|
| Rolle | **Kanonische Rohdatenquelle** für Regime-Scanner / Backtests |
| Format | Feather OHLCV (`date`, `open`, `high`, `low`, `close`, `volume`) |
| Zeitzone | UTC |
| Rows | 52569 |
| Start (candle open) | 2025-12-27 00:00:00+00:00 |
| Ende (candle open) | 2026-06-27 12:40:00+00:00 |
| Lücken (Δ ≠ 5m) | 0 |
| Dateigröße | 1254690 Bytes |
| SHA256 | `cc0ac7797ddc1562f2fc5097221996fcebb7b166b7b17cb72679cfc47f27e37a` |

Scanner-Laden: `research.backtests.candle_loader.DEFAULT_DATA_DIR` → dieser Ordner; Symbol `APTUSDT` → Dateiname `APT_USDT_USDT-5m-futures.feather`.

**Diese Datei darf durch Freqtrade-Downloads nicht überschrieben werden.** Kein `--erase` auf dem kanonischen Research-Datenordner.

---

## D. HTF-Staging (15m / 30m)

Staging-Root:

```text
/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging
```

Wichtig: Mit `--datadir <staging>` und `--trading-mode futures` speichert diese Freqtrade-Version die Feathers unter

```text
<data_apt_htf_staging>/futures/
```

**nicht** unter `.../bybit/futures/`. Die erwartete Zwischenstruktur mit `bybit/` entstand in diesem Lauf nicht.

### Tatsächliche Dateien (Download 2026-07-14)

| TF | Pfad |
|----|------|
| 15m | `/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/APT_USDT_USDT-15m-futures.feather` |
| 30m | `/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/APT_USDT_USDT-30m-futures.feather` |

| Feld | 15m | 30m |
|------|-----|-----|
| Rows | 17999 | 8999 |
| Start | 2025-12-27 00:00:00+00:00 | 2025-12-27 00:00:00+00:00 |
| Ende | 2026-07-02 11:30:00+00:00 | 2026-07-02 11:00:00+00:00 |
| Lücken | 0 | 0 |
| Größe | 457834 Bytes | 241098 Bytes |
| SHA256 | `f55e9a004e77c375aa87f40bc9eb8a69d7d060fa3aed91bb293339df09d3bfbd` | `d10844d1036934f79e3983cd1f5af0c5daf159bacead3247b0032f6b7d7eb387` |
| Validierung | OK (UTC, sortiert, keine Duplikate/Nulls/negativen Preise/Volumina) | OK |

Hinweis zum Timerange: Angefragt war `20251227-20260628`. Freqtrade lud lückenlos bis ca. **2026-07-02** (etwas über das angeforderte Ende). Für Vergleiche mit der kanonischen 5m-Datei auf den gemeinsamen Zeitraum beschneiden (5m endet 2026-06-27 12:40 UTC).

Nebenbei (Futures-Default): Freqtrade holte zusätzlich Mark-/Funding-Dateien in denselben Staging-Ordner (`APT_USDT_USDT-4h-mark.feather`, `APT_USDT_USDT-8h-funding_rate.feather`, `leverage_tiers_USDT.json`). Das war nicht das Ziel dieses Runs, aber übliches Verhalten im Futures-Trading-Mode.

### Download-Log

```text
/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/download_apt_15m_30m.log
```

Exit-Code des Downloads: **0**.

---

## E. Exakt ausgeführter Download-Befehl (2026-07-14)

```bash
cd /home/telgenbuescher/projects/freqtrade || exit 1
source .venv/bin/activate

set -o pipefail

freqtrade download-data \
  --config user_data/config.json \
  --datadir /home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging \
  --pairs 'APT/USDT:USDT' \
  --timeframes 15m 30m \
  --timerange 20251227-20260628 \
  --trading-mode futures \
  --data-format-ohlcv feather \
  2>&1 | tee \
  /home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/download_apt_15m_30m.log
```

```text
NIEMALS --erase auf dem kanonischen Research-Datenordner verwenden.
```

Auch nicht auf dem Staging-Ordner, wenn bestehende Dateien erhalten bleiben sollen. Ohne `--erase` ergänzt Freqtrade fehlende Daten und überschreibt die Historie nicht willkürlich.

---

## F. Sicherer zukünftiger Download (Staging)

```bash
cd /home/telgenbuescher/projects/freqtrade
source .venv/bin/activate

mkdir -p /home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging

# Vorher prüfen, was schon liegt:
find /home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging \
  -type f -iname '*APT*' -print | sort

freqtrade download-data \
  --config user_data/config.json \
  --datadir /home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging \
  --pairs 'APT/USDT:USDT' \
  --timeframes 15m 30m \
  --timerange 20251227-20260628 \
  --trading-mode futures \
  --data-format-ohlcv feather
```

Sicherheitsregeln:

1. Nur Staging-`--datadir`, nicht den kanonischen `.../Signal_Generator_Ralf/data/bybit/futures`-Pfad.
2. Kein `--erase`.
3. Bestehende 5m-Research-Datei nie als Download-Ziel verwenden.
4. HTF-Staging-Dateien nicht ohne separaten, bewussten Auftrag in den kanonischen Ordner kopieren.

Optional nur 5m in ein **anderes** Staging (niemals in-place auf der kanonischen Datei):

```bash
freqtrade download-data \
  --config user_data/config.json \
  --datadir /home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging \
  --pairs 'APT/USDT:USDT' \
  --timeframes 5m \
  --timerange 20251227-20260628 \
  --trading-mode futures \
  --data-format-ohlcv feather
```

---

## G. Datenstrategie

```text
Kanonische Research-Wahrheit:
5m Bybit-Futures-Feather

15m und 30m:
- direkte Freqtrade-Dateien dienen zunächst als Validierungsquelle
- kanonische HTF-Daten sollen später deterministisch aus 5m aggregiert werden
- erst nach OHLCV- und Bucket-Vergleich wird entschieden, welche HTF-Version in MySQL gespeichert wird
```

Aktueller Regime-Scanner (`research/regime_scanner/timeframes.py`) aggregiert 15m/30m bereits kausal aus geschlossenen 5m-Kerzen. Die Staging-Feathers sind der Exchange-Referenzpfad für den späteren Abgleich.

---

## H. Symbol-Mapping

```text
Scanner-Symbol: APTUSDT
Freqtrade-Pair: APT/USDT:USDT
Dateiname:      APT_USDT_USDT
```

Beispiele:

```text
APT_USDT_USDT-5m-futures.feather
APT_USDT_USDT-15m-futures.feather
APT_USDT_USDT-30m-futures.feather
```

---

## I. Zeitsemantik

```text
date = Candle-Open-Zeit
decision_time = Candle-Open-Zeit + Timeframe
nur vollständig geschlossene Candles verwenden
alle Zeiten UTC
```

Beispiel 15m: Candle `date=2026-03-01T12:00:00Z` deckt `[12:00, 12:15)` ab; bei Entscheidung zur Open-Zeit `12:15` ist diese Candle geschlossen.

---

## J. Pfad-Unterschied Staging vs. Kanonisch

| Rolle | Basisordner |
|-------|-------------|
| Kanonisch 5m | `.../Signal_Generator_Ralf/data/bybit/futures/` |
| HTF-Staging (dieser Download) | `.../Signal_Generator_Ralf/data_apt_htf_staging/futures/` |

Ursache: `--datadir` zeigt auf den Staging-Root; Freqtrade legt darunter `futures/` an und **keinen** weiteren `bybit/`-Ordner. Beim Einbinden späterer Loader den tatsächlichen Staging-Pfad verwenden.

---

## HTF Equality Audit

Vergleich direkter Freqtrade-15m/30m-Staging-Dateien mit der Scanner-Aggregation (`timeframes.aggregate_candles`) aus der kanonischen 5m-Datei.

| | |
|--|--|
| Modul | `research/regime_scanner/htf_freqtrade_equality_audit.py` |
| Ergebnisse | `research/regime_scanner/results_htf_freqtrade_equality_audit/` |
| Artefakte | `summary.json`, `README_results.md`, `mismatches_15m.csv`, `mismatches_30m.csv` |

### Gemeinsamer Vergleichszeitraum (Stand Audit 2026-07-14)

```text
15m: 2025-12-27T00:00:00+00:00 → 2026-06-27T12:30:00+00:00 (candle open)
30m: 2025-12-27T00:00:00+00:00 → 2026-06-27T12:00:00+00:00 (candle open)
```

Nur vollständige Buckets (15m: 3×5m, 30m: 6×5m). Direkte HTF-Candles nach dem 5m-Ende separat als `direct_only_after_5m_end`.

### Match-Raten (Shared Buckets)

```text
15m: shared=17523; exact OHLC=100%; volume exact≈72.6% / within_tol=100%; missing=0/0
30m: shared=8761;  exact OHLC=100%; volume exact≈64.3% / within_tol=100%; missing=0/0
erste Preisabweichung: keine
erste Volume-Abweichung (nur Float, abs≈1e-11…1e-9): 2025-12-27T00:30:00+00:00
direct_only_after_5m_end: 15m=476, 30m=238
deterministic_hash: b795131e7360a5b3a2e217e5d37a5d8d50cba0dd36c74354739bfc6f7b4f6d42
```

Volume-Differenzen liegen außerhalb von `==`, aber innerhalb abs/rel-Toleranz (Float-Summation); OHLC ist bit-exakt identisch.

### Entscheidung

```text
USE_5M_AGGREGATION
```

Begründung: Overlap-Buckets und OHLC stimmen mit der Scanner-5m-Aggregation überein; Volume nur float-nah; direkte Dateien haben zusätzliche Rand-Bars nach dem 5m-Ende. Für Research/MySQL bleibt die deterministische Aggregation aus der kanonischen 5m-Quelle die HTF-Wahrheit. Direkte HTF-Dateien bleiben **nur im Staging** und dienen als Validierungsreferenz (SHA256 als Metadaten speicherbar).
