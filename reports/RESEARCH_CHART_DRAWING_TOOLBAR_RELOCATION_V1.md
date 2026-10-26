# Research Chart — Drawing Toolbar Relocation V1

**Date:** 2026-10-22 (updated: GO TO / Layout in pane header; Zeit dock row removed)  
**Scope:** UI layout only (drawing toolbar, GO TO, layout in chart pane header)

---

## Changed files

| File | Change |
|------|--------|
| `spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html` | `#trpDrawToolbar` + `#trpPaneNavControls` (GO TO, Springen, Layout); Zeit-Zeile entfernt; History-Felder in `#researchHistoryHidden`; CSS `phase6xt-pane-goto-layout-header-v2` |
| `spread_recovery_hedge_short_dev/dashboard/static/js/research/research_charts.js` | `mountDrawToolbar` / `drawToolbarTargetPaneId`; pane header host `.trp-pane-draw-host`; mount on layout, pane focus, chart click |
| `spread_recovery_hedge_short_dev/dashboard/static/css/research.css` | Pane header flex-wrap; draw toolbar host/toolbar styles; status ellipsis |

**Unchanged:** Candle API, pane load, V7 diag, LMV2/density/min-depth/canvas logic, `buildTools()` tool list and IDs, `bindUi` listeners on `trpTools` / `trpDrawColor` / `trpDrawWidth` / `trpDelete` / `trpClear` / `trpPositionSettings`.

---

## New DOM position

**Order in each pane header (`buildPanes`):**

`SYMBOL` | `TF` | `⟲` | `⛶` | **Zeichenwerkzeuge** | *Abstand* | **GO TO** + **Springen** | *Abstand* | **LAYOUT 1/2H/2V/4** | `status` (right)

The **single** toolbar DOM node (`#trpDrawToolbar`) is **moved** into the active target pane’s `.trp-pane-draw-host`:

- Default: first visible pane (layout 1 → `pane-0`).
- Pane fullscreen: toolbar on the fullscreen pane.
- Multi-pane: follows last header mousedown or chart click on that pane.

**Removed from dock:** Complete **`researchDockRowTime`** (Zeit, History UI, Laden, Chart nach Backtest, Dock-Vollbild, Resize). **Hist. Pools** row unchanged. History preset/apply/hints remain in hidden `#researchHistoryHidden` for JS/Backtest/GO-TO logic.

---

## Preserved behavior

- All tool modes (`TOOLS` / `setTool` / workspace `tool`).
- Color and line width (`trpDrawColor`, `trpDrawWidth` → `/api/research/drawings/style`).
- Delete / clear / position settings (same IDs and listeners).
- Drawings still tagged with `pane_id` in `handleDrawingEvent`.
- Per-pane reset (`.trp-reset`) and pane fullscreen (`.trp-fs`) unchanged.
- Layout buttons 1 / 2H / 2V / 4 and `applyLayout` unchanged.

---

## Tests

| Test | Result |
|------|--------|
| `pytest test_research_charts_interaction.py::test_host_toolbar_bridge_table` | **pass** |
| `pytest test_research_charts_interaction.py` (broader) | 17 passed; failures in DB/CH-dependent cases (`pane_bundle`, coalesce, pool backtester) — **pre-existing / env**, not toolbar DOM |

**Manual checklist (operator):** all tools, color/width, drawings on correct pane, TF change, pane + page fullscreen, layouts 1/2H/2V/4, no duplicate IDs, console clean.

---

## Limitations

1. **One physical toolbar** — not cloned per pane (avoids duplicate IDs/listeners). In multi-pane layouts the bar appears in **one** pane header at a time (focused/clicked pane or pane fullscreen).
2. **Narrow headers** — `flex-wrap` + `max-height: 72px` on `.trp-pane-header`; very small widths may wrap tools to a second line within the header (does not overlay the chart iframe).
3. **Hard refresh** required to load new HTML/CSS (`?v=phase6xt-pane-draw-toolbar-header-v1`).

---

## STOP

No server restart required for static files. No git commit/push in this task.
