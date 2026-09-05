# Historical Structure-Break Orderbook Deep Dive

**Primary Decision:** `HISTORICAL_BREAK_OB_PATTERNS_VISIBLE`

Mode: **ORDERBOOK_ONLY** (no historical public trades for these days).
Scanner: C3.4B `protected_medium` on feather 5m + existing 1h/4h feathers. No new structure definition.

## 1. How many important structure breaks on the 10 OB days?

- Clustered important events: **67**
- Raw rising-edge prints before clustering: 231

## 2. Selected for deep dive

- `APTUSDT_PROTECTED_LOW_BREAK_bearish_20260512_1p0801_4h` | APTUSDT bearish PROTECTED_LOW_BREAK TF=4h level=1.0801 avail=2026-05-12T16:00:00.000Z
- `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260228_0p09133_4h` | DOGEUSDT bearish PROTECTED_LOW_BREAK TF=4h level=0.09133 avail=2026-02-28T08:00:00.000Z
- `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260115_0p14187_4h` | DOGEUSDT bearish PROTECTED_LOW_BREAK TF=4h level=0.14187 avail=2026-01-15T20:00:00.000Z
- `APTUSDT_PROTECTED_LOW_BREAK_bearish_20251229_1p589_4h` | APTUSDT bearish PROTECTED_LOW_BREAK TF=4h level=1.589 avail=2025-12-29T20:00:00.000Z
- `APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260523_0p9572_1h` | APTUSDT bullish PROTECTED_HIGH_BREAK TF=1h level=0.9572 avail=2026-05-23T19:00:00.000Z
- `DOGEUSDT_PROTECTED_HIGH_BREAK_bullish_20260228_0p09259_1h` | DOGEUSDT bullish PROTECTED_HIGH_BREAK TF=1h level=0.09259 avail=2026-02-28T20:00:00.000Z
- `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260228_0p09106_1h` | DOGEUSDT bearish PROTECTED_LOW_BREAK TF=1h level=0.09106 avail=2026-02-28T07:00:00.000Z
- `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260220_0p09777_1h` | DOGEUSDT bearish PROTECTED_LOW_BREAK TF=1h level=0.09777 avail=2026-02-20T14:00:00.000Z
- `DOGEUSDT_PROTECTED_HIGH_BREAK_bullish_20260220_0p09882_1h` | DOGEUSDT bullish PROTECTED_HIGH_BREAK TF=1h level=0.09882 avail=2026-02-20T03:00:00.000Z
- `APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260118_1p8316_1h` | APTUSDT bullish PROTECTED_HIGH_BREAK TF=1h level=1.8316 avail=2026-01-18T19:00:00.000Z
- `APTUSDT_PROTECTED_LOW_BREAK_bearish_20260118_1p8352_1h` | APTUSDT bearish PROTECTED_LOW_BREAK TF=1h level=1.8352 avail=2026-01-18T04:00:00.000Z
- `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260115_0p14449_1h` | DOGEUSDT bearish PROTECTED_LOW_BREAK TF=1h level=0.14449 avail=2026-01-15T03:00:00.000Z
- `APTUSDT_PROTECTED_LOW_BREAK_bearish_20260106_1p896_1h` | APTUSDT bearish PROTECTED_LOW_BREAK TF=1h level=1.896 avail=2026-01-06T18:00:00.000Z
- `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h` | DOGEUSDT bearish PROTECTED_LOW_BREAK TF=1h level=0.14909 avail=2026-01-06T17:00:00.000Z
- `APTUSDT_PROTECTED_HIGH_BREAK_bullish_20251230_1p72_1h` | APTUSDT bullish PROTECTED_HIGH_BREAK TF=1h level=1.72 avail=2025-12-30T10:00:00.000Z

## 3. Timeframes / structure types

- Clustered types: {'PROTECTED_HIGH_BREAK': 30, 'PROTECTED_LOW_BREAK': 36, 'CHOCH': 1}
- Clustered TFs: {'1h': 16, '5m': 46, '4h': 5}
- Selected TFs: {'4h': 4, '1h': 11}
- Selected directions: {'bearish': 10, 'bullish': 5}
- Selected symbols: {'APTUSDT': 7, 'DOGEUSDT': 8}

## 4. Orderbook BEFORE breaks

Before break (examples):
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20251229_1p589_4h: support_wall Δ(−10s→break)=0 notional; dist_pre10=588.4203901825056
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20251230_1p72_1h: support_wall Δ(−10s→break)=11183 notional; dist_pre10=-8.720930232557178
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260118_1p8316_1h: support_wall Δ(−10s→break)=7475 notional; dist_pre10=-4.640751255731575
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260512_1p0801_4h: support_wall Δ(−10s→break)=-50883 notional; dist_pre10=11.573002499768291
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260523_0p9572_1h: support_wall Δ(−10s→break)=-30243 notional; dist_pre10=-29.774341830338685
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h: support_wall Δ(−10s→break)=-604906 notional; dist_pre10=19.115970219331107
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260115_0p14449_1h: support_wall Δ(−10s→break)=9081 notional; dist_pre10=2.4223129628352997
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260220_0p09777_1h: support_wall Δ(−10s→break)=-554879 notional; dist_pre10=3.5798302137677456

## 5. Orderbook AT break

At break (examples):
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20251229_1p589_4h: at break mid=1.6825 beyond=0 support_wall=6466.8164400000005
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20251230_1p72_1h: at break mid=1.7215 beyond=1 support_wall=54336.41312
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260118_1p8316_1h: at break mid=1.8322500000000002 beyond=1 support_wall=23074.82703
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260512_1p0801_4h: at break mid=1.0778500000000002 beyond=1 support_wall=7874.982467
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260523_0p9572_1h: at break mid=0.95795 beyond=1 support_wall=8730.351643
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h: at break mid=0.14715499999999998 beyond=1 support_wall=0.0
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260115_0p14449_1h: at break mid=0.144475 beyond=1 support_wall=559864.0703
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260220_0p09777_1h: at break mid=0.097605 beyond=1 support_wall=74255.32773

## 6. Continue vs reclaim/hold

Across 15 deep-dive events (ORDERBOOK_ONLY): pulled-before-break=3, removed-at-break=1, reclaim/hold=4, accepted-no-quick-reclaim=7, mixed/unclear=0. Without trades, pull vs consumption cannot be separated — size drops are pull/consume proxies.
- accepted_n=7 reclaim_hold_n=4

## 7–8. Wall-lifecycle patterns / cases

- Classification counts: {'REFILL_THEN_RECLAIM': 3, 'BREAK_ACCEPTED_NO_QUICK_RECLAIM': 7, 'WALL_CONSUMED_OR_REMOVED_BREAK': 1, 'WALL_HELD_OR_RECLAIM': 1, 'WALL_PULLED_BEFORE_BREAK': 3}
- pulled_before_break=3
- consumed_or_removed=1
- refill/reclaim or held=4

## 9. Visible before break

Before break (examples):
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20251229_1p589_4h: support_wall Δ(−10s→break)=0 notional; dist_pre10=588.4203901825056
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20251230_1p72_1h: support_wall Δ(−10s→break)=11183 notional; dist_pre10=-8.720930232557178
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260118_1p8316_1h: support_wall Δ(−10s→break)=7475 notional; dist_pre10=-4.640751255731575
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260512_1p0801_4h: support_wall Δ(−10s→break)=-50883 notional; dist_pre10=11.573002499768291
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260523_0p9572_1h: support_wall Δ(−10s→break)=-30243 notional; dist_pre10=-29.774341830338685
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h: support_wall Δ(−10s→break)=-604906 notional; dist_pre10=19.115970219331107
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260115_0p14449_1h: support_wall Δ(−10s→break)=9081 notional; dist_pre10=2.4223129628352997
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260220_0p09777_1h: support_wall Δ(−10s→break)=-554879 notional; dist_pre10=3.5798302137677456

## 10. Visible only after break

After break (examples):
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20251229_1p589_4h: +60s beyond=0 support_wall=14809.5234 class=REFILL_THEN_RECLAIM
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20251230_1p72_1h: +60s beyond=1 support_wall=46705.4874 class=BREAK_ACCEPTED_NO_QUICK_RECLAIM
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260106_1p896_1h: +60s beyond=1 support_wall=12668.54433 class=BREAK_ACCEPTED_NO_QUICK_RECLAIM
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260118_1p8316_1h: +60s beyond=1 support_wall=14395.61799 class=BREAK_ACCEPTED_NO_QUICK_RECLAIM
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260118_1p8352_1h: +60s beyond=1 support_wall=15553.185267 class=BREAK_ACCEPTED_NO_QUICK_RECLAIM
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260512_1p0801_4h: +60s beyond=1 support_wall=10007.839395 class=REFILL_THEN_RECLAIM
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260523_0p9572_1h: +60s beyond=1 support_wall=8455.636885000002 class=REFILL_THEN_RECLAIM
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h: +60s beyond=1 support_wall=0.0 class=WALL_CONSUMED_OR_REMOVED_BREAK

## 11. Especially informative events

- APTUSDT_PROTECTED_LOW_BREAK_bearish_20251229_1p589_4h → REFILL_THEN_RECLAIM
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20251230_1p72_1h → BREAK_ACCEPTED_NO_QUICK_RECLAIM
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260106_1p896_1h → BREAK_ACCEPTED_NO_QUICK_RECLAIM
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260118_1p8316_1h → BREAK_ACCEPTED_NO_QUICK_RECLAIM
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260118_1p8352_1h → BREAK_ACCEPTED_NO_QUICK_RECLAIM
- APTUSDT_PROTECTED_LOW_BREAK_bearish_20260512_1p0801_4h → REFILL_THEN_RECLAIM
- APTUSDT_PROTECTED_HIGH_BREAK_bullish_20260523_0p9572_1h → REFILL_THEN_RECLAIM
- DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h → WALL_CONSUMED_OR_REMOVED_BREAK

## 12. Data problems

- Quality: {'DATA_WARNING': 3, 'DATA_VALID': 12}
- ORDERBOOK_ONLY (no historical trades). DOGE 2026-01-15 sequence RESET_SEEN → DATA_WARNING. Some scanner close-breaks lack contemporaneous BBO cross in the OB window (marked no_bbo_break_at_marked_break). Pull vs consumption cannot be separated without trades.
- One clustered 4h event (APT 2026-05-23 00:00 close) excluded from selection because candle_open was prior day without OB.
- Raw rising edges include CHOCH/EXTERNAL_BOS; after clustering most map into PROTECTED_*_BREAK labels.

## 13. Logical next test (not executed)

Next (not run): attach historical trades for the same 10 days to separate pull vs consumption; then compare WALL_PULLED_BEFORE_BREAK vs BREAK_ACCEPTED_NO_QUICK_RECLAIM with distance-conditioned residuals — still no live gate.

Artifacts: `results/historical_structure_break_ob_deep_dive_20260808`

