# Implementation Plan (Phases A–H)

All new code stays under `research/liquidation_level/` until Phase H explicitly considers scanner integration.
No threshold inventing in early phases. No commits implied by this plan.

---

## Phase A — Read-only Event-Join and Timeline Audit

| Item | Spec |
|------|------|
| New file | `research/liquidation_level/sweep_scanner_join_audit.py` (+ `run_sweep_scanner_join_audit.py`) |
| Inputs | Winner config; feather OHLCV; optional existing ValidationEvent table |
| Outputs | `results/.../join_timeline_audit/` — per-event as-of 5m/15m/30m availability table; HTF aggregator equality report; lookahead assertions |
| Tests | `tests/test_sweep_scanner_join_audit.py` — synthetic contiguous 5m grid; assert incomplete HTF absent; pivot lag; no future bars |
| Stop | Join deterministic; causality table matches `timeframe_causality.md`; no feature classification yet |

## Phase B — Sweep-activated Analysis Window (no entry)

| Item | Spec |
|------|------|
| New file | `research/liquidation_level/sweep_analysis_window.py` |
| Inputs | `SweepTriggerEvent` list; 5m frame |
| Outputs | Window trajectories for variants W3/W6/W12; states IDLE→…→EXPIRED skeleton without path labels |
| Tests | Window lengths; start at `signal_index+1`; no entry fields emitted |
| Stop | Every winner event has closed-candle windows; expiry works; no TP/SL |

## Phase C — Feature Snapshots 30m/15m/5m per Event

| Item | Spec |
|------|------|
| New file | `research/liquidation_level/sweep_context_snapshots.py` |
| Inputs | Events; `point_audit` / indicator frames as-of decision times |
| Outputs | Frozen HTF snapshot at sweep + per-bar 5m feature rows in window |
| Tests | Frozen HTF unchanged across window; 5m updates only; warm-up flags |
| Stop | Feature pack covers inventory columns marked available; missing features explicitly null |

## Phase D — Reversal vs Breakout Classification

| Item | Spec |
|------|------|
| New file | `research/liquidation_level/sweep_path_classifier.py` |
| Inputs | Snapshots + structure/PA event streams (read-only) |
| Outputs | Labels `{short_reversal, long_continuation, unclear}` + reason codes (**rules research, still threshold-gated later**) |
| Tests | Synthetic acceptance vs failed break examples; unclear default |
| Stop | Labels produceable without entries; dialect collision guarded |

## Phase E — 2–3 Candle Momentum Confirmation

| Item | Spec |
|------|------|
| New file | `research/liquidation_level/sweep_momentum_bridge.py` |
| Inputs | Path label + optional PA arm from 5m |
| Outputs | Momentum confirm/invalidate/expire relative to **path arm**, not raw sweep |
| Tests | Ages 0..3 semantics preserved; cannot confirm on sweep bar alone without arm rules |
| Stop | Momentum only after defined confirmation level; separate from W3/W6/W12 expiry |

## Phase F — Entry Audit with Next-Candle Open

| Item | Spec |
|------|------|
| New file | `research/liquidation_level/sweep_entry_audit.py` |
| Inputs | Confirmed events |
| Outputs | Hypothetical entry at **open after confirmation close**; no live wiring |
| Tests | `entry_index > confirmation_index`; no same-bar entry |
| Stop | Entry set defined; still no fee/overlap optimization required |

## Phase G — TP/SL, Fees, Non-overlapping Trades

| Item | Spec |
|------|------|
| New file | `research/liquidation_level/sweep_trade_economics_audit.py` |
| Inputs | Entry audit + OHLCV |
| Outputs | Horizon/TP-SL summaries; overlap filter; cost model |
| Tests | Conservative SL-first; no lookahead exit; overlap exclusion |
| Stop | Economics comparable across path classes; config frozen |

## Phase H — Possible Scanner Integration (last)

| Item | Spec |
|------|------|
| New file | only if justified: shim under `research/regime_scanner/` **or** keep liquidation consumer that calls scanner APIs read-only |
| Inputs | Proven Phase G edge + causality proof |
| Outputs | Integration design + optional behind-flag hook; **still no live trading** |
| Tests | Scanner protected modules hash-stable; liquidation config_id unchanged |
| Stop | Explicit go/no-go: integrate vs keep parallel research stack |

---

## Dependency order

```
A → B → C → D → E → F → G → H
```

Do not start D before C freeze semantics are proven.
Do not start H before G economics and causality audits pass.

## Global constraints (all phases)

- Do not change winner liquidation thresholds
- Do not alter existing regime_scanner confirmation logic in-place
- Do not commit unless explicitly requested
- Audit artifacts for this architecture stay under `results/sweep_scanner_architecture_audit/`
