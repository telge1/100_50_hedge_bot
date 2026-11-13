# DOGE-LIVE-P24F — 1h / 4h Pool-Bounds

**Erstellt:** 2026-11-13  
**Modus:** Phase A/B Read-only + minimaler Frontend-Fix (1h); 4h PD10 dokumentiert

**Kein Commit / Push / Restart**

---

## Executive Summary

| Pool-TF | UI-Scope | Live Nov 2026 | Ursache (Browser/Log) | Fix |
|---------|----------|---------------|------------------------|-----|
| **1h** | 1h **PD1** (released) | **Backend JA** | `loadCaps` lud **nur 30m** On-Demand-Bounds → `scopeCaps` → `capabilities_root_fallback` Apr–Sep → **NO_INTERSECTION** | **P24F:** 1h (+4h pd1) Bounds-GET in `loadCapsOnce` |
| **4h** | **4h PD10 only** (released) | **Publish Nein** | Frozen `historical_mixed_frozen_*_4hpd10_v1` Apr–Sep; On-Demand **nur PD1** | **Kein Fake-Bounds**; expliziter UI-Hinweis |

---

## Phase A — Unterstützung (getrennt)

### 1h — `1h_PD1_ON_DEMAND_SUPPORTED`

| # | Prüfung | Befund |
|---|---------|--------|
| 1 | Kanonische PD1-Engine | **Ja** — `on_demand_ch1m_1hpd1_v1` / CH 1m → 1h strict candles (`on_demand_ch1m_pd1_v1.py`) |
| 2 | On-Demand-Service | **Ja** — `ON_DEMAND_CH1M_SCOPES["1h"]`, `build_landscape_window_on_demand_ch1m_pd1_v1` |
| 3 | Pipeline | `on_demand_ch1m_1hpd1_v1` (≠ 30m, ≠ 5m publish) |
| 4 | 1m-Eingaben | **Ja** — `_ch_symbol_bounds()` / ClickHouse meta |
| 5 | Bounds bis November | **Ja** (API) — `doge_pd1_on_demand_viewer_bounds(timeframe="1h")` → CH high-water |
| 6 | Window-API | **Ja** — POST `timeframe: "1h"`, `pool_detail: 1`, `exact_origin_v1` |
| 7 | Exact-Origin-Density | **Ja** — gleicher JS-Pfad wie 30m; Planner 72h tiles |
| 8 | Geschlossene Kerzen | On-Demand engine: causal close / membership 60 min |

### 4h — Split PD1 vs PD10

**4h PD1 (On-Demand, Backend):**

| # | Befund |
|---|--------|
| Engine / Service / Pipeline | **Ja** — `on_demand_ch1m_4hpd1_v1`, `SCOPE_4H_PD1` |
| Bounds-API | **Ja** — `doge_pd1_on_demand_viewer_bounds(..., timeframe="4h")` |
| UI `RELEASED_VIEWER_SCOPES` | **Nein** — nur `{ timeframe: "4h", pool_detail: **10** }` |

**4h PD10 (UI-Standard):**

| # | Befund |
|---|--------|
| Datenquelle | **Frozen publish** — `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1` |
| On-Demand | `window_service`: **4h + pd≠1 → INVALID_SCOPE** / On-Demand nur **PD1** |
| Live November | **Nicht verfügbar** ohne Publisher-Daten; **kein** On-Demand für PD10 |

---

## Phase B — Nachweis 1h (Nutzer-Log)

```
scopeCaps path=capabilities_root_fallback
bounds=2026-04-01 → 2026-09-30   (capabilities root / frozen scope)
view=2026-11-08 → 2026-11-13     (P24D chart visible — korrekt)
buildWindows → NO_INTERSECTION
```

**Kette:**

1. `loadCapsOnce` fetchte nur `?timeframe=30m&pool_detail=1`.  
2. `_onDemand1hPd1ViewerBounds` **nie** gesetzt.  
3. `scopeCaps` für 1h/PD1: kein `on_demand_1h_pd1_patch` → `capabilities_scope_match` oder **root_fallback** mit Apr–Sep.  
4. `dataBoundsMsForScopes` clippt auf Sep; November-Viewport → **NO_INTERSECTION** → **keine** Window-Tiles.

**4h (inferiert, kein separater Nutzer-Trace):** Pool-TF **4h + PD10** → kein On-Demand-Merge → immer frozen Apr–Sep bei Live-Nov → gleiches NO_INTERSECTION-Muster.

---

## Phase C — Änderungen

**Nur Frontend** (`pd1_exact_origin_density_research_v1.js`):

- `ON_DEMAND_BOUNDS_FETCH_POOL_TFS = ["30m", "1h", "4h"]`  
- Loop in `loadCapsOnce`: `GET /api/research/doge-pd1-on-demand-viewer-bounds?timeframe=…&pool_detail=1`  
- Storage keys: `_onDemand1hPd1ViewerBounds`, `_onDemand4hPd1ViewerBounds`  
- `scopeCaps`: On-Demand-Patch für **4h PD1** (falls später freigegeben)  
- Hints: 1h wie 30m bei fehlenden Bounds; **4h PD10** → erklärender Text (kein Fake)

**Unverändert:** 5m publish, 30m pipeline/semantik, Backend-Engines, MC4.

**Cache-Bust:** `pd1-p24f-1h-4h-bounds-v1`

---

## Phase D — Tests

```text
pytest test_p24f_1h_4h_on_demand_bounds_v1.py \
       test_p24e_identical_viewport_coalesce_v1.py \
       test_p24d_on_demand_viewport_resolve_v1.py -q
```

| Szenario | Automatisiert | Backend | Browser |
|----------|---------------|---------|---------|
| 5m/5m, 30m/30m | Regression strings | PASS (unverändert) | **UNVERIFIED** |
| **1h/1h** | Bounds fetch 1h | **PASS** (API exists) | **UNVERIFIED** (erwartet: `on_demand_1h_pd1_patch`, kein NO_INTERSECTION) |
| **4h/4h PD10** | Hint + frozen doc | **N/A Nov live** | **UNVERIFIED** (Hinweis erwartet) |
| 4h PD1 (nicht in UI) | Bounds stored only | PASS | — |
| P24E coalesce | test_p24e | — | — |

---

## Dateien

- `pd1_exact_origin_density_research_v1.js`  
- `templates/research_charts.html`  
- `test_p24f_1h_4h_on_demand_bounds_v1.py` (neu)

---

## STOP

**1h:** technisch unterstützt; Fix = fehlende Bounds-Anbindung (P24F).  
**4h (UI PD10):** live November **nicht** unterstützt — kein simuliertes Bounds-Overlay.
