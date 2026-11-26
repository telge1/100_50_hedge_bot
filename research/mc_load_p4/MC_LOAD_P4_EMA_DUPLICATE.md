# P4 — EMA-Catch-up nach Phase B

## Beobachtung (P3B-Trace)

Nach abgeschlossener History Phase B erschien ein separater Pane-POST mit `sourceAction: ema-post-catch-up`, **50.000** Kerzen, ~14,1 s.

## Datenabhängigkeit & Stale-Bedingung

`paneEmaStaleVsCandles(pane, { afterPaneBundle: true })`:

- Vergleicht letzte geschlossene Kerze (`lastClosedBarTime`) mit dem letzten EMA-Zeitstempel in `pendingData.ema`.
- Mit `afterPaneBundle: true`: wenn `maxEmaT >= lastC - step` → **nicht stale** (Bundle-EMA gilt als aktuell).

Phase B liefert vollständiges `pane_bundle` inkl. EMA vom Server. Trotzdem kann nach `ensurePaneCaughtUp` Stale **true** bleiben, wenn:

- Merge/Poll die Kerzenspitze vor EMA-Spitze schiebt, oder
- Bundle-EMA-Zeitreihe kürzer als Kerzen (Randfälle),

aber der **dominante** Pfad im Trace war: Phase B nutzt den **nicht-deferred** Zweig in `loadPane` (kein `mcLoadPostLoadDeferred`), der **immer** EMA-Catch-up prüft — unabhängig davon, dass Phase B bereits Bundle-EMA enthielt.

`refreshPaneEmaOverlays` → `loadPane({ indicatorsOnly: true, from/to/limit: paneIndicatorReloadRange })` → erneuter großer POST (typisch bis `full_history_limit` bzw. Span-Limit, hier ~50k).

## Doppelarbeit (bewiesen)

| Schritt | EMA-Quelle |
|---------|------------|
| Phase B POST | Server `pane_bundle.ema` |
| `ema-post-catch-up` | Zweiter POST nur Indikatoren/Span |

Das ist funktional redundant für den Normalfall „Phase B erfolgreich mit Bundle-EMA“.

## Fix P4 (minimal)

```javascript
function mcLoadSkipEmaCatchUpAfterPaneBundle(pane, opts) {
  if (opts && opts._historyPhaseB) return { skip: true, reason: "history_phase_b_bundle_ema" };
  return { skip: false, reason: null };
}
```

Angewendet im synchronen Post-Load-Pfad vor `refreshPaneEmaOverlays(..., "ema-post-catch-up")`.

**Nicht geändert:**

- Deferred Phase-A-Pfad (`runPaneDeferredPostLoad`) — weiterhin EMA nur wenn `!_mcLoadAwaitingHistoryB`.
- `ema-after-candle-merge` / Poll — unverändert.
- Keine EMA-Mathematik.

## Diagnose P4

Events: `ema_catch_up_decision` + `phase_decisions[]` Eintrag `ema_post_catch_up` mit `skipped` / `skip_reason`.

## EMA-Parität

Baseline: Stale-Definition und `paneIndicatorReloadRange` unverändert. Nach P4 sollte nach tf-change **kein** 50k-`ema-post-catch-up` mehr folgen, wenn Phase B Bundle liefert (`BROWSER_PENDING` zur Verifikation).