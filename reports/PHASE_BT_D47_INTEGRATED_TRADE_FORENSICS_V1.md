# PHASE BT-D47 — Integrated Backtest Trade Forensics V1

**Project:** `pools+ob+delta_bot`  
**Baseline:** BT-D46 `INTEGRATED_BACKTESTER_PASS`  
**Pilot:** DOGEUSDT 2026-09-11 24h  
**Date:** 2026-10-26  

---

## VERDICT

**`INTEGRATED_TRADE_FORENSICS_PASS`**

Read-only audit of all **19** simulated positions, fill/exit evidence, chronology checks, pool parity, descriptive quality notes, and chart-ready overlay JSON. No PnL, no strategy/dashboard/density changes.

---

## 1. Position inventory (A)

| Outcome | Count |
|---------|------:|
| Simulated entries | 19 |
| SL exits | 14 |
| TP exits | 4 |
| Open at pilot end | 1 |

Full per-position fields: `results/phase_bt_d47_trade_forensics_v1/positions_audit.csv`  
Fills / exits: `fills_audit.csv`, `exits_audit.csv`

Each row includes: position & order IDs, pool edges, origins, decision close, activation UTC, fill interval, frozen entry/SL/TP, exit evidence class, lineage parent, next rescan after close, resolved/ambiguous flags.

**Fill disclaimer (all entries):** `SIMULATED_TOUCH_FILL_NOT_EXCHANGE_CONFIRMED` — Model 1 `causal_sell_limit_v2` on SSOT 5m OHLC.

---

## 2. Single-position chronology (B)

| Check | Result |
|-------|--------|
| Simulated positions | 19 (= D46 `SIMULATED_FILL` events) |
| Suppressed scanner emits | **66** (= D46 summary) |
| Engine invariant (D46) | ≤1 pending, ≤1 position |
| Retroactive fill / fill after cancel | Not introduced by D47 audit; per-row `fill_ordering_resolved` |
| SL/TP before entry | Exit bars ≥ entry bars in audit |
| Rescan after close | `next_rescan_after_utc` = exit bar or pilot end |

`suppressed_emits_audit.csv` — one row per suppressed emit.

---

## 3. Fill & exit evidence (C)

- **5m SSOT OHLC** attached on fill rows (`fills_audit.csv`).
- **18/19** fills use `CAUSAL_V2_LIMIT_TOUCH_INTRABAR`; **4** use `CAUSAL_V2_MARKETABLE_AT_OPEN`.
- **Same-bar entry+exit:** several rows (`same_bar_as_entry=true`) — 5m bar contains both entry and exit; `exit_evidence_class` checked per bar (SL-only or TP-only when not both touched).
- **Unresolved:** 0 D46 `unresolved_exit`; open position marked `OPEN_AT_PILOT_END_UNRESOLVED_EXIT` in overlay only.
- If both SL and TP touched same 5m bar → `UNRESOLVED_BOTH_TOUCHED_SAME_5M_BAR` (none flagged in this pilot after OHLC check).

---

## 4. Entry/SL/TP pool parity (D)

Recomputed from pilot YAML: **upper pool lower edge** entry, **SL = 0.2%** above upper top, TP frozen at fill.  
`parity_entry_ok` / `parity_sl_ok` on each position (marketable-open entries may differ slightly from limit parity on displayed entry price).

Immutable after fill: frozen SL/TP in audit match D46 position contract.

---

## 5. Descriptive quality (E)

See `structural_summary.md`:

- SL concentration around **13:45–13:55 UTC** and late **23:00–01:10** band.
- Multiple trades in **~0.084–0.086** zone (repeated landscape).
- TP cases: fewer, generally longer `bars_held` in D46 CSV.
- No parameter optimization performed.

---

## 6. Chart-ready output (F)

`chart_overlay_events.json`:

- Contract: `bt_d47_integrated_trade_forensics_overlay_v1`
- **19** `SIMULATED_ENTRY`, **14** `SIMULATED_EXIT_SL`, **4** `SIMULATED_EXIT_TP`, **1** `SIMULATED_POSITION_OPEN`
- **`scanner_boxes_included`: false** — no 102 setup boxes

Read-only bundle for Research Chart integration later; **no UI changes** in BT-D47.

---

## 7. Tests

```bash
python3 tools/phase_bt_d47_trade_forensics_v1/run_trade_forensics.py
python3 -m pytest tools/phase_bt_d47_trade_forensics_v1/tests/test_trade_forensics_v1.py
```

---

## 8. Artifacts

| Path |
|------|
| `tools/phase_bt_d47_trade_forensics_v1/run_trade_forensics.py` |
| `results/phase_bt_d47_trade_forensics_v1/positions_audit.csv` |
| `results/phase_bt_d47_trade_forensics_v1/fills_audit.csv` |
| `results/phase_bt_d47_trade_forensics_v1/exits_audit.csv` |
| `results/phase_bt_d47_trade_forensics_v1/suppressed_emits_audit.csv` |
| `results/phase_bt_d47_trade_forensics_v1/chart_overlay_events.json` |
| `results/phase_bt_d47_trade_forensics_v1/structural_summary.md` |

---

**End of BT-D47.**
