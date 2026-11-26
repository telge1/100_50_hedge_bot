# MC-POOL-P4B2 — Controlled Browser Activation

**Status:** `MC_POOL_P4B2_ACTIVE_BROWSER_TEST_READY` (HTTP static JS verified; HTML `?v=` auf Disk; siehe Template-Cache-Hinweis)  
**Datum:** 2026-11-26  
**SSOT Live-CWD:** `spread_recovery_hedge_short_dev/dashboard/`  
**Kein Commit · kein Push · kein Service-Restart durchgeführt**

---

## 0. Scope dieser Aktivierung

Nur:

1. Asset-Version-Bump in `templates/research_charts.html` (Default-`{% else %}`-Zweig, zwei Script-Zeilen).
2. P4B/P4B1-JS liegt bereits auf der produktiven Static-Pfad-Quelle (gleiches Verzeichnis wie `dashboard.service`).

Keine weiteren funktionalen Änderungen in dieser Phase.

---

## 1. Produktive JS-Pfade

| Rolle | Pfad (relativ zu Dashboard-Root) |
|-------|----------------------------------|
| Exact Origin | `static/js/research/pd1_exact_origin_density_research_v1.js` |
| Research UI | `static/js/research/research_charts.js` |
| Template | `templates/research_charts.html` (nur Default-Zweig, keine `?pd1=` Query-Overrides) |

Alternate Template-Zweige (`mc4v_f3`, `mc6a`, …) **unverändert** — für P4B-Browser-Tests **kein** `?pd1=…` Query verwenden.

---

## 2. Prüfsummen (SHA-256, Bytes)

| Datei | SHA-256 | Bytes |
|-------|---------|------:|
| `pd1_exact_origin_density_research_v1.js` | `e5073ab84a11030ef7dba8f13ad982462fb87a24f8e6bc6b409bf9148f0863e4` | 85581 |
| `research_charts.js` | `881aca0d70cc8a5b20725cbd1a0f109df4ce23eea4b612e7659b7ed8dc6824b6` | 534914 |

**HTTP ↔ Disk:** `curl http://127.0.0.1:3000/static/js/research/{pd1,research_charts}.js` liefert dieselben SHA-256 (200 OK).

---

## 3. Asset-Versionen

| Script | Vor P4B2 | Nach P4B2 (Disk) |
|--------|----------|------------------|
| `pd1_exact_origin_density_research_v1.js` | `?v=pd1-mc-multi-pane-pools-v5` | `?v=pd1-mc-pool-p4b-v1` |
| `research_charts.js` | `?v=research-mc-load-p4i-v1` | `?v=research-mc-pool-p4b-v1` |

Template-Zeilen (Spread SSOT):

```758:763:spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html
    <script src="/static/js/research/pd1_exact_origin_density_research_v1.js?v=pd1-mc-pool-p4b-v1"></script>
    ...
    <script src="/static/js/research/research_charts.js?v=research-mc-pool-p4b-v1"></script>
```

### 3.1 Template-Cache (wichtig)

`app.py` nutzt `jinja_env = Environment(loader=FileSystemLoader(...))` **ohne** `auto_reload`. Bereits gerenderter `research_charts.html` kann im Prozess gecacht sein.

- **Static JS** ist sofort die P4B-Implementierung (HTTP verifiziert).
- **Neue `?v=` in HTML** erscheint erst nach **hartem Reload mit frischem HTML** — typischerweise **`systemctl restart dashboard.service`** (oder Neustart des laufenden Uvicorn/Workers).

**Nicht durchgeführt** (per Auftrag). Vor manuellem Test:

1. Entweder kurzer Dashboard-Restart, **oder**
2. Im Browser „View Source“ prüfen, ob `pd1-mc-pool-p4b-v1` / `research-mc-pool-p4b-v1` in den Script-Tags steht; falls nein → Restart melden/ausführen.

`GET /live-charts/research` per `curl` → **401 Not authenticated** (erwartet); Asset-`?v=` im HTML bitte eingeloggt im Browser prüfen.

---

## 4. HTTP-Symbol-Verifikation (ohne Auth)

`grep`-Treffer in ausgelieferten Bodies (`127.0.0.1:3000`):

| Symbol | `pd1_exact_origin_density_research_v1.js` | `research_charts.js` |
|--------|----------------------------------------:|---------------------:|
| `stripPaneVisual` | 4 | 2 |
| `stripAllPanesVisual` | 6 | 0 |
| `refreshPaneAfterReEnable` | 3 | 2 |
| `refreshAllAfterReEnable` | 3 | 0 |
| `exactOriginReenableFreshnessBaselineKnown` | 3 | 0 |

Export in `PD1ExactOriginResearch` (pd1-Datei, u. a. L2394–L2404).

---

## 5. Selektiver Rollback

### 5.1 Nur Cache-Bust rückgängig (minimal)

In `spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html` (Default-Zweig):

```html
<script src="/static/js/research/pd1_exact_origin_density_research_v1.js?v=pd1-mc-multi-pane-pools-v5"></script>
<script src="/static/js/research/research_charts.js?v=research-mc-load-p4i-v1"></script>
```

→ ggf. Dashboard-Restart wie oben.

### 5.2 P4B/P4B1-JS vollständig zurück

| Datei | Rollback-Quelle |
|-------|-----------------|
| `pd1_exact_origin_density_research_v1.js` | `pd1_exact_origin_density_research_v1.js.bak-20261122T092540Z` (älterer Stand; **nicht** P4B) |
| `research_charts.js` | `research_charts.js.bak_bt_d12c_20261025` oder Git/Backup eurer Wahl |

P4B-Stand **ohne** separates File-Backup außerhalb Git: SHA oben als Referenz; Rollback nur wenn bewusst gewünscht.

**Unrelated dirty changes:** Nur `templates/research_charts.html` (2 Zeilen) in P4B2 geändert — alle übrigen lokalem Dirty bleibt unangetastet.

---

## 6. Browser-Test — Voraussetzungen

**Setup:** `/live-charts/research`, SOLUSDT, Single Chart (oder Multi für B5), Pane-TF **5m**, Exact Origin **an**, Pool-TF **5m**, **PD1**, Historical LMv2 **aus**.

**Harness laden** (Inhalt aus Repo):

`pools+ob+delta_bot/research/mc_pool_p4a/MC_POOL_P4A_BROWSER_MEASUREMENT_CONSOLE.js`

**B1 Equality (optional, minimal):**

`pools+ob+delta_bot/research/mc_pool_p4b/MC_POOL_P4B2_B1_EQUALITY_SNAPSHOT.js`

---

## 7. Kopierbare Befehle — Session-Start

```javascript
// 1) P4A Harness einfügen (ganze Datei MC_POOL_P4A_BROWSER_MEASUREMENT_CONSOLE.js), dann:
__MC_POOL_P4A__.start({ auto_section_a: false });
__MC_POOL_P4A__.help();

// 2) Optional B1 Equality:
// MC_POOL_P4B2_B1_EQUALITY_SNAPSHOT.js einfügen, dann:
__MC_POOL_P4B2_EQ__.help();
```

Vor **jeder** manuellen UI-Aktion: `__MC_POOL_P4A__.userClick();`

---

## 8. Tests B1–B5 (P4B-spezifisch)

### B1 — OFF→ON, unverändertes Datenstand & Viewport + Equality-Vorbereitung

```javascript
__MC_POOL_P4A__.userClick();
__MC_POOL_P4A__.startSection("P4B_B1", "off_on_static_viewport", { interaction: "user" });
// Warten bis Pools stabil (chunk_stores > 0, status ready)

// Optional: Referenz nach kanonischem Refresh (einmalig, USER-gesteuert):
// var p = __researchDebug.panes["pane-0"];
// __MC_POOL_P4A__.userClick(); await PD1ExactOriginResearch.refreshPane(p, { force: true });
// var ref = __MC_POOL_P4B2_EQ__.fingerprint("ref_after_force");

__MC_POOL_P4A__.sampleViewport("B1_before_off");

__MC_POOL_P4A__.userClick();
// UI: Exact Origin Haken AUS
__MC_POOL_P4A__.sampleViewport("B1_off");

__MC_POOL_P4A__.userClick();
// UI: Exact Origin Haken wieder AN — Cache-Repaint-Pfad
// Erwartung P4B: pool_window_started in Section oft 0 wenn fullyCached + segMatch + freshnessKnown
await new Promise((r) => setTimeout(r, 800));
__MC_POOL_P4A__.sampleViewport("B1_after_on");

// var cand = __MC_POOL_P4B2_EQ__.fingerprint("cand_after_reenable");
// __MC_POOL_P4B2_EQ__.compare(ref, cand);  // equal:true erst nach manueller Bewertung — kein Auto-PASS

__MC_POOL_P4A__.endSection({ test: "P4B_B1" });
```

**Equality:** `pool_ids`, `segment_sig`, `renderKey`, ausgewählte `metrics` — nicht nur `pools`-Count.

---

### B2 — OFF→ON nach neuer bestätigter 5m PD1-Publication während OFF

```javascript
__MC_POOL_P4A__.userClick();
__MC_POOL_P4A__.startSection("P4B_B2", "off_new_5m_publication", { interaction: "user" });
__MC_POOL_P4A__.sampleViewport("B2_baseline_on");

__MC_POOL_P4A__.userClick();
// Exact Origin AUS
__MC_POOL_P4A__.sampleViewport("B2_off");

// Keine UI — bis nächster 5m-Close / sichtbare Live-Erweiterung (max ~6 min)
// Section bleibt OFF; __MC_POOL_P4A__ protokolliert live_status/candles getrennt von pool_window

__MC_POOL_P4A__.userClick();
// Exact Origin AN
await new Promise((r) => setTimeout(r, 1200));
__MC_POOL_P4A__.sampleViewport("B2_after_on");

__MC_POOL_P4A__.endSection({ test: "P4B_B2" });
```

**Auswertung B2 (manuell):**

- Nach ON: in Export `sections[].events` / `http_log` nach `pool_bounds` oder bounds-URL (`multicoin-5m-pd1-viewer-bounds` / `doge-5m-…`).
- **Fail-closed:** `ensurePaneViewerBounds` macht bei `!bRes.ok` **kein** `applyDoge5mViewerBoundsToCaps` → kein bestätigtes `data_available_to`.
- `exactOriginReenableFreshnessBaselineKnown(pane)` erfordert `pane.exactOriginStripCapsHiMs != null` (bei OFF gesetzt). Ohne Baseline **kein** blindes `repaintPaneFromCache`.
- `hiAdvanced` nur wenn neues `bounds.hiMs` **>** gespeichertes `stripCapsHiMs`.

Diagnose-Konsole (read-only):

```javascript
(function () {
  var p = __researchDebug && __researchDebug.panes && __researchDebug.panes["pane-0"];
  var R = window.PD1ExactOriginResearch;
  if (!p || !R) return console.warn("no pane/R");
  console.log({
    stripCapsHiMs: p.exactOriginStripCapsHiMs,
    freshnessKnown: R.exactOriginReenableFreshnessBaselineKnown(p),
    segCacheKey: p.exactOriginSegmentCache && p.exactOriginSegmentCache.key,
  });
})();
```

---

### B3 — OFF→ON nach Viewport-Verschiebung während OFF

```javascript
__MC_POOL_P4A__.userClick();
__MC_POOL_P4A__.startSection("P4B_B3", "off_pan_then_on", { interaction: "user" });
__MC_POOL_P4A__.userClick();
// Exact Origin AUS
__MC_POOL_P4A__.userClick();
// Chart langsam horizontal verschieben (Kerzen/Viewport ändern, Pools bleiben aus)
__MC_POOL_P4A__.sampleViewport("B3_after_pan_off");
__MC_POOL_P4A__.userClick();
// Exact Origin AN
await new Promise((r) => setTimeout(r, 800));
__MC_POOL_P4A__.sampleViewport("B3_after_on");
__MC_POOL_P4A__.endSection({ test: "P4B_B3" });
```

Erwartung dokumentieren: Viewport-Shift kann `renderKey` / `fullyCached` ändern → ggf. selektive Window-POSTs, aber kein voller Store-Wipe wie altes `stripPane`.

---

### B4 — Schnelles mehrfaches OFF→ON (+ OFF während Request)

```javascript
__MC_POOL_P4A__.userClick();
__MC_POOL_P4A__.startSection("P4B_B4", "rapid_off_on", { interaction: "user" });
__MC_POOL_P4A__.userClick();
// Optional erst ON + slow pan starten, dann sofort OFF während Spinner/Request:
// Exact Origin AUS → AN → AUS → AN (schnell, innerhalb ~2–3 s)
await new Promise((r) => setTimeout(r, 1500));
__MC_POOL_P4A__.sampleViewport("B4_after_rapid");
__MC_POOL_P4A__.endSection({ test: "P4B_B4" });
```

Auswertung: `pool_window_aborted`, `load_serial` in `chunk_stores` / Pane-State monoton, keine Zombie-Canvas ohne Daten.

---

### B5 — Multi-Pane-Isolation

```javascript
__MC_POOL_P4A__.userClick();
__MC_POOL_P4A__.startSection("P4B_B5", "multi_pane_isolation", { interaction: "user" });
// Multi-Manual-Layout, beide Panes 5m + EO-fähig (z. B. SOLUSDT)
// Nur Pane A: Exact Origin OFF → ON; Pane B: EO durchgehend AN
__MC_POOL_P4A__.sampleViewport("B5_end");
__MC_POOL_P4A__.endSection({ test: "P4B_B5" });
```

Prüfen: `snapshotSettings().chunk_stores` — betroffenes Pane `chunk_entries` bleibt bei OFF > 0 (P4B); Nachbar-Pane unverändert.

---

## 9. Session-Abschluss

```javascript
__MC_POOL_P4A__.exportJSON();
__MC_POOL_P4A__.stop();
```

---

## 10. Explizit nicht behauptet

- **Kein** `Equality-PASS`
- **Kein** `Performance-PASS`
- **Kein** P4B-Freigabe ohne Ihre Browser-Nachmessung

---

## 11. Lokale Tests (vor Aktivierung, unverändert)

- `node static/js/research/test_mc_pool_p4b_cache_lifecycle_v1.js` → 13 PASS  
- `pytest` MC-LOAD (P4i/P4g/P4e/P3) → 11 PASS  

---

**Token:** `MC_POOL_P4B2_ACTIVE_BROWSER_TEST_READY`