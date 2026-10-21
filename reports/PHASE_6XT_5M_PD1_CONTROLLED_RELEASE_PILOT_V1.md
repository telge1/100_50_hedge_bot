# PHASE 6XT – CONTROLLED 5M PD1 IMPORT & RELEASE PILOT V1

**VERDICT: `5M_PD1_PILOT_BLOCKED`**

Die 2592 Original-Snapshots sind bytegleich im freigegebenen Katalog, im Provenance-Index und in Staging, Katalog und Sidecar. Der Publish-Ledger der Ziel-Line enthält 50 Closes, die vor dem aggregierten Readback entstanden sind. Die übrigen 2542 Closes sind nicht veröffentlicht. Der Chart-Endpunkt hat keinen Timeframe-Parameter. Es wurde keine Viewer-Änderung, kein Dashboard-Neustart und kein Git-Commit ausgeführt.

## Ergebnis je Gate

| Gate | Ergebnis |
|---|---|
| 2592 Originale, Fingerprints, keine Lücken, kausal, Frozen `fe549309…` | PASS, vor dem Kopieren |
| 15m-Baseline 17568, Hash `4aeb36fe…` | PASS vor dem Kopieren und erneut im Readback |
| September-Regression 97 Preise, `phase5s_batch_reader_active` | PASS vor dem Kopieren und erneut nach dem Teil-Publish |
| Ziel-Line vor dem ersten Write leer | PASS |
| Bytekopie und SHA256 Quelle=Ziel | PASS, 2592/2592 |
| Provenance-Append, alte Einträge wertgleich, Coverage unverändert | PASS |
| `validate_batch` aller 2592 | PASS, 440098 Katalogzeilen |
| Staging + Katalog + Sidecar, Publish-Zähler am Ende 0 | PASS |
| Aggregierter Readback vor Publish | FAIL: Ledger enthielt bereits 50 Zeilen |
| Publish der übrigen 2542 | nicht ausgeführt |
| `POST /live-charts/viewer/api/window` für 5m PD1 | nicht ausführbar; Parameter fehlt; keine Codeänderung |

## Scope

- Symbol `DOGEUSDT`, Timeframe `5m`, Pool Detail `1`
- Sichtbar: `2026-09-16T00:00:00Z` bis `2026-09-17T23:55:00Z`, 576 Closes
- Kontext: `2026-09-09T00:00:00Z` bis `2026-09-15T23:55:00Z`, 2016 Closes
- Summe: 2592 Closes, 440098 ACTIVE-Pools
- Frozen-Input: `fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66`
- Katalog: `doge_apr_sep_2026_v1` unter `doge_apr_sep_2026_v1_history`
- Ziel-Line: `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`
- Combo: vorhandenes `StrategyCombo("5m", 1)` aus `ISOLATED_IMPORT_COMBOS`
- Batch-UUID: `c18994e7-77f7-4e74-a688-8a2ec5bbc059`
- Config-Hash, identisch zur 15m-PD1-Line: `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9`
- History-Policy aus dem Manifest: `ROLLING_4_CALENDAR_MONTHS`

Kein Generator lief. Dashboard `app.py` PID 1171055 blieb unverändert.

## Production-Guard vor der ersten Änderung

Alle 2592 Dateien im Staging vorhanden. Fingerprint, Symbol, Timeframe, PD1, Frozen-ID, Vier-Monats-Abstand, Bounds, `known_at` und Origin-Zeit gegen `history_from_close` und `decision_close` ohne Abweichung. Keine doppelten Pool-IDs. Zielordner `production/results/2026-09/5m_PD1/` existierte nicht.

15m-Line `historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1`: 17568 Closes, 17568 Snapshot-IDs, Hash wie oben.

`build_landscape_window("2026-09-14T21:45:00Z","2026-09-15T05:45:00Z")`: 97 Preise, 182 Pools, `phase5s_batch_reader_active=true`, Pipeline die 15m-PD1-Line.

5m-PD1-Meta und die Ziel-Line waren 0.

## Dateiübernahme

2592 Dateien neu nach:

`doge_apr_sep_2026_v1_history/production/results/2026-09/5m_PD1/`

Keine vorhandene Datei überschrieben. SHA256 jeder Quelle gleich SHA256 des Ziels. 0 Abweichungen. Keine LMV2-Neuberechnung.

Erster und letzter Hash:

- `20260909T000000Z.json` `b3326fae9bbf55a5c04e1f01b0d3329cac70d8a96beb44b1abd17810b1e642a4`
- `20260917T235500Z.json` `41bef59e3a563be641c19e9d35428b5166dd81aaccd6d0e82fbadd2aa6db7dd4`

Alle 2592 Paare: `logs/historical_import/overnight_2026/phase6xt_5m_pd1_controlled_release_pilot_v1/added_provenance_paths.json`

## Provenance

`production/provenance_lookup.json` nur um die 2592 neuen Schlüssel erweitert. `write_provenance_lookup` wurde nicht verwendet.

| | Wert |
|---|---|
| Einträge vorher | 53802 |
| Neu | 2592 |
| Danach | 56394 |
| Alte Werte abweichend | 0 |
| Lookup-SHA256 vorher | `2489777e4133428714f6c4b2b317faf1b013d11d5384ae716edffe8b29b0c4b2` |
| Lookup-SHA256 nachher | `185ddfc1ca31f9bc31fa51cf9609893aa0ffa1cfed90424e77c48b1de7c074b4` |
| Coverage-Matrix SHA256 | `17aedaa34a389dd7c9d3efd881deac5811323cfd16b0e19c3a0e7f467fba5a6b`, unverändert |

Sicherung vor dem Append: `logs/historical_import/overnight_2026/phase6xt_5m_pd1_controlled_release_pilot_v1/provenance_lookup_before.json`

`validate_batch` in 52 Batches à 50, letzter Batch 42: 2592 Closes, 440098 Katalogzeilen, keine Fehler.

## Import ohne Ledger

`import_historical_close` schreibt den Publish-Ledger im selben Aufruf. Der geforderte aggregierte Readback liegt davor. Deshalb wurden nur die vorhandenen Funktionen davor aufgerufen:

- `build_import_bundle`
- `validate_bundle`
- `write_active_snapshot_staged`
- `write_active_pool_catalog_from_json_rows`
- `verify_ch_catalog_identity`
- Sidecar `write_and_verify` und `require_before_publish`

`commit_publish_ledger` wurde von diesem Lauf nicht aufgerufen. Schreibgates nur in diesem Prozess: `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1` und `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`. Ein Host-Lock über `historical_import_write_lock`.

2592/2592: staged `written`, catalog `written`, sidecar `written`. Laufzeit 708,1 s. Letzte Prüfung im Lauf: Publish-Zähler der Ziel-Line 0. Ende `2026-10-21T08:12:02Z`.

Zieltabellen:

- `research_lmv2_active_snapshot_meta_v1`
- `research_lmv2_active_snapshot_member_v1`
- `research_lmv2_snapshot_active_pool_v1`
- `research_lmv2_snapshot_import_provenance_v1`

Keine 15m- oder 4h-Closes und keine andere PD-Kombination in diesem Lauf.

## Readback vor Publish

| Prüfung | Ist |
|---|---|
| Meta 5m PD1, `write_complete=1`, eindeutige Snapshot-IDs und Closes | 2592 / 2592 / 2592 |
| Spanne | `2026-09-09 00:00:00` – `2026-09-17 23:55:00` |
| Config-Hash-Abweichungen | 0 |
| Members | 440098 Zeilen, 2592 Snapshots |
| Katalog auf der Ziel-Line | 440098 Zeilen, 2592 Snapshots |
| Doppelte `(snapshot_id, pool_id)` in Members und Katalog | 0 |
| Mengendifferenz Members gegen Katalog | 0 / 0 |
| Bounds `bottom>0` und `top>=bottom` | 0 Verletzungen |
| `known_at` nach `decision_close` | 0 |
| Origin-Unix nach `decision_close` | 0 |
| JSON gegen Katalog: Pool-IDs, Side, Upper, Lower | 440098/440098 |
| Sidecar | 2592, eine Batch-UUID, Frozen `fe549309…` |
| 15m-PD1-Baseline | 17568, Hash unverändert |

Origin-IDs sind die Original-IDs. 427169 tragen `lldv2:DOGEUSDT:5m:micro:`, 12929 tragen `lldv2:DOGEUSDT:5m:normal:`. Beide Formen stehen so in den Original-JSON.

Andere Publish-Lines im Readback, unverändert gegenüber dem bekannten Bestand:

- `…_15mpd1_v1` 17568
- `…_15mpd5_v1` 17568
- `…_15mpd10_v1` 17568
- `…_4hpd10_v1` 1098
- `phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b` 2016

September-Regression danach erneut: 97 Preise, `phase5s_batch_reader_active=true`, Pipeline `…_15mpd1_v1`.

## Blocker: 50 Ledger-Zeilen vor dem Gate

Beim aggregierten Readback stand der Zähler der Ziel-Line auf 50, nicht auf 0.

Die 50 Zeilen:

- `decision_close` `2026-09-09 00:00:00` bis `2026-09-09 04:05:00` (genau die ersten 50 Closes, eine Batchgröße)
- `created_at` `2026-10-21 08:13:04.751` bis `2026-10-21 08:13:15.132` UTC
- `publish_complete=1`
- Config-Hash und Fingerprint stimmen mit der Meta-Zeile überein (0 Mismatches)
- History-Policy `ROLLING_4_CALENDAR_MONTHS`

Der Staging-Lauf war um 08:12:02 UTC mit Zähler 0 beendet. Die Ledger-Zeilen liegen danach. Zum Prüfzeitpunkt lief kein Importprozess mehr. Die übrigen 2542 Closes wurden nicht in den Ledger geschrieben.

Damit ist die Vorgabe „Readback vollständig, danach erst Publish, sonst STOP“ nicht erfüllt. Es wurde nicht weiter veröffentlicht und nichts aus dem Ledger gelöscht.

## Chart

`POST /live-charts/viewer/api/window` mit `DOGEUSDT`, `5m`, `PD1` und dem sichtbaren Fenster antwortet ohne Session mit HTTP 401 `Not authenticated`. Der Handler liest danach nur `from_utc` und `to_utc`. `timeframe` und `pool_detail` aus dem Body werden nicht gelesen.

`resolve_historical_pd_config` setzt `HISTORICAL_TIMEFRAME = "15m"` und die 15m-PD1-Line. `paneSupports` lässt nur `pane.tf === "15m"` zu. `causalDecisionCloseMs` rastert auf 15 Minuten. Der Request-Body des Density-Clients ist `{ from_utc, to_utc }`.

168h-Vorlauf, 576 sichtbare 5m-Closes, Origin-Sweep, 48h-Tile, Density-Buckets, Min-Depth, Farben und JS/Python-Parität auf der 5m-Line sind deshalb nicht gelaufen. Es wurde keine Chart-Komponente geändert.

## Was liegt jetzt vor

- 2592 Dateien im Produktionsordner `2026-09/5m_PD1`
- 2592 neue Provenance-Pfade
- 2592 Meta-Zeilen, 440098 Members, 440098 Katalogzeilen, 2592 Sidecar-Zeilen auf `…_5mpd1_v1`
- 50 Ledger-Zeilen auf derselben Line
- 2542 Closes gestaged und katalogisiert, nicht veröffentlicht
- 15m-Baseline und die 97-Preis-Regression weiterhin PASS

## Nicht ausgeführt

Kein Publish der restlichen 2542. Kein Rollback der 50 Ledger-Zeilen. Keine Änderung an Coverage, Dichteformel, Canvas, Slider oder 15m-Lines. Kein Dashboard-Neustart. Kein Git-Commit und kein Push.
