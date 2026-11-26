# MC-LOAD — Abschluss auf Stand P4J

**Datum:** 2026-11-26  
**P4K:** nicht implementiert (bewusst).

## Gelieferte Software-Stand (P4E → P4J)

- Fast-First Phase A/B, History-Live-Range, Cache-Tail bei live `end`
- EMA-Catch-up nach Phase B, Merge/Inflight-Guards
- Viewport-Restore (P4G): logical + `barSpacing` bei Updates
- Optional: ein automatischer finaler `resetView` nach Vollload (P4I) — **Benutzer akzeptiert primär manuelle Zentrierung über Toolbar ⟲**

## Bekannte Einschränkungen

| Thema | Status |
|-------|--------|
| **Browser-PASS P4J** | Ausstehend — Benutzer-Export `MC_LOAD_P4J_<load_id>.json` |
| **Browser-PASS P4H/P4G Viewport** | Teilweise durch P4F; P4G-Fix + P4I-Reset nicht formal mit 120s-Export abgeschlossen |
| **P4D t≈26s Caller** | Unbestätigt (in P4F nicht reproduziert) |
| **Candle Integrity 1795685460** | CH ≠ Debug-Payload; keine DB-Reparatur |
| **Deferred EO Phase A vs Phase B** | Theoretisches Overlay-Race; kein `resetView` aus Poll/Forming |
| **Explizite historische Navigation** | Pin `to` in Vergangenheit liefert volles Fenster (by design) |

## Manuelle Bedienung

**Chartansicht zurücksetzen** (⟲) = kanonisches `chartApi.resetView()` / `applyDefaultView()` — vom Benutzer ausdrücklich akzeptiert als finale Zentrierung.

## Deploy-Stand (letzter Neustart P4J)

Dashboard PID **1928817**, Assets **`mc-load-p4i-v1`**. Kein weiterer Neustart im Abschluss-Scommit.

## Tests (lokal)

15× pytest MC-LOAD-Regression (P3/P4E/P4G/P4I + Cache) — siehe Commit-Bericht.