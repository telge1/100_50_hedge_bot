# PHASE 6XT — EXACT ORIGIN CANVAS RUNTIME ERROR HOTFIX V1

**Date:** 2026-10-21  
**VERDICT:** `EXACT_ORIGIN_CANVAS_RUNTIME_ERROR_FIXED`

**Symptom:** Research UI red status: `Exact Origin: planned is not defined` (DOGEUSDT, 5m, B Exact Origin-Density, PD1, Min-Depth 6, Canvas default on).

---

## 1. Reproduktion / Ursache

| Frage | Antwort |
|-------|---------|
| **Ausgelieferte JS** | `phase6xt-exact-origin-canvas-planned-hotfix-v1` (nach Fix); vorher `phase6xt-4h-pd10-strict-viewer-v1` mit Bug |
| **Wer wirft?** | `refreshPane()` in `pd1_exact_origin_density_research_v1.js` beim Bau von `lastMetrics` **nach** erfolgreichem Window-Fetch und Canvas-Segment-Merge |
| **Wann?** | Initial render / Redraw auf **Canvas-Pfad** (`isCanvasPathEnabled()`), sobald Metriken geschrieben werden — nicht beim Slider allein |
| **Root cause** | Multi-Scope-Refactor: Window-Plan liegt in `scopePlans[].planned`, aber Metrik `windowsPlanned: planned.length` referenzierte eine **nicht existierende** globale `planned` (Überbleibsel Single-Scope) |
| **research_charts.js** | Kein freies `planned`; nur `*_planned` Payload-Felder — **unbeteiligt** |
| **Canvas vs. Legacy** | Fehler nur im gemeinsamen Metrik-Block am Ende von `refreshPane` (Canvas **und** TRP würden dort scheitern); Canvas-Default macht den Pfad häufiger sichtbar |

**Betroffene Zeile (vorher):** `windowsPlanned: planned.length` (~914).

**Korrekte Quelle:** Summe `sp.planned.length` über `scopePlans`.

---

## 2. Minimaler Fix

**Datei:** `spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js`

```javascript
windowsPlanned: scopePlans.reduce(function (sum, sp) {
  return sum + (sp.planned ? sp.planned.length : 0);
}, 0),
```

- Keine Änderung an Sweep, Merge, Depth, API, Geometrie, `fetchPlannedWindows(planned, …)` (Parameter dort korrekt).
- `VERSION` → `phase6xt-exact-origin-canvas-planned-hotfix-v1` (Cache-Buster).

**Tests (VERSION-String):** `test_density_pd_checkbox_ui_v1.py`, `test_pd1_density_window_422_hotfix_v1.py`, `test_5m_multi_pd_density_integration_v1.py`, `test_4h_pd10_strict_viewer_integration_v1.py`.

---

## 3. Regression (automatisiert)

| Check | Ergebnis |
|-------|----------|
| `node --check` pd1_exact_origin_density_research_v1.js | **OK** |
| Kein `windowsPlanned: planned.length` auf Disk | **OK** |
| JS-Struktur / VERSION Tests | **7 passed** (Checkbox, 422 harness VERSION, 4h scope matrix, …) |
| `ReferenceError: planned` | **behoben** (statische Referenz entfernt) |

**Manuell ausstehend (wie bei früheren Aktivierungen):** 5m/15m/4h PD-Matrix, Min-Depth 1–10, Checkbox-Wechsel, Zoom/Pan im Browser.

**Canvas-Default:** `localStorage[phase6xt_eod_density_canvas_v1] !== "0"` — unverändert (**opt-out**, Default **an**).

**Rekursive Verkabelung:** Kein neuer Hook; Fehler war **ReferenceError**, keine Rekursion.

---

## 4. Aktivierung

| Port | Served hotfix | `scopePlans.reduce` |
|------|---------------|---------------------|
| **3000** | ja | ja |
| **8080** | ja (Proxy/Static) | ja |

**Kein Dashboard-Neustart** (nur statisches JS; FastAPI `StaticFiles` liefert aktuelle Datei).

---

## 5. Grenzen

- Volle visuelle Canvas-Parität / 900-Rohzonen-Pfad: **nicht** Teil dieses Hotfixes.
- CH / Publish / Generator: **unberührt**.

**STOP**
