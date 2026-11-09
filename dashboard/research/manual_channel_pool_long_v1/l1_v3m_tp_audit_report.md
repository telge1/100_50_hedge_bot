# L1 V3M — V3L Take-Profit Audit (READ-ONLY)

## 1. TP selection implementation

- Function: `run_l1_pilot.pick_tp()`
- Input qualified list: `analyze_d2_landscape()` → `qualify_upper_d2_bands()` (canonical PD1 ACTIVE Exact Unique-Origin D2, depth ≥ 2)
- Qualification: upper band `bottom > entry` and distance ≥ `MIN_POOL_DIST_PCT` (0.50 %)
- **Selection rule (no channel deckel): `best = max(qualified, key=lower_bound)`**
- Interpretation: **highest lower_bound** = **farthest** qualified target above entry, **not** nearest
- Nearest would be `min(lower_bound)`
- Executed V3L trades use TP from `apply_d2_gate_to_pending()`; label is often `POOL_BOTTOM_NO_CLEAR_CHANNEL` even without channel

## 2. TP selection verdict

**Selection matches code, but code picks the farthest qualified D2 bottom, not the nearest.**
This can assign distant TP targets when nearer qualified D2 pools exist.

## 3. September focus trades

### 2026-09-16 18:05:00 UTC `d-1f70c9798935` exit=TP pnl=14.5818%

- Entry 0.0796200000 | TP 0.0912300000 | SL 0.0786645600
- Planned TP dist 14.581763% | nearest qualified 0.08026 (0.803818%)
- Skipped nearer pools: 56
- Exit reported 2026-09-19 17:15:00 UTC @ 0.0912300000
- OHLC replay: TP @ 2026-09-19 17:15:00 UTC (ambig=False)
- Max favorable 14.6948% | max adverse -1.1555%

Top qualified D2 targets:
- bottom=0.08026000 top=0.08027400 depth=2 dist=0.803818% selected=False nearest=True
- bottom=0.08030000 top=0.08032800 depth=2 dist=0.854057% selected=False nearest=False
- bottom=0.08035000 top=0.08037000 depth=2 dist=0.916855% selected=False nearest=False

### 2026-09-20 12:10:00 UTC `d-1f70c9798935` exit=TP pnl=11.8944%

- Entry 0.0852500000 | TP 0.0953900000 | SL 0.0842270000
- Planned TP dist 11.894428% | nearest qualified 0.08576 (0.59824%)
- Skipped nearer pools: 32
- Exit reported 2026-09-21 15:00:00 UTC @ 0.0953900000
- OHLC replay: TP @ 2026-09-21 15:00:00 UTC (ambig=False)
- Max favorable 15.2023% | max adverse -0.6452%

Top qualified D2 targets:
- bottom=0.08576000 top=0.08577000 depth=2 dist=0.59824% selected=False nearest=True
- bottom=0.08577000 top=0.08581200 depth=3 dist=0.609971% selected=False nearest=False
- bottom=0.08581200 top=0.08581800 depth=2 dist=0.659238% selected=False nearest=False

### 2026-09-24 09:40:00 UTC `d-1f70c9798935` exit=SL pnl=-1.2000%

- Entry 0.0919600000 | TP 0.1044840000 | SL 0.0908564800
- Planned TP dist 13.618965% | nearest qualified 0.09485 (3.142671%)
- Skipped nearer pools: 39
- Exit reported 2026-10-02 18:40:00 UTC @ 0.0908564800
- OHLC replay: SL @ 2026-10-02 18:40:00 UTC (ambig=False)
- Max favorable 8.5146% | max adverse -1.7399%

Top qualified D2 targets:
- bottom=0.09485000 top=0.09486000 depth=2 dist=3.142671% selected=False nearest=True
- bottom=0.09487000 top=0.09488000 depth=2 dist=3.164419% selected=False nearest=False
- bottom=0.09488000 top=0.09496400 depth=3 dist=3.175294% selected=False nearest=False

## 4. Variant stats

- **A**: executed=12 wins=6 sl=6 hit=50.0% gross=38.5805% pool_tp=3 channel=3 ambig=0 far_tp_flag=12
- **B**: executed=11 wins=6 sl=5 hit=54.5% gross=39.7805% pool_tp=3 channel=3 ambig=0 far_tp_flag=11
- **C**: executed=4 wins=3 sl=1 hit=75.0% gross=9.0290% pool_tp=0 channel=3 ambig=0 far_tp_flag=4
