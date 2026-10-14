"""Phase 6I — frozen short backtest on LMV2 CH SSOT + SSOT floor guard (read-only)."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

from research_charts.historical_lmv2_frozen_short_paths_v1 import (
    load_frozen_entry_module,
    verify_frozen_entry_hash,
)
from research_charts.historical_lmv2_frozen_short_signal_v1 import (
    _iso_z,
    _utc,
    evaluate_decision,
    load_market_and_bars,
    parse_decision_close,
)
from research_charts.historical_lmv2.strategy_loop import four_h_closes_from_market
from research_charts.historical_lmv2_ssot_floor_guard_v1 import SsotH4FloorGuard
from research_charts.historical_pool_state_v1 import (
    HistoricalPoolStateError,
    get_published_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.oa_import import ensure_oa_on_path

ensure_oa_on_path()
from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_execution_simulator_v1 import (  # noqa: E402
    FEE_CLOSE_RATE,
    FEE_OPEN_RATE,
    describe_fee_contract,
    describe_timing_contract,
)

HORIZON_BARS = 48 * 4  # analyze_4h_lower_short_block_v1 — 48h on 15m
DEFAULT_TEST_NOTIONAL_USDT = 1000.0
PANE_FROM_DEFAULT = datetime(2026, 2, 10, tzinfo=timezone.utc)


class ExecutionModel(str, Enum):
    NEXT_BAR_OPEN = "NEXT_BAR_OPEN"
    HYPOTHETICAL_SIGNAL_CLOSE = "HYPOTHETICAL_SIGNAL_CLOSE"


class ExitReason(str, Enum):
    SL = "SL"
    TP = "TP"
    SL_GAP_AT_OPEN = "SL_GAP_AT_OPEN"
    TP_GAP_AT_OPEN = "TP_GAP_AT_OPEN"
    HORIZON_MARK = "HORIZON_MARK"
    HORIZON_UNRESOLVED = "HORIZON_UNRESOLVED"
    OPEN_OR_UNRESOLVED = "OPEN_OR_UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NO_FILL_NEXT_BAR = "NO_FILL_NEXT_BAR"


@dataclass
class SignalFunnel:
    bars_replayed: int = 0
    decisions_in_window: int = 0
    entry_setup_candidates: int = 0
    down_4h_blocked: int = 0
    floor_guard_blocked: int = 0
    duplicate_pool_skipped: int = 0
    raw_signals: int = 0
    position_suppressed: int = 0
    ch_publication_missing: int = 0


@dataclass
class RawShortSignal:
    decision_close: str
    entry_open: str
    entry_pool_id: str
    stop_price: float
    tp_top: float
    tp_pool_id: str | None
    gap_pct: float | None
    down_4h: bool
    floor_guard_blocked: bool
    trace: dict[str, Any]


@dataclass
class TradeRecord:
    signal: RawShortSignal
    execution_model: str
    entry_fill_price: float | None
    entry_fill_time: str | None
    exit_reason: str
    exit_price: float | None
    exit_time: str | None
    quantity: float
    gross_pnl_usdt: float
    open_fee_usdt: float
    close_fee_usdt: float
    net_pnl_usdt: float
    same_bar_sl_tp: bool = False
    ambiguity_note: str | None = None
    suppressed: bool = False
    suppress_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_close": self.signal.decision_close,
            "entry_open": self.signal.entry_open,
            "entry_pool_id": self.signal.entry_pool_id,
            "execution_model": self.execution_model,
            "entry_fill_price": self.entry_fill_price,
            "entry_fill_time": self.entry_fill_time,
            "stop_price": self.signal.stop_price,
            "tp_top": self.signal.tp_top,
            "exit_reason": self.exit_reason,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time,
            "quantity": self.quantity,
            "gross_pnl_usdt": self.gross_pnl_usdt,
            "open_fee_usdt": self.open_fee_usdt,
            "close_fee_usdt": self.close_fee_usdt,
            "net_pnl_usdt": self.net_pnl_usdt,
            "same_bar_sl_tp": self.same_bar_sl_tp,
            "ambiguity_note": self.ambiguity_note,
            "suppressed": self.suppressed,
            "suppress_reason": self.suppress_reason,
        }


def _fee(notional: float, rate: float) -> float:
    return abs(notional) * rate


def _bar_index_by_close(bars: list[dict]) -> dict[str, int]:
    return {_iso_z(_utc(b["close_time"])): i for i, b in enumerate(bars)}


def _bar_index_by_open(bars: list[dict]) -> dict[str, int]:
    return {_iso_z(_utc(b["open_time"])): i for i, b in enumerate(bars)}


def count_decision_closes_in_window(from_dc: datetime, to_dc: datetime) -> int:
    """Inclusive 15m decision closes (bar close timestamps)."""
    n = 0
    cur = _utc(from_dc)
    end = _utc(to_dc)
    while cur <= end:
        n += 1
        cur += timedelta(minutes=15)
    return n


def preflight_data_contract(
    *,
    report_first_dc: datetime,
    report_last_dc: datetime,
    pool_detail: int = 10,
) -> dict[str, Any]:
    """15m publications + 4h June coverage for guard."""
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config
    from research_charts.historical_lmv2_ssot_four_h_pools_v1 import inventory_four_h_source

    hcfg = resolve_historical_pd_config(pool_detail)
    cfg_h = hcfg.config_hash
    reader = PublishedActiveReader()
    pub15 = get_published_decision_closes(reader, cfg_h, pipeline_line_id=hcfg.pipeline_line_id)
    in_window = [c for c in pub15 if report_first_dc <= c <= report_last_dc]
    inv4 = inventory_four_h_source()
    return {
        "pool_detail": pool_detail,
        "pipeline_line_id": hcfg.pipeline_line_id,
        "config_hash": cfg_h,
        "report_first_decision_close": _iso_z(report_first_dc),
        "report_last_decision_close": _iso_z(report_last_dc),
        "expected_decision_closes_in_window": count_decision_closes_in_window(
            report_first_dc, report_last_dc
        ),
        "published_15m_closes_in_window": len(in_window),
        "first_published_in_window": _iso_z(in_window[0]) if in_window else None,
        "last_published_in_window": _iso_z(in_window[-1]) if in_window else None,
        "four_h_inventory": inv4,
        "window_publication_complete": len(in_window) == count_decision_closes_in_window(
            report_first_dc, report_last_dc
        ),
    }


def walk_raw_signals(
    *,
    symbol: str,
    bars15: list[dict],
    market: dict,
    pool_detail: int,
    replay_from: datetime,
    report_from: datetime,
    report_to: datetime,
    client=None,
    database: str | None = None,
) -> tuple[list[RawShortSignal], SignalFunnel]:
    guard = SsotH4FloorGuard(
        symbol,
        replay_from,
        report_to + timedelta(days=2),
        pool_detail_4h=10,
        four_h_bar_closes=four_h_closes_from_market(market),
        client=client,
        database=database,
    )
    seen: set[str] = set()
    fn = SignalFunnel()
    out: list[RawShortSignal] = []

    for bar in bars15:
        moment = _utc(bar["close_time"])
        if moment < replay_from:
            guard.advance(bar, moment)
            continue
        fn.bars_replayed += 1
        blocked, _reason = guard.advance(bar, moment)
        if moment < report_from or moment > report_to:
            continue
        fn.decisions_in_window += 1
        try:
            tr = evaluate_decision(
                symbol=symbol,
                bar=bar,
                market=market,
                pool_detail=pool_detail,
                client=client,
                database=database,
                seen_pool_ids=seen,
            )
        except HistoricalPoolStateError:
            fn.ch_publication_missing += 1
            continue

        td = tr.to_dict()
        if not tr.entry_setup:
            continue
        fn.entry_setup_candidates += 1
        if tr.block_reason == "DOWN_4H_FALSE":
            fn.down_4h_blocked += 1
            continue
        if blocked:
            fn.floor_guard_blocked += 1
            continue
        if tr.block_reason == "EPISODE_ALREADY_SEEN":
            fn.duplicate_pool_skipped += 1
            continue
        if not tr.signal:
            continue
        seen.add(str(tr.entry_pool_id))
        fn.raw_signals += 1
        out.append(
            RawShortSignal(
                decision_close=tr.decision_close,
                entry_open=tr.candle_open_utc,
                entry_pool_id=str(tr.entry_pool_id),
                stop_price=float(tr.stop_price),
                tp_top=float(tr.tp_top),
                tp_pool_id=(tr.extra or {}).get("tp_pool_id"),
                gap_pct=tr.gap_pct,
                down_4h=bool(tr.down_4h),
                floor_guard_blocked=False,
                trace=td,
            )
        )
    return out, fn


def _exit_short_on_bar(
    *,
    bar: dict,
    stop: float,
    tp: float,
    check_open_gap: bool,
) -> tuple[ExitReason | None, float | None, bool]:
    """SL_FIRST intrabar; optional open-gap checks on this bar."""
    o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
    same_both = False
    if check_open_gap:
        if o >= stop:
            return ExitReason.SL_GAP_AT_OPEN, o, False
        if o <= tp:
            return ExitReason.TP_GAP_AT_OPEN, o, False
    sl_hit = h >= stop
    tp_hit = l <= tp
    if sl_hit and tp_hit:
        same_both = True
        return ExitReason.SL, stop, same_both
    if sl_hit:
        return ExitReason.SL, stop, False
    if tp_hit:
        return ExitReason.TP, tp, False
    return None, None, False


def simulate_short_trade(
    signal: RawShortSignal,
    bars: list[dict],
    *,
    model: ExecutionModel,
    notional_usdt: float = DEFAULT_TEST_NOTIONAL_USDT,
    data_last_idx: int | None = None,
) -> TradeRecord:
    """Simulate one short from SSOT signal levels (no pool updates after signal)."""
    by_close = _bar_index_by_close(bars)
    sig_idx = by_close.get(signal.decision_close)
    empty = TradeRecord(
        signal=signal,
        execution_model=model.value,
        entry_fill_price=None,
        entry_fill_time=None,
        exit_reason=ExitReason.NO_FILL_NEXT_BAR.value,
        exit_price=None,
        exit_time=None,
        quantity=0.0,
        gross_pnl_usdt=0.0,
        open_fee_usdt=0.0,
        close_fee_usdt=0.0,
        net_pnl_usdt=0.0,
    )
    if sig_idx is None:
        empty.ambiguity_note = "missing_signal_bar"
        return empty

    stop, tp = signal.stop_price, signal.tp_top
    if model == ExecutionModel.HYPOTHETICAL_SIGNAL_CLOSE:
        entry_idx = sig_idx
        entry_px = float(bars[sig_idx]["close"])
        entry_time = _iso_z(_utc(bars[sig_idx]["close_time"]))
        first_scan_idx = sig_idx + 1
        open_gap = False
    else:
        if sig_idx + 1 >= len(bars):
            empty.exit_reason = ExitReason.NO_FILL_NEXT_BAR.value
            empty.ambiguity_note = "no_next_bar"
            return empty
        entry_idx = sig_idx + 1
        entry_px = float(bars[entry_idx]["open"])
        entry_time = _iso_z(_utc(bars[entry_idx]["open_time"]))
        sig_close = _utc(bars[sig_idx]["close_time"])
        entry_open = _utc(bars[entry_idx]["open_time"])
        if entry_open != sig_close:
            empty.exit_reason = ExitReason.AMBIGUOUS.value
            empty.ambiguity_note = f"next_open_ne_signal_close:{entry_open}!={sig_close}"
            return empty
        first_scan_idx = entry_idx
        open_gap = True

    qty = notional_usdt / entry_px if entry_px > 0 else 0.0
    open_fee = _fee(qty * entry_px, FEE_OPEN_RATE)

    horizon_exit_idx = entry_idx + HORIZON_BARS
    last_bar_idx = len(bars) - 1
    if data_last_idx is not None:
        last_bar_idx = min(last_bar_idx, data_last_idx)

    exit_reason: ExitReason | None = None
    exit_px: float | None = None
    exit_time: str | None = None
    same_both = False

    scan_end = min(horizon_exit_idx, last_bar_idx)
    for i in range(first_scan_idx, scan_end + 1):
        bar = bars[i]
        er, px, sb = _exit_short_on_bar(
            bar=bar,
            stop=stop,
            tp=tp,
            check_open_gap=open_gap and i == entry_idx,
        )
        open_gap = False
        if er is not None:
            exit_reason, exit_px, same_both = er, px, sb
            exit_time = _iso_z(_utc(bar["close_time"]))
            break

    if exit_reason is None:
        if horizon_exit_idx > last_bar_idx:
            exit_reason = ExitReason.OPEN_OR_UNRESOLVED
            exit_px = None
            exit_time = None
        else:
            exit_reason = ExitReason.HORIZON_MARK
            exit_px = float(bars[horizon_exit_idx]["close"])
            exit_time = _iso_z(_utc(bars[horizon_exit_idx]["close_time"]))

    gross = 0.0
    close_fee = 0.0
    if exit_px is not None and exit_reason not in (
        ExitReason.OPEN_OR_UNRESOLVED,
        ExitReason.HORIZON_UNRESOLVED,
        ExitReason.AMBIGUOUS,
        ExitReason.NO_FILL_NEXT_BAR,
    ):
        gross = (entry_px - exit_px) * qty
        close_fee = _fee(qty * exit_px, FEE_CLOSE_RATE)

    net = gross - open_fee - close_fee
    return TradeRecord(
        signal=signal,
        execution_model=model.value,
        entry_fill_price=entry_px,
        entry_fill_time=entry_time,
        exit_reason=exit_reason.value,
        exit_price=exit_px,
        exit_time=exit_time,
        quantity=qty,
        gross_pnl_usdt=gross,
        open_fee_usdt=open_fee,
        close_fee_usdt=close_fee,
        net_pnl_usdt=net,
        same_bar_sl_tp=same_both,
    )


def _exit_moment(tr: TradeRecord) -> datetime | None:
    if tr.exit_time:
        return parse_decision_close(tr.exit_time)
    if tr.entry_fill_time and tr.exit_reason == ExitReason.OPEN_OR_UNRESOLVED.value:
        return datetime.max.replace(tzinfo=timezone.utc)
    return None


def apply_single_position_policy(
    trades: list[TradeRecord],
) -> list[TradeRecord]:
    """Mark signals that cannot execute while a prior trade is still open."""
    ordered = sorted(trades, key=lambda t: t.signal.decision_close)
    open_until: datetime | None = None
    for tr in ordered:
        dc = parse_decision_close(tr.signal.decision_close)
        if open_until is not None and dc < open_until:
            tr.suppressed = True
            tr.suppress_reason = "POSITION_ALREADY_OPEN"
            tr.quantity = 0.0
            tr.gross_pnl_usdt = 0.0
            tr.open_fee_usdt = 0.0
            tr.close_fee_usdt = 0.0
            tr.net_pnl_usdt = 0.0
            continue
        if tr.exit_reason in (
            ExitReason.NO_FILL_NEXT_BAR.value,
            ExitReason.AMBIGUOUS.value,
        ):
            continue
        ex = _exit_moment(tr)
        if ex is None or tr.exit_reason == ExitReason.OPEN_OR_UNRESOLVED.value:
            open_until = datetime.max.replace(tzinfo=timezone.utc)
        elif tr.exit_reason == ExitReason.OPEN_OR_UNRESOLVED.value:
            open_until = datetime.max.replace(tzinfo=timezone.utc)
        else:
            open_until = ex
    return ordered


def run_backtest_window(
    *,
    symbol: str = "DOGEUSDT",
    report_first_decision_close: str,
    report_last_decision_close: str,
    pool_detail: int = 10,
    pane_from: datetime | None = None,
    client=None,
    database: str | None = None,
    notional_usdt: float = DEFAULT_TEST_NOTIONAL_USDT,
) -> dict[str, Any]:
    verify = verify_frozen_entry_hash()
    if not verify["match"]:
        return {"verdict": "STOP", "reason": "FROZEN_HASH_MISMATCH", "verify": verify}

    fe = load_frozen_entry_module()
    r0 = parse_decision_close(report_first_decision_close)
    r1 = parse_decision_close(report_last_decision_close)
    pane_from = pane_from or PANE_FROM_DEFAULT
    horizon_end = r1 + timedelta(minutes=15 * HORIZON_BARS)

    market, bars15, load_meta = load_market_and_bars(
        symbol,
        pane_from=pane_from,
        pane_to=horizon_end,
        client=client,
    )
    raw, funnel = walk_raw_signals(
        symbol=symbol,
        bars15=bars15,
        market=market,
        pool_detail=pool_detail,
        replay_from=pane_from,
        report_from=r0,
        report_to=r1,
        client=client,
        database=database,
    )
    data_last_idx = len(bars15) - 1

    trades_nb: list[TradeRecord] = []
    trades_sc: list[TradeRecord] = []
    for sig in raw:
        trades_nb.append(
            simulate_short_trade(
                sig, bars15, model=ExecutionModel.NEXT_BAR_OPEN, notional_usdt=notional_usdt, data_last_idx=data_last_idx
            )
        )
        trades_sc.append(
            simulate_short_trade(
                sig,
                bars15,
                model=ExecutionModel.HYPOTHETICAL_SIGNAL_CLOSE,
                notional_usdt=notional_usdt,
                data_last_idx=data_last_idx,
            )
        )

    trades_nb = apply_single_position_policy(trades_nb)
    trades_sc = apply_single_position_policy(trades_sc)
    funnel.position_suppressed = sum(1 for t in trades_nb if t.suppressed)

    return {
        "verify": verify,
        "frozen_constants": {
            "MIN_GAP_PCT": fe.MIN_GAP_PCT,
            "STOP_PAD": fe.STOP_PAD,
            "HORIZON_BARS": HORIZON_BARS,
        },
        "load_meta": load_meta,
        "funnel": funnel,
        "raw_signals": [asdict(s) for s in raw],
        "trades_next_bar_open": trades_nb,
        "trades_signal_close": trades_sc,
        "notional_usdt": notional_usdt,
    }


def trades_to_csv(trades: list[TradeRecord]) -> str:
    buf = io.StringIO()
    rows = [t.to_dict() for t in trades if not t.suppressed]
    if not rows:
        return "decision_close,entry_pool_id,exit_reason,net_pnl_usdt\n"
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def summarize_trades(trades: list[TradeRecord], *, label: str) -> dict[str, Any]:
    executed = [t for t in trades if not t.suppressed and t.quantity > 0]
    closed = [t for t in executed if t.exit_reason not in (ExitReason.OPEN_OR_UNRESOLVED.value,)]
    wins = [t for t in closed if t.net_pnl_usdt > 0]
    losses = [t for t in closed if t.net_pnl_usdt < 0]
    gross = sum(t.gross_pnl_usdt for t in executed)
    net = sum(t.net_pnl_usdt for t in executed)
    fees = sum(t.open_fee_usdt + t.close_fee_usdt for t in executed)
    pf = None
    win_sum = sum(t.net_pnl_usdt for t in wins)
    loss_sum = abs(sum(t.net_pnl_usdt for t in losses))
    if loss_sum > 0:
        pf = round(win_sum / loss_sum, 4)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(executed, key=lambda x: x.signal.decision_close):
        equity += t.net_pnl_usdt
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    by_exit: dict[str, int] = {}
    for t in executed:
        by_exit[t.exit_reason] = by_exit.get(t.exit_reason, 0) + 1
    return {
        "label": label,
        "executed_trades": len(executed),
        "suppressed_trades": sum(1 for t in trades if t.suppressed),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate": round(len(wins) / len(closed), 4) if closed else None,
        "gross_pnl_usdt": round(gross, 6),
        "fees_usdt": round(fees, 6),
        "net_pnl_usdt": round(net, 6),
        "profit_factor": pf,
        "max_drawdown_usdt": round(max_dd, 6),
        "equity_definition": "cumulative sum of net_pnl_usdt per executed trade in time order",
        "exit_reason_counts": by_exit,
    }


def execution_contract() -> dict[str, Any]:
    return {
        **describe_timing_contract(),
        "execution_models": [ExecutionModel.NEXT_BAR_OPEN.value, ExecutionModel.HYPOTHETICAL_SIGNAL_CLOSE.value],
        "primary_research_model": ExecutionModel.NEXT_BAR_OPEN.value,
        "signal_close_note": "HYPOTHETICAL_SIGNAL_CLOSE — not live-executable; mathematical baseline only",
        "intrabar_rule": "SL_FIRST on high/low after entry; same-bar SL+TP counts as SL",
        "open_gap_rule": "NEXT_BAR_OPEN: if open >= stop -> SL_GAP_AT_OPEN at open; if open <= tp -> TP_GAP_AT_OPEN at open",
        "horizon_bars": HORIZON_BARS,
        "horizon_exit": "if no SL/TP within horizon and data available: exit at close of horizon bar (HORIZON_MARK)",
        "end_of_data": "OPEN_OR_UNRESOLVED — no fictitious close",
        "end_of_backtest_window": "open positions not force-closed at window end",
        "position_policy": "single position — suppress overlapping raw signals (documented deviation from scanner baseline)",
        "test_notional_usdt": DEFAULT_TEST_NOTIONAL_USDT,
        "event_order_next_bar_open": "signal known at decision_close -> earliest fill next bar open -> intrabar exits from that bar",
    }


def freeze_and_source_contract() -> dict[str, Any]:
    fe = load_frozen_entry_module()
    return {
        "strategy": "find_short_entry_15m_v1",
        "freeze_verification": verify_frozen_entry_hash(),
        "data_path": "CH Publication -> HistoricalPoolState -> 6B adapter -> 6C bridge -> 6H2 signal -> 6H3 SSOT guard",
        "pool_detail": 10,
        "symbol": "DOGEUSDT",
        "e1r": "OFF",
        "floor_guard": "SsotH4FloorGuard / short_block_4h_guard.advance",
        "legacy_baseline_pool_id_not_used": "lld:DOGEUSDT:15m:upper:1780482600",
        "lmv2_pool_id_authoritative": "lldv2:DOGEUSDT:15m:micro:upper:1780484400",
        "frozen_stop_pad": fe.STOP_PAD,
    }
