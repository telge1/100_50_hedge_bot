# MC-LOAD-P2 Request Dependencies (BNBUSDT 5m Initial Load)

READ-ONLY Dependency-Graph für den beobachteten Trace (26.11.2026).  
Zahlen aus **User-Trace**; Kanten aus **Code**.

---

## Request-Übersicht (Trace)

| # | Endpoint | sourceAction | candles | Server ~ms | Cache | Blockiert `loading…`? |
|---|----------|----------------|---------|------------|-------|----------------------|
| R1 | `POST /api/research/pane` | `pane-load` | 66.804 | 22.387 | miss | Ja (Owner aktiv) |
| R2 | `POST /api/research/pane` | `ema-post-catch-up` | 50.000 | 13.062 | miss | Ja (vor `releasePaneLoadOwner`) |
| R3 | `GET /api/research/candles` | `gap-backfill` | 3.000 | 2.172 | miss | Ja (`await ensurePaneCaughtUp`) |
| — | `GET /api/research/ob1000-levels` | (User-Konsole) | n/a | — | — | Nein (parallel, wenn OBL an) |

---

## R1 — Initial Pane Load

**Trigger:** Symbol/TF-Boot oder Reload → `loadPane(paneId, { sourceAction: "pane-load" })`.

**Request body (relevant):**

| Feld | Quelle | Wert (Typisch) |
|------|--------|----------------|
| `limit` | `historyLoadLimitForTimeframe("5m")` | ≈ `weeks × 2016` → **66804** bei ~33 Wochen |
| `from` / `to` | `resolvePaneLoadRange()` | oft **null** (rolling History) |
| `ema` | `state.workspace.ema` | lines enabled → Server `ema_overlays_payload` |
| `liquidity` | workspace | Trace: **off** |

**Server DAG:**

```
api_research_pane
  └── pane_bundle
        ├── resolve_candle_pack(limit)
        │     └── load_candles
        │           ├── CH: get_1m_candles(limit = lim×need+pad)
        │           ├── aggregate → 5m
        │           └── trim to lim (max MAX_LIMIT)
        ├── _indicators_from_candles (EMA)
        └── _build_lld_section (skipped wenn liquidity off)
```

**Client nach Response:**

```
whenReady(pane) [Trace ~0 ms]
  └── applyPaneBundle
        ├── chart.setData(66804)  ← Main-Thread-heavy
        ├── chart.setEmaOverlays
        └── … optional panes …
  └── ensurePaneCaughtUp → R3
  └── wenn stale → R2
  └── releasePaneLoadOwner
```

**Abhängigkeiten (wer braucht R1-Daten):**

| Consumer | Needs full R1 candle array |
|----------|---------------------------|
| LWC price series | Yes |
| EMA overlays (first paint) | Server series length ≈ candles |
| `state.history.loadedFrom/To` | Yes |
| Volume profile / OBP / OPI schedulers | Later async; not blocking R1 |
| Exact origin / ACTIVE | Trace off |

---

## R2 — EMA Post Catch-Up

**Trigger:** `paneEmaStaleVsCandles(pane) === true` after R1 + R3 path.

**Parent:** `refreshPaneEmaOverlays` → `loadPane({ indicatorsOnly: true, sourceAction: "ema-post-catch-up", from, to, limit: 50000 })`.

**Range:**

- `from = candle_min - warm×300` (`warm` = max EMA period + 20, default lines → **220** für EMA200)
- `limit = min(50000, len(candles)+warm+5)` → **50000**

**Overlap mit R1:**

```
R1: [t_min … t_max]  ×  ~66804 bars (full history)
R2: [t_min - 220×5m … t_max] × up to 50000 bars
```

→ Großteil identisch; R2 erneut CH read + JSON parse; **kein** `setData` wenn `indicatorsOnly` (nur EMA refresh im chart).

**Depends on:** R1 completed (`pane.pendingData` populated).

---

## R3 — Gap Backfill

**Trigger:** `ensurePaneCaughtUp` if `paneClosedTipLagSec > 2× step`.

**Request:** `GET /api/research/candles?from=<lastClosed>&limit=<pollLimit>` → **3000** bei großem Lag.

**Depends on:** R1 `setData` (needs `lastClosedBarTime`).

**Necessary for first paint?** **No** for default view on historical tail; **Yes** for live tip accuracy if CH pack was behind wall clock.

**Side effects after merge:** may retrigger `scheduleRefreshPaneEmaOverlays` via `mergePollCandlesIntoPane` (separate debounced path).

---

## Parallel / Orthogonal Requests

| Request family | Gated by | Blocks candle owner? |
|----------------|----------|----------------------|
| `ob1000/lease` | `isOnDemandBookMode()` | No |
| `ob1000-levels` | `state.obl.enabled` | No |
| Pool / PD1 refresh | various flags | No (void after load) |
| Live poll | `initialLoadDone` | No |

**Diag pollution:** Any parallel `getJson` may overwrite `frontend_timings_ms.candle_response_received` (see root cause §7).

---

## Limit vs. Display (5m)

| Concept | Value | Source |
|---------|-------|--------|
| Bars requested (client) | ~66804 | `historyLoadLimitForTimeframe` |
| Server cap (code) | 50000 | `MAX_LIMIT` |
| Visible default | 120 | `DEFAULT_VISIBLE_BARS` |
| EMA warmup bars (server GO-TO logic) | max period 200+ | `_max_indicator_warmup_bars` |
| EMA reload cap | 50000 | `PANE_EMA_RELOAD_LIMIT_CAP` |

---

## Serial load owner timeline (conceptual)

```
registerPaneLoadOwner → "loading…"
  → R1 fetch
  → applyPaneBundle (setData)
  → R3 await
  → R2 await (if stale)
  → optional layer schedules (void)
  → releasePaneLoadOwner → hide loading
```

**Critical path for trace `loading_hidden_ms`:** R1 + setData + R3 + R2 (not optional pools).

**STOP**