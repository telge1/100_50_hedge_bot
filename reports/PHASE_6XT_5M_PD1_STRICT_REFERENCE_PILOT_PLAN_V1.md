# PHASE 6XT – 5M PD1 STRICT REFERENCE PILOT PLAN V1

**VERDICT: `READY_FOR_CONTROLLED_5M_PD1_IMPORT_APPROVAL`**

Read-only. Kein ClickHouse-Write, kein Publish, keine Dateiverschiebung, kein Generator-, Dashboard- oder Git-Eingriff.

Architektur bleibt die 15m-PD1-Kette aus `reports/PHASE_6XT_5M_STRICT_15M_REFERENCE_TRACE_V1.md`. Der Chart liest Staging nicht. Es wird keine zweite Importpipeline gebaut.

Importer, der zu verwenden ist: `spread_recovery_hedge_short_dev/dashboard`. Dort existiert `StrategyCombo("5m", 1)` in `ISOLATED_IMPORT_COMBOS`. Der Bulk-Satz `STRATEGY_COMBOS` bleibt ohne 5m.

## Pilotfenster

Sichtbar, und genau ein 48h-UTC-Tile der bestehenden Funktion `planExactOriginFixedTiles`:

| | UTC |
|---|---|
| Von | 2026-09-16 00:00:00 |
| Bis | 2026-09-17 23:55:00 |
| Closes | 576 |
| Raster | 300 Sekunden |

2026-09-16 00:00:00 liegt auf dem Unix-48h-Raster (`timestamp % 172800 == 0`). Das Tile endet inklusive beim letzten 5m-Schritt, 23:55. Das ist dieselbe Tile-Mathematik wie bei 15m, nicht ein neues Raster.

Importumfang, weil `build_landscape_window` `WARMUP_HOURS = 168` Mitgliedschaft vor dem sichtbaren Anfang lädt:

| | UTC | Closes |
|---|---|---:|
| Kontext | 2026-09-09 00:00:00 – 2026-09-15 23:55:00 | 2016 |
| Sichtbar | 2026-09-16 00:00:00 – 2026-09-17 23:55:00 | 576 |
| Summe | 2026-09-09 00:00:00 – 2026-09-17 23:55:00 | 2592 |

Ohne die 2016 Kontext-Closes schneidet der vorhandene Builder den linken Rand auf `data_lo` ab. Das wäre nicht dasselbe Fensterverhalten wie 15m.

Alle 2592 Dateien liegen vor. Quelle, unverändert:

`doge_multi_month_2026_history/staging/phase6xt_5m_pd1_jan_sep_v1/results/2026-09/5m_PD1/`

## Originaldaten, geprüft

Jede der 2592 Dateien, voll geparst:

| Prüfung | Ergebnis |
|---|---|
| Datei vorhanden, ein Close pro Stem | 2592/2592, keine Lücke, keine doppelten Stems |
| `symbol` / `timeframe` / `pool_detail` | DOGEUSDT / 5m / 1 |
| `active_state_fingerprint` | stimmt mit `active_state_fingerprint(ids, sides)` überein, 0 Abweichungen |
| ACTIVE-IDs | eindeutig, Menge gleich `pools`, `active_sides` gleich lang |
| Preisgrenzen | `bottom_price < top_price` auf allen 440098 Pools |
| Zukunft | kein `known_at` und keine Origin-Zeit nach `decision_close` |
| Vier Monate | `history_from_close` liegt bei jedem Close vier Kalendermonate davor |
| Ältere noch aktive Origins | früheste Origin im Umfang 2026-06-02 00:05 UTC, also vor den September-Closes und nach dem jeweiligen `history_from_close`. Keine Origin vor `history_from_close` |
| Frozen-Input | ausschließlich `fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66`, bereits in `authorized_frozen_input_ids` des freigegebenen Katalogs |
| `engine_input_hash` | 2592 verschiedene Werte, einer pro Close. Das ist der per-Close-Input-Hash, nicht der PD-Preset-Hash |

PD-Preset-Hash, den der Importer setzt und den die 15m-PD1-Line heute trägt:

`4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9`

Catalog-Zeilen, die derselbe Mapper `catalog_rows_from_payload` schreiben würde: **440098** (sichtbares Tile allein 95541).

## 15m-Referenz, vor jedem Write festgehalten

Gelesen aus `research_lmv2_active_snapshot_publish_v1` mit `publish_complete = 1`. Nach dem Piloten müssen diese Werte gleich sein.

| Feld | Wert |
|---|---|
| Line | `historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1` |
| Closes | 17568 |
| Snapshot-IDs | 17568, eindeutig |
| Bereich | 2026-04-01 00:00 – 2026-09-30 23:45 |
| `config_hash` | `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9` |
| Symbol / TF / PD | DOGEUSDT / 15m / 1 |
| Stichprobe 2026-09-14 21:45 | Snapshot `3c6bacc268daa59b24f239fb3ad804cb4dc45a53a1b6a92536055f66d7d0dc4c` |
| Fingerprint dieser Zeile | `71cc7b3ee8f4f0ea7e802fab08eead62c96d30d6a567f5d4e5365b960e9415a0` |
| Prefix-Candle-Fingerprint | `4da21e346a9b4b014bbeb97594611423` |

## Gate A

Vorhanden, nicht veröffentlicht.

| Konstante | Wert |
|---|---|
| Line | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` |
| Combo | `5m_PD1` |
| Monat / Stem | `2026-05` / `20260501T000000Z` |
| Batch-ID | `phase6xt_5m_pd1_gate_a_v1` |
| Katalogname | `doge_multi_month_2026_history` |

Zustand jetzt:

- Publish-Zeilen dieser Line: **0**
- Meta-Zeilen DOGEUSDT 5m PD1: **0**
- Catalog-Zeilen DOGEUSDT 5m PD1: **0**
- Provenance-Zeilen dieser Line: **0**

Der Gate-A-Katalogroot hat kein `production/catalog_manifest.json`. `load_manifest_context` würde dort stoppen. Der 15m-Import läuft gegen `doge_apr_sep_2026_v1_history` (`catalog_id` `doge_apr_sep_2026_v1`, `data_release_status` `RELEASED`).

Der Pilot benutzt deshalb:

- dieselbe Line wie Gate A, `StrategyCombo("5m", 1).pipeline_line_id()`
- den Katalogroot, den 15m wirklich benutzt
- eine neue Batch-UUID, nicht `phase6xt_5m_pd1_gate_a_v1`
- nicht den Mai-Close von Gate A

Die Line ist leer. Es gibt keinen Teilzustand, der vor dem September-Pilot gelöscht werden müsste.

## Importkette, vorhandene Funktionen

Nur diese, in dieser Reihenfolge, aus dem Spread-Dashboard:

1. `stem_path` / `build_import_bundle` / `validate_bundle` (`validate_batch` ist der Schreib-freie Test derselben Checks)
2. `assert_productive_env_gates` — `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1` und `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`
3. `execute_batch_import`
4. `import_historical_close`: `write_active_snapshot_staged` → `write_active_pool_catalog_from_json_rows` → Provenance-Sidecar → `commit_publish_ledger`
5. `run_batch_readback_gate` mit `require_sidecar=True`

Tabellen, keine neuen:

| Stufe | Tabelle | Erwartete Zeilen nur auf der 5m-Line |
|---|---|---:|
| staged meta | `research_lmv2_active_snapshot_meta_v1` | 2592 |
| staged members | `research_lmv2_active_snapshot_member_v1` | 440098 |
| Catalog | `research_lmv2_snapshot_active_pool_v1` | 440098 |
| Sidecar | `research_lmv2_snapshot_import_provenance_v1` | 2592 |
| Publish | `research_lmv2_active_snapshot_publish_v1` | 2592 |

`publish_complete = 1` nur für `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`.  
`timeframe = 5m`, `pool_detail = 1`, `config_hash` wie oben.  
15m-Line: 0 neue Zeilen.

Batchgröße der 15m-Kette: `DEFAULT_BATCH_SIZE_CLOSES = 50`. Das sind 51 Batches à 50 und ein Batch à 42, chronologisch, 52 `BatchSpec`s. `combo_folder = "5m_PD1"`. `pipeline_line_id` wie oben.

Idempotenz, schon im Code: `resolve_existing_publish` überspringt denselben Snapshot, wenn Snapshot-ID, Active-Fingerprint und Prefix-Candle-Fingerprint gleich sind. Ein anderer Fingerprint auf demselben Close wirft `PublishConflictError` und stoppt. Kein stilles Überschreiben.

## Was vor dem ersten Write noch fehlt

Der freigegebene Katalog liest nur

`doge_apr_sep_2026_v1_history/production/results/{month}/{combo}/{stem}.json`

Dort gibt es kein `2026-09/5m_PD1`. `validate_provenance` verlangt den relativen Pfad in `production/provenance_lookup.json` mit Frozen-ID und SHA-256. Die 2592 Staging-Pfade stehen dort nicht.

Das sind dieselben Release-Schritte wie bei 15m, noch nicht ausgeführt:

1. Die 2592 JSON bytegleich unter den `stem_path` legen. Kein Umschreiben der Poolkörper. 15m-Dateien nicht anfassen.
2. Nur für diese relativen Pfade Provenance-Einträge anhängen. Bestehende 15m-Keys bleiben bytegleich.
3. Keine Coverage-Zeile mit `MISSING != 0` in `production/coverage_matrix.json`. `discover_mixed_frozen_catalog` bricht den gesamten Katalog ab, sobald irgendeine Zeile `MISSING != 0` hat. Eine halbe 5m-Zeile würde die 15m-Validierung mit abschalten. `assert_mixed_coverage` wird vom Importer heute nicht aufgerufen. Die Matrix bleibt für diesen Piloten unverändert.

## Freigabepunkt vor dem ersten Write

Dieser Bericht ist die Freigabe nicht.

Der erste Write, gleich ob Dateiablage, Provenance-Anhang oder ClickHouse, startet erst nach einer gesonderten Freigabe, die alle folgenden Punkte nennt:

1. Die 15m-Baseline oben ist unmittelbar davor erneut gelesen und identisch.
2. Write-Scope ist nur die 2592 Stems 2026-09-09 00:00:00Z bis 2026-09-17 23:55:00Z, Combo `5m_PD1`, Line `…_5mpd1_v1`.
3. Gate-A-Batch-ID und der Mai-Close werden nicht importiert.
4. `STRATEGY_COMBOS` und die 15m-Window-Funktion werden in diesem Schritt nicht geändert.
5. Nach jedem Batch `run_batch_readback_gate`. Bei Fehler Stop.
6. Nach dem letzten Batch die 15m-Baseline erneut. Jede Abweichung stoppt und löst den Rollback aus.

## Viewer, nach bestandenem Readback

Noch nicht anbinden. Dieselben Funktionen, nur diese Parameter:

| Heute 15m | 5m-Parameter |
|---|---|
| `resolve_historical_pd_config(1)` → `…_15mpd1_v1` | zusätzlicher Schlüssel `("5m", 1)` → `…_5mpd1_v1`. Aufruf `("15m", 1)` bleibt |
| `_causal_15m_decision_close` / `causalDecisionCloseMs`, Divisor 15 | Divisor = Timeframe der Line. 15m-Pfad behält 15. 5m-Pfad benutzt 5 |
| `paneSupports`: `pane.tf === "15m"` | `5m` zusätzlich, und nur wenn die 5m-Line `publish_complete` ist |

Unverändert: `computeExactBandsForSide`, Buckets, Paletten, Opacity, `filterSegmentsByMinOrigins`, `planExactOriginFixedTiles`, `drawEodDensity`, Zoom und Pan.

## Testmatrix

| Test | PASS-Kriterium |
|---|---|
| 15m-PD1-Regression | Baseline: 17568 Closes, Hash `4aeb36fe…`, Stichprobe 2026-09-14 21:45 unverändert |
| September-Fenster 15m | `build_landscape_window("2026-09-14T21:45:00Z","2026-09-15T05:45:00Z")` liefert 97 Preise und `phase5s_batch_reader_active = true` |
| JSON gegen Staging | 2592/2592, Fingerprint 0 Fehler, Frozen-ID nur `fe549309…` |
| Staging gegen Catalog-Mapping | `catalog_rows_from_payload`: gleiche `pool_id`, `top_price`, `bottom_price`, `side`. Kein zweiter Mapper |
| Provenance | Lookup-SHA gleich Datei-SHA, Frozen-ID gleich Payload, Sidecar-Readback der Batch |
| Line-Isolation | Neue Zeilen nur auf `…_5mpd1_v1`. 15m-Count bleibt 17568 |
| Origin-Sweep | `computeExactBandsForSide` unverändert. Python-Replikation aus `phase6xt_pd1_density_parity_audit_v1.py` auf einem 5m-Close des Tiles, Tiefe = distinct `origin` |
| 48h-Tile | `planExactOriginFixedTiles` für 2026-09-16 00:00Z–2026-09-17 23:55Z ergibt ein Tile, `gridStartMs` auf der 48h-Grenze, 576 Schritte |
| JS/Python | Dieselbe Sweep-Funktion, kein zweites JS. Ein Close, beide Seiten, Bandgrenzen gleich |
| Canvas | `drawEodDensity` unverändert. `test_pd1_density_canvas_stacking_hotfix_v1.py` bleibt grün. Kein visueller Ersatz für die Zahlen oben |

## Rollback

Vor dem Write die Baseline speichern.

Während des Writes: ein Batch, der `PublishConflictError` oder Readback-Fehler wirft, stoppt. Unvollständige Member ohne `write_complete = 1` räumt die vorhandene Funktion `cleanup_orphan_members_for_snapshot` nur für diese Snapshot-ID.

Nach einem abgeschlossenen Batch ist der Wiederholungslauf ein Skip, kein Update.

Vollständiger Rückbau, nur nach eigener Freigabe, nur diese Prädikate:

- `pipeline_line_id = historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`
- `snapshot_id` in der Menge der 2592 Pilot-Snapshots

auf Publish, Provenance, Catalog, Meta und Members. Es gibt dafür kein vorhandenes Ein-Kommando. `cleanup.py` löscht abgeschlossene Publishes nicht. Danach muss die 15m-Baseline exakt der Tabelle oben entsprechen. 15m-Snapshot-IDs werden nicht gelöscht.

## Nicht in diesem Piloten

PD5, PD10, April, Mai–August, 2026-01-01 bis 2026-04-10, die phase10-5m-PD10-Line, Gate-A-Close `20260501T000000Z`, Viewer-Code, Dashboard-Neustart.

Stop. Der nächste Schritt ist die gesonderte Freigabe des Write-Scope oben.
