#!/usr/bin/env python3
"""BT-D46 integrated backtest driver."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
D45 = ROOT / "results/phase_bt_d45_execution_contract_v1"
OUT = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1"

for p in (
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d46_integrated_short_backtester_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from integrated_backtester_v1 import CONTRACT, FILL_LABEL, run_integrated_backtest  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range  # noqa: E402

REF = "2026-09-11T15:35:00Z"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    setups_doc = json.loads((RUN_DIR / "setups.json").read_text(encoding="utf-8"))
    setups_by_close = {s["decision_utc"]: s for s in setups_doc["setups"]}

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_excl)
    density = create_density_session(config)

    st = run_integrated_backtest(
        config, prices, bars, setups_by_close, density, pilot_end=end
    )

    d45_cmp = ""
    if (D45 / "execution_model_comparison.csv").exists():
        rows = list(csv.DictReader((D45 / "execution_model_comparison.csv").open()))
        m1 = next(r for r in rows if "model_1" in r["execution_model"])
        d45_cmp = f"""## BT-D45 baseline (segment replay, not identical engine)

| Metric | BT-D45 M1 | BT-D46 integrated |
|--------|-----------|-------------------|
| Modeled entries | {m1['modeled_entries']} | {st.counts.get('model_1_simulated_entries', 0)} |
| Closed (SL+TP) | {int(m1['modeled_closed_positions'])} | {st.counts.get('closed_sl', 0) + st.counts.get('closed_tp', 0)} |
| Open at end | {m1['still_open_positions']} | {st.counts.get('open_at_pilot_end', 0)} |
| Blocked scanner | {m1['blocked_scanner_emits_while_position_open']} | {st.counts.get('suppressed_scanner_emits_position_open', 0)} |

Discrepancies: D45 replays pre-built order timelines with fill scan **before** cancel bar;
D46 evaluates fill **at each live bar** then lifecycle review at close (may differ on same-bar cancel/fill).
Neither is exchange-confirmed. Fill label: `{FILL_LABEL}`.
"""

    summary = {
        "contract": CONTRACT,
        "fill_label": FILL_LABEL,
        "scanner_candidate_emits": st.counts.get("scanner_candidate_emit", 0),
        "pending_create": st.counts.get("pending_create", 0),
        "pending_keep": st.counts.get("pending_keep", 0),
        "pending_cancel": st.counts.get("pending_cancel", 0),
        "pending_replace": st.counts.get("pending_replace", 0),
        "lineage_links": len(st.lineage),
        "model_1_simulated_entries": st.counts.get("model_1_simulated_entries", 0),
        "closed_sl": st.counts.get("closed_sl", 0),
        "closed_tp": st.counts.get("closed_tp", 0),
        "open_at_pilot_end": st.counts.get("open_at_pilot_end", 0),
        "unresolved_exit": st.counts.get("unresolved_exit", 0),
        "suppressed_scanner_emits_position_open": st.counts.get(
            "suppressed_scanner_emits_position_open", 0
        ),
        "independent_trade_count": "NOT_EXCHANGE_CONFIRMED",
        "pnl_computed": False,
        "reference_1535": {
            "note": "Replace 15:40, no touch before — no SIMULATED_FILL on ref order if integrated matches D42",
            "decision_close": REF,
        },
    }

    ref_events = [e for e in st.events if e.get("decision_close_utc") in (REF, "2026-09-11T15:40:00Z")]

    write_csv(OUT / "chronological_events.csv", st.events)
    write_csv(OUT / "pending_order_lineage.csv", st.lineage)
    write_csv(OUT / "simulated_positions.csv", st.positions_log)
    write_csv(OUT / "execution_ambiguities.csv", st.ambiguities)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (OUT / "comparison_bt_d45.md").write_text(d45_cmp, encoding="utf-8")

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
