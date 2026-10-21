# PHASE 6XT – 5M EXACT ORIGIN-DENSITY VIEWPORT CAP FIX V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_VIEWPORT_CAP_FIX_BLOCKED`**

Kein Code geändert. Die bestehende Grenze von 900 Rohzonen kann das Referenzfenster nach einem Viewport-Schnitt nicht vollständig zeichnen. Eine stillere Kürzung oder eine neue Auswahl der Zonen ist nicht zulässig.

Keine ClickHouse-Writes, keine Publishes, keine Imports, kein Dashboard-Neustart, kein Git-Commit.

Referenz: `reports/PHASE_6XT_5M_MIN_DEPTH_LIVE_CANVAS_ROOT_CAUSE_V2.md`

---

## 1. Renderpfade, vor jeder Änderung

Standard im Browser: **DOM-Overlays**. Der Canvas-Pfad ist aus, solange `localStorage["phase6xt_eod_density_canvas_v1"]` nicht exakt `"1"` ist. Denselben Schlüssel lesen `isCanvasPathEnabled` und `exactOriginDensityCanvasEnabled`.

| Pfad | Reihenfolge | Grenze |
|------|-------------|--------|
| DOM, Default | Sweep über den Load-Span inklusive 2h-Vorlauf → Rohzonen aneinanderhängen → `applyZoneCap` = `zones.slice(0, 900)` → `filterSegmentsByMinOrigins` → DOM-Overlays | 900, still, zeitliches Präfix |
| Canvas, Flag an | Sweep → `cullZonesToViewport` → `mergeExactOriginSegments` → Filter in `pushCanvasDensity` → `setExactOriginDensity` | 8000 Segmente. Darüber Abbruch mit Hinweis `PERF_BLOCK`, kein stilles `slice` |

`computeViewportCullBounds` erweitert das sichtbare Fenster um `max(24h, 8 % der Spanne)` und um einen Preisrand. Für das Referenzfenster ist das **2026-09-15 00:00Z–2026-09-18 23:55Z**. Der 2h-Vorlauf bleibt damit im Cull. Zoom und Pan rufen `scheduleViewport` → `refreshPane` auf und bauen den Sweep neu. Die 900er-Grenze bleibt das Präfix dieser neuen Liste.

Der Min-Depth-Filter, der Origin-Sweep, die Merge-Lückenregel und die 168h-Berechnung der Pools sind davon unabhängig. Der Viewport-Schnitt wäre nur Darstellung.

---

## 2. Zählung nach dem Viewport-Schnitt

DOGEUSDT, `2026-09-16T00:00:00Z`–`2026-09-17T23:55:00Z`. „Strikt“ heißt: Zone schneidet genau dieses Fenster, ohne den 24h-Cull-Rand. Dieselben Pools wie im Root-Cause-Audit.

| Scope | Roh | Nach Pad-Cull | Strikt im Fenster | Nach Merge, strikt | Davon > 900 |
|-------|----:|---------------:|------------------:|-------------------:|:-----------:|
| 5m PD1 | 150 685 | 150 685 | **144 910** | **2 078** | ja |
| 5m PD5 | 100 390 | 100 390 | 96 501 | 1 204 | ja |
| 5m PD10 | 85 981 | 85 981 | 82 651 | 875 | Roh ja, Merge nein |
| 5m PD1+5+10, Merge je PD, dann concat | — | — | — | **4 157** | ja |
| 15m PD1 | 52 015 | 52 015 | 50 071 | 796 | Roh ja, Merge nein |

Die ersten 900 strikten 5m-PD1-Rohzonen sind nur **2026-09-16 00:00Z–00:20Z**. Ein Cull vor `slice(0, 900)` ändert das Präfix nicht, weil der Pad-Cull hier 0 Zonen entfernt und auch der strikte Schnitt 144 910 Zonen lässt.

Min-Depth 7, 5m PD1, identischer Filter: **68** Segmente im Fenster, davon **3** in `2026-09-16T12:00:00Z`–`18:00:00Z`. Diese drei liegen nicht in den ersten 900 Rohzonen. Ein Präfix bis 00:20Z ist nicht der Darstellungsweg, der sie erreicht.

Auch ein Zoom auf genau diese 6 Stunden lässt **18 154** strikte Rohzonen. Die ersten 900 davon enden **12:20Z**. Nach dem Merge sind es in diesen 6 Stunden **476** Segmente, also unter 900, aber nur wenn vorher gemergt wird. Die heutige 900er-Grenze sitzt auf den Rohzonen, vor dem Merge.

Der Canvas-Pfad mit 8000 Segmenten fasst 5m PD1 (2 091 inklusive Pad-Merge) und die Dreier-Komposition (4 157). Ein Überschreiten bricht mit `PERF_BLOCK` ab und schneidet nicht still.

---

## 3. Architektur-Lücke

Die darstellungsrelevanten Zonen des Referenzfensters sind nach dem Schnitt mehr als 900, als Rohzonen und nach dem bestehenden Merge. Die 900er-Grenze kann sie nicht vollständig zeichnen.

Nicht umgesetzt, weil jede dieser Varianten die Vorgabe verfehlt:

- Cull und danach weiterhin `slice(0, 900)` lässt die drei Segmente von 12:00–18:00Z draußen und schneidet Upper/Lower nach 00:20Z still ab.
- Eine andere Auswahl unter 900 wäre eine neue Rangfolge.
- Den Canvas-Flag zum Default zu machen oder die 900er-Grenze anzuheben wäre eine andere Renderarchitektur, nicht die nachgewiesene Umordnung.

15m-PD1-Merge im selben Fenster: 796 Segmente, unter 900. Die Rohzonen sind 50 071. Die bestehende DOM-Grenze schneidet auch dort das Präfix, nicht die validierte Merge-Geometrie. Diese Geometrie wurde nicht angefasst.

---

## 4. Was unverändert bleibt

Origin-Sweep, Unique-Origin-Depth, `mergeExactOriginSegments` mit `end >= start`, `filterSegmentsByMinOrigins`, Slider, 48h-Tiles, PD-Caches, Multi-PD-Concat, Canvas-8000-Pfad, ACTIVE-SSOT, Origin-IDs, Upper/Lower, 168h-Lookback.

Runtime-Aktivierung: keine. Es gibt keine neue Datei, die das Dashboard laden müsste.

## 5. STOP
