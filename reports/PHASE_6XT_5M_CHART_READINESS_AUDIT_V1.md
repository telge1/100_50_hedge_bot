# PHASE 6XT – 5M EXACT ORIGIN-DENSITY CHART READINESS AUDIT V1

**VERDICT: `5M_CHART_INTEGRATION_REQUIRED`**

Read-only audit. Keine Snapshots erzeugt, keine ClickHouse-Writes, kein Publish, keine Chart- oder Generatoränderung.

Die historischen 5m-Originalsnapshots für DOGEUSDT PD1, PD5 und PD10 liegen vollständig im geplanten Staging. Der funktionierende 15m-PD1-Exact-Origin-Density-Chart kann sie in diesem Zustand nicht zeichnen. Exact Parity ist nicht validiert.

## Vier Zustände

| Zustand | 5m PD1 | 5m PD5 | 5m PD10 |
|---|---|---|---|
| DATEN VORHANDEN | Ja, Staging-Plan vollständig. September lückenlos. April-Kopf lückenlos bis zum Produktionsanschluss. | Gleich | Gleich |
| DATEN IM BACKEND VERFÜGBAR | Nein. Kein Reader und keine Publish-Zeile für diese Snapshots. | Nein | Nein |
| DATEN IM CHART DARSTELLBAR | Nein. UI und Window-API sind auf 15m PD1 und den veröffentlichten Katalog festgelegt. | Nein | Nein |
| EXACT PARITY VALIDIERT | Nein | Nein | Nein |

Die vier Zustände sind unabhängig. Vorhandene JSON-Dateien sind weder ein Backend-Datensatz noch eine Chart-Ansicht.

## Referenz, die heute funktioniert

Exact Origin-Density (Modus B) läuft im Research-Chart von `spread_recovery_hedge_short_dev/dashboard`:

- Checkbox `researchIndExactOriginDensity`, Hinweistext „nur DOGEUSDT 15m Historical PD1“
- Mindesttiefe-Slider `researchExactOriginMinDepth` (1–10, reiner Darstellungsfilter)
- 48h-UTC-Tiles über Local-Storage-Flag `phase6xt_eod_fixed_tile_48h_v1`
- Datenpfad `POST /live-charts/viewer/api/window`
- Mitgliedschaft aus dem veröffentlichten 15m-PD1-Katalog, Preisachse als 5m-Kerzen
- ACTIVE only, Remaining aus

In diesem Repository (`pools+ob+delta_bot`) fehlt `static/js/research/pd1_exact_origin_density_research_v1.js`. `templates/research_charts.html` enthält die Density-Checkbox nicht. Eine zweite, inhaltlich abweichende Kopie von `pd1_landscape_viewer_v1/historical_window_v1.py` liegt hier und in `spread_recovery_hedge_short_dev`. Beide Kopien sind auf 15m PD1 fest verdrahtet. Eine Änderung nur in einer Kopie erreicht den laufenden Chart nicht.

## Aufgabe A – Dateninventar

Quelle: Staging unter `doge_multi_month_2026_history/staging/`. Alle drei NOHUP-Läufe `COMPLETE`, Exit-Code 0.

| Lauf | Run | Ende (UTC) | Plan-Closes | Dateien | Plan fehlend | Zusätzlich zum Plan |
|---|---|---|---:|---:|---:|---:|
| 5m PD1 | `phase6xt_5m_pd1_20261020T165149Z` | 2026-10-20 20:30 | 12222 | 12235 | 0 | 13 |
| 5m PD5 | `phase6xt_5m_pd5_20261020T210058Z` | 2026-10-20 23:33 | 10641 | 10717 | 0 | 76 |
| 5m PD10 | `phase6xt_5m_pd10_20261020T233508Z` | 2026-10-21 01:16 | 9007 | 9007 | 0 | 0 |

Die zusätzlichen Dateien sind die im Manifest bereits vor dem Lauf vorhandenen April-Closes, geschlossen am Anfang des April-Kopfs:

- PD1: 2026-04-11 00:00–01:00 UTC (13), Manifest `already_in_staging: 13`
- PD5: 2026-04-11 00:00–06:15 UTC (76), Manifest `already_in_staging: 76`

Dateinamen sind eindeutig. Planlisten enthalten keine doppelten Closes. Jede Datei beginnt mit `{` und endet mit `}`. Größe PD1 108–234 KB, PD5 113–238 KB, PD10 142–250 KB. Keine Datei unter 1 KB.

Feldprüfung über alle Dateien: `symbol=DOGEUSDT`, `timeframe=5m`, `pool_detail` passend zum Ordner, `decision_close` gleich dem Dateinamen, Frozen-Snapshot passend zum Monat (`84c22453…` im April, `fe549309…` im September). Abweichungen: 0.

### Zeitabdeckung im Staging

5m-Raster, Schritt 300 Sekunden, innerhalb des jeweiligen Min/Max keine Lücken.

| Combo | April im Staging | September im Staging |
|---|---|---|
| PD1 | 2026-04-11 00:00 – 2026-04-23 11:30 (3595/3595 des erlaubten April-Kopfs) | 2026-09-01 00:00 – 2026-09-30 23:55 (8640/8640) |
| PD5 | 2026-04-11 00:00 – 2026-04-18 05:00 (2077). Danach nicht in diesem Staging. | 8640/8640 |
| PD10 | 2026-04-11 00:00 – 2026-04-12 06:30 (367). Danach nicht in diesem Staging. | 8640/8640 |

Der erlaubte April-Kopf im Manifest endet für alle drei bei 2026-04-23 11:30. PD5 und PD10 erzeugen dort nur den Kopf, weil der Rest schon in `production/results` liegt. Der Anschluss auf dem Dateinamenraster ist die nächste 5m-Kerze:

| Combo | Letzter Staging-Close | Erster Production-Close April | Dateiname-Lücke |
|---|---|---|---|
| PD1 | 2026-04-23 11:30 | 2026-04-23 11:35 (2165 Dateien bis 2026-04-30 23:55) | keine |
| PD5 | 2026-04-18 05:00 | 2026-04-18 05:05 (3683 Dateien bis Monatsende) | keine |
| PD10 | 2026-04-12 06:30 | 2026-04-12 06:35 (5393 Dateien bis Monatsende) | keine |

Production-Dateinamen, nur gezählt, Inhalt dieser Bäume in diesem Audit nicht geöffnet:

| Monat | PD1 | PD5 | PD10 | Erwartetes volles 5m-Monat |
|---|---:|---:|---:|---:|
| 2026-05 | 8928 | 8928 | 8928 | 8928 |
| 2026-06 | 8640 | 8640 | 8640 | 8640 |
| 2026-07 | 8928 | 8928 | 8928 | 8928 |
| 2026-08 | 8928 | 8928 | 8928 | 8928 |

`production/results` hat keinen Ordner `2026-09`. September existiert für diese drei Combos nur im Staging. Die Manifeste markieren Mai–August als `regen_skip_complete_months`.

### Bekannte Lücken und NOT_VERIFIED

- **NOT_VERIFIED, in keinem Baum:** 2026-01-01 bis 2026-04-10, im Manifest 28800 Closes. Bewusst ausgeschlossen.
- **April nach dem Staging-Ende sowie Mai–August:** Dateien liegen in `production/results`, nicht im Overnight-Staging. Für einen Chart, der nur Staging liest, sind das Lücken. Für einen späteren Gesamtzeitraum sind die Dateinamen anschlussfähig. Ihr Inhalt ist hier nicht erneut geprüft.
- **4h-Jobs:** unverändert am Pilot-Gate blockiert. Für diese 5m-Prüfung ohne Wirkung.

Frozen-Input-Deckung der erzeugten Snapshots:

- April: `84c22453…`, 1m ab 2025-09-30. `candle_load_start` der April-Samples liegt im Dezember 2025.
- September: `fe549309…`, 1m von 2026-02-28 20:00 bis 2026-10-01. Der früheste gesehene September-`candle_load_start` (PD1, 2026-09-01) ist 2026-04-18 12:30 und liegt innerhalb dieses Snapshots.

### Integrität der Original-Snapshots

Stichprobe: je Combo zehn Dateien voll geparst (Quartile beider Monate, einschließlich 2026-09-16 00:00 und der jeweiligen Endcloses). In jeder Stichprobe:

- `active_pool_ids` eindeutig und gleich den Schlüsseln von `pools`
- `active_sides` gleich lang
- `bottom_price < top_price`
- `known_at` nicht nach `decision_close`
- `invalidated_at` leer
- Origin-Unix im Pool-ID-Suffix nicht nach `decision_close`
- `history_from_close` liegt vier Kalendermonate vor `decision_close`

Über alle Dateien, per Muster auf den Pool-IDs:

- keine Origin-Zeit vor `candle_load_start`
- keine Origin-Zeit nach `decision_close`
- keine Origin-Zeit vor `history_from_close`

Ältere noch ACTIVE Origins innerhalb des Vier-Monats-Fensters sind vorhanden. Globales Minimum der Origin-Zeit:

| Combo | früheste Origin-Zeit in den Staging-Dateien | späteste Origin-Zeit |
|---|---|---|
| PD1 | 2026-01-07 08:55 | 2026-09-30 23:50 |
| PD5 | 2026-01-14 16:45 | 2026-09-30 23:50 |
| PD10 | 2026-01-20 02:45 | 2026-09-30 23:50 |

Die Januar-Origins stammen aus den April-Snapshots, deren `history_from_close` im Dezember 2025 liegt. Sie sind also älter als der Decision-Close und zugleich innerhalb des Generatorfensters. Der Generator trägt keinen Zustand vor `candle_load_start`. Das ist die bestehende `rolling_history_from_close(decision_close, 4)`-Regel plus PD-Warmup, kein zusätzlicher Chart-Schnitt.

Pool-Felder im JSON, unverändert aus dem Originalgenerator: `pool_id`, `side`, `bottom_price`, `top_price`, `strength`, `strength_class`, `known_at`, `break_at`, `invalidated_at`, `row_fingerprint`. ID-Form `lldv2:DOGEUSDT:5m:micro:{side}:{origin_unix}`.

Diese Prüfung ist eine Struktur- und Kausalprüfung der Staging-Dateien. Sie ist keine gerenderte Dichte-Parität gegen den 15m-Chart und kein Abgleich jeder Poolzeile aller 31959 Dateien per `json.loads`.

## Aufgabe B – Backend

Der Window-Builder `pd1_landscape_viewer_v1/historical_window_v1.py` (beide Dashboard-Kopien):

- `PD1_POOL_DETAIL = 1`
- `resolve_historical_pd_config()` liefert immer `timeframe="15m"` und eine der drei Pipeline-Lines `…_15mpd1_v1`, `…_15mpd5_v1`, `…_15mpd10_v1`
- `load_historical_pool_state_at_close()` lehnt jedes Timeframe außer `15m` mit `INVALID_SCOPE` ab
- Mitgliedschaft wird mit `_causal_15m_decision_close` zwischen 15m-Publikationen konstant gehalten
- Preisreihe kommt aus ClickHouse-Kerzen, auf 5m resampled. Das ist die Anzeigeachse, nicht das LMV2-Timeframe
- Es gibt keinen Pfad, der `staging/phase6xt_5m_*` liest

`build_landscape_window()` nimmt kein `timeframe` und kein `pool_detail` entgegen.

ClickHouse, nur gelesen (`signal_generator.research_lmv2_active_snapshot_publish_v1`, `publish_complete=1`):

| Pipeline | Closes | Bereich |
|---|---:|---|
| `…_15mpd1_v1` | 17568 | 2026-04-01 00:00 – 2026-09-30 23:45 |
| `…_15mpd5_v1` | 17568 | gleich |
| `…_15mpd10_v1` | 17568 | gleich |
| `phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_…` | 2016 | 2026-09-24 00:00 – 2026-09-30 23:55 |

Katalog `research_lmv2_snapshot_active_pool_v1` für DOGEUSDT: 15m PD1/PD5/PD10 je 17568 Closes, 4h PD10 1098 Closes, 5m nur PD10 mit 2016 Closes im genannten September-Fenster. Keine Zeile für die Overnight-Staging-Snapshots. Keine 5m-PD1- und keine 5m-PD5-Publish-Line. Die phase10-Zeile ist ein älterer 7-Tage-Pilot und wird vom Density-Window nicht gelesen.

Damit: der Data Provider liest den veröffentlichten SSOT-Katalog. Staging-JSON ist für ihn unsichtbar. PD1, PD5 und PD10 sind im Vertrag nur als 15m-Linien vorhanden.

## Aufgabe B – Frontend

Timeframe-abhängig im laufenden Density-Modul `pd1_exact_origin_density_research_v1.js`:

- `paneSupports`: Symbol DOGEUSDT und `pane.tf === "15m"`, sonst aus
- Hinweis bei Ablehnung: nur 15m Historical PD1
- Fensterplanung auf 5m-Raster der **Anzeige**, maximal 864 inclusive 5m-Schritte, weil das 15m-Window intern so begrenzt ist (`MAX_5M_BARS = 864`, 72h)
- Fixe 48h-Tiles sind ein Cache-Planer über demselben 15m-API

Wiederverwendbar, sobald ein Fenster dieselbe Pool-Form liefert (`pool_id`, `side`, `bottom`, `top`, `origin`, `segments`, `known_at`):

- `pd1_exact_density_trp_adapter.js`: Sweep über eindeutige Origins, `filterSegmentsByMinOrigins`
- Origin-Schlüssel `_origin_logical_v2_key`: aus `lldv2:SYMBOL:TF:micro:SIDE:UNIX` wird `lldv2:SYMBOL:TF:SIDE:UNIX`. Das ist timeframe-neutral und würde 5m-IDs stabil lassen
- Mindesttiefe-Slider und 48h-Tile-Flag

Nicht wiederverwendbar ohne neue Semantik: das Festhalten der Mitgliedschaft zwischen 15m-Closes. Bei 5m-LMV2 wechselt ACTIVE jeden 5m-Close. Dieselbe Funktion würde 5m-Pools auf 15m-Slots zusammenziehen.

Feldbrücke, die ein Staging-Reader leisten müsste, ohne Werte neu zu rechnen: `bottom_price` → `bottom`, `top_price` → `top`. Der Katalogpfad erwartet heute `lower_bound` / `upper_bound`.

## Aufgabe C – Parity-Anforderungen und vorhandene Tests

| Anforderung | Heute für 5m-Staging |
|---|---|
| Keine Neuinterpretation der Original-LMV2-Pools | JSON enthält die Generatorfelder. Kein Chart-Reader konsumiert sie. |
| Originale Upper-/Lower-Bounds | `top_price` / `bottom_price` in den Dateien. Stichprobe `bottom < top`. |
| Eindeutige Origin-IDs | Stichprobe ohne Duplikate. ID-Suffix ist die Origin-Zeit. |
| Original ACTIVE, Remaining separat | Dateien sind ACTIVE-Sets (`invalidated_at` leer). Remaining ist nicht Teil dieser Dateien und im 15m-Density aus. |
| Kausales as-of | `decision_close` im Dateinamen. `known_at` und Origin-Zeit nicht danach. |
| Keine Future-Leaks | In der genannten Prüfung keine. |
| Mindestens vier Monate Originalgenerator-Kontext | Stichprobe: `history_from_close` vier Kalendermonate vor dem Close, plus Warmup bis `candle_load_start`. |
| Ältere noch ACTIVE Origins nicht abschneiden | Origins vor dem Decision-Close und nach `history_from_close` sind in den Dateien. Nichts vor `history_from_close`. |
| Gleiche Density-Semantik wie 15m | Sweep-Code wäre nutzbar. Der 15m-Hold wäre für 5m falsch. Nicht ausgeführt. |
| Gleicher Min-Depth-Slider | Vorhanden, an `pane.tf === "15m"` gebunden. |
| Dieselbe Quelle für Research-Chart und Backtester | 15m nutzt den Publish-Katalog. 5m-Staging ist eine andere Quelle. Backtester-Consumer in den vorhandenen Tests ist 15m. |

Vorhandene Tests, alle auf 15m PD1 bzw. auf die 5m-**Anzeigeachse** des 15m-Fensters:

- `test_pd1_density_research_live_integration_v1.py` (Pfad zeigt auf das Spread-Dashboard)
- `test_pd1_density_rootcause_repair_v1.py`
- `test_pd1_landscape_axis_density_render_v1.py`
- `test_pd1_landscape_goto_5m_align_v1.py` (5m-Raster der Kerzen im 15m-Window, nicht 5m-LMV2)
- `test_pd1_historical_window_v1.py`, Landscape-Nav/Goto/Candle-Tests
- `phase6xt_pd1_density_parity_audit_v1.py` (15m-Dichte-Parität)
- `historical_lmv2_pd_contract_v1.py`: PD 1/5/10 nur mit `HISTORICAL_TIMEFRAME = "15m"`

Ein Test, der 5m-Staging-JSON in das Density-Window lädt, existiert nicht. Exact Parity für 5m ist damit offen.

## Aufgabe D – Kleinstmöglicher Pilot

**Fenster:** DOGEUSDT, 5m PD1, sichtbar 2026-09-16 00:00 UTC bis 2026-09-17 23:55 UTC (576 Closes, 48h). Dieses Intervall liegt in dem September, das für alle drei PDs Datei für Datei geprüft wurde.

Kontext wie beim 15m-Chart: 168h Mitgliedschaft vor dem sichtbaren Anfang, also ab 2026-09-09 00:00 UTC. Das bleibt im September-Staging. Ein Fenster am 1. September würde den 168h-Kontext in den August schieben; der August liegt in `production/results` und ist hier nicht inhaltlich geprüft. Deshalb nicht als ersten Piloten nehmen.

PD5 und PD10 haben im selben September dieselbe volle Abdeckung. Dieselbe Route kann sie danach über `pool_detail` 5 und 10 ansprechen, ohne das 15m-Window zu ändern.

### Variante A – Read-only aus dem Staging

Neue Route und ein neuer Reader, der nur die Staging-JSON-Dateien dieses Fensters öffnet und sie in die bestehende Pool-Form des Adapters mappt. `historical_window_v1.py` und die 15m-Checkbox bleiben unverändert. Mitgliedschaft schreitet in 5m-Closes. Bounds und IDs werden durchgereicht.

Gefahr für die bestehende 15m-Implementierung: gering, solange der 15m-Loader und die 15m-Pipeline-Lines unangetastet bleiben und der 5m-Pfad hinter einem eigenen Endpunkt liegt. Die zwei abweichenden Window-Kopien dürfen dabei nicht „mitgezogen“ werden.

Offen danach: Staging ist nicht die Quelle des Backtesters. Ein späterer Research-Chart und der Backtester hätten erst dann dieselbe Quelle, wenn ein gemeinsamer Reader denselben Baum oder denselben Katalog liest. Variante A allein erfüllt diesen Punkt nicht.

### Variante B – eigener 5m-SSOT-/Catalog-Release, danach Chart

Validierter Publish auf **neuen** Pipeline-Lines, danach derselbe Chart-Anschluss gegen den Katalog. Das ist der Weg zur gemeinsamen Quelle für Chart und Backtester.

Gefahr für 15m: höher. Publish und Katalog liegen in `research_lmv2_active_snapshot_publish_v1` und `research_lmv2_snapshot_active_pool_v1`, also in den Tabellen, die der 15m-Reader liest. Dieselbe `config_hash` wie 15m PD5/PD10 (`533e04c7…` bzw. `bd9ac4f3…`) beschreibt den PD, nicht das Timeframe. Ein Publish unter `…_15mpd*_v1` würde die 15m-Linie vermischen. Zusätzlich wäre das ein Write, der in diesem Auftrag ausgeschlossen ist. Keine Produktionsfreigabe.

### Empfehlung

**Variante A** für den ersten 48h-Piloten. Sie lässt die 15m-Window-Funktion, die 15m-Pipeline-Lines und die ClickHouse-Tabellen unverändert. Variante B bleibt der spätere Schritt, bevor 5m als gemeinsame Quelle für Research-Chart und Backtester gilt, und nur mit neuen Pipeline-Lines.

## Fehlende Komponenten

1. Staging-JSON-Reader für ein 5m-as-of (PD, Close → Original-ACTIVE), ohne Katalog.
2. 5m-Lifetime-Builder: Mitgliedschaft pro 5m-Close, nicht per 15m-Hold.
3. Eigener API-Endpunkt. Das bestehende `/live-charts/viewer/api/window` bleibt 15m PD1.
4. Frontend-Freigabe für eine 5m-Pane, ohne die 15m-Sperre in `paneSupports` aufzuweichen.
5. Vertragseintrag für historisches 5m PD1/PD5/PD10, getrennt von `HISTORICAL_TIMEFRAME = "15m"`.
6. Paritätstest: ein bekannter 5m-Close, Bounds und Origin-IDs identisch zur Datei, keine Future-`known_at`, 15m-Window-Regressionszahl unverändert.
7. Für Variante B später: neue Pipeline-Lines und ein Publish. Jetzt nicht vorhanden und nicht auszuführen.

## Parity-Risiken

- 15m-Hold auf 5m-Daten anwenden würde Dichte zeitlich vergröbern.
- `bottom_price`/`top_price` in `lower_bound`/`upper_bound` umzubenennen ist erlaubt. Werte neu zu runden oder zu clustern nicht.
- Origin-Schlüssel muss das Timeframe `5m` behalten, sonst kollidieren IDs gedanklich mit 15m-Origins derselben Unix-Zeit.
- September-Staging und Mai–August-Production sind zwei Bäume. Ein Pilot, der nur einen Baum mountet, darf den anderen nicht als lückenlos zeichnen.
- 2026-01-01 bis 2026-04-10 bleibt NOT_VERIFIED.
- Die phase10-5m-PD10-Publish-Line (7 Tage) ist ein anderer Bestand. Sie darf nicht als dieser Overnight-Lauf angezeigt werden.
- Zwei abweichende Kopien von `historical_window_v1.py`. Der laufende Density-Chart ist die Spread-Kopie plus deren JS.

## Aufwand

Isolierter Variante-A-Pilot, 5m PD1, ein 48h-Fenster plus 168h Kontext, neuer Endpunkt, Adapter unverändert, 15m-Pfad unangetastet, ein Paritätstest gegen die JSON-Datei: etwa **2–3 Arbeitstage**.

Dieselbe Route für PD5 und PD10 im selben Septemberfenster: etwa **ein weiterer Arbeitstag**, weil die Dateien dieselbe Form haben und die Abdeckung bereits gleich ist.

Variante B (Validierung, neue Pipeline-Lines, Publish, dann Chart): zusätzlich etwa **ein bis zwei Wochen**, abhängig vom bestehenden Publish-Gate. Nicht Teil des Piloten und ohne gesonderte Freigabe nicht zu starten.

## Verdict

`5M_CHART_INTEGRATION_REQUIRED`

Die Overnight-Snapshots sind im Staging vorhanden, an den Plan angeschlossen und in der Strukturprüfung konsistent. Backend und Chart stellen sie nicht dar. Exact Parity ist nicht validiert. `5M_CHART_READY_NOW` trifft nicht zu. `5M_DATA_VALIDATION_INCOMPLETE` trifft auf den geplanten Staging-Lauf nicht zu; die bekannten Lücken sind der dokumentierte NOT_VERIFIED-Abschnitt und die bewusst außerhalb dieses Stagings liegenden Production-Monate.

Stop. Keine Implementierung ohne gesonderte Freigabe.
