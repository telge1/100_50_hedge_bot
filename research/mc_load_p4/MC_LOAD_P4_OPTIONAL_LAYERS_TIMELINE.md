# P4 — `all_optional_layers_scheduled` (~36,9 s)

## Event-Position im Code

`loadPane` emittiert `all_optional_layers_scheduled` **nach**:

1. Kerzen-Apply, optional deferred / catch-up / EMA (Pfadabhängig)
2. `scheduleLiquidity…`, Pools, OPI, …
3. `scheduleOrderbookLevels`, Trade-Bubbles, BT-Scanner, …
4. **`await PD1ExactOriginResearch.refreshPane`** — wenn Exact Origin für Pane aktiv (und nicht multi-boot-Ausnahme)

Bei **Fast-First Phase A** war `paneLoadReleasedEarly === true`, aber Exact Origin wurde trotzdem **await**-et → blockiert den Rest von `loadPane` inkl. finales Diagnose-Event.

## Erforderlich vs vermeidbar

| Arbeit | Erforderlich für erste Kerzen? | P4 |
|--------|----------------------------------|-----|
| Phase A POST + Render | Ja (Fast-First) | unverändert |
| Phase B POST | Ja (volle History) | unverändert |
| Deferred `ensurePaneCaughtUp` (A) | Gap-Fill nach Early Release | unverändert |
| EMA nach B | Nein, wenn Bundle-EMA | skip (siehe EMA-Doc) |
| Exact Origin nach A | Overlay-Dichte, nicht erste OHLC | **async** wenn `paneLoadReleasedEarly` |
| `schedule*` (OBL, Bubbles, …) | Nur Timer/Schedule; leicht | unverändert |

## Fix P4

Wenn `paneLoadReleasedEarly`:

```javascript
void PD1ExactOriginResearch.refreshPane(...).then(...).catch(...);
```

Sonst weiterhin `await` (vollständiger Load ohne Fast-First).

Zusätzliche Diagnose: `exact_origin_refresh_begin/end` mit `deferred_async: true`.

## Erwartung

`all_optional_layers_scheduled` rückt näher an Ende von Phase-A-Netzwerk + Apply + Schedules, **ohne** auf Exact-Origin-CPU zu warten.

**Messung:** `BROWSER_PENDING` — Ziel: deutlich unter ~37 s bei gleichen Einstellungen, sofern Exact Origin an war.

## Kein PD1-Formel-Change

Nur Scheduling/Await-Verhalten; `PD1ExactOriginResearch.refreshPane` Inhalt unangetastet.