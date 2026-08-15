"""Read-only Entry MAE/MFE audit for continuous hedge-bot backtests.

Does not modify strategy, entries, exits, sizes, or fees.
Analyzes market price path after each historical trade start_time.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.simulated_execution import DEFAULT_SIMULATED_FEE_RATE

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCES = (
    ROOT
    / "research/backtests/results/full_history_continuous_long_recovery"
    / "APTUSDT_original_hedge_5m_continuous_results.json",
    ROOT
    / "research/backtests/results/apt_continuous"
    / "APTUSDT_original_hedge_5m_continuous_results.json",
)
DEFAULT_OUT = ROOT / "research/backtests/results/hedge_entry_mae_mfe_audit"

SUCCESS_EXIT_QUALITY = frozenset(
    {"closed_ok", "closed_profitable_with_cycle_undercoverage"}
)
WINDOWS_MIN = (15, 30, 60, 120, 240, 480, 720, 1440)
TARGET_PCTS = (0.25, 0.50, 1.00, 1.50, 2.00, 3.00, 5.00)
SYMMETRIC_PAIRS = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 5.00)
ASYMMETRIC = (
    (0.50, 1.00),
    (1.00, 0.50),
    (1.00, 1.00),
    (1.00, 1.50),
    (1.00, 3.00),
    (1.50, 1.00),
    (1.50, 3.00),
    (2.00, 1.00),
    (2.00, 1.50),
    (2.00, 3.00),
    (2.00, 4.00),
    (3.00, 1.50),
    (3.00, 2.00),
)
CAPITAL_USDT = 1000.0
LONG_NOTIONAL = 100.0
SHORT_NOTIONAL = 50.0
LEVERAGE = 15.0
FEE_RATE = DEFAULT_SIMULATED_FEE_RATE  # 0.00055


def _parse_ts(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        # pandas.Timestamp / numpy datetime64 via string fallback
        to_pydatetime = getattr(value, "to_pydatetime", None)
        if callable(to_pydatetime):
            ts = to_pydatetime()
        else:
            text = str(value).replace("Z", "+00:00")
            ts = datetime.fromisoformat(text)
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def _pctile(xs: Sequence[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ys[int(k)]
    return ys[f] * (c - k) + ys[c] * (k - f)


def _dist_summary(xs: Sequence[float]) -> dict[str, Any]:
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": min(xs),
        "max": max(xs),
        "mean": statistics.mean(xs),
        "median": statistics.median(xs),
        "stdev": statistics.stdev(xs) if len(xs) > 1 else 0.0,
        "p10": _pctile(xs, 10),
        "p25": _pctile(xs, 25),
        "p50": _pctile(xs, 50),
        "p75": _pctile(xs, 75),
        "p80": _pctile(xs, 80),
        "p90": _pctile(xs, 90),
        "p95": _pctile(xs, 95),
        "p99": _pctile(xs, 99),
    }


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def is_success(exit_quality: str | None) -> bool:
    return str(exit_quality or "") in SUCCESS_EXIT_QUALITY


def is_blocker(exit_quality: str | None, final_status: str | None) -> bool:
    eq = str(exit_quality or "")
    st = str(final_status or "")
    if eq in {"open", "max_candles", "error"} or st in {"open", "max_candles", "error"}:
        return True
    if eq in {"closed_negative_pnl", "closed_undercovered_final_exit"}:
        return False  # closed unsuccessful, not necessarily chain blocker
    return False


@dataclass
class TradeStart:
    trade_id: str
    symbol: str
    direction: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None
    final_status: str
    exit_reason: str
    exit_quality: str
    trade_success: bool
    is_blocker: bool
    corpus: str
    base_notional_usdt: float
    hedge_ratio_short: float
    realized_pnl: float | None
    start_index: int | None


def load_runs(paths: Sequence[Path]) -> list[TradeStart]:
    out: list[TradeStart] = []
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        runs = payload.get("runs") if isinstance(payload, dict) else payload
        corpus = path.parent.name
        for i, r in enumerate(runs or [], start=1):
            entry_ts = _parse_ts(r.get("start_time"))
            entry_price = r.get("entry_price")
            if entry_ts is None or entry_price is None:
                continue
            eq = str(r.get("exit_quality") or "")
            st = str(r.get("final_status") or "")
            out.append(
                TradeStart(
                    trade_id=f"{corpus}_{r.get('symbol','UNK')}_{r.get('direction','')}_{i:04d}",
                    symbol=str(r.get("symbol") or "UNK"),
                    direction=str(r.get("direction") or ""),
                    entry_ts=entry_ts,
                    entry_price=float(entry_price),
                    exit_ts=_parse_ts(r.get("end_time")),
                    final_status=st,
                    exit_reason=str(r.get("exit_reason") or ""),
                    exit_quality=eq,
                    trade_success=is_success(eq),
                    is_blocker=is_blocker(eq, st),
                    corpus=corpus,
                    base_notional_usdt=float(r.get("base_notional_usdt") or 0),
                    hedge_ratio_short=float(r.get("hedge_ratio_short") or 0),
                    realized_pnl=None if r.get("realized_pnl") is None else float(r["realized_pnl"]),
                    start_index=None if r.get("start_index") is None else int(r["start_index"]),
                )
            )
    return out


def align_entry_index(candles: list[dict[str, Any]], entry_ts: datetime) -> int | None:
    """Index of candle whose open timestamp equals entry_ts (bot uses that bar's close as entry)."""
    target = _parse_ts(entry_ts)
    assert target is not None
    for i, c in enumerate(candles):
        ts = _parse_ts(c["timestamp"])
        if ts == target:
            return i
    # nearest at-or-before
    best = None
    for i, c in enumerate(candles):
        ts = _parse_ts(c["timestamp"])
        assert ts is not None
        if ts <= target:
            best = i
        else:
            break
    return best


def excursion_path(
    candles: list[dict[str, Any]],
    *,
    entry_idx: int,
    entry_price: float,
    until_idx: int | None = None,
) -> dict[str, Any]:
    """MAE/MFE using only candles AFTER the entry candle (no pre-entry intracandle highs/lows).

    Entry is assumed at the close of candles[entry_idx]. Subsequent bars start at entry_idx+1.
    """
    end = len(candles) - 1 if until_idx is None else min(until_idx, len(candles) - 1)
    start = entry_idx + 1
    if start > end:
        return {
            "mae_pct": 0.0,
            "mfe_pct": 0.0,
            "mae_ts": None,
            "mfe_ts": None,
            "minutes_to_mae": None,
            "minutes_to_mfe": None,
            "mae_before_mfe": None,
            "min_price": entry_price,
            "max_price": entry_price,
            "bars_used": 0,
            "data_complete": False,
            "note": "no_bars_after_entry",
        }
    entry_ts = candles[entry_idx]["timestamp"]
    if not isinstance(entry_ts, datetime):
        entry_ts = _parse_ts(entry_ts)
    assert entry_ts is not None

    min_px = entry_price
    max_px = entry_price
    mae_ts = entry_ts
    mfe_ts = entry_ts
    mae_first = None
    for j in range(start, end + 1):
        hi = float(candles[j]["high"])
        lo = float(candles[j]["low"])
        ts = candles[j]["timestamp"]
        if not isinstance(ts, datetime):
            ts = _parse_ts(ts)
        assert ts is not None
        if lo < min_px:
            min_px = lo
            mae_ts = ts
            if mae_first is None and max_px == entry_price:
                mae_first = True
        if hi > max_px:
            max_px = hi
            mfe_ts = ts
            if mae_first is None and min_px == entry_price:
                mae_first = False
        # order within path: first time either extreme moves
        if mae_first is None:
            if lo < entry_price and hi > entry_price:
                mae_first = None  # ambiguous same bar first move both ways
            elif lo < entry_price:
                mae_first = True
            elif hi > entry_price:
                mae_first = False

    mae_pct = (min_px / entry_price - 1.0) * 100.0
    mfe_pct = (max_px / entry_price - 1.0) * 100.0
    # refine mae_before_mfe by scanning chronologically for first -eps vs +eps from entry
    first_adverse = first_favor = None
    for j in range(start, end + 1):
        hi = float(candles[j]["high"])
        lo = float(candles[j]["low"])
        ts = candles[j]["timestamp"]
        if not isinstance(ts, datetime):
            ts = _parse_ts(ts)
        if first_adverse is None and lo < entry_price:
            first_adverse = ts
        if first_favor is None and hi > entry_price:
            first_favor = ts
        if first_adverse and first_favor:
            break
    if first_adverse and first_favor:
        mae_before_mfe = first_adverse <= first_favor
    elif first_adverse and not first_favor:
        mae_before_mfe = True
    elif first_favor and not first_adverse:
        mae_before_mfe = False
    else:
        mae_before_mfe = None

    def mins(ts):
        if ts is None or entry_ts is None:
            return None
        return (ts - entry_ts).total_seconds() / 60.0

    return {
        "mae_pct": mae_pct,
        "mfe_pct": mfe_pct,
        "mae_ts": mae_ts,
        "mfe_ts": mfe_ts,
        "minutes_to_mae": mins(mae_ts),
        "minutes_to_mfe": mins(mfe_ts),
        "mae_before_mfe": mae_before_mfe,
        "min_price": min_px,
        "max_price": max_px,
        "bars_used": end - start + 1,
        "data_complete": end >= start,
        "note": "excludes_entry_candle_ohlc_range",
    }


def mae_before_target(
    candles: list[dict[str, Any]],
    *,
    entry_idx: int,
    entry_price: float,
    target_pct: float,
    until_idx: int | None = None,
) -> dict[str, Any]:
    """Min drawdown before first reaching +target_pct (long-oriented)."""
    end = len(candles) - 1 if until_idx is None else min(until_idx, len(candles) - 1)
    start = entry_idx + 1
    target_px = entry_price * (1.0 + target_pct / 100.0)
    entry_ts = candles[entry_idx]["timestamp"]
    if not isinstance(entry_ts, datetime):
        entry_ts = _parse_ts(entry_ts)
    min_px = entry_price
    for j in range(start, end + 1):
        hi = float(candles[j]["high"])
        lo = float(candles[j]["low"])
        ts = candles[j]["timestamp"]
        if not isinstance(ts, datetime):
            ts = _parse_ts(ts)
        # update MAE running before checking target on same bar: conservative —
        # if same bar hits both, count MAE including that bar's low then target reached
        if lo < min_px:
            min_px = lo
        if hi >= target_px:
            return {
                "target_pct": target_pct,
                "target_reached": True,
                "target_ts": ts,
                "mae_before_target_pct": (min_px / entry_price - 1.0) * 100.0,
                "minutes_to_target": None
                if entry_ts is None or ts is None
                else (ts - entry_ts).total_seconds() / 60.0,
            }
    return {
        "target_pct": target_pct,
        "target_reached": False,
        "target_ts": None,
        "mae_before_target_pct": (min_px / entry_price - 1.0) * 100.0,
        "minutes_to_target": None,
        "status": "NOT_REACHED",
    }


def first_touch(
    candles: list[dict[str, Any]],
    *,
    entry_idx: int,
    entry_price: float,
    tp_pct: float,
    sl_pct: float,
    until_idx: int | None = None,
) -> dict[str, Any]:
    """Long-oriented: TP = +tp_pct, SL = -sl_pct. Same-candle → AMBIGUOUS + conservative SL."""
    end = len(candles) - 1 if until_idx is None else min(until_idx, len(candles) - 1)
    start = entry_idx + 1
    tp_px = entry_price * (1.0 + tp_pct / 100.0)
    sl_px = entry_price * (1.0 - sl_pct / 100.0)
    entry_ts = candles[entry_idx]["timestamp"]
    if not isinstance(entry_ts, datetime):
        entry_ts = _parse_ts(entry_ts)
    for j in range(start, end + 1):
        hi = float(candles[j]["high"])
        lo = float(candles[j]["low"])
        ts = candles[j]["timestamp"]
        if not isinstance(ts, datetime):
            ts = _parse_ts(ts)
        hit_tp = hi >= tp_px
        hit_sl = lo <= sl_px
        mins = None if entry_ts is None or ts is None else (ts - entry_ts).total_seconds() / 60.0
        if hit_tp and hit_sl:
            return {
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "first_touch": "AMBIGUOUS_SAME_CANDLE",
                "first_touch_ts": ts,
                "minutes_to_first_touch": mins,
                "same_candle_ambiguous": True,
                "conservative_result": "SL",
            }
        if hit_sl:
            return {
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "first_touch": "SL",
                "first_touch_ts": ts,
                "minutes_to_first_touch": mins,
                "same_candle_ambiguous": False,
                "conservative_result": "SL",
            }
        if hit_tp:
            return {
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "first_touch": "TP",
                "first_touch_ts": ts,
                "minutes_to_first_touch": mins,
                "same_candle_ambiguous": False,
                "conservative_result": "TP",
            }
    return {
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "first_touch": "NONE",
        "first_touch_ts": None,
        "minutes_to_first_touch": None,
        "same_candle_ambiguous": False,
        "conservative_result": "NONE",
    }


def hedge_2to1_pnl(r_pct: float) -> dict[str, float]:
    """r_pct as percent move, e.g. -1.5 for -1.5%."""
    r = r_pct / 100.0
    long_pnl = LONG_NOTIONAL * r
    short_pnl = -SHORT_NOTIONAL * r
    net = long_pnl + short_pnl  # = 50 * r
    return {
        "price_move_pct": r_pct,
        "long_pnl_usdt": long_pnl,
        "short_pnl_usdt": short_pnl,
        "net_pnl_usdt": net,
        "net_pct_of_capital": net / CAPITAL_USDT * 100.0,
    }


def leverage_context() -> dict[str, Any]:
    long_margin = LONG_NOTIONAL / LEVERAGE
    short_margin = SHORT_NOTIONAL / LEVERAGE
    gross = LONG_NOTIONAL + SHORT_NOTIONAL
    net = abs(LONG_NOTIONAL - SHORT_NOTIONAL)
    return {
        "capital_usdt": CAPITAL_USDT,
        "long_notional_usdt": LONG_NOTIONAL,
        "short_notional_usdt": SHORT_NOTIONAL,
        "leverage_setting": LEVERAGE,
        "initial_margin_long_usdt": long_margin,
        "initial_margin_short_usdt": short_margin,
        "initial_margin_total_usdt": long_margin + short_margin,
        "gross_exposure_usdt": gross,
        "net_exposure_usdt": net,
        "gross_exposure_over_capital": gross / CAPITAL_USDT,
        "net_exposure_over_capital": net / CAPITAL_USDT,
        "note": (
            "Bybit liquidation depends on maintenance margin tier; no project SoT liquidations "
            "function used. Context is initial margin / exposure only — not a liquidation price."
        ),
    }


def run_audit(
    *,
    source_jsons: Sequence[Path] | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    sources = list(source_jsons or DEFAULT_SOURCES)
    output_dir = Path(output_dir or DEFAULT_OUT)
    output_dir.mkdir(parents=True, exist_ok=True)

    trades = load_runs(sources)
    if not trades:
        raise RuntimeError(f"no trades loaded from {sources}")

    # load candles per symbol once
    candle_cache: dict[str, list[dict[str, Any]]] = {}
    for sym in sorted({t.symbol for t in trades}):
        # Raw OHLC dicts; do not mutate strategy/simulator paths.
        candle_cache[sym] = load_candles_for_symbol(sym, timeframe="5m")

    trade_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    mae_target_rows: list[dict[str, Any]] = []
    first_touch_rows: list[dict[str, Any]] = []
    hedge_rows: list[dict[str, Any]] = []

    for t in trades:
        candles = candle_cache[t.symbol]
        entry_idx = align_entry_index(candles, t.entry_ts)
        if entry_idx is None:
            trade_rows.append(
                {
                    "trade_id": t.trade_id,
                    "symbol": t.symbol,
                    "entry_ts": _iso(t.entry_ts),
                    "error": "entry_candle_not_found",
                }
            )
            continue
        # verify entry price ~ close
        close_px = float(candles[entry_idx]["close"])
        exit_idx = None
        if t.exit_ts is not None:
            exit_idx = align_entry_index(candles, t.exit_ts)
        overall = excursion_path(
            candles, entry_idx=entry_idx, entry_price=t.entry_price, until_idx=exit_idx
        )
        to_end = excursion_path(
            candles, entry_idx=entry_idx, entry_price=t.entry_price, until_idx=None
        )
        trade_rows.append(
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "direction": t.direction,
                "corpus": t.corpus,
                "entry_ts": _iso(t.entry_ts),
                "entry_price": t.entry_price,
                "entry_candle_close": close_px,
                "entry_price_matches_close": abs(close_px - t.entry_price) <= max(1e-9, abs(t.entry_price) * 1e-8),
                "exit_ts": _iso(t.exit_ts),
                "final_status": t.final_status,
                "exit_reason": t.exit_reason,
                "exit_quality": t.exit_quality,
                "trade_success": t.trade_success,
                "is_blocker": t.is_blocker,
                "realized_pnl": t.realized_pnl,
                "overall_mae_pct": overall["mae_pct"],
                "overall_mfe_pct": overall["mfe_pct"],
                "mae_ts": _iso(overall["mae_ts"]),
                "mfe_ts": _iso(overall["mfe_ts"]),
                "minutes_to_mae": overall["minutes_to_mae"],
                "minutes_to_mfe": overall["minutes_to_mfe"],
                "mae_before_mfe": overall["mae_before_mfe"],
                "to_data_end_mae_pct": to_end["mae_pct"],
                "to_data_end_mfe_pct": to_end["mfe_pct"],
                "analysis_note": overall["note"],
            }
        )

        entry_ts = t.entry_ts
        for wmin in WINDOWS_MIN:
            # window ends at entry + wmin; map to candle index by timestamp
            end_ts = entry_ts + timedelta(minutes=wmin)
            until = entry_idx
            complete = True
            for j in range(entry_idx, len(candles)):
                ts = candles[j]["timestamp"]
                if not isinstance(ts, datetime):
                    ts = _parse_ts(ts)
                assert ts is not None
                if ts <= end_ts:
                    until = j
                else:
                    break
            else:
                # reached end of series before window
                if candles[-1]["timestamp"] if isinstance(candles[-1]["timestamp"], datetime) else _parse_ts(candles[-1]["timestamp"]) < end_ts:
                    complete = False
            # check completeness
            last_ts = candles[until]["timestamp"]
            if not isinstance(last_ts, datetime):
                last_ts = _parse_ts(last_ts)
            if last_ts is None or last_ts < end_ts - timedelta(minutes=5):
                complete = False
            ex = excursion_path(
                candles, entry_idx=entry_idx, entry_price=t.entry_price, until_idx=until
            )
            window_rows.append(
                {
                    "trade_id": t.trade_id,
                    "symbol": t.symbol,
                    "window": f"{wmin}m",
                    "window_end_ts": _iso(end_ts),
                    "mae_pct": ex["mae_pct"],
                    "mfe_pct": ex["mfe_pct"],
                    "mae_ts": _iso(ex["mae_ts"]),
                    "mfe_ts": _iso(ex["mfe_ts"]),
                    "minutes_to_mae": ex["minutes_to_mae"],
                    "minutes_to_mfe": ex["minutes_to_mfe"],
                    "data_complete": complete,
                }
            )

        # until exit window already in overall; also explicit exit window row
        if exit_idx is not None:
            ex = overall
            window_rows.append(
                {
                    "trade_id": t.trade_id,
                    "symbol": t.symbol,
                    "window": "until_exit",
                    "window_end_ts": _iso(t.exit_ts),
                    "mae_pct": ex["mae_pct"],
                    "mfe_pct": ex["mfe_pct"],
                    "mae_ts": _iso(ex["mae_ts"]),
                    "mfe_ts": _iso(ex["mfe_ts"]),
                    "minutes_to_mae": ex["minutes_to_mae"],
                    "minutes_to_mfe": ex["minutes_to_mfe"],
                    "data_complete": True,
                }
            )
        window_rows.append(
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "window": "to_data_end",
                "window_end_ts": _iso(
                    candles[-1]["timestamp"]
                    if isinstance(candles[-1]["timestamp"], datetime)
                    else _parse_ts(candles[-1]["timestamp"])
                ),
                "mae_pct": to_end["mae_pct"],
                "mfe_pct": to_end["mfe_pct"],
                "mae_ts": _iso(to_end["mae_ts"]),
                "mfe_ts": _iso(to_end["mfe_ts"]),
                "minutes_to_mae": to_end["minutes_to_mae"],
                "minutes_to_mfe": to_end["minutes_to_mfe"],
                "data_complete": True,
            }
        )

        # MAE before targets — for successful trades primarily, but compute all
        for tgt in TARGET_PCTS:
            row = mae_before_target(
                candles,
                entry_idx=entry_idx,
                entry_price=t.entry_price,
                target_pct=tgt,
                until_idx=exit_idx,
            )
            mae_target_rows.append(
                {
                    "trade_id": t.trade_id,
                    "symbol": t.symbol,
                    "trade_success": t.trade_success,
                    **row,
                    "target_reached": row["target_reached"],
                    "target_ts": _iso(row.get("target_ts")),
                }
            )

        # first touch pairs
        pairs = [(p, p) for p in SYMMETRIC_PAIRS] + list(ASYMMETRIC)
        for tp, sl in pairs:
            ft = first_touch(
                candles,
                entry_idx=entry_idx,
                entry_price=t.entry_price,
                tp_pct=tp,
                sl_pct=sl,
                until_idx=exit_idx,
            )
            first_touch_rows.append(
                {
                    "trade_id": t.trade_id,
                    "symbol": t.symbol,
                    "trade_success": t.trade_success,
                    "tp_pct": tp,
                    "sl_pct": sl,
                    "first_touch": ft["first_touch"],
                    "first_touch_ts": _iso(ft["first_touch_ts"]),
                    "minutes_to_first_touch": ft["minutes_to_first_touch"],
                    "same_candle_ambiguous": ft["same_candle_ambiguous"],
                    "conservative_result": ft["conservative_result"],
                }
            )

        # simplified 2:1 hedge pnl at overall MAE/MFE
        h_mae = hedge_2to1_pnl(overall["mae_pct"])
        h_mfe = hedge_2to1_pnl(overall["mfe_pct"])
        hedge_rows.append(
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "trade_success": t.trade_success,
                "overall_mae_pct": overall["mae_pct"],
                "overall_mfe_pct": overall["mfe_pct"],
                "theo_max_net_loss_usdt": h_mae["net_pnl_usdt"],
                "theo_max_net_gain_usdt": h_mfe["net_pnl_usdt"],
                "theo_max_net_loss_pct_capital": h_mae["net_pct_of_capital"],
                "theo_max_net_gain_pct_capital": h_mfe["net_pct_of_capital"],
                "note": "constant_2to1_hold_no_fees_no_refill",
            }
        )

    write_csv(output_dir / "trade_mae_mfe.csv", trade_rows)
    write_csv(output_dir / "trade_window_mae_mfe.csv", window_rows)
    write_csv(output_dir / "mae_before_target.csv", mae_target_rows)
    write_csv(output_dir / "first_touch_results.csv", first_touch_rows)
    write_csv(output_dir / "hedge_pnl_2to1.csv", hedge_rows)

    # first touch summary
    ft_summary = []
    by_pair: dict[tuple[float, float], list[dict]] = {}
    for r in first_touch_rows:
        by_pair.setdefault((float(r["tp_pct"]), float(r["sl_pct"])), []).append(r)
    for (tp, sl), rows in sorted(by_pair.items()):
        n = len(rows)
        tp_n = sum(1 for r in rows if r["first_touch"] == "TP")
        sl_n = sum(1 for r in rows if r["first_touch"] == "SL")
        amb = sum(1 for r in rows if r["first_touch"] == "AMBIGUOUS_SAME_CANDLE")
        none_n = sum(1 for r in rows if r["first_touch"] == "NONE")
        cons_sl = sum(1 for r in rows if r["conservative_result"] == "SL")
        cons_tp = sum(1 for r in rows if r["conservative_result"] == "TP")
        # R multiple: win +tp/sl, loss -1; fees approx 2*fee*notional net rough
        # theoretical R without fees: TP first => +tp/sl R, SL => -1R
        decided = tp_n + sl_n
        avg_r = None
        if decided:
            avg_r = (tp_n * (tp / sl) + sl_n * (-1.0)) / decided
        # with ambiguous counted as SL (conservative)
        cons_decided = cons_tp + cons_sl
        avg_r_cons = None
        if cons_decided:
            avg_r_cons = (cons_tp * (tp / sl) + cons_sl * (-1.0)) / cons_decided
        be = sl / (tp + sl) if (tp + sl) > 0 else None
        # fee estimate on roundtrip notional gross exposure / capital — exploratory
        fee_cost_usdt = (LONG_NOTIONAL + SHORT_NOTIONAL) * FEE_RATE * 2  # open+close both legs rough
        mins = [r["minutes_to_first_touch"] for r in rows if r["minutes_to_first_touch"] is not None]
        ft_summary.append(
            {
                "tp_pct": tp,
                "sl_pct": sl,
                "n_trades": n,
                "tp_first": tp_n,
                "sl_first": sl_n,
                "ambiguous_same_candle": amb,
                "none_reached": none_n,
                "tp_first_rate": tp_n / n if n else None,
                "sl_first_rate": sl_n / n if n else None,
                "conservative_tp": cons_tp,
                "conservative_sl": cons_sl,
                "avg_hold_minutes_to_touch": None if not mins else statistics.mean(mins),
                "median_hold_minutes_to_touch": None if not mins else statistics.median(mins),
                "theo_avg_R_ignore_ambiguous": avg_r,
                "theo_avg_R_conservative_sl_on_ambiguous": avg_r_cons,
                "breakeven_hit_rate": be,
                "approx_roundtrip_fee_usdt_2legs": fee_cost_usdt,
                "fee_rate": FEE_RATE,
            }
        )
    write_csv(output_dir / "first_touch_summary.csv", ft_summary)

    def collect(rows, pred, key):
        return [float(r[key]) for r in rows if pred(r) and r.get(key) is not None]

    ok_rows = [r for r in trade_rows if r.get("trade_success") is True]
    all_valid = [r for r in trade_rows if r.get("overall_mae_pct") is not None]
    blocker_rows = [r for r in trade_rows if r.get("is_blocker") is True]
    fail_closed = [
        r
        for r in trade_rows
        if r.get("trade_success") is False and not r.get("is_blocker") and r.get("overall_mae_pct") is not None
    ]

    dist_rows = []
    for label, subset in (
        ("all", all_valid),
        ("successful", ok_rows),
        ("blocker", blocker_rows),
        ("unsuccessful_closed", fail_closed),
    ):
        for metric in ("overall_mae_pct", "overall_mfe_pct"):
            xs = [float(r[metric]) for r in subset if r.get(metric) is not None]
            dist_rows.append({"group": label, "metric": metric, **_dist_summary(xs)})
    write_csv(output_dir / "distribution_summary.csv", dist_rows)

    # symbol summary
    sym_rows = []
    for sym in sorted({r["symbol"] for r in all_valid}):
        sub = [r for r in all_valid if r["symbol"] == sym]
        sok = [r for r in sub if r.get("trade_success")]
        sym_rows.append(
            {
                "symbol": sym,
                "n": len(sub),
                "n_success": len(sok),
                "median_mae_all": _pctile([float(r["overall_mae_pct"]) for r in sub], 50),
                "median_mae_success": _pctile([float(r["overall_mae_pct"]) for r in sok], 50),
                "p90_mae_success": _pctile([float(r["overall_mae_pct"]) for r in sok], 90),
                "median_mfe_all": _pctile([float(r["overall_mfe_pct"]) for r in sub], 50),
                "median_mfe_success": _pctile([float(r["overall_mfe_pct"]) for r in sok], 50),
            }
        )
    write_csv(output_dir / "symbol_summary.csv", sym_rows)

    # successful trade MAE highlight
    succ_mae = [float(r["overall_mae_pct"]) for r in ok_rows]
    succ_mfe = [float(r["overall_mfe_pct"]) for r in ok_rows]
    succ_summary = {
        "n_successful": len(ok_rows),
        "median_mae": _pctile(succ_mae, 50),
        "p75_mae": _pctile(succ_mae, 75),
        "p90_mae": _pctile(succ_mae, 90),
        "p95_mae": _pctile(succ_mae, 95),
        "median_mfe": _pctile(succ_mfe, 50),
        "p75_mfe": _pctile(succ_mfe, 75),
        "p90_mfe": _pctile(succ_mfe, 90),
        "p95_mfe": _pctile(succ_mfe, 95),
        "interpretation_p90": (
            None
            if not succ_mae
            else f"90% of successful trades had MAE no worse than {_pctile(succ_mae, 10):.3f}% "
            f"(i.e. drawdown magnitude <= {abs(_pctile(succ_mae, 10) or 0):.3f}% for the best 90% by MAE; "
            f"equivalently p90 of MAE values={_pctile(succ_mae, 90):.3f}% which is more negative)"
        ),
        "note_mae_sign": "MAE is negative; more negative = larger adverse move. p90 of MAE is near zero if most are mild; use abs for 'how far against'.",
    }
    # clearer: distribute absolute adverse
    abs_mae = [abs(x) for x in succ_mae]
    succ_summary.update(
        {
            "median_abs_mae": _pctile(abs_mae, 50),
            "p75_abs_mae": _pctile(abs_mae, 75),
            "p90_abs_mae": _pctile(abs_mae, 90),
            "p95_abs_mae": _pctile(abs_mae, 95),
            "statement_p90_abs": (
                None
                if not abs_mae
                else f"90% of successful trades ran at most {_pctile(abs_mae, 90):.3f}% against entry (abs MAE)."
            ),
        }
    )
    write_csv(output_dir / "successful_trade_mae_summary.csv", [succ_summary])

    # adverse depth counts
    lev = leverage_context()
    adverse_counts = {}
    for thr in (3.0, 5.0, 7.5, 10.0):
        n_all = sum(1 for r in all_valid if abs(float(r["overall_mae_pct"])) > thr)
        n_ok = sum(1 for r in ok_rows if abs(float(r["overall_mae_pct"])) > thr)
        adverse_counts[f"abs_mae_gt_{thr}"] = {"all": n_all, "successful": n_ok}
    lev["adverse_excursion_counts"] = adverse_counts
    lev["n_trades"] = len(all_valid)
    lev["n_successful"] = len(ok_rows)
    write_csv(output_dir / "leverage_15x_context.csv", [lev])

    # pick best first-touch combos by conservative avg R among those with enough touches
    ranked = sorted(
        [r for r in ft_summary if (r.get("tp_first") or 0) + (r.get("sl_first") or 0) + (r.get("ambiguous_same_candle") or 0) >= max(10, len(all_valid) // 5)],
        key=lambda r: (r.get("theo_avg_R_conservative_sl_on_ambiguous") is not None, r.get("theo_avg_R_conservative_sl_on_ambiguous") or -999),
        reverse=True,
    )
    top_combos = ranked[:5]

    all_mae = [float(r["overall_mae_pct"]) for r in all_valid]
    all_mfe = [float(r["overall_mfe_pct"]) for r in all_valid]
    abs_all = [abs(x) for x in all_mae]
    p90_abs_succ = succ_summary.get("p90_abs_mae")
    p95_abs_succ = succ_summary.get("p95_abs_mae")
    median_abs_succ = succ_summary.get("median_abs_mae")

    # primary decision (use abs MAE of winners; tight SL = <=1.5%)
    winners_hit_by_1pct = sum(1 for r in ok_rows if abs(float(r["overall_mae_pct"])) >= 1.0)
    winners_hit_share_1 = winners_hit_by_1pct / len(ok_rows) if ok_rows else 0.0
    if not ok_rows:
        decision = "DATA_INSUFFICIENT_FOR_SL_TP_DECISION"
    elif (p90_abs_succ or 0) <= 1.0 and (p95_abs_succ or 0) <= 1.5:
        decision = "ENTRY_DRAWDOWN_SMALL_AND_STABLE"
    elif winners_hit_share_1 >= 0.55 and (p90_abs_succ or 0) >= 2.0:
        # majority of winners need >1% adverse room; tight SL not compatible
        decision = "ENTRY_DRAWDOWN_TOO_LARGE_FOR_TIGHT_SL"
    elif (median_abs_succ or 0) >= 2.0 or (p90_abs_succ or 0) >= 4.0:
        decision = "WINNERS_REQUIRE_LARGE_INITIAL_DRAWDOWN"
    elif (p90_abs_succ or 0) <= 2.5 and (p95_abs_succ or 0) <= 4.5:
        decision = "ENTRY_DRAWDOWN_MODERATE_BUT_MANAGEABLE"
    else:
        decision = "TP_SL_RESULTS_DEPEND_ON_HOLDING_WINDOW"

    # recommend 2-3 SL/TP: prefer tested grid pairs with SL beyond p90 abs MAE of winners
    med_mfe = succ_summary.get("median_mfe") or 0
    recommendations = []
    # Prefer asymmetric wide-SL pairs from the audited grid
    grid_pref = [
        (1.0, 3.0, "TP +1% / SL -3%: SL near p90 abs MAE winners; TP often reached by winners"),
        (1.5, 3.0, "TP +1.5% / SL -3%: wider TP vs same SL zone"),
        (2.0, 4.0, "TP +2% / SL -4%: near p95 abs MAE; exploratory wider hold"),
    ]
    # snap to nearest audited asymmetric if present in ft_summary
    ft_lookup = {(float(r["tp_pct"]), float(r["sl_pct"])): r for r in ft_summary}
    for tp, sl, reason in grid_pref:
        # use exact if in grid else closest audited
        key = (tp, sl)
        meta = ft_lookup.get(key)
        if meta is None:
            # find closest audited by sl then tp
            candidates = sorted(
                ft_summary,
                key=lambda r: (abs(float(r["sl_pct"]) - sl), abs(float(r["tp_pct"]) - tp)),
            )
            meta = candidates[0] if candidates else None
            if meta:
                tp, sl = float(meta["tp_pct"]), float(meta["sl_pct"])
        item = {"tp_pct": tp, "sl_pct": sl, "reason": reason}
        if meta:
            item["tp_first_rate"] = meta.get("tp_first_rate")
            item["sl_first_rate"] = meta.get("sl_first_rate")
            item["theo_avg_R_conservative"] = meta.get("theo_avg_R_conservative_sl_on_ambiguous")
            item["winners_abs_mae_ge_sl"] = sum(
                1 for r in ok_rows if abs(float(r["overall_mae_pct"])) >= sl
            )
        recommendations.append(item)
    # If distribution suggests different rounded levels, keep first three only
    recommendations = recommendations[:3]

    answers = {
        "q1_median_mae_all": _pctile(all_mae, 50),
        "q2_median_mae_successful": succ_summary.get("median_mae"),
        "q3_p75_p90_p95_mae_successful": {
            "p75": succ_summary.get("p75_mae"),
            "p90": succ_summary.get("p90_mae"),
            "p95": succ_summary.get("p95_mae"),
            "p75_abs": succ_summary.get("p75_abs_mae"),
            "p90_abs": succ_summary.get("p90_abs_mae"),
            "p95_abs": succ_summary.get("p95_abs_mae"),
        },
        "q4_median_mfe": succ_summary.get("median_mfe") if ok_rows else _pctile(all_mfe, 50),
        "q5_tp_thresholds_reached": {
            str(t): sum(1 for r in mae_target_rows if r["target_pct"] == t and r["target_reached"] and r.get("trade_success"))
            for t in TARGET_PCTS
        },
        "q6_sl_would_stop_winners": {
            str(sl): sum(1 for r in ok_rows if abs(float(r["overall_mae_pct"])) >= sl)
            for sl in (0.5, 1.0, 1.5, 2.0, 3.0)
        },
        "q7_best_first_touch": top_combos[:3],
        "q8_typical_minutes": {
            "median_minutes_to_mae_success": _pctile([float(r["minutes_to_mae"]) for r in ok_rows if r.get("minutes_to_mae") is not None], 50),
            "median_minutes_to_mfe_success": _pctile([float(r["minutes_to_mfe"]) for r in ok_rows if r.get("minutes_to_mfe") is not None], 50),
        },
        "q9_hedge_2to1_at_p90_abs_mae": hedge_2to1_pnl(-(p90_abs_succ or 0)),
        "q10_is_15x_necessary": {
            "gross_over_capital": lev["gross_exposure_over_capital"],
            "initial_margin_total": lev["initial_margin_total_usdt"],
            "answer": "15x is not required for 100/50 on 1000 capital (margin ~10 USDT); lower leverage settings also work for this notional.",
        },
        "q11_is_15x_robust": {
            "successful_with_abs_mae_gt_5": adverse_counts["abs_mae_gt_5.0"]["successful"],
            "successful_with_abs_mae_gt_7_5": adverse_counts["abs_mae_gt_7.5"]["successful"],
            "note": "No liquidation SoT; robustness judged by historical abs MAE vs account net exposure only.",
        },
        "q12_sl_zone_for_structure_test": {
            "suggested_sl_zone_pct": [round((p90_abs_succ or 1.5), 2), round((p95_abs_succ or 2.5), 2)],
            "reason": "between p90 and p95 abs MAE of successful trades",
        },
    }

    # corpus / direction splits
    split_rows = []
    for key_name, key_fn in (
        ("corpus", lambda r: r.get("corpus")),
        ("direction", lambda r: r.get("direction")),
    ):
        keys = sorted({key_fn(r) for r in all_valid if key_fn(r) is not None})
        for k in keys:
            sub = [r for r in all_valid if key_fn(r) == k]
            sok = [r for r in sub if r.get("trade_success")]
            abs_s = [abs(float(r["overall_mae_pct"])) for r in sok]
            split_rows.append(
                {
                    "split": key_name,
                    "value": k,
                    "n": len(sub),
                    "n_success": len(sok),
                    "median_abs_mae_success": _pctile(abs_s, 50),
                    "p90_abs_mae_success": _pctile(abs_s, 90),
                    "p95_abs_mae_success": _pctile(abs_s, 95),
                    "median_mfe_success": _pctile([float(r["overall_mfe_pct"]) for r in sok], 50),
                }
            )
    write_csv(output_dir / "corpus_direction_summary.csv", split_rows)

    summary = {
        "primary_decision": decision,
        "n_trades": len(all_valid),
        "n_successful": len(ok_rows),
        "n_blocker": len(blocker_rows),
        "sources": [str(p) for p in sources if p.exists()],
        "splits": split_rows,
        "methodology": {
            "mae_mfe_orientation": "long-oriented vs entry_price",
            "entry_candle_policy": "entry at candle close; MAE/MFE use only subsequent candles (no pre-entry intracandle)",
            "success_definition": sorted(SUCCESS_EXIT_QUALITY),
            "fee_rate": FEE_RATE,
            "actual_bot_unrealized_path": "not available reliably — omitted; simplified 2:1 only",
        },
        "successful_mae_highlight": succ_summary,
        "leverage_context": lev,
        "recommendations_next_backtests": recommendations,
        "answers": answers,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    md = f"""# Hedge Entry MAE/MFE Audit

**Primary decision:** `{decision}`

## Corpus
- trades: {len(all_valid)} | successful: {len(ok_rows)} | blockers: {len(blocker_rows)}
- sources: {summary['sources']}

## Methodology
- Long-oriented MAE/MFE vs shared `entry_price`
- Entry = candle **close** at `start_time`; excursion uses **only later candles** (no pre-entry high/low)
- Success = `exit_quality` in {sorted(SUCCESS_EXIT_QUALITY)}
- Actual bot unrealized equity path: **not used** (unavailable); simplified constant 2:1 PnL only

## Successful-trade adverse depth
- median abs MAE: `{succ_summary.get('median_abs_mae')}`
- p75 / p90 / p95 abs MAE: `{succ_summary.get('p75_abs_mae')}` / `{succ_summary.get('p90_abs_mae')}` / `{succ_summary.get('p95_abs_mae')}`
- {succ_summary.get('statement_p90_abs')}

## Answers
1. Median MAE all: `{answers['q1_median_mae_all']}`
2. Median MAE successful: `{answers['q2_median_mae_successful']}`
3. p75/p90/p95 MAE (signed) and abs: `{answers['q3_p75_p90_p95_mae_successful']}`
4. Median MFE successful: `{answers['q4_median_mfe']}`
5. TP targets reached (successful): `{answers['q5_tp_thresholds_reached']}`
6. Winners that would hit SL abs MAE ≥ threshold: `{answers['q6_sl_would_stop_winners']}`
7. Best first-touch combos: see summary.json
8. Typical minutes to MAE/MFE: `{answers['q8_typical_minutes']}`
9. 2:1 hedge at p90 abs MAE: `{answers['q9_hedge_2to1_at_p90_abs_mae']}`
10. 15x necessary?: `{answers['q10_is_15x_necessary']['answer']}`
11. 15x robustness note: `{answers['q11_is_15x_robust']}`
12. SL zone for PL/PH tests: `{answers['q12_sl_zone_for_structure_test']}`

## Next SL/TP backtests
```
{json.dumps(recommendations, indent=2)}
```
"""
    (output_dir / "summary.md").write_text(md)
    return summary
