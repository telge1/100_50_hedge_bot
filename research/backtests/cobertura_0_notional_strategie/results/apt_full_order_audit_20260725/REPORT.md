# APT Full Order Audit (Netto-BE)

Read-only double-check of executed orders, averages, fees, PnL, and final net-BE exit for `shared_be`, `individual_tp_2p00`, and `individual_tp_scaled` on the canonical APT seed.

## Tolerances

- AVG_TOL = 1e-09
- FEE_TOL = 1e-09
- PNL_TOL = 1e-06
- QTY_TOL = 1e-09

## Per-policy verdict

| policy | status | flat | invariant_fails | fee_fails | avg_fails | overall |
|---|---|---|---|---|---|---|
| shared_be | RECOVERED_BE | True | 0 | 0 | 0 | PASS |
| individual_tp_2p00 | RECOVERED_BE | True | 0 | 0 | 0 | PASS |
| individual_tp_scaled | RECOVERED_BE | True | 0 | 0 | 0 | PASS |

## Answers

- **shared_be**: overall PASS; fee_ledger_match=True; first_BE=2026-02-06T00:15:00+00:00; ambiguous_intrabar=2.
- **individual_tp_2p00**: overall PASS; fee_ledger_match=True; first_BE=2026-01-30T06:25:00+00:00; ambiguous_intrabar=5.
- **individual_tp_scaled**: overall PASS; fee_ledger_match=True; first_BE=2026-01-30T18:20:00+00:00; ambiguous_intrabar=7.

## Event order (all policies under net_be)

1) activate pending TP/BE from prior bar; 2) arm recovery if activation touched; 3) process overlay exits (shared BE / individual TP); 4) net_be full-exit gate (before adds); 5) short adds shallow→deep; 6) legacy post-add full-exit skipped under net_be

## Artifacts

See CSV/JSON siblings in this folder plus per-policy walkthroughs.

No invariant violations recorded.
