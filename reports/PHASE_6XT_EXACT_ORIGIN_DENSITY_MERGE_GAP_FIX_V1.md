# PHASE 6XT – EXACT ORIGIN-DENSITY MERGE GAP FIX V1

**Date:** 2026-10-21  
**VERDICT:** **`MERGE_GAP_FIX_READY_FOR_ACTIVATION`**

Keine ClickHouse-Writes, keine Publishes, keine Snapshots, keine Generatorläufe, kein Dashboard-Neustart, kein Git-Commit.

Grundlage: `reports/PHASE_6XT_5M_MIN_DEPTH_SLIDER_PARITY_AUDIT_V1.md` (`5M_MIN_DEPTH_SLIDER_DEFECT_CONFIRMED`).

---

## 1. Ursache

`poolsToExactOriginZones` setzt das Spaltenende auf den Start der nächsten Spalte.

- 5m: eine Spalte ist 300s. `end_timestamp ==` nächster `start_timestamp`.
- 15m: drei 5m-Schritte, Spalte 900s, dieselbe Berührung.

`mergeExactOriginSegments` hat mit `end >= start - 300` auch eine fehlende 5m-Spalte überbrückt. Die Sortierung legt gleiche Tiefen zusammen, deshalb verbindet diese Lücke zwei Bänder gleicher Tiefe über die Spalte hinweg, die der Min-Depth-Filter danach entfernen soll. Bei 15m ist eine Spalte 900s. Dieselbe Toleranz überbrückt sie nicht.

Der Filter, der Slider und `drawEodDensity` waren nicht die Fehlerstelle. Reihenfolge bleibt:

`poolsToExactOriginZones` → `cullZonesToViewport` → `mergeExactOriginSegments` → `filterSegmentsByMinOrigins` → `setExactOriginDensity` → `drawEodDensity`.

---

## 2. Änderung

Eine Bedingung in `spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_density_trp_adapter.js`:

`last.end_timestamp >= z.start_timestamp - 300`  
wird zu  
`last.end_timestamp >= z.start_timestamp`.

Berührung (`end == start`) und Überlappung (`end > start`) werden weiter zusammengeführt. Eine Lücke von 300s nicht mehr.

SHA256 des Adapters: `4b1e26ce…` → `681b5f3aead840ce55de9f22d7656a5a08927bd15151a4c5856836bb6c9b4593`.  
`chart.js` bleibt `8ba510e8…`. `historical_window_v1.py` bleibt `4e12bede…`.

Nicht geändert: Sweep, Depth-Formel, Min-Depth-Filter, Slider, Canvas, 48h-Tiles, Cache-Keys, Publish-Lines, Original-Origins und Bounds. Der Render-Pfad konkateniert weiter pro PD (`mergedSegments.concat`) und ruft keine PD-übergreifende Merge-Funktion auf.

Neue Tests: `test_exact_origin_density_merge_gap_fix_v1.py` und `test_exact_origin_density_merge_gap_fix_v1.js`. Der Byte-Lock in `test_15m_multi_pd_minimal_viewer_release_v1.py` zeigt auf den neuen Adapter und verlangt die Berührungsbedingung. Die Locks von `chart.js` und dem Window-Builder sind dieselben.

---

## 3. Vorher / nachher, 5m

Fenster `2026-09-16T00:00:00Z`–`2026-09-17T23:55:00Z`. Rohzonen unverändert (Sweep unangetastet). „Vorher“ ist die alte 300s-Regel als Oracle im Test, „Nachher“ der Produktionsadapter.

| Scope | Roh | Vorher Merge | Nachher Merge | Überbrückte Lücken | Löcher im Merge vorher | nachher |
|-------|----:|-------------:|--------------:|-------------------:|-----------------------:|--------:|
| 5m PD1 | 150 685 | 2080 | **2091** | 11 | 11 | **0** |
| 5m PD5 | 100 390 | 1201 | **1206** | 5 | 4 | **0** |
| 5m PD10 | 85 981 | 876 | **876** | 0 | 0 | **0** |

Rechtecke nach Min-Depth (Upper / Lower):

| Scope | Min 1 | Min 2 | Min 3 | Min 5 | Min 10 |
|-------|------:|------:|------:|------:|-------:|
| PD1 vorher | 2080 | 1473 | 916 | 287 | 2 |
| PD1 nachher | **2091** (979/1112) | **1480** (675/805) | **920** (444/476) | 287 (178/109) | 2 (1/1) |
| PD5 vorher | 1201 | 556 | 228 | 40 | 0 |
| PD5 nachher | **1206** (663/543) | 556 (342/214) | 228 (164/64) | 40 (34/6) | 0 |
| PD10 | 876 (450/426) | 205 (119/86) | 39 (26/13) | 2 (1/1) | 0 |

PD10 ist bytegleich zur alten Regel. Bei PD5 liegen die fünf Lücken in Depth-1-Bändern, deshalb ändern sich Min 2, 3, 5 und 10 nicht. Bei PD1 ändern sich Min 1, 2 und 3. Min 5 und 10 bleiben.

Synthese, Depth 5 / 1 / 5, Min-Depth 3:

- 5m-Spalten (300s): zwei Rechtecke `0–300` und `600–900`. Die alte Regel malte `0–900`.
- Direkt berührend `0–300` plus `300–600`: ein Rechteck `0–600`.
- Überlappend: ein Rechteck `0–600`.
- 15m-Spalten (900s): vorher und nachher dieselben zwei Rechtecke mit Loch.

---

## 4. 15m-PD1-Parität

Dieselbe Geometrie wie die alte 300s-Regel. Brücken 0, Fingerprints gleich, Filtertabellen einschließlich Upper/Lower gleich. Line `…_15mpd1_v1`, Slot 15.

| Fenster | Preise | Pools | Merge | Min 1 / 2 / 3 / 5 / 10 | SHA256 |
|---------|-------:|------:|------:|------------------------|--------|
| 16.–17.09. 23:55Z | 576 | 285 | 803 | 803 / 662 / 460 / 147 / 0 | `ffa918d3…` |
| 01.–02.04. | 574 | 355 | 899 | 899 / 601 / 367 / 106 / 7 | `431eb794…` |
| 30.06. 12:00Z–01.07. 12:00Z | 289 | 281 | 558 | 558 / 365 / 217 / 64 / 0 | `60db2e6f…` |
| 29.–30.09. | 574 | 290 | 749 | 749 / 505 / 313 / 78 / 0 | `e55a6daf…` |
| Regression 14.09. 21:45Z–15.09. 05:45Z | 97 | 182 | 362 | 362 / 271 / 188 / 71 / 0 | `f8c8333b…` |

---

## 5. Multi-PD

5m PD1+PD5+PD10, nach dem Fix, gemeinsames y-Fenster:

- Concat **4173** = 2091 + 1206 + 876. Keine verlorenen Segmente (`missing` 0 bei Min 1, 2, 3, 5, 10).
- `mergeExactOriginSegmentLists` über alle drei PDs würde auf 4018 verkürzen. Der Render-Pfad ruft das nicht auf.
- Jeder Pool im Pilot hat eine Origin. PD1: 599 Pools, 594 verschiedene Origins. Das ist der Bestand, kein Verlust durch den Merge.

---

## 6. Tests

`test_exact_origin_density_merge_gap_fix_v1.py`: **3 passed**.

Mitgelaufen und bestanden: Min-Depth-Filter-Harness, Canvas-Integration (15m-Fixture, Default-Slot 15), Cache-/Tile-Identität, 5m-Multi-PD-Wiring, 15m-Release-Matrix einschließlich der neuen Adapter-Prüfsumme, 15m-Referenzfenster der Viewer-Release-Suite.

Drei ältere Tests erwarten noch die Version-Strings `phase6xt-density-min-depth-slider-v1` und `phase6xt-density-fixed-tile-integration-v1`. Diese Strings stehen seit der späteren Viewer-Freigabe nicht mehr in den Dateien. Diese Phase hat die Dateien nicht angefasst und die Asserts nicht gelockert.

---

## 7. Risiken

Kein Browser-Zug. Eine schon geöffnete Seite behält das alte Skript bis zum Neuladen. Der Python-Prozess auf Port 3000 wurde nicht neu gestartet. StaticFiles liest den Adapter von Disk.

Die sichtbare Änderung im September-Pilot sind 11 PD1-Rechtecke und 5 PD5-Rechtecke, die nicht mehr über eine leere 5m-Spalte gezogen werden. PD10 und 15m PD1 bleiben in den geprüften Fenstern gleich.

## 8. STOP

Kein Runtime-Reload, kein Commit.
