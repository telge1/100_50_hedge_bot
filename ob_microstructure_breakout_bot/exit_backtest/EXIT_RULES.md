# Exit Rules V2

This file tracks the planned exit logic for the EMA Touch V2 work.

## Goal

Define a no-lookahead exit plan that uses only:

- order book context
- public trade flow
- EMA structure

The goal is to keep winners alive while locking in risk early enough to avoid giving back the whole move.

## Core Principle

Never use information from candles or flow that are not fully available yet.

Exit decisions must be based on the latest closed bar plus the already known OB / public-trade state at that moment.

## Initial Stop Loss

The protective stop belongs below the most recent validated lower low, not on a fixed time.

For the current DOGE example, the first hard stop is around `0.08293`.
That stop should stay in place until the trade either:

- confirms a strong continuation and the stop is moved up, or
- invalidates the setup and gets closed by price

### Stop-setting rule

1. Find the last structural lower-low area before the entry.
2. Use the **actual candle low** of that lower-low leg as the anchor.
3. Place the initial SL 0.15% below that low as a buffer.
4. Do not move the SL based on an unconfirmed intrabar dip.
5. Only move the SL up after a new confirmed higher low or after a partial exit that reduces risk.

The confirmed pivot low is still useful as structure context, but the SL anchor is the actual price low, not the later pivot confirmation timestamp.

For the `2026-09-07T08:45:00Z | tier2_strong` case:

- last lower-low candle low: `0.08909`
- buffered SL target: `0.08909 * 0.9985 = 0.08896` approx

## Liquidity-Pool Exit Plan

For Phase 1 we do **not** use the old partial-exit / break-even ladder.
The exit decision is driven by the **5m** liquidity pools plus OB and public-trade
flow. We switched from 1h to 5m so small intermediate pools above entry are
visible and do not get skipped.

### Rule 1: Check the 5m pools above price

After entry, look at the liquidity pools **above the current price**:

- first upper pool
- second upper pool
- only then larger / more distant clusters

### Rule 2: First pool can be full exit

If the first major peak sits under a large liquidity void and the next heavy
cluster above is far away, while strong liquidity builds below price, then the
first peak can be a **100% exit**.

That means:

- limited upside into the next cluster
- clear downside liquidity below
- good chance of mean reversion after the first exhaustion peak

### Rule 3: Continue only if momentum is still strong

If price breaks the first pool and public-trade delta is still strongly
positive, do not wait for the middle of the next cluster.

Instead:

- treat the first pool as a checkpoint only
- move the TP to the next **meaningful** overhead pool (min ~0.8% further up),
  not the next 5m micro-stack a few bps higher
- use the upper edge of that pool when flow is strong
- if we de-risk there, keep it small
- set TP at the **lower edge of the second upper pool**
- keep that TP fixed unless a new breakout changes the plan

### Rule 4: Use OB + public trades at the pool touch

When price reaches the first pool:

- if delta is still strong and volatility is healthy, continue toward the second pool
- if delta is weak, use a small safety buffer under the pool edge
- if OB turns ask-heavy, the continuation case is weaker

### Rule 5: Skip weak ladder setups

If the first pool is too close to the entry and the next pool is much farther
away, the setup can be too poor to trade.

Use the sparse ladder / fee filter (narrow — do not blanket-block close first pools):

- round-trip fee assumption: **~0.11%** (`ROUND_TRIP_FEE_PCT`)
- **never** ignore when a calibrated mass TP is set
- **never** ignore when stretch is live: second pool exists, not void, confirm Δ
  strong, followthrough healthy (protects trades like 17.09)
- ignore only when:
  1. TP is locked to the first pool (void / no second) **and** first pool `< 0.4%`, or
  2. first pool itself is below fees+dust (`< ~0.16%`) **and** stretch is not live
- ignore reason: `reward_below_fees`

In the DOGE example, a first pool around `0.08392` with only about `0.3%`
room is not enough on its own if the next pool sits much higher, around
`0.08512`.

## Thresholds (starting values)

These are the first concrete values to use in the backtest. We can tune them
later from the long sample.

Phase-1b (loosened after first run):

- **Delta okay for continuation**: `confirm_delta >= +100k`
- **Delta strong**: `confirm_delta >= +200k`
- **Followthrough still healthy**: followthrough delta should stay positive or
  at least not fade sharply against the trade
- **OB bid-stark okay**: `bid_5bps / ask_5bps >= 1.05`
- **OB bid-stark strong**: `bid_5bps / ask_5bps >= 1.20`
- **Weak-delta safety buffer**: `0.1%` below the selected pool edge
- **First pool too close**: `< 0.4%` from entry → **always ignore** (even with strong delta)
- **Second pool far**: `>= 1.0%` extra room above the first pool
- **Second pool void**: `>= 2.0%` extra room; only forces first-pool exit when
  flow is also weak. Strong flow may still continue to the second pool.

## Evaluation Order

1. Is the trade still structurally valid?
2. Is the first upper pool far enough away to matter?
3. Is public-trade delta still strong at the pool touch?
4. Does OB still support continuation?
5. Choose the TP based on the first / second pool rule.
6. Use the SL only for invalidation below the last structural low.

## Liquidity Map (5m)

Use the **5m** liquidity map as the exit filter (was 1h; 5m shows smaller pools).

After the trade is opened, check the liquidity pools **above price** first:

- first upper pool
- second upper pool
- only then larger / more distant pools

If the first major peak already sits under a large liquidity void and the next
heavy cluster above is far away, while thick liquidity blocks build up below,
then the first peak is a valid **100% exit candidate**.

In that situation, the market has:

- limited upside room into the next cluster
- clear downside liquidity targets below price
- a high chance of mean reversion after the first exhaustion peak

This is a stronger exit signal than the EMA stack alone and can justify a full
de-risk at the first peak.

### Two-pool continuation rule

If price has already broken the first pool and the public-trade delta is still
strongly positive, do **not** wait for the move to run into the middle of the
next cluster.

Instead:

- treat the first pool as a checkpoint only
- if you de-risk there, keep it small
- set TP immediately at the **lower edge of the second upper pool**
- treat the second pool as the target zone
- use the first pullback into that zone as the exit, not a later extension
- if the first pool is hit with strong positive delta and high volatility,
  lock the TP at the second pool immediately
- do **not** keep ratcheting the TP higher just because price temporarily keeps
  moving; once the second pool is chosen, keep it fixed unless a new breakout
  and fresh OB/public-trade expansion justify a new plan
- if delta is **not** strongly positive at the pool touch, keep a small safety
  buffer under the pool edge instead of assuming a perfect touch fill

This avoids giving back the run after the first strong breakout leg has already
done its job.

### Sparse ladder filter

Sometimes the first upper pool is very close to the entry, so the available
profit into that pool is too small to matter after fees and noise. If the next
upper pool is much farther away, the trade only makes sense when the entry is
backed by clearly positive and expanding delta.

Use this as a filter:

- if the first pool is closer than about `0.8%` from the entry, treat it as a
  weak reward setup
- if the first pool is closer than `0.8%` and delta is also very low, ignore
  the trade
- if the next pool is far away, require strong delta and clean continuation
  structure before entering
- if delta is not strong enough, skip the trade instead of forcing a setup that
  only pays a few tenths of a percent into the first pool

In the DOGE example, a first pool around `0.08392` with only about `0.3%`
room is not enough on its own if the next pool sits much higher, around
`0.08512`. In that case, the trade should only be taken when public-trade delta
confirms that the move can realistically reach beyond the first pool.

## Phase C calibrated mass TP (longs)

At entry (decision_ts), prefer a **1m LLD cluster** as the TP target when all
gates pass (Phase C on EMA59-touch calibration):

- target distance from entry: `0.8% … 2.15%`
- cluster `strength_sum ≥ 5.51`
- `|confirm_delta| ≥ 71_000`
- entry OB ratio ≥ `1.0`

Mode: `calibrated_mass_cluster` (mass → synthetic UpperPool as TP).
If no cluster qualifies, fall back to the 5m ladder + approach-zone rules above.

Locked 10-long backtest: `reports/long_exit_phase1g_calibrated_mass.json`
(dashboard default via `exit_pool_backtester`).

## Open Work

- live cancel lead-time once MD period is past
- EMA-band refinement of the mass target
- separate short-side thresholds
