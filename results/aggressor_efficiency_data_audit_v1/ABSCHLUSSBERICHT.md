# ABSCHLUSSBERICHT — AGGRESSOR_EFFICIENCY_FLIP_V1 Data Audit

**Audit-ID:** aggressor_efficiency_data_audit_v1  
**Datum (UTC):** 2026-08-29 16:38:31Z  
**Referenz:** DOGEUSDT, 2026-08-29 08:00–15:30 UTC  
**Scope:** Read-only ClickHouse-Datenaudit — keine Strategie, kein Detector, kein Backtester.

---

## 1. Verdict

**B. AGGRESSOR_EFFICIENCY_DATA_READY_WITH_LIMITATIONS**

Begründung in einem Satz: `public_trades_canonical` liefert millisekundenfeine Aggressor-Trades mit sauberer Side-/Notional-Semantik und erlaubt kausale 1s/5s/30s/60s-Impact-Messung über Trade-Preise; für das Referenzfenster fehlen jedoch unabhängige Mid/Micro-Serien (`orderbook_features_1s_v2` / `ticker_samples`), und innerhalb identischer Millisekunden ist die Exchange-Sequenz nicht vollständig rekonstruierbar.

---

## 2. Executive Summary

- Primärquelle **`orderbook_analysis.public_trades_canonical`** ist **trade-level mit DateTime64(3)** (Millisekunden).
- Im Referenzfenster: **41 523 Rows**, **41 523 distinct `trade_id`**, **0 Duplikate**, **FINAL ≡ non-FINAL**, Quelle **`live`**.
- Side = Bybit-Taker (**Buy/Sell**); keine ungültigen Sides; Preis/Size/Notional sauber.
- Mehrere Trades pro Sekunde und sogar **bis 490 Trades auf demselben Millisekunden-Timestamp** → Burst-Kompression, aber **keine 1s-Aggregationstabelle**.
- Kausale 1s-Buckets aus Trades sind **lokal erzeugbar**; Smoke zeigt deskriptiv getrennte Phasen (hohes Sell-Notional mit geringer Down-Exkursion vs. später hohes Buy-Notional mit stärkerer Up-Exkursion) — **ohne** Profit-/Regel-Claim.
- **Preisquelle für Aug-29-Fenster:** Trade-Last/First (und optional 1m-Candles). **Kein** Mid/Micro/BBO im Fenster.
- **OI 5s** ist im Fenster vollständig und als späterer Klassifikator geeignet.
- `orderbook_deltas` ist **broken/unattachable** — nicht genutzt, nicht repariert.

---

## 3. Live-Sicherheitsbestätigung

Während des Audits wurden **keine** Collector-, Dashboard-, API-, Job-, Lock-, Manifest- oder Live-Prozesse gestartet, gestoppt oder neu gestartet.

Beobachteter Ist-Zustand (nur `pgrep`, keine Eingriffe):

- `clickhouse-server` lief bereits
- Live-Candle/Public-Trades-Collector lief bereits
- OI/Liquidation-Collector lief bereits
- Dashboard `app.py` lief bereits

ClickHouse: ausschließlich **SELECT / DESCRIBE / SHOW CREATE**.  
Keine Writes, DDLs, Inserts, Mutations, Optimizes.  
Keine Git-Commits.  
Keine bestehenden Projektdateien geändert.  
Neue Artefakte nur unter `results/aggressor_efficiency_data_audit_v1/`.

Hinweis: `getSetting('readonly')` war `0` (Server nicht im Readonly-Modus) — der Audit hat trotzdem nur gelesen.

---

## 4. Relevante Tabellen und Schemas

Siehe `source_inventory.csv`. Kurz:

| Tabelle | Rolle | Ref-Fenster DOGE |
|---|---|---|
| `orderbook_analysis.public_trades_canonical` | Primäre Aggressor-Trades | 41 523 rows |
| `orderbook_analysis.public_trades` | Live-Recorder TTL30d | 0 im Fenster |
| `orderbook_analysis.public_trades_archive` | Archiv | 0 im Fenster |
| `orderbook_analysis.orderbook_features_1s_v2` | 1s Mid/Micro/BBO/Depth | **0** (Serie endet 2026-08-28 16:26) |
| `orderbook_analysis.ticker_samples` | BBO/Last/Mark/OI samples | **0** (DOGE endet 2026-08-11) |
| `signal_generator.candles_1m` | 1m OHLC | 450 bars (vollständig) |
| `orderbook_analysis.open_interest_5s` | OI 5s | 5 400 buckets (vollständig) |
| `orderbook_analysis.open_interest_events` | OI Events | 5 336 |
| `orderbook_analysis.open_interest_5m_history` | OI 5m | 0 im Fenster |
| `orderbook_analysis.liquidations` | Legacy Liq | 0 |
| `orderbook_analysis.all_liquidations` | Liq Collector | 16 events |
| `orderbook_analysis.orderbook_deltas` | Raw L2 | **UNATTACHABLE / nicht abgefragt** |

Es gibt **keine** persistente `public_trades_1s`-Aggregationstabelle. 1s-Felder müssen aus Trades abgeleitet werden.

---

## 5. Event-Time- / Ingestion-Time-Semantik

**public_trades_canonical**

- **Event time:** `trade_ts` `DateTime64(3, 'UTC')` — Exchange-Tradezeit, ms.
- **Ingestion time:** `ingest_timestamp` `DateTime64(6, 'UTC')`.
- Lag im Fenster: p50 ≈ 1.4 s, p90 ≈ 5.2 s, p99 ≈ 9.2 s, max ≈ 16 s.
- Sortierung nach `ingest_timestamp` ist **nicht** monoton in `trade_ts` (~8.3k Rücksprünge) → **Event-Ordnung = `trade_ts` (+ `trade_id` als Tie-Break)**, niemals Ingest-Reihenfolge.

**orderbook_features_1s_v2** (außerhalb Ref-Fensters verfügbar)

- `bucket_start` = 1s-Bucket-Identität.
- `first_source_ts` / `last_source_ts` = Book-Event-Spanne in der Sekunde.
- Semantik: Feature der Sekunde aus Updates **innerhalb** `[bucket_start, bucket_start+1s)` — für kausalen Impact eines Trades bei `t` darf Mid erst ab **geschlossener** Sekunde bzw. `last_source_ts ≤ t` verwendet werden (kein Lookahead in die Zukunft der Sekunde).
- Im Ref-Fenster **nicht anwendbar** (keine Rows).

**candles_1m**

- `open_time` / `close_time`, `is_closed`.
- Nur geschlossene Kerzen für kausale Features; 1m zu grob für Phasen-Trennung allein.

**open_interest_5s**

- `bucket_time` + `source_event_time`; `state_age_ms` p50≈280, p99≈2180 im Fenster.
- Als Klassifikator auf ≥5s / besser ≥5m-Blöcken geeignet.

---

## 6. Trade-Level- oder Aggregationsnachweis

### Verdict Granularität: **MILLISECOND_TRADE_LEVEL**

Belege (DOGE Ref-Fenster):

| Check | Ergebnis |
|---|---|
| Timestamp-Typ | `DateTime64(3)` |
| Unique ms stamps | 8 405 |
| Davon nicht volle Sekunde | 8 395 |
| Unique Sekunden mit Trades | 5 010 |
| Rows | 41 523 > unique seconds |
| Max Trades / gleiche ms | **490** |
| Groups mit >1 Trade gleicher ms | 4 945 |
| Max unterschiedliche Preise / gleiche ms | 15 |
| Busy-Second-Stichprobe | mehrere distinct `trade_id`, gleiche ms, unterschiedliche sizes |

**Nicht** `ONE_SECOND_AGGREGATED`: qty/notional sind pro Trade; Aggregation wäre Verlust.

**Einschränkung:** Bei identischem `trade_ts` (ms) fehlt Exchange-Sequence → Intra-ms-Reihenfolge nur näherungsweise (`trade_id`-Sort), nicht beweisbar exchange-true.

Schema-Dokumentation und Migrationstext beschreiben Side als **Bybit Taker/Aggressor**. Empirisch korreliert `Buy` stark mit `PlusTick`/`ZeroPlusTick` und `Sell` mit `MinusTick`/`ZeroMinusTick`, aber es gibt Cross-Fälle (Tick-Direction ≠ Side) — Side bleibt die maßgebliche Aggressor-Variable; Tick-Direction ist Zusatz, kein Widerspruch zur Taker-Semantik.

---

## 7. Deduplizierung und Datenqualität

Engine: **`ReplacingMergeTree(ingest_timestamp)`**, `ORDER BY (symbol, trade_id)`, Partition `toYYYYMM(trade_ts)`.

| Prüfung | Wert |
|---|---|
| Rows vs distinct trade_id | 41 523 = 41 523 |
| FINAL rows | 41 523 |
| Leere trade_id | 0 |
| Bad sides / nulls / nonpositive | 0 |
| Buy/Sell count | 22 516 / 19 007 |
| Buy/Sell notional USDT | ≈ 7.96M / 7.25M |
| Notional p50/p90/p99/max | ≈ 8.9 / 844 / 5.7k / 111k USDT |
| Trades/sec p50/p90/p99/max | 3 / 17 / 81 / **966** |

**FINAL:** Im Fenster unnötig (bereits unique). FINAL auf große unpartitionierte Scans wäre teuer — bei Research immer `symbol` + Zeitpartition filtern; dedupe defensiv via `GROUP BY trade_id` / `argMax(..., ingest_timestamp)` statt blindem FINAL über Monate.

Details: `data_quality_summary.csv`.

---

## 8. DOGEUSDT-Coverage

- Global canonical DOGE: ≈ **2026-07-19 → 2026-08-29**, ~12.5M trades.
- Ref-Fenster **vollständig** mit Live-Trades abgedeckt (min 08:00:00.256 → max 15:29:55.118).
- 26 996 Sekunden Spanne, davon 5 010 mit ≥1 Trade; 21 986 Sekunden ohne Trade = **Markt-Ruhe**, kein Tape-Ausfall der Quelle.
- **Nicht** coverage-blocked.

---

## 9. Unterstützte Impact-Horizonte

Siehe `impact_horizon_support.csv`.

| Horizont | Support |
|---|---|
| 1s, 2s, 5s, 10s, 30s, 60s | **vollständig messbar** (Trade-Preispfad) |
| 180s, 5m | **näherungsweise** (Phasen-Mix-Risiko steigt) |
| 15m allein | **nicht zuverlässig** für Flip-Isolation |

**Wichtig:** Interne kausale 1s/5s-Fenster sind aus Trades erzeugbar, auch wenn der spätere Strategiekontext 5m/15m ist. 15m darf nur Struktur-Bestätigung sein, nicht die Impact-Einheit.

---

## 10. Geeignetste Preisquelle

| Quelle | Ref-Fenster | Eignung 1s/5s/30s Impact |
|---|---|---|
| **A. Last/First Public-Trade-Preis** | ja | **Primär empfohlen** |
| B. Mid aus BBO (`ticker_samples`) | **nein (0 rows)** | nicht verfügbar |
| C. Microprice (`orderbook_features_1s_v2`) | **nein (0 rows)** | historisch ja bis 28.08., nicht am 29.08. Vormittag/Nachmittag |
| D. 1s-Candle | existiert nicht persistent | aus Trades ableitbar |
| E. 1m-Candle | ja (450) | zu grob für Flip-Phasen; ok als Confirm |

**Clock-Skew Trades vs Mid:** im Ref-Fenster nicht messbar (kein Mid).  
**Bid/Ask-Bounce:** entfällt ohne Mid; Trade-Preis-Impact misst tatsächlich gehandelte Preise (inkl. Spread-Crossing) — für Aggressor-Efficiency fachlich passend.

Für Tage **mit** `orderbook_features_1s_v2`: Mid/Micro als Zweitmaß möglich, kausal nur geschlossene `bucket_start` / `last_source_ts ≤ trade_ts`.

---

## 11. Kleiner kausaler Smoke

Datei: `doge_causal_smoke.csv` (30 Zeilen).

Methode:

1. Trades → 1s-Buckets (nur Sekunden mit Trades; Coverage-Feld ausgewiesen).
2. Nur Fenster endend ≤ 15:30 (geschlossen relativ zum Audit-Ende des Tagesfensters).
3. Selektion **outcome-blind**: (a) feste Orientierungsfenster, (b) Top-Sell-/Top-Buy-Notional auf 5/30/60s-Grid, (c) Morning-Sell- vs Post-Noon-Buy-Fokus nach Notional.

Deskriptive Beobachtungen (keine Regel, kein Profit-Claim):

- **11:55 UTC 60s:** Sell-Notional ≈ 214k USDT, Buy ≈ 300, `max_down_bps = 0`, End move 0 → hohes aggressives Sell-Volumen ohne Down-Exkursion in diesem Fenster.
- **10:21 UTC 60s:** Sell ≈ 259k vs Buy ≈ 30k, `max_down_bps ≈ 8.3`, End ≈ −7.1 bps → Sell-dominant mit begrenztem Down.
- **12:20 UTC 60s:** Buy ≈ 1.06M vs Sell ≈ 202k, `max_up_bps ≈ 30.7`, End ≈ +22.4 bps → späteres Buy-Fenster mit deutlich stärkerer Aufwärts-Exkursion.

Das belegt **Daten-Machbarkeit** der Sequenz „Sell-inefficient → Buy-efficient“ als messbare Abfolge — **nicht**, dass ein Long profitabel war.

---

## 12. OI-Verfügbarkeit

- `open_interest_5s`: **vollständig** im Ref-Fenster (5 400 / 5 400 erwartete 5s-Buckets).
- `open_interest_events`: 5 336 Events.
- 5m-Join Smoke (deskriptiv): unter 90 Fünf-Minuten-Blöcken alle vier Quadranten (Preis↑/↓ × OI↑/↓) besetzt → **Klassifikator später machbar**.
- OI ist **kein** Entry-Trigger in diesem Audit; Auflösung 5s reicht für Squeeze vs. fresh-long Klassifikation auf ≥5m.

---

## 13. Performance- / Volumenschätzung

Aus Canonical-Counts:

| Scope | Schätzung Public-Trade-Rows |
|---|---|
| DOGE 1 Tag (2026-08-29) | ≈ 89k |
| DOGE Aug-Durchschnitt / Tag | ≈ 340k |
| DOGE 30 Tage | ≈ 5–10M (je nach Aktivität; Aug-Summe Domäne) |
| 51 Symbole, 1 Tag (2026-08-28) | ≈ **19.9M** rows |
| 51 Symbole, 30 Tage | grob **0.4–0.6B** rows (sehr grob; BTC/ETH-dominiert) |

Empfehlungen für spätere Research-Jobs:

- Immer `symbol` + `trade_ts` Partition-Pruning (`toYYYYMM` / Tageschunks).
- Keine unbegrenzten FINAL-Scans.
- Streaming/Chunk: 1 Symbol × 1 Tag → lokal aggregieren → Checkpoint nur in neuem Research-Outputordner.
- Persistente 1s-Aggregation **nicht zwingend**; bei 51×30d optional später erwägen (Felder: buy/sell count/qty/notional, OHLC trade, first/last). Vorhandene `orderbook_features_1s_v2` hat **keine** Aggressor-Notional-Felder und deckt Aug-29 nicht.

---

## 14. Blocker und Einschränkungen

1. **Kein Mid/Micro/BBO im DOGE-Ref-Fenster** (ticker bis 11.08., OB1s bis 28.08. 16:26).
2. **Intra-ms-Reihenfolge** nicht exchange-sequenziert.
3. **Burst-Grenzen:** Extreme Sekunden (bis 966 Trades) — Bucket-Grenzen können Bursts splitten.
4. **Überlappende Fenster** → Doppelzählung, wenn Scoring nicht non-overlap/causal gated.
5. **Lookahead:** High/Low eines noch offenen Fensters verboten; nur geschlossene Buckets.
6. **Kleine Notional-Nenner** → Efficiency-Scores explodieren; Floor/Winsorize nötig.
7. **`orderbook_deltas` broken** — kein Raw-L2-Fallback ohne Repair (Repair ist **außerhalb** dieses Audits).
8. Live-`public_trades` / Archive im Fenster leer — Research muss **canonical** nutzen.

**Nicht blockierend:** Side-Semantik, Trade-Coverage, OI, 1m-Candles.

---

## 15. Klare Empfehlung für den nächsten Schritt

1. **Neuen Research-Baustein** (separates Modul/Ordner) skizzieren: kausale 1s-Aggregation aus `public_trades_canonical` → Features `sell_impact_efficiency` / `buy_impact_efficiency` / Absorption-Scores auf **nicht-überlappenden** 5–30s-Fenstern mit Trade-Preispfad.
2. **Nenner-Skalierung zuerst robust machen:** Notional relativ zu rollierendem Median-1s- oder 1m-Volumen + MAD; Floor gegen Mikro-Trades.
3. **OI 5s** nur als nachgelagerter Label-/Klassifikator-Layer.
4. **Mid/Micro** nur an Tagen mit `orderbook_features_1s_v2`-Coverage als Parallelmaß — nicht Voraussetzung für v1.
5. **Keine** Produktions-Tabelle anlegen, bevor ein symbol×tag Smoke reproduzierbar und lookahead-frei ist.
6. Optional später: Coverage-Gap von OB1s/Ticker für aktuelle Tage klären (Collector-Frage) — separat vom Flip-Detector.

---

## Feature-Machbarkeit (Auftrag 7) — Kurz

| Feature | Machbar? | Hinweis |
|---|---|---|
| sell/buy_impact_efficiency | ja | Preisimpuls aus Trade-OHLC geschlossener Fenster / Notional-Normierung |
| sell/buy_absorption_score | ja | Perzentile symbol-lokal, kausal rolling past-only |
| long/short_efficiency_flip | ja mit Limitations | braucht strikte Zeitordnung prior→later + Structure-Confirm (5m/15m) ohne Lookahead |
| Nenner | USDT roh unsicher | besser: / rolling median notional oder / typical second volume; MAD-Scores |
| Risiken | dokumentiert | Div-by-tiny, Tick-Size, Burst-split, Mix Absorption/Breakout, overlap, open-window H/L |

---

*Ende des Audits. Keine Strategie-Implementierung erfolgt.*
