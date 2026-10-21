# PHASE 6XT – STRICT 15M REFERENCE TRACE V1

**Bindend für alle weiteren Arbeiten an DOGEUSDT 5m PD1, PD5 und PD10.**

**STRICT 15M REFERENCE PARITY – NO NEW DESIGN**

Read-only. Keine Implementierung, keine Migration, kein Publish, keine Git-Änderung.

Die funktionierende 15m-PD1-Exact-Origin-Density ist die einzige technische und fachliche Referenz. Die 5m-Staging-JSON sind Generator-Eingangsartefakte. Sie sind kein veröffentlichter SSOT und kein Chart-Eingang.

Die im Chart-Readiness-Audit genannte Variante A (Chart liest Staging direkt) gilt nicht. Sie wäre ein zweiter Datenfluss. Verbindlich ist derselbe Fluss wie bei 15m.

## So funktioniert 15m heute

Laufender Chart: `spread_recovery_hedge_short_dev/dashboard`.  
Generator- und Importcode, mit dem die veröffentlichten 15m-Linien entstanden sind, liegt in beiden Dashboard-Bäumen und ist nicht dateiidentisch. Die Dichte-Oberfläche existiert nur im Spread-Dashboard.

### Datenfluss

```
Original-LMV2-JSON
  → ACTIVE staged (meta + members, write_complete=1)
  → Catalog research_lmv2_snapshot_active_pool_v1
  → Provenance-Sidecar
  → Publish-Ledger publish_complete=1
  → POST /live-charts/viewer/api/window
  → Exact-Origin-Sweep (dieselbe Formel)
  → 48h-UTC-Tiles oder Sliding-Fenster (Client-Cache)
  → Canvas fillRect
```

Es gibt keinen separaten Tile-Server. Ein 48h-Tile ist ein UTC-Rasterfenster, das dieselbe Window-API aufruft und unter einem stabilen Cache-Key liegt.

### 1. Original-LMV2

Eine JSON-Datei pro Decision-Close unter dem Catalog-Root `production/results/{month}/15m_PD1/`. Inhalt ist der Generatorzustand: `active_pool_ids`, `pools` mit `bottom_price` / `top_price`, `known_at`, Fingerprints, `frozen_input`, `history_from_close`, `candle_load_start`.

Policy: `rolling_history_from_close(decision_close, 4)` plus die PD-Warmup-Bars aus `preset_matrix_entry`. Die Vorgeschichte wird nicht gekürzt. `config_hash` hasht nur die Engine-Felder aus `effective_config_dict` (PD-Preset). Das Timeframe steht im `engine_input_hash` und in der Pipeline-Line, nicht im `config_hash`.

PD1-Hash, identisch für jede Timeframe-Zeile desselben Presets: `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9`.

### 2. ACTIVE SSOT, Catalog, Publish

Dasselbe Verfahren für jeden Close, `historical_json_import/import_close.py`:

1. `productive_prewrite_gate` — Schreibgatter `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1` und `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`
2. `execute_batch_import` — Batch validieren, Scope gegen schon publizierte Snapshot-IDs der **Ziel-Line**
3. `write_active_snapshot_staged` — Tabellen `research_lmv2_active_snapshot_meta_v1`, `research_lmv2_active_snapshot_member_v1`
4. `write_active_pool_catalog_from_json_rows` — `research_lmv2_snapshot_active_pool_v1` (`top_price`, `bottom_price` unverändert)
5. Provenance-Sidecar — `research_lmv2_snapshot_import_provenance_v1`, Read-back vor dem Publish
6. `commit_publish_ledger` — `research_lmv2_active_snapshot_publish_v1`, `publish_complete=1`

`publish_guard.LineIdentity` trägt `symbol`, `timeframe`, `pool_detail`, `config_hash`. Ein abweichender Fingerprint auf demselben `(pipeline_line_id, config_hash, decision_close)` ist ein `PublishConflictError`.

15m-PD1-Line, heute in ClickHouse 17568 Closes, 2026-04-01 00:00 bis 2026-09-30 23:45:

`historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1`

Gebildet von `StrategyCombo("15m", 1).pipeline_line_id()`. Dieselbe Funktion ergibt für 5m PD1 `…_5mpd1_v1`. Diese Line ist nicht veröffentlicht.

`STRATEGY_COMBOS` des Bulk-Imports sind nur `15m` PD1/PD5/PD10 und `4h` PD10. Im Spread-Importer existiert zusätzlich `ISOLATED_IMPORT_COMBOS = (StrategyCombo("5m", 1),)` und ein Gate-A-Konstantensatz für **einen** Close `20260501T000000Z` auf `…_5mpd1_v1`. Dieser Close ist nicht im Publish-Ledger. Der Gate-A-Satz ist kein fertiger 5m-Release und umfasst nicht das September-Staging.

Im Pool-Repository fehlt `ISOLATED_IMPORT_COMBOS`. Die beiden Importkopien sind auseinandergelaufen. Vor einem Schreibschritt muss die Kopie festliegen, die den laufenden 15m-Chart und den Import bedient: das Spread-Dashboard.

### 3. Chart-API

`POST /live-charts/viewer/api/window` → `build_landscape_window`.

- `resolve_historical_pd_config(1)` erzwingt `timeframe="15m"` und die 15m-PD1-Line. Anderes Timeframe: `INVALID_SCOPE`.
- `PublishedActiveReader` liest nur `publish_complete=1`.
- Catalog-Zeilen werden zu Mitgliedern: `bottom_price` → `lower_bound`, `top_price` → `upper_bound`. `known_at` nach dem Decision-Close ist `CAUSALITY_VIOLATION`.
- Preisachse: ClickHouse-Kerzen, auf 5m resampled. Das ist die Zeichenachse.
- Mitgliedschaft: `_causal_15m_decision_close`. Zwischen zwei 15m-Publikationen bleibt ACTIVE konstant, weil der SSOT alle 15 Minuten veröffentlicht.
- Remaining ist aus.
- Sichtfenster maximal 72 Stunden, höchstens 864 inclusive 5m-Schritte.

### 4. Density, Tiles, Canvas

Modul, nur im Spread-Dashboard:

| Stück | Datei | Rolle |
|---|---|---|
| Schalter, Slider, Fetch | `static/js/research/pd1_exact_origin_density_research_v1.js` | nur `pane.tf === "15m"` und DOGEUSDT |
| Formel und Tiles | `static/js/research/pd1_exact_density_trp_adapter.js` | `computeExactBandsForSide`, `poolsToExactOriginZones` |
| Zeichnen | `static/research_trp/chart.js` `drawEodDensity` / `setExactOriginDensity` | füllt vorberechnete Rechtecke |
| HTML | `templates/research_charts.html` | Checkbox, Slider 1–10, Reset |

Formel, unverändert zu lassen: Sweep über `bottom`/`top`. Bei jedem Preisereignis ist die Tiefe die Anzahl **verschiedener** `origin`-Werte, nicht die Poolanzahl und nicht `strength`. Benachbarte Bänder gleicher Tiefe werden zusammengezogen (`1e-15`).

Farben und Deckkraft, fest:

- Lower: `#1a237e #3949ab #5c6bc0 #7986cb #9fa8da`
- Upper: `#4a148c #6a1b9a #8e24aa #ab47bc #ce93d8`
- Deckkraft nach Bucket: `0.35 0.45 0.55 0.65 0.78`
- Buckets: Tiefe 1 / 2–3 / 4–5 / 6–7 / ≥8

`causalDecisionCloseMs` im Adapter rundet die Zeichenzeit auf 15 Minuten, passend zur 15m-Publikation. Der Sweep selbst kennt kein Timeframe.

48h-Tiles: Flag `phase6xt_eod_fixed_tile_48h_v1`, Default aus. `planExactOriginFixedTiles` legt ein UTC-Raster von 48 Stunden. Jedes Tile ist ein Window-API-Aufruf mit Cache-Key `pd1-eod-tile`.

Min-Depth: `filterSegmentsByMinOrigins` auf `metadata.depth`, nach der Formel, vor `setExactOriginDensity`. Der Slider ändert keine SSOT-Zeile.

Canvas: `fillRect` mit `style.color` und `opacity`. Keine zweite Dichteberechnung.

Origin-Schlüssel serverseitig: `lldv2:SYMBOL:TF:micro:SIDE:UNIX` wird zu `lldv2:SYMBOL:TF:SIDE:UNIX`. Das Timeframe bleibt im Schlüssel.

### 5. Validierung, an der 15m gemessen wurde

| Stufe | Nachweis |
|---|---|
| Original-Snapshots | JSON-Fingerprint, Frozen-Input-ID aus `AUTHORIZED_FROZEN_INPUT_IDS`, Decision-Close = Dateiname |
| SSOT-Parity | `publish_guard`: Member-Fingerprint = Meta = erwarteter Zustand; Catalog-`pool_id`-Menge = ACTIVE-Menge |
| Backend-Parity | `load_historical_pool_state_at_close` gegen Publish + Catalog; `known_at` nicht nach dem Close |
| JS/Python-Parity | `phase6xt_pd1_density_parity_audit_v1.py` gegen `computeExactBandsForSide` |
| Tile-Parity | `test_pd1_density_fixed_tile_integration_v1.py` |
| Fenster-Regression | `test_pd1_density_research_live_integration_v1.py`: 2026-09-14 21:45Z–2026-09-15 05:45Z, 97 Preise, Phase-5S-Batch aktiv |
| Canvas | `test_pd1_density_canvas_stacking_hotfix_v1.py`, `drawEodDensity` zeichnet nur Segmente |

## So wird dieselbe Komponente für 5m genutzt

Keine neue Formel, keine neue Pooldefinition, kein Staging-Reader im Chart.

| Komponente heute (15m) | Dieselbe Komponente für 5m | Unvermeidbarer Parameter | Nachweis, dass 15m unverändert bleibt |
|---|---|---|---|
| LMV2-JSON im Catalog-Root `15m_PD1` | Dieselben Dateifeldern unter `5m_PD1` / `5m_PD5` / `5m_PD10`, nachdem sie im Catalog-Root liegen | Ordner und `timeframe` im Dokument | 15m-Dateien und ihre Publish-IDs werden nicht überschrieben |
| `StrategyCombo.pipeline_line_id()` | Dieselbe Funktion | `StrategyCombo("5m", pd)` → `…_5mpd{n}_v1` | `…_15mpd1_v1` bleibt die einzige Line, die der heutige Vertrag auflöst |
| `import_historical_close` | Dieselbe Reihenfolge staged → catalog → sidecar → publish | `LineIdentity.timeframe="5m"`, `pool_detail` 1, 5 oder 10 | Prewrite-Gate und Conflict-Check bleiben; 15m-Line wird nicht beschrieben |
| `config_hash(preset)` | Derselbe Hash je PD | keiner am Hash | PD1 bleibt `4aeb36fe…`. Timeframe steckt weiter nur in Line und `engine_input_hash` |
| Vier-Monats-History + Warmup | Die schon in den 5m-JSON stehenden `history_from_close` und `candle_load_start` | keiner | Generatorcode und 15m-Vorgeschichte bleiben |
| `resolve_historical_pd_config` | Dieselbe Auflösung, Schlüssel `(timeframe, pool_detail)` | Eintrag `("5m", pd)` neben den 15m-Einträgen | Aufruf `( "15m", 1 )` liefert weiter `…_15mpd1_v1` |
| `build_landscape_window` | Dieselbe Funktion, Line aus dem Vertrag | Slotbreite = Timeframe der veröffentlichten Line (15m bleibt 15, 5m wird 5) | Bestehender Aufruf ohne Timeframe-Argument bleibt 15m PD1 |
| `_causal_15m_decision_close` / `causalDecisionCloseMs` | Dieselbe Abrundung auf den veröffentlichten Close | Minutendivisor 15 oder 5, gleich der Line | 15m-Pfad behält Divisor 15 |
| `computeExactBandsForSide` | Unverändert | keiner | Bytegleich; Paritätsaudit 15m bleibt grün |
| Farben, Buckets, Opacity, Slider, Canvas | Unverändert | keiner | `drawEodDensity` und Paletten unangetastet |
| `paneSupports` | Dieselbe Sperre, erweitert um die Pane, deren TF eine veröffentlichte Line hat | erlaubtes `pane.tf` enthält `5m`, sobald `…_5mpd*_v1` publish_complete ist | `15m` bleibt erlaubt und weiter an `…_15mpd1_v1` gebunden |
| 48h-UTC-Tiles | `planExactOriginFixedTiles` | keiner (48h, UTC, derselbe API-Call) | Flag default aus; 15m-Cache-Keys bleiben |
| Validierung | Dieselben Stufen an einem 5m-Close | der Close und die 5m-Line | Die 15m-Regression 14.–15. September, 97 Preise, bleibt Pflicht |

Die Slotbreite ist der einzige Eingriff in die zeitliche Zuordnung. Sie ist das Timeframe der bereits veröffentlichten Closes. Bei 5m-SSOT liegt ein Close alle 300 Sekunden. Dieselbe 15-Minuten-Klammer würde fünf Originalzustände zu einem zusammenziehen. Das wäre eine andere Zuordnung, keine Parametrierung.

## Release-Schritte, die für 5m noch fehlen

Dieselben Schritte wie bei 15m. Nichts davon ist für die Overnight-5m-Dateien ausgeführt.

1. **Eingang bleibt JSON.** September PD1/PD5/PD10 liegt nur unter `staging/phase6xt_5m_*`. Mai–August und der April-Rest liegen unter `production/results`, ebenfalls ohne 5m-Publish. Der Importer liest den Catalog-Root, nicht das Staging. September muss vor einem Import in dieser Catalog-Form vorliegen, ohne Poolkörper zu verändern. Das ist ein Release-Schritt, kein Chart-Pfad.
2. **Eigene Lines** über `StrategyCombo.pipeline_line_id()`: `…_5mpd1_v1`, `…_5mpd5_v1`, `…_5mpd10_v1`. Nicht `…_15mpd*_v1`. Dieselbe `config_hash` je PD ist beabsichtigt; die Line trennt das Timeframe.
3. **Gates** `HISTORICAL_JSON_IMPORT_ALLOW_WRITE` und `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE`, dazu das produktive Prewrite-Gate und die Batch-Validierung.
4. **`import_historical_close`** je Close: staged, Catalog, Sidecar-Readback, Publish. Fingerprint-Konflikt bricht ab.
5. **Vertrag und Window** lösen `("5m", pd)` auf diese Lines auf. Der parameterlose 15m-PD1-Aufruf bleibt.
6. **Dieselben Prüfungen:** deterministische Snapshots, SSOT-Parity, Backend-Parity, JS/Python-Parity der unveränderten Sweep-Funktion, Tile-Parity, Canvas-Prüfung, plus die bestehende 15m-Fensterregression.
7. **Erst danach** darf die 5m-Pane die Window-API mit der 5m-Line rufen.

Bekannt und unverändert: 2026-01-01 bis 2026-04-10 ist NOT_VERIFIED und hat keine Original-Snapshots. Die phase10-Zeile `phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_…` (2016 Closes, 24.–30. September) ist ein anderer Bestand und nicht diese Line.

## 15m-Sperre

Unberührt bleiben:

- `computeExactBandsForSide`, Paletten, Buckets, Opacity, Slider, `drawEodDensity`
- Publish-Zeilen `…_15mpd1_v1`, `…_15mpd5_v1`, `…_15mpd10_v1`
- `history_from_close` / Warmup der schon erzeugten 15m- und 5m-Snapshots
- Default des 48h-Flags (aus)
- Die Fensterregression 2026-09-14 21:45Z–2026-09-15 05:45Z

Stop. Nächster Schritt nur nach ausdrücklicher Freigabe, und dann als Parametrierung dieses Flusses, nicht als neuer Reader.
