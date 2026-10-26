#!/usr/bin/env python3
"""BT-D45 execution contract pilot."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D42 = ROOT / "results/phase_bt_d42_fill_cancel_chronology_v1"
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
OUT = ROOT / "results/phase_bt_d45_execution_contract_v1"

for p in (
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d45_execution_contract_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
sys.path.insert(0, str(ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1"))
from fill_chronology_v1 import activation_contract_note  # noqa: E402
from execution_contract_v1 import (  # noqa: E402
    CONTRACT,
    MODEL_1,
    MODEL_2,
    build_execution_evidence_rows,
    comparison_row,
    first_modeled_fill,
    run_single_position_contract,
)
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402

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
    timelines = list(csv.DictReader((D42 / "order_timelines.csv").open(encoding="utf-8")))
    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    pilot_end = end

    evidence = build_execution_evidence_rows(timelines, prices, pilot_end)
    m1 = run_single_position_contract(timelines, model=MODEL_1, config=config, prices=prices, pilot_end=pilot_end)
    m2 = run_single_position_contract(timelines, model=MODEL_2, config=config, prices=prices, pilot_end=pilot_end)

    positions = m1.positions + m2.positions
    ambiguities = m1.ambiguities + m2.ambiguities
    comparison = comparison_row(m1, m2)

    ref_tl = next(t for t in timelines if t["decision_close_utc"] == REF)
    ref_m1 = first_modeled_fill(ref_tl, prices, MODEL_1, pilot_end)
    ref_m2 = first_modeled_fill(ref_tl, prices, MODEL_2, pilot_end)

    summary = f"""# BT-D45 execution contract pilot

Contract: `{CONTRACT}`

## Models (explicit — no silent optimism)
- **{MODEL_1}**: `causal_sell_limit_v2` while pending active and before cancel effective close
- **{MODEL_2}**: only `CAUSAL_V2_MARKETABLE_AT_OPEN` (open >= limit); intrabar touch → UNRESOLVED

## Activation
{activation_contract_note()}

## Comparison
{json.dumps(comparison, indent=2)}

## Reference 15:35 UTC
- Model 1 fill: {ref_m1}
- Model 2 fill: {ref_m2}
- Expected: no fill before 15:40 replace (high < entry on 15:40 bar)

## PnL
Not computed in BT-D45.

## BT-D44 alignment
D44 Path B used Model-1-like entries ({m1.modeled_entries} vs D44 reported 19 — should match if same rules).
"""

    write_csv(OUT / "execution_evidence.csv", evidence)
    write_csv(OUT / "execution_model_comparison.csv", comparison)
    write_csv(OUT / "position_timelines.csv", positions)
    write_csv(OUT / "ambiguity_cases.csv", ambiguities)
    (OUT / "summary.md").write_text(summary, encoding="utf-8")
    print(json.dumps({"m1_entries": m1.modeled_entries, "m2_entries": m2.modeled_entries, "ref_ok": ref_m1[0] is None}, indent=2))


if __name__ == "__main__":
    main()
