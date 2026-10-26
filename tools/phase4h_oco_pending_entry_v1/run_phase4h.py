#!/usr/bin/env python3
"""Phase 4H — OCO pending entry manager (isolated V2 extension)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
TOOLS4D = ROOT / "tools" / "phase4d_pool_landscape_backtester_v2"
sys.path.insert(0, str(TOOLS4D))
sys.path.insert(0, str(TOOLS))

from oco_engine import run_oco_backtest  # noqa: E402
from signal_provider import load_all_signals  # noqa: E402

CONFIG = TOOLS / "strategy_config_oco_v1.json"
RESULTS = ROOT / "results" / "phase4h_oco_pending_entry_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4H_OCO_PENDING_ENTRY_V1.md"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def build_unresolved_md() -> str:
    return """# Unresolved execution decisions (Phase 4H)

**Not activated in this extension — require explicit user sign-off.**

| ID | Topic | Status |
|----|-------|--------|
| TP-QUALITY-GEEIGNET | TP = *geeigneter* dense cluster vs nearest depth-2 band | UNRESOLVED |
| MS-3B-GATE | Experimental WATCH / LONG_CANDIDATE as hard block | UNRESOLVED |
| MANUAL-NO-TRADE | C001/C002 manual NO_SHORT labels not auto-filtered in signal feed | UNRESOLVED — documented only |
| LONG-TP-SOURCE | Full PD1 upper landscape vs 3B confirmation snapshot | UNRESOLVED |
| SLIPPAGE-FUNDING | Slippage and funding | UNRESOLVED |
| INTRABAR-EXIT | SL/TP same-bar ordering (conservative ambiguous) | UNRESOLVED |

OCO pending manager implements **confirmed** limit/SL/OCO-cancel/position-cap rules only.
"""


def build_report(cfg: dict, out: dict) -> str:
    events = out["oco_order_events"]
    fills = [e for e in events if e.get("event_type") == "ORDER_FILLED"]
    oco_cancel = [e for e in events if e.get("event_type") == "OCO_CANCELLED"]
    amb = out["oco_ambiguous_cases"]
    checks = out["oco_single_position_checks"]
    ok_checks = sum(1 for c in checks if str(c.get("ok")).lower() == "true")
    return "\n".join(
        [
            "# DOGEUSDT Phase 4H — OCO Pending Entry Manager V1",
            "",
            "**Status:** Isolated extension; Phase 4D results unchanged.",
            "",
            f"- Variant: `{cfg['entry_execution_variant']}`",
            f"- Pilot: {cfg['pilot_from_utc']} → {cfg['pilot_to_utc']}",
            "",
            "## Summary",
            "",
            f"- ORDER_FILLED events: **{len(fills)}**",
            f"- OCO_CANCELLED events: **{len(oco_cancel)}**",
            f"- Ambiguous OCO cases: **{len(amb)}**",
            f"- Single-position checks OK: **{ok_checks}/{len(checks)}**",
            "",
            "## Confirmed behavior",
            "",
            "- SHORT + LONG limits may coexist; first fill cancels other pendings (OCO).",
            "- Per-order POOL_ZONE_INVALIDATED on depth-2 loss; no time timeout.",
            "- Max one open position; no new ORDER_PENDING while position open.",
            "- Same-bar SHORT+LONG touch → `AMBIGUOUS_OCO_FILL_SEQUENCE` (no invented ordering).",
            "",
            "## Artifacts",
            "",
            "`results/phase4h_oco_pending_entry_v1/`",
            "",
            "**STOP**",
        ]
    )


def main() -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    RESULTS.mkdir(parents=True, exist_ok=True)
    signals = load_all_signals(cfg)
    prices = load_prices()
    out = run_oco_backtest(cfg, signals, prices)

    write_csv(RESULTS / "oco_order_events.csv", out["oco_order_events"])
    write_csv(RESULTS / "oco_fill_audit.csv", out["oco_fill_audit"])
    write_csv(RESULTS / "oco_ambiguous_cases.csv", out["oco_ambiguous_cases"])
    write_csv(RESULTS / "oco_single_position_checks.csv", out["oco_single_position_checks"])
    (RESULTS / "unresolved_execution_decisions.md").write_text(
        build_unresolved_md(), encoding="utf-8"
    )
    REPORT.write_text(build_report(cfg, out), encoding="utf-8")
    print(json.dumps({"fills": len(out["oco_fill_audit"]), "events": len(out["oco_order_events"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
