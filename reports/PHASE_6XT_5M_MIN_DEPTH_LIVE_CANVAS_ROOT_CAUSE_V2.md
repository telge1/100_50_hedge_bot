# PHASE 6XT – 5M MIN-DEPTH LIVE CANVAS ROOT-CAUSE AUDIT V2

**Date:** 2026-10-21  
**VERDICT:** **`5M_MIN_DEPTH_LIVE_DEFECT_CONFIRMED`**

Kein Browser-Canvas wurde ausgelesen. Die Zahlen unten sind die Arrays, die der ausgelieferte Adapter an `setExactOriginDensity` beziehungsweise an die DOM-Overlays übergibt. Ein Pixelnachweis des Screenshots liegt nicht vor.

Keine Codeänderung, kein ClickHouse-Write, kein Dashboard-Neustart, kein Git-Commit.

---

## 1. Ursache

Der Slider und `filterSegmentsByMinOrigins` sind für 5m und 15m dieselbe Funktion. `metadata.depth` ist die Zahl verschiedener Origins. Bei Min-Depth 7 enthält die gefilterte Liste **kein** Segment mit Depth kleiner als 7.

Der sichtbare 5m-Chart bleibt trotzdem stehen, weil der **Standardpfad den Canvas-Flag aus hat** und danach die ersten 900 Rohzonen behält. Im Referenzfenster sind das nur **2026-09-15 22:00Z–22:20Z**, also der Sweep-Vorlauf vor dem sichtbaren 16.–17.09. Der Regler filtert diese 20 Minuten. Die Kerzen des sichtbaren Tages bekommen auf diesem Pfad keine Exact-Origin-Rechtecke, deshalb ändert der Regler dort nichts.

Der Canvas-Pfad (`localStorage phase6xt_eod_density_canvas_v1 = "1"`) ist nicht der Default. Auf ihm wäre Min-Depth 7 ein Wechsel von 2091 auf 68 Segmente, alle mit Depth mindestens 7.

---

## 2. Datenfluss

```
#researchExactOriginMinDepth  (ein Element, Wert 1–10)
  input → setMinOrigins
        → localStorage phase6xt_eod_density_min_origins_v1
        → reapplyMinDepthFilterAllPanes
```

`getMinOrigins` liest zuerst den DOM-Wert, danach den Storage. Der Handler ruft kein Fetch auf.

Zwei Zeichenpfade, gesteuert nur durch den Canvas-Flag:

| Flag | Speicher | Was der Regler filtert | Was gezeichnet wird |
|------|----------|------------------------|---------------------|
| aus (Default) | `exactOriginSegmentCache.zones` = erste 900 Rohzonen | diese 900 | DOM-Overlays im iframe. `drawEodDensity` bleibt leer, sofern der Flag beim letzten Refresh aus war |
| an | `exactOriginSegmentCache.segments` = gemergte Liste, ungefiltert | diese Liste | `setExactOriginDensity` → `drawEodDensity`. Der Canvas wird vorher mit `clearRect` geleert |

`drawEodDensity` filtert nicht noch einmal. Es zeichnet das übergebene Array und lässt Segmente weg, die außerhalb des Chart-Viewports liegen. Diesen Viewport des Screenshots gab es hier nicht.

Ein späterer Window-Request ruft beim Abschluss wieder `getMinOrigins()` auf und setzt den Regler nicht auf 1 zurück. Der Tile-Cache speichert Pools, nicht die gefilterten Rechtecke. Der Segment-Cache speichert die ungefilterte Liste und filtert bei jedem Reglerzug neu.

---

## 3. Referenzfenster, nur 5m PD1

Fenster `2026-09-16T00:00:00Z`–`2026-09-17T23:55:00Z`. Dieselben Pools und derselbe Sweep wie im Fix-Bericht. Zusätzlich eine feste Scheibe `2026-09-16T12:00:00Z`–`18:00:00Z`, ohne den übrigen Bereich zu wechseln.

Canvas-Pfad, nach Merge, vor dem Chart-Clip:

| Min-Depth | Vor Filter | Nach Filter | Davon Depth unter der Schwelle | Upper / Lower | In der 6h-Scheibe |
|----------:|-----------:|------------:|-------------------------------:|--------------:|------------------:|
| 1 | 2091 | 2091 | 0 | 979 / 1112 | 476 |
| 3 | 2091 | 920 | 0 | 444 / 476 | 188 |
| 5 | 2091 | 287 | 0 | 178 / 109 | 40 |
| 7 | 2091 | **68** | **0** | 53 / 15 | **3** |
| 10 | 2091 | 2 | 0 | 1 / 1 | 0 |

Depth-Histogramm der 2091: 611×1, 560×2, 392×3, 241×4, 138×5, 81×6, 43×7, 17×8, 6×9, 2×10.

Default-DOM-Pfad, `zones.slice(0, 900)`:

| | Wert |
|--|------|
| Zeit der 900 Zonen | **2026-09-15T22:00:00Z – 2026-09-15T22:20:00Z** |
| Nach Min-Depth 7 | 4 Zonen, keine mit Depth unter 7 |
| Zonen des sichtbaren 16.–17.09. in diesem Cap | keine |

15m PD1, derselbe Adapter, Slot 15: Merge 803. Min 1/3/5/7/10 = 803 / 460 / 147 / 24 / 0, jeweils 0 unter der Schwelle. Die ersten 900 Rohzonen liegen auf **2026-09-15 22:00Z–23:00Z**. Dieselbe Cap-Funktion, aber eine 15m-Spalte ist 900s, deshalb reicht das Budget für eine Stunde statt für 20 Minuten.

---

## 4. Min-Depth 7, an den Filter übergebene Segmente

68 Segmente, alle `metadata.depth` 7, 8, 9 oder 10. Keines mit Depth kleiner als 7. Der Draw-Loop wurde nicht ausgeführt. Diese 68 wären die Eingabe von `setExactOriginDensity`, wenn der Canvas-Flag an ist.

Stichprobe, Unique-Origin-Count am Spaltenanfang gegen `metadata.depth`, und jede 5m-Stufe im Segment gegen denselben Sweep:

| Seite | Start | Ende | Unten | Oben | metadata.depth | Origins am Start |
|-------|-------|------|------:|-----:|---------------:|-----------------:|
| lower | 2026-09-16T17:35:00Z | 17:40Z | 0.078716 | 0.07873 | 7 | 7 |
| lower | 2026-09-16T07:20:00Z | 07:25Z | 0.079818 | 0.07982 | 7 | 7 |
| lower | 2026-09-16T07:20:00Z | 07:25Z | 0.079834 | 0.07984 | 7 | 7 |

Alle 68 Segmente wurden stufenweise geprüft. 65 stimmen auf jeder Stufe mit `metadata.depth` überein. Drei Fälle scheitern am Preisabgleich des Prüfers, nicht an einer Depth unter 7: eine bande mit Höhe nahe 0 (`0.08131`–`0.08131000000000001`, Depth 7), eine Folgezone 08:05Z und ein langes Upper-Band `0.10098`–`0.101038` ab 15.09. 22:00Z. In der Filterliste steht bei allen dreien Depth 7. Sie wären bei Min-Depth 7 zulässig, nicht ein Verstoß.

Der Merge verbindet nur Spalten gleicher Depth. Er hebt eine Depth-5-Spalte nicht auf 7. Die frühere 300s-Lücke kann ein Depth-7-Band über eine fremde Spalte malen. Im aktuellen Adapter ist diese Lücke für berührende Spalten geschlossen. Sie erklärt nicht, warum bei Min-Depth 7 weiterhin ein dichtes Bild über den ganzen Tag stehen bleibt.

---

## 5. Vergleich mit 15m

| Stück | 15m PD1 | 5m PD1 |
|-------|---------|--------|
| Slider-DOM, Handler, Storage-Key | dasselbe | dasselbe |
| `filterSegmentsByMinOrigins` | nach dem Merge | nach dem Merge |
| Redraw | `setExactOriginDensity` oder DOM-`syncOverlays` | derselbe Zweig |
| Cache | Pools je Scope, Segmente ungefiltert | dasselbe |
| Cap 900 auf dem Default-Pfad | erste Stunde des Vorlaufs | erste 20 Minuten des Vorlaufs |

Es gibt keine zweite Slider-Verdrahtung und keinen 5m-Sonderfilter. 5m verbraucht das 900-Zonen-Budget schneller, weil jede Spalte 300s statt 900s breit ist und mehr Bänder je Minute entstehen.

---

## 6. Kleinster Fix

Auf dem Nicht-Canvas-Zweig die Zonen zuerst mit `cullZonesToViewport` auf das sichtbare Fenster begrenzen und erst danach `slice(0, 900)` anwenden. Der Canvas-Zweig macht diese Reihenfolge bereits. `filterSegmentsByMinOrigins` und der Slider bleiben.

Solange der Flag aus ist, erreicht der korrekte Min-Depth-7-Satz von 68 Segmenten den sichtbaren 16.–17.09. nicht.

Nicht der Fix: eine neue Depth-Formel. Die ausgelieferte Formel zählt bereits eindeutige Origins, und die gefilterte Liste enthält keine Depth unter 7.

---

## 7. Nicht beobachtet

Kein Zugriff auf den Browser des Screenshots, dessen Viewport oder dessen `localStorage`. Ob der Canvas-Flag dort an ist, ist offen. Ist er an, gilt die Tabelle mit 68 Segmenten, und ein weiterhin volles Bild käme nicht aus dieser Filterliste. Die Script-URL im Research-HTML trägt noch `?v=phase6xt-density-min-depth-slider-v1`. Die Antwort von `:3000` ist der neue Adapter (`681b5f3a…`, kein `start_timestamp - 300`). Ein Browser, der diese URL nicht neu lädt, führt den alten Merge aus. Der Min-Depth-Filter ist in beiden Ständen derselbe.

## 8. STOP
