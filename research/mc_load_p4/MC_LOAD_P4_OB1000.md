# P4 — `ob1000-levels` bei `orderbook_levels: false`

## Diagnose-Snapshot

`mcLoadDiagSettings()` → `orderbook_levels: !!(state.obl && state.obl.enabled)`.

Wenn Checkbox/State OBL aus: **`orderbook_levels: false`**.

## Warum trotzdem HTTP-Einträge möglich

1. **`scheduleOrderbookLevels(pane)`** wird am Ende von `loadPane` **immer** aufgerufen (auch Fast-First).
2. **`refreshPaneOrderbookLevels`** bricht sofort ab, wenn `!state.obl.enabled` (und nicht Multi-Manual) → `clearPaneOrderbookLevels`, **kein** GET `ob1000-levels`.
3. **Lease/Heartbeat:** `startOrderbookLevelsRefresh` / `ensureOb1000Lease` laufen nur wenn `state.obl.enabled`. Bei disabled: `stopOb1000Heartbeat()`.

## Typische Trace-Erklärungen

| Symptom | Wahrscheinlichkeit |
|---------|-------------------|
| `ob1000-levels` während Run mit `orderbook_levels: false` | Request aus **vorherigem** Run, Timing-Overlap, oder Diagnose-`settings` von `stop()`-Snapshot **nach** Deaktivierung |
| `ob1000-heartbeat` / `lease` | OBL war kurz an oder Multi-Manual-Pfad |
| Nur `scheduleOrderbookLevels` ohne HTTP | 200 ms Timer → early return (erwartet) |

## CPU / Main Thread

- Disabled: ein `setTimeout(200)` + kurzer Return — vernachlässigbar gegenüber Pane/EMA.
- Enabled + depth 1000: REST + optional WS — relevant; nicht Gegenstand dieser Deaktivierung.

## P4-Aktion

Kein Code-Change am OBL-Produkt (Auftrag). Nur Verifikationspfad dokumentiert.

**UI-Check (Browser):** `#researchObLevels` / `state.obl.enabled` gegen Network-Tab. `BROWSER_PENDING`.