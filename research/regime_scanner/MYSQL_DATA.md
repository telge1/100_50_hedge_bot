# Regime Scanner MySQL Candle Store

Stand: 2026-07-14 (historischer Bootstrap: Direct Feather für 5m/15m/30m)

Isolierter Research-Datenlayer. **Noch keine Scanner-/State-Machine-Umstellung.**

## Datenstrategie

```text
Historischer Bootstrap:
5m, 15m und 30m direkt aus vorhandenen Feather-Dateien.

Fortlaufende Daten:
5m ist kanonisch; neue 15m/30m können aus 5m aggregiert werden
(mode=fill-missing; überschreibt keine Direct-Historie).

Validierung:
Direct-HTF wird im gemeinsamen Fenster gegen temporäre 5m-Aggregation geprüft.

Historische Direct-HTF-Daten nach dem 5m-Ende:
bleiben erhalten und sind operativ nutzbar.
```

Das Equality-Audit (`USE_5M_AGGREGATION`) bedeutet:

- OHLC-Identität Direct ↔ 5m-Aggregation im Overlap
- **nicht**, dass historische Direct-15m/30m ignoriert werden

## Source-Semantik

| Herkunft | `source` | `source_timeframe` |
|----------|----------|--------------------|
| Feather-Import 5m/15m/30m | `freqtrade_direct` | `5m` / `15m` / `30m` |
| später aus 5m befüllt | `aggregated_from_5m` | `5m` |

## Priorität / Konflikte

Fachlicher Unique-Key: `(exchange, symbol, timeframe, open_time)`.

| Fall | Verhalten |
|------|-----------|
| Bucket fehlt | Insert |
| Gleiche Source | Idempotenter Upsert |
| Existiert `freqtrade_direct`, kommt `aggregated_from_5m` | Direct **nicht** überschreiben; bei OHLC-Gleichheit skip; bei Abweichung **Konflikt** |
| Existiert `aggregated_from_5m`, kommt `freqtrade_direct` | Bei OHLC-Gleichheit kontrolliert auf Direct promoten; sonst **Konflikt** |

Kein stilles Last-Write-Wins.

## Aggregation

```bash
PYTHONPATH=. python3 -m research.regime_scanner.mysql_candle_store aggregate \
  --mode fill-missing   # Default: nur fehlende Buckets
  # --mode validate-only  # nur berechnen/prüfen, nicht schreiben
```

Keine destruktive `replace-all`-Option.

## Feather-Quellen (APT)

| TF | Pfad | SHA256 | Rows | Zeitraum (UTC open) |
|----|------|--------|------|---------------------|
| 5m | `/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather` | `cc0ac7797ddc1562f2fc5097221996fcebb7b166b7b17cb72679cfc47f27e37a` | 52569 | 2025-12-27 00:00 → 2026-06-27 12:40 |
| 15m | `/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/APT_USDT_USDT-15m-futures.feather` | `f55e9a004e77c375aa87f40bc9eb8a69d7d060fa3aed91bb293339df09d3bfbd` | 17999 | 2025-12-27 00:00 → 2026-07-02 11:30 |
| 30m | `/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/APT_USDT_USDT-30m-futures.feather` | `d10844d1036934f79e3983cd1f5af0c5daf159bacead3247b0032f6b7d7eb387` | 8999 | 2025-12-27 00:00 → 2026-07-02 11:00 |

Spalten überall: `date` (UTC), `open/high/low/close/volume` (`float64`).

Equality-Referenz (Overlap): shared 15m≈17523, 30m≈8761; OHLC 100%; Volume within tol 100%; Direct-only after 5m: 15m=476, 30m=238.

## Tabellen

Unverändert: `market_candles`, `data_validation_runs`.

Datentypen: `DATETIME(6)` UTC, OHLCV `DOUBLE`, Unique wie oben.

## Environment

```text
REGIME_DB_HOST
REGIME_DB_PORT
REGIME_DB_NAME
REGIME_DB_USER
REGIME_DB_PASSWORD
```

Vorlage: `research/regime_scanner/env.regime_db.example`. Optional: `pip install PyMySQL`.

## CLI / Importreihenfolge (später mit REGIME_DB_*)

```bash
PYTHONPATH=. python3 -m research.regime_scanner.mysql_candle_store print-schema
PYTHONPATH=. python3 -m research.regime_scanner.mysql_candle_store init-schema

# Dry-Runs
PYTHONPATH=. python3 -m research.regime_scanner.mysql_candle_store import-feather \
  --input /home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather \
  --exchange bybit --symbol APTUSDT --timeframe 5m --dry-run

PYTHONPATH=. python3 -m research.regime_scanner.mysql_candle_store import-feather \
  --input /home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/APT_USDT_USDT-15m-futures.feather \
  --exchange bybit --symbol APTUSDT --timeframe 15m --dry-run

PYTHONPATH=. python3 -m research.regime_scanner.mysql_candle_store import-feather \
  --input /home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/APT_USDT_USDT-30m-futures.feather \
  --exchange bybit --symbol APTUSDT --timeframe 30m --dry-run

# Echte Imports (ohne --dry-run), dann:
PYTHONPATH=. python3 -m research.regime_scanner.mysql_candle_store audit \
  --exchange bybit --symbol APTUSDT \
  --compare-direct-htf-with-5m \
  --record-direct-htf-refs
```

`import-5m` bleibt als Wrapper auf denselben Importpfad.

## Decision-Time

`load_candles(..., decision_time=T)` → nur `close_time <= T`. Keine implizite Kappung auf den 5m-Zeitraum.

## Noch nicht umgesetzt

- Scanner-Migration auf MySQL
- Live-Streaming
- Alembic-Migrationen
- Echter DB-Import in diesem Schritt (nur Code + Dry-Run/Unit-Tests)
