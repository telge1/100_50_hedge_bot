# ABSCHLUSSBERICHT — BTC/DOGE Research DB Phase 1B

## 1. Verdict
BTC_DOGE_RESEARCH_DB_PHASE_1B_DIFFERENT_VALID_SEMANTICS_PROVEN

## 2. Branch/HEAD/Dirty
Start: `feature/btc-doge-research-db` / `48bf56fbff1e82abee0c8ff09a95a1701df10965` / tracked clean. Am Ende sind nur der
freigegebene Phase-1B-Code, Tests und Ergebnisordner neu.

## 3. Sicherheitsstatus
Der Audit nutzte ausschließlich SELECT-Abfragen und bounded Raw-Replays. Kein DDL/DML.

## 4. Geprüfte Fenster
Die sechs unveränderten Phase-1-Fenster: je 300 Sekunden am 25.08. 12:00, 26.08. 06:30
und 28.08. 15:00 UTC für BTCUSDT und DOGEUSDT. Keine Zusatzfenster waren notwendig.

## 5. Bestehende Seam-Semantik
Phase 1 wählte den letzten Raw-Event nach Exchange-Event-Time in `[bucket,bucket+1)`;
fehlende Sekunden wurden kausal fortgeschrieben. UTC und der Timestamp-Join sind korrekt.

## 6. Historische Producer-Lineage
`orderbook_features_1s_v2` entstand über Live-Collector → `LiveSecondClock` →
`compute_features` → `FeatureWriter`. Der Clock floort Exchange-Event-Time, finalisiert
Buckets aber am lokalen Wall-Clock-Sekundenende. Ein Event mit Event-Time `.928`, das
erst lokal bei `+1.015` ankommt, ändert den bereits finalisierten Vorbucket nicht, fließt
aber in den Folgezustand ein. Die Tabelle trägt keinen expliziten Semantikversionswert.
Ein separater Day-ZIP-Importer schreibt über denselben Feature-Builder in dieselbe Tabelle,
sein belegtes Fenster endet jedoch am 17.08.2026. Für den geprüften 24.–28.08.-Overlap
existieren null Import-Manifest-Rows; `created_at` und das `queue_full`-Ende um
`2026-08-28T16:26:23Z` belegen den Live-Pfad.

## 7. Rekonstruktionsvarianten
Getestet wurden: FIRST_EVENT_IN_SECOND, LAST_EVENT_IN_SECOND, LAST_EVENT_AT_OR_BEFORE_SECOND_START, LAST_EVENT_AT_OR_BEFORE_SECOND_END, NEAREST_EVENT_TO_SECOND_START, NEAREST_EVENT_TO_SECOND_END, EVENT_TIME_LAST, RECEIVE_TIME_LAST, CURRENT_PHASE1_IMPLEMENTATION.

## 8. BTC-Ergebnisse
Receive-Time/Wall-As-Of: 899/900 exakt.
Phase-1 Event-Time-Last: 35/900 exakt.

## 9. DOGE-Ergebnisse
Receive-Time/Wall-As-Of: 897/900 exakt.
Phase-1 Event-Time-Last: 11/900 exakt.

## 10. Event-Lineage-Stichproben
Je Symbol 10 Phase-1-Mismatches und 5 Kontrollen. Der CH-Terminalevent aus
`last_source_ts` und `last_update_seq` ist in
1800/1800 Fällen exakt im Raw-Stream vorhanden.

## 11. Hypothesenmatrix
`EVENT_TIME_VS_RECEIVE_TIME`, kombiniert mit der Wall-Clock-/Processing-As-Of-Grenze, ist PROVEN.
Timezone, First/Last allein, Source Gap, Feed-, Formel- und Tiefenunterschiede sind widerlegt.

## 12. Bewiesene Ursache
`ROOT_CAUSE = COMBINATION(EVENT_TIME_VS_RECEIVE_TIME, ASOF_BOUNDARY_DIFFERENCE)`.
Beide Ergebnisse sind intern gültig, aber besitzen verschiedene Sampling-Semantik.

## 13. Notwendige Codekorrekturen
Kein Fehler im OB200-Parser. Für kanonische Offline-Raw-Facts bleibt die kausale
Event-Time-End-of-Second-Regel bestehen. Der Audit ergänzt nur explizite Varianten;
bestehende DB-Rows werden nicht umgeschrieben.

## 14. Transition Contract
Vor `2026-08-24T22:47:54Z`: historische CH-Aggregatsemantik `ch_live_receive_asof_v1`.
Ab `2026-08-24T22:47:54Z`: vollständige Raw-Semantik `raw_ob200_event_time_eos_v1`.
Im Überlappungsbereich werden kanonische Research-Rows vollständig aus Raw neu aufgebaut;
keine Mischung innerhalb einer Row oder eines Buckets.

## 15. Full-Level-Coverage
Vollständige OB200-Level existieren ab `2026-08-24T22:47:54Z`. Davor besitzt das CH-Aggregat
nur Features/Proxies, keine vollständigen 200×2 Level.

## 16. Backfill-Gate
Phase 2 ist für den vorhandenen Raw-Dateibestand freigegeben, sofern jede Row
`source_semantics_version`, `source_id`, Coverage und `full_levels_available` trägt.
Der Zeitraum vor Raw-Beginn darf separat aus CH übernommen werden, nicht als full-level.

## 17. Tests
Siehe `test_report.json`; Phase-1- und Phase-1B-Tests sind Bestandteil des Gates.

## 18. Neue/geänderte Dateien
`research/btc_doge_research/seam_variants.py`,
`research/btc_doge_research/seam_root_cause_audit.py`,
`tests/research/test_btc_doge_research_phase1b.py` und dieser neue Ergebnisordner.

## 19. Offene Gaps
Die exakte historische Prozessbinary ist nicht archiviert. 1.796/1.800 Buckets sind allein
über gespeicherte `local_receive_ts` exakt rekonstruierbar; vier Grenzfälle hängen von der
nicht gespeicherten Event-Loop-Verarbeitungsreihenfolge relativ zum Wall-Timer ab. Alle
1.800 CH-Terminalevents sind per Event-Time und Update-ID im Raw-Stream vorhanden.
Dynamische Delta-Aktivitätsfelder sind nicht Teil des kanonischen Übergangsvergleichs.
Vor Raw-Beginn fehlen vollständige Level.

## 20. Empfehlung und Backfill-Antworten
1. Ja, CH-Aggregate vor Raw-Beginn dürfen mit `ch_live_receive_asof_v1` übernommen werden.
2. Raw gilt ab exakt `2026-08-24T22:47:54Z` als kanonisch.
3. Ja, der gesamte Raw-/CH-Überlappungsbereich wird einheitlich aus Raw neu aufgebaut.
4. `source_semantics_version` als nicht-null LowCardinality(String) je Fact-Row; zusätzlich
   Source-ID und Contractfelder gemäß `transition_contract.json`.
5. Backtests dürfen die Grenze nur explizit segmentiert/stratifiziert überschreiten; keine
   stillen Sub-BPS- oder Depth-Vergleiche über beide Semantiken.
6. Pool-/Wall-, Queue-, Leveldistanz-, Verbrauchs-, OFI- und vollständige Depth-Forschung
   benötigen Raw-Level.
7. Vor `2026-08-24T22:47:54Z` fehlen vollständige OB200-Level.
8. Phase 2 Full-History-Backfill: **PASS_WITH_TRANSITION_CONTRACT** für vorhandene Raw-Dateien
   plus separaten CH-Prefix; keine Behauptung vollständiger Levels im CH-Prefix.

ClickHouse writes: none
DDL executed: none
DML executed: none
Existing research DB rows changed: none
Invalid-timezone tables changed: none
orderbook_deltas repair attempts: none
Collector changes: none
Collector restarts: none
Dashboard changes: none
Live changes: none
Full-history backfill: not started
Persistent watcher: not started
Existing results modified: none
Commit: none
Push: none
