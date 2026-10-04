# E1R Freeze V1 — Independent Forensic Audit

### Main verdict
**AUDIT_PASS_WITH_CAVEATS**

### Lookahead audit
- future pools: 0
- future break state: 0 (explicit causal break_at<=t checks on snapshots)
- EMA timing: cross count ref vs ind mismatch 0
- reclaim timing: retroactive block mismatches 0
- entry-bar leakage: 0

### E1R state parity
Trade-level block flags match reference: True (mismatches=0)

### Outcome parity
Independent first_hit mismatches (excl. same-bar): 0; same_bar_ambiguous=0

### PnL parity
pnl_mismatch_count=0; identity {"baseline_sum": 61.4442, "e1r_sum": 64.2888, "blocked_net": -2.8446, "baseline_minus_blocked": 64.2888}

### DD parity
{
  "XRPUSDT": {
    "expected": 0.8254,
    "independent": 0.8254,
    "match": true
  },
  "ADAUSDT": {
    "expected": 2.1017,
    "independent": 2.1017,
    "match": true
  },
  "DOGEUSDT": {
    "expected": 1.0579,
    "independent": 1.0579,
    "match": true
  },
  "CROSS": {
    "expected": 1.7465,
    "independent": 1.7465,
    "match": true
  }
}

### Overlapping positions
overlap_count=18 (sum-of-trade-% model does not enforce one position per coin)

### Portfolio interpretation
+64.29% is the **sum of per-trade pnl_pct** across 48 E1R trades (non-compounded, no shared capital constraint). Cross-coin DD uses chronologically interleaved trades; can be **lower** than single-coin ADA DD (2.10%) when other coins peak concurrently.

### BAD-4 audit
See manual_bad4_audit.md (4/4 rows in blocked table)

### Negative controls
12 allowed trades checked — see negative_control_audit.csv

### Any caveats
- Pool enumeration uses same `scanner_pools_for_index` data path as research (not reimplemented LLD kernel).
- EMA on bars: independent SMA-seed EMA compared to frozen bar ema200 for crosses (count mismatch logged).
- Same-bar SL+TP resolved conservatively as SL first (matches frozen short_trade_detail).

### Trust level
**MEDIUM** — PnL/DD/outcome independently reproduced with caveats on scanner dependency and EMA cross parity; E1R state machine reimplemented with 0 mismatches vs frozen CSV.

