# P4 — History Phase B: welches Limit gilt?

## Formel (authoritative)

In `research_charts.js`:

```text
historyLoadLimitForTimeframe(tf):
  weeks = mcLoadHistoryWeeks()
  step  = TF_SEC[tf]   // 5m → 300
  limit = max(100, round((weeks * 7 * 24 * 3600) / step))
```

Für **5m**:

```text
bars_per_week = 7 * 24 * 12 = 2016
limit = weeks * 2016
```

| `history_weeks` | `limit` (5m) |
|-----------------|--------------|
| 12 | **24.192** |
| 33 | **66.528** |
| (Trace 66.804) | ≈ 33,1×2016 (+Server/Warmup/Forming) |

Der Nutzer-Trace **66.804** Kerzen passt zu **~33 Wochen** im UI/`state`, nicht zu 12.

## Warum Diagnose `history_weeks: 12` zeigen kann

`buildMcLoadPhaseADecision` snapshotiert `history_weeks: mcLoadHistoryWeeks()` zum Zeitpunkt der **Phase-A-Entscheidung** (ein Event pro `loadPane`-Aufruf).

- Phase A (`tf-change`): oft noch Preset/UI-Snapshot mit 12 Wochen in der JSON-Zeile `phase_a_decision`.
- Phase B (`history-phase-b`): eigener `loadPane` — Limit kommt erneut aus **aktuellen** `mcLoadHistoryWeeks()` → kann 33 sein, wenn `#lldHistoryWeeks` oder `state.history.weeks` zwischen A-Start und B-Request gesetzt wurde.

**P4:** `mcLoadHistoryWeeks()` ruft zuerst `readHistoryFromUi()` auf, damit DOM und `state` vor Phase B konsistent sind.

## Was im POST wirklich mitgeht

Phase B `reqBody`:

```javascript
limit: opts.limit ?? historyLoadLimitForTimeframe(pane.tf)
```

Ohne explizites `opts.limit` gilt **immer** die volle Wochen-Spanne zum Request-Zeitpunkt — nicht der Wert aus `phase_a_decision.full_history_limit`.

## Diagnose P4

Pro HTTP-Request (Pane POST):

- `request_limit`, `history_weeks` (Meta `mcLoadReqLimit` / `mcLoadHistoryWeeks`)
- Run-Level: `phase_b_decision` mit `lld_history_weeks_dom`, `full_history_limit`, `request_limit`
- `phase_decisions[]` chronologisch (`pane_load_phase_a`, `pane_load_phase_b`, …)

**Interpretation:** Bei Diskrepanz 12 vs 66804 zuerst `requests[].history_weeks` und `requests[].request_limit` für **Phase-B-`http_id`** lesen, nicht nur `phase_a_decision`.

## Kein Produkt-Change

Es wurde **kein** Default der History-Wochen geändert — nur Lesekonsistenz und Transparenz.