# ABSCHLUSSBERICHT — Phase 0 BTC/DOGE Research Database

**Phase:** 0 — Dateninventur, Semantik-Contract, Architekturentwurf
**Datum (UTC):** 2026-09-02
**Output:** `results/research_db_phase_0_inventory_contract_v1/`

---

## 1. Finales Verdict

**`BTC_DOGE_RESEARCH_DB_PHASE_0_READY_WITH_EXPLICIT_GAPS`**

Phase 0 ist abgeschlossen. Inventur, Semantik-Contract und Zielarchitektur sind belastbar dokumentiert. Explizite Lücken (OB-Aggregat-Ende, FS-Archiv-Start, OI-Collector-Stall, kein Funding in CH) sind benannt und blockieren den Pilot nicht, sofern Phase 1 mit Golden Windows und Hybrid-OB-Pfad startet.

---

## 2. Branch, HEAD und Dirty-Status

| Check | Ergebnis |
|-------|----------|
| Branch | `feature/btc-doge-research-db` ✓ |
| HEAD | `4a083bcdf4cefcc245a3c15ff842df01de411f36` ✓ |
| Tracked dirty | **sauber** ✓ |
| Untracked | nur vorbestehende Artefakte (`.md`, `results/`, etc.) — unverändert |

---

## 3. Sicherheitsbestätigung

| Operation | Count |
|-----------|-------|
| ClickHouse writes | **none** |
| DDL executed | **none** |
| DML executed | **none** |
| Collector changes | **none** |
| Collector restarts | **none** |
| Dashboard changes | **none** |
| Live changes | **none** |
| Existing results modified | **none** |
| Existing untracked artifacts modified | **none** |
| Commit | **none** |
| Push | **none** |

Nur SELECT/WITH-Abfragen mit `max_execution_time` und `sql_guard`. Keine Collector-/Dashboard-Prozesse gestartet oder gestoppt.

---

## 4. Inventarisierte Datenquellen

10 Quellen in `source_inventory.csv` (Details je Pflichtfeld):

| source_id | System | Typ |
|-----------|--------|-----|
| CH_CANDLES_1M | ClickHouse | 1m Kerzen |
| CH_PUBLIC_TRADES_CANONICAL | ClickHouse | Public Trades kanonisch |
| CH_OB_FEATURES_1S_V2 | ClickHouse | OB 1s Aggregat (historisch) |
| FS_RAW_OB200_V3 | Filesystem | Raw OB200 Snapshots/Deltas |
| CH_OPEN_INTEREST_5S | ClickHouse | OI 5s |
| CH_ALL_LIQUIDATIONS | ClickHouse | Liquidationen |
| CH_PUBLIC_TRADES_ARCHIVE | ClickHouse | DOGE History Jan-Feb 2026 |
| CH_TICKER_SAMPLES | ClickHouse | Ticker/Funding legacy |
| RESEARCH_BTC_OB_FIGHT | Filesystem | Golden Research Runs |
| CH_ORDERBOOK_DELTAS_LEGACY | ClickHouse | **BLOCKED** |

Evidence: `evidence/inventory_query_results.json`, `evidence/supplemental_queries.json`

---

## 5. BTC-Coverage

| Datenart | Von (UTC) | Bis (UTC) | Rows/Events |
|----------|-----------|-----------|-------------|
| candles_1m | 2025-12-11 | 2026-09-02 | 382 637 |
| public_trades | 2026-07-19 | 2026-09-02 | 72 475 097 |
| ob_features_1s | 2026-07-19 | **2026-08-28** | 3 300 775 |
| ob_raw FS | 2026-08-24 | 2026-09-02 | 211 Segmente / 210h |
| open_interest_5s | 2026-08-18 | 2026-09-01* | 243 085 |
| liquidations | 2026-08-18 | 2026-09-01 | 34 900 events |

\* OI max `2026-09-01T14:46:50Z` — Collector stale at audit.

7-Tage-Candle-Smoke: **100%** vollständig, 0 bad_hl/bad_px.

---

## 6. DOGE-Coverage

| Datenart | Von (UTC) | Bis (UTC) | Rows/Events |
|----------|-----------|-----------|-------------|
| candles_1m | 2025-12-11 | 2026-09-02 | 382 636 |
| public_trades | 2026-07-19 | 2026-09-02 | 13 474 573 |
| ob_features_1s | 2026-07-19 | **2026-08-28** | 3 298 811 |
| ob_raw FS | 2026-08-24 | 2026-09-02 | 211 Segmente / 210h |
| open_interest_5s | 2026-08-18 | 2026-09-01* | 242 353 |
| liquidations | 2026-08-18 | 2026-09-01 | 3 477 events |
| trades_archive | 2026-01-06 | 2026-02-28 | 2 710 057 (pre-canonical) |

Referenz-Audit DOGE 2026-08-29: `results/aggressor_efficiency_data_audit_v1/`

---

## 7. History-/Live-Übergänge

### Public Trades
- **archive:** 2026-07-18 → 2026-08-20 (BTC 42.6M / DOGE 6.0M rows)
- **live:** ab 2026-08-17 (Overlap ~3 Tage)
- **Dedup:** `trade_id`; duplicates=0 at FINAL globally
- **Phase 1 Pflicht:** Konflikt-Audit im Overlap-Fenster

### Orderbook
- **CH aggregate:** endet `2026-08-28T16:26:23Z` (Collector-Moduswechsel)
- **FS raw archive:** ab `2026-08-24T22:47:53Z` live
- **Lücke FS vor 2026-08-24:** nur CH-Aggregat verfügbar

### Open Interest
- **5m history:** BTC only bis 2026-08-18
- **5s live:** ab 2026-08-18; Stall ~2026-09-01

---

## 8. Daten außerhalb ClickHouse

| Pfad | Inhalt |
|------|--------|
| `orderbook_analyse/.../ob200_v3/{BTC,DOGE}USDT/` | Hourly zst + manifest (Primary post-2026-08-28) |
| `results/btc_ob_fight_cases/` | Golden runs run_011..run_018 |
| `results/aggressor_efficiency_*` | DOGE trade semantics audits |

Kein MySQL/InfluxDB-Bezug in Projektconfigs gefunden.

---

## 9. Gefundene Lücken und Überschneidungen

**Lücken:**
- OB-Aggregat ohne Live-Fortschreibung nach 2026-08-28
- FS-Raw erst ab 2026-08-24 (4-Tage-Lücke zu reinem Raw-Rebuild Jul-Aug)
- OI-Collector stale seit ~2026-09-01
- Funding: keine CH-Tabelle
- Trades canonical erst ab 2026-07-19
- `orderbook_deltas`: 108 broken parts — unattached

**Überschneidungen:**
- Trades archive+live 2026-08-17..20
- OB CH aggregate + FS raw 2026-08-24..28 (parität prüfen)

---

## 10. Semantik-Konflikte

| Thema | Status |
|-------|--------|
| Trade side = taker aggressor | **PROVEN** (prior audits + schema) |
| Liquidation S-mapping v1 | **PROVEN** (frozen contract + CH sample) |
| execution_price für Liq | **NULL by contract** |
| Trade seq | **NOT STORED** — nicht erfinden |
| OI long/short split | **NOT AVAILABLE** — nicht erfinden |
| OB genuine vs CF | **PROVEN** in aggregate (~1.08% CF via quality_flags) |
| Raw vs aggregate OB | **Different valid semantics** — dokumentiert |

Kein zentraler Side-/Timestamp-Konflikt ungeklärt für Pilot-Fenster.

---

## 11. Source of Truth je Datenart

Siehe `source_priority.json`. Kurz:

| Datenart | Primary |
|----------|---------|
| Candles | `signal_generator.candles_1m` |
| Public Trades | `public_trades_canonical` |
| OB Raw | FS `ob200_v3` |
| OB 1s | **research layer** (build); history fallback `orderbook_features_1s_v2` |
| OI | `open_interest_5s` |
| Liquidations | `all_liquidations` + frozen v1 contract |
| Funding | **NONE** |
| TPO/Profile | compute from trades |
| Pool/Wall/Fight | compute; heuristics UNFROZEN |

---

## 12. Vorgeschlagene Tabellen

Neue DB `btc_doge_research`:

1. `research_public_trades`
2. `research_liquidation_events`
3. `research_orderbook_1s`
4. `research_market_1s`
5. `research_market_1m`
6. `research_orderbook_levels` (optional Phase 2)
7. `research_coverage`
8. `research_pipeline_state`
9. `research_features` (abgeleitet, versioniert)

DDL: `proposed_schema.sql` — **DESIGN ONLY, NOT EXECUTED**

---

## 13. Vorgeschlagene Partitionierung und Sortierung

| Tabelle | PARTITION BY | ORDER BY |
|---------|--------------|----------|
| public_trades | (symbol, YYYYMM) | (symbol, event_time, trade_id) |
| liquidation_events | (symbol, YYYYMM) | (symbol, event_time, event_key) |
| orderbook_1s | (symbol, YYYYMMDD) | (symbol, bucket_time) |
| market_1s | (symbol, YYYYMMDD) | (symbol, bucket_time) |
| market_1m | (symbol, YYYYMM) | (symbol, bucket_time) |

Engine: ReplacingMergeTree mit `ingested_at`; Views mit argMax für dedup ohne FINAL.

---

## 14. Speicherabschätzung

| Horizont | ESTIMATED (beide Symbole, ohne levels) |
|----------|----------------------------------------|
| 1 Tag | 1–2 GB |
| 1 Monat | 30–60 GB |
| OB levels (falls materialisiert) | +5× — **deferred** |

---

## 15. Erwartete Query-Performance

**NOT_PROVEN** — Ziele in `performance_benchmark_plan.md`:

- Point ±30 min: < 5s
- 1h: < 2s
- 1d 1s: < 5s
- Erhebliche Beschleunigung vs. ~20 min Rohpipeline (Hypothese)

Benchmarks erst Phase 1 nach Tabellenbefüllung.

---

## 16. Full-History-Backfill-Plan

`full_history_backfill_plan.md` — 12-Schritte-Reihenfolge, 1-Tag-Batches, BTC vor DOGE, Pilot:

- BTC: 2026-08-31 18:30–19:30 UTC (run_018)
- DOGE: 2026-08-29 08:00–15:30 UTC

Idempotenz via ReplacingMergeTree + Tages-Checksum. Rollback ohne Raw-Verlust.

---

## 17. Inkrementeller Processor-/Watcher-Plan

`incremental_processor_design.md`:

- Separater Processor hinter Collectors
- Checkpoint in `research_pipeline_state`
- Overlap/W watermark OPEN CONFIG (aus ingest lag ableiten)
- Symbol-Isolation, SELECT-only auf Quellen
- OB: CH import bis 2026-08-28, danach FS replay

---

## 18. Akzeptanzkriterien

`acceptance_criteria.json` — 15 Gates für Phase 1 inkl.:

`UTC_CONTRACT_PROVEN`, `PUBLIC_TRADE_DEDUP_PROVEN`, `LIQUIDATION_V1_PARITY_PROVEN`, `ORDERBOOK_RECONSTRUCTION_PARITY_PROVEN`, `GENUINE_CARRIED_FORWARD_PRESERVED`, `BACKFILL_IDEMPOTENT`, `INCREMENTAL_IDEMPOTENT`, `COLLECTOR_UNCHANGED`

---

## 19. Offene Blocker

**Keine harten Blocker** für Pilot.

Offene Punkte (`open_questions.json`):

- OI-Collector-Restart (ops)
- Trade-Seam-Konfliktregel archive vs live
- Processor-Interval/Overlap/Finalization-Werte messen
- Funding-Quelle klären (Phase 2)

---

## 20. Empfehlung für Phase 1

1. **DDL ausführen** — nur `btc_doge_research` DB (Collector unberührt)
2. **Pilot-Backfill** Golden Windows BTC + DOGE
3. **Parität beweisen** gegen run_018 + aggressor audit
4. **Hybrid OB 1s** — CH bulk ≤2026-08-28, FS replay danach
5. **OI-Gap schließen** bevor Live-Inkremental
6. **source_priority.json einfrieren** nach Pilot
7. **Benchmarks** aus `performance_benchmark_plan.md` ausführen
8. **Incremental processor** aktivieren mit gemessenen Overlap-Werten

---

## Anhang: Semantik-Contract

Vollständig in `semantic_contract.md`. Liquidation `liquidation_flow_facts_v1` **frozen** — unverändert übernommen aus `research/btc_ob_fight/liquidation_flow_contract.py`.

---

## Abschluss-Sicherheitsbestätigung

```text
ClickHouse writes: none
DDL executed: none
DML executed: none
Collector changes: none
Collector restarts: none
Dashboard changes: none
Live changes: none
Existing results modified: none
Existing untracked artifacts modified: none
Commit: none
Push: none
```

**Phase 0 endet hier. Phase 1 (DB-Erstellung, Pilotimport, Backfill) nicht gestartet.**
