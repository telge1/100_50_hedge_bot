# PHASE 6XT – 15M PD1 / PD5 / PD10 HISTORICAL DENSITY READINESS AUDIT V1

**Date:** 2026-10-21  
**Agent:** 2 (read-only)  
**VERDICT:** **`15M_MULTI_PD_READY_FOR_CONTROLLED_PREPARATION`**

DOGEUSDT 15m PD1, PD5 und PD10 liegen als Original-LMV2-JSON, im Catalog-Root, in der Provenance-Lookup und als veröffentlichte ClickHouse-Lines vollständig auf dem 15m-Raster 2026-04-01 00:00Z bis 2026-09-30 23:45Z. Fehlende Closes: **0 / 0 / 0**. Die Immutable-Inputs decken die tatsächlichen `candle_load_start`-Zeiten. Ein neuer Generatorlauf und ein neuer Import sind für diese Anzeige nicht erforderlich.

Was für die Anzeige im bestehenden Historical Research Chart fehlt, ist die **Viewer-Freigabe** von `("15m", 5)` und `("15m", 10)`. Die Publish-Lines sind im Contract bereits abgelegt. `resolve_viewer_historical_scope` und die Checkbox-Matrix lassen sie nicht durch.

Referenz bleibt die funktionierende 15m-PD1-Kette. Kein neues Density-System. Keine Umsetzung in diesem Auftrag.

Evidence (neu, nichts überschrieben):

- `logs/historical_import/overnight_2026/phase6xt_15m_multi_pd_readiness_audit_v1/clickhouse_inventory.json`
- `logs/historical_import/overnight_2026/phase6xt_15m_multi_pd_readiness_audit_v1/file_inventory.json`

---

## 1. Ist-Zustand je PD

| | 15m PD1 | 15m PD5 | 15m PD10 |
|---|---:|---:|---:|
| Publish-Line | `historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1` | `…_15mpd5_v1` | `…_15mpd10_v1` |
| `config_hash` | `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9` | `533e04c7cd0e1c93540ed9466a2a5a2eb626f82b828b9a45d1da234703ebd03c` | `bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2` |
| Original-JSON im Catalog-Root | 17568 | 17568 | 17568 |
| Publish `publish_complete=1` | 17568 | 17568 | 17568 |
| Bereich | 2026-04-01 00:00Z – 2026-09-30 23:45Z | gleich | gleich |
| Fehlende / doppelte Closes | 0 / 0 | 0 / 0 | 0 / 0 |
| Viewer-Scope | freigegeben | von der Window-API abgelehnt | von der Window-API abgelehnt |
| Checkbox auf 15m-Pane | aktiv | disabled | disabled |

PD1 ist die laufende Referenz. PD5 und PD10 sind daten- und SSOT-seitig auf demselben Stand und chartseitig gesperrt.

Zusätzliche, vom Viewer nicht verwendete Line: `historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1`, 10 Closes, 2026-08-01 00:00Z bis 2026-08-07 06:45Z, derselbe PD10-`config_hash`. Dieselben 10 `snapshot_id` und dieselben 10 Decision-Closes stehen auch auf der Produktions-Line. Die Catalog-Zeilen dieser 10 Snapshots (2750 Pools) tragen die Pilot-Line-ID. Der Reader lädt den Catalog nach `snapshot_id`, deshalb bleiben diese August-Closes sichtbar. Das September-Fenster ist davon nicht betroffen.

---

## 2. Originaldaten-Inventar

Catalog-Root, heute die SSOT-Dateien:

`backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history/production/results/{month}/15m_PD{1,5,10}/`

Raster je Monat, für alle drei Combos identisch, Stem-Menge gegen das 15m-UTC-Raster geprüft (nicht nur die Anzahl):

| Monat | Erwartet | PD1 | PD5 | PD10 | Missing | Extra | Duplikat-Stems |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-04 | 2880 | 2880 | 2880 | 2880 | 0 | 0 | 0 |
| 2026-05 | 2976 | 2976 | 2976 | 2976 | 0 | 0 | 0 |
| 2026-06 | 2880 | 2880 | 2880 | 2880 | 0 | 0 | 0 |
| 2026-07 | 2976 | 2976 | 2976 | 2976 | 0 | 0 | 0 |
| 2026-08 | 2976 | 2976 | 2976 | 2976 | 0 | 0 | 0 |
| 2026-09 | 2880 | 2880 | 2880 | 2880 | 0 | 0 | 0 |
| Summe | **17568** | **17568** | **17568** | **17568** | **0** | **0** | **0** |

`coverage_matrix.json` (`validated_at` 2026-10-10) sagt dasselbe. Die Zählung oben ist ein neuer Dateiscan vom 2026-10-21.

Header aller 52704 Dateien (Prefix bis `"pools"`):

- `decision_close` = Dateiname
- `history_from_close` = `rolling_history_from_close(decision, 4)` auf allen Dateien
- `pool_detail` 1 / 5 / 10 passend zum Ordner
- `candle_load_start` = `history_from_close` minus Preset-Warmup
- Frozen-`snapshot_id` nur die drei autorisierten IDs
- keine Origin-Zeit vor `candle_load_start`
- keine Origin-Zeit nach `decision_close`

`config_hash` steht nicht im JSON. Er steht auf jeder Publish-Zeile und entspricht dem Phase-5V-Preset-Hash. `engine_input_hash` ist pro Close eindeutig (17568 verschiedene je PD), weil das Engine-Dokument den Close enthält. `active_state_fingerprint` wiederholt sich, wenn sich das ACTIVE-Set zwischen zwei Closes nicht ändert (PD1: 12173 verschiedene Fingerprints, 3824 davon auf mehr als einer Datei; PD5: 13976 / 2743; PD10: 16551 / 969). Das sind keine doppelten Closes.

### Frozen-Input-Mix in den Original-JSONs

Identisch zur Sidecar-Zählung:

| Frozen ID | Start | Ende exklusiv | PD1 | PD5 | PD10 |
|---|---|---|---:|---:|---:|
| `84c22453…` apr-sep | 2025-09-30 20:00Z | 2026-10-01 00:00Z | 4459 | 2941 | 1327 |
| `cfae2571…` multi-month | 2025-12-11 00:00Z | 2026-09-01 00:00Z | 10229 | 11747 | 13361 |
| `fe549309…` september | 2026-02-28 20:00Z | 2026-10-01 00:00Z | 2880 | 2880 | 2880 |

Parquet und Manifest aller drei IDs sind auf der Platte. `fe549309…` ist genau der September (2880). PD1 braucht den langen Snapshot `84c22453…` für mehr frühe Closes als PD10, weil das PD1-Warmup länger ist.

### Weitere Ablagen (nicht der Chart-Eingang)

| Ort | PD1 | PD5 | PD10 | Rolle |
|---|---:|---:|---:|---|
| `doge_multi_month_2026_history/production/results` | 10229 | 11747 | 13361 | Quelle der `cfae2571…`-Closes, Teilmenge |
| `doge_september_2026_history/production/results` | 2880 | 2880 | 2880 | September-Originale |
| `_controlled_import_progress*` | 308 | 308 | 308 | Import-Fortschritt, keine SSOT |
| `phase11o_single_worker_benchmark/.../pools` | 30 | 30 | 30 | Benchmark |

Ein eigenes Historical-Staging, das noch in den Catalog-Root müsste, gibt es für 15m PD5/PD10 nicht. Anders als bei 5m liegen die Dateien bereits unter `production/results`.

### Provenance

| Artefakt | PD1 | PD5 | PD10 |
|---|---:|---:|---:|
| `provenance_lookup.json` → `entries` | 17568 | 17568 | 17568 |
| CH-Sidecar, distinct `snapshot_id` | 17568 | 17568 | 17568 |
| `provenance_index.json` `entries` | 13109 | 14627 | 16241 |

Die Lookup-Datei hat zusätzlich 4h PD10 (1098) und die von der 5m-Arbeit registrierten 5m-PD1/PD5/PD10-Fenster (je 2592). `entry_count` = 61578. Diese Datei nicht anfassen.

Der Index ist das Copy-Log für `cfae2571…` + `fe549309…` (PD1: 10229+2880=13109, analog PD5/PD10). Die `84c22453…`-Dateien stehen in Lookup und Sidecar, nicht in diesem Copy-Log. Für den Chart-Pfad zählt Sidecar + Lookup. Beide sind vollständig. `copied_count` im Index ist 52430 und enthält die späteren 5m-Registrierungen.

---

## 3. CH- / Publish-Line-Inventar

Nur `SELECT` / `DESCRIBE` gegen `signal_generator` auf `127.0.0.1`. Kein Write.

Produktions-Lines, `FINAL`, `publish_complete=1`:

| Line | Rows | Distinct Closes | Distinct Snapshots | Hash | Missing vs 15m-Raster | Extra | Duplikat-Closes | Duplikat-Snapshots |
|---|---:|---:|---:|---|---:|---:|---:|---:|
| `…_15mpd1_v1` | 17568 | 17568 | 17568 | PD1-Hash, 1 Wert | 0 | 0 | 0 | 0 |
| `…_15mpd5_v1` | 17568 | 17568 | 17568 | PD5-Hash, 1 Wert | 0 | 0 | 0 | 0 |
| `…_15mpd10_v1` | 17568 | 17568 | 17568 | PD10-Hash, 1 Wert | 0 | 0 | 0 | 0 |

Monatszählung auf allen drei Lines: 2880 / 2976 / 2880 / 2976 / 2976 / 2880.

`history_policy` auf allen Zeilen: `ROLLING_4_CALENDAR_MONTHS`. `history_from_close` von 2025-12-01 00:00Z bis 2026-05-30 23:45Z.

Schichten, gezählt über die Publish-`snapshot_id`s:

| | PD1 | PD5 | PD10 |
|---|---:|---:|---:|
| Meta, `write_complete=1` | 17568 | 17568 | 17568 |
| Member-Snapshots | 17568 | 17568 | 17568 |
| Member-Zeilen | 3535199 | 3415237 | 3917234 |
| Catalog-Zeilen derselben Snapshot-IDs | 3535199 | 3415237 | 3917234 |
| Catalog-Zeilen mit Produktions-`pipeline_line_id` | 3535199 | 3415237 | 3914484 |
| Sidecar-Zeilen / distinct Snapshots | 17568 / 17568 | 17568 / 17568 | 17568 / 17568 |

Die 2750 Catalog-Zeilen Differenz bei PD10 sind die 10 gemeinsamen August-Pilot-Snapshots (Catalog trägt dort die Pilot-Line-ID). Member- und Catalog-Zählung über `snapshot_id` sind gleich. Kein Snapshot der drei Produktions-Lines wird von einer anderen der drei Lines geteilt.

Catalog der Produktions-Lines, volle Korpora: `known_at` nach `decision_close` = 0, `invalidated_at` gesetzt = 0, `bottom_price >= top_price` = 0. Dieselben drei Zähler auf der August-Pilot-Line: 0.

Früheste `known_at` im Catalog (ältere noch ACTIVE Origins):

| PD | frühestes `known_at` | spätestes `known_at` |
|---|---|---|
| PD1 | 2025-10-26 22:15Z | 2026-09-30 23:30Z |
| PD5 | 2025-11-11 01:30Z | 2026-09-30 23:45Z |
| PD10 | 2026-01-06 14:30Z | 2026-09-30 23:45Z |

JSON-Origin-Minima liegen daneben (PD1 2025-10-26 22:00Z, PD5 2025-11-11 01:15Z, PD10 2026-01-06 14:15Z). Das sind Origins aus dem Warmup vor `history_from_close` und nach `candle_load_start`. Dateien mit mindestens einer solchen Origin: PD1 12235, PD5 7628, PD10 3344. Keine davon liegt vor `candle_load_start`.

---

## 4. Coverage und fehlende Closes

15m-Raster: Close alle 900 Sekunden, letzter Close eines Tages 23:45Z.

Sichtfenster 2026-09-16 00:00Z bis 2026-09-17 23:45Z inklusive:

- Dauer 47h 45m
- **15m-Closes: 192** (2 × 96)
- 5m-Preispunkte, die `build_landscape_window` bei genau diesem `to_utc` zählt: `(171900 // 300) + 1` = **574**

Mitgliedschafts-Vorlauf ab 2026-09-09 00:00Z ist `vis_from - 168h` (`WARMUP_HOURS = 168`):

- Vorlauf allein, 2026-09-09 00:00Z bis 2026-09-15 23:45Z: **672** Closes
- Vorlauf plus Sichtfenster bis 2026-09-17 23:45Z: **864** Closes

Dateien und Publish-Closes in beiden Fenstern: 192 und 864 auf PD1, PD5 und PD10. Missing 0.

Das Sichtfenster liegt unter dem 72h-Exportlimit.

Stichprobe voll geparster JSONs (2026-04-01, 2026-09-09, 2026-09-16, 2026-09-17 23:45Z, 2026-09-30 23:45Z), alle drei PDs:

- `timeframe=15m`, `active_pool_ids` = Pool-Menge
- `invalidated_at` leer, `known_at` nicht nach dem Close
- im September-Fenster Origins älter als 168h (typisch ab etwa 2026-05-14, also im Vier-Monats-Kontext)
- `candle_load_start` trifft die Warmup-Formel auf die Minute

Fehlende Closes für eine Anzeige von Apr–Sep, und für das Pilotfenster inklusive 168h: **keine**.

---

## 5. Input-Coverage-Risiken

Die 4h-PD1-Lücke wird hier nicht übernommen. Dort zieht `warmup_bars × 4h` plus 240 1m-Quellbars den Bedarf auf 2024-04-20. Für 15m ist `expected_source_bars("1m","15m")` = **15 Minuten**.

Preset (`trading_research_platform/indicators/live_micro_lld_v2/presets.py`), Warmup = `volume_lookback + swing_len + 64`:

| PD | Preset | Warmup-Bars | Dauer auf 15m | Frühester `candle_load_start` | 1m-Bedarf davor |
|---|---|---:|---|---|---|
| PD1 | swing 30, vol 3500 | 3594 | 37d 10h 30m | 2025-10-24 13:30Z | 2025-10-24 13:15Z |
| PD5 | swing 12, vol 2000 | 2076 | 21d 15h 00m | 2025-11-09 09:00Z | 2025-11-09 08:45Z |
| PD10 | swing 2, vol 300 | 366 | 3d 19h 30m | 2025-11-27 04:30Z | 2025-11-27 04:15Z |

`84c22453…` beginnt 2025-09-30 20:00Z. Der engste Fall ist PD1, mit etwa 23 Tagen 17 Stunden Reserve. PD5 und PD10 brauchen weniger Vorgeschichte als PD1 und liegen in Snapshots, mit denen PD1 bereits erzeugt und veröffentlicht wurde.

Join Publish-`candle_load_start` gegen Sidecar-`frozen_input_id`, alle 17568 Closes je PD: 0 Zeilen, deren 1m-Bedarf vor dem Range-Start des zugewiesenen Snapshots liegt, und 0 Zeilen nach dem Range-Ende.

Die Original-JSONs fehlen nicht. Ein Nachgenerieren ist nicht nötig und würde keine neuen 1m-Daten verlangen, wenn es trotzdem verlangt würde. Dieser Audit hat keinen Generator gestartet.

---

## 6. Contract- / Viewer-Gaps

Kanonische Laufzeit laut den 5m-Berichten: `spread_recovery_hedge_short_dev/dashboard`. Gelesen, nicht geändert.

Datenweg, der für PD5/PD10 schon bis zum Ledger reicht:

```
Original-LMV2-JSON
  → ACTIVE (meta write_complete=1, members)
  → Catalog research_lmv2_snapshot_active_pool_v1
  → Provenance-Sidecar
  → Publish-Ledger publish_complete=1
  → POST /live-charts/viewer/api/window
  → resolve_viewer_historical_scope   ← hier Stopp für 15m PD5/PD10
  → build_landscape_window (168h, 5m-Preise, Slot = pane-TF)
  → Origin-Sweep, 48h-Tiles, Canvas
```

`StrategyCombo("15m", 5|10).pipeline_line_id()` ist die bestehende Bulk-Allowlist (`STRATEGY_COMBOS` in beiden Dashboard-Bäumen). `ISOLATED_IMPORT_COMBOS` sind nur die 5m-Combos. Für 15m PD5/PD10 ist keine neue Importer-Allowlist nötig.

Spread-Contract `historical_lmv2_pd_contract_v1.py`:

- `_PD_REGISTRY[5|10]` zeigt bereits auf `…_15mpd5_v1` / `…_15mpd10_v1`
- `resolve_historical_pd_config(5|10, timeframe="15m")` liefert diese Lines
- `ALLOWED_VIEWER_SCOPES` ist heute `{("15m", 1), ("5m", 1), ("5m", 5), ("5m", 10)}`
- `("15m", 5)` und `("15m", 10)` werfen `INVALID_SCOPE`, bevor die Line benutzt wird

Window-API liest `timeframe` und `pool_detail` und ruft `resolve_viewer_historical_scope`. Slotbreite kommt von `membership_slot_minutes(timeframe)`: 15 für 15m, unabhängig vom PD. Der Adapter macht dasselbe (`membershipSlotMinutesForTimeframe`: alles außer `5m` → 15). Timeframe der Anzeige bleibt `pane.tf`.

Checkboxen existieren (`researchExactOriginPoolDetail` 1/5/10). HTML setzt PD5/PD10 initial `disabled`. `syncPoolDetailUi` schaltet nach `RELEASED_VIEWER_SCOPES` × `pane.tf`. Auf der 15m-Pane bleiben PD5/PD10 aus. Dieselbe Funktion gibt sie auf der 5m-Pane frei, weil 5m PD5/PD10 in der Matrix stehen.

Cache- und Inflight-Keys sind schon pro Scope (`timeframe + pool_detail`), Tiles ebenfalls. Multi-PD-Komposition (eigener Fetch je PD, Sweep je PD, danach `concat`) ist der 5m-Stand und gilt für jedes freigegebene Paar. Gleichzeitige 15m-PD-Auswahl braucht keine zweite Density-Rechnung, sobald die zwei Scopes in beiden Matrizen stehen.

Schattenpfad im Pools-Tree: `pd1_landscape_viewer_v1/historical_window_v1.py` importiert `ALLOWED_VIEWER_SCOPES`, die Pools-Kopie von `historical_lmv2_pd_contract_v1.py` exportiert das Symbol nicht. Die Spread-Kopie tut es. Vor einer Aktivierung muss die laufende Instanz die Spread-Module laden. Diese Dateien wurden hier nicht angeglichen.

---

## 7. Vergleich mit 15m PD1 und Gap-Matrix

PD5 und PD10 folgen demselben Import, derselben History-Policy, demselben Catalog-Root und demselben Fenster-Builder wie PD1. Unterschiede, die schon in den Daten stehen: Preset, `config_hash`, Warmup-Länge, Frozen-Mix, Poolzahlen. Der einzige Unterschied auf dem Weg zur Anzeige ist die Release-Matrix.

Zustände: `VORHANDEN` `VALIDIERT` `FEHLEND` `NICHT_VERIFIZIERT`.

| Prüfung | 15m PD1 | 15m PD5 | 15m PD10 |
|---|---|---|---|
| Original-JSON verfügbar | VALIDIERT | VALIDIERT | VALIDIERT |
| Immutable-Coverage ausreichend | VALIDIERT | VALIDIERT | VALIDIERT |
| Production-Datei registriert (Catalog-Root + Lookup) | VALIDIERT | VALIDIERT | VALIDIERT |
| Provenance vollständig (Lookup + CH-Sidecar) | VALIDIERT | VALIDIERT | VALIDIERT |
| CH Catalog/Sidecar vorhanden | VALIDIERT | VALIDIERT | VALIDIERT |
| Publish-Line vollständig | VALIDIERT | VALIDIERT | VALIDIERT |
| Line-Mapping im Contract | VALIDIERT | VORHANDEN | VORHANDEN |
| Viewer-Scope / Window-API | VALIDIERT | FEHLEND | FEHLEND |
| Checkbox freigeschaltet | VALIDIERT | FEHLEND | FEHLEND |
| 168h-Kontext im Pilotfenster | VALIDIERT | VALIDIERT | VALIDIERT |
| Original ACTIVE inkl. älterer Origins | VALIDIERT | VALIDIERT | VALIDIERT |
| SSOT-Parity JSON↔Publish | VALIDIERT (Phase 4Z, 2026-10-12, `parity_failures=0`; heute Zähler, Raster, Hashes und Schichten erneut gelesen, kein neuer Voll-Readback) | gleich | gleich |
| Viewer-Density-Parity (Window + Sweep + Canvas) | VALIDIERT in früheren 15m-Regressionen; in diesem Audit nicht erneut ausgeführt | NICHT_VERIFIZIERT | NICHT_VERIFIZIERT |

Der Copy-Index ist für die `84c22453…`-Dateien absichtlich kürzer als die Lookup. Das ist kein fehlender Sidecar.

---

## 8. Minimaler Umsetzungsplan

Der 5m-Importablauf (Datei-Registration → isolierter CH-Pilot → Restimport) ist hier der falsche nächste Schritt. Die Closes sind veröffentlicht. Ein zweiter Import derselben Fingerprints läuft auf `PublishConflictError`.

Fehlende Closes: **PD5 = 0, PD10 = 0, PD1 = 0.**

Kleinster Weg, nachdem Agent 1 die 5m-Dateien losgelassen hat:

1. In der Spread-Quelle `ALLOWED_VIEWER_SCOPES` und `RELEASED_VIEWER_SCOPES` um `("15m", 5)` und `("15m", 10)` erweitern. Line-IDs nicht neu vergeben.
2. Tests, die 15m PD5 heute als `INVALID_SCOPE` erwarten, auf die neue Matrix nachziehen. 5m-Scopes nicht zurücknehmen.
3. Read-only Window-Readback auf dem Referenzfenster, ohne Publish: PD1, PD5, PD10 einzeln. Erwartung: Line `…_15mpd{1,5,10}_v1`, Slot 15, Kontext ab 2026-09-09 00:00Z, 192 Mitgliedschafts-Closes, 574 5m-Preise bei `to_utc=2026-09-17T23:45:00Z`. PD1-Hash unverändert.
4. Dieselbe Prüfung für die nichtleeren Checkbox-Kombinationen. Komposition bleibt der vorhandene Pro-PD-Sweep plus `concat`.
5. Die bestehende 15m-PD1-Regression (2026-09-14 21:45Z–2026-09-15 05:45Z, 97 Preise) mitlaufen lassen.
6. Browser-Sichtprüfung erst nach separater Freigabe eines Reloads der kanonischen Spread-Instanz.

Kein neuer Import, kein Generator, keine neuen 1m-Daten, keine zweite Pipeline.

---

## 9. Konkreter erster Freigabeschritt

Freigabe nur für die zwei Scope-Einträge in der Spread-Quelle, und nur wenn Agent 1 `historical_lmv2_pd_contract_v1.py` und `pd1_exact_origin_density_research_v1.js` nicht mehr schreibt:

- `ALLOWED_VIEWER_SCOPES` += `("15m", 5)`, `("15m", 10)`
- `RELEASED_VIEWER_SCOPES` += dieselben Paare
- Tests der Release-Matrix entsprechend

In diesem Schritt keine ClickHouse-Writes, keine Publishes, keine Imports, kein Dashboard-Restart, kein Anfassen von `provenance_lookup.json` / `provenance_index.json`.

---

## 10. Risiken für parallele Agenten

Agent 1 arbeitet an der 5m-Viewer-Integration in genau den Dateien, die Schritt 9 ändern würde: Spread-Contract, Exact-Origin-JS, die zugehörigen Tests. Ein gleichzeitiger Edit vermischt 5m- und 15m-Freigaben.

Weitere gemeinsame Artefakte, die dieser Audit nur gelesen hat:

- `provenance_lookup.json` und `provenance_index.json` enthalten bereits die 5m-Registrierungen vom 2026-10-21
- ClickHouse-Lines `…_15mpd1_v1` (Referenz) und die 5m-Lines
- Pools-Schattenkopie von Contract und Window-Builder, derzeit nicht symbolgleich mit Spread
- Laufende Dashboard-Prozesse auf der kanonischen Spread-Instanz

Dieser Audit hat keinen dieser Zustände geschrieben, außer den zwei neuen Evidence-Dateien unter `phase6xt_15m_multi_pd_readiness_audit_v1/` und diesem Bericht.

---

**VERDICT: `15M_MULTI_PD_READY_FOR_CONTROLLED_PREPARATION`**

Die Vorbereitung ist die Viewer-Scope-Freigabe. Sie ist nicht durch fehlende JSONs, fehlende 1m-Coverage oder fehlende Publish-Closes blockiert.

**STOP.** Keine Umsetzung ohne gesonderte Freigabe.
