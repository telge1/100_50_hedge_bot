# ABSCHLUSSBERICHT — BTC/DOGE Research DB Phase 1

## 1. Finales Verdict
BTC_DOGE_RESEARCH_DB_PHASE_1_PILOT_READY_WITH_EXPLICIT_GAPS

## 2. Branch, Start-HEAD und Dirty-Status
`feature/btc-doge-research-db` / `b0521b3918dff5c049cd22a58e03fec71cb55ff5` / tracked clean at start

## 3. gelesene Phase-0-Verträge
Alle 15 verpflichtenden Artefakte vollständig gelesen; CSVs aus committed blobs wegen Cursor-Default-Ignore.

## 4. angelegte Datenbank und Tabellen
`btc_doge_research`: research_coverage, research_ingestion_batches, research_liquidation_events, research_market_1m, research_market_1m_invalid_timezone_v0, research_market_1s, research_market_1s_invalid_timezone_v0, research_orderbook_1s, research_orderbook_levels_pilot, research_orderbook_ob200_snapshots, research_pipeline_state, research_public_trades, research_source_files

## 5. ausgeführte DDL
Idempotente `CREATE DATABASE/TABLE IF NOT EXISTS` aus `applied_schema.sql`. Nach bewiesenem Python-UTC-Joinfehler wurden ausschließlich die im Pilot neu erzeugten Market-Tabellen per `RENAME TABLE` verlustfrei als `*_invalid_timezone_v0` erhalten und die kanonischen Tabellen neu angelegt; keine Drops, Truncates oder Mutations.

## 6. bewiesenes OB200-Quelldateiformat
zstd-NDJSON `ob200_v3_live_archive/v1`; `rotation_checkpoint` enthält 200×200 Vollsnapshot; Deltas über `data.u`.

## 7. ausgewähltes OB200-Speicherformat
Eine Row pro rekonstruiertem Event, vier kompakte Decimal-Arrays.

## 8. Arrays/Nested gegenüber normalisierten Levels
Arrays kanonisch; normalisierte Volllevel nur 10-Snapshot-`PILOT_ONLY`-Sample.

## 9. BTC-Pilotfenster
BTCUSDT 2026-08-31T18:30:00Z–19:30:00Z (run_018).

## 10. DOGE-Pilotfenster
DOGEUSDT 2026-08-29T11:45:00Z–12:30:00Z; enthält die committed 11:55-/12:20-Probes.

## 11. importierte Source-Dateien und Fingerprints
4 registrierte Segmentverwendungen; SHA-256 in Manifest/CSV.

## 12. vollständige Level-Parität
{"research_orderbook_ob200_snapshots:BTCUSDT": {"physical_rows": 35999, "logical_keys": 35999, "duplicate_keys": 0, "min_key": "0001178285b2d48b13f3dc30134252a179293df04b7f1b996b005e06e1d67581", "max_key": "ffff3eed6aefcb4145ea79cf0fc8c689f0b51d11dfe9fd3727596a49811384ba", "key_fingerprint": "1601400418328699925", "uses_final": false}, "research_orderbook_ob200_snapshots:DOGEUSDT": {"physical_rows": 20415, "logical_keys": 20415, "duplicate_keys": 0, "min_key": "0000e1fb9946383784d44d17e92d207c8757157a5acc14472d890779a5a84fae", "max_key": "ffff2086c78a4a6056408c70df097df1d636dcd34cd0b1e9a1bfb8f2bb62b051", "key_fingerprint": "10613978052767195348", "uses_final": false}}

## 13. Source-of-Truth je Datenart
Trades canonical; Liquidationen all_liquidations/v1; OI open_interest_5s; OB FS raw; Funding NOT_AVAILABLE.

## 14. Public-Trade-Dedup
[{"logical_rows": 80738, "physical_duplicates": 0, "physical_rows": 80738, "rows_in_duplicate_groups": 0, "uses_final": false}, {"logical_rows": 4275, "physical_duplicates": 0, "physical_rows": 4275, "rows_in_duplicate_groups": 0, "uses_final": false}]

## 15. Liquidations-v1-Parität
PASS

## 16. OI-/Funding-Status
OI Freshness explizit; Funding `NOT_AVAILABLE`.

## 17. Orderbook-History-/Raw-Seam
Alle sechs Fenster haben 300/300 Sekunden und identische genuine/CF-Flags, aber Werte außerhalb der Toleranz. Klassifikation `NOT_COMPARABLE`; konkrete Ursache der Raw-/Aggregat-Semantik nicht bewiesen, Pflicht-Gate daher `BLOCKED`.

## 18. genuine/carried_forward
1s-Buckets speichern beide Flags explizit; Raw-Events sind genuine.

## 19. erster Import
[{"liquidation_events": 60, "market_1m": 60, "market_1s": 3600, "orderbook_1s": 3600, "orderbook_ob200_snapshots": 35999, "pilot_normalized_levels": 4000, "public_trades": 80738, "source_files_new": 2}, {"liquidation_events": 4, "market_1m": 45, "market_1s": 2700, "orderbook_1s": 2700, "orderbook_ob200_snapshots": 20415, "pilot_normalized_levels": 4000, "public_trades": 4275, "source_files_new": 2}]

## 20. zweiter Idempotenzlauf
Identische Batches wurden als IDEMPOTENT_SKIP erkannt.

## 21. physische und logische Row Counts
{"research_public_trades:BTCUSDT": {"physical_rows": 80738, "logical_keys": 80738, "duplicate_keys": 0, "min_key": "BTCUSDT|00000335-e735-5d93-8204-0262f683c6aa", "max_key": "BTCUSDT|ffff42fe-d5a1-576e-806f-bc37b8b0dfb9", "key_fingerprint": "9358544341830936726", "uses_final": false}, "research_public_trades:DOGEUSDT": {"physical_rows": 4275, "logical_keys": 4275, "duplicate_keys": 0, "min_key": "DOGEUSDT|00092b57-ecfa-549c-9df9-b94a5e8f5a0b", "max_key": "DOGEUSDT|ffd31b97-c87a-5b92-855b-b6246a6710be", "key_fingerprint": "6132423911095494159", "uses_final": false}, "research_liquidation_events:BTCUSDT": {"physical_rows": 60, "logical_keys": 60, "duplicate_keys": 0, "min_key": "BYBIT|BTCUSDT|1788201467368|Buy|0.001|78760.30", "max_key": "BYBIT|BTCUSDT|1788203654761|Sell|0.009|79482.00", "key_fingerprint": "5693070044955539907", "uses_final": false}, "research_liquidation_events:DOGEUSDT": {"physical_rows": 4, "logical_keys": 4, "duplicate_keys": 0, "min_key": "BYBIT|DOGEUSDT|1788006005201|Sell|122|0.08547", "max_key": "BYBIT|DOGEUSDT|1788006005468|Sell|1500|0.08553", "key_fingerprint": "252754385939822363", "uses_final": false}, "research_orderbook_ob200_snapshots:BTCUSDT": {"physical_rows": 35999, "logical_keys": 35999, "duplicate_keys": 0, "min_key": "0001178285b2d48b13f3dc30134252a179293df04b7f1b996b005e06e1d67581", "max_key": "ffff3eed6aefcb4145ea79cf0fc8c689f0b51d11dfe9fd3727596a49811384ba", "key_fingerprint": "1601400418328699925", "uses_final": false}, "research_orderbook_ob200_snapshots:DOGEUSDT": {"physical_rows": 20415, "logical_keys": 20415, "duplicate_keys": 0, "min_key": "0000e1fb9946383784d44d17e92d207c8757157a5acc14472d890779a5a84fae", "max_key": "ffff2086c78a4a6056408c70df097df1d636dcd34cd0b1e9a1bfb8f2bb62b051", "key_fingerprint": "10613978052767195348", "uses_final": false}, "research_orderbook_1s:BTCUSDT": {"physical_rows": 3600, "logical_keys": 3600, "duplicate_keys": 0, "min_key": "BTCUSDT|2026-08-31T18:30:00+00:00|btc_doge_research_phase_1_v1", "

## 22. Parität
BTC=PASS; DOGE=PILOT_WITH_GOLDEN_DESCRIPTIVE_REFERENCE.

## 23. Speicherbedarf
[{"table": "research_orderbook_levels_pilot", "physical_rows": 8000, "compressed_bytes": 22614, "uncompressed_bytes": 1064171}, {"table": "research_orderbook_ob200_snapshots", "physical_rows": 56414, "compressed_bytes": 21562677, "uncompressed_bytes": 568225814}]

## 24. Performance
{"single_timestamp_ob200": 4.286, "ob200_plus_minus_5m": 2.448, "ob200_plus_minus_30m": 2.867, "ob200_one_hour": 2.459, "btc_1m": 2.724, "btc_1s": 16.181, "doge_1m": 3.039, "doge_1s": 11.84, "trade_events": 2.977, "liquidations": 2.396, "orderbook_1s": 10.369, "pool_wall_near_levels": 21.588, "joined_market": 4.759}

## 25. Acceptance-Gates
{"UTC_CONTRACT_PROVEN": "PASS", "SOURCE_PRIORITY_FROZEN": "PASS", "PUBLIC_TRADE_DEDUP_PROVEN_FOR_PILOT": "PASS", "LIQUIDATION_V1_PARITY_PROVEN": "PASS", "OB200_SOURCE_FORMAT_PROVEN": "PASS", "OB200_FULL_LEVELS_PRESERVED": "PASS", "OB200_SOURCE_FILE_PROVENANCE_PRESERVED": "PASS", "OB200_STORAGE_FORMAT_SELECTED": "PASS", "OB200_PILOT_IMPORT_COMPLETE": "PASS", "OB200_PILOT_REIMPORT_IDEMPOTENT": "PASS", "ORDERBOOK_RECONSTRUCTION_PARITY_PROVEN": "PASS", "HISTORY_RAW_SEAM_PROVEN_FOR_OVERLAP": "BLOCKED", "GENUINE_CARRIED_FORWARD_PRESERVED": "PASS", "PILOT_FIRST_LOAD_COMPLETE": "PASS", "PILOT_SECOND_LOAD_IDEMPOTENT": "PASS", "NO_PHYSICAL_DUPLICATES_AFTER_RERUN": "PASS", "NO_FINAL_REQUIRED": "PASS", "OI_STALENESS_EXPLICIT": "PASS", "FUNDING_NOT_AVAILABLE_EXPLICIT": "PASS", "NO_HINDSIGHT_IN_LIVE_FACTS": "PASS", "PREFIX_CAUSALITY_PROVEN": "PASS", "POINT_QUERY_TARGET_MET": "PASS", "WINDOW_QUERY_TARGET_MET": "PASS", "COLLECTOR_UNCHANGED": "PASS", "EXISTING_DATABASES_UNCHANGED": "PASS"}

## 26. offene Gaps
History-/Raw-Seam ist trotz vollständiger Sekunden-/Quality-Coverage wegen ungeklärter Wertabweichungen `BLOCKED`. Legacy-Manifeste bewerten `seq` fälschlich als Kontinuität und bleiben replayable=false/open; `data.u` wurde separat lückenlos bewiesen. Die fehlerhaften ersten Market-Materialisierungen bleiben transparent als `*_invalid_timezone_v0` erhalten; die neu materialisierten kanonischen Tabellen sind validiert. OI bleibt nach 2026-09-01 stale; Funding fehlt.

## 27. geänderte/neue Code-Dateien
`research/btc_doge_research/`, `scripts/run_btc_doge_research_pilot.py`, `tests/research/test_btc_doge_research_phase1.py`.

## 28. neue Ergebnisartefakte
`results/research_db_phase_1_pilot_v1/` (keine Raw-Dumps).

## 29. verbindlicher Phase-2-Plan
Alle historischen BTC-/DOGE-OB200-Segmente einmalig fingerprinten/importieren, vollständige Level und Seam beweisen, danach separater checkpoint-basierter Processor im identischen Format; Collector unverändert.

## 30. Sicherheitsbestätigung
Writes outside btc_doge_research: none
Existing database changes: none
orderbook_deltas repair attempts: none
Collector changes: none
Collector restarts: none
Dashboard changes: none
Live changes: none
Trading-rule changes: none
Full-history backfill: not started
Persistent watcher: not started
Existing results modified: none
Existing untracked artifacts modified: none
Commit: none
Push: none
