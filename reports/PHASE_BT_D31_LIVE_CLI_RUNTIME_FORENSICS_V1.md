# PHASE BT-D31 — Live CLI Runtime Forensics (Read-Only)

**Date:** 2026-10-26  
**Mode:** READ-ONLY — no code/config changes, no restarts, no fixes  
**Dashboard:** `spread_recovery_hedge_short_dev/dashboard`  
**Artifacts:** `results/phase_bt_d31_live_cli_forensics_v1/`

---

## VERDICT

**`LIVE_RUNTIME_ROOT_CAUSE_CONFIRMED`**

Primary confirmed failure: **the live `app.py` process on port 3000 accepts TCP connections but does not complete HTTP responses** (static files and pages time out). That blocks **B – Exact Origin `fetchWindow`** and **BT-D26 `load`/`sync-viewport`**, producing **no density canvas** and a **stale “lade PLANNED setups …”** label.

Secondary confirmed (not a loader bug): at **GO TO 2026-09-11 11:34:51 UTC**, BT-D30 **“Aktueller Kandidat”** has **no** active setup (first emit **12:00 UTC**). The **visible PLANNED box + 11 vline markers** match **persisted TRP drawings** on disk (`1` planned + `11` markers), not a successful live sync at that as-of.

Browser UI not re-tested (no session); CLI auth limitation noted below.

---

## A. Live process and ports

| Item | Value |
|------|--------|
| PID | **2936957** |
| Command | `.venv/bin/python app.py` |
| CWD | `.../spread_recovery_hedge_short_dev/dashboard` |
| Listen | `0.0.0.0:3000` |
| PYTHONPATH | `spread_recovery_hedge_short_dev`, `dashboard`, `pools+ob+delta_bot` |
| `:8080` | Responds **301** (proxy); not fully traced to backend |

**Socket evidence:** multiple `ESTAB` connections to `:3000` with **`Recv-Q=607`** and **`curl` timeouts (0 bytes)** while **TCP connect succeeds** → event loop / worker saturation or long-running handlers blocking responses.

See `process_ports.json`.

---

## B. Served assets (disk vs live HTTP)

| Check | Result |
|--------|--------|
| Template script `?v=` | All **`phase6xt-bt-d30a-live-v1`** (aligned) |
| `ASSET_V` in `research_charts.js` | **`phase6xt-bt-d30a-live-v1`** |
| BT-D30A helpers on disk | `repaintExactOriginDensityAllVisiblePanes`, `btD26DisplayMode` present |
| Live `GET` static JS | **Timeout** (15s, 0 bytes) — cannot byte-compare over HTTP |

**Conclusion:** Working-tree assets are **coherent**; **bt-d29 mismatch ruled out** on server disk. Browser cache not verified.

See `served_asset_versions.json`.

---

## C. API endpoints

Routes registered in `research_charts/api.py` (grep): BT-D26 **runs / load / sync-viewport**; viewer window via `pd1_landscape_viewer_v1/api.py`.

**Live HTTP probes:** failed with **timeouts** (not 401). Unauthenticated CLI cannot distinguish auth vs stall for POST APIs; **401 without cookie is expected** when the server responds.

**In-process (same code, no HTTP):** `load_setups_document` → **102 setups** OK; `build_landscape_window` → **262 pools** for GO TO ±4h window.

See `route_inventory.json`, `api_probe_results.json`.

---

## D. Dynamic selector load path

**Code path** (`research_charts.js`):

1. `setStatus("… lade PLANNED setups …")` (~8827)  
2. `applyBtD26DynamicSelectorBacktester` → `POST …/load`  
3. On success: `setStatus(backtester.message)` **before** optional `zoomChartToIsoRange` (~6170–6174)

**Runtime:** With HTTP stalled, step 2 **does not complete** → status **remains** on “lade …”.

**Visible box + markers despite stall:** `research_charts/user_data/drawings.json` contains **`btd26-planned` ×1** and **`btd26-marker` ×11** — matches screenshot pattern from **earlier** successful sync.

See `dynamic_load_trace.json`.

---

## E. Density end-to-end

| Stage | CLI result |
|--------|------------|
| Replay view (11:34:51 ±4h) | `resolveViewSec` rule: `replayTargetTs ± pad` |
| `build_landscape_window` (in-process) | **262 pools**, `phase5s_batch_reader_active` |
| Live `POST …/viewer/api/window` | **Not reachable** (HTTP timeout) |
| Canvas / `setExactOriginDensity` | **Cannot observe** without browser + working HTTP |

**Conclusion:** Density SSOT path **works in-process**; **missing bands in browser** are **consistent with blocked HTTP**, not with empty catalog data.

See `density_pipeline_trace.json`.

---

## F. Time / viewport (screenshot GO TO 11:34:51 UTC)

| Layer | As-of / window | Outcome |
|--------|----------------|---------|
| B density (replay) | 07:34–15:34 UTC | Pools available in-process |
| BT-D26 **Aktueller Kandidat** | as-of **11:34:51** | **NONE** (first setup **12:00**) |
| BT-D26 at **15:35** | — | ACTIVE entry **0.08681** (reference) |
| Persisted drawings | — | 1 box + 11 markers (any as-of) |

User-visible box at 11:34 is **not** explained by current-candidate logic at that timestamp; **persisted drawings** explain it.

See `viewport_time_analysis.json`.

---

## G. Isolation scenarios (CLI-only)

| Scenario | CLI assessment |
|----------|----------------|
| B ON, no backtester | Blocked if HTTP stall |
| BT-D26 without B | Drawings can still render from file |
| B + BT-D26 | Both need HTTP for fresh data; repaint on disk after overlay sync cannot run without JS execution |
| Strategy switch | Not exercised in browser |

---

## H. Root cause matrix

Full table: `results/phase_bt_d31_live_cli_forensics_v1/root_cause_matrix.md`

---

## I. Recommended next steps (outside BT-D31 scope — not executed)

Operational recovery only (user did not authorize in this phase): investigate **why PID 2936957 blocks HTTP** (long CH/pane requests, connection backlog), then restore responsive `:3000` before re-testing B + BT-D26 in browser.

---

## Constraints honored

No code changes, no restarts, no config edits, no commits.
