"""Separate simplified global single-position event loop for audit (no global_engine import)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_signal_confluence_db import TF_RANK
from orderbook_analyse.fractal_signal_confluence_db.cluster import pair_window
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck import (
    FEE_PCT,
    STRATEGY_MAX_HOLD_BY_TF,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.path_replay import (
    MinuteBook,
    exit_price_from_gross,
    gross_dir,
    scan_bar_sl_first,
    tpsl,
    ts_utc,
)


def sort_key(row: pd.Series) -> tuple:
    return (
        ts_utc(row["entry_time"]).value,
        ts_utc(row["confirmation_available_at"]).value,
        -int(TF_RANK[str(row["signal_tf"])]),
        str(row["symbol"]),
    )


@dataclass
class _Pos:
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
    end_i: int
    cursor: int
    upgrades: list[str] = field(default_factory=list)
    cluster_key: str = ""


def _hold_end(ei: int, times: np.ndarray, hold_min: int) -> int:
    t_end = times[ei] + np.timedelta64(int(hold_min), "m")
    return min(len(times) - 1, max(ei, int(np.searchsorted(times, t_end, side="right") - 1)))


def independent_global_replay(
    events: pd.DataFrame,
    books: dict[str, MinuteBook],
    first_of_cluster: set[tuple[str, int]],
    *,
    fee_pct: float = FEE_PCT,
    window_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """
    Fresh FLAT→ENTRY→EXIT loop. first_of_cluster = {(symbol, signal_id), ...} for cluster firsts.
    """
    if events.empty:
        return {"trades": [], "suppressed": 0}

    ev = events.copy()
    ev["_sk"] = ev.apply(sort_key, axis=1)
    ev = ev.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)

    pos: _Pos | None = None
    last_exit: pd.Timestamp | None = None
    entered: set[str] = set()
    trades: list[dict[str, Any]] = []
    suppressed = 0

    def close_at(i: int, book: MinuteBook, reason: str, gross: float, px: float) -> None:
        nonlocal pos, last_exit
        assert pos is not None
        seq = pos.first_tf
        for t in pos.upgrades:
            seq = f"{seq}->{t}"
        rec = {
            "symbol": pos.symbol,
            "side": pos.side,
            "signal_time": pos.entry_conf,
            "entry_time": pos.entry_time,
            "exit_time": ts_utc(pd.Timestamp(book.times[i])),
            "first_signal_tf": pos.first_tf,
            "highest_tf_reached": pos.plan_tf,
            "entry_price": pos.epx,
            "exit_price": float(px),
            "exit_reason": reason,
            "gross_return_pct": float(gross),
            "fee_pct": float(fee_pct),
            "net_return_pct": float(gross) - float(fee_pct),
            "upgrade_count": len(pos.upgrades),
            "upgrade_sequence": seq,
            "holding_minutes": float(
                (book.times[i] - book.times[pos.ei]) / np.timedelta64(1, "m")
            ),
        }
        trades.append(rec)
        last_exit = rec["exit_time"]
        pos = None

    def flush_before(cutoff: pd.Timestamp) -> None:
        nonlocal pos
        if pos is None:
            return
        b = books[pos.symbol]
        cut = np.datetime64(ts_utc(cutoff).tz_localize(None).to_datetime64())
        bar_i = int(np.searchsorted(b.times, cut, side="left"))
        scan_end = min(pos.end_i, bar_i - 1)
        for i in range(pos.cursor, scan_end + 1):
            r, g, _ = scan_bar_sl_first(
                pos.side, pos.epx, float(b.highs[i]), float(b.lows[i]), pos.tp, pos.sl
            )
            if r is not None:
                close_at(i, b, r, float(g), exit_price_from_gross(pos.side, pos.epx, float(g)))
                return
        if pos is not None:
            pos.cursor = max(pos.cursor, scan_end + 1)
        if pos is not None and bar_i > pos.end_i and pos.cursor > pos.end_i:
            i = pos.end_i
            g = gross_dir(pos.side, pos.epx, float(b.closes[i]))
            close_at(i, b, "TIMEOUT", g, float(b.closes[i]))

    for _, row in ev.iterrows():
        sym = str(row["symbol"])
        side = str(row["side"])
        tf = str(row["signal_tf"])
        sid = int(row["signal_id"])
        ckey = str(row["cluster_key"])
        entry_time = ts_utc(row["entry_time"])
        conf = ts_utc(row["confirmation_available_at"])
        ei = int(row["entry_i"])
        b = books[sym]

        flush_before(entry_time)

        if pos is not None:
            if pos.symbol == sym and side == pos.side:
                if TF_RANK[tf] > TF_RANK[pos.plan_tf]:
                    pos.tp, pos.sl = tpsl(tf)
                    pos.plan_tf = tf
                    pos.upgrades.append(tf)
                    hold = max(STRATEGY_MAX_HOLD_BY_TF[pos.first_tf], STRATEGY_MAX_HOLD_BY_TF[tf])
                    for ut in pos.upgrades:
                        hold = max(hold, STRATEGY_MAX_HOLD_BY_TF[ut])
                    pos.end_i = _hold_end(pos.ei, books[pos.symbol].times, hold)
            elif pos.symbol == sym and side != pos.side:
                if TF_RANK[tf] > TF_RANK[pos.plan_tf]:
                    dt = (conf - pos.entry_conf).total_seconds() / 60.0
                    if dt <= pair_window(pos.first_tf, tf):
                        ob = books[pos.symbol]
                        # conflict at this T0 on open symbol
                        oi = ob.index_at(entry_time)
                        if oi >= 0:
                            g = gross_dir(pos.side, pos.epx, float(ob.opens[oi]))
                            close_at(oi, ob, "HIGHER_TF_CONFLICT", g, float(ob.opens[oi]))
            if ckey not in entered:
                entered.add(ckey)
            suppressed += 1
            if pos is not None:
                ob = books[pos.symbol]
                oi = ob.index_at(entry_time)
                if oi >= 0:
                    r, g, _ = scan_bar_sl_first(
                        pos.side, pos.epx, float(ob.highs[oi]), float(ob.lows[oi]), pos.tp, pos.sl
                    )
                    if r is not None:
                        close_at(
                            oi,
                            ob,
                            r,
                            float(g),
                            exit_price_from_gross(pos.side, pos.epx, float(g)),
                        )
                    else:
                        pos.cursor = max(pos.cursor, oi + 1)
            continue

        # FLAT
        if last_exit is not None and not (entry_time > last_exit):
            entered.add(ckey)
            suppressed += 1
            continue
        if ckey in entered:
            continue
        if (sym, sid) not in first_of_cluster:
            continue

        tp, sl = tpsl(tf)
        hold = STRATEGY_MAX_HOLD_BY_TF[tf]
        end_i = _hold_end(ei, b.times, hold)
        if window_end is not None:
            we = np.datetime64(ts_utc(window_end).tz_localize(None).to_datetime64())
            end_i = min(end_i, max(ei, int(np.searchsorted(b.times, we, side="right") - 1)))
        pos = _Pos(
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
            end_i=end_i,
            cursor=ei + 1,
            cluster_key=ckey,
        )
        entered.add(ckey)
        r, g, _ = scan_bar_sl_first(
            pos.side, pos.epx, float(b.highs[ei]), float(b.lows[ei]), pos.tp, pos.sl
        )
        if r is not None:
            close_at(ei, b, r, float(g), exit_price_from_gross(pos.side, pos.epx, float(g)))

    if pos is not None:
        b = books[pos.symbol]
        if window_end is not None:
            flush_before(ts_utc(window_end) + pd.Timedelta(minutes=1))
        else:
            flush_before(ts_utc(pd.Timestamp(b.times[-1])) + pd.Timedelta(minutes=1))
        if pos is not None:
            for i in range(pos.cursor, pos.end_i + 1):
                r, g, _ = scan_bar_sl_first(
                    pos.side, pos.epx, float(b.highs[i]), float(b.lows[i]), pos.tp, pos.sl
                )
                if r is not None:
                    close_at(i, b, r, float(g), exit_price_from_gross(pos.side, pos.epx, float(g)))
                    break
            if pos is not None:
                i = pos.end_i
                g = gross_dir(pos.side, pos.epx, float(b.closes[i]))
                close_at(i, b, "TIMEOUT", g, float(b.closes[i]))

    for i, t in enumerate(trades, start=1):
        t["trade_id"] = i
    return {"trades": trades, "suppressed": suppressed}
