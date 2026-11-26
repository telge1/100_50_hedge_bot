# MC-LOAD-P3B — Phase-A-Fallback exakt identifizieren

**Status:** `MC_LOAD_P3B_BROWSER_PENDING`  
**Backup:** `dashboard/backups/mc-load-p3b-20261126T084124Z/`  
**Asset (Ziel):** `research-mc-load-p3b-v1` / `mc-load-p3b-v1`

---

## 1. Messwert 24.192 Kerzen

**Bestätigt:** `24.192 = 12 × 2016` mit `historyLoadLimitForTimeframe("5m")`:

```
weeks = max(1, #lldHistoryWeeks || state.history.weeks || 12)  → typisch 12
limit = round((weeks × 7 × 24 × 3600) / 300) = weeks × 2016
```

Kein Zufallswert — **volles History-Weeks-Limit** auf dem Pane-POST, **kein** Phase-A-Fenster (~400 Bars).

**range_origin** bei diesem Lauf sehr wahrscheinlich: `history_preset_rolling` (kein `from`/`to` im Body, nur `limit: 24192`).

---

## 2. Root Cause (Code + Trace-Symptome)

### A — Phase A wurde **nicht** ausgeführt

Trace: `load_started.phase: "full"`, `candle_count: 24192`, kein `mc_load_phase_a_begin`, kein `history-phase-b`.

Die Request-Payload entspricht dem **Legacy-Volllast**-Pfad in `loadPane`:

- `limit: historyLoadLimitForTimeframe(pane.tf)`
- ohne Phase-A-`from`/`to`/`limit`-Override

### B — **Fehlklassifikation** `first_candles_owner_release`

Ältere Builds: `phase: "fast"` wenn nur `mcLoadPostLoadDeferred` aktiv, **ohne** `pane._mcLoadPhaseA` (P3 verwirrt „deferred“ mit „Fast-First“).

P3A stellte auf `phase: "full"` um; Traces mit `phase: fast` deuten auf **älteren Bundle-Inhalt** trotz neuer Query-URL oder auf Trace vor P3A.

### C — Kein Phase B

`schedulePaneHistoryPhaseB` läuft nur wenn `pane._mcLoadPhaseA === true` nach Phase-A-`setData`.

### D — Mögliche Skip-Gründe (per P3B im Export sichtbar)

| `fallback_reason` | Bedeutung |
|-------------------|-----------|
| `fast_first_disabled` | `localStorage mc.load.fastFirst === "0"` |
| `explicit_history_window` | `opts.from`+`opts.to`, GO TO, Replay, `history-phase-b` |
| `fast_window_covers_full_history` | Phase-A-Bars ≥ volles Limit (selten bei 12 Wochen) |
| `historical_replay` / `history_blocks_live` | Live-Fast-First aus |
| `source_action_<name>` | Nicht allowlisted |

**P3A-Fix** (Pins nur auf **explizite** opts, nicht History-Preset-Range) sollte tf-change **ohne** GO-TO-Pin freigeben — wenn trotzdem `full`, liefert P3B `mc_load_phase_a_decision` den **konkreten** Grund.

### E — Deploy / Cache

`:8080` in Agent-Session: kein direkter Dashboard-Static-Nachweis.  
Prüfung:

```js
window.__MC_LOAD_PHASE_P3B__   // muss "mc-load-p3b-v1" sein
// oder:
typeof buildMcLoadPhaseADecision === "undefined" // in Host-IIFE nicht global; stattdessen:
document.querySelector('script[src*="research_charts.js"]')?.src
```

Served file muss `buildMcLoadPhaseADecision` und `mc_load_phase_a_decision` enthalten.

---

## 3. P3B Änderungen (minimal)

| Änderung | Zweck |
|----------|--------|
| `buildMcLoadPhaseADecision` + Event `mc_load_phase_a_decision` | Alle geforderten Felder im JSON |
| `mcLoadHistoryWeeks` / `mcLoadRangeOrigin` | Wochen + Herkunft der Range |
| `mcLoadPostLoadDeferred(opts, phaseA)` — **nur bei echtem Phase A** | Kein frühes Release beim Full-Load |
| `window.__MC_LOAD_PHASE_P3B__` | Laufzeit-Versionsmarker |
| `effective_fast_first` nur wenn `phaseA` | Kein falscher PASS |
| Asset `p3b-v1` | Cache-Bust |

**Keine** Änderung an PD1, Trading, EMA-Formeln, `pane_bundle`.

---

## 4. Diagnose-Export (neu/ergänzt)

In `runs[].latest` / Event `mc_load_phase_a_decision`:

| Feld | Inhalt |
|------|--------|
| `fast_first_enabled` | bool |
| `fast_first_localStorage` | Wert / null |
| `input_from` / `input_to` / `input_limit` | opts |
| `resolved_range_from` / `resolved_range_to` | `resolvePaneLoadRange` |
| `history_weeks` | z. B. 12 |
| `full_history_limit` | z. B. 24192 |
| `range_origin` | `history_preset_rolling`, … |
| `is_explicit_history_pin` | bool |
| `gotoNavViewCenterTs` / `gotoNavTargetTs` | Navigation |
| `decision` | `A` \| `skip` |
| `fallback_reason` | wenn skip |
| `phase_a_requested_limit` / `phase_a_effective_limit` | Zahlen |
| `mc_load_p3b_code` | `mc-load-p3b-v1` |

Bereits aus P3A: `phase_requested`, `phase_executed`, `phase_*_http_id`, `request_completion_status`, …

**PASS-Kriterium:** `decision: "A"`, `phase_a_candle_count` ≪ 24192, danach `history-phase-b` mit ~24192, `effective_fast_first: true`.

---

## 5. Entry-Points (Matrix)

| Entry | Phase A (P3B) |
|-------|----------------|
| Initial / pane-load | Ja, wenn nicht gepinnt |
| symbol-switch | Ja |
| **tf-change** | Ja (ohne opts-from/to; Jump nach Load) |
| tf-change-replay / GO TO | Nein (`explicit_history_window`) |
| history-reload / reload mit from+to | Nein |
| history-phase-b | Nein (Full + preserveView) |
| ema-* / indicatorsOnly | Nein |

---

## 6. ema-after-candle-merge (50k)

Im **P3A-clean-Trace** nicht aufgetreten. Wenn nach Gap-Merge während `_mcLoadAwaitingHistoryB`: P3A unterdrückt Schedule bis Phase B fertig.

Nach P3B ohne Phase A: voller synchroner Catch-up-Pfad (kein defer), EMA-Reload nur bei echtem Stale (`afterPaneBundle`).

---

## 7. Tests

| Test | Ergebnis |
|------|----------|
| `test_mc_load_p3_fast_first_v1.py` | **PASS** (statisch, 3) |
| Browser SOL/BNB tf-change, Opt-out, GO TO, EMA-Parität | **BROWSER_PENDING** |

### Manuelle Abnahme

```js
__MC_LOAD_DIAG__.start({ symbol: "BNBUSDT", timeframe: "5m", test: "P3B-tf" });
// Hard-Reload, prüfen: window.__MC_LOAD_PHASE_P3B__
// BNB 1m → 5m
__MC_LOAD_DIAG__.stop(); __MC_LOAD_DIAG__.exportJSON();
```

Erwarte im Export:

1. `phase_a_decision.decision === "A"`
2. `phase_a_effective_limit` ~ 400
3. `history_weeks` → `full_history_limit` 24192
4. Erster Pane mit ~400 Kerzen, zweiter `history-phase-b` ~24192

---

## 8. Call-Path nach P3B (tf-change, Fast-First an)

```
tfSel.change → pane.tf=5m
  → loadPane({ sourceAction: "tf-change" })
  → buildMcLoadPhaseADecision → mc_load_phase_a_decision
  → reqBody: Phase-A from/to/limit
  → setData(klein) → release (nur wenn Phase A)
  → schedulePaneHistoryPhaseB
  → optional jumpChartsToUnix(center)
```

**Vollpfad** nur wenn `decision: "skip"` — dann `deferPost === false`, klassisches `loading…` bis Catch-up/EMA fertig.

---

## 9. Warum der P3A-Trace noch „full“ zeigte

1. **Phase A übersprungen** (Grund im nächsten Export unter `fallback_reason`) **oder**
2. **Alter JS-Inhalt** (Events `phase: fast` passen nicht zu aktuellem Repo nach P3A).

P3B trennt das eindeutig und verhindert Schein-Fast-First ohne Phase A.

**STOP** — kein Commit/Push.