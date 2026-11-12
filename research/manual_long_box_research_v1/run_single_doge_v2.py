#!/usr/bin/env python3
"""Single manual DOGEUSDT long box — V2 entry via original V3AO watcher_fsm."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASH = ROOT / "backtester/short/dashboard"
OVERLAY_OUT = DASH / "research_charts/user_data/manual_long_box_v1_doge.json"
REPORT_OUT = Path(__file__).resolve().parent / "reports/MANUAL_LONG_BOX_V2_SINGLE_DOGE_REPORT.md"


def _bootstrap():
    if str(DASH) not in sys.path:
        sys.path.insert(0, str(DASH))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _load_candles_5m(symbol: str, start_ts: int, end_ts: int) -> list[dict]:
    from research_charts.service import load_candles

    packed = load_candles(symbol, "5m", start=start_ts, end=end_ts, limit=50000)
    rows = packed.get("candles") or packed.get("bars") or []
    out = []
    for c in rows:
        t = c.get("time")
        if isinstance(t, (int, float)):
            out.append(
                {
                    "time": int(t),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                }
            )
    out.sort(key=lambda x: x["time"])
    return out


def main() -> int:
    _bootstrap()
    from research.manual_long_box_research_v1.l1_imports import default_watcher_config
    from research.manual_long_box_research_v1.markers_v1 import build_marker_bundle, report_rows
    from research.manual_long_box_research_v1.simulator_v2 import (
        DEFAULT_CONFIG_SOURCE,
        REPLAY_WATCHER_FROM_CONFIG,
        replay_long_box_v2,
    )
    from research.manual_long_box_research_v1.single_box import SingleBoxError, require_single_manual_doge_long

    ap = argparse.ArgumentParser()
    ap.add_argument("--drawings", type=Path, default=None)
    args = ap.parse_args()

    try:
        sel = require_single_manual_doge_long(drawings_path=args.drawings)
    except SingleBoxError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}, indent=2))
        return 2

    setup = sel.setup
    margin = timedelta(days=14)
    t0 = setup.start_ts - int(margin.total_seconds())
    t1 = setup.end_ts + int(margin.total_seconds())
    candles = _load_candles_5m(setup.symbol, t0, t1)
    cfg = default_watcher_config()
    fsm, research = replay_long_box_v2(candles, setup, watcher_cfg=cfg)

    bundle = build_marker_bundle(setup, research)
    bundle["entry_engine"] = "v2"
    bundle["fsm"] = REPLAY_WATCHER_FROM_CONFIG
    bundle["config"] = DEFAULT_CONFIG_SOURCE
    OVERLAY_OUT.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY_OUT.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    rows = report_rows(setup, research)
    cfg_doc = cfg.to_documented_dict() if hasattr(cfg, "to_documented_dict") else {}
    REPORT_OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_OUT.write_text(
        "\n".join(
            [
                "# Manual Long Box V2 — Single DOGE",
                "",
                f"- FSM: `{REPLAY_WATCHER_FROM_CONFIG}`",
                f"- Config: `{DEFAULT_CONFIG_SOURCE}`",
                f"- cfg: `{json.dumps(cfg_doc)}`",
                f"- terminal: `{fsm.terminal_reason if fsm else 'n/a'}`",
                f"- trades: {len(research.trades)}",
                f"- trade_status: {bundle.get('trade_status')}",
                "",
                "See `WATCHER_PARITY_V2_REPORT.md` for parity tests.",
            ]
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "v2": True,
                "drawing_id": setup.drawing_id,
                "fsm_entries": len(fsm.entries) if fsm else 0,
                "trade_status": bundle.get("trade_status"),
                "trades_simulated": len(research.trades),
                "overlay": str(OVERLAY_OUT),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
