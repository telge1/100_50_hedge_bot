# PHASE 6XT – Original 15m Min-Depth Regression Forensic V1

Read-only. Kein Code, kein Git-Reset, kein Dashboard-Neustart, keine Flag-Umschaltung, keine ClickHouse-Writes.

## VERDICT

**MIN_DEPTH_REGRESSION_ROOT_CAUSE_NOT_VERIFIED**

Der letzte festgeschriebene 15m-PD1-Stand und der heutige Code zeichnen für DOGEUSDT, 15m, PD1, B – Exact Origin-Density dieselben Zahlen. Eine spätere Änderung, die diesen 15m-Pfad kaputt gemacht hat, ist in Git und in den Messungen nicht nachweisbar.

Der gemeinsame Grund, warum der Regler auf dem sichtbaren Tag heute bei 5m und bei 15m nichts ändert, ist belegt: der Standardpfad behält die ersten 900 Rohzonen. Die liegen im Sweep-Vorlauf vor dem sichtbaren Fenster. Dieser Pfad steht schon im letzten festgeschriebenen Stand.

## Letzter nachweislich festgeschriebener Stand

| | |
|---|---|
| Repo | `spread_recovery_hedge_short_dev` |
| Commit | `5e7726fd86115a07b3677d0fe05e0871ad2ac9dd` |
| Datum | 2026-10-20 15:40:10 +0200 |
| Betreff | Phase 6XT – Working Exact Origin Density Min-Depth Slider V1 |
| Branch | `phase6xt/exact-origin-density-min-depth-slider-v1` (Tip = dieser Commit) |

Es gibt keinen späteren Commit auf diesem Branch. Alles danach liegt nur im Arbeitsverzeichnis und ist nicht committet. Der laufende Branch `feature/ema-touch-structure-v2` enthält diese Dateien nicht im Index.

Ein Browser-Mitschnitt, der an diesem Commit den Regler auf dem ganzen sichtbaren Chart zeigt, liegt in den Berichten nicht vor. „Funktionierend“ ist hier der Commit-Stand, nicht ein beobachtetes Pixelbild. **NOT_VERIFIED:** ob der Browser an diesem Tag den Canvas-Flag an hatte.

## Alt gegen neu

| | Commit `5e7726fd` | Heute, Disk und `:3000` |
|---|---|---|
| JS-Version | `phase6xt-density-min-depth-slider-v1` | `phase6xt-4h-pd10-strict-viewer-v1` |
| HTML-Query | dieselbe Version | weiter `?v=phase6xt-density-min-depth-slider-v1` |
| Pane-Filter | nur `pane.tf === "15m"` | freigegebene Scopes, inklusive 15m PD1 |
| `chart.js` | — | bytegleich mit dem Commit |
| Slider | `#researchExactOriginMinDepth`, `input` → `setMinOrigins` → `reapplyMinDepthFilterAllPanes` | unverändert |
| Filter | `filterSegmentsByMinOrigins` auf `metadata.depth` | unverändert |
| Canvas-Flag | `phase6xt_eod_density_canvas_v1 === "1"`, Default aus | gleich |
| Standardpfad | `applyZoneCap` = `zones.slice(0, 900)` vor dem Filter | gleich |
| Canvas-Pfad | Cull, Merge, Maximum 8000, sonst `PERF_BLOCK` | gleich |
| Merge | `end >= start - 300` | `end >= start` |
| 15m-Spaltenbreite | 900 s (Default-Slot 15) | 900 s (`slotMinutes` 15) |

`chart.js` löscht beim Canvas-Zeichnen zuerst mit `clearRect`. Der Standardpfad ersetzt die Overlay-Liste. Beides steht schon im alten Stand. Ein Slider-Schritt ohne Cache kehrt in beiden Ständen zurück, ohne alte Rechtecke anzufassen.

## 15m PD1, festes Fenster

DOGEUSDT, 15m, PD1, B – Exact Origin-Density. Fenster 2026-09-16 00:00Z–2026-09-17 23:55Z, Sweep-Vorlauf 2 h, dieselben 285 Pools. Alter Adapter aus dem Commit, neuer Adapter von der Platte.

| Stufe | Rohzonen | Erste 900 Rohzonen | Canvas: gemergt, nach Filter |
|---|---|---|---|
| Min-Depth 1 | 52015 | 2026-09-15 22:00Z–23:00Z | 803 |
| 3 | 52015 | dieselbe Stunde | 460 |
| 5 | 52015 | dieselbe Stunde | 147 |
| 7 | 52015 | dieselbe Stunde | 24 |
| 10 | 52015 | dieselbe Stunde | 0 |

Alt und neu sind auf jeder Zeile gleich. Die 803 gemergten Segmente liegen unter 8000. Der Standardpfad liefert dem Chart nur die Stunde vor dem sichtbaren 16. September. Der Filter ändert diese Stunde. Die Kerzen ab 2026-09-16 00:00Z bekommen auf diesem Pfad keine Rechtecke.

Der Draw-Loop im Browser wurde nicht ausgeführt. `localStorage` des Nutzers wurde nicht gelesen.

## Wann der 900er-Cap kam

Er steht in `5e7726fd` als `DEFAULT_MAX_CHART_ZONES = 900`. Er war in der letzten festgeschriebenen Version schon da. Die 5m- und Multi-PD-Erweiterungen haben ihn nicht eingeführt und für 15m PD1 die Präfix-Stunde nicht verschoben.

## Warum der Effekt damals sichtbar sein konnte

Auf dem Standardpfad liegen die Rechtecke in beiden Ständen links vom sichtbaren Tag. Ein Effekt über den ganzen Tag entsteht nur auf dem Canvas-Pfad: 803, 460, 147, 24, 0. Dieser Pfad ist im Commit vorhanden und heute noch vorhanden. Er ist nur aktiv, wenn der Flag `1` ist. Ob er am 20. Oktober an war, ist **NOT_VERIFIED**.

## Gemeinsame Komponente für 5m und 15m

Dieselbe Datei, derselbe Flag, derselbe Cap, derselbe Slider, derselbe Store, dieselbe `setExactOriginDensity`-Funktion.

| Pfad | 15m PD1, dieses Fenster | 5m PD1, dasselbe Fenster (frühere Messung) |
|---|---|---|
| Standard, erste 900 Rohzonen | 22:00–23:00Z am 15.09., vor dem Tag | 22:00–22:20Z am 15.09., vor dem Tag |
| Canvas, Min-Depth 1 → 7 | 803 → 24, unter 8000 | 2091 → 68, unter 8000 |

Die 5m-Erweiterung ändert die 15m-Zahlen nicht. Sie benutzt denselben Standardpfad, und dort reicht das Budget von 900 Rohzonen noch weniger weit.

## Was diese Phase nicht erneut vorschlägt

Cull und danach `slice(0, 900)` ist in `PHASE_6XT_5M_VIEWPORT_CAP_FIX_V1` blockiert. Nach einem strengen Schnitt bleiben mehr als 900 Rohzonen und mehr als 900 gemergte 5m-Segmente. Ein stilles Präfix lässt die Min-Depth-7-Segmente im Nachmittag weg. Die Merge-Lücke von 300 s ist bereits entfernt. Der Tiefenfilter selbst ist in beiden Ständen korrekt.

## Kleinster sicherer Fix, nur Vorschlag

Den vorhandenen Canvas-Pfad für die Anzeige benutzen: Cull, Merge, `filterSegmentsByMinOrigins`, `setExactOriginDensity`. Obergrenze 8000, bei Überschreitung `PERF_BLOCK` statt eines stillen Schnitts. 15m PD1 (803) und 5m PD1 (2091) sowie die früher gemessene Drei-PD-Summe 4157 passen darunter.

Das ist eine Änderung des Default-Flags `phase6xt_eod_density_canvas_v1`. Diese Phase schaltet den Flag nicht um. Dafür braucht es eine eigene Freigabe.

Unangetastet bleiben Original-LMV2 ACTIVE, Origin-Sweep, Unique-Origin-Zählung, Tiefenfilter, Publish-Lines und die historischen Daten. Keine neue Auswahl unter 900.

## Regressionstests, falls der Canvas-Pfad freigegeben wird

1. 15m PD1, dieses Fenster: Min-Depth 1/3/5/7/10 → 803/460/147/24/0 an `setExactOriginDensity`, und `clearRect` vor dem neuen Bild.
2. 5m PD1: 2091/920/287/68/2, davon 3 Segmente in 2026-09-16 12:00–18:00Z bei Min-Depth 7.
3. 15m PD5 und PD10: bisherige Geometrie, keine neue Fusion.
4. Über 8000 Segmenten: `PERF_BLOCK`, kein stilles `slice`.
5. Flag aus: bisheriges Präfix, damit der Default-Unterschied dokumentiert bleibt.

## Offen

- Ob der Browser am 20. Oktober den Canvas-Flag an hatte.
- Ob der aktuelle Browser den Flag an hat. Ohne das ist ein Live-Pixelvergleich nicht geführt.
- Die Script-URL im HTML trägt noch die alte Query. Der ausgelieferte Body ist die 4h-Version. Für 15m PD1 sind die Segmentzahlen trotzdem gleich.
