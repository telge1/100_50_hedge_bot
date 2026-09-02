# Semantik-Contract — BTC/DOGE Research Database (Phase 0)

**Version:** `research_semantic_contract_v0`
**Status:** DESIGN — not frozen until Phase 1 pilot parity
**Symbols:** BTCUSDT, DOGEUSDT

---

## 1. Zeit — globale Regeln

| Feldtyp | Semantik | Spalten (Quelle) |
|---------|----------|------------------|
| Event-Time | Exchange-reported event timestamp | `trade_ts`, `event_time`, `exchange_ts` |
| Receive-Time | Collector receive | `ingest_timestamp`, `received_at` |
| System-Time | Insert/processor | `inserted_at`, `ingested_at` |
| Bucket-Time | Identität des Aggregationsfensters | `open_time`, `bucket_start`, `bucket_time` |

**Regeln:**
- UTC überall; `DateTime64(3)` für ms-Events, `DateTime64(0)` für 1s/1m-Buckets.
- Bucket `[T, T+Δ)` — untere Grenze inklusiv, obere exklusiv.
- Event-Time und Ingestion-Time **getrennt halten** in allen Research-Tabellen.
- Identische Event-Timestamps: Sortierung `event_time, trade_id` (Trades) bzw. `event_time, event_key` (Liq).
- **Niemals** Ingest-Reihenfolge als Event-Reihenfolge verwenden (~8k trade_ts-Rücksprünge bei ingest-Sort).

---

## 2. Public Trades

**Quelle:** `orderbook_analysis.public_trades_canonical`

| Feld | Semantik |
|------|----------|
| `trade_ts` | Exchange Event-Time (ms) |
| `ingest_timestamp` | Receive-Time (µs) |
| `side` | **Taker-Aggressor** (Buy/Sell) — NOT maker |
| `price` | Execution price |
| `size` | Base size |
| `notional` | Quote notional |
| `trade_id` | Dedup-Schlüssel |
| `source` | `archive` \| `live` — History/Live-Marker |

**Nicht vorhanden:** Trade-Sequenznummer (`seq`) — **nicht erfinden**.

**Dedup:** `trade_id` + `FINAL` auf Quelle; Research-Schicht dedup via `ReplacingMergeTree(ingested_at)`.

**History/Live:** Überlappung 2026-08-17..2026-08-20 (archive+live). Phase 1 muss Konflikte pro `trade_id` beweisen.

**Identische Timestamps:** Bis 490 Trades/ms (DOGE-Referenzaudit); Burst-Kompression erlaubt.

---

## 3. Liquidationen — `liquidation_flow_facts_v1` (FROZEN)

```text
contract_version = liquidation_flow_facts_v1
contract_frozen = true
```

**Bybit allLiquidation Mapping** ([Docs](https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation)):

| raw `S` | liquidated_position_side | forced_trade_direction |
|---------|--------------------------|------------------------|
| Buy | LIQUIDATED_LONG | FORCED_SELL |
| Sell | LIQUIDATED_SHORT | FORCED_BUY |

**Felder:**
- `size` / `v` = executed base size
- `bankruptcy_price` / `p` = bankruptcy price — **NOT execution price**
- `bankruptcy_reference_quote` = Σ(v × p) — reference only
- `execution_price` = **NULL**
- `execution_notional` = **NULL**
- Dedup: versionierter `event_key`

**Verboten:**
- Gemeinsame ID zwischen Liquidation und Public Trade erfinden
- Liq↔Trade-Zuordnung in neutralen Facts (bleibt heuristisch in Feature-Schicht)
- Trade-Volumen doppelt zuordnen
- Liquidationsquote vom Taker-Delta subtrahieren
- SUPERSEDED explanatory audit als kanonische Quelle

**Beweis Phase 0:** Side-Mapping in CH bestätigt (BTC: Sell→SHORT 26190, Buy→LONG 8710).

---

## 4. Open Interest

**Quelle:** `open_interest_5s`

| Feld | Semantik |
|------|----------|
| `open_interest` | Total OI (base) |
| `open_interest_value` | Total OI (quote) |
| `bucket_time` | 5s-Bucket-Identität |
| `source_event_time` | Exchange event |
| `state_age_ms` | Staleness indicator |

**Regeln:**
- Nur Gesamt-OI und OI-Delta berichten.
- OI-Anstieg ≠ neue Longs; OI-Abfall ≠ bestimmte Positionsrichtung.
- **Keine** Long/Short-Aufteilung erfinden.
- Forward-Fill nur mit `quality_flags` markiert in Research-Schicht.

---

## 5. Orderbook

### 5.1 Raw (FS ob200_v3)

- Format: zstd-NDJSON hourly segments + manifest
- Events: snapshot + delta
- Sequenz: `seq` + `u` (Bybit); `u` muss +1 sein (sonst book invalid)
- Replay: `research/btc_ob_fight/ob_replay.py` MutableBook

### 5.2 Aggregat (CH orderbook_features_1s_v2)

- `bucket_start` = 1s-Identität
- `first_source_ts` / `last_source_ts` = Event-Spanne in Sekunde
- `quality_flags`: `""` = genuine update; `"carried_forward"` = CF (~1.08%)
- `is_valid`: 1 = usable
- **Static since 2026-08-28T16:26:23Z** — kein Live-Fortschreiben

### 5.3 Genuine vs Carried-Forward

| Quelle | Genuine | CF |
|--------|---------|-----|
| CH aggregate | `quality_flags != 'carried_forward'` | `quality_flags = 'carried_forward'` |
| Raw replay | Sekunde mit ≥1 Book-Update | Sekunde ohne Update, last state forward |

**Raw vs Aggregat-Unterschiede** sind unterschiedliche gültige Semantiken — nicht automatisch Fehler.

### 5.4 Preis/Tick

- BTCUSDT tick: 0.1
- DOGEUSDT tick: 0.00001 (Bybit linear)
- Size: Base-Asset-Einheiten

---

## 6. Candles

**Quelle:** `signal_generator.candles_1m`

- `open_time` = Bucket-Start UTC
- Nur `is_closed=1` für kausale Features
- 7-Tage-Smoke: 100% vollständig BTC+DOGE

---

## 7. Funding

**Status:** Keine dedizierte CH-Tabelle gefunden.
`ticker_samples.funding_rate` existiert im Schema, Coverage **NOT_PROVEN**.
Funding bleibt **außerhalb** neutraler Research-Facts bis Quelle geklärt.

---

## 8. Abgeleitete Features (Schicht-Trennung)

Neutrale Facts (`research_market_1s`, `research_market_1m`, `research_orderbook_1s`) enthalten **nicht**:

- Trading-Signale
- Entry/Exit-Regeln
- Outcomes / Labels aus Zukunft
- PEAK/RECLAIM-Hindsight-Phasen
- Strategie-Schwellenwerte

Abgeleitete Features → `research_features` mit:

```text
feature_contract_version
causal_or_hindsight
usable_for_live_signal
input_watermark
computed_at
```

---

## 9. Datenqualität & Lücken

- Liquidations-Stream: event-only; 0 Events ≠ numerische Null-Lücke
- OI-Frequenz (5s) ≠ Trades (ms) ≠ OB (1s)
- Lücken explizit in `research_coverage` — kein stilles Kaschieren

---

## 10. Verweise

- `research/btc_ob_fight/liquidation_flow_contract.py` — frozen
- `results/aggressor_efficiency_data_audit_v1/` — DOGE trade semantics
- `results/btc_ob_fight_cases/20260831T190000Z/run_018/` — golden parity
- `results/btc_ob_fight_explanatory_audit_20260831_1900_v1/SUPERSEDED.json` — do not use
