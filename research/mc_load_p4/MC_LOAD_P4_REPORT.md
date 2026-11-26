# MC-LOAD-P4 — Fast-First beibehalten, Doppelarbeit & Diagnose

**Status:** Code-Änderungen in `spread_recovery_hedge_short_dev/dashboard` (Asset `mc-load-p4-v1`).  
**Browser-Nachweis P4:** `BROWSER_PENDING` (P3B-Fast-First weiterhin vom Nutzer bestätigt; P4-Messungen hier nicht neu im Browser erfasst).

## Kurzfassung

| Thema | Befund | Maßnahme P4 |
|--------|--------|-------------|
| EMA 50k nach Phase B | Phase-B-`loadPane` lief synchronen Pfad: `ensurePaneCaughtUp` + `ema-post-catch-up` (`indicatorsOnly`-Pane-POST bis ~50k) | `mcLoadSkipEmaCatchUpAfterPaneBundle` bei `opts._historyPhaseB` — kein zweiter EMA-POST |
| 66.804 vs `history_weeks: 12` / 24192 | Geltendes Limit = `historyLoadLimitForTimeframe(tf)` = `weeks × (7×24×3600 / TF_SEC)`; 66804 ≈ **33×2016** (5m), nicht 12 Wochen | `readHistoryFromUi()` vor Wochen-Lesen; Phase-B-Snapshot in Diagnose (`request_limit`, `lld_history_weeks_dom`) |
| `all_optional_layers_scheduled` ~36,9 s | Event steht **nach** `await PD1ExactOriginResearch.refreshPane` im Phase-A-Pfad | Bei `paneLoadReleasedEarly`: Exact-Origin **async** (`void`), Event früher |
| Diagnose overwrite | `phase_a_decision` wurde bei Phase-B-„skip“ mitgeschrieben | Phase A nur bei `decision === "A"`; `phase_decisions[]` + HTTP-`phase_*_decision` |
| `ob1000-levels` bei `orderbook_levels: false` | `scheduleOrderbookLevels` immer getriggert; Fetch nur wenn `state.obl.enabled` | Dokumentiert; kein Trading/OPI-Change |

## Referenz-Trace (P3B, Nutzer, SOLUSDT 5m `tf-change`)

| Phase | Kerzen | Dauer (ms) |
|-------|--------|------------|
| A | 302 | 255 |
| Erste Freigabe | — | ~284 nach A-Start |
| Render-Proxy | — | ~307 nach A-Start |
| B | 66.804 | 9.048 |
| `ema-post-catch-up` (vor P4) | 50.000 | 14.152 |
| `all_optional_layers_scheduled` | — | 36.910 s ab Trace-Start |

**Fast-First:** unverändert beabsichtigt (Phase A weiterhin kleines Fenster, frühes `releasePaneLoadOwner`).

## Artefakte in diesem Ordner

- `MC_LOAD_P4_EMA_DUPLICATE.md`
- `MC_LOAD_P4_HISTORY_LIMITS.md`
- `MC_LOAD_P4_OPTIONAL_LAYERS_TIMELINE.md`
- `MC_LOAD_P4_DIAG_PARTITIONING.md`
- `MC_LOAD_P4_OB1000.md`
- `MC_LOAD_P4_BASELINE_BROWSER.md`
- `MC_LOAD_P4_CODE_CHANGELOG.md`

## Regression / Parität

- Keine neuen EMA-Formeln; Stale-Logik unverändert (`paneEmaStaleVsCandles`, `afterPaneBundle`).
- `node --check` + `pytest test_mc_load_p3_fast_first_v1.py`: **3 passed** (lokal nach P4-Edits).
- Kein Commit/Push (Auftrag).