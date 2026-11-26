# MC-LOAD-P4E — Fix Report

**Endstatus:** `MC_LOAD_P4E_FIX_READY_BROWSER_PENDING`

Kein Live-Deployment, Commit oder Push (gemäß Vorgabe).

---

## Verifizierte Root Causes (Runtime P4D + Codepfad)

### RC1 — Phase-B-Request / API-Limit (P0)

| Feld | Befund |
|------|--------|
| **Symptom** | `request_limit: 24192`, Response `66804`, `cache: hit` |
| **Ursache** | Phase B nutzte `resolvePaneLoadRange()` → bei History-Preset **custom** volles `from`/`to` inkl. **veraltetem custom `to`** (~57 Tage vor Live-Tip). Bei gesetztem `from`/`to` ignorierte `_tail_limit_candles` das `limit` vollständig. |
| **Call chain** | `schedulePaneHistoryPhaseB` → `loadPane({_historyPhaseB})` → `resolvePaneLoadRange` → `computeHistoryRangeUnix` → `POST /api/research/pane` → `load_candles` → `_packed_from_cached` / `_tail_limit_candles` |

**Fix**

- Frontend: `mcLoadHistoryPhaseBRange()` — Live-Modus wie `liveRestorePaneLoadRange()` (`custom from` + **`to: now`**) bzw. rolling `{null,null,limit}`.
- Backend: `_range_end_is_live()` — bei **live `end`** (`now ± 30 min`) neueste `limit` Kerzen innerhalb des Fensters; historische Pins unverändert vollständig.

**Tests:** `test_candle_cache_limit_hit.py` (live pin + cache hit), `test_mc_load_p4e_v1.py`.

**Vorher/Nachher (erwartet, lokal):** Phase-B-Request `to ≈ now`, `candles_returned ≤ 24192` (+ forming); nicht mehr 66 804 mit altem `to`.

---

### RC2 — EMA stale nach Phase B (P0)

| Feld | Befund |
|------|--------|
| **Symptom** | Bundle-EMA `last_time: 1790764200`, Kerzen `1795685400`, `stale_vs_candles: true`; Catch-up `skipped: false`, Snapshot danach weiter stale |
| **Ursache A** | Historisches Fenster (RC1) → Server-EMA endet mit historischem `to`. |
| **Ursache B (entscheidend)** | `refreshPaneEmaOverlays` bricht ab, solange `pane._mcLoadAwaitingHistoryB === true` — Flag wurde erst **nach** EMA-Entscheidung in `loadPane` gelöscht → **Catch-up lief nie**. |

**Fix**

- `_mcLoadAwaitingHistoryB = false` direkt nach erfolgreichem Phase-B-`applyPaneBundle`.
- `paneEmaCatchUpRange({ afterHistoryPhaseB })` → `to: now`, `liveRestorePaneLoadRange` + Warmup.
- Phase-B-Complete: `request_completion_status: completed_ema_stale` solange EMA stale.

**Regression:** EMA `last_time` ≥ `last_candle_time - step` nach Phase B (Browser).

---

### RC3 — Konkurrierende Candle-Updates (P0)

| Feld | Befund |
|------|--------|
| **Symptom** | `304 → 3000 → 66804 → 69804`; Polls `from=1790764200&limit=3000` |
| **Ursache** | Phase B ersetzte Serie mit historischem Tip; `lastClosedBarTime` → Poll von historischem Cursor; parallele Polls während Phase-B-Apply. |

**Fix**

- `pane._historyPhaseBInflight` — `mergePollCandlesIntoPane` no-op während Phase B.
- `mergeLiveTailCandles` — Phase-B-Apply behält Phase-A-Live-Tail, wenn Bundle älteren Tip hat.

**69804 ≈ 66804 + 3000** bestätigt Poll-Gap nach historischem Tip (Messung); Merge verhindert Verlust des Live-Tails beim Apply.

---

### RC4 — Viewport t≈26 s (P0, teilweise)

| Feld | Befund |
|------|--------|
| **Symptom** | Sprung nach `mc_load_phase_b_complete`; `gotoNavViewCenterTs: 1790769630` |
| **Wahrscheinliche Mitursachen** | (1) `preserveViewTime` auf Serie mit historischem Ende; (2) persistiertes `gotoNavViewCenterTs` aus historischer Mitte (TF-Wechsel-Jump); (3) späteres `setData` mit ~66804 ohne Live-Merge |
| **Nicht instrumentiert** | Exakter Caller des t=26-Sprungs (z. B. EO async, verzögerter EMA-Reload) — **ohne P4E-Browser-Export nicht erneut verifiziert** |

**Fix**

- `capturePaneViewportSnapshot` unmittelbar vor Apply → `chart.setData({ viewportSnapshot })`.
- `clearStaleGotoNavCenterAfterLiveLoad` — Center >14 Tage hinter `lastClosedBarTime` löschen.
- RC1–RC3 reduzieren historischen Vollreplace.

**Verbleibendes Risiko:** Async Exact-Origin / `jumpChartsToUnix` bei gültigem Center — bei Bedarf Diag-Event `viewport_restore_caller` (nicht in P4E implementiert).

---

## READ-ONLY — Candle Integrity (SOLUSDT 1795685460)

| Prüfpfad | Ergebnis |
|----------|----------|
| **Kanonsiche CH-1m** | In dieser Session **nicht** abgefragt (keine DB-Mutation, kein Auth-Curl auf CH). |
| **Plausible Herkunft** | `apply_live_forming_tip` / `applyFormingToPane` — Forming-Bar kann `high`/`low` aus Collector mit **schmal absolutem Low** (70.89) gegenüber `open` 122.28 stampen; wirkt wie Ausreißer, nicht zwingend CH-History. |
| **Doppel-Timestamp 1795685640** | `mergePollCandlesIntoPane` keyed by `time`; letzter Schreibvorgang gewinnt — Duplikate im **Debug-Payload** eher **Snapshot-Artefakt** (forming + closed) oder Debug-Export; Code dedupliziert beim Merge. |

**Empfehlung:** Separater CH-1m-Read für `1795685460` / `1795685640` vor Datenänderung.

---

## Geänderte Dateien (Dashboard-Repo)

| Datei | Änderung |
|-------|----------|
| `research_charts/service.py` | `_range_end_is_live`, `_tail_limit_candles` |
| `static/js/research/research_charts.js` | Phase-B-Range, EMA-Gate, Merge, Viewport-Snapshot, Inflight |
| `static/research_trp/chart.js` | `viewportSnapshot` in `setData` |
| Templates / `pane.html` / `mc_load_diag_v1.js` | Asset `mc-load-p4e-v1` |
| Tests | `test_mc_load_p4e_v1.py`, erweiterte Cache-Tests |

---

## Regressionstests (ausgeführt)

```text
pytest test_mc_load_p3_fast_first_v1.py test_mc_load_p4e_v1.py research_charts/tests/test_candle_cache_limit_hit.py → 9 passed
```

**Browser noch offen:** SOLUSDT 1m→5m (Test A/B/C), Custom-History-Preset, Pan/Zoom in Phase B, EMA-Endzeit = Kerzen-Tip, kein Sprung ~26 s.

---

## P4B-Verbesserungen

`preserveViewTime`, stale-aware `mcLoadSkipEmaCatchUpAfterPaneBundle`, Cache-Tail auf unbounded Hits — **unverändert beibehalten**, auf P4E erweitert.

---

## Deployment-Hinweis (manuell durch Benutzer)

Service-Neustart + Hard-Reload `mc-load-p4e-v1` erforderlich — **nicht** in P4E ausgeführt.