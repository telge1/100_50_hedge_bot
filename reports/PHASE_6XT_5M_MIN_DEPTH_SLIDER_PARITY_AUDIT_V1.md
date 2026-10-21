# PHASE 6XT – 5M MIN-DEPTH SLIDER PARITY AUDIT V1

**Date:** 2026-10-21  
**Mode:** Read-only. Keine Codeänderung, kein ClickHouse-Write, kein Dashboard-Neustart, kein Git-Commit.

## VERDICT

**`5M_MIN_DEPTH_SLIDER_DEFECT_CONFIRMED`**

Der Slider liest einen Wert, filtert mit derselben Funktion wie 15m PD1 und löst einen Canvas-Redraw aus. Die sichtbare 5m-Abweichung entsteht **davor**, in `mergeExactOriginSegments`: die feste Lücke von 300 Sekunden ist bei 15m kleiner als eine Spalte und bei 5m genau eine Spalte. Dadurch übermalt ein zusammengeschobenes Band die Spalte, die der Slider entfernen soll.

---

## 1. 15m-PD1-Referenz

Kette, unverändert zur 15m-PD1-Density:

1. `poolsToExactOriginZones` schreibt `metadata.depth` (Anzahl verschiedener `origin`, nicht `strength`).
2. `cullZonesToViewport`.
3. `mergeExactOriginSegments` zieht zeitlich benachbarte Bänder gleicher Geometrie, Tiefe, Farbe und Deckkraft zusammen.
4. `filterSegmentsByMinOrigins` behält Segmente mit `metadata.depth >= min`.
5. `setExactOriginDensity` → `drawEodDensity`.

Der Slider ändert keine SSOT-Zeile. Schwelle ist der Rohwert `depth`, nicht der Farb-Bucket.

| Stück | Ort |
|-------|-----|
| Slider 1–10, Reset | `templates/research_charts.html` `#researchExactOriginMinDepth` |
| Lesen / Speichern | `getMinOrigins` / `setMinOrigins`, Schlüssel `phase6xt_eod_density_min_origins_v1` |
| Neuzeichnen | `input` und Reset rufen `reapplyMinDepthFilterAllPanes` |
| Filter | `pd1_exact_density_trp_adapter.js` `filterSegmentsByMinOrigins` |
| Draw | `chart.js` `setExactOriginDensity` ruft `drawEodDensity` sofort auf |

---

## 2. Antworten auf die Prüfpunkte

| # | Befund |
|---|--------|
| 1 | Min-Depth ist ein reiner Anzeigefilter auf `metadata.depth` nach dem Merge. |
| 2 | Der Wert kommt aus `slider.value` (`parseInt`, Minimum 1). Fehlt das Element, gilt `localStorage`, sonst 1. Kein 5m-Sonderzweig. |
| 3 | Jedes `input` und der Reset rufen `reapplyMinDepthFilterAllPanes` auf. Bei vorhandenem `exactOriginSegmentCache` folgt `pushCanvasDensity` → `setExactOriginDensity` → `drawEodDensity`. Kein API-Fetch. |
| 4 | 5m PD1, PD5 und PD10 nutzen dieselbe Funktion und dieselbe Schwelle. Der Filter läuft einmal auf der konkatenierten Segmentliste. |
| 5 | **Nach** `mergeExactOriginSegments`. Pro PD wird gemergt, dann über die PDs konkateniert, dann gefiltert. |
| 6 | Der Slider hat keinen fest codierten 15m-Scope. `paneSupports` gilt für jedes freigegebene `pane.tf`, einschließlich 5m. Der 5m-Fehler sitzt in der Merge-Bedingung `last.end_timestamp >= z.start_timestamp - 300`. |
| 7 | Tile-Cache speichert Pools, nicht die gefilterten Rechtecke. `exactOriginSegmentCache` speichert die **ungefilterte** Mergeliste und filtert bei jedem Slider-Zug neu. `localStorage` wird beim `install` einmal in den Slider geschrieben und danach vom Slider selbst aktualisiert. Ein 5m-Wert wird nicht durch einen 15m-Cache ersetzt. Der Schlüssel ist timeframe-neutral: ein Wert gilt für 15m und 5m gemeinsam. |
| 8 | Zählung unten. Viewport ist das volle Fenster; `cullZonesToViewport` hat darin 0 Segmente verworfen. Ein abweichender Chart-Zoom wurde nicht rekonstruiert. |

---

## 3. Rechtecke im identischen Fenster

Fenster: `2026-09-16T00:00:00Z`–`2026-09-17T23:55:00Z`. Sweep ±2h, Load-Span ab `2026-09-09T00:00:00Z`. Produktionsadapter, Canvas-Pfad.

15m PD1 live gelesen (GET/POST nur lesend): 576 Preise, 285 Pools, Line `…_15mpd1_v1`, Slot 15.  
5m aus dem vorhandenen Export `window_pools_pilot.json`: PD1 599, PD5 692, PD10 913 Pools, Slot 5.

Zählung nach Merge, vor dem Chart-Pixelclip. Das sind die Rechtecke, die der Slider an `drawEodDensity` gibt.

| Scope | Min 1 | Min 2 | Min 3 | Min 5 | Merge-Lücken 1–300s |
|-------|------:|------:|------:|------:|--------------------:|
| 15m PD1 | 803 | 662 | 460 | 147 | **0** |
| 5m PD1 | 2080 | 1473 | 916 | 287 | **11** |
| 5m PD5 | 1201 | 556 | 228 | 40 | **5** |
| 5m PD10 | 876 | 205 | 39 | 2 | **0** |

15m PD1 Tiefe nach Merge: 141×1, 202×2, 178×3, 135×4, 82×5, 41×6, 18×7, 6×8.  
5m PD1: 607×1, 557×2, 389×3, 240×4, 138×5, 81×6, 43×7, 17×8, 6×9, 2×10.

Der Filter selbst ist kein No-Op. Von Min 1 auf Min 2 fallen bei 5m PD1 607 Rechtecke weg, bei 15m PD1 141.

---

## 4. Abweichung von der 15m-Referenz

Synthetisch, dieselbe Funktion, drei aneinanderliegende Spalten Tiefe 5 / 1 / 5, gleiche Preise:

| Spaltenbreite | Nach Merge | Nach Min-Depth 2 |
|---------------|------------|------------------|
| 300s (5m) | Tiefe 1 auf 300–600, Tiefe 5 bereits auf **0–900** | ein Rechteck **0–900**. Die entfernte Spalte bleibt übermalt. |
| 900s (15m) | drei getrennte Rechtecke | zwei Tiefe-5-Rechtecke mit Loch 900–1800. |

Ursache: die Sortierung stellt gleiche Tiefen zusammen, und `- 300` verbindet sie, wenn genau eine 5m-Spalte dazwischen fehlt. Eine 15m-Spalte ist 900s. Dieselbe 300s-Toleranz überbrückt sie nicht. Im Referenzfenster bleibt die 15m-PD1-Mergezahl bei einer Berührungsregel identisch (803). Bei 5m PD1 entstehen 11 zusätzliche Verschmelzungen (2080 statt 2091), bei PD5 fünf, bei PD10 keine.

Ein falscher 15m-Slot auf 5m-PD1-Pools ist **nicht** der laufende Pfad. Der Code übergibt `slotMinutes` aus dem Timeframe. Zur Kontrolle: Slot 15 auf denselben 5m-PD1-Pools ergäbe 1438 / 1006 / 617 / 179 Rechtecke. Der Live-Pfad mit Slot 5 ergibt 2080 / 1473 / 916 / 287.

---

## 5. Kleinster Fix

Eine Bedingung in `mergeExactOriginSegments`:

`last.end_timestamp >= z.start_timestamp - 300`  
wird zu  
`last.end_timestamp >= z.start_timestamp`.

Benachbarte Spalten berühren sich (`end == start`) und werden weiter zusammengezogen. Eine fehlende Spalte bleibt eine Lücke, danach greift der bestehende Filter. `filterSegmentsByMinOrigins`, der Slider und die 15m-Formel bleiben. Auf diesem Fenster ändert das die 15m-PD1-Mergezahl nicht.

Nicht der Fix: eine neue Min-Depth-Formel, ein 5m-eigener Schwellenwert oder Filter vor dem Merge. Das wäre nicht mehr die 15m-Reihenfolge.

---

## 6. Nicht geprüft

Kein Browser-Zug am laufenden Slider. Ob ein konkreter Chart-Zoom zusätzliche Rechtecke außerhalb des Fensters clippt, ist offen. Die Funktionskette und die Zählung oben sind am Produktionsadapter und an den Window-Pools belegt.

## 7. STOP

Read-only abgeschlossen.
