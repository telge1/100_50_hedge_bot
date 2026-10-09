#!/usr/bin/env python3
"""UI vs scanner pool parity at each bar close (no signals). Read-only audit."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RANGE_START = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 6, 8, 23, 59, 59, tzinfo=timezone.utc)
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
HISTORY_WEEKS = 12
SYMBOLS = ("XRPUSDT", "ADAUSDT")
TIMEFRAMES = ("15m", "4h")
OUT = ROOT / "ui_scanner_parity_week_2026-06-01_08_v1.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _ensure_paths() -> None:
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(root / "backtester" / "short") not in sys.path:
        sys.path.insert(0, str(root / "backtester" / "short"))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _break_status(row: dict, decision: datetime) -> str:
    from dashboard.research_charts.lld_research_kernel import pool_known_at

    if not pool_known_at(row, decision):
        return "not_available"
    break_at = row.get("break_at")
    if break_at is not None and break_at <= decision:
        return "broken"
    return "open"


def _row_fingerprint(row: dict, decision: datetime) -> dict:
    known = row["known"]
    break_at = row.get("break_at")
    return {
        "pool_id": row["pool_id"],
        "bottom": row["bottom"],
        "top": row["top"],
        "known": known.isoformat() if known else None,
        "break_at": break_at.isoformat() if break_at else None,
        "break_status_at_decision": _break_status(row, decision),
    }


def _ref_rows_from_chain(candles: list, timeframe: str, config) -> list[dict]:
    from dashboard.research_charts.lld_research_kernel import rows_from_pools
    from dashboard.research_charts.service import lld_config_for_timeframe, select_lld_pools_for_chart
    from dashboard.research_charts.trp_import import load_trp
    from dashboard.research_charts.workspace_session import get_workspace

    trp = load_trp()
    cfg_ref = lld_config_for_timeframe(config or get_workspace().lld_config, str(timeframe))
    result_ref = trp["run_liquidity_location"](candles, cfg_ref)
    tip_ref = getattr(result_ref, "last_timestamp", None)
    selected_ref = select_lld_pools_for_chart(
        result_ref.pools_all,
        int(cfg_ref.amount),
        tip=tip_ref,
    )
    return rows_from_pools(selected_ref)


def parity_at_close(
    candles: list,
    timeframe: str,
    index: int,
    config,
    edge_tol: float = 1e-8,
) -> dict:
    from dashboard.research_charts.lld_research_kernel import (
        candle_stamps,
        parity_diff_pool_rows,
        ui_selected_rows_for_pane,
    )
    from pool_scan.clock import bar_close

    slice_candles = candles[: index + 1]
    stamps = candle_stamps(candles)
    decision = bar_close(stamps[index], timeframe)
    kernel_rows, tip = ui_selected_rows_for_pane(slice_candles, timeframe, config)
    ref_rows = _ref_rows_from_chain(slice_candles, timeframe, config)
    diff = parity_diff_pool_rows(ref_rows, kernel_rows)
    field_mismatch: list[dict] = []
    ref_map = {r["pool_id"]: r for r in ref_rows}
    ker_map = {r["pool_id"]: r for r in kernel_rows}
    for pid in sorted(set(ref_map) & set(ker_map)):
        a, b = ref_map[pid], ker_map[pid]
        fa, fb = _row_fingerprint(a, decision), _row_fingerprint(b, decision)
        if fa != fb:
            if abs(a["bottom"] - b["bottom"]) <= edge_tol and abs(a["top"] - b["top"]) <= edge_tol:
                if fa["known"] == fb["known"] and fa["break_status_at_decision"] == fb["break_status_at_decision"]:
                    continue
            field_mismatch.append({"pool_id": pid, "ref": fa, "kernel": fb})
    ids_match = bool(diff.get("ids_match")) and not field_mismatch
    return {
        "index": index,
        "entry_open": stamps[index].isoformat(),
        "decision_close": decision.isoformat(),
        "tip": tip.isoformat(),
        "pane_len": len(slice_candles),
        "ids_match": ids_match,
        "diff": diff,
        "field_mismatch": field_mismatch[:5],
        "field_mismatch_count": len(field_mismatch),
    }


def indices_in_range(candles: list, timeframe: str) -> list[int]:
    from dashboard.research_charts.lld_research_kernel import candle_stamps
    from pool_scan.clock import bar_close

    stamps = candle_stamps(candles)
    out: list[int] = []
    for i, op in enumerate(stamps):
        close_t = bar_close(op, timeframe)
        if RANGE_START <= close_t <= RANGE_END:
            out.append(i)
    return out


def sample_indices_for_tf(candles: list, indices: list[int], timeframe: str, per_day: int) -> list[int]:
    from dashboard.research_charts.lld_research_kernel import candle_stamps
    from pool_scan.clock import bar_close

    stamps = candle_stamps(candles)
    by_day: dict[str, list[int]] = {}
    for i in indices:
        day = bar_close(stamps[i], timeframe).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(i)
    picked: list[int] = []
    for day in sorted(by_day):
        day_idxs = by_day[day]
        if len(day_idxs) <= per_day:
            picked.extend(day_idxs)
            continue
        step = (len(day_idxs) - 1) / max(per_day - 1, 1)
        for k in range(per_day):
            pos = int(round(k * step))
            picked.append(day_idxs[min(pos, len(day_idxs) - 1)])
    return sorted(set(picked))


def xrp_custom_ui_report() -> dict:
    from dashboard.research_charts.lld_research_kernel import (
        find_pool_by_bottom,
        hist_replay_report,
        load_pane_candles,
        ui_lld_config,
    )
    from pool_scan.clock import bar_close

    entry = datetime(2026, 6, 1, 16, 30, tzinfo=timezone.utc)
    decision = bar_close(entry, "15m")
    _p, candles = load_pane_candles(
        "XRPUSDT",
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    cfg = ui_lld_config("15m")
    h = hist_replay_report(candles, "15m", decision, cfg)
    h1 = h["h1_selected_rows"]

    def pick_upper(bottom: float) -> list[dict]:
        return [
            {
                "pool_id": r["pool_id"],
                "bottom": r["bottom"],
                "top": r["top"],
            }
            for r in find_pool_by_bottom(h1, bottom)
            if r.get("side") == "upper"
        ]

    u96 = pick_upper(1.2896)
    u72 = pick_upper(1.3072)
    return {
        "mode": "xrp_custom_pane_ui_report_only",
        "entry_open": entry.isoformat(),
        "decision_close": decision.isoformat(),
        "pane_from": PANE_FROM.isoformat(),
        "pane_to": PANE_TO.isoformat(),
        "lookahead": True,
        "note": "Full custom pane; no scanner signals",
        "upper_1_2896_in_h1": u96,
        "upper_1_3072_in_h1": u72,
        "expect_1_2896_absent": len(u96) == 0,
        "expect_1_3072_present": len(u72) > 0,
        "expectations_met": len(u96) == 0 and len(u72) > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-only", action="store_true")
    parser.add_argument("--sample", action="store_true", help="force 4 points/day/TF")
    parser.add_argument("--per-day", type=int, default=4)
    args = parser.parse_args()

    _ensure_paths()
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config

    t0 = time.perf_counter()
    report: dict = {
        "range_close_utc": [RANGE_START.isoformat(), RANGE_END.isoformat()],
        "pane_from": PANE_FROM.isoformat(),
        "pane_to": PANE_TO.isoformat(),
        "rule": "pane prefix ends at bar close; tip=last bar; parity before strategy filters",
        "symbols": {},
        "xrp_custom_ui": None,
    }

    # Benchmark on XRP 15m first 3 indices in range
    bench: dict = {}
    for tf in TIMEFRAMES:
        _p, candles = load_pane_candles(
            "XRPUSDT",
            tf,
            from_unix=int(PANE_FROM.timestamp()),
            to_unix=int(PANE_TO.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        cfg = ui_lld_config(tf)
        idxs = indices_in_range(candles, tf)
        t_b = time.perf_counter()
        n = min(3, len(idxs))
        for i in idxs[:n]:
            parity_at_close(candles, tf, i, cfg)
        elapsed_b = time.perf_counter() - t_b
        per_check = elapsed_b / max(n, 1)
        total_checks = len(SYMBOLS) * sum(
            len(indices_in_range(
                load_pane_candles(
                    s,
                    tf,
                    from_unix=int(PANE_FROM.timestamp()),
                    to_unix=int(PANE_TO.timestamp()),
                    history_weeks=HISTORY_WEEKS,
                )[1],
                tf,
            ))
            for s in SYMBOLS
        )
        # avoid double load - approximate
        total_checks = len(SYMBOLS) * len(idxs)
        bench[tf] = {
            "bars_in_range_xrp": len(idxs),
            "benchmark_checks": n,
            "benchmark_seconds": round(elapsed_b, 3),
            "seconds_per_check": round(per_check, 3),
            "estimated_full_two_symbols_seconds": round(per_check * total_checks, 1),
        }
    report["benchmark"] = bench

    if args.benchmark_only:
        print(json.dumps(report, indent=2))
        return 0

    use_sample = args.sample
    for tf in TIMEFRAMES:
        est = bench[tf]["estimated_full_two_symbols_seconds"]
        if est > 600 and not args.sample:
            use_sample = True
            report["sampling"] = {
                "forced": True,
                "reason": f"estimated>{600}s",
                "per_day": args.per_day,
            }

    for symbol in SYMBOLS:
        report["symbols"][symbol] = {}
        for tf in TIMEFRAMES:
            _p, candles = load_pane_candles(
                symbol,
                tf,
                from_unix=int(PANE_FROM.timestamp()),
                to_unix=int(PANE_TO.timestamp()),
                history_weeks=HISTORY_WEEKS,
            )
            cfg = ui_lld_config(tf)
            all_idx = indices_in_range(candles, tf)
            if use_sample:
                check_idx = sample_indices_for_tf(candles, all_idx, tf, args.per_day)
                mode = "sample"
            else:
                check_idx = all_idx
                mode = "full"
            mismatches: list[dict] = []
            first_fail: dict | None = None
            t_tf = time.perf_counter()
            for i in check_idx:
                res = parity_at_close(candles, tf, i, cfg)
                if not res["ids_match"]:
                    mismatches.append(
                        {
                            "entry_open": res["entry_open"],
                            "decision_close": res["decision_close"],
                            "diff": res["diff"],
                            "field_mismatch_count": res["field_mismatch_count"],
                            "field_mismatch": res["field_mismatch"],
                        }
                    )
                    if first_fail is None:
                        first_fail = mismatches[-1]
            elapsed_tf = time.perf_counter() - t_tf
            report["symbols"][symbol][tf] = {
                "mode": mode,
                "closed_bars_in_range": len(all_idx),
                "checked": len(check_idx),
                "matches": len(check_idx) - len(mismatches),
                "mismatches": len(mismatches),
                "first_mismatch": first_fail,
                "elapsed_seconds": round(elapsed_tf, 2),
            }

    report["xrp_custom_ui"] = xrp_custom_ui_report()
    report["total_elapsed_seconds"] = round(time.perf_counter() - t0, 2)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", OUT)
    print("total_elapsed_s", report["total_elapsed_seconds"])
    return 0 if all(
        report["symbols"][s][tf]["mismatches"] == 0
        for s in SYMBOLS
        for tf in TIMEFRAMES
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
