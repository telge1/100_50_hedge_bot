# MC-LOAD-P1 Root Cause Analysis (P1 Diagnostic)

## Classification key

- **MEASURED** — reproduced in this session (server Python benchmark)
- **SUSPECTED** — derived from static code path review
- **NOT_MEASURED** — requires browser trace (`__MC_LOAD_DIAG__`)

---

## Top 3 delay sources (BNBUSDT 5m, ACTIVE on)

| Rank | Source | Class | Evidence |
|------|--------|-------|----------|
| 1 | ClickHouse candle fetch (`resolve_candle_pack`) | **MEASURED** | **~475 ms** cache miss, BNB 5m × 1500 |
| 2 | LLD / ACTIVE overlay build inside `pane_bundle` | **MEASURED** | +**~30–515 ms** (cold miss total ~516 ms; warm ~28–37 ms LLD on hot candle cache) |
| 3 | Frontend `loading…` gate after HTTP returns | **SUSPECTED** | `loadPane` awaits `whenReady` (≤8s), then `ensurePaneCaughtUp`, optional `exactOrigin` **before** `releasePaneLoadOwner` |

---

## Question matrix

| # | Question | Answer |
|---|----------|--------|
| 1 | Where does BNB 5m lose most time? | **MEASURED:** server `pane_bundle` dominated by **candle CH read**; with ACTIVE, full POST ~516 ms cold. |
| 2 | Is CH/backend slow? | **MEASURED:** yes for BNB candle pack cold (~475 ms). Not browser-tested E2E. |
| 3 | Chart waits on optional layers before candles? | **SUSPECTED:** **No** for `setData` — pools/OBL scheduled **after** `applyPaneBundle`. **Yes** for clearing `loading…` — owner held through `ensurePaneCaughtUp` (+ exact origin if on). |
| 4 | Duplicate requests / races? | **SUSPECTED:** `loadGen`/`paneGen` abort stale loads; `inflightPosts` coalesce POST pane. **NOT_MEASURED** in browser. |
| 5 | When do candles actually appear? | **NOT_MEASURED** E2E. **SUSPECTED:** after `whenReady` + POST + `applyPaneBundle.setData`. |
| 6 | Why `loading…` stays? | **SUSPECTED:** status set at POST start; cleared only in `releasePaneLoadOwner` after post-`setData` awaits. |
| 7 | Smallest safe fix (P2, not P1)? | **SUSPECTED:** decouple status from `ensurePaneCaughtUp`; split LLD from pane POST or stream candles-first JSON. **Not implemented in P1.** |
| 8 | JSON export? | `window.__MC_LOAD_DIAG__.exportJSON()` after `start()`/`stop()` — see `MC_LOAD_P1_TRACE_SCHEMA.md`. |

---

## ACTIVE vs Orderbook blocking

| Layer | Blocks `setData`? | Blocks `loading…` clear? |
|-------|-------------------|---------------------------|
| ACTIVE LLD in `pane_bundle` | **Yes** (same POST) | **Yes** (same POST wait) |
| `refreshHistoricalLmv2` | No (void after load) | No |
| Orderbook levels | No | No |
| OPI | No | No |
| Exact Origin refresh | No (after setData) | **Yes** if enabled (`await` before release) |

**STOP**