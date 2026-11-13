# DOGE-LIVE-P24C — TF-Wechsel: Viewport-Suppress (30m-Rendering)

**Erstellt:** 2026-11-13  
**Modus:** Minimaler Frontend-Fix (Spread, uncommitted)  
**Vorgänger:** P24B `P24B_RENDER_SUPERSEDE_STORM_NO_STABLE_PUSH`

**Ergebnis:** `P24C_VIEWPORT_SUPPRESS_TF_SWITCH_WIRED` — **BROWSER_UNVERIFIED**

---

## Phase A — Ursache (Call-Chain, bestätigt)

| Mechanismus | Befund |
|-------------|--------|
| **GO-TO (Referenz)** | `beginExactOriginViewportSuppress` um `refreshAll` — verhindert `on_visible_range` → `scheduleViewport` während programmatischem Refresh (`research_charts.js` ~1386–1405). |
| **Chart-TF-Wechsel (vorher)** | `loadPane` + `jumpCharts` + **sofort** `onPaneTimeframeChange` → parallele `refreshPane({force})`; **`on_visible_range`** → `scheduleViewport` **ohne** Suppress → `bumpExactOriginLoadSerial` bei Coalesce → Abbruch vor `pushCanvasDensity`. |
| **Pool-TF-Wechsel (vorher)** | `stripAllPanes` + `refreshAll({force})` ohne Suppress — gleiches Coalesce-Risiko während Chart-Layout nach TF-Wechsel. |
| **Request-Sturm (P24B)** | ~23 Zyklen × **2** feste 72h-Tiles = **~46** identische Window-POSTs. |

**Hypothese P24B:** durch Code-Chain **bestätigt** (nicht spekulativ).

---

## Phase B — Implementierung (minimal, isoliert)

### Geänderte Dateien (Spread)

| Datei | Änderung |
|-------|----------|
| `dashboard/static/js/research/research_charts.js` | Refcount `exactOriginViewportSuppressDepth`, `begin`/`end`, `exactOriginRefreshAfterTfSwitch`, `finalizeExactOriginChartTfSwitch`; Chart-`tfSel`: Suppress **vor** `loadPane`, Finalize mit **einem** Viewport-Refresh; GO-TO auf refcount umgestellt. |
| `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` | `runExactOriginPoolTfSwitch` für Pool-TF- und PD-Detail-Wechsel. |
| `dashboard/templates/research_charts.html` | Cache-Bust `pd1-p24c-tf-switch-viewport-suppress-v1` (JS + `research_charts.js`). |
| `dashboard/test_p24c_tf_switch_viewport_suppress_v1.py` | String-Regressionstests (neu). |
| `dashboard/test_p23_exact_origin_auth_session_v1.py` | Cache-Bust-Assert auf P24C-Query angepasst. |

### Verhalten

1. **Während** programmatischem Chart-/Pool-TF-Wechsel: `scheduleViewport` no-op (`pane._suppressExactOriginViewport` via refcount).
2. **`finally` / Fehlerpfad:** `endExactOriginViewportSuppress()` (refcount → 0).
3. **Danach genau ein** `exactOriginRefreshAfterTfSwitch(pane)` aus sichtbarem Chart-Bereich (`viewport: true`).
4. **Manuelles** Scrollen/Zoomen: Suppress nur während Wechsel — danach normale `scheduleViewport`-Debounce (400 ms).
5. **Kein** globaler Serial-Bump-Change — nur weniger parallele `viewport`-Coalesces während `force`-Läufen.
6. **Keine** feste Timeout-„Warte-X-Sekunden“-Lösung.

### deps an Exact-Origin-Modul

`beginExactOriginViewportSuppress`, `endExactOriginViewportSuppress`, `refreshExactOriginAfterTfSwitch`.

---

## Phase C — Regressionstests

```text
cd spread_recovery_hedge_short_dev/dashboard
python -m pytest test_p24c_tf_switch_viewport_suppress_v1.py test_p23_exact_origin_auth_session_v1.py -q
# 8 passed
```

| Fall | Automatisiert |
|------|----------------|
| 5m→30m / 30m→5m Chart+Pool | **BROWSER_UNVERIFIED** |
| Nur Chart / nur Pool TF | Code-Pfad vorhanden |
| Schnelles Umschalten | Refcount nested-safe |
| Scroll nach Wechsel | Suppress endet vor manuellen Events |
| GO TO | Refcount-Suppress (gleiche Semantik) |
| Live-Updates / P23 Auth | Unveränderte P23-Logik in EO-JS |
| POST-Sturm reduziert | **Erwartung** ~2–4 POSTs statt ~46; **Journal nach Fix: UNVERIFIED** |

---

## Phase D — Verifikation

| Prüfung | Status |
|---------|--------|
| Authentifizierter Browser (5m→30m, Bänder sichtbar) | **BROWSER_UNVERIFIED** |
| Journal POST-Zählung nach Fix | **UNVERIFIED** (kein Nutzer-Retest in dieser Session) |
| Statische Tests | **8 passed** |

**Nutzer-Check:** Hard-Refresh (Cache-Bust `pd1-p24c-tf-switch-viewport-suppress-v1`), Exact Origin an, 5m→30m Chart+Pool — Bänder + Network-Tab (Window-POST-Anzahl).

---

## Request-Anzahl (vorher / erwartet nachher)

| Metrik | P24B (vorher, Journal) | Erwartung P24C |
|--------|-------------------------|----------------|
| Window-POSTs / TF-Wechsel (~26 s) | **46** (2 Tiles × ~23 Zyklen) | **~2–6** (1× force-Lauf + 1× final viewport, 2 Tiles, ggf. 1× cache miss + hits) |
| `pushCanvasDensity` | oft abgebrochen (`loadSerial_mismatch`) | finaler `exactOriginRefreshAfterTfSwitch` sollte Render abschließen |

---

## Verbleibende Unsicherheiten

- Doppel-`refreshPane` aus `onPaneTimeframeChange` + `loadPane`-Ende bleibt (bewusst nicht geändert — P16/P12-Pfad).
- Wenn Viewport nach Wechsel noch animiert, könnte **ein** zusätzlicher debounced `scheduleViewport` nach Unsuppress normal sein (kein Sturm).
- 30m-On-Demand-Backend unberührt.

---

## Grenzen eingehalten

Keine Änderungen an PD1-Engine, Publisher, Collector, 5m-SSOT, Trading, MC4-Backend. **Kein Commit / kein Push / kein Dashboard-Restart.**

---

## STOP

P24C abgeschlossen. Visueller Browser-Test durch Nutzer empfohlen.
