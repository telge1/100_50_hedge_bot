# BTC Sep-4 Trade / OI / Liquidation Availability Audit v1

**Gesamtverdict:** `BTC_FLOW_DATA_PARTIALLY_AVAILABLE`

## Kurzantwort auf „Trades/OI Sep-4 = nicht verfügbar“

| Datenart | Realität Sep-4 11:17–12:57 UTC | Ursache der Fehlmeldung |
| --- | --- | --- |
| Public Trades | **vorhanden** in `orderbook_analysis.public_trades_canonical` (**389723** Rows) | Analyse fragte falsche DB/Tabelle (`btc_doge_research.research_public_trades.event_time`) und mied `orderbook_analysis` pauschal wegen kaputtem `orderbook_deltas` |
| OI | **fehlt** in Live-Tabellen (0 Rows; letzter Write **2026-09-01T16:46:23Z**) | Collector-PID läuft, Writer/CH-Pfad seit ~01.09. ohne neue Rows |
| Liquidationen | **fehlt** (0 Rows; letzter Write **2026-09-01T16:36:59Z**) | dieselbe OI/Liq-Collector-Lücke |

`TIMEZONE_SHIFT_DETECTED=false` · `SYMBOL_MAPPING_OK=true` · Canonical-Schema ok; Prior-Analyse-Schema **nicht** ok.

## Phase A – Prozesse

| PID | Rolle | Status |
| --- | ---: | --- |
| 1692334 | Full-OB Collector | läuft (unverändert) |
| 147111 | OI+Liquidation Collector | Prozess läuft seit 18.08.; **keine Sep-4 CH-Rows** |
| 1661773 | Signal_Generator live + `--enable-public-trades` | läuft; speist `public_trades_canonical` |

OI-Log: historische `InsertError` / `SESSION_IS_LOCKED`; Sep-4 Reconnects ohne nachweisbare neuen Inserts.

## Phase B – Kanonische Quellen

Siehe `canonical_source_map.json`.

- Trades: `orderbook_analysis.public_trades_canonical.trade_ts` (UTC)
- OI: `orderbook_analysis.open_interest_events.event_time`
- Liq: `orderbook_analysis.all_liquidations.event_time`

`orderbook_deltas` bleibt defekt; **andere** Tabellen derselben DB sind direkt per FQN lesbar.

## Phase C – Coverage (Auszug)

Public Trades Analysefenster 11:17–12:57 UTC:

- Rows **389723**, uniq trade_ids ≈ gleichwertig
- Buy/Sell Notional: siehe `clickhouse_coverage.csv`
- Taker-Delta Quote: **≈ −11.7M USDT** (Sell-lastig im Gesamtfenster)
- Sekunden mit Daten **4928** / Span 6000; max Gap **7s**

OI/Liq denselben Fenster: **0** Rows.

## Phase F – Analyse-Codepfad (Prior)

`results/btc_full_ob_signal_to_crash_20260904_v1/_run_analysis.py`

→ `btc_doge_research.research_*` (Ende ≤ 2026-08-31)  
→ leeres Resultat  
→ `NOT_AVAILABLE` in `public_trade_buckets.csv` / `oi_liquidation_context.csv`  
→ `REPORT.md`

Siehe `analysis_query_diff.md`.

## Root Cause

```text
PUBLIC_TRADES_PRESENT_ANALYSIS_QUERY_BUG
OI_COLLECTOR_GAP
LIQUIDATIONS_COLLECTOR_GAP
```

Klassifikation je Quelle: `root_cause_by_source.json`.

## Offline-Fix (keine Live-Aktivierung)

Neu:

- `adapter/canonical_flow_reader.py` — korrekte FQNs, kein stilles „nicht verfügbar“
- `tests/test_sep4_canonical_flow_windows.py` — 6/6 PASS
- `recomputed_flow_facts/` — Trade-Fakten für Signale + Crash-Fenster

**Nicht** überschrieben: `results/btc_full_ob_signal_to_crash_20260904_v1/REPORT.md`

OI/Liq können nicht „gefixt“ werden ohne Collector/DB-Reparatur (außerhalb dieses Auftrags).

## Ergänzte Trade-Fakten (kanonisch)

Siehe `signal_flow_facts.csv` / `crash_flow_facts.csv`.

Beispiele (0–10m nach Signal):

- Parent UPPER: positiv/leicht negativer Preisverlauf; Taker-Delta gemischt je Horizont
- Nested UPPER: vor Crash noch positiver Delta im 10m-Fenster
- Crash 12:30–12:35: stark negatives Taker-Delta, Preisrückgang

`TRADE_DIRECTION=NOT_EVALUATED`

## Safety

- Collector 1692334 / OI 147111 unverändert
- DB-Writes dieses Audits: **0** (kein INSERT/DELETE/DROP/ALTER/TRUNCATE/OPTIMIZE)
- Restart: nein
- Kein Commit/Push
