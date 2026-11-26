# MC-LOAD-P4A — Phase-B-Limit & TF-Wechsel-Stale-Arbeit

**Code:** `spread_recovery_hedge_short_dev/dashboard` (`mc-load-p4a-v1`)  
**Browser P4A (nach Fix):** `BROWSER_PENDING`  
**P4-Browser (EMA-Skip, Fast-First):** vom Nutzer bestätigt (SOLUSDT 1m→5m).

## A. Befund `limit: 24192` → 66.804 Kerzen (cache: hit)

### Request-Kette (history-phase-b)

| Stufe | Verhalten |
|-------|-----------|
| **Frontend** | `loadPane(..., _historyPhaseB: true)`: `reqBody.limit = historyLoadLimitForTimeframe(pane.tf)` → bei 12 Wochen/5m **24.192**; kein `from`/`to`. |
| **POST** | `/api/research/pane` → `pane_bundle(symbol, tf, start=None, end=None, limit=24192, ...)`. |
| **`resolve_candle_pack`** | Cache-Key `(symbol, timeframe, start, end, lim)` = `(SOLUSDT, 5m, None, None, 24192)`. |
| **Miss-Pfad** | Aggregation + `candles[-lim:]` vor `_cache_put`. |
| **Hit-Pfad (Bug)** | `_packed_from_cached` serialisierte die **gesamte** gecachte Liste ohne erneutes `[-lim:]`. |
| **Response** | `"limit": 24192` in Metadaten, aber **`candles.length` = gecachte Länge** (z. B. 66.804). |

**Warum 66.804?** Aktueller Code cappt Neuberechnungen mit `MAX_LIMIT = 50000`; 66.804 > 50.000 ⇒ die Liste kam **nicht** aus einem frischen Miss mit `limit=24192`, sondern aus einem **Cache-Wert**, der unter dem Schlüssel `(…, 24192)` mit zu vielen Kerzen lag ( älterer Prozess/höheres früheres `MAX_LIMIT`, fehlendes Trim vor `put`, oder manuell überlanger Eintrag).

Cache-Key enthält `lim`; es gibt **keinen** separaten Superset-Key-Lookup. Das Problem ist **Wiederverwendung eines überlangen Blob-Werts** unter dem korrekten Limit-Key, kombiniert mit fehlendem Trim beim Hit.

### Fix P4A (minimal, semantik-erhaltend)

`research_charts/service.py`:

- `_tail_limit_candles(candles, start, end, lim)` — identisch zur Miss-Logik: nur wenn `start` und `end` beide `None`, `candles[-lim:]`.
- Anwendung in `_packed_from_cached` (Cache-Hit) und zusätzlich vor `_cache_put` (Defense in depth).
- **Kein** Trim bei explizitem GO-TO-/History-Fenster (`start`/`end` gesetzt).

Tests: `research_charts/tests/test_candle_cache_limit_hit.py`.

## B. TF-Wechsel 1m → 5m — alte 1m-Arbeit

### Beobachtung (P4-Browser)

- Vor Wechsel: 1m `setData` ~334k (erwartetes 1m-History-Fenster).
- Während 5m-Load: **1m** `ema-after-candle-merge` ~50k / 7,6s.

### Ursachen (Code-Audit)

| Mechanismus | Lücke |
|-------------|--------|
| `tf-change` | setzt `pane.tf` sofort, ruft **kein** Abort; alte `paneGen`-Jobs laufen weiter. |
| `ensurePaneCaughtUp` | prüfte `loadGen`, **nicht** `paneGen`; konnte nach TF-Wechsel noch mergen. |
| `mergePollCandlesIntoPane` | keine Prüfung `packed.timeframe === pane.tf`. |
| `schedulePaneHistoryPhaseB` | nur `loadGen`, nicht `paneGen` beim Timeout. |
| EMA-Debounce | Timer aus 1m-Ära konnte noch feuern. |

### Fix P4A (minimal)

`research_charts.js`:

- `abortStalePaneWork(pane)` — `paneGen++`, EMA-Timer clear, Fast-First-Flags reset.
- Aufruf bei **TF-Wechsel** und in `abortInflightPaneLoads()` (Replay).
- `ensurePaneCaughtUp`: `paneGen` + Timeframe-Mismatch nach HTTP.
- `mergePollCandlesIntoPane`: Abbruch bei TF-Mismatch.
- `schedulePaneHistoryPhaseB`: `schedulePaneGen`-Guard.
- `refreshPaneEmaOverlays`: `loadAbort.signal`, `emaPaneGen`-Guard.

**Nicht geändert:** EMA-Formeln, Poll-Intervalle für gültiges TF, PD1/OPI/OBL/Trading.

## C. Nachweise / Regression

| Check | Status |
|-------|--------|
| `pytest` P3 + cache limit | 6 passed |
| `node --check` | OK |
| Browser Phase B = 24.192 nach Cache-Hit | `BROWSER_PENDING` |
| Kein 1m-EMA während 5m-Wechsel | `BROWSER_PENDING` |

## Artefakte

- `MC_LOAD_P4A_PHASE_B_TRACE.md`
- `MC_LOAD_P4A_TF_STALE_WORK.md`
- `MC_LOAD_P4A_BASELINE.md`
- `MC_LOAD_P4A_CODE_CHANGELOG.md`