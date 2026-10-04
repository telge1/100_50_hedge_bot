# FROZEN E1R V1 — DO NOT MODIFY

Immutable reference copy of the shadow/research strategy **E1R V1** (EMA200 bullish cross + `rising_3` + one-bar reclaim).  
**Do not edit files in this directory.** New work belongs in a new versioned folder (e.g. `e1r_cluster3_ema200_v2`).

## Identity

| Field | Value |
|--------|--------|
| Repo | `/home/telgenbuescher/projects/pools+ob+delta_bot` |
| Branch | `pools-ob-delta-bot` |
| Freeze commit | `adf7dac448e5fcb4202c22a0e4955ee4d43b06a4` |
| Git tag | `e1r_cluster3_ema200_shadow_freeze_v1` |
| Freeze date (commit) | 2026-10-02 (UTC+2 author timestamp) |
| Source tree | `results/pool_scan/context_study_v1/` (read-only copy; originals not moved) |

Verify tag → commit: `git rev-parse e1r_cluster3_ema200_shadow_freeze_v1^{commit}`

## Status

- **Research / shadow strategy**
- **`production_integrated`: false** — not wired into live `pool_pattern` or bot execution paths

## Strategy logic (E1R V1)

**START:** bullish 15m EMA200 cross + `rising_3` on structural lower clusters.

**ACTIVE:** block new shorts while regime active (`close > EMA200` and `rising_3`).

**EMA dip:** `close <= EMA200`; if structure intact → **PENDING** (one bar).

**RECLAIM:** next closed 15m bar — `close > EMA200`, `rising_3` intact, structure intact → remain **ACTIVE**.

**FAIL:** otherwise reset (failed reclaim / structure break / `rising_3` false).

**Not in V1:** GAP3 start rule, D4, entry-distance / momentum / pool-age filters, extra 30m/4h overlays, long strategy, V2 experiments.

Canonical code copy: `code/analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py`

## Development window (freeze)

**2026-06-01 – 2026-07-31** (UTC) · coins: **XRPUSDT**, **ADAUSDT**, **DOGEUSDT**

Baseline cohort: `data_reference/floor_guard_signal_list_v1.json` (`with_guard`).

## Freeze results (E1R, dev window)

| Metric | Value |
|--------|------:|
| E1R trades | 48 |
| Total PnL % (sum) | 64.29 |
| Profit factor | 13.08 |
| Max drawdown % | 1.75 |
| XRP | 15 trades / +22.74% / DD 0.8254% |
| ADA | 11 trades / +14.75% / DD 2.1017% |
| DOGE | 22 trades / +26.81% / DD 1.0579% |

Full tables: `reports/analyze_crosscoin_e1r_final_pnl_comparison_v1.md`

## Audit

- Verdict: **AUDIT_PASS_WITH_CAVEATS**
- No detected future pool leakage
- No future break leakage
- No entry-bar leakage
- No retroactive reclaim mismatch
- 71/71 block-state parity
- Outcome / PnL / DD parity (see `audit/AUDIT_REPORT.md`)

## Caveats

- Summed trade % (not a single compounded capital account)
- Overlapping positions possible in research sum model
- No fees, funding, or slippage
- Pool scanner not independently rebuilt from zero in audit

## Post-freeze OOS (separate — not original freeze performance)

Under `reports/oos_validation/` — **POST-FREEZE VALIDATION** only.

| Segment | Trades | PnL % | PF | DD % |
|---------|-------:|------:|---:|-----:|
| Apr–Jul 2026 research sum | 55 | +68.15 | 9.57 | 2.63 |
| Execution-realistic (1 pos/coin) | 47 | +59.92 | 9.61 | 2.11 |

## Layout

- `code/` — frozen Python reproduction chain
- `data_reference/` — signal JSON/MD (no raw candles)
- `reports/` — final PnL artifacts (dev window)
- `audit/` — independent freeze audit
- `docs/` — original `E1R_FREEZE_V1.md`
- `FROZEN_MANIFEST.json` — machine-readable inventory + SHA256
- `DO_NOT_EDIT.txt` — policy reminder
