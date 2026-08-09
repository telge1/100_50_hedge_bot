"""Chronological multi-symbol strategy engine (frozen cluster / P5A / conflict)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.simulate import (
    apply_upgrade_plan,
    tpsl_for_tf,
)
from orderbook_analyse.fractal_signal_confluence_db import TF_RANK
from orderbook_analyse.fractal_signal_confluence_db.cluster import (
    build_same_side_clusters,
    pair_window,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import (
    STRATEGY_MAX_HOLD_BY_TF,
    UNIT_SIZE,
)


def _dir_ret(side: str, epx: float, px: float) -> float:
    if side == "LONG":
        return (px / epx - 1.0) * 100.0
    return (epx - px) / epx * 100.0


def _hold_end_i(ei: int, open_times: np.ndarray, max_hold_min: int, n: int) -> int:
    t_end = open_times[ei] + np.timedelta64(int(max_hold_min), "m")
    return min(n - 1, max(ei + 1, int(np.searchsorted(open_times, t_end, side="right") - 1)))


def _scan_exit(
    side: str,
    epx: float,
    high: np.ndarray,
    low: np.ndarray,
    start_i: int,
    end_i: int,
    tp: float,
    sl: float,
) -> tuple[str | None, float | None, int | None, bool]:
    """Returns exit_type, gross, exit_i, ambiguous_sl_first."""
    if end_i < start_i:
        return None, None, None, False
    hh = high[start_i : end_i + 1]
    ll = low[start_i : end_i + 1]
    if hh.size == 0:
        return None, None, None, False
    if side == "LONG":
        fav = (hh / epx - 1.0) * 100.0
        adv = (ll / epx - 1.0) * 100.0
    else:
        fav = (epx - ll) / epx * 100.0
        adv = -((hh - epx) / epx * 100.0)
    hit_tp = fav >= tp
    hit_sl = adv <= -sl
    any_tp = bool(np.any(hit_tp))
    any_sl = bool(np.any(hit_sl))
    i_tp = int(np.argmax(hit_tp)) if any_tp else -1
    i_sl = int(np.argmax(hit_sl)) if any_sl else -1
    if not any_tp and not any_sl:
        return None, None, None, False
    if not any_tp or (any_sl and i_sl <= i_tp):
        amb = bool(any_tp and any_sl and i_sl == i_tp)
        return "SL", float(-sl), start_i + i_sl, amb
    return "TP", float(tp), start_i + i_tp, False


@dataclass
class OpenTrade:
    symbol: str
    side: str
    ei: int
    epx: float
    entry_time: pd.Timestamp
    entry_conf: pd.Timestamp
    first_tf: str
    plan_tf: str
    tp: float
    sl: float
    max_hold_min: int
    end_i: int
    cluster_id: int
    is_tier_a_entry: bool
    is_q4_entry: bool
    upgrade_tfs: list[str] = field(default_factory=list)
    conflict_seen: bool = False
    cursor: int = 0  # next bar to scan
    n_upgrades: int = 0


@dataclass
class SymbolBooks:
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    opens: np.ndarray
    open_times: np.ndarray


def prepare_signal_events(
    sig_valid: pd.DataFrame,
    *,
    tier_a_only: bool,
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[int, int]]:
    """Filter signals, build frozen clusters, return events + signal→cluster map."""
    df = sig_valid.copy()
    if tier_a_only:
        df = df[df["is_tier_a"].astype(bool)].copy().reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
    clusters = build_same_side_clusters(df)
    # map original signal_id → cluster index
    sig_to_cluster: dict[int, int] = {}
    for ci, c in enumerate(clusters):
        for _, row in c["rows"].iterrows():
            sig_to_cluster[int(row["signal_id"])] = ci
    return df, clusters, sig_to_cluster


def _close_trade(
    tr: OpenTrade,
    *,
    exit_i: int,
    exit_reason: str,
    gross: float,
    exit_price: float,
    fee_pct: float,
    books: SymbolBooks,
    ambiguous: bool = False,
) -> dict[str, Any]:
    hold_min = float(
        (books.open_times[exit_i] - books.open_times[tr.ei]) / np.timedelta64(1, "m")
    )
    seq = tr.first_tf
    for t in tr.upgrade_tfs:
        seq = f"{seq}->{t}"
    net = float(gross) - fee_pct
    return {
        "symbol": tr.symbol,
        "side": tr.side,
        "first_signal_tf": tr.first_tf,
        "highest_tf_reached": tr.plan_tf,
        "entry_time": pd.Timestamp(tr.entry_time),
        "entry_price": float(tr.epx),
        "exit_time": pd.Timestamp(books.open_times[exit_i]),
        "exit_price": float(exit_price),
        "exit_reason": exit_reason,
        "gross_return": float(gross),
        "fees": float(fee_pct),
        "net_return": net,
        "holding_time_min": hold_min,
        "number_of_upgrades": int(tr.n_upgrades),
        "upgrade_sequence": seq,
        "conflict_seen": bool(tr.conflict_seen),
        "is_tier_a_entry": bool(tr.is_tier_a_entry),
        "is_q4_entry": bool(tr.is_q4_entry),
        "cluster_id": int(tr.cluster_id),
        "ambiguous_sl_first": bool(ambiguous),
        "unit_size": UNIT_SIZE,
        "entry_i": int(tr.ei),
        "exit_i": int(exit_i),
    }


def run_symbol_backtest(
    symbol: str,
    sig_valid: pd.DataFrame,
    books: SymbolBooks,
    *,
    tier_a_only: bool,
    upgrade_policy: str,  # "P0" | "P5A"
    conflict_exit: bool,
    fee_pct: float,
    extra_4h: bool = False,
) -> dict[str, Any]:
    """Chronological single-symbol backtest with max one open position."""
    df, clusters, sig_to_cluster = prepare_signal_events(sig_valid, tier_a_only=tier_a_only)
    n = len(books.close)
    if df.empty or n < 2:
        return {
            "trades": [],
            "funnel": {
                "symbol": symbol,
                "raw_signals": int(len(sig_valid)),
                "universe_signals": 0,
                "clusters": 0,
                "entered_trades": 0,
                "signals_suppressed_while_open": 0,
                "upgrades": 0,
                "conflicts": 0,
                "completed_trades": 0,
                "ambiguous_sl_first": 0,
            },
            "clusters": clusters,
        }

    # Sort signals by entry_i then confirmation
    events = df.sort_values(["entry_i", "confirmation_available_at"]).reset_index(drop=True)

    open_tr: OpenTrade | None = None
    trades: list[dict[str, Any]] = []
    entered_clusters: set[int] = set()
    suppressed = 0
    n_upgrades = 0
    n_conflicts = 0
    n_ambiguous = 0
    # track which cluster ids already had a first-entry attempt while we might still upgrade

    def _px_from_gross(tr: OpenTrade, eg: float) -> float:
        if tr.side == "LONG":
            return tr.epx * (1.0 + float(eg) / 100.0)
        return tr.epx * (1.0 - float(eg) / 100.0)

    def close_open(exit_i: int, reason: str, gross: float, px: float, amb: bool = False) -> None:
        nonlocal open_tr, n_ambiguous
        assert open_tr is not None
        if amb:
            n_ambiguous += 1
        trades.append(
            _close_trade(
                open_tr,
                exit_i=int(exit_i),
                exit_reason=reason,
                gross=float(gross),
                exit_price=float(px),
                fee_pct=fee_pct,
                books=books,
                ambiguous=amb,
            )
        )
        open_tr = None

    def flush_before(bar_i: int) -> None:
        """Resolve TP/SL/TIMEOUT for bars strictly before bar_i."""
        nonlocal open_tr
        if open_tr is None:
            return
        scan_end = min(open_tr.end_i, bar_i - 1)
        if open_tr.cursor <= scan_end:
            et, eg, exi, amb = _scan_exit(
                open_tr.side,
                open_tr.epx,
                books.high,
                books.low,
                open_tr.cursor,
                scan_end,
                open_tr.tp,
                open_tr.sl,
            )
            if et is not None:
                close_open(int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)
                return
            open_tr.cursor = scan_end + 1
        if open_tr is not None and bar_i > open_tr.end_i and open_tr.cursor > open_tr.end_i:
            exi = open_tr.end_i
            g = _dir_ret(open_tr.side, open_tr.epx, float(books.close[exi]))
            close_open(exi, "TIMEOUT", g, float(books.close[exi]))

    def mark_suppressed(cid: int) -> None:
        nonlocal suppressed
        if cid >= 0 and cid not in entered_clusters:
            suppressed += 1
            entered_clusters.add(cid)

    for _, row in events.iterrows():
        ei = int(row["entry_i"])
        if ei < 0 or ei >= n:
            continue
        side = str(row["side"])
        tf = str(row["signal_tf"])
        cid = int(sig_to_cluster.get(int(row["signal_id"]), -1))
        conf = pd.Timestamp(row["confirmation_available_at"])

        flush_before(ei)

        if open_tr is not None:
            if side == open_tr.side:
                if TF_RANK[tf] > TF_RANK[open_tr.plan_tf] and upgrade_policy == "P5A":
                    open_tr.tp, open_tr.sl = apply_upgrade_plan(
                        "P5A", open_tr.tp, open_tr.sl, tf, extra_4h=extra_4h
                    )
                    open_tr.plan_tf = tf
                    open_tr.max_hold_min = max(
                        open_tr.max_hold_min, STRATEGY_MAX_HOLD_BY_TF[tf]
                    )
                    open_tr.end_i = _hold_end_i(
                        open_tr.ei, books.open_times, open_tr.max_hold_min, n
                    )
                    open_tr.upgrade_tfs.append(tf)
                    open_tr.n_upgrades += 1
                    n_upgrades += 1
                mark_suppressed(cid)
            else:
                # opposite
                if TF_RANK[tf] > TF_RANK[open_tr.plan_tf]:
                    dt = (conf - open_tr.entry_conf).total_seconds() / 60.0
                    if dt <= pair_window(open_tr.first_tf, tf):
                        open_tr.conflict_seen = True
                        n_conflicts += 1
                        if conflict_exit:
                            g = _dir_ret(open_tr.side, open_tr.epx, float(books.opens[ei]))
                            close_open(ei, "HIGHER_TF_CONFLICT", g, float(books.opens[ei]))
                if open_tr is not None:
                    mark_suppressed(cid)

            if open_tr is not None:
                et, eg, exi, amb = _scan_exit(
                    open_tr.side,
                    open_tr.epx,
                    books.high,
                    books.low,
                    ei,
                    ei,
                    open_tr.tp,
                    open_tr.sl,
                )
                if et is not None:
                    close_open(int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)
                else:
                    open_tr.cursor = ei + 1
            continue

        # flat → enter only on first signal of a new cluster
        if cid < 0 or cid in entered_clusters:
            continue
        crow = clusters[cid]["rows"].iloc[0]
        if int(row["signal_id"]) != int(crow["signal_id"]):
            continue
        tp, sl = tpsl_for_tf(tf, extra_4h=extra_4h and tf == "4h")
        max_hold = STRATEGY_MAX_HOLD_BY_TF[tf]
        end_i = _hold_end_i(ei, books.open_times, max_hold, n)
        open_tr = OpenTrade(
            symbol=symbol,
            side=side,
            ei=ei,
            epx=float(row["entry_price"]),
            entry_time=pd.Timestamp(row["entry_time"]),
            entry_conf=conf,
            first_tf=tf,
            plan_tf=tf,
            tp=tp,
            sl=sl,
            max_hold_min=max_hold,
            end_i=end_i,
            cluster_id=cid,
            is_tier_a_entry=bool(row.get("is_tier_a", False)),
            is_q4_entry=bool(row.get("is_q4", False)),
            cursor=ei + 1,
        )
        entered_clusters.add(cid)

        et, eg, exi, amb = _scan_exit(
            open_tr.side, open_tr.epx, books.high, books.low, ei, ei, open_tr.tp, open_tr.sl
        )
        if et is not None:
            close_open(int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)

    # end of data: close or timeout remaining
    if open_tr is not None:
        flush_before(n)
        if open_tr is not None:
            et, eg, exi, amb = _scan_exit(
                open_tr.side,
                open_tr.epx,
                books.high,
                books.low,
                open_tr.cursor,
                open_tr.end_i,
                open_tr.tp,
                open_tr.sl,
            )
            if et is not None:
                close_open(int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)
            else:
                exi = min(open_tr.end_i, n - 1)
                reason = "TIMEOUT"
                if open_tr.end_i >= n - 1:
                    reason = "END_OF_DATA"
                    exi = n - 1
                g = _dir_ret(open_tr.side, open_tr.epx, float(books.close[exi]))
                close_open(exi, reason, g, float(books.close[exi]))

    trades.sort(key=lambda t: (t["exit_time"], t["entry_time"]))
    return {
        "trades": trades,
        "funnel": {
            "symbol": symbol,
            "raw_signals": int(len(sig_valid)),
            "universe_signals": int(len(df)),
            "clusters": int(len(clusters)),
            "entered_trades": int(len(trades)),
            "signals_suppressed_while_open": int(suppressed),
            "upgrades": int(n_upgrades),
            "conflicts": int(n_conflicts),
            "completed_trades": int(len(trades)),
            "ambiguous_sl_first": int(n_ambiguous),
        },
        "clusters": clusters,
    }
