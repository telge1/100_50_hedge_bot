# Phase 2 — kontrollierter Ein-Tages-Pilot

## 1. Finales Verdict

`BTC_DOGE_RESEARCH_DB_PHASE_2_PILOT_READY`

Der Pilot für BTCUSDT und DOGEUSDT am 26. August 2026 UTC ist vollständig,
paritätsgeprüft und idempotent. Kein Full-History-Backfill wurde gestartet.

## 2. Repository

- Branch: `feature/btc-doge-research-db`
- Start-HEAD: `48bf56fbff1e82abee0c8ff09a95a1701df10965`
- Tracked Worktree beim Start und Abschluss: sauber
- Vorbestehende untracked Dateien wurden bewahrt; Phase-2-Code/-Reports sind neu untracked.
- Commit: keiner; Push: keiner.

## 3–6. Recovery-Preflight und Datenbank

`btc_doge_research` existierte bereits tatsächlich aus Phase 1. Deshalb wurde entgegen
dem neuen Namensvorschlag keine zweite Datenbank `market_research` erzeugt. Vorhanden
waren zwei Phase-1-Batches und Pilotdaten. Alle Phase-1-DDL-Tabellen waren real vorhanden;
keine existierte nur als Code.

Die Verbindung nutzt die bestehende sichere Research-Konfiguration. Der Account hat
technisch globale Rechte; jeder Phase-2-Write wurde deshalb vom bestehenden
fail-closed-Zielguard auf `btc_doge_research` begrenzt. Das Query-Log enthält 188
Research-DDL/DML-Queries und null Writes mit einem anderen Ziel.

Neu idempotent angelegt:

- `research_schema_versions`, `research_producer_lineage`, `research_batch_runs`,
  `research_data_quality_events`
- `research_public_trade_buckets_100ms`, `_500ms`, `_1s`
- `research_liquidation_buckets_500ms`, `research_open_interest_observations`
- `research_tpo_bracket_ranges_30m`, `research_tpo_profile_bins_session`
- `research_volume_profile_bins_session`, `research_profile_levels_session`
- `research_ob200_snapshots_1s`

Bestehende logische Bereiche wurden weiterverwendet: `research_coverage` für
Coverage-Segmente, `research_pipeline_state` für Source-Watermarks und
`research_liquidation_events` als kanonische Liquidationsevents.

## 7. Pilotdatum

Eingefroren wurde `2026-08-26T00:00:00Z` bis `2026-08-27T00:00:00Z`: der erste
vollständig bewiesene gemeinsame Kandidat. Der 25. August wurde nicht gewählt, weil OI
nur 17.279/17.280 Beobachtungen je Symbol besitzt.

Am 26. August wurden je Symbol 1.440 Candles und 17.280 OI-Beobachtungen bewiesen.
Alle 48 OB200-Stundensegmente wurden vollständig gelesen und fingerprinted:
BTC 863.816 Events, DOGE 784.972 Events, keine `data.u`-Gaps, keine Duplicate-`u`,
kein Queue-Overflow und kein Writer-Error.

## 8–10. Producer-, Transition- und Coverage-Contract

`research_producer_lineage_v1` speichert Live-Collector und Day-ZIP-Importer als getrennte
Producer. Live-Semantik ist `RECEIVE_TIME_ASOF`; `queue_full` setzt
`coverage_complete=false`. Day-ZIP wird nicht still als identisch behandelt.

Der Pilot verwendet ausschließlich Raw-Live-Collector-Dateien und
`raw_ob200_event_time_eos_v1`; kein Day-ZIP-/Live-Mix. Der queue_full-Terminal
`2026-08-28T16:26:23Z` liegt nicht im Pilottag, und es wird nicht darüber
carried-forward.

BTC und DOGE besitzen für den Pilottag vollständige Trades, Candles, OI und OB200.
Liquidationen sind ereignisbasiert; ein ereignisfreier Rand ist kein Gap.

## 11. Counts

- Trade-Buckets 100ms: BTC 163.383, DOGE 51.573, gesamt 214.956
- Trade-Buckets 500ms: BTC 96.578, DOGE 39.993, gesamt 136.571
- Trade-Buckets 1s: BTC 66.093, DOGE 33.158, gesamt 99.251
- Kanonische Liquidationen: BTC 803, DOGE 130, gesamt 933
- Liquidations-Buckets 500ms: BTC 520, DOGE 113, gesamt 633
- OI-Beobachtungen: BTC 17.280, DOGE 17.280, gesamt 34.560
- TPO-Brackets: BTC 21, DOGE 21, gesamt 42
- TPO-Bins: BTC 160, DOGE 81, gesamt 241
- Volume-Bins: BTC 160, DOGE 81, gesamt 241
- Profil-Level: BTC 6, DOGE 6, gesamt 12
- OB200-1s: BTC 86.400, DOGE 86.400, gesamt 172.800

## 12–14. Parität und Seam

Public Trades erhalten Count, Buy/Sell-Base, Buy/Sell-Quote, Delta sowie ersten/letzten
Timestamp exakt über Raw → 100ms → 500ms → 1s. BTC hat 2.131.954 und DOGE 370.993
deduplizierte Trades.

Alle 933 Liquidationen sind event-key-eindeutig. Long/Short und Forced-Side folgen dem
eingefrorenen Contract; 3.485.185,043 Base-Einheiten und
6.643.587,66557 Bankruptcy-Referenznotional bleiben erhalten.
`execution_notional` ist durchgehend NULL.

OI hat 17.280 eindeutige Originalbeobachtungen je Symbol, 5s Originalfrequenz und keine
Interpolation. Echtes 30m-Bracket-TPO und Basisvolumenprofil wurden separat berechnet;
beide Integritäts- und Conservation-Gates sind PASS.

Die 1.800 Phase-1B-Seam-Samples bleiben verbindlich: Receive-Time 1.796/1.800 exakt.
Die vier nicht reproduzierbaren Scheduler-Grenzen sind:

- BTCUSDT `2026-08-25T12:00:14Z`
- DOGEUSDT `2026-08-25T12:00:14Z`
- DOGEUSDT `2026-08-28T15:00:06Z`
- DOGEUSDT `2026-08-28T15:00:12Z`

Sie sind `ORDERING_AMBIGUOUS`, nicht Source Gaps. Es wurde keine Event-Loop-Reihenfolge
erfunden.

## 15. Idempotenz

Der erste erfolgreiche Abschluss erzeugte genau eine READY-Zeile. Jeder weitere Lauf mit
dem identischen Contract liefert `IDEMPOTENT_SKIP`. Ein zusätzlicher Beweislauf ließ
Batch-Rows bei 10→10 und sämtliche Fact-Counts/Checksums unverändert. Der Output-
Fingerprint ist `e7bbe6bf463b52e763ca2d3638ad802e27ad303a1afefbb00514166b7277db7c`.

Die zehn Batch-Rows enthalten bewusst RUNNING-/FAILED-/Recovery-Historie der drei
fail-closed Zwischenabbrüche sowie genau einen READY-Abschluss; ihre fachliche Identität
umfasst `started_at` und Status. Keine Fact-Dubletten entstanden.

## 16–19. Queries, Speicher und Laufzeit

- Trades/Liquidationen/OI, 60 Minuten: 6,15 ms warm (Ziel <3s)
- Profile einer Session: 1,89 ms warm (Ziel <1s)
- 3.601 vollständige OB-Sekunden: 227,09 ms warm (Ziel <5s)
- kombinierter Fight-Input: 8,69 ms warm (Ziel <10s)
- Alle Queries ohne `FINAL`; kein langsames EXPLAIN erforderlich.

Phase-2-Pilotdaten belegen 104.694.680 komprimierte Bytes (ca. 99,85 MiB).
OB200-1s belegt 82.661.769 Bytes bei 1.700.370.308 Bytes unkomprimiert
(Kompressionsfaktor 20,57). Lineare, ausdrücklich nicht garantierte Projektion:
30 Tage 3.140.840.400 Bytes, 90 Tage 9.422.521.200 Bytes. Für die in Phase 1
inventarisierten ca. 210 Raw-Stunden: ca. 916.078.450 Bytes.

Vollständiger Profil-/OB-Lauf: 304,385 s Wall-Time. Beobachteter Peak-RSS:
460.644 KiB. Der reine vollständige Source-/`u`-Scan dauerte 8,728 s.

## 20–21. Dateien und Tests

Neu unter `research/btc_doge_research/`:

- `phase2_contracts.py`, `phase2_ddl.py`, `phase2_transform.py`
- `phase2_runner.py`, `phase2_validate.py`

Neu: `tests/research/test_btc_doge_research_phase2.py` und die Artefakte in diesem
Ergebnisordner. Relevante Research-Tests: 118 PASS, 1 SKIP, 0 FAIL. Lints und
`git diff --check`: PASS.

## 22–24. Live-Sicherheit

Die Collector-PIDs 147111, 1661773 und 3946369 waren vor und nach dem Pilot unverändert
aktiv. Freier Speicher: 372 GiB vorher, 371 GiB nachher.

- Writes outside `btc_doge_research`: none
- Existing source database changes: none
- `orderbook_deltas` repair/query attempts: none
- Collector changes/restarts: none
- Dashboard, Live-Bot, Trading-Regeln: unchanged
- Full-history backfill: not started
- Persistent watcher: not started
- Commit: none
- Push: none
