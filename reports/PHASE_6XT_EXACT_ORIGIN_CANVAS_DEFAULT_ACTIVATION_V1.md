# PHASE 6XT – Exact Origin-Density Canvas Default Activation V1

Kontrollierte Umschaltung des bestehenden Canvas-Flags auf den Default an. Keine neue Engine, kein Python-Neustart, keine ClickHouse-Writes.

## VERDICT

**EXACT_ORIGIN_CANVAS_DEFAULT_ACTIVATED_PASS**

## Geänderte Dateien und Flag

Schlüssel unverändert: `localStorage["phase6xt_eod_density_canvas_v1"]`.

Zwei Leser, dieselbe Bedingung. Der Recursion-Hotfix verbietet, dass `research_charts.js` `PD1ExactOriginResearch.isCanvasPathEnabled` aufruft. Ein einzelner Leser hätte den Density-Pfad und `layoutOverlays` auseinanderlaufen lassen. Das ist die bestehende Architektur, keine Abweichung.

| Datei | Funktion | Neu |
|---|---|---|
| `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` | `isCanvasPathEnabled` | `getItem(...) !== "0"`, bei Speicherfehler `true` |
| `dashboard/static/js/research/research_charts.js` | `exactOriginDensityCanvasEnabled` | dieselbe Bedingung |

Fehlender Schlüssel und `"1"` aktivieren den Canvas-Pfad. Nur `"0"` behält die DOM-Overlays mit den ersten 900 Rohzonen. Rücknahme: `localStorage.setItem("phase6xt_eod_density_canvas_v1","0")` und die Research-Seite neu laden. `removeItem` ist jetzt der Default an.

Unangetastet: Adapter (`last.end_timestamp >= z.start_timestamp`, mtime 2026-10-21 15:48:56 +0200), `chart.js` (`8ba510e87c3194bcc6bcb1bffd8f4a48eadc20c6534a05118ab0e7600a60d886`), Sweep, Merge, `filterSegmentsByMinOrigins`, Slider, Multi-PD-`concat`, Publish-Lines, 48h-Tile-Flag.

## Preflight

| | |
|---|---|
| Prozess | PID **1684404**, Start 2026-10-21 17:56:57 +0200, unverändert. Diese Phase hat ihn nicht neu gestartet. |
| Apache | `:8080` liefert dieselben Bytes wie `:3000`. `Cache-Control` fehlt. |
| Schreiber auf den beiden JS-Dateien | keine |
| Canvas-Path-Validation | Kein Bericht dieses Namens. Offen aus `logs/.../phase6xt_density_canvas_visibility_forensic_v1`: das Canvas lag damals unter dem Chart (`z-index` 0). Heute setzt `chart.js` `z-index` 2, `#eod-density-overlay` in `style.css` ebenfalls 2, der Chart 1. Live-Pixel dieser Stacking-Lage: nicht geprüft. |
| Flag vorher | an nur bei exakt `"1"` |

## Canvas-Limit 8000

Referenz 2026-09-16 00:00Z–2026-09-17 23:55Z, Sweep ±2 h, Cull, Merge, dann Filter. 4h bis 2026-09-17 20:00Z, weil das der 4h-Close des Fensters ist. Alle gemergten Sätze liegen unter 8000.

Der Überlaufzweig ist unverändert: `mergedSegments.length > 8000` ruft `clearCanvasOnPane` und `mergeOverlays(pane, [])` auf, setzt den Hinweis `PERF_BLOCK` und kehrt zurück. In diesem Zweig steht kein `slice`. `clearExactOriginDensity` löscht das Canvas mit `clearRect`. Für die gemessenen Kombinationen wird der Zweig nicht betreten. Ein Browser-Hinweis wurde nicht ausgelöst.

## Segmentzahlen

DOGEUSDT, B – Exact Origin-Density, Canvas-Pfad. Depth unter der Schwelle: 0.

### 15m PD1, Soll aus dem Forensic

| Min-Depth | Segmente | Soll |
|---|---|---|
| 1 | 803 | 803 |
| 3 | 460 | 460 |
| 5 | 147 | 147 |
| 7 | 24 | 24 |
| 10 | 0 | 0 |

### 5m

| Scope | 1 | 3 | 5 | 7 | 10 | gemergt |
|---|---|---|---|---|---|---|
| PD1 | 2091 | 920 | 287 | 68 | 2 | 2091 |
| PD5 | 1206 | 228 | 40 | 3 | 0 | 1206 |
| PD10 | 876 | 39 | 2 | 0 | 0 | 876 |
| PD1+PD5+PD10, concat | — | — | — | 71 | — | **4173** |

### 15m und 4h

| Scope | 1 | 3 | 5 | 7 | 10 | gemergt |
|---|---|---|---|---|---|---|
| 15m PD5 | 486 | 85 | 7 | 0 | 0 | 486 |
| 15m PD10 | 388 | 15 | 1 | 0 | 0 | 388 |
| 15m PD1+PD5+PD10, concat | — | — | — | 24 | — | **1677** |
| 4h PD10 | 65 | 6 | 0 | 0 | 0 | 65 |

PD-übergreifend bleibt es `concat`. `mergeExactOriginSegmentLists` über die drei 5m-Sätze ergibt 4018, über die drei 15m-Sätze 1624. Der Renderpfad benutzt diese Fusion nicht. Tiefen werden nicht über PDs addiert.

Zoom, neu gerechnet für 5m PD1, 2026-09-16 12:00Z–18:00Z inklusive Sweep: 631 / 248 / 61 / 9 / 0. Pan auf 2026-09-15 12:00Z–2026-09-16 12:00Z: 714 / 244 / 55 / 4 / 0. Beides unter 8000 und verschieden vom 48h-Satz, also kein stehengebliebenes Tile dieses Fensters. `scheduleViewport` baut den Sweep neu. Der 48h-Tile-Flag bleibt aus, solange er nicht `"1"` ist. Ein Klick im Browser auf Zoom, Pan oder die PD-Checkbox ist nicht ausgeführt.

## Regression der sieben Scopes

`GET /live-charts/viewer/api/capabilities`: **7** Scopes. Lines und `config_hash`-Präfixe:

| Scope | Line | Hash-Präfix |
|---|---|---|
| 15m PD1, 5m PD1 | `…_15mpd1_v1` / `…_5mpd1_v1` | `4aeb36fe` |
| 15m PD5, 5m PD5 | `…_15mpd5_v1` / `…_5mpd5_v1` | `533e04c7` |
| 15m PD10, 5m PD10, 4h PD10 | `…_15mpd10_v1` / `…_5mpd10_v1` / `…_4hpd10_v1` | `bd9ac4f3` |

4h PD10-Fenster: 529 Preise, 78 Pools, Slot 240. 4h PD1 und 15m PD3 bleiben HTTP 422 `INVALID_SCOPE`. Historical LLD V2 ist nicht geändert.

## Ausgelieferte JS-Version und Neustart

| | |
|---|---|
| `VERSION` im Density-Skript | `phase6xt-4h-pd10-strict-viewer-v1` (nicht angehoben) |
| HTML-Query Density | weiter `?v=phase6xt-density-min-depth-slider-v1` |
| HTML-Query Host | weiter `?v=phase6xt-research-fullscreen-tools-v3` |
| Body `:3000`, `:8080`, `http://dash.immotel.de:8080` | Density 39574 Bytes, Host 333492 Bytes, beide mit `!== "0"`, kein `=== "1"` mehr am Flag |
| Neustart | **nein** |

## Tests

Die Canvas-Harnesses sperren noch den alten Default aus: `canvasFlagDefaultOff` und `A_flag_missing` erwarten `false`. Der Recursion-Harness enthält eine eigene Kopie der alten Host-Bedingung `=== "1"` und meldet deshalb einen Unterschied, den die beiden ausgelieferten Funktionen nicht haben. Zusätzlich schlagen die schon vorher veralteten Versionsperren `phase6xt-density-continuous-history-v1` und `phase6xt-density-fixed-tile-integration-v1` fehl. Diese Asserts wurden nicht gelockert. 5 bestanden, 5 dieser Alt-Sperren fehlgeschlagen.

## Verbleibende Browser-Sichtprüfung

Die Seite neu laden. Liegt der Schlüssel bereits auf `"0"`, bleibt der DOM-Pfad. Sonst zeichnet der Canvas-Pfad. Ob die Rechtecke über den Kerzen sichtbar sind und der Regler sie dort wechselt, ist in diesem Lauf nicht als Pixel geprüft.
