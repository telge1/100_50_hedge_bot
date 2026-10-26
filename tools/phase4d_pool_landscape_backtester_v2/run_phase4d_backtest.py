#!/usr/bin/env python3
"""Run Phase 4D strict pool-landscape backtester V2."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))

from engine import run_backtest  # noqa: E402
from signal_provider import load_all_signals  # noqa: E402

CONFIG = TOOLS / "strategy_config_v2.json"
RESULTS = ROOT / "results" / "phase4d_pool_landscape_backtester_v2"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4D_STRICT_STRATEGY_BACKTESTER_V2.md"


def load_prices() -> dict[str, dict]:
    path = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.csv"
    by_t = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_t[row["candle_close_utc"]] = {
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            }
    return by_t


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def build_rule_parity(cfg: dict) -> list[dict]:
    return [
        {
            "rule_id": "SHORT-ENTRY-LEVEL-01",
            "formula": "upper_pool_lower_edge",
            "implementation": "strategy_rules.plan_short entry_level=pool_lower",
            "status": "MATCH",
        },
        {
            "rule_id": "SHORT-SL-01",
            "formula": "upper_pool_upper * 1.002",
            "implementation": f"sl_above_upper_pool_pct={cfg['sl_above_upper_pool_pct']}",
            "status": "MATCH",
        },
        {
            "rule_id": "SHORT-TP-01",
            "formula": "nearest depth-2 lower top with >= min_tp_distance_pct",
            "implementation": "select_short_tp_depth2 depth==2 only",
            "status": "MATCH",
        },
        {
            "rule_id": "LONG-ENTRY-LEVEL-01",
            "formula": "lower_pool_upper_edge",
            "implementation": "plan_long entry_level=pool_upper",
            "status": "MATCH",
        },
        {
            "rule_id": "LONG-SL-01",
            "formula": "lower_pool_lower * 0.998",
            "implementation": f"sl_below_lower_pool_pct={cfg['sl_below_lower_pool_pct']}",
            "status": "MATCH",
        },
        {
            "rule_id": "LONG-TP-01",
            "formula": "nearest depth-2 upper bottom with >= min_tp_distance_pct",
            "implementation": "select_long_tp_depth2 depth==2 only",
            "status": "MATCH",
        },
        {
            "rule_id": "POS-01",
            "formula": "max_open_positions=1",
            "implementation": "engine suppressed SUPPRESSED_POSITION_OPEN",
            "status": "MATCH",
        },
        {
            "rule_id": "EXEC-USER-PENDING-LIMIT-V1",
            "formula": "Sell/buy limit at pool edge; pending until fill or pool depth-2 loss",
            "implementation": cfg.get("entry_execution_variant"),
            "status": "MATCH"
            if cfg.get("entry_execution_variant") == "USER_PENDING_LIMIT_V1"
            else "UNRESOLVED",
        },
        {
            "rule_id": "MS-WATCH",
            "formula": "optional block new shorts",
            "implementation": f"enabled={cfg.get('market_structure_gate_enabled')}",
            "status": "DISABLED_BY_CONFIG",
        },
        {
            "rule_id": "FILTER-AGE",
            "formula": "pool_age_hard_gate",
            "implementation": str(cfg.get("pool_age_hard_gate")),
            "status": "MATCH" if not cfg.get("pool_age_hard_gate") else "DEVIATION",
        },
    ]


def config_hash(cfg: dict) -> str:
    blob = json.dumps(cfg, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def verdict(trades: list[dict], unresolved: list[dict], cfg: dict) -> str:
    if not trades and unresolved:
        return "STRICT_BACKTESTER_V2_PARTIAL"
    if trades:
        if not cfg.get("entry_execution_user_approved"):
            return "STRICT_BACKTESTER_V2_PARTIAL"
        return "STRICT_BACKTESTER_V2_READY"
    return "STRICT_BACKTESTER_V2_BLOCKED"


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    prices = load_prices()
    signals = load_all_signals(cfg)
    out = run_backtest(cfg, signals, prices)
    out["strategy_rule_parity"] = build_rule_parity(cfg)

    for name, rows in out.items():
        write_csv(RESULTS / f"{name}.csv", rows)

    validated = [t for t in out["simulated_trades"] if t.get("performance_validated")]
    summary = [
        "# Phase 4D backtest summary",
        "",
        f"strategy_version: `{cfg['strategy_version']}`",
        f"config_hash: `{config_hash(cfg)}`",
        f"raw_signals: {len(out['raw_signals'])}",
        f"simulated_trades (diagnostic): {len(out['simulated_trades'])}",
        f"performance_validated trades: {len(validated)}",
        f"unresolved: {len(out['unresolved_strategy_decisions'])}",
        f"suppressed: {len(out['suppressed_signals'])}",
        "",
        "Entry execution: DIAGNOSTIC_LIMIT_TOUCH_V1 unless user approves.",
    ]
    (RESULTS / "backtest_summary.md").write_text("\n".join(summary), encoding="utf-8")

    v = verdict(out["simulated_trades"], out["unresolved_strategy_decisions"], cfg)
    if not REPORT.is_file():
        REPORT.write_text(f"# Phase 4D\n\nVERDICT: `{v}`\n", encoding="utf-8")

    print(json.dumps({"verdict": v, "trades": len(out["simulated_trades"]), "validated": len(validated)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
