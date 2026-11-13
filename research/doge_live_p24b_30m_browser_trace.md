# DOGE-LIVE-P24B — 30m-Pool-Rendering (Browser-Trace & Request-Sturm)

**Erstellt:** 2026-11-13  
**Modus:** STRICT READ-ONLY (kein Code-Fix, kein Restart, kein Commit)  
**Vorgänger:** `doge_live_p24_30m_pool_timeframe_diagnostic.md` (`P24_BACKEND_30M_OK_…`)

**Ergebnis-Kürzel:** `P24B_RENDER_SUPERSEDE_STORM_NO_STABLE_PUSH`

---

## Schritt 1 — Browser-Test

| Punkt | Status |
|--------|--------|
| Authentifizierte UI-Session in Cursor-Browser (`/live-charts/research`) | **BLOCKED** — Seite ohne Inhalt (kein Login-Cookie; Screenshot leer). |
| `DogeExactOriginTrace` in dieser Session | **Nicht erfasst** (Trace default OFF; Konsole nicht verfügbar). |
| Proxy für authentifizierten Nutzer | **Journal PID 2379483**, Client-IP `197.186.72.177`, Zeitraum **15:30:24–15:30:50** (Pool-/Chart-TF-Wechsel 30m). |

**Nachweis statt Live-Browser:** Server-`window_trace` + Request-Zeitfenster + Codepfad-Analyse (gleicher Nutzer wie P24-Journal).

### Rekonstruiertes Szenario (Journal 15:30)

| Phase | HTTP | Inhalt |
|--------|------|--------|
| Caps | `GET …/capabilities` **200** | Root-Scopes |
| 5m-Bounds | `GET …/doge-5m-pd1-viewer-bounds` **200** | Publish-SSOT-Patch |
| 30m-Bounds | `GET …/doge-pd1-on-demand-viewer-bounds?timeframe=30m&pool_detail=1` **200** | On-Demand-CH-Bounds **mergable** |
| Window (30m) | **46×** `POST …/api/window` **200** | **2×** `cache_hit=False` (On-Demand-Compute ~2,2 s), **43×** `cache_hit=True` (~3–7 ms) |

**Identische Fenster (je Zyklus 2 Tiles, 72h-Planner):**

| Tile | `from_utc` | `to_utc` | Pools (Trace) |
|------|------------|----------|----------------|
| A | `2026-11-09T00:00:00Z` | `2026-11-11T23:55:00Z` | **193** |
| B | `2026-11-12T00:00:00Z` | `2026-11-13T14:25:00Z` | **149** |

→ **~23 Wiederholungen** derselben **2** Fenster (**≈46 POSTs** in ~26 s), keine variierenden `from`/`to` im Sturm.

**Body (unverändert, aus JS):** `timeframe: "30m"`, `pool_detail: 1`, `symbol: "DOGEUSDT"`, `response_mode: "exact_origin_v1"`, plus `from_utc`/`to_utc` der Tiles.

---

## Schritt 2 — Trace-Analyse (Code-Stufen vs. Datenfluss)

Aktivierung für manuelle Repro (nicht in dieser Session ausgeführt):  
`localStorage.setItem('DOGE_EXACT_ORIGIN_TRACE','1')` → Konsole: `TRACE | id=… | stage=…` (`doge_exact_origin_trace_v1.js`).

### Pipeline-Tabelle

| Stufe | 5m (Referenz) | 30m (Fehlerfall) | Daten an nächste Stufe? (Journal + Code) |
|-------|---------------|------------------|------------------------------------------|
| **loadCaps** | 5m-Bounds-Patch | + On-Demand-30m-GET | **Ja** — alle **200** (15:30:25/38). |
| **On-Demand-Merge** | — | `_onDemand30mPd1ViewerBounds` → `scopeCaps` | **Ja** — sonst kein Nov-Fenster; Window-POSTs beweisen Nov-Schnitt. |
| **resolveViewSec** | `hist_viewport` / candles @ 300s | Chart=Pool=30m: oft Pilot-Cutoff → Fallback `chart_visible_*` | **Ja** — Fenster Nov 2026 in POSTs. |
| **buildWindows** | 5m-Sliding-Tiles | `fixed48h` / **72h** → **2** geplante Fenster | **OK** (kein `NO_INTERSECTION` im Server-Log). |
| **fetchPlannedWindows** | … | 2 Worker, maxConcurrent=3 für 30m | **Ja** — `fetched`+Cache; Server **149–193** Pools. |
| **fetchStats** | stabilere Zyklen | viele Zyklen, meist Cache-Hits | Pools im Store (implizit via erneute 200er). |
| **refreshPane_coalesced** | seltener | **Erwartet häufig** bei TF-Wechsel | Serial-Bump s. unten. |
| **_eoRefreshGate** | coalescing | `inflight` + parallele `viewport`/`force` | Pending-Kette bis Sturm endet. |
| **loadSerial check** | … | `loadSerial_mismatch_after_fetch` / `_before_render` | **Render-Abbruch** → kein `pushCanvasDensity` in diesem Lauf. |
| **pushCanvasDensity** | nach stabilem Lauf | oft **übersprungen** | **INFERRED** — Sturm + `stripAllPanes` → leere Canvas. |
| **mergedSegments** | >0 | **Nicht in Browser gemessen**; Server-Pools **>0** | Segmente **nicht verifiziert**; Adapter sollte bei Pools >0 liefern. |

**Kritischer Code-Punkt (Render-Abbruch):**

```1769:1780:spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js
    if (loadSerial !== pane.exactOriginLoadSerial) {
      // … trace async_superseded reason loadSerial_mismatch_before_render
      scheduleSupersededDensityRefresh(pane, opts);
      return;
    }
    // pushCanvasDensity nur darunter
```

**Serial-Bump bei Coalesce:**

```1382:1385:spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js
    if (gate.inflight) {
      if (eoRefreshPendingSupersedesInflight(gate.pending)) {
        bumpExactOriginLoadSerial(pane, "coalesced_viewport_or_force");
```

`viewport: true` in pending **supersedes** immer → jeder `on_visible_range` während inflight **invalidiert** den laufenden Lauf.

---

## Schritt 3 — Request-Sturm (42/46 POSTs)

### Auslöser (nachgewiesen / stark gestützt)

1. **Pool-TF 30m:** `change`-Handler → `stripAllPanes()` + `refreshAll({ force: true })` (`pd1_exact_origin_density_research_v1.js` ~2107–2116).
2. **Chart-TF 30m:** `onPaneTimeframeChange` → `stripAllPanes()` + `refreshPane({ force: true })` (~2126–2141).
3. **loadPane nach TF-Wechsel:** `research_charts.js` ~8560–8568 erneut `refreshPane({ force: true })`.
4. **Chart-Bridge:** `on_visible_range` → `scheduleViewport` (debounce **400 ms**) → `refreshPane({ viewport: true })` (~2229–2241).
5. **Kein Viewport-Suppress beim TF-Wechsel** — im Gegensatz zu **GO TO**, wo `_suppressExactOriginViewport` gesetzt wird (~1387–1405 `research_charts.js`).

### Warum dieselben Zeitfenster?

- Planner `exactOriginWindowPlannerForScope` → **`fixed48h` / 72h** für 30m → immer **dieselben 2 UTC-Tiles** bis Viewport-Hit (`win.hit`) wechselt.
- Während des Sturms ändert sich der sichtbare Bereich kaum → **20× Tile A + 20× Tile B**.

### Werden alte Requests verworfen?

- **Ja** — `exactOriginLoadSerial` / `loadSerial`-Checks in `fetchPlannedWindows` und vor Render.
- HTTP-Dedup: `inflight` Map in `fetchWindow` → parallele **identische** POSTs oft **seriell dedupliziert**, Journal trotzdem viele Zeilen (sequenzielle Zyklen).

### Refresh-Loop?

**Ja (coalescing loop):**

```
force refresh inflight
  → on_visible_range → refreshPane(viewport) coalesced
  → bumpExactOriginLoadSerial
  → inflight endet mit mismatch → scheduleSupersededDensityRefresh(48ms, viewport:true)
  → neuer Zyklus (2× cache_hit POST) → erneut viewport während inflight …
```

**30m verstärkt:** Erster Tile-Lauf **~2,2 s** `cache_hit=False` — lange **inflight**-Phase → mehr Viewport-Events → mehr Serial-Bumps. 5m-Wechsel (15:32): nur **~5** POSTs — kürzere/kompaktere Läufe.

### Blockiert Coalescing das Rendering?

**Ja — Hauptthese P24B:** Coalescing + Serial-Bump + `scheduleSupersededDensityRefresh` verhindern **stabile** Erreichung von `pushCanvasDensity`, während `stripAllPanes` die Canvas zuvor **leert**.

### Kommt `pushCanvasDensity` zum Abschluss?

| Quelle | Befund |
|--------|--------|
| Browser | **Nicht beobachtet** |
| Code | Nur nach bestandener `loadSerial`-Prüfung; superseded Läufe **return** davor. |
| Journal | Kein Server-Indikator für Canvas. |
| Nutzer | Keine Bänder trotz 200 + Pools → passt zu **fehlendem stabilem Client-Render**, nicht zu leerem Backend. |

**Canvas-Renderstatus:** **UNVERIFIED** direkt; **indirekt LEER/W unstable** (Symptom + Mechanismus).

---

## Schritt 4 — Minimaler Fix-Vorschlag (nur nach Ursache; nicht implementiert)

**Ursache (für Fix-Zweck):** Überlappende **`force`**-Refreshes (Chart-/Pool-TF-Wechsel + `loadPane`) und **`viewport`**-Refreshes (`on_visible_range`) ohne Viewport-Sperre → `bumpExactOriginLoadSerial` → wiederholtes Abbruch vor `pushCanvasDensity` bei weiterhin gültigen Pool-Daten.

### Option A — **Empfohlen, kleinstes Muster (existiert bereits für GO TO)**

| Wo | Was |
|----|-----|
| `research_charts.js` — TF-Wechsel-Handler (~5129) und ggf. Pool-TF-Wechsel-Kette | Wie GO-TO (~1387–1405): `pane._suppressExactOriginViewport = true` für Dauer von `loadPane` + `PD1ExactOriginResearch.onPaneTimeframeChange` / `refreshAll`, danach `false`. |

**Bedingung:** Unterdrücken von `scheduleViewport`, solange programmatischer TF-/Pool-TF-Wechsel läuft.

### Option B — Serial-Bump einschränken

| Wo | Was |
|----|-----|
| `refreshPane` in `pd1_exact_origin_density_research_v1.js` ~1382–1385 | **Nicht** `bumpExactOriginLoadSerial`, wenn inflight mit `{ force: true }` gestartet wurde und pending nur `viewport` ohne neue `fromUnix`/`toUnix` ist — pending nur in `gate.pending` merken. |

**Bedingung:** `eoRefreshPendingSupersedesInflight(pending) && !inflightWasForce`.

### Option C — Render trotz Supersede (größerer Eingriff)

Nach erfolgreichem Fetch mit `allPools.length > 0` einmalig rendern, auch bei Serial-Drift (SWR) — höheres Duplikat-Risiko; **nicht** erste Wahl.

**Explizit ausgeschlossen (Auftrag):** PD1-Engine, Publisher, 5m-SSOT, Backend-Window-Service, On-Demand-Compute.

---

## Abschluss-Metriken (Journal + Rekonstruktion)

| Metrik | Wert |
|--------|------|
| On-Demand-Bounds | **200** (Nov-CH-Zeitraum in Caps mergeable) |
| Viewport (inferiert) | Nov 2026 (Window-`from`/`to`) |
| Fenster pro Refresh-Zyklus | **2** (72h fixed tiles) |
| Pool-Anzahl (Server) | **149** / **193** pro Tile |
| Segmentanzahl | **UNVERIFIED** (Browser) |
| Window-POSTs im Sturm | **46** (43 cache hit, 2 miss) |
| Canvas | **Leer** (Nutzer) / **kein stabiler push** (Code-These) |

---

## Grenzen

- Kein authentifizierter Browser-Trace in dieser Agent-Session.
- Keine Änderung an produktivem Code in P24B.

---

## STOP

P24B READ-ONLY abgeschlossen. Nächster Schritt auf Wunsch: Option A im Frontend implementieren + Trace mit `DOGE_EXACT_ORIGIN_TRACE=1` verifizieren.
