"""Global max-1-position chronological sequencer (frozen P5A / conflict / SL_FIRST)."""

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
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import STRATEGY_MAX_HOLD_BY_TF
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import (
    SymbolBooks,
    _close_trade,
    _dir_ret,
    _hold_end_i,
    _scan_exit,
    OpenTrade,
    prepare_signal_events,
)


def _ts_utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def event_sort_key(row: pd.Series) -> tuple:
    """Deterministic global event ordering (documented tie-break)."""
    tf = str(row["signal_tf"])
    return (
        _ts_utc(row["entry_time"]).value,
        _ts_utc(row["confirmation_available_at"]).value,
        -int(TF_RANK[tf]),  # higher TF first
        str(row["symbol"]),
    )


def build_global_event_frame(
    per_symbol: dict[str, tuple[pd.DataFrame, list[dict[str, Any]], dict[int, int]]],
) -> pd.DataFrame:
    """Merge Tier-A (pre-filtered) signals across symbols into one sorted event list."""
    frames = []
    for sym, (df, _clusters, sig_to_cluster) in per_symbol.items():
        if df.empty:
            continue
        part = df.copy()
        part["symbol"] = sym
        part["cluster_id"] = part["signal_id"].map(lambda sid: int(sig_to_cluster.get(int(sid), -1)))
        # namespaced cluster key so DOGE cid=3 ≠ APT cid=3
        part["cluster_key"] = part.apply(
            lambda r: f"{r['symbol']}::{int(r['cluster_id'])}", axis=1
        )
        frames.append(part)
    if not frames:
        return pd.DataFrame()
    ev = pd.concat(frames, ignore_index=True)
    ev["_sk"] = ev.apply(event_sort_key, axis=1)
    ev = ev.sort_values("_sk").reset_index(drop=True)
    ev = ev.drop(columns=["_sk"])
    return ev


@dataclass
class _Suppressed:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def add(self, row: pd.Series, *, reason: str, open_symbol: str | None) -> None:
        self.rows.append(
            {
                "symbol": str(row["symbol"]),
                "side": str(row["side"]),
                "signal_tf": str(row["signal_tf"]),
                "signal_id": int(row["signal_id"]),
                "cluster_key": str(row["cluster_key"]),
                "signal_available_at": _ts_utc(row["confirmation_available_at"]),
                "entry_available_at": _ts_utc(row["entry_time"]),
                "reason": reason,
                "open_symbol": open_symbol,
            }
        )


def run_global_single_position(
    events: pd.DataFrame,
    books: dict[str, SymbolBooks],
    clusters_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    fee_pct: float,
    upgrade_policy: str = "P5A",
    conflict_exit: bool = True,
    window_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """
    Chronological global FLAT/OPEN sequencer.

    Entry only when FLAT and entry_time > last_exit_time (strict).
    P5A / conflict only for the open symbol; cross-symbol → suppress.
    """
    suppressed = _Suppressed()
    trades: list[dict[str, Any]] = []
    open_tr: OpenTrade | None = None
    entered_clusters: set[str] = set()
    last_exit_time: pd.Timestamp | None = None
    n_upgrades = 0
    n_conflicts = 0
    n_ambiguous = 0
    total_signals = int(len(events))

    def books_of(sym: str) -> SymbolBooks:
        return books[sym]

    def _px_from_gross(tr: OpenTrade, eg: float) -> float:
        if tr.side == "LONG":
            return tr.epx * (1.0 + float(eg) / 100.0)
        return tr.epx * (1.0 - float(eg) / 100.0)

    def close_open(exit_i: int, reason: str, gross: float, px: float, amb: bool = False) -> None:
        nonlocal open_tr, n_ambiguous, last_exit_time
        assert open_tr is not None
        b = books_of(open_tr.symbol)
        if amb:
            n_ambiguous += 1
        rec = _close_trade(
            open_tr,
            exit_i=int(exit_i),
            exit_reason=reason,
            gross=float(gross),
            exit_price=float(px),
            fee_pct=fee_pct,
            books=b,
            ambiguous=amb,
        )
        # normalize field names for this audit
        rec["gross_return_pct"] = rec.pop("gross_return")
        rec["fee_pct"] = rec.pop("fees")
        rec["net_return_pct"] = rec.pop("net_return")
        rec["upgrade_count"] = rec.pop("number_of_upgrades")
        rec["holding_minutes"] = rec.pop("holding_time_min")
        rec["signal_time"] = _ts_utc(open_tr.entry_conf)
        rec["entry_time"] = _ts_utc(rec["entry_time"])
        rec["exit_time"] = _ts_utc(rec["exit_time"])
        trades.append(rec)
        last_exit_time = _ts_utc(rec["exit_time"])
        open_tr = None

    def flush_before_abs(cutoff: pd.Timestamp) -> None:
        """Resolve TP/SL/TIMEOUT on open trade for bars with open_time < cutoff."""
        nonlocal open_tr
        if open_tr is None:
            return
        b = books_of(open_tr.symbol)
        # last bar index strictly before cutoff (books are tz-naive ns)
        cut = np.datetime64(_ts_utc(cutoff).tz_localize(None).to_datetime64())
        bar_i = int(np.searchsorted(b.open_times, cut, side="left"))
        scan_end = min(open_tr.end_i, bar_i - 1)
        if open_tr.cursor <= scan_end:
            et, eg, exi, amb = _scan_exit(
                open_tr.side,
                open_tr.epx,
                b.high,
                b.low,
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
            g = _dir_ret(open_tr.side, open_tr.epx, float(b.close[exi]))
            close_open(exi, "TIMEOUT", g, float(b.close[exi]))

    def first_of_cluster(row: pd.Series) -> bool:
        sym = str(row["symbol"])
        cid = int(row["cluster_id"])
        if cid < 0:
            return False
        clusters = clusters_by_symbol[sym]
        if cid >= len(clusters):
            return False
        crow = clusters[cid]["rows"].iloc[0]
        return int(row["signal_id"]) == int(crow["signal_id"])

    if events.empty:
        return {
            "trades": [],
            "suppressed": [],
            "funnel": {
                "total_signals": 0,
                "executed_trades": 0,
                "suppressed_signals": 0,
                "upgrades": 0,
                "conflicts": 0,
                "ambiguous_sl_first": 0,
            },
        }

    for _, row in events.iterrows():
        sym = str(row["symbol"])
        ei = int(row["entry_i"])
        b = books_of(sym)
        n = len(b.close)
        if ei < 0 or ei >= n:
            continue
        entry_time = _ts_utc(row["entry_time"])
        side = str(row["side"])
        tf = str(row["signal_tf"])
        ckey = str(row["cluster_key"])
        conf = _ts_utc(row["confirmation_available_at"])

        # Resolve any exit that occurs before this signal's entry time
        flush_before_abs(entry_time)

        if open_tr is not None:
            same_symbol = open_tr.symbol == sym
            if same_symbol and side == open_tr.side:
                if TF_RANK[tf] > TF_RANK[open_tr.plan_tf] and upgrade_policy == "P5A":
                    open_tr.tp, open_tr.sl = apply_upgrade_plan(
                        "P5A", open_tr.tp, open_tr.sl, tf, extra_4h=False
                    )
                    open_tr.plan_tf = tf
                    open_tr.max_hold_min = max(
                        open_tr.max_hold_min, STRATEGY_MAX_HOLD_BY_TF[tf]
                    )
                    open_tr.end_i = _hold_end_i(
                        open_tr.ei, books_of(open_tr.symbol).open_times, open_tr.max_hold_min, n
                    )
                    open_tr.upgrade_tfs.append(tf)
                    open_tr.n_upgrades += 1
                    n_upgrades += 1
                # still suppress new entry for this cluster
                if ckey not in entered_clusters:
                    suppressed.add(
                        row,
                        reason="SUPPRESSED_WHILE_POSITION_OPEN",
                        open_symbol=open_tr.symbol,
                    )
                    entered_clusters.add(ckey)
                else:
                    suppressed.add(
                        row,
                        reason="SUPPRESSED_WHILE_POSITION_OPEN",
                        open_symbol=open_tr.symbol,
                    )
            elif same_symbol and side != open_tr.side:
                if TF_RANK[tf] > TF_RANK[open_tr.plan_tf]:
                    dt = (conf - open_tr.entry_conf).total_seconds() / 60.0
                    if dt <= pair_window(open_tr.first_tf, tf):
                        open_tr.conflict_seen = True
                        n_conflicts += 1
                        if conflict_exit:
                            ob = books_of(open_tr.symbol)
                            # conflict exit at this signal's T0 open on THE OPEN symbol book
                            # For same-symbol, ei indexes open symbol books.
                            g = _dir_ret(open_tr.side, open_tr.epx, float(ob.opens[ei]))
                            close_open(ei, "HIGHER_TF_CONFLICT", g, float(ob.opens[ei]))
                if open_tr is not None:
                    if ckey not in entered_clusters:
                        entered_clusters.add(ckey)
                    suppressed.add(
                        row,
                        reason="SUPPRESSED_WHILE_POSITION_OPEN",
                        open_symbol=open_tr.symbol,
                    )
            else:
                # other symbol — never upgrade / never open; discard forever
                if ckey not in entered_clusters:
                    entered_clusters.add(ckey)
                suppressed.add(
                    row,
                    reason="SUPPRESSED_WHILE_POSITION_OPEN",
                    open_symbol=open_tr.symbol,
                )

            # same-bar TP/SL check on open symbol at this absolute entry minute
            if open_tr is not None:
                ob = books_of(open_tr.symbol)
                # map entry_time to open symbol bar index
                cut = np.datetime64(entry_time.tz_localize(None).to_datetime64())
                oi = int(np.searchsorted(ob.open_times, cut, side="left"))
                if 0 <= oi < len(ob.close) and ob.open_times[oi] == cut:
                    et, eg, exi, amb = _scan_exit(
                        open_tr.side,
                        open_tr.epx,
                        ob.high,
                        ob.low,
                        oi,
                        oi,
                        open_tr.tp,
                        open_tr.sl,
                    )
                    if et is not None:
                        close_open(int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)
                    else:
                        open_tr.cursor = max(open_tr.cursor, oi + 1)
            continue

        # FLAT
        if last_exit_time is not None and not (entry_time > last_exit_time):
            # equal or earlier — do not open (includes same-minute as exit)
            if ckey not in entered_clusters:
                entered_clusters.add(ckey)
            suppressed.add(
                row,
                reason="SUPPRESSED_ENTRY_NOT_STRICTLY_AFTER_EXIT",
                open_symbol=None,
            )
            continue

        if ckey in entered_clusters:
            continue
        if not first_of_cluster(row):
            # non-first of cluster while flat (e.g. first was suppressed earlier)
            continue

        tp, sl = tpsl_for_tf(tf, extra_4h=False)
        max_hold = STRATEGY_MAX_HOLD_BY_TF[tf]
        end_i = _hold_end_i(ei, b.open_times, max_hold, n)
        if window_end is not None:
            we = np.datetime64(_ts_utc(window_end).tz_localize(None).to_datetime64())
            end_cap = int(np.searchsorted(b.open_times, we, side="right") - 1)
            end_i = min(end_i, max(ei, end_cap))

        open_tr = OpenTrade(
            symbol=sym,
            side=side,
            ei=ei,
            epx=float(row["entry_price"]),
            entry_time=entry_time,
            entry_conf=conf,
            first_tf=tf,
            plan_tf=tf,
            tp=tp,
            sl=sl,
            max_hold_min=max_hold,
            end_i=end_i,
            cluster_id=int(row["cluster_id"]),
            is_tier_a_entry=bool(row.get("is_tier_a", False)),
            is_q4_entry=bool(row.get("is_q4", False)),
            cursor=ei + 1,
        )
        entered_clusters.add(ckey)

        et, eg, exi, amb = _scan_exit(
            open_tr.side, open_tr.epx, b.high, b.low, ei, ei, open_tr.tp, open_tr.sl
        )
        if et is not None:
            close_open(int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)

    # end of stream: close remaining
    if open_tr is not None:
        b = books_of(open_tr.symbol)
        n = len(b.close)
        if window_end is not None:
            flush_before_abs(_ts_utc(window_end) + pd.Timedelta(minutes=1))
        else:
            flush_before_abs(_ts_utc(b.open_times[-1]) + pd.Timedelta(minutes=1))
        if open_tr is not None:
            et, eg, exi, amb = _scan_exit(
                open_tr.side,
                open_tr.epx,
                b.high,
                b.low,
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
                if open_tr.end_i >= n - 1 or (
                    window_end is not None
                    and _ts_utc(b.open_times[exi]) >= _ts_utc(window_end)
                ):
                    reason = "END_OF_DATA"
                    if window_end is not None:
                        we = np.datetime64(_ts_utc(window_end).tz_localize(None).to_datetime64())
                        exi = min(exi, max(open_tr.ei, int(np.searchsorted(b.open_times, we, side="right") - 1)))
                g = _dir_ret(open_tr.side, open_tr.epx, float(b.close[exi]))
                close_open(exi, reason, g, float(b.close[exi]))

    trades.sort(key=lambda t: (_ts_utc(t["exit_time"]), _ts_utc(t["entry_time"])))
    for i, t in enumerate(trades, start=1):
        t["trade_id"] = i

    return {
        "trades": trades,
        "suppressed": suppressed.rows,
        "funnel": {
            "total_signals": total_signals,
            "executed_trades": int(len(trades)),
            "suppressed_signals": int(len(suppressed.rows)),
            "upgrades": int(n_upgrades),
            "conflicts": int(n_conflicts),
            "ambiguous_sl_first": int(n_ambiguous),
            "suppression_rate": (
                float(len(suppressed.rows) / total_signals) if total_signals else None
            ),
        },
    }


def prepare_symbol_universe(
    symbol: str,
    sig_valid: pd.DataFrame,
    *,
    tier_a_only: bool = True,
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[int, int]]:
    return prepare_signal_events(sig_valid, tier_a_only=tier_a_only)
