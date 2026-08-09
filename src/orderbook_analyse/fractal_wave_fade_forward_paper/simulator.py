"""Causal paper simulator mirroring frozen strategy backtest engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.simulate import (
    apply_upgrade_plan,
    tpsl_for_tf,
)
from orderbook_analyse.fractal_signal_confluence_db import TF_RANK
from orderbook_analyse.fractal_signal_confluence_db.cluster import pair_window
from orderbook_analyse.fractal_wave_fade_forward_paper.state import OpenPositionState
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import STRATEGY_MAX_HOLD_BY_TF
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import (
    SymbolBooks,
    _hold_end_i,
    _scan_exit,
    prepare_signal_events,
)


def _utc(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _dir_ret(side: str, epx: float, px: float) -> float:
    if side == "LONG":
        return (px / epx - 1.0) * 100.0
    return (epx - px) / epx * 100.0


def _tp_sl_prices(side: str, epx: float, tp: float, sl: float) -> tuple[float, float]:
    if side == "LONG":
        return epx * (1 + tp / 100.0), epx * (1 - sl / 100.0)
    return epx * (1 - tp / 100.0), epx * (1 + sl / 100.0)


@dataclass
class LiveOpen:
    trade_id: str
    cluster_id: int
    side: str
    ei: int
    epx: float
    entry_time: pd.Timestamp
    entry_conf: pd.Timestamp
    signal_time: pd.Timestamp
    first_tf: str
    plan_tf: str
    tp: float
    sl: float
    tp_initial: float
    sl_initial: float
    max_hold_min: int
    end_i: int
    cursor: int
    upgrade_tfs: list[str] = field(default_factory=list)
    n_upgrades: int = 0
    conflict_seen: bool = False
    is_tier_a: bool = True
    is_q4: bool = True
    validation_mode: str = "REPLAY"


EventCb = Callable[[dict[str, Any]], None]


def open_from_state(op: OpenPositionState) -> LiveOpen:
    return LiveOpen(
        trade_id=op.trade_id,
        cluster_id=int(op.cluster_id),
        side=op.side,
        ei=int(op.entry_i),
        epx=float(op.entry_price),
        entry_time=_utc(op.entry_time),
        entry_conf=_utc(op.entry_conf),
        signal_time=_utc(op.signal_time or op.entry_conf),
        first_tf=op.first_signal_tf,
        plan_tf=op.plan_tf,
        tp=float(op.tp_pct),
        sl=float(op.sl_pct),
        tp_initial=float(op.tp_pct_initial or op.tp_pct),
        sl_initial=float(op.sl_pct_initial or op.sl_pct),
        max_hold_min=int(op.max_hold_min),
        end_i=int(op.end_i),
        cursor=int(op.cursor),
        upgrade_tfs=list(op.upgrade_tfs or []),
        n_upgrades=int(op.n_upgrades),
        conflict_seen=bool(op.conflict_seen),
        is_tier_a=bool(op.is_tier_a_entry),
        is_q4=bool(op.is_q4_entry),
        validation_mode=op.validation_mode or "REPLAY",
    )


def open_to_state(tr: LiveOpen) -> OpenPositionState:
    return OpenPositionState(
        trade_id=tr.trade_id,
        cluster_id=tr.cluster_id,
        side=tr.side,
        entry_time=tr.entry_time.isoformat(),
        entry_price=tr.epx,
        entry_conf=tr.entry_conf.isoformat(),
        first_signal_tf=tr.first_tf,
        plan_tf=tr.plan_tf,
        tp_pct=tr.tp,
        sl_pct=tr.sl,
        max_hold_min=tr.max_hold_min,
        entry_i=tr.ei,
        end_i=tr.end_i,
        cursor=tr.cursor,
        n_upgrades=tr.n_upgrades,
        upgrade_tfs=list(tr.upgrade_tfs),
        conflict_seen=tr.conflict_seen,
        is_tier_a_entry=tr.is_tier_a,
        is_q4_entry=tr.is_q4,
        signal_time=tr.signal_time.isoformat(),
        validation_mode=tr.validation_mode,
        tp_pct_initial=tr.tp_initial,
        sl_pct_initial=tr.sl_initial,
    )


def simulate_symbol_paper(
    symbol: str,
    sig_valid: pd.DataFrame,
    books: SymbolBooks,
    *,
    paper_start: pd.Timestamp,
    forward_capture_start: pd.Timestamp | None,
    fee_pct: float,
    conflict_exit: bool,
    trade_id_start: int,
    entered_cluster_ids: set[int] | None = None,
    restore_open: OpenPositionState | None = None,
    process_from_1m: pd.Timestamp | None = None,
    until_1m: pd.Timestamp | None = None,
    force_close_end: bool = False,
    emit: EventCb | None = None,
) -> dict[str, Any]:
    """
    Run frozen strategy causally.

    - No entries with entry_time < paper_start
    - Stops managing bars after until_1m (inclusive last processed)
    - Leaves open position unless force_close_end
    """
    paper_start = _utc(paper_start)
    if forward_capture_start is not None:
        forward_capture_start = _utc(forward_capture_start)
    emit = emit or (lambda e: None)

    df, clusters, sig_to_cluster = prepare_signal_events(sig_valid, tier_a_only=True)
    n = len(books.close)
    if n < 2:
        return {
            "trades": [],
            "events": [],
            "open": None,
            "entered_cluster_ids": set(),
            "last_processed_1m_ts": None,
            "next_trade_seq": trade_id_start,
            "stats": {},
        }

    # bar index bounds
    if until_1m is not None:
        until_i = int(np.searchsorted(books.open_times, np.datetime64(_utc(until_1m).to_datetime64()), side="right") - 1)
        until_i = min(max(until_i, 0), n - 1)
    else:
        until_i = n - 1

    if process_from_1m is not None:
        from_i = int(
            np.searchsorted(
                books.open_times,
                np.datetime64(_utc(process_from_1m).to_datetime64()),
                side="right",
            )
        )
    else:
        from_i = 0

    events_out: list[dict] = []
    trades_out: list[dict] = []
    entered = set(entered_cluster_ids or [])
    open_tr: LiveOpen | None = open_from_state(restore_open) if restore_open else None
    # Recompute end_i against current books length if restored
    if open_tr is not None:
        open_tr.end_i = _hold_end_i(open_tr.ei, books.open_times, open_tr.max_hold_min, n)
        open_tr.cursor = max(open_tr.cursor, open_tr.ei + 1)

    trade_seq = trade_id_start
    n_upgrades = 0
    n_entries = 0
    n_suppressed = 0
    last_sig_avail: dict[str, str | None] = {tf: None for tf in ("15m", "30m", "1h", "4h")}

    def _emit(ev: dict) -> None:
        events_out.append(ev)
        emit(ev)

    def _validation_mode(signal_time: pd.Timestamp) -> str:
        if forward_capture_start is None:
            return "REPLAY"
        return "TRUE_FORWARD" if _utc(signal_time) >= forward_capture_start else "REPLAY"

    def _px_from_gross(tr: LiveOpen, eg: float) -> float:
        if tr.side == "LONG":
            return tr.epx * (1.0 + float(eg) / 100.0)
        return tr.epx * (1.0 - float(eg) / 100.0)

    def _close(tr: LiveOpen, exit_i: int, reason: str, gross: float, px: float, amb: bool = False) -> None:
        nonlocal open_tr
        hold = float((books.open_times[exit_i] - books.open_times[tr.ei]) / np.timedelta64(1, "m"))
        seq = tr.first_tf
        for t in tr.upgrade_tfs:
            seq = f"{seq}->{t}"
        row = {
            "trade_id": tr.trade_id,
            "symbol": symbol,
            "side": tr.side,
            "cluster_id": tr.cluster_id,
            "first_signal_tf": tr.first_tf,
            "highest_tf_reached": tr.plan_tf,
            "signal_time": tr.signal_time.isoformat(),
            "entry_time": tr.entry_time.isoformat(),
            "entry_price": tr.epx,
            "exit_time": _utc(pd.Timestamp(books.open_times[exit_i])).isoformat(),
            "exit_price": float(px),
            "exit_reason": reason,
            "tp_pct_initial": tr.tp_initial,
            "sl_pct_initial": tr.sl_initial,
            "tp_pct_final": tr.tp,
            "sl_pct_final": tr.sl,
            "upgrade_count": tr.n_upgrades,
            "upgrade_sequence": seq,
            "holding_minutes": hold,
            "gross_return_pct": float(gross),
            "fee_pct": fee_pct,
            "net_return_pct": float(gross) - fee_pct,
            "same_bar_ambiguous": bool(amb),
            "validation_mode": tr.validation_mode,
        }
        trades_out.append(row)
        _emit(
            {
                "event_ts": row["exit_time"],
                "symbol": symbol,
                "event_type": {
                    "TP": "TP_EXIT",
                    "SL": "SL_EXIT",
                    "HIGHER_TF_CONFLICT": "CONFLICT_EXIT",
                    "TIMEOUT": "TIMEOUT_EXIT",
                    "END_OF_DATA": "END_OF_DATA",
                }.get(reason, reason),
                "trade_id": tr.trade_id,
                "cluster_id": tr.cluster_id,
                "details": {"reason": reason, "gross": gross, "net": row["net_return_pct"]},
            }
        )
        open_tr = None

    def flush_before(bar_i: int) -> None:
        nonlocal open_tr
        if open_tr is None:
            return
        scan_end = min(open_tr.end_i, bar_i - 1, until_i)
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
                _close(open_tr, int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)
                return
            open_tr.cursor = scan_end + 1
        if open_tr is not None and bar_i > open_tr.end_i and open_tr.cursor > open_tr.end_i:
            exi = min(open_tr.end_i, until_i)
            g = _dir_ret(open_tr.side, open_tr.epx, float(books.close[exi]))
            _close(open_tr, exi, "TIMEOUT", g, float(books.close[exi]))

    # Filter signal events: process those with entry_i in range and confirmation known
    if df.empty:
        events = df
    else:
        events = df.sort_values(["entry_i", "confirmation_available_at"]).reset_index(drop=True)

    for _, row in events.iterrows():
        ei = int(row["entry_i"])
        if ei < 0 or ei > until_i:
            continue
        # skip already-processed signal entries when resuming (entry bar already passed watermark)
        if ei < from_i and open_tr is None:
            # still mark cluster entered if first signal before resume? skip — state has entered clusters
            continue
        if ei < from_i and open_tr is not None and ei < open_tr.cursor:
            continue

        side = str(row["side"])
        tf = str(row["signal_tf"])
        cid = int(sig_to_cluster.get(int(row["signal_id"]), -1))
        conf = _utc(row["confirmation_available_at"])
        last_sig_avail[tf] = conf.isoformat()

        # No forward PnL entries before paper_start
        entry_time = _utc(row["entry_time"])
        if entry_time < paper_start and (open_tr is None or side != open_tr.side):
            # allow upgrades/conflicts for open trades even if signal after paper — entry_time of upgrade signal can be after paper
            if open_tr is None:
                continue

        flush_before(ei)
        if ei > until_i:
            break

        _emit(
            {
                "event_ts": conf.isoformat(),
                "symbol": symbol,
                "event_type": "SIGNAL",
                "trade_id": open_tr.trade_id if open_tr else None,
                "cluster_id": cid,
                "details": {
                    "tf": tf,
                    "side": side,
                    "available_at": conf.isoformat(),
                    "entry_time": entry_time.isoformat(),
                    "decision_time": entry_time.isoformat(),
                    "is_tier_a": bool(row.get("is_tier_a", False)),
                },
            }
        )

        if open_tr is not None:
            if side == open_tr.side:
                if TF_RANK[tf] > TF_RANK[open_tr.plan_tf]:
                    from_tf = open_tr.plan_tf
                    old_tp, old_sl = open_tr.tp, open_tr.sl
                    open_tr.tp, open_tr.sl = apply_upgrade_plan(
                        "P5A", open_tr.tp, open_tr.sl, tf, extra_4h=False
                    )
                    open_tr.plan_tf = tf
                    open_tr.max_hold_min = max(open_tr.max_hold_min, STRATEGY_MAX_HOLD_BY_TF[tf])
                    open_tr.end_i = _hold_end_i(open_tr.ei, books.open_times, open_tr.max_hold_min, n)
                    open_tr.upgrade_tfs.append(tf)
                    open_tr.n_upgrades += 1
                    n_upgrades += 1
                    ur = _dir_ret(open_tr.side, open_tr.epx, float(books.opens[ei]))
                    _emit(
                        {
                            "event_ts": entry_time.isoformat(),
                            "symbol": symbol,
                            "event_type": "UPGRADE",
                            "trade_id": open_tr.trade_id,
                            "cluster_id": open_tr.cluster_id,
                            "details": {
                                "from_tf": from_tf,
                                "to_tf": tf,
                                "old_tp": old_tp,
                                "new_tp": open_tr.tp,
                                "old_sl": old_sl,
                                "new_sl": open_tr.sl,
                                "price_at_upgrade": float(books.opens[ei]),
                                "unrealized_return_at_upgrade": ur,
                                "upgrade_time": entry_time.isoformat(),
                            },
                        }
                    )
                if cid not in entered:
                    n_suppressed += 1
                    entered.add(cid)
                    _emit(
                        {
                            "event_ts": entry_time.isoformat(),
                            "symbol": symbol,
                            "event_type": "SUPPRESSED_SIGNAL_WHILE_OPEN",
                            "trade_id": open_tr.trade_id,
                            "cluster_id": cid,
                            "details": {"tf": tf, "side": side},
                        }
                    )
            else:
                if TF_RANK[tf] > TF_RANK[open_tr.plan_tf]:
                    dt = (conf - open_tr.entry_conf).total_seconds() / 60.0
                    if dt <= pair_window(open_tr.first_tf, tf):
                        open_tr.conflict_seen = True
                        if conflict_exit:
                            g = _dir_ret(open_tr.side, open_tr.epx, float(books.opens[ei]))
                            _close(open_tr, ei, "HIGHER_TF_CONFLICT", g, float(books.opens[ei]))
                if open_tr is not None and cid not in entered:
                    n_suppressed += 1
                    entered.add(cid)

            if open_tr is not None and ei <= until_i:
                et, eg, exi, amb = _scan_exit(
                    open_tr.side, open_tr.epx, books.high, books.low, ei, ei, open_tr.tp, open_tr.sl
                )
                if et is not None:
                    _close(open_tr, int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)
                else:
                    open_tr.cursor = ei + 1
            continue

        # flat — entry only on first cluster signal at/after paper_start
        if entry_time < paper_start:
            continue
        if cid < 0 or cid in entered:
            continue
        crow = clusters[cid]["rows"].iloc[0]
        if int(row["signal_id"]) != int(crow["signal_id"]):
            continue
        if not bool(row.get("is_tier_a", False)):
            continue

        tp, sl = tpsl_for_tf(tf, extra_4h=False)
        max_hold = STRATEGY_MAX_HOLD_BY_TF[tf]
        end_i = _hold_end_i(ei, books.open_times, max_hold, n)
        tid = f"{symbol}-{trade_seq:06d}"
        trade_seq += 1
        n_entries += 1
        vmode = _validation_mode(conf)
        open_tr = LiveOpen(
            trade_id=tid,
            cluster_id=cid,
            side=side,
            ei=ei,
            epx=float(row["entry_price"]),
            entry_time=entry_time,
            entry_conf=conf,
            signal_time=conf,
            first_tf=tf,
            plan_tf=tf,
            tp=tp,
            sl=sl,
            tp_initial=tp,
            sl_initial=sl,
            max_hold_min=max_hold,
            end_i=end_i,
            cursor=ei + 1,
            is_tier_a=True,
            is_q4=bool(row.get("is_q4", False)),
            validation_mode=vmode,
        )
        entered.add(cid)
        tp_px, sl_px = _tp_sl_prices(side, open_tr.epx, tp, sl)
        _emit(
            {
                "event_ts": entry_time.isoformat(),
                "symbol": symbol,
                "event_type": "CLUSTER_CREATED",
                "trade_id": tid,
                "cluster_id": cid,
                "details": {"first_tf": tf, "side": side},
            }
        )
        _emit(
            {
                "event_ts": entry_time.isoformat(),
                "symbol": symbol,
                "event_type": "ENTRY",
                "trade_id": tid,
                "cluster_id": cid,
                "details": {
                    "signal_time": conf.isoformat(),
                    "decision_time": entry_time.isoformat(),
                    "entry_time": entry_time.isoformat(),
                    "entry_price": open_tr.epx,
                    "first_signal_tf": tf,
                    "side": side,
                    "tp_pct": tp,
                    "sl_pct": sl,
                    "tp_price": tp_px,
                    "sl_price": sl_px,
                    "validation_mode": vmode,
                },
            }
        )

        et, eg, exi, amb = _scan_exit(
            open_tr.side, open_tr.epx, books.high, books.low, ei, ei, open_tr.tp, open_tr.sl
        )
        if et is not None:
            _close(open_tr, int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)

    # flush remaining bars to until_i
    if open_tr is not None:
        flush_before(until_i + 1)
        if open_tr is not None and open_tr.cursor <= min(open_tr.end_i, until_i):
            et, eg, exi, amb = _scan_exit(
                open_tr.side,
                open_tr.epx,
                books.high,
                books.low,
                open_tr.cursor,
                min(open_tr.end_i, until_i),
                open_tr.tp,
                open_tr.sl,
            )
            if et is not None:
                _close(open_tr, int(exi), et, float(eg), _px_from_gross(open_tr, float(eg)), amb)
            else:
                open_tr.cursor = min(open_tr.end_i, until_i) + 1
                if open_tr.cursor > open_tr.end_i:
                    exi = min(open_tr.end_i, until_i)
                    g = _dir_ret(open_tr.side, open_tr.epx, float(books.close[exi]))
                    _close(open_tr, exi, "TIMEOUT", g, float(books.close[exi]))

    if force_close_end and open_tr is not None:
        exi = until_i
        g = _dir_ret(open_tr.side, open_tr.epx, float(books.close[exi]))
        _close(open_tr, exi, "END_OF_DATA", g, float(books.close[exi]))

    last_ts = _utc(pd.Timestamp(books.open_times[until_i])).isoformat()
    return {
        "trades": trades_out,
        "events": events_out,
        "open": open_to_state(open_tr) if open_tr else None,
        "entered_cluster_ids": entered,
        "last_processed_1m_ts": last_ts,
        "last_signal_available_at": last_sig_avail,
        "next_trade_seq": trade_seq,
        "stats": {
            "n_entries": n_entries,
            "n_upgrades": n_upgrades,
            "n_suppressed": n_suppressed,
            "n_closed": len(trades_out),
            "n_clusters": len(clusters),
            "n_universe_signals": int(len(df)),
        },
    }
