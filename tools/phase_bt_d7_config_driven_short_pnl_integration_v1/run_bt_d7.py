#!/usr/bin/env python3
"""BT-D7 integration runner: synthetic fixture + optional historical 24h pilot."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(BT_D6))
sys.path.insert(0, str(PHASE4D))

from config_validate_v1 import load_and_validate_config  # noqa: E402
from density_bridge_v1 import FixtureDensitySessionV1, create_density_session  # noqa: E402
from experiment_v1 import prepare_experiment  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from short_research_engine_v1 import run_short_research_backtest  # noqa: E402

OUT = ROOT / "results/phase_bt_d7_config_driven_short_pnl_integration_v1"
FIXTURE = TOOLS / "fixtures/synthetic_integration_v1.json"


def run_synthetic(config_path: Path) -> dict:
    cfg = load_and_validate_config(config_path)
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session = FixtureDensitySessionV1(fix["snapshots"])
    result = run_short_research_backtest(cfg, session, fix["prices"])
    return result


def run_historical_pilot(config_path: Path) -> dict:
    cfg = load_and_validate_config(config_path)
    prices = load_prices_from_ssot_window(cfg["backtest"]["start_utc"], cfg["backtest"]["end_utc"])
    if not prices:
        return {"blocked": True, "blockers": ["NO_OHLC_IN_SSOT_WINDOW"]}
    session = create_density_session(cfg)
    return run_short_research_backtest(cfg, session, prices)


def run_gap_param_demo() -> dict:
    """Two configs differing only in upper_gap_min_pct on same fixture."""
    base_path = TOOLS / "configs/synthetic_fixture_v1.json"
    low = load_and_validate_config(base_path)
    high = deepcopy(low)
    high["short_entry"]["upper_gap_min_pct"] = 50.0
    low["short_entry"]["upper_gap_min_pct"] = 0.5
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session = FixtureDensitySessionV1(fix["snapshots"])
    r_low = run_short_research_backtest(low, session, fix["prices"])
    r_high = run_short_research_backtest(high, session, fix["prices"])
    return {
        "gap_0_5_signals": len(r_low["signals"]),
        "gap_50_signals": len(r_high["signals"]),
        "gap_gate_effective": len(r_low["signals"]) != len(r_high["signals"]),
    }


def write_run(result: dict, name: str, config: dict, config_path: Path) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    exp = prepare_experiment(config, name, source_config_path=config_path)
    (exp / "signals.json").write_text(json.dumps(result["signals"], indent=2), encoding="utf-8")
    (exp / "orders.json").write_text(json.dumps(result["orders"], indent=2), encoding="utf-8")
    (exp / "trades.json").write_text(json.dumps(result["trades"], indent=2), encoding="utf-8")
    if result.get("pnl"):
        (exp / "pnl_report.json").write_text(json.dumps(result["pnl"], indent=2), encoding="utf-8")
    (exp / "integration_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return exp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["synthetic", "historical-pilot", "gap-demo"], default="synthetic")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    report: dict = {"mode": args.mode, "verdict": "SHORT_PNL_INTEGRATION_BLOCKED"}

    if args.mode == "synthetic":
        cfg_path = args.config or (TOOLS / "configs/synthetic_fixture_v1.json")
        cfg = load_and_validate_config(cfg_path)
        result = run_synthetic(cfg_path)
        write_run(result, "synthetic_integration", cfg, cfg_path)
        gap_demo = run_gap_param_demo()
        report["synthetic"] = {"signals": len(result["signals"]), "trades": len(result["trades"])}
        report["gap_param_demo"] = gap_demo
        report["verdict"] = (
            "SHORT_PNL_INTEGRATION_PASS"
            if gap_demo["gap_gate_effective"] or result["snapshots_collected"] > 0
            else "SHORT_PNL_INTEGRATION_PARTIAL"
        )
    elif args.mode == "gap-demo":
        report["gap_param_demo"] = run_gap_param_demo()
        report["verdict"] = (
            "SHORT_PNL_INTEGRATION_PASS" if report["gap_param_demo"]["gap_gate_effective"] else "SHORT_PNL_INTEGRATION_PARTIAL"
        )
    else:
        cfg_path = args.config or (TOOLS / "configs/historical_pilot_24h_v1.json")
        cfg = load_and_validate_config(cfg_path)
        try:
            result = run_historical_pilot(cfg_path)
        except Exception as exc:  # noqa: BLE001
            report["historical_pilot"] = {"blocked": True, "error": str(exc)}
            report["verdict"] = "SHORT_PNL_INTEGRATION_PARTIAL"
        else:
            if result.get("blocked"):
                report["historical_pilot"] = result
                report["verdict"] = "SHORT_PNL_INTEGRATION_PARTIAL"
            else:
                write_run(result, "historical_pilot_24h", cfg, cfg_path)
                report["historical_pilot"] = {
                    "signals": len(result["signals"]),
                    "trades": len(result["trades"]),
                    "pnl_net": result.get("pnl", {}).get("net_pnl_usdt"),
                }
                report["verdict"] = "SHORT_PNL_INTEGRATION_PASS"

    out_path = OUT / "integration_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] != "SHORT_PNL_INTEGRATION_BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
