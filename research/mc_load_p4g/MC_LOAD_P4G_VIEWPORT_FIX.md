# MC-LOAD-P4G — Right-Margin / Zoom Regression

**Endstatus:** `MC_LOAD_P4G_VIEWPORT_FIX_READY_BROWSER_PENDING`

Kein Deployment, Commit oder Push.

---

## Root Cause (bestätigt)

**Symptom (P4F, keine User-Interaktion):** `barsAfter` −96 → 0 bei gleicher `payloadCount` (24192), gleicher Visible-Time-Range, `barSpacing` ~4.72 → ~12.82, `rightOffset` config bleibt 96.

**Mechanismus:** Nach `setData` / `setEmaOverlays` wurde der Viewport per **`setVisibleRange` (UTC)** wiederhergestellt. Die sichtbaren Kerzenzeiten bleiben gleich, aber der **kerzenfreie logical right margin** (logical `to` > lastBarIndex) geht verloren — die Library skaliert die Darstellung auf logisch `to ≈ lastBar` (= „Zoom-In“).

**Hypothese P4G (zeitbasierter Restore ohne Logical Margin):** **bestätigt** für die Restore-Pfade in `chart.js` (`setData_preserveViewTime`, `setEmaOverlays`).

**t≈44.524 `double_raf_after_setData`:** Passt zu einem **vollständigen `setData`**-Pfad (z. B. forming-Fallback oder erneuter Bundle-Apply), nicht zum Poll (erst t≈44.844). Poll als alleinige Ursache: **nicht** ausreichend belegt.

**t≈26 s Sprung (P4D):** weiterhin **nicht** diesem Fix zugeordnet / unbestätigt.

---

## Änderung (minimal, nur Viewport)

| Datei | Inhalt |
|-------|--------|
| `static/research_trp/chart.js` | `readViewportState`, `captureViewportForRestore`, `restoreViewportAfterDataChange` (logical + `barSpacing` vor time-only), `emitViewportMutation` |
| `setData` | Einheitlicher Restore über logical wenn `logical.to > lastIdx` |
| `setEmaOverlays` | Gleicher Restore; `applyDefaultView` nur wenn **nicht** bereits `isNearLiveLogicalRange` |
| `research_charts.js` | `on_viewport_mutation` → `viewport_mutation` Diag; forming-`setData` mit `viewportSnapshot` |
| Assets | `mc-load-p4g-v1` |

**P4E History/EMA/Cache:** unverändert.

---

## Regressionstests (statisch)

```text
pytest test_mc_load_p3_fast_first_v1.py test_mc_load_p4e_v1.py test_mc_load_p4g_v1.py research_charts/tests/test_candle_cache_limit_hit.py → 12 passed
```

---

## Browser-Verifikation (Benutzer)

1. Hard-Reload `mc-load-p4g-v1` (nach Deployment).
2. P4F-Skript / 60 s, SOLUSDT 1m→5m, **keine** Interaktion.
3. Prüfen: `barsAfter` bleibt negativ (~−96) oder gewählter Margin; kein `barSpacing`-Sprung bei gleicher Time-Range.
4. `event_ring`: neue Events `viewport_mutation` mit `reason` (`setData_*`, `setEmaOverlays`).

**Erfolgskriterium:** P4E-Befunde erhalten + `barsAfter` stabil über t≥60 s.