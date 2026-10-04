"""Find 15m short entries on pools the Research Chart already selected.

Live scanner (``--lld-mode causal``): UI ``select_lld_pools_for_chart`` on the
pane prefix ending at each bar (tip = last bar). Modes ``pane-parity`` and
``hist-replay`` are JSON-only (hist-replay reproduces custom pane; no signals).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_CAUSAL_ROWS = ROOT / "short_entry_15m_causal_v1.csv"
OUT_CAUSAL_METHOD = ROOT / "short_entry_15m_causal_method_v1.json"
OUT_PANE_PARITY = ROOT / "short_entry_15m_pane_parity_v1.json"
OUT_HIST_REPLAY = ROOT / "short_entry_15m_hist_replay_v1.json"
OUT_PROBE = ROOT / "short_entry_15m_probe_v1.json"
OUT_XRP_UI_PARITY = ROOT / "short_entry_15m_xrp_ui_parity_v1.json"
LOAD_START = datetime(2025, 12, 11, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
REPORT_FROM = datetime(2026, 3, 1, tzinfo=timezone.utc)
REPORT_TO_DEFAULT = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
MIN_GAP_PCT = 0.8
MIN_BRIDGE_GAP_PCT = 0.5
MIN_TP_PCT = 0.8
STOP_PAD = 0.002
ATTACH_PCT = 1.0
TIMEFRAMES = ("15m", "30m", "1h", "4h")
HISTORY_WEEKS = 12
PANE_FROM_DEFAULT = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO_DEFAULT = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _ensure_paths() -> None:
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def load_market(symbol: str, load_end: datetime | None = None):
    """Load multi-TF market bars from ClickHouse 1m (research default: ``LOAD_END``)."""
    from dashboard.research_charts.service import _candles_from_packed, load_candles
    from dashboard.research_charts.trp_import import load_trp
    from dashboard.research_charts.ui_lld_parity import ui_lld_config
    from pool_scan.clock import bar_close
    from pool_state_maschine.features import ema

    trp = load_trp()
    end_dt = load_end if load_end is not None else LOAD_END
    end_unix = int(end_dt.timestamp())
    markets = {}
    for timeframe in TIMEFRAMES:
        packed = load_candles(
            symbol,
            timeframe,
            start=int(LOAD_START.timestamp()),
            end=end_unix,
            limit=50000,
        )
        if not packed.get("strict_complete_buckets"):
            raise SystemExit(f"candles not strict for {symbol} {timeframe}")
        candles = _candles_from_packed(packed)
        closes = [float(candle.close) for candle in candles]
        ema9 = ema(closes, 9)
        ema20 = ema(closes, 20)
        ema59 = ema(closes, 59)
        ema200 = ema(closes, 200)
        bars = []
        for index, candle in enumerate(candles):
            close_time = bar_close(candle.timestamp, timeframe)
            bars.append({
                "open_time": candle.timestamp if candle.timestamp.tzinfo else candle.timestamp.replace(tzinfo=timezone.utc),
                "close_time": close_time,
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "ema9": ema9[index],
                "ema20": ema20[index],
                "ema59": ema59[index],
                "ema200": ema200[index],
                "candle_index": index,
            })
        markets[timeframe] = {"bars": bars, "candles": candles, "trp": trp, "config_tf": timeframe}
        if timeframe == "15m":
            markets["lld_config"] = ui_lld_config("15m")
    return markets


def last_closed_index(bars: list[dict], moment: datetime) -> int | None:
    chosen = None
    for index, bar in enumerate(bars):
        if bar["close_time"] <= moment:
            chosen = index
        elif chosen is not None:
            break
    return chosen


def down_4h(bars: list[dict], index: int | None) -> bool:
    if index is None or index < 3:
        return False
    bar = bars[index]
    if None in (bar["ema9"], bar["ema20"], bar["ema59"], bar["ema200"]):
        return False
    depth = (bar["ema200"] - bar["ema59"]) / bar["ema200"] * 100.0
    return (
        bar["close"] < bar["ema20"] < bar["ema200"]
        and bar["ema59"] < bar["ema200"]
        and bar["ema9"] < bar["ema200"]
        and depth >= 2.0
    )


def attached(pool: dict, bar: dict) -> bool:
    if bar["close"] >= pool["bottom"]:
        return False
    if pool["bottom"] <= 0:
        return False
    if max(bar["open"], bar["close"]) >= pool["bottom"]:
        return False
    if bar["high"] + 1e-9 < pool["bottom"]:
        return False
    distance = (pool["bottom"] - bar["close"]) / pool["bottom"] * 100.0
    return distance <= ATTACH_PCT


def next_upper(pools: list[dict], current: dict, moment: datetime) -> dict | None:
    choices = [
        pool for pool in pools
        if pool["side"] == "upper"
        and pool["pool_id"] != current["pool_id"]
        and pool["known"] <= moment
        and pool["bottom"] > current["top"]
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if not choices:
        return None
    choices.sort(key=lambda pool: (pool["bottom"], pool["known"]))
    for pool in choices:
        bridge_gap = (pool["bottom"] - current["top"]) / current["top"] * 100.0
        if bridge_gap >= MIN_BRIDGE_GAP_PCT:
            return pool
    return None


def next_lower(pools: list[dict], close: float, moment: datetime, min_gap_pct: float) -> dict | None:
    choices = [
        pool for pool in pools
        if pool["side"] == "lower"
        and pool["known"] <= moment
        and pool["top"] < close
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if not choices:
        return None
    choices.sort(key=lambda pool: (-pool["top"], pool["known"]))
    for pool in choices:
        gap_pct = (close - pool["top"]) / close * 100.0
        if gap_pct >= min_gap_pct:
            return pool
    return None


def gap_pct(current: dict, nxt: dict | None) -> float | None:
    if nxt is None or current["top"] <= 0:
        return None
    return (nxt["bottom"] - current["top"]) / current["top"] * 100.0


def row_for(symbol: str, pool: dict, birth: dict, entry: dict, h4: dict, m30, h1, upper, lower, gap: float) -> dict:
    tp_gap = ""
    if lower is not None and entry["close"] > 0:
        tp_gap = round((entry["close"] - lower["top"]) / entry["close"] * 100.0, 4)
    return {
        "symbol": symbol,
        "mode": "live_scanner",
        "entry_open": entry["open_time"].isoformat(),
        "pool_open": birth["open_time"].isoformat(),
        "known_at": pool["known"].isoformat(),
        "pool_id": pool["pool_id"],
        "bottom_price": pool["bottom"],
        "top_price": pool["top"],
        "stop_price": round(pool["top"] * (1.0 + STOP_PAD), 8),
        "close": entry["close"],
        "next_upper_id": upper["pool_id"],
        "next_upper_bottom": upper["bottom"],
        "gap_pct": round(gap, 4),
        "tp_pool_id": "" if lower is None else lower["pool_id"],
        "tp_top": "" if lower is None else lower["top"],
        "tp_gap_pct": tp_gap,
        "trend_4h": "down",
        "ema9_4h": round(h4["ema9"], 8),
        "ema20_4h": round(h4["ema20"], 8),
        "ema59_4h": round(h4["ema59"], 8),
        "ema200_4h": round(h4["ema200"], 8),
    }


def open_pools(
    pools: list[dict],
    side: str,
    moment: datetime,
    *,
    min_bottom: float | None = None,
) -> list[dict]:
    rows = [
        pool for pool in pools
        if pool["side"] == side
        and pool["known"] <= moment
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if min_bottom is not None and side == "upper":
        rows = [pool for pool in rows if pool["bottom"] > min_bottom]
    return rows


def entry_setup(pools: list[dict], bar: dict, moment: datetime) -> tuple[dict, dict, dict, float] | None:
    touched = [
        pool for pool in open_pools(pools, "upper", moment, min_bottom=bar["close"])
        if bar["close"] > bar["open"] and attached(pool, bar)
    ]
    if not touched:
        return None
    pool = min(touched, key=lambda item: (item["bottom"], item["known"]))
    upper = next_upper(pools, pool, moment)
    gap = gap_pct(pool, upper)
    if upper is None or gap is None or gap < MIN_GAP_PCT:
        return None
    lower = next_lower(pools, bar["close"], moment, MIN_TP_PCT)
    if lower is None:
        return None
    return pool, upper, lower, gap


def _serialize_dt(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(type(obj))


def build_15m_bars(candles: list) -> list[dict]:
    from dashboard.research_charts.trp_import import load_trp
    from pool_scan.clock import bar_close
    from pool_state_maschine.features import ema

    load_trp()
    closes = [float(candle.close) for candle in candles]
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    ema59 = ema(closes, 59)
    ema200 = ema(closes, 200)
    bars = []
    for index, candle in enumerate(candles):
        close_time = bar_close(candle.timestamp, "15m")
        bars.append({
            "open_time": candle.timestamp if candle.timestamp.tzinfo else candle.timestamp.replace(tzinfo=timezone.utc),
            "close_time": close_time,
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "ema9": ema9[index],
            "ema20": ema20[index],
            "ema59": ema59[index],
            "ema200": ema200[index],
            "candle_index": index,
        })
    return bars


def run_causal(args: argparse.Namespace) -> None:
    from dashboard.research_charts.lld_research_kernel import (
        MODE_LIVE_SCANNER,
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )

    for path in (OUT_CAUSAL_ROWS, OUT_CAUSAL_METHOD):
        if path.exists() and not args.force:
            raise SystemExit(f"refuse overwrite {path} (use --force)")

    pane_from = datetime.fromisoformat(args.pane_from.replace("Z", "+00:00"))
    pane_to = datetime.fromisoformat(args.pane_to.replace("Z", "+00:00"))
    report_from = datetime.fromisoformat(args.report_from.replace("Z", "+00:00"))
    report_to = datetime.fromisoformat(args.report_to.replace("Z", "+00:00"))
    t0 = time.perf_counter()
    symbols = [args.symbol] if args.symbol else list(SYMBOLS)
    rows = []
    for symbol in symbols:
        market = load_market(symbol)
        _packed, candles = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(pane_from.timestamp()),
            to_unix=int(pane_to.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        lld_cfg = ui_lld_config("15m")
        bars15 = build_15m_bars(candles)
        by_open = {bar["open_time"]: bar for bar in bars15}
        cache: dict[int, tuple] = {}
        seen_pools: set[str] = set()
        floor_block = not args.no_floor_block
        guard = None
        blocked_signals = 0
        if floor_block:
            from short_block_4h_guard import H4FloorGuard, guard_spec

            guard = H4FloorGuard(symbol, pane_from, pane_to)
        for bar in bars15:
            moment = bar["close_time"]
            blocked = False
            if guard is not None:
                blocked, _reason = guard.advance(bar, moment)
            if moment < report_from or moment > report_to:
                continue
            idx = bar["candle_index"]
            pools, _ui_selected, proof = scanner_pools_for_index(
                candles,
                "15m",
                idx,
                lld_cfg,
                cache=cache,
            )
            setup = entry_setup(pools, bar, moment)
            if setup is None:
                continue
            if blocked:
                blocked_signals += 1
                continue
            pool, upper, lower, gap = setup
            if pool["pool_id"] in seen_pools:
                continue
            h4_index = last_closed_index(market["4h"]["bars"], moment)
            if not down_4h(market["4h"]["bars"], h4_index):
                continue
            h4 = market["4h"]["bars"][h4_index]
            source_t = pool.get("source")
            birth = by_open.get(source_t, bar) if source_t is not None else bar
            rows.append(row_for(symbol, pool, birth, bar, h4, None, None, upper, lower, gap))
            seen_pools.add(pool["pool_id"])
        n_short = sum(1 for row in rows if row["symbol"] == symbol)
        extra = f" floor_blocked={blocked_signals}" if floor_block else ""
        print(symbol, "live_scanner shorts", n_short, extra, flush=True)

    elapsed = time.perf_counter() - t0
    if not rows:
        raise SystemExit("no causal rows")
    rows.sort(key=lambda row: (row["symbol"], row["entry_open"]))
    with OUT_CAUSAL_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1
    method: dict = {
        "script": Path(__file__).name,
        "mode": MODE_LIVE_SCANNER,
        "lookahead": False,
        "pane_from": pane_from.isoformat(),
        "pane_to": pane_to.isoformat(),
        "report_from": report_from.isoformat(),
        "report_to": report_to.isoformat(),
        "pool_analyzer": (
            "lld_research_kernel.scanner_pools_for_index: loaded pane candles + "
            "compute_lld_selected/select_lld_pools_for_chart (UI baseline), "
            "tip=last loaded bar; temporal filter at bar_close; then strategy filters"
        ),
        "short_filters": "UI-selected pools only; then known/break; upper bottom>close; attached(); gap/tp/4h",
        "counts": counts,
        "rows": len(rows),
        "elapsed_seconds": round(elapsed, 2),
    }
    if not args.no_floor_block:
        from short_block_4h_guard import guard_spec

        method["floor_block_4h"] = {
            "module": "short_block_4h_guard.py",
            "enabled": True,
            **guard_spec(),
        }
    else:
        method["floor_block_4h"] = {"enabled": False}
    OUT_CAUSAL_METHOD.write_text(json.dumps(method, indent=2) + "\n")
    print("causal rows", len(rows), counts, f"elapsed_s={elapsed:.1f}")


def run_pane_parity(args: argparse.Namespace) -> None:
    from dashboard.research_charts.lld_research_kernel import (
        MODE_PANE_PARITY,
        load_pane_candles,
        pane_parity_selected,
        parity_diff_vs_lld_objects,
    )

    if OUT_PANE_PARITY.exists() and not args.force:
        raise SystemExit(f"refuse overwrite {OUT_PANE_PARITY}")

    pane_from = datetime.fromisoformat(args.pane_from.replace("Z", "+00:00"))
    pane_to = datetime.fromisoformat(args.pane_to.replace("Z", "+00:00"))
    symbols = [args.symbol] if args.symbol else list(SYMBOLS)
    report = {"mode": MODE_PANE_PARITY, "lookahead": True, "symbols": {}}
    for symbol in symbols:
        _packed, candles = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(pane_from.timestamp()),
            to_unix=int(pane_to.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        pane = pane_parity_selected(candles, "15m")
        diff = parity_diff_vs_lld_objects(candles, "15m")
        pane_out = dict(pane)
        pane_out["selected_rows"] = [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
            for row in pane["selected_rows"]
        ]
        report["symbols"][symbol] = {
            "pane": pane_out,
            "parity_diff": diff,
        }
    OUT_PANE_PARITY.write_text(json.dumps(report, indent=2, default=_serialize_dt) + "\n")
    print("wrote", OUT_PANE_PARITY)


def _probe_pool_summary(rows: list[dict], close: float, decision: datetime) -> dict:
    from dashboard.research_charts.lld_research_kernel import find_pool_by_bottom, pool_known_at

    def upper_at(bottom: float) -> list[dict]:
        out = []
        for row in find_pool_by_bottom(rows, bottom):
            if row.get("side") != "upper":
                continue
            out.append({
                "pool_id": row["pool_id"],
                "bottom": row["bottom"],
                "top": row["top"],
                "known": row["known"].isoformat(),
                "break_at": row["break_at"].isoformat() if row["break_at"] else None,
                "known_at_decision": pool_known_at(row, decision),
                "bottom_gt_close": row["bottom"] > close,
            })
        return out

    return {
        "upper_1_2896": upper_at(1.2896),
        "upper_1_3072": upper_at(1.3072),
    }


def run_probe(args: argparse.Namespace) -> None:
    """Single-bar / single-pane acceptance probe (no full-symbol causal scan)."""
    from dashboard.research_charts.lld_research_kernel import (
        MODE_CAUSAL,
        MODE_HIST_REPLAY,
        causal_pools_for_index,
        find_pool_by_bottom,
        hist_replay_report,
        load_pane_candles,
        pool_known_at,
    )

    symbol = args.symbol or "XRPUSDT"
    entry_open = datetime.fromisoformat(args.entry_open.replace("Z", "+00:00"))
    pane_from = datetime.fromisoformat(args.pane_from.replace("Z", "+00:00"))
    pane_to = datetime.fromisoformat(args.pane_to.replace("Z", "+00:00"))
    t0 = time.perf_counter()
    report: dict = {
        "symbol": symbol,
        "entry_open": entry_open.isoformat(),
        "lld_mode": args.lld_mode,
        "probe": True,
    }

    if args.lld_mode == "hist-replay":
        from pool_scan.clock import bar_close

        decision = bar_close(entry_open, "15m")
        _packed, candles = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(pane_from.timestamp()),
            to_unix=int(pane_to.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        h = hist_replay_report(candles, "15m", decision)
        bar = next(
            c for c in candles
            if (c.timestamp if c.timestamp.tzinfo else c.timestamp.replace(tzinfo=timezone.utc)) == entry_open
        )
        close = float(bar.close)
        report.update({
            "mode": MODE_HIST_REPLAY,
            "lookahead": True,
            "decision_time": decision.isoformat(),
            "reference_ohlc": {
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": close,
            },
            "h1_selected_count": h["h1_selected_count"],
            "h2_available_count": h["h2_available_at_decision_count"],
            "h1_pools": _probe_pool_summary(h["h1_selected_rows"], close, decision),
            "h2_pools": _probe_pool_summary(h["h2_available_rows"], close, decision),
            "note": h["note"],
        })
    elif args.lld_mode == "causal":
        from dashboard.research_charts.lld_research_kernel import (
            MODE_LIVE_SCANNER,
            hist_replay_report,
            parity_diff_pool_rows,
            parity_scanner_vs_ui_at_index,
            scanner_pools_for_index,
            ui_lld_config,
        )

        _packed, candles = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(pane_from.timestamp()),
            to_unix=int(pane_to.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        bars15 = build_15m_bars(candles)
        bar = next(b for b in bars15 if b["open_time"] == entry_open)
        moment = bar["close_time"]
        idx = bar["candle_index"]
        lld_cfg = ui_lld_config("15m")
        pools, ui_selected, proof = scanner_pools_for_index(
            candles,
            "15m",
            idx,
            lld_cfg,
        )
        h = hist_replay_report(candles, "15m", moment, lld_cfg)
        parity_ui = parity_scanner_vs_ui_at_index(candles, "15m", idx, lld_cfg)
        parity_hist_h1 = parity_diff_pool_rows(h["h1_selected_rows"], ui_selected)
        setup = entry_setup(pools, bar, moment)
        report.update({
            "mode": MODE_LIVE_SCANNER,
            "lookahead": False,
            "decision_time": moment.isoformat(),
            "reference_ohlc": {
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
            },
            "scanner_proof": proof.as_dict(),
            "ui_parity_at_prefix": parity_ui,
            "hist_replay_full_pane_h1_vs_scanner_prefix": parity_hist_h1,
            "ui_selected_count": len(ui_selected),
            "pool_count_after_time_filter": len(pools),
            "ui_selected_pools": _probe_pool_summary(ui_selected, bar["close"], moment),
            "pools_after_time_filter": _probe_pool_summary(pools, bar["close"], moment),
            "entry_setup": None
            if setup is None
            else {
                "pool_id": setup[0]["pool_id"],
                "bottom": setup[0]["bottom"],
                "next_upper_bottom": setup[1]["bottom"],
                "gap_pct": round(setup[3], 4),
            },
        })
    else:
        raise SystemExit("--probe only supports hist-replay and causal")

    report["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
    OUT_PROBE.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", OUT_PROBE, "elapsed_s=", report["elapsed_seconds"])


def run_ui_parity(args: argparse.Namespace) -> None:
    """Document UI vs live-scanner pool IDs (before strategy filters) at one bar."""
    from dashboard.research_charts.lld_research_kernel import (
        MODE_HIST_REPLAY,
        MODE_LIVE_SCANNER,
        MODE_PANE_PARITY,
        candle_stamps,
        hist_replay_report,
        load_pane_candles,
        pane_parity_selected,
        parity_diff_pool_rows,
        parity_scanner_vs_ui_at_index,
        scanner_pools_for_index,
        ui_lld_config,
        ui_selected_rows_at_index,
    )
    from pool_scan.clock import bar_close

    symbol = args.symbol or "XRPUSDT"
    entry_open = datetime.fromisoformat(args.entry_open.replace("Z", "+00:00"))
    pane_from = datetime.fromisoformat(args.pane_from.replace("Z", "+00:00"))
    pane_to = datetime.fromisoformat(args.pane_to.replace("Z", "+00:00"))
    decision = bar_close(entry_open, "15m")
    _packed, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(pane_from.timestamp()),
        to_unix=int(pane_to.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    stamps = candle_stamps(candles)
    idx = stamps.index(entry_open)
    lld_cfg = ui_lld_config("15m")
    ui_selected, tip_ts, _ = ui_selected_rows_at_index(candles, "15m", idx, lld_cfg)
    _avail, _ui2, proof = scanner_pools_for_index(candles, "15m", idx, lld_cfg)
    pane_full = pane_parity_selected(candles, "15m", lld_cfg)
    hist = hist_replay_report(candles, "15m", decision, lld_cfg)
    parity_prefix = parity_scanner_vs_ui_at_index(candles, "15m", idx, lld_cfg)
    parity_vs_full_pane = parity_diff_pool_rows(pane_full["selected_rows"], ui_selected)

    def row_list(rows: list[dict]) -> list[dict]:
        return [
            {
                "pool_id": r["pool_id"],
                "side": r["side"],
                "bottom": r["bottom"],
                "top": r["top"],
            }
            for r in sorted(rows, key=lambda x: (x["side"], x["bottom"], x["pool_id"]))
        ]

    report = {
        "symbol": symbol,
        "entry_open": entry_open.isoformat(),
        "decision_time": decision.isoformat(),
        "pane_from": pane_from.isoformat(),
        "pane_to": pane_to.isoformat(),
        "modes": {
            "pane_full_lookahead": MODE_PANE_PARITY,
            "live_scanner_prefix": MODE_LIVE_SCANNER,
            "hist_replay_reference": MODE_HIST_REPLAY,
        },
        "scanner_proof": proof.as_dict(),
        "tip_at_prefix": tip_ts.isoformat(),
        "parity_scanner_vs_ui_lld_chain": parity_prefix,
        "parity_prefix_vs_full_pane_h1": parity_vs_full_pane,
        "hist_replay_h1_vs_prefix": parity_diff_pool_rows(hist["h1_selected_rows"], ui_selected),
        "ui_selected_rows_at_prefix": row_list(ui_selected),
        "full_pane_h1_rows": row_list(pane_full["selected_rows"]),
        "note": (
            "Live scanner pool universe = ui_selected_rows_at_prefix (empty parity vs "
            "parity_diff_vs_lld_objects). hist-replay H1 uses full custom pane (lookahead); "
            "strategy filters (bottom>close) apply only after this report."
        ),
    }
    OUT_XRP_UI_PARITY.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", OUT_XRP_UI_PARITY, "ids_match=", parity_prefix.get("ids_match"))


def run_hist_replay(args: argparse.Namespace) -> None:
    from dashboard.research_charts.lld_research_kernel import (
        MODE_HIST_REPLAY,
        hist_replay_report,
        load_pane_candles,
    )
    from pool_scan.clock import bar_close

    if OUT_HIST_REPLAY.exists() and not args.force:
        raise SystemExit(f"refuse overwrite {OUT_HIST_REPLAY}")

    pane_from = datetime.fromisoformat(args.pane_from.replace("Z", "+00:00"))
    pane_to = datetime.fromisoformat(args.pane_to.replace("Z", "+00:00"))
    entry_open = datetime.fromisoformat(args.entry_open.replace("Z", "+00:00"))
    decision = bar_close(entry_open, "15m")
    symbols = [args.symbol] if args.symbol else list(SYMBOLS)
    report = {
        "mode": MODE_HIST_REPLAY,
        "lookahead": True,
        "entry_open": entry_open.isoformat(),
        "decision_time": decision.isoformat(),
        "symbols": {},
    }
    for symbol in symbols:
        _packed, candles = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(pane_from.timestamp()),
            to_unix=int(pane_to.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        report["symbols"][symbol] = hist_replay_report(candles, "15m", decision)
    OUT_HIST_REPLAY.write_text(json.dumps(report, indent=2, default=_serialize_dt) + "\n")
    print("wrote", OUT_HIST_REPLAY)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="15m short entry scanner (LLD modes P/H/C)")
    parser.add_argument(
        "--lld-mode",
        required=True,
        choices=("pane-parity", "hist-replay", "causal", "ui-parity"),
        help="ui-parity: XRP UI vs scanner IDs; pane/hist: reports; causal: live scanner CSV",
    )
    parser.add_argument("--force", action="store_true", help="overwrite output files")
    parser.add_argument(
        "--no-floor-block",
        action="store_true",
        help="disable 4h floor short-block guard (causal mode)",
    )
    parser.add_argument("--symbol", default="", help="single symbol for P/H reports")
    parser.add_argument("--pane-from", default=PANE_FROM_DEFAULT.isoformat())
    parser.add_argument("--pane-to", default=PANE_TO_DEFAULT.isoformat())
    parser.add_argument("--report-from", default=REPORT_FROM.isoformat())
    parser.add_argument("--report-to", default=REPORT_TO_DEFAULT.isoformat())
    parser.add_argument(
        "--entry-open",
        default="2026-06-01T16:30:00+00:00",
        help="reference bucket open for hist-replay",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="single entry-open probe only (hist-replay or causal); no full scan",
    )
    return parser.parse_args()


def main() -> None:
    _ensure_paths()
    args = parse_args()
    if args.probe:
        if args.lld_mode not in ("hist-replay", "causal"):
            raise SystemExit("--probe requires --lld-mode hist-replay or causal")
        run_probe(args)
        return
    if args.lld_mode == "causal":
        run_causal(args)
    elif args.lld_mode == "pane-parity":
        run_pane_parity(args)
    elif args.lld_mode == "hist-replay":
        run_hist_replay(args)
    elif args.lld_mode == "ui-parity":
        run_ui_parity(args)
    else:
        raise SystemExit(f"unknown mode {args.lld_mode}")


if __name__ == "__main__":
    main()
