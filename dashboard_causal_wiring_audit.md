## Dashboard Causal Wiring Audit (NO_BE50, Frozen Tier-A)

**Date (UTC+3):** 2026-08-18  
**Repositories (read-only):**
- Gold Source of Truth: `/home/telgenbuescher/projects/wave_fade_gold_f16ae32`
- Dashboard: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev`

---

### Executive Finding

The dashboard endpoint `/stoch-signale` is **not** wired to the canonical causal Frozen-Tier-A NO_BE50 strategy:
`wave_fade_frozen_f16ae32_causal_entry_v1` (Confirmation policy `cross_recognition`).

Instead, the dashboard “Frozen” research/evaluation pipeline is hard-pinned to:
`wave_fade_frozen_f16ae32` (legacy confirmation semantics = `wave_end`).

What *is* aligned:
- `NO_BE50` exit-policy is used.
- Intrabar policy is `SL_FIRST`.
- Outcome evaluation uses the full 1m-scan engine (`evaluate_signal_no_be50_full_1m`).

What is *not* aligned:
- Confirmation policy / entry causality is not causal:
  `confirmation_available_at` is produced as `end_available_at` (wave_end), not `recognition_available_at` (cross_recognition).
- The chart/telemetry currently does not display wave-end vs recognition-bar B markers.

**Classification:** `DASHBOARD_CAUSAL_NO_BE50_LEGACY_WIRED`

---

## 1. Dashboard Call-Graph (UI → API → Job → Strategy → Signal Gen → Entry Mapping → Outcome → Storage → Dashboard → Chart)

### Frontend

1. **Route:** `/stoch-signale`
   - Backend serves template in `dashboard/app.py`:
     - `@app.get("/stoch-signale", response_class=HTMLResponse)` → `stoch_signale.html`
2. **Template:** `dashboard/templates/stoch_signale.html`
   - Includes: `/static/js/stoch_signale.js`
   - Contains “Frozen Stochastic Fade – Signale berechnen” and “Frozen-NO_BE50-Outcomes berechnen”
3. **Start-button wiring:** `dashboard/static/js/stoch_signale.js`
   - Frozen signals job:
     - `POST /api/stoch/frozen-fade-jobs` with body `{symbols, signal_start, signal_end_exclusive}`
   - Frozen outcomes/evaluation:
     - `POST /api/stoch/frozen-fade-evaluations` with body `{source_job_id: jobId}`
4. **Polling endpoints:**
   - `GET /api/stoch/frozen-fade-jobs/status`
   - `GET /api/stoch/frozen-fade-evaluations/status`
5. **Trade table / outcomes feed:**
   - Outcomes rows are ultimately obtained via:
     - `GET /api/stoch/frozen-fade-evaluations/{evaluation_id}/outcomes` (used by UI)
6. **Chart modal:**
   - `dashboard/static/js/stoch_signale.js` opens chart using:
     - `state.chart.open({ ... row fields ... })` (trade fields passed include entry/TP/SL/exit, but not wave-end/recognition bars)
   - `dashboard/static/js/stoch_chart_modal.js` draws:
     - entry line, TP/SL/exit lines and candlestick markers
     - uses `/api/stoch/klines?symbol=...&interval=5&limit=300` (5m klines) for candles

### Backend (API)

**API endpoints (read-only audit):**
- `GET /api/stoch/signals`
- `POST /api/stoch/frozen-fade-jobs`
- `GET /api/stoch/frozen-fade-jobs/status`
- `GET /api/stoch/frozen-fade-jobs/{job_id}/signals`
- `POST /api/stoch/frozen-fade-evaluations`
- `GET /api/stoch/frozen-fade-evaluations/status`
- `GET /api/stoch/frozen-fade-evaluations/{evaluation_id}/outcomes`
- (chart candles) `GET /api/stoch/klines`

Backend “/api/stoch/frozen-fade-jobs/*” and “/api/stoch/frozen-fade-evaluations/*” are delegated to:
- `stoch_fade_research_jobs/*`
- `stoch_fade_research_evaluations/*`

### Jobs / Strategy Registry / Signal Generator / Outcome Evaluator

1. **Signals Job Create → Worker spawn**
   - `dashboard/stoch_fade_research_jobs/config.py` pins:
     - `STRATEGY_VERSION = "wave_fade_frozen_f16ae32"`
   - `dashboard/stoch_fade_research_jobs/jobs.py` handles create/resume and starts worker.
   - `dashboard/stoch_fade_research_jobs/worker.py` runs:
     - `python -m research.stoch_fade_runner --clickhouse-readonly --symbol ... --start ... --end ... --out-root ...`

2. **Runner (signal generation / entry mapping)**
   - `dashboard/research/stoch_fade_runner/config.py` pins:
     - `STRATEGY_ID = "wave_fade_frozen_f16ae32"`
   - `dashboard/research/stoch_fade_runner/engine.py` builds:
     - signals via `signal_generator.pipeline.mapper` and `signal_generator.pipeline.trade_plan`
     - and resolves entries using `confirmation_available_at`

3. **Evaluation Job Create → Worker spawn**
   - `dashboard/stoch_fade_research_evaluations/config.py` pins:
     - `STRATEGY_VERSION = "wave_fade_frozen_f16ae32"`
     - `EXIT_POLICY = "NO_BE50"`
     - `INTRABAR_POLICY = "SL_FIRST"`
     - `OUTCOME_ENGINE = "evaluate_signal_no_be50"`
       (note: the actual runner used below pins full_1m_scan)
   - `dashboard/stoch_fade_research_evaluations/worker.py` runs:
     - `python -m research.stoch_fade_evaluation --clickhouse-readonly --symbol ... --signals-jsonl ...`

4. **Outcome Engine (full 1m scan)**
   - `dashboard/research/stoch_fade_evaluation/config.py` pins:
     - `OUTCOME_ENGINE = research.stoch_fade_evaluation.full_1m_scan.evaluate_signal_no_be50_full_1m`
     - `INTRABAR_POLICY = "SL_FIRST"`
     - `EXIT_POLICY = "NO_BE50"`

5. **Result Storage**
   - Signals:
     - `results/stoch_fade_research_jobs/<job_id>/coin_runs/<symbol>/<run_id>/signals.jsonl`
   - Outcomes:
     - `results/stoch_fade_research_evaluations/<evaluation_id>/coin_runs/<symbol>/outcomes.jsonl`

6. **Dashboard response / table / chart**
   - UI loads outcomes rows via API, filters client-side, and passes selected row to chart modal.

### Required UI → API → Job → Strategy → Signal Generator → Entry Mapping → Outcome Engine → Result Storage → Dashboard Response → Chart

The implemented wiring matches the above chain structurally, but the **strategy_id/confirmation semantics** diverge (wave_end vs cross_recognition).

---

## 2. What Strategy is actually used by the Start-Buttons?

### 2.1 Frozen Signals “berechnen” Start button

Frontend sends only universe/window:
- `POST /api/stoch/frozen-fade-jobs` body contains:
  - `symbols`
  - `signal_start`
  - `signal_end_exclusive`

Backend pins strategy server-side (browser cannot override):
- `dashboard/stoch_fade_research_jobs/config.py`:
  - `STRATEGY_VERSION = "wave_fade_frozen_f16ae32"`

Hard evidence from stored job request:
- `results/stoch_fade_research_jobs/8e86d1527a4749a79531d787cf67a032/request.json`:
  - includes `job_id`, `signal_start`, `signal_end_exclusive`
  - does **not** include strategy override fields (strategy is pinned elsewhere)

### 2.2 Frozen-NO_BE50-Outcomes “berechnen” Start button

Frontend sends:
- `POST /api/stoch/frozen-fade-evaluations`
  - body: `{ source_job_id: <jobId> }`

Hard evidence from stored evaluation request:
- `results/stoch_fade_research_evaluations/84da5ecf51c94a2c897ce4bfcab3d937/request.json` shows:
  - `fixed_strategy_version: "wave_fade_frozen_f16ae32"`
  - `exit_policy: "NO_BE50"`
  - `intrabar_policy: "SL_FIRST"`
  - `outcome_engine: "evaluate_signal_no_be50"`
  - `signal_strategy_version: "wave_fade_frozen_f16ae32"`

Therefore:
- **Strategy version actually used:** `wave_fade_frozen_f16ae32`
- **Confirmation policy:** derived from that strategy’s confirmation semantics → `wave_end` (see Section 4)

### 2.3 Which exact runner implementations are called?

Signals job worker spawns:
- `research.stoch_fade_runner` (dashboard side)

Evaluation job worker spawns:
- `research.stoch_fade_evaluation` (dashboard side)

---

## 3. Legacy Wiring Search (required keywords) + Classification

Search results in the dashboard UI/backend show active wiring to:
- `wave_fade_frozen_f16ae32`
- `NO_BE50`
- `SL_FIRST`

Non-presence of causal entry wiring:
- There is **no** UI option nor backend pin for:
  - `wave_fade_frozen_f16ae32_causal_entry_v1`
  - confirmation policy `cross_recognition`

Classification:
- `wave_fade_frozen_f16ae32`:
  - **active in job/eval create path**
- `cross_recognition`:
  - **unused in dashboard wiring**
- `full_1m_scan`:
  - **used in actual outcome runner** (`research/stoch_fade_evaluation/config.py`)
- `uses_be50_exit` / `BE50`:
  - dashboard request/evaluation paths show `BE50` deactivated (see Section 6)

---

## 4. Entry Causality in the Dashboard (10 signals audit)

### 4.1 What the dashboard data contains

Frozen job signals JSONL (example from):
`results/stoch_fade_research_jobs/8e86d1527a4749a79531d787cf67a032/coin_runs/AAVEUSDT/e6b88c5bca914cfb9e18cadb6f833794/signals.jsonl`

First signals show:
- `strategy_version: "wave_fade_frozen_f16ae32"`
- `confirmation_available_at` equals the candle close time
- `entry_time` is exactly the next 1m open timestamp (consistent with “T0 first 1m open strictly after confirmation”)

Example rows (first audit window):
1. `confirmation_available_at: "2026-08-01T00:45:00Z"`
   - `entry_time: "2026-08-01T00:46:00Z"`
2. `confirmation_available_at: "2026-08-01T01:30:00Z"`
   - `entry_time: "2026-08-01T01:31:00Z"`
3. `confirmation_available_at: "2026-08-01T02:15:00Z"`
   - `entry_time: "2026-08-01T02:16:00Z"`
... (first 10 lines audited; pattern is consistent)

### 4.2 Why this is not causal cross_recognition entry

Gold Source of Truth semantics (signal generator):

`src/signal_generator/strategy/wave_fade/signals.py`:
- `build_symbol_signals(... confirmation_source=CONFIRMATION_WAVE_END)`
- doc states: default confirmation_source=wave_end preserves frozen entry.

`src/signal_generator/strategy/wave_fade/annotation.py`:
- `apply_confirmation_policy`:
  - `wave_end` sets `confirmation_available_at = end_available_at`
  - `cross_recognition` sets `confirmation_available_at = recognition_available_at`

Since the dashboard strategy_id is pinned to:
`wave_fade_frozen_f16ae32` (not `..._causal_entry_v1`),
the dashboard confirmations follow `wave_end`, not `cross_recognition`.

Additionally:
- The dashboard signals JSONL in the audited job does not contain `recognition_ts` / `recognition_available_at` fields, consistent with wave_end confirmation policy.

### 4.3 Hard requirements check

Entry strictly after confirmation:
- PASS (entry_time is after confirmation_available_at and matches “next 1m open” semantics)

No wave_end entry:
- FAIL

Entry must be based on cross_recognition confirmation (Close of Bar B):
- FAIL

No BE50-exit:
- PASS (`exit_policy: NO_BE50` everywhere in evaluation request and outcomes)

Full 1m scan:
- PASS (runner pins `evaluate_signal_no_be50_full_1m`, see Section 6)

---

## 5. Dashboard Data Source (new backtest vs import vs ClickHouse vs mixed)

For the Frozen research UI:
- `DASHBOARD_SIGNAL_SOURCE`:
  - default is `FROZEN_BASELINE` (display-only)
  - RESEARCH_1M_TIMING is only active with explicit env override
- Backend enforces non-mixing:
  - `dashboard/stoch_signal_source.py::assert_sources_do_not_mix(...)`

For the Frozen Start-button computation:
- It **starts** a research job via:
  - `/api/stoch/frozen-fade-jobs`
  - `/api/stoch/frozen-fade-evaluations`
- Those jobs are based on pinned source commits and read-only ClickHouse inside the runner.

Conclusion:
- No mixing of legacy vs causal datasets was found; causal dataset simply is not used.

---

## 6. Expected UI Fields for manual visibility

The dashboard trade table (`stoch_signale.html`) contains columns such as:
- Symbol, Timeframe, Direction, Entry, TP, SL, Stoch K, Signal Time, Result, PnL, Duration

However, the table does not include required causal fields:
- `confirmation_available_at`
- `recognition_ts`
- `setup_id`
- `confirmation_policy`
- `strategy_version` as a visible column

Filters exist for:
- Outcome (WIN/LOSS/OPEN)
- Symbol
- Direction
- Timeframe

But the causal-only filters required for manual causal audits are missing from table schema.

---

## 7. Chart Audit

The chart modal (`dashboard/static/js/stoch_chart_modal.js`) draws:
- Candlesticks (from `/api/stoch/klines` at `interval=5`)
- Entry line + entry candle marker
- TP/SL/exit lines

It does **not** draw:
- wave-end line A
- recognition bar B
- confirmation_available_at marker

Therefore the chart cannot satisfy the causal visual requirements as-is.

---

## 8. Reconciliation vs canonical causal artifacts

The canonical headline population is:
- `wave_fade_frozen_f16ae32_causal_entry_v1`
- Frozen Dedup Trades: 35,162
- WIN: 16,368, LOSS: 18,794, WR: 46.55%

The dashboard evaluation artifacts inspected here were generated with:
- `strategy_version: wave_fade_frozen_f16ae32` (legacy)

Example mismatch on existing dashboard artifacts:
- outcomes/evaluation for BTC/ETH in the dashboard result directories do not match the canonical BTC/ETH counts provided for the causal headline.

Conclusion:
- Without running the canonical causal strategy, the dashboard’s current artifacts cannot be reconciled to the canonical causal headline.

---

## 9. Safety of the Start Button

Start triggers only offline research computations:
- No endpoints for live collectors/orders are called by the Frozen Start buttons.
- The research configs explicitly set side effects off:
  - `writes_to_clickhouse: False`
  - `writes_to_signals: False`
  - `publish_enabled: False`
  - `live_orders_enabled: False`

Concrete evidence from evaluation artifacts:
- `request.json` and runner config show `exit_policy: NO_BE50`, no BE activation.

---

## 10. Final decision logic for the 15 requested questions

1. Which strategy runs on Start?
   - `wave_fade_frozen_f16ae32` (legacy, not `..._causal_entry_v1`)
2. Is `cross_recognition` active?
   - No. `confirmation_available_at` is produced as `end_available_at` (`wave_end` semantics).
3. Is entry strictly after confirmation?
   - Yes: signals show `entry_time` = first 1m open after `confirmation_available_at`.
4. Is NO_BE50 active?
   - Yes: `exit_policy: NO_BE50` and outcomes `exit_policy: NO_BE50`.
5. Is `full_1m_scan` used?
   - Yes: `research/stoch_fade_evaluation/config.py` pins `evaluate_signal_no_be50_full_1m`.
6. Is Max-Hold deactivated?
   - Yes: `max_hold_applied: False` is written by the evaluation CLI.
7. Is SL_FIRST active?
   - Yes: `intrabar_policy: SL_FIRST` and outcomes close reason shows SL-first behavior.
8. Do overall/BTC/ETH numbers match?
   - No reconciliation possible to canonical headline because strategy_id differs; inspected existing artifacts do not match canonical BTC/ETH counts.
9. Can losses be filtered cleanly?
   - Yes for LOSS rows in the current table/outcomes filtering (client-side WIN/LOSS/OPEN filter).
10. Does chart open the same trade?
   - Yes: chart modal receives `signal_id` and entry/exit prices from the selected filtered row.
11. Are legacy and causal data mixed?
   - No mixing found; causal is not used at all.
12. Can Start trigger live actions?
   - No: Start buttons trigger only research job APIs; live collector/orders are separate UI controls.
13. Which files/functions must be changed?
   - Strategy pinning:
     - `dashboard/stoch_fade_research_jobs/config.py` and `dashboard/stoch_fade_research_evaluations/config.py`
     - `dashboard/research/stoch_fade_runner/config.py` (`STRATEGY_ID`)
   - Ensure causal confirmation wiring:
     - make runner use `wave_fade_frozen_f16ae32_causal_entry_v1` (which sets confirmation_source=cross_recognition)
   - UI/Chart enhancements:
     - expose `recognition_ts`, `confirmation_available_at`, `confirmation_policy`, `setup_id` in table and chart modal payload
14. Minimal implementation plan afterwards:
   1. Replace legacy strategy pins with `wave_fade_frozen_f16ae32_causal_entry_v1`.
   2. Verify generated signals contain `recognition_available_at` and `confirmation_source=cross_recognition`.
   3. Ensure outcomes/evaluation keep `NO_BE50`, `SL_FIRST`, and full_1m scan.
   4. Extend UI table + chart modal to display causal markers (wave_end/recognition/confirmation_available_at).
   5. Re-run (read-only) smoke tests and reconcile to the canonical causal headline.
15. Tests required before starting a production run:
   - Call-graph/static guard:
     - ensure causal strategy id is the only frozen strategy id.
   - Data-shape tests:
     - confirm `confirmation_source=cross_recognition` and presence of `recognition_ts` fields.
   - Deterministic sampling:
     - 10+ deterministic signals: assert `entry_time` is the first 1m open after cross_recognition confirmation.
   - Outcome parity:
     - validate losses filter and chart modal trade-id parity against outcomes feed.

---

## Output Label (must be exactly one)

`DASHBOARD_CAUSAL_NO_BE50_LEGACY_WIRED`

