#!/usr/bin/env python3
"""Compare LLD modes P / H / C for a fixed reference bucket (read-only report)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _ensure_paths() -> None:
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _serialize_row(row: dict) -> dict:
    out = dict(row)
    for key in ("known", "source", "break_at"):
        val = out.get(key)
        if isinstance(val, datetime):
            out[key] = val.isoformat()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare LLD modes P/H/C")
    parser.add_argument("--symbol", default="XRPUSDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--entry-open", default="2026-06-01T16:30:00+00:00")
    parser.add_argument(
        "--pane-from", default="2026-02-10T00:00:00+00:00", help="UI custom from (UTC)"
    )
    parser.add_argument("--pane-to", default="2026-09-30T23:59:59+00:00")
    parser.add_argument("--history-weeks", type=int, default=12)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    _ensure_paths()
    from pool_scan.clock import bar_close

    from dashboard.research_charts.lld_research_kernel import (
        MODE_CAUSAL,
        MODE_HIST_REPLAY,
        MODE_PANE_PARITY,
        candle_stamps,
        find_pool_by_bottom,
        hist_replay_report,
        load_pane_candles,
        pane_parity_selected,
        parity_diff_vs_lld_objects,
        pool_known_at,
        ui_lld_config,
    )

    entry_open = datetime.fromisoformat(args.entry_open.replace("Z", "+00:00"))
    decision_time = bar_close(entry_open, args.timeframe)
    from_unix = int(datetime.fromisoformat(args.pane_from.replace("Z", "+00:00")).timestamp())
    to_unix = int(datetime.fromisoformat(args.pane_to.replace("Z", "+00:00")).timestamp())

    packed, pane_candles = load_pane_candles(
        args.symbol,
        args.timeframe,
        from_unix=from_unix,
        to_unix=to_unix,
        history_weeks=args.history_weeks,
    )
    stamps = candle_stamps(pane_candles)
    try:
        idx = stamps.index(entry_open)
    except ValueError:
        print("entry-open not in pane candles", file=sys.stderr)
        return 1

    ref = pane_candles[idx]
    ref_ohlc = {
        "open": float(ref.open),
        "high": float(ref.high),
        "low": float(ref.low),
        "close": float(ref.close),
        "bucket_open": entry_open.isoformat(),
        "decision_close": decision_time.isoformat(),
    }

    cfg = ui_lld_config(args.timeframe)
    lld_cfg = {
        "highest_len": cfg.highest_len,
        "lowest_len": cfg.lowest_len,
        "amount": int(cfg.amount),
    }

    mode_p = pane_parity_selected(pane_candles, args.timeframe, cfg)
    parity = parity_diff_vs_lld_objects(pane_candles, args.timeframe, cfg)
    mode_h = hist_replay_report(pane_candles, args.timeframe, decision_time, cfg)
    from dashboard.research_charts.lld_research_kernel import (
        parity_scanner_vs_ui_at_index,
        scanner_pools_for_index,
        ui_selected_rows_at_index,
    )

    ui_selected, tip_prefix, _ = ui_selected_rows_at_index(
        pane_candles, args.timeframe, idx, cfg
    )
    causal_rows, _ui_sel, proof = scanner_pools_for_index(
        pane_candles,
        args.timeframe,
        idx,
        cfg,
    )
    scanner_parity = parity_scanner_vs_ui_at_index(
        pane_candles, args.timeframe, idx, cfg
    )

    def pick(rows: list[dict], bottom: float, side: str = "upper") -> list[dict]:
        return [
            _serialize_row(r)
            for r in find_pool_by_bottom(rows, bottom)
            if r.get("side") == side
        ]

    close_px = ref_ohlc["close"]

    def bottom_gt(rows: list[dict]) -> list[dict]:
        return [
            _serialize_row(r)
            for r in rows
            if r["side"] == "upper" and r["bottom"] > close_px and pool_known_at(r, decision_time)
        ]

    report = {
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "reference_ohlc": ref_ohlc,
        "lld_config": lld_cfg,
        "pane_packed": {
            "strict_complete_buckets": packed.get("strict_complete_buckets"),
            "candle_count": len(pane_candles),
            "first": stamps[0].isoformat() if stamps else None,
            "last": stamps[-1].isoformat() if stamps else None,
        },
        "mode_pane_parity": {
            **mode_p,
            "selected_rows": [_serialize_row(r) for r in mode_p["selected_rows"]],
            "parity_diff": parity,
            "pools_1_2896_h1": pick(mode_p["selected_rows"], 1.2896),
            "pools_1_3072_h1": pick(mode_p["selected_rows"], 1.3072),
        },
        "mode_hist_replay": {
            **{k: v for k, v in mode_h.items() if k not in ("h1_selected_rows", "h2_available_rows")},
            "pools_1_2896_h1": pick(mode_h["h1_selected_rows"], 1.2896),
            "pools_1_3072_h1": pick(mode_h["h1_selected_rows"], 1.3072),
            "pools_1_2896_h2": pick(mode_h["h2_available_rows"], 1.2896),
            "pools_1_3072_h2": pick(mode_h["h2_available_rows"], 1.3072),
            "bottom_gt_close_upper_h2": bottom_gt(mode_h["h2_available_rows"]),
        },
        "mode_live_scanner": {
            "mode": MODE_CAUSAL,
            "lookahead": False,
            "scanner_proof": proof.as_dict(),
            "tip_at_prefix": tip_prefix.isoformat(),
            "parity_vs_ui": scanner_parity,
            "ui_selected_rows": [_serialize_row(r) for r in ui_selected],
            "pool_rows_after_time_filter": [_serialize_row(r) for r in causal_rows],
            "pools_1_2896_ui": pick(ui_selected, 1.2896),
            "pools_1_3072_ui": pick(ui_selected, 1.3072),
            "pools_1_2896_after_time": pick(causal_rows, 1.2896),
            "pools_1_3072_after_time": pick(causal_rows, 1.3072),
            "bottom_gt_close_upper": bottom_gt(causal_rows),
        },
        "modes": [MODE_PANE_PARITY, MODE_HIST_REPLAY, MODE_CAUSAL],
    }

    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n")
        print("wrote", args.out)
    else:
        print(text)
    ok = parity.get("ids_match") and scanner_parity.get("ids_match")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
