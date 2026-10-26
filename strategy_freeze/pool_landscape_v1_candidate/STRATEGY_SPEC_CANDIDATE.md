# STRATEGY_SPEC_CANDIDATE — Pool Landscape PD1 Depth-2

| Field | Value |
|-------|--------|
| strategy_candidate_id | `pool_landscape_v1_candidate` |
| approval | **NOT** `FINAL_APPROVED` |
| separates_from | Original LMV2 (`historical_lmv2/`, `frozen_strategies/*`) |

---

## Rules

### DATA-01
- **Description:** Trade and research on DOGEUSDT 5m candles.
- **Side:** BOTH
- **Status:** USER_CONFIRMED
- **Source:** `Signal_definierung.md` L5–8
- **Parameters:** symbol, timeframe
- **Causal data:** 5m OHLC series aligned to SSOT
- **Open questions:** —

### DATA-02
- **Description:** Entry landscape from PD1; do not sum PD1/PD5/PD10 bands.
- **Side:** BOTH
- **Status:** USER_CONFIRMED
- **Source:** `Signal_definierung.md` L7–8
- **Parameters:** pool_pd = PD1 for entry
- **Causal data:** PD1 pool snapshots per bar
- **Open questions:** O-13 PD10 for TP-only landscape

### DATA-03
- **Description:** Exact unique-origin density; minimum depth 2 for strategy signals.
- **Side:** BOTH
- **Status:** USER_CONFIRMED
- **Source:** `Signal_definierung.md` L8
- **Parameters:** min_depth = 2
- **Causal data:** ACTIVE origins, union intervals from adapter
- **Open questions:** —

### DATA-04
- **Description:** ≥168h history; include older ACTIVE origins in landscape build.
- **Side:** BOTH
- **Status:** USER_CONFIRMED
- **Source:** `Signal_definierung.md` L5–6, §383–387
- **Parameters:** lookback_hours = 168
- **Causal data:** SSOT origin lifecycle
- **Open questions:** —

### SHORT-ENTRY-01
- **Description:** Short when price interacts with a **relevant** upper depth-2 pool and signal candle shows bearish reaction; entry at next 5m open when that rule applies.
- **Side:** SHORT
- **Status:** USER_CONFIRMED (qualitative) + **OPEN** details
- **Source:** User task; S3; C003
- **Parameters:** entry_proximity_pct **EXPERIMENTAL** (0.05 in 2A only)
- **Causal data:** upper intervals, completed candle OHLC
- **Open questions:** Definition of “relevant” upper; rejection shape; O-01, O-05, O-06

### SHORT-RISK-01
- **Description:** Stop loss 0.2% above upper edge of entry upper pool.
- **Side:** SHORT
- **Status:** USER_CONFIRMED
- **Source:** `Signal_definierung.md` L7
- **Parameters:** sl_above_upper_pool_pct = 0.2
- **Causal data:** entry pool upper at decision time
- **Open questions:** Which pool if multiple touches — O-01

### SHORT-EXIT-01
- **Description:** Take profit at **large/dense lower-density cluster** top (user narrative); minimum 0.5% from entry price to cluster **top**.
- **Side:** SHORT
- **Status:** USER_CONFIRMED intent; **OPEN** algorithm
- **Source:** `Signal_definierung.md` L7
- **Parameters:** min_tp_distance_pct = 0.5
- **Causal data:** lower intervals at entry
- **Open questions:** O-01 dense cluster rank

### LONG-ENTRY-01
- **Description:** Long at relevant lower pool (mirror of short entry logic).
- **Side:** LONG
- **Status:** UNRESOLVED
- **Source:** User task
- **Parameters:** —
- **Causal data:** lower intervals
- **Open questions:** O-03 trigger candle; MS-02 confirmation

### LONG-RISK-01
- **Description:** SL 0.2% below lower edge of entry lower pool.
- **Side:** LONG
- **Status:** USER_CONFIRMED
- **Source:** User task
- **Parameters:** sl_below_lower_pool_pct = 0.2
- **Causal data:** entry lower pool bounds
- **Open questions:** Which lower pool on two-pool path

### LONG-EXIT-01
- **Description:** TP at large/dense upper cluster; min 0.5% from entry to cluster **bottom**.
- **Side:** LONG
- **Status:** USER_CONFIRMED intent; **OPEN** algorithm
- **Source:** User task; Two-lower-pool §522
- **Parameters:** min_tp_distance_pct = 0.5
- **Causal data:** upper intervals at decision
- **Open questions:** O-02

### MS-WATCH-01
- **Description:** After first lower reach + large gap below: WATCH_REVERSAL — observe; provisionally no new shorts; no long yet.
- **Side:** BOTH
- **Status:** USER_MANUAL_REFERENCE — **EXPERIMENTAL** automation
- **Source:** `Signal_definierung.md` §484–523
- **Parameters:** gap thresholds **OPEN** (0.5% config vs ~1.34% chart measure)
- **Causal data:** lower touch episodes, gaps, origin lifecycle
- **Open questions:** O-08, O-09

### MS-LONG-01
- **Description:** Second **new** lower pool after recovery + gap → LONG_CANDIDATE; evaluate upper dense TP.
- **Side:** LONG
- **Status:** USER_MANUAL_REFERENCE — **EXPERIMENTAL** automation
- **Source:** Same
- **Parameters:** POOL_NEW evidence rules
- **Causal data:** 3A timeline, SSOT origins
- **Open questions:** O-09 exact timing

### POS-01
- **Description:** Maximum one open position; block new entries until prior position closes (SL/TP).
- **Side:** BOTH
- **Status:** USER_CONFIRMED
- **Source:** User task; C011
- **Parameters:** max_open_positions = 1
- **Causal data:** position state machine
- **Open questions:** —

### POS-02
- **Description:** Store all raw signals even when suppressed.
- **Side:** BOTH
- **Status:** USER_CONFIRMED
- **Source:** C011 §475–476
- **Causal data:** signal log
- **Open questions:** —

### OPP-01
- **Description:** Multiple raw signals may belong to one upper opportunity; do not open duplicate trades on same opportunity while in trade.
- **Side:** SHORT
- **Status:** USER_MANUAL_REFERENCE
- **Source:** C010/C011/C013
- **Parameters:** identity = origins/zone continuity (**OPEN** formal spec)
- **Causal data:** Phase 2G-style overlap (research)
- **Open questions:** O-10 re-entry after exit

### COST-01
- **Description:** Deduct 0.11% round-trip fees in simulation unless user revises.
- **Side:** BOTH
- **Status:** USER_CONFIRMED
- **Source:** `Signal_definierung.md` L7
- **Parameters:** roundtrip_fees_pct = 0.11
- **Causal data:** —
- **Open questions:** O-12 slippage/funding

### FILTER-AGE-01
- **Description:** min_pool_age_bars = 5 as research parameter for upper pool persistence.
- **Side:** SHORT
- **Status:** RESEARCH_HYPOTHESIS — **not** hard confirmed gate
- **Source:** User task §3; Signal_definierung §D
- **Parameters:** min_pool_age_bars = 5
- **Causal data:** continuous depth-2 zone age, origin age
- **Open questions:** O-04 vs C003

### FILTER-GAP-01
- **Description:** min_pool_gap_pct = 0.5 for **large** pool-to-pool gaps in landscape.
- **Side:** SHORT (primarily upper context)
- **Status:** RESEARCH_HYPOTHESIS
- **Source:** User task; Signal_definierung §D
- **Parameters:** min_pool_gap_pct = 0.5
- **Causal data:** gap table from intervals
- **Open questions:** O-05 vs N3/S5

### EXEC-INTRABAR-01
- **Description:** If one bar touches both SL and TP, do not assume optimistic fill.
- **Side:** BOTH
- **Status:** RESEARCH_HYPOTHESIS (4A behavior documented)
- **Source:** Phase 4B spec; not user-signed
- **Parameters:** —
- **Causal data:** bar H/L
- **Open questions:** O-11

---

## Implementation defaults (must not become freeze without user approval)

| rule_id | Default | Location |
|---------|---------|----------|
| IMPL-PROX-01 | proximity_pct 0.05 | `timeline_scanner.js` |
| IMPL-REJ-01 | rejection_v1 | `timeline_scanner.js` |
| IMPL-CD-01 | cooldown_candles 6 | `timeline_scanner.js` |
| IMPL-TP-SHORT-01 | nearest lower top ≥0.5% | `backtest_pilot.py` |
| IMPL-TP-LONG-01 | 3B rank-1 upper bottom | `backtest_pilot.py` |
| IMPL-MS-01 | WATCH block list from 3B | `backtest_pilot.py` scenario B |
