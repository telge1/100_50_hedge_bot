# MC-LOAD-P1 — Research Chart 5m Candle Load Architecture

## Call chain (Single View)

```text
Browser /live-charts/research?view=single
  → research_charts.js boot()
  → switchSymbol() | loadPane("pane-0")
       → POST /api/research/pane  (pane_bundle: candles + EMA/Stoch/OI + LLD overlays wenn aktiv)
       → await whenReady(pane)     (iframe chartApi, max 8s)
       → applyPaneBundle()
            → chart.setData()      (TRP chart.js, Lightweight Charts)
       → await ensurePaneCaughtUp() (optional GET /api/research/candles)
       → refreshHistoricalLmv2 / OBL / OPI … (meist void, nicht await vor release)
       → await PD1ExactOriginResearch.refreshPane() nur wenn Exact Origin an
       → releasePaneLoadOwner() → trp-pane-status ≠ "loading…"
  Parallel (nicht blockierend vor setData):
       → refreshHistoricalLmv2 nach loadPane (void)
       → scheduleOrderbookLevels nach loadPane (void)
```

## Kanonische Module

| Stage | Modul |
|-------|--------|
| UI load gate | `loadPane`, `applyPaneBundle`, `switchSymbol` — `static/js/research/research_charts.js` |
| HTTP POST bundle | `sendJson` → `/api/research/pane` — `research_charts/api.py` → `pane_bundle()` |
| Candles CH | `resolve_candle_pack` / `load_candles` — `research_charts/service.py`, `clickhouse_source.py` |
| Chart render | `setData` — `static/research_trp/chart.js`, LWC **v4.2.3** |
| ACTIVE pools (LLD in bundle) | `_build_lld_section` inside `pane_bundle` when `liquidity.enabled` |
| ACTIVE pools (viewport) | `refreshHistoricalLmv2` — separate POST, **async** after candles |
| Orderbook levels | `scheduleOrderbookLevels` — GET, **nach** `applyPaneBundle` |
| Loading UI | `trp-pane-status` — `"loading…"` bis `releasePaneLoadOwner` |

## Bestehende Diagnose

- `state.requestLog` (120 Einträge) via `logReq`
- `research.paneSendJsonDiag` + `window.__researchPaneSendJsonDiag`
- **Neu P1:** `window.__MC_LOAD_DIAG__` + `mc_load_diag_v1.js` (Ringbuffer, JSON-Export)

## P1-Hypothese (Code, nicht Browser-PASS)

1. `loading…` hängt an **gesamtem** `loadPane` bis `releasePaneLoadOwner`, nicht nur an `setData`.
2. `await whenReady` vor `setData` kann verzögern, wenn iframe/langsam.
3. `await ensurePaneCaughtUp` nach `setData` verlängert „loading…“ sichtbar.
4. **ACTIVE (LLD)** steckt in **demselben** `pane_bundle`-Request (seriell mit Candles), nicht erst danach.

**STOP (Architektur)**