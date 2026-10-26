# PHASE BT-D22 — Pool-First Complete Order / Trade Lifecycle Audit

**Project:** `pools+ob+delta_bot`  
**Upstream:** BT-D21 `POOL_FIRST_SHORT_READY`  
**Mode:** Read-only (forensic replay of BT-D21 pilot; no strategy/YAML/code changes)  
**Pilot config:** `configs/pilot_bt_d21_pool_first_24h.yaml`  
**Artifacts:** `results/phase_bt_d22_pool_first_trade_audit_v1/`  
**Provenance:** `config_provenance.json` (hash `4822db251577…`)

---

## Executive summary

| Metric | Pool-first pilot (24h) |
|--------|-------------------------|
| Signals | 7 |
| Order fills (limit) | 3 |
| Closed trades (ledger) | 1 |
| Net PnL | **−3.219172216936251 USDT** (matches BT-D21 comparison ≈ −3.22) |

**VERDICT: `POOL_FIRST_LIFECYCLE_VERIFIED`**

Ledger PnL reconciles exactly. The 3 fills vs 1 trade split is explained by `intrabar_ambiguity_policy: no_trade_ambiguous` on two fills. SL/TP simulation includes the **fill bar** (`execution_v1.simulate_short_exit`); no `EXECUTION_PROTECTION_DEFECT` (next-bar-only deferral). Reference setups 15:35 and 17:00 were **not** pre-handled under pool-first due to **single pending slot** held by signal #7 from 14:05.

**Caveat (documented semantics, not silent fix):** After `AMBIGUOUS_INTRABAR`, the engine does **not** set `position_open_until`, so `max_concurrent_positions: 1` does not block subsequent limit fills even though those fills also fail to become trades. Only the third fill produced a `ClosedTradeV1` and PnL.

---

## 1. Source results

Replay used the same normalized config as BT-D21 (`run_short_research_backtest` + validated density cache). Full structured ledger: `trade_lifecycle.json`.

BT-D21 comparison reference: `results/phase_bt_d21_pool_first_short_limit_strategy_v1/historical_comparison.json` (pool-first `net_pnl_usdt`: −3.219172216936251).

---

## 2. Signal table (all 7)

| # | UTC close | Close px | Pool lo / hi | Origin IDs (abbrev) | Gap % @ signal | Entry mode / limit | SL | TP / band lo–hi | Order active from | Status | Reason |
|---|-----------|----------|--------------|---------------------|----------------|-------------------|-----|-----------------|-------------------|--------|--------|
| 1 | 12:00 | 0.08356 | 0.08365 / 0.08366 | …27100\|…27400\|…27700 | 1.23 | lower_edge / 0.08365 | 0.08382732 | 0.08293 (0.08288–0.08293) | 12:05 | CANCELLED | `CANCELLED_POOL_INACTIVE` (`origin_set_v1`) at 12:05 |
| 2 | 12:05 | 0.08338 | 0.08384 / 0.083844 | …20800\|…28000 | 1.23 | lower_edge / 0.08384 | 0.084011688 | 0.08293 | 12:10 | **FILLED** | Limit touch 12:25 (`causal_sell_limit_v2`) |
| 3 | 12:25 | 0.08351 | 0.08394 / 0.083976 | …18400\|…20800 | 1.23 | lower_edge / 0.08394 | 0.084143952 | 0.08293 | 12:30 | **FILLED** | Limit touch 12:30 |
| 4 | 12:30 | 0.08403 | 0.08408 / 0.08409 | 4 origins | 1.23 | lower_edge / 0.08408 | 0.08425818 | 0.08293 | 12:35 | **FILLED** | Limit touch 12:35 |
| 5 | 12:35 | 0.08488 | 0.08542 / 0.08545 | …27200\|…37700\|…43400 | 0.76 | lower_edge / 0.08542 | 0.0856209 | 0.08223 (0.08216–0.08223) | 12:40 | CANCELLED | Pool inactive at 13:50 |
| 6 | 13:50 | 0.08655 | 0.08699 / 0.08703 | …84900\|…87300\|…87600 | 1.61 | lower_edge / 0.08699 | 0.08720406 | 0.08422 | 13:55 | CANCELLED | Pool inactive at 14:00 |
| 7 | 14:00 | 0.08767 | 0.08856 / 0.08859 | …81300\|…81600 | 0.51 | lower_edge / 0.08856 | 0.08876718 | 0.08422 | 14:05 | PENDING_AT_END | No fill before `2026-09-12T12:00:00Z` |

CSV: `all_signals.csv`. Pool-first does **not** wait for bearish candle or rejection; signals fire on structural pass + lowest eligible upper pool.

**Why 7 signals?** Seven distinct 5m closes where (a) no position lock (`position_open_until`), (b) no other pending order, (c) pool not in `pool_signal_hold` block set, and (d) `detect_short_signal_pool_first` returns a candidate. Burst 12:00–12:35 (5) then 13:50, 14:00 after prior pending cleared by fill/cancel.

---

## 3. Order / position lifecycle (3 fills)

### Fill 1 — signal 12:05, fill 12:25 @ 0.08384

- **OHLC evidence:** o 0.08317, h 0.08399, l 0.08314, limit 0.08384  
- **Rule:** `CAUSAL_V2_LIMIT_TOUCH_INTRABAR`  
- **SL/TP (recomputed at fill):** SL 0.084011688, TP 0.08293  
- **Exit sim:** Fill bar — no SL/TP touch. **Next bar 12:30** — both SL and TP touched → `AMBIGUOUS_INTRABAR` → order event only, **no** `ClosedTradeV1`, **no** PnL/fees  
- **Position:** `FILL_NO_CLOSED_TRADE` (see `all_positions.csv`)

### Fill 2 — signal 12:25, fill 12:30 @ 0.08394

- **OHLC:** o 0.08351, h 0.08436, l 0.08213  
- **Same bar:** SL and TP both hit → `AMBIGUOUS_INTRABAR` immediately  
- **No trade, no PnL**

### Fill 3 — signal 12:30, fill 12:35 @ 0.08408

- **OHLC:** o 0.08403, h 0.0855, l 0.08402  
- **Same bar:** SL 0.08425818 hit (TP not) → `SL`, `BT7_0003`  
- **Notional:** 1000 USDT; gross −2.11917 USDT; fees 1.1 USDT; **net −3.21917 USDT**  
- **Bars held:** 1 (includes entry bar)

### 3 fills, 1 trade — explanation

| Fill | Ledger trade? | Mechanism |
|------|---------------|-----------|
| 12:25 | No | `simulate_short_exit` → `AMBIGUOUS_INTRABAR` on 12:30 bar; `no_trade_ambiguous` |
| 12:30 | No | Ambiguous on fill bar 12:30 |
| 12:35 | Yes | Unambiguous SL on fill bar |

Not open positions at test end, not aggregation error. Two fills are **execution events without economic trade records** per config policy.

Engine reference (`short_research_engine_v1._process_entry_fill`): appends `ClosedTradeV1` only when `ex_p is not None and not amb`.

---

## 4. Immediate SL / TP activation

**Code path:** After each fill, `simulate_short_exit(..., entry_utc=fill_t, ...)` iterates bars with `t >= entry_utc` — **includes the fill bar**.

| Fill UTC | SL/TP checked same bar? | Outcome |
|----------|-------------------------|---------|
| 12:25 | Yes (no hit) | Protection logic runs; ambiguous on **following** bar |
| 12:30 | Yes | Ambiguous same bar |
| 12:35 | Yes | SL same bar |

**Not** deferred to “next bar only.” **`EXECUTION_PROTECTION_DEFECT`:** not observed for next-bar-only SL/TP.

Intrabar ordering when both hit: `no_trade_ambiguous` → no conservative SL-first trade (documented policy).

---

## 5. Reference cases 15:35 & 17:00

See `reference_cases_1535_1700.md`.

- No prior pool-first signals for those origin sets.  
- No limit on those pools at the baseline touch times.  
- Signal **#7** pending (0.08856) blocked **all** new pool-first signals from 14:05 onward.  
- Bands at 15:35 / 17:00 were structurally eligible if a signal had been allowed.

---

## 6. `origin_set_v1` pool cancel & `pool_signal_hold`

**Cancel (`pool_identity_v1`):** Pending order cancelled when **no** upper band in snapshot has `wanted_origins ⊆ band.origin_ids`. Logged as `CANCELLED_POOL_INACTIVE` with `pool_cancel_identity_mode: origin_set_v1`.

Observed:

- Signal 1: origins lost from union at 12:05 → cancel.  
- Signals 5 & 6: full origin set no longer covered → cancel at 13:50 / 14:00.  
- Signal 7: origin set still present in snapshot → pending until test end.

**`pool_signal_hold`:** On signal emit, `pool_signal_hold[pool_key] = {pool_lower, pool_upper, origin_ids}`. Released when `pool_still_active_for_pending` is false for that held metadata. Prevents re-signaling the same pool until origin-set inactive; does **not** release on fill alone (pending cleared separately).

---

## 7. PnL reconciliation

`pnl_reconciliation.json`:

- Reported net: **−3.219172216936251 USDT**  
- Sum of `trade_ledger`: **−3.219172216936251 USDT**  
- `matches: true`  
- Only **one** round-trip fee block (1.1 USDT) — ambiguous fills excluded from synthetic trades.

---

## 8. Closing answers

| Question | Answer |
|----------|--------|
| Why 7 signals? | Seven eligible pool-first closes with pending/position gates free; cluster morning + two afternoon after cancels. |
| Why 3 fills? | Three causal sell-limit touches (12:25, 12:30, 12:35); four orders cancelled (pool inactive) or still pending (#7). |
| Why 1 closed trade? | Two fills → `AMBIGUOUS_INTRABAR` under `no_trade_ambiguous`; only fill 3 → closed SL trade. |
| Other orders/positions? | No open positions at end; two “fills” without trades; one live pending (#7). |
| SL/TP immediate? | Yes — evaluated from fill bar onward in `simulate_short_exit`. |
| 15:35 / 17:00 early? | **No** — blocked by long-lived pending on unrelated pool (#7). |
| Execution / PnL defects? | No proven PnL mismatch; no next-bar-only SL/TP defect. Document ambiguous-fill vs position-lock interaction. |

---

## VERDICT

**`POOL_FIRST_LIFECYCLE_VERIFIED`**

---

*End of BT-D22 audit — no code, config, or backtest parameter changes.*
