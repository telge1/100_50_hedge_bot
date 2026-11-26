# MC-LOAD-P3A — Fast-First-Pfad (tf-change / History-Preset)

**Status:** `MC_LOAD_P3A_BROWSER_PENDING`  
**Backup:** `spread_recovery_hedge_short_dev/dashboard/backups/mc-load-p3a-20261126T082713Z/`  
**Deploy-Check:** Produktions-`:8080` lieferte in der Agent-Session **kein** Dashboard-JS (Apache/WordPress-301). Lokales Dashboard **:3000** enthält `research-mc-load-p3a-v1`.

---

## 1. Root Cause (bestätigt aus Code + Traces)

### A — `resolveMcLoadPhaseA` blockierte auf **implizitem** History-Fenster

```867:873:spread_recovery_hedge_short_dev/dashboard/static/js/research/research_charts.js (vor P3A)
if (range && (range.from != null || range.to != null)) return null;
```

`loadPane` ruft `resolvePaneLoadRange(opts)` auf, das **ohne** opts oft `computeHistoryRangeUnix()` liefert (`30d`/`7d`/`90d`/custom) → **from/to gesetzt**, obwohl der Aufrufer kein GO-TO ist.

**Folge:** Phase A **aus** → volles `limit` (≈66804) → `phase: full` bei `tf-change`, `symbol-switch`, Initial.

### B — `tf-change` mit gesetztem `gotoNavViewCenterTs`

Pan/Viewport setzt `gotoNavViewCenterTs` (`handleVisibleRangeForGotoNav`). Beim TF-Wechsel wurde `navPaneLoadRangeForFreeNav(center)` in **opts.from/to** von `loadPane` gegeben → `explicit_history_window` + Voll-POST.

**Folge:** kein Phase-A-Request, kein `history-phase-b`, aber **`first_candles_owner_release` meldete fälschlich `phase: fast`** weil nur `mcLoadPostLoadDeferred` ohne `_mcLoadPhaseA`.

### C — Diagnose-Fehlklassifikation

`deferPost === true` ⇒ `phase: "fast"` wenn nicht `pane._mcLoadPhaseA` — **kein** echter Fast-First.

### D — `ema-after-candle-merge` (50k)

Nach `ensurePaneCaughtUp` → `mergePollCandlesIntoPane` (bis 3000 Bars) → `paneEmaStaleVsCandles` → debounced `refreshPaneEmaOverlays` → **50k POST**, oft **vor** Phase B und redundant zum ersten Pane-EMA.

---

## 2. Entry-Point-Matrix (nach P3A)

| Entry | Phase A (fast first an) | Ausnahme |
|-------|------------------------|----------|
| Initial / `pane-load` | Ja (wenn kein Pin) | Pin: opts `from`+`to`, GO TO, Replay |
| `symbol-switch` | Ja | wie oben |
| `tf-change` | Ja — **ohne** Nav-Range im POST | Replay: `tf-change-replay` |
| `tf-change` + Center | Phase A, danach `jumpChartsToUnix` | kein Full-POST mit Nav-Spanne |
| `history-phase-b` | Nein (full limit) | `preserveView` |
| GO TO / `go-to*` | Nein | `mcLoadOptsPinsHistoryWindow` |
| EMA catch-up / merge | Nein | `indicatorsOnly` |

**Gate `mc.load.fastFirst`:** Default **an**; Opt-out `localStorage mc.load.fastFirst = "0"`.

---

## 3. Minimaler Fix (P3A)

| Änderung | Datei |
|----------|--------|
| `mcLoadOptsPinsHistoryWindow` — nur **explizite** Fenster | `research_charts.js` |
| `mcLoadFastFirstFallbackReason` — dokumentierbare Skip-Gründe | `research_charts.js` |
| Phase-A-**reqBody** setzt `from`/`to`/`limit`; Full nur wenn **kein** Phase A | `loadPane` |
| **tf-change:** ein `loadPane(tf-change)`; Jump nach Phase A | TF-Handler |
| `first_candles_owner_release`: nur `phase: A` wenn `_mcLoadPhaseA` | `loadPane` |
| `_mcLoadAwaitingHistoryB`: kein `ema-after-candle-merge` / kein deferred EMA bis Phase B | `mergePollCandlesIntoPane`, `runPaneDeferredPostLoad` |
| Diagnose-Felder (s. u.) | `mc_load_diag_v1.js`, `loadPane`, `sendJson` |
| Asset **`research-mc-load-p3a-v1`** | `research_charts.html` |

---

## 4. Diagnose-Export (neu / erweitert)

| Feld | Bedeutung |
|------|-----------|
| `effective_fast_first` | Nur `true` wenn Phase-A-HTTP lief |
| `fast_first_fallback_reason` | z. B. `explicit_history_window`, `fast_first_disabled`, … |
| `phase_requested` / `phase_executed` | `A` / `B` / `full` |
| `phase_a_candle_count` / `phase_b_candle_count` | Nach Response |
| `phase_a_http_id` / `phase_b_http_id` | Korrelation Requests |
| `symbol_at_request` / `timeframe_at_request` / `load_generation` | Kontext |
| `request_completion_status` | `pending` / `completed` / `aborted` / `failed` |
| `frontend_timings_ms.*_sec` | Sekunden seit Trace-Start (kein ms/s-Verwechsel) |

**PASS-Kriterium:** `effective_fast_first === true`, `phase_a_candle_count` ≪ full weeks, danach `history-phase-b` + `phase_b_candle_count` ≈ 66804.

---

## 5. Ausgelieferte JS-Version prüfen

```bash
curl -s "http://<dashboard-host>/static/js/research/research_charts.js?v=research-mc-load-p3a-v1" | rg mcLoadOptsPinsHistoryWindow
# Konsole:
localStorage.getItem("mc.load.fastFirst")  // null oder "0" = aus
```

Hard-Reload mit **deaktiviertem Cache**; Template muss `research-mc-load-p3a-v1` referenzieren.

---

## 6. Tests

| Test | Ergebnis |
|------|----------|
| `test_mc_load_p3_fast_first_v1.py` | **PASS** (statisch, 3 Tests) |
| Browser SOL 1m→5m, BNB, Sym-Wechsel, GO TO, Opt-out, EMA-Parität | **BROWSER_PENDING** |

**Kein** Browser-PASS ohne JSON-Export.

### Manuelle Browser-Matrix (nach Deploy P3A)

1. `__MC_LOAD_DIAG__.start({ symbol: "SOLUSDT", timeframe: "5m", test: "P3A-tf" })`
2. SOL 1m → 5m `tf-change`
3. Erwarte: `load_started.phase: "A"`, `phase_a_candle_count` ~400, `history-phase-b`, dann `phase_b_candle_count` ~66k
4. `first_candles_owner_release.effective_fast_first: true`
5. Kein vorzeitiges `ema-after-candle-merge` vor Phase B (oder nur wenn echtes Stale nach B)

---

## 7. Call-Path nach Fix (tf-change, live)

```
tfSel.change → loadPane({ sourceAction: "tf-change" })
  → mcLoadFastFirstFallbackReason → null
  → Phase A reqBody (from/to/limit klein)
  → applyPaneBundle → setData
  → releasePaneLoadOwner (effective_fast_first)
  → schedulePaneHistoryPhaseB
  → deferred ensurePaneCaughtUp (ohne EMA-50k wenn awaiting B)
  → optional jumpChartsToUnix(center)
  → loadPane history-phase-b (preserveView, full limit)
```

---

## 8. Doppel-EMA

- **ema-post-catch-up:** unterdrückt während `_mcLoadAwaitingHistoryB` in deferred path.
- **ema-after-candle-merge:** unterdrückt während `_mcLoadAwaitingHistoryB`; nach Phase B gilt wieder kanonische Stale-Logik (`afterPaneBundle`).

---

## Ergebnis

| Frage | Antwort |
|-------|---------|
| Warum `tf-change` + `phase: full`? | Implizites History-`from`/`to` + ggf. Nav-Range im POST; **kein** Phase A |
| Warum `phase: fast`? | Diagnose-Bug: deferred ≠ Phase A |
| Fix deployed? | Repo **ja** `p3a-v1`; **Browser auf Zielhost unbestätigt** |

**STOP** — kein Commit/Push.