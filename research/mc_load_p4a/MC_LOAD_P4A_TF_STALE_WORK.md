# P4A — Stale 1m-Arbeit beim TF-Wechsel

## Symptom (P4-Browser-Messung)

Während SOLUSDT **1m → 5m**:

- Laufender **1m** `ema-after-candle-merge` (50k, ~7,6s).
- 5m Phase A/B und EMA-Skip nach B funktionieren (P4).

## Warum gültige Guards nicht griffen

### `loadPane` / `paneGen`

Neuer 5m-`loadPane` macht `paneGen++`. Alte 1m-`loadPane`-Antworten sollten an `exitSupersededPaneLoad` scheitern — **wenn** sie noch in `loadPane` waren.

### Lücken

1. **`ensurePaneCaughtUp`** (deferred nach Fast-First): nur `gen === state.loadGen`, kein `paneGen`. Nach TF-Wechsel gleiche `loadGen`, aber Pane schon 5m → 1m-Gap-URL konnte noch gemerged werden.

2. **`mergePollCandlesIntoPane`**: Poll/Backfill mit 1m-Response in 5m-Pane theoretisch möglich, wenn TF im Payload nicht geprüft wurde.

3. **EMA-Debounce**: `scheduleRefreshPaneEmaOverlays` aus 1m-Poll; Feuer nach `pane.tf` bereits 5m, aber **in-flight** 1m-`indicatorsOnly`-POST vom vorherigen Tick.

4. **TF-Wechsel**: kein `abortStalePaneWork` — Phase-B-Timer von vorherigem Zyklus konnte theoretisch mit altem `gen` starten (selten); primär EMA/catch-up.

## Änderungen P4A

| Hook | Guard |
|------|--------|
| TF `change` | `abortStalePaneWork(pane)` vor `loadPane` |
| `ensurePaneCaughtUp` | `paneGen`, `packed.timeframe`, optional `loadAbort.signal` |
| `mergePollCandlesIntoPane` | `packed.timeframe === pane.tf` |
| `schedulePaneHistoryPhaseB` | `schedulePaneGen === pane.paneGen` |
| `refreshPaneEmaOverlays` | `loadAbort.signal`, `emaPaneGen` |

## Bewusst nicht abgebrochen

- Laufender **5m**-Request desselben Wechsels (`paneGen` nach abort=Startgeneration für neuen Load).
- Multi-Pane andere Tiles (nur betroffenes Pane bei TF-Dropdown).
- Gültige 5m-Polls nach abgeschlossenem Load.

## Verifikation

Diag: während 5m-Trace kein HTTP `ema-after-candle-merge` mit `timeframe: 1m` auf demselben Pane.

`BROWSER_PENDING`.