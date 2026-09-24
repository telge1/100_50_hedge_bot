# Dashboard Cluster Sweep Backtester — Abschlussbericht

**Verdikt: `DASHBOARD_CLUSTER_SWEEP_BACKTESTER_READY`**

Keine Profitabilitätsaussage. Ziel: Cluster-Sweep im bestehenden Research Chart kausal prüfen.

## A. Repositories / Git

| Repo | Branch | HEAD | Dirty |
|---|---|---|---|
| `spread_recovery_hedge_short_dev` | `feature/dashboard-research-charts` | `70c64798eaa3715458520784a145860d28c9064c` | viele vorbestehende Stoch/Research-Änderungen — unberührt außer gezielte Integration |
| `orderbook_analyse` | `research/confirmed-orderbook-entries` | `136a9860311495ecf841c488776643222858077a` | vorbestehend `coin_regime_scanner` / `fake_impulse_filter` unberührt; `cluster_sweep_research` erweitert |

Seite `/live-charts/research` liegt im **Dashboard-Repo**.

## B. Stoch-Fade-Fundorte

| Thema | Ort |
|---|---|
| UI Backtester-Button | `templates/research_charts.html` `#researchBacktesterBtn` |
| JS Load | `static/js/research/research_charts.js` → `POST /api/research/backtester/load` |
| API | `research_charts/api.py` |
| Signal→Position | `research_charts/stoch_backtester.py` `signal_to_position_spec` |
| Workspace Import | `workspace_session.import_stoch_backtester` (Long/Short Drawings) |
| Jobs „Backtest starten“ | Stoch-Signale-Seite (`stoch_signale.html`), nicht Research Charts |

Cluster Sweep nutzt dieselbe Backtester-Schaltfläche und erweitert die API um `strategy_id` + `POST /api/research/backtester/run`.

## C. Wiederverwendete OA-Komponenten

Adapter `research_charts/oa_import.py` → `orderbook_analyse.cluster_sweep_research` (kein Copy der LLD-Formel).
Pipeline: `pipeline.run_cluster_sweep_on_candles`, Detector, Enrichment, Outcomes, TRP LLD via OA-Adapter.

## D. Ursache der falsch wirkenden Bull/Bear-Events

1. **Approach-only / schwache Filter** und **pool_count&lt;3** erzeugten viele Marker.
2. **UI vs. Features:** Markerzeiten = Candle-`open_time`; Hover zeigte Candidate-EMAs, während der Chart-Close anderer Kerzen „falschen“ Stack suggerierte.
3. **Confirmation ohne gespeicherten EMA-Audit:** `CLOSE_BACK_IN_CLUSTER` konnte CONFIRMED wirken, ohne dass Confirmation-Stack im Artefakt prüfbar war.
4. **Dedup fehlte** → mehrere überlappende Low-Pool-Zonen.

## E. Fachlicher Fix

- Candidate erfordert Cluster-**Entry** + Stack am Candidate-Close.
- `_resolve_forward`: Stack auf jeder Forward-Bar; bei Bruch → `INVALIDATED`, **keine** spätere Confirmation.
- `ema_audit` an Candidate / Sweep / Confirmation.
- `final_status=CONFIRMED` nur mit `structure_ok` am Confirm.
- `prior_touch_count` + deterministische Dedup überlappender Events.
- Default `minimum_cluster_pools=3`; `&lt;3` nur mit **Low-pool debug**.

Smoke (XRPUSDT 5m 14–22 UTC, debug): 3 Events → 2 CONFIRMED (Stack ok) + 1 INVALIDATED.

## F. Backend-API

- `POST /api/research/backtester/run` — `strategy_id=cluster_sweep_ema_9_20_59`, Symbol/TF/Start/Ende/EMA/min_pools/debug/detail
- `POST /api/research/backtester/load` — Stoch unverändert; bei Cluster Sweep Toggle `visible`
- `POST /api/research/backtester/cluster-sweep/nav` — Eventnavigation

Response: `meta`, `coverage`, `events` (inkl. `ema_audit`, Orderflow, MFE/MAE), Marker-Specs → OverlayMarkers.

## G. Frontend

Strategy-Select, „Backtest starten“, CS-Settings-Modal (UTC), Debug-Badge, Nav ◀/▶/Zoom, Event-Detailpanel mit Review-Feldern (lokal, kein Auto-MATCH).

## H. Navigation

`cluster_sweep.event_index` / `n_events`; Zoom ±30 min um Event; Backtester blendet Marker ein/aus.

## I. Cluster-Mindestwert

Default **3**. Debug: Checkbox „Low-pool debug zones“ + Badge.

## J. Neue/geänderte Dateien

**OA:** `event_detector.py`, `pipeline.py`, `audit_export.py`, Enrichment/Loader (bestehend)

**Dashboard:**
- `research_charts/oa_import.py`
- `research_charts/cluster_sweep_backtester.py`
- `research_charts/api.py`, `workspace_session.py`, `trp_import.py`
- `templates/research_charts.html`, `static/js/research/research_charts.js`, `static/css/research.css`
- `tests/test_cluster_sweep_backtester.py`

## K. Tests

```
OA: 23 passed (test_cluster_sweep_research.py)
Dashboard: 8 passed (test_cluster_sweep_backtester.py)  # OA-venv (pandas)
```

## L. Browser-QA

`http://dash.immotel.de:8080/live-charts/research` antwortet mit Auth-Gate (kein unautorisierter Login).  
Code/API/Smoke lokal verifiziert. **Produktions-Dashboard-Prozess wurde nicht neu gestartet** — Deploy nötig, damit UI live erscheint.

Manuelle Checkliste nach Deploy: XRPUSDT → 5m → Zeitraum → Cluster Sweep → Backtest → Coverage → Backtester → bull/bear Event → Nav → ausblenden → Stoch Regression.

## M. Grenzen

- Review-Felder nicht persistent (keine Prod-Migration).
- Lokaler Candle-Fallback, wenn Research-Symbolkatalog andere CH-DB nutzt.
- ORDERFLOW_REVERSAL weiter nur Platzhalter.
- Kein Universe-Lauf.

## Follow-up (Stoch-map review)

- Event-Zoom nutzte fälschlich `pane.chart.setVisibleTimeRange` (existierte nicht).
- Nachgezogen: `chart.js` exportiert `setVisibleTimeRange`; Host zoomt über `api(pane)`.
