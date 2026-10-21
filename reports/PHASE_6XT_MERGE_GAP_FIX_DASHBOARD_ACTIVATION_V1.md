# PHASE 6XT – MERGE GAP FIX CONTROLLED DASHBOARD ACTIVATION V1

**Date:** 2026-10-21  
**VERDICT:** **`MERGE_GAP_FIX_DASHBOARD_ACTIVATED_PASS`**

Der Merge-Gap-Fix liegt nur im statischen Adapter. Das laufende Spread-Dashboard liefert diese Datei bereits aus. Kein Prozessneustart.

Keine ClickHouse-Writes, keine Imports, keine Publishes, keine Generatorläufe, kein Git-Commit.

Grundlage: `reports/PHASE_6XT_EXACT_ORIGIN_DENSITY_MERGE_GAP_FIX_V1.md`

---

## 1. Runtime

| | Wert |
|--|------|
| **Neustart notwendig** | **nein** |
| **PID vorher** | **1655845** |
| **PID nachher** | **1655845** (Start 2026-10-21 14:48:04 +0200) |
| **Listener** | ein Prozess, `0.0.0.0:3000` |
| **CWD** | `spread_recovery_hedge_short_dev/dashboard` |
| **PYTHONPATH** | Spread → Dashboard → Pools → TRP |
| **Env** | `HISTORICAL_LMV2_BATCH_READER=1` |
| **Apache :8080** | Master PID **795137**, Start 2026-10-20 00:05:55 +0200, unverändert |
| **Schreiber auf Adapter, Research-JS, chart.js** | keine |

Der Adapter ist kein Python-Modul. `StaticFiles` liest ihn bei jedem GET von Disk. Die Datei wurde um 15:21:33 +0200 geändert, nach dem Prozessstart. Der HTTP-Body ist bytegleich zur Disk-Datei.

| Datei | HTTP | SHA256 | Bedingung |
|-------|-----:|--------|-----------|
| `pd1_exact_density_trp_adapter.js` | 200 | `681b5f3aead840ce55de9f22d7656a5a08927bd15151a4c5856836bb6c9b4593` | `last.end_timestamp >= z.start_timestamp`, kein `start_timestamp - 300` |
| `pd1_exact_origin_density_research_v1.js` | 200 | `892f651d0c42187f6763fe2442346756192195fa287f6acf92bdc4ac963665cf` | `mergeExactOriginSegments` je Scope, danach `mergedSegments.concat` |

Aufrufpfad der ausgelieferten Skripte:

`poolsToExactOriginZones` → `cullZonesToViewport` → `mergeExactOriginSegments` → `filterSegmentsByMinOrigins` → `setExactOriginDensity` → `drawEodDensity`.

Keine Shadow-Kopie synchronisiert. `chart.js` und der Window-Builder wurden nicht angefasst.

---

## 2. Publish-Baseline (SELECT, `FINAL`)

| Line | Closes |
|------|-------:|
| 15m PD1 / PD5 / PD10 | **17568** / **17568** / **17568** |
| 5m PD1 / PD5 / PD10 | **2592** / **2592** / **2592** |

Fingerprints 2026-09-16 00:00Z unverändert: PD1 `50e16fc8…`, PD5 `4ef7e733…`, PD10 `ffd67753…`.

---

## 3. Ausgelieferte Merge-Bedingung

Dieselbe Datei, die `:3000` ausliefert, im Referenzfenster `2026-09-16T00:00:00Z`–`2026-09-17T23:55:00Z`:

| Prüfung | Ergebnis |
|---------|----------|
| 5m-Lücke Depth 5 / 1 / 5, Min-Depth 3 | zwei Rechtecke `0–300` und `600–900` |
| Dieselbe Lücke mit der alten 300s-Regel | ein Rechteck `0–900` |
| Berührende 5m-Spalten | ein Rechteck `0–600` |
| Überlappung | ein Rechteck `0–600` |
| 15m-Spaltenbreite 900s | identisch zur alten Regel |

Ungefilterte Merge-Zahlen, gleiche Pools und derselbe Sweep wie im Fix-Bericht:

| Scope | Merge | Min 1 | Min 2 | Min 3 | Min 5 |
|-------|------:|------:|------:|------:|------:|
| 5m PD1 | **2091** | 2091 | 1480 | 920 | 287 |
| 5m PD5 | **1206** | 1206 | 556 | 228 | 40 |
| 5m PD10 | **876** | 876 | 205 | 39 | 2 |
| Summe | **4173** | | | | |

4173 = 2091 + 1206 + 876. Fehlende Keys je PD bei Min 1, 2, 3 und 5: 0. Eine PD-übergreifende Merge-Funktion würde auf 4018 verkürzen und wird im Render-Pfad nicht aufgerufen. Gemalte Löcher nach dem Fix: 0.

---

## 4. Live-API, sechs Scopes

`GET /live-charts/viewer/api/capabilities`: **6** Scopes. Kein Fallback.

Fenster 16.–17.09.2026 23:55Z, `POST /live-charts/viewer/api/window`:

| Scope | HTTP | Preise | Pools | Slot | Line |
|-------|-----:|-------:|------:|-----:|------|
| 15m PD1 | 200 | 576 | 285 | 15 | `…_15mpd1_v1` |
| 15m PD5 | 200 | 576 | 311 | 15 | `…_15mpd5_v1` |
| 15m PD10 | 200 | 576 | 410 | 15 | `…_15mpd10_v1` |
| 5m PD1 | 200 | 576 | 599 | 5 | `…_5mpd1_v1` |
| 5m PD5 | 200 | 576 | 692 | 5 | `…_5mpd5_v1` |
| 5m PD10 | 200 | 576 | 913 | 5 | `…_5mpd10_v1` |

4h PD10, 15m PD3 und 30m PD1 bleiben HTTP 422 `INVALID_SCOPE`.

15m-PD1-Geometrie gegen die alte Merge-Regel, fünf Fenster (September-Referenz, April-Anfang, Monatsgrenze 30.06.–01.07., Korpusende, 97er-Regression): Fingerprints und Min-Depth-Tabellen gleich, Brücken 0. Test `test_15m_pd1_geometry_unchanged_on_several_windows` bestanden.

---

## 5. Ausstehend

Keine Browser-Sichtprüfung. Eine bereits geöffnete Seite behält das vorher geladene Skript bis zum Neuladen. Der Python-Prozess musste dafür nicht neu starten.

## 6. STOP

Keine weitere Änderung.
