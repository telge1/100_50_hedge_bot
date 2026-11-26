# MC-LOAD-P4I — Final Chart View Reset

**Endstatus:** `MC_LOAD_P4I_FINAL_VIEW_RESET_READY_BROWSER_PENDING`

Kein Deployment, Commit oder Push.

---

## Kanonische Reset-Kette (wiederverwendet)

| Schritt | Code |
|---------|------|
| Toolbar ⟲ | `wrap.querySelector(".trp-reset").click` → `chartApi.resetView()` |
| Pane | `resetView()` → `applyDefaultView()` → `computeDefaultLogicalRange` / `DEFAULT_RIGHT_OFFSET` |

P4I ruft dieselbe **`chart.resetView()`**-API auf — keine duplizierte Zoom-Logik.

---

## Ladeablauf

1. Phase A (schnell) — **kein** finaler Reset.
2. Phase B (Historie) + EMA/Layers — **kein** Zwischen-`force`-Reset während `awaitingHistoryB`.
3. Nach `loadPane`-Abschluss (Phase B oder Vollload ohne Fast-First): **`scheduleFinalPaneViewReset`** (50 ms + double rAF) → `runFinalPaneViewResetLikeToolbar`.

**Multi-Chart:** Reset pro `paneId` / `gen`, nicht global.

---

## Schutz

| Regel | Implementierung |
|-------|-----------------|
| Max. 1 × pro Load-Generation & Pane | `pane._mcLoadFinalViewResetGen === gen` |
| Kein Poll/Forming/OI | nur aus `loadPane`-Finalize |
| Kein Reset bei manuellem View | `chart.isViewManual()` → `mc_load_final_view_reset_skipped` |
| Keine veraltete Generation | `loadGen`, `paneGen`, `exitSupersededPaneLoad` |
| TF-Change `force` Reset | unterdrückt während Phase A / awaiting B / Phase B |

**P4E/P4G** unverändert (History, EMA, `restoreViewportAfterDataChange`).

---

## Diagnose

- `mc_load_final_view_reset`
- `mc_load_final_view_reset_skipped` (`view_manual`)

---

## Tests

```text
pytest … test_mc_load_p4i_v1.py → 15 passed (gesamter MC-LOAD-Lauf)
```

---

## Browser

Nach Deploy `mc-load-p4i-v1`: SOLUSDT 1m→5m, 120 s ohne Interaktion — erwartet **ein** zusätzliches `resetViewCount`-Inkrement nach Phase B; kein erneuter Reset durch Poll.