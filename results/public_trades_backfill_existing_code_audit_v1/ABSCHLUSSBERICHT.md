# ABSCHLUSSBERICHT — public_trades_backfill_existing_code_audit_v1

## 1. Verdict

**PUBLIC_TRADES_BACKFILL_PIPELINE_PARTIALLY_READY**

Begründung: Produktionsreifer Download+Backfill+Idempotenz-Pfad existiert (Signal_Generator → `public_trades_canonical`), für BTC/DOGE 6–12 Monate nutzbar. Volles 51-Coin-Fenster 6–12 Monate ist durch Speicher-Gate (~430 GiB) und fehlende Listing-Kalender-Orchestrierung nur gestuft möglich. OA-Archiv-Ingest schreibt absichtlich **nicht** in die Analyse-Tabelle.

## 2. Bestehender Downloader

**ja** (zwei Implementierungen; kanonisch SG)

## 3. Bestehender Backfill

**ja** (`run_public_trades_7d_backfill.py`, `run_public_trades_30d_pipeline.py`)

## 4. Dateien / Einstiegspunkte

- `/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/scripts/run_public_trades_7d_backfill.py`
- `.../scripts/run_public_trades_30d_pipeline.py`
- Package `signal_generator.bybit.public_trades.*` + `signal_generator.db.public_trades`
- Live: `run_live_collector_service.py --enable-public-trades` (PID **1661773**)
- OA Parallel (nur Download / Archive-Tabelle):  
  `orderbook_analyse/scripts/download_bybit_public_trades.py`,  
  `orderbook_analyse/scripts/ingest_public_trades_archive.py`

## 5. Datenquelle

**SOURCE_CONFIRMED:** `https://public.bybit.com/trading/{SYMBOL}/{SYMBOL}{YYYY-MM-DD}.csv.gz`  
(USDT-Perp Public Trading Day Files; keine Cookies).  
Live zusätzlich Bybit WS `publicTrade.{symbol}`.

## 6. Maximale historische Reichweite

- BTCUSDT Archive: HEAD **2021-01-01 = 200**, **2020-01-01 = 404** → mindestens ~5 Jahre für BTC.  
- Neuere Listings: 404 → `ARCHIVE_UNAVAILABLE` / `LISTING_LIMITED`.  
- In CH heute nur ab **2026-07-19** (48 Tage) für alle 51 Coins.

## 7. BTC-Coverage

- min/max: **2026-07-19 → 2026-09-04 (live)**  
- ~77M physical rows; 48 distinct days; last-365 missing ≈ **318 ganze Tage** (alles vor 2026-07-19)  
- 7d/30d Fenster: kalendervoll (0 missing days)  
- Sep-4 11:17–12:57 UTC: **389 723** rows = **389 723** uniq trade_id — **verifiziert**

## 8. DOGE-Coverage

- gleiche Spanne **2026-07-19 → 2026-09-04**  
- ~14.2M rows; 48 Tage; analoge 318-Tage-Lücke vor Jul-19  
- 30d kalendervoll

## 9. 51-Coin-Coverage

- Alle **51** Symbole haben Daten **2026-07-19 … 2026-09-04** (Status weitgehend COMPLETE in dieser Spanne)  
- Universe-Datei: `orderbook_analyse/config/universe_tradeable_51.json`  
- Live Public-Trades: **51** Symbole enabled

## 10. ClickHouse-Zieltabelle (Analyse)

**`orderbook_analysis.public_trades_canonical`**

Nicht: `public_trades` (TTL), `public_trades_archive` (OA), `btc_doge_research.research_public_trades` (endet 2026-08-31, Spalte `event_time`).

## 11. Idempotenz

**ja** — `ReplacingMergeTree(ingest_timestamp)` ORDER BY `(symbol, trade_id)`; Backfill skippt existierende IDs / AUDITED; physische Duplikate möglich, logisch stabil. Exakte Counts: FINAL oder uniqExact.

## 12. Resume

**ja** — Manifest-Statusmaschine + Checkpoints; Skip AUDITED / ARCHIVE_UNAVAILABLE.

## 13. Erkannte Lücken

- Primär: **keine CH-Daten vor 2026-07-19** für Universe (318/365 Kalendertage leer)  
- Innerhalb 2026-07-19→heute: keine fehlenden ganzen Tage (max consecutive missing between present = 0)  
- Stale research mirror endet 2026-08-31  
- FR `public_trades_raw` Placeholder leer (kein Ersatz)  
- Intraday-Gap-Metrik aus Window-Funktion unzuverlässig (Artefakt); nicht als Collector-Loch werten

## 14. Eignung 6 Monate

- **BTC+DOGE: ja** mit bestehendem SG-CLI  
- **51 Coins: nur gestuft** / Gate-Anpassung nötig (Speicherprojektion)

## 15. Eignung 12 Monate

- **BTC+DOGE: ja** (Archive vorhanden)  
- **51 Coins: nicht one-shot** unter aktuellem 430 GiB `MAX_SAFE_USE_BYTES`

## 16. Notwendige Codeänderungen (noch nicht implementieren)

1. Optional: Listing-Datum-/404-Kalender pro Symbol dokumentieren  
2. Für 51×6–12m: Storage-Gate/Staging (Monats-Slices)  
3. Analyse-Queries: FINAL/uniqExact-Konvention festziehen  
4. Klare Docs: OA-Archive ≠ Canonical  
5. Kein Merge von OA-Archive-Ingest in Canonical ohne neuen, geprüften Pfad

## 17. Empfohlener sicherer Pilot

BTCUSDT+DOGEUSDT, `--start-date 2026-01-20 --end-date-exclusive 2026-07-19`, workers=1, neues run-dir, Gate-A Smoke-Tag zuerst. Collector unverändert lassen.

## 18. Vorgeschlagener CLI (nicht ausgeführt)

Siehe `proposed_commands_not_executed.sh`.

## 19. PIDs unverändert

- Full-OB Collector **1692334** OK  
- OI/Liq **147111** OK  
- Signal_Generator live+public trades **1661773** OK (LIVE, 51 PT symbols)

## 20. DESTRUCTIVE_ACTIONS_EXECUTED

**false**

Keine Downloads, keine Imports, keine Schema-/Prozessänderungen, kein Commit/Push.
