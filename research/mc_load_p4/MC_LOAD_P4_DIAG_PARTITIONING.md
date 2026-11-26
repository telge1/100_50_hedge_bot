# P4 — Diagnose: Phase A / B / EMA getrennt

## Problem (P3B)

- `buildMcLoadPhaseADecision` lief bei **jedem** `loadPane` (auch Phase B) und konnte `latest.phase_a_decision` überschreiben (z. B. `decision: "skip"`, `fallback_reason: "opts_skip"`).
- `phase_requested` / `effective_fast_first` wurden bei Phase-B-Track erneut gesetzt.

## Modell P4

### Run-Level (`currentRun`)

| Feld | Regel |
|------|--------|
| `phase_a_decision` | Nur wenn `decision === "A"` und `source_action !== "history-phase-b"` |
| `phase_b_decision` | Aus `mcLoadDiagTrackPhaseRun` bei `_historyPhaseB` |
| `phase_b_decision_resolve` | Optional: resolve-Zeile bei B-`loadPane` |
| `phase_decisions[]` | Append-only Timeline (`kind`: `phase_a_resolve`, `pane_load_phase_a`, `pane_load_phase_b`, `ema_post_catch_up`, …) |
| `phase_a_http_id` / `phase_b_http_id` | Unverändert, an Response gekoppelt |

### Request-Level (`requests[]`)

Bei Pane-POST-Abschluss:

- `request_limit`, `history_weeks` auf der HTTP-Zeile
- `phase_a_decision` / `phase_b_decision` **Kopie** zum jeweiligen `http_id`

Späteres `skip` auf Run-Level überschreibt **nicht** mehr die gespeicherte Phase-A-Entscheidung.

### Events

- `ema_catch_up_decision` mit `skipped`, `skip_reason`, `stale`
- Schema `mc_load_p4_v1`, Asset `mc-load-p4-v1`

## Export

`__MC_LOAD_DIAG__.exportJSON()` enthält `phase_decisions` und per-Request-Entscheidungen für Korrelation mit `http_id`.