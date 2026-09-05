# BTCUSDT Profile-Edge OB Fight — 2026-08-31 19:00 UTC

## 1. Final Verdict

`BTC_OB_FIGHT_CONTESTED_WAIT_NO_TRADE`

## 2. Symbol and UTC Window

- Symbol: `BTCUSDT` only
- T0: `2026-08-31T19:00:00Z`
- Core: `2026-08-31T18:30:00Z` → `2026-08-31T19:30:00Z`
- Extension observed to: `2026-08-31T21:00:00Z`

## 3. Earliest Causal Decision Timestamp

No LONG/SHORT decision was causally justified.

```text
19:00 bis 19:30: WAIT
19:30 bis 21:00: still CONTESTED for a profile-edge trade
Entscheidung: CONTESTED_WAIT_NO_TRADE at end of allowed window (21:00:00 UTC)
```

Observable sub-events (not trade signals):

- first trade above US VAH 79140: `2026-08-31 19:08:13.577000`
- first reclaim below VAH after that poke: `2026-08-31 19:10:58.515000`
- peak print: `2026-08-31 19:10:42.600000` at `79280.8`

## 4. ACTIVE_AGGRESSOR

`MIXED`

- 19:00–19:10: **BUYERS** (delta ≈ +$2.76M, +25.9 bps)
- 19:10–19:30: **SELLERS** (combined sell-dominant, −20.8 bps in 10–20m)

## 5. PASSIVE_CONTROLLER

`NONE_CLEAR`

Ask walls near the push were mostly `PULLED_OR_CANCELLED` / `REFILLED_OR_ADDED`, with `ask_consumed_with_trades=0` in the 19:00–19:09 sample set. That is **not** clean passive seller absorption of aggressive buys.

## 6. NET_CONTROLLER

`CONTESTED`

Buyers briefly controlled the ascent through VAH; sellers controlled the failed hold and drift back. No single side meets the joint control definition for a ready trade at a fixed edge from T0.

## 7. Resolution

`CONTESTED` (failed breakout / quick reclaim — **not** `REJECTION_CONFIRMED` under absorption rules, **not** `BREAKOUT_ACCEPTED`)

## 8. Trade Readiness

`NO_TRADE`

## 9. Relevant Market / Volume Profile Level

Engine: existing `orderbook_analyse.market_profile` **volume-at-price** (VA 70%, ~160 bins). **Not classic TPO letters.** 30-minute VP blocks used only as context.

Causal at T0 (no peek past 19:00):

| Profile | POC | VAH | VAL | Shape |
|---|---:|---:|---:|---|
| US developing 13:30→T0 | 78565.0 | 79140.0 | 78190.0 | DOUBLE_DISTRIBUTION |
| Day developing 00:00→T0 | 77970.0 | 78520.0 | 77520.0 | TREND_UP |
| Prior day 2026-08-30 | 78830.0 | 79200.0 | 78120.0 | UNCLEAR |
| VP block 18:30–19:00 | 79166.25 | 79195.0 | 79005.0 | DOUBLE_DISTRIBUTION |

**Price at T0: 78984.4**

Actual location at T0:

- **Inside US value area**, nearest node **LVN ≈ 78985** (−0.08 bps)
- **~19.7 bps below US VAH 79140** (approaching, not testing the edge yet)
- Day-developing VAH 78520 already overhead (price already above day VA)
- Prior-day HVN 78990 / prior VAH 79200 are the overhead confluence for the later poke

Exact VAH touch/cross occurred **during** the core window (~`2026-08-31 19:08:13.577000`), not at T0 itself. Case was **not** relocated to a prettier timestamp.

## 10. Orderbook Wall Behavior

- Source: shadow `ob200_v3` hour archives; chunked zstd replay (dashboard `readline` path cannot read these files)
- Probes: genuine **200/200** levels at 18:29:59, 19:00, 19:30; `u_gaps=0` on probes
- Near the push: ask walls **pulled** and **refilled**; **no** clear trade-backed consumption cluster (`ask_consumed_with_trades=0` in 19:00–19:09 samples)
- Size drops without matching tape ⇒ `PULLED_OR_CANCELLED` (not “eaten”)

## 11. Public Trades and Price Impact

Aggressor = `public_trades_canonical.side` (Bybit taker). Dedup OK (count = uniq trade_id).

| Window | Buy $ | Sell $ | Delta $ | Δbps | Note |
|---|---:|---:|---:|---:|---|
| T0–+1m | 1139530 | 1116532 | 22998 | 5.80 | balanced→up |
| +1–+3m | 2068708 | 918663 | 1150044 | 1.47 | buy agg, stalling |
| +3–+10m | 8480695 | 6897495 | 1583199 | 18.62 | buy control through VAH |
| +10–+20m | 19560222 | 20919453 | -1359231 | -20.81 | sell control, reclaim |
| +20–+30m | 8352604 | 10622892 | -2270288 | 4.05 | still below VAH |

Prices: T0=78984.4 · 19:10=79188.9 · 19:30=79056.1 · 20:00=78863.6 · 21:00=78824.6

## 12. OI / Liquidations Context

- OI 5s: ΔOI T0→19:10 ≈ **+1.6** contracts (tiny); T0→19:30 ≈ **+122.8**
- Core liquidations: **0** LIQUIDATED_LONG notional, **~$126k** LIQUIDATED_SHORT (shorts squeezed on the push — consistent with upward progress, not seller absorption)
- OI/liq are confirmatory only

## 13. Target Room 0.5% / 0.8%

No trade. Illustrative only if one forced a short after reclaim near VAH 79140:

- to prior HVN/POC area 78830 ≈ **0.39%** (< 0.5%)
- to prior VAL 78120 ≈ **1.29%** (≥ 0.5% and ≥ 0.8%)

Without a confirmed SHORT_READY signal, room does not create a trade.

## 14. Counter-Arguments / Why Not LONG or SHORT

- **Not LONG_READY:** breakout above VAH was **not accepted** (quick reclaim; no hold/build above).
- **Not SHORT_READY / not REJECTION_CONFIRMED under §8:** during the buy attack, price **advanced efficiently**; walls were not shown to be absorbed-and-defended with trade-backed consumption; seller control arrived as a **later** wave after a successful poke.
- **Not DATA_INSUFFICIENT:** OB200 + tape + candles + OI reconstructable for BTCUSDT in-window.
- Do **not** force a side after the fact from the 19:30–21:00 drift.

## 15. Separate Outcome Validation

```text
outcome_used_for_decision = false
outcome_used_for_thresholds = false
outcome_used_for_profile_definition = false
```

Illustrative path after reclaim (not used for verdict): by 20:00 price ≈ 78863.6, by 21:00 ≈ 78824.6 (lower). See `outcome_validation.json`.

## 16. Data Coverage and Uncertainties

- Trades 18:30–21:00: 171142 rows, dedup_ok=True
- Candles 1m: 150/150 complete
- OI 5s: 1800
- OB200 hours present and reconstructable with chunked reader; dashboard readline path broken (documented, not modified)
- Profile is volume-at-price, not true TPO period counts
- Wall sampling is discrete (10–30s); LLD pool geometry not used as SoT for room (prior-day HVN/VAL proxy only)

## 17. Generated Files

`results/btc_profile_edge_ob_fight_20260831_1900_v1/`

- REPORT.md
- analysis_manifest.json
- coverage_audit.json
- decision_timeline.csv
- orderbook_wall_events.csv
- orderbook_samples.json
- public_trade_windows.csv
- profile_levels.json
- outcome_validation.json

Research-only code (no dashboard edits in this run beyond pre-existing dirty files):

- `research/btc_profile_edge_ob_fight_20260831_1900_v1/run_case.py`
- `research/btc_profile_edge_ob_fight_20260831_1900_v1/ob_replay.py`

## 18. Commands Executed

```bash
python3 -B research/btc_profile_edge_ob_fight_20260831_1900_v1/run_case.py
# plus read-only ClickHouse SELECTs and OB200 zst chunked replay
```

## 19. Safety Confirmation

- ClickHouse: read-only SELECTs only
- No collectors started/stopped/restarted
- No live processes changed
- No dashboard processes restarted
- No orders / exchange actions
- No tables altered
- New results folder only (no overwrite of prior case folders)
- No Market/Volume Profile dashboard files modified for this analysis
- Dirty worktree otherwise preserved; **no commit; no push**
