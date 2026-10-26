"""Collect normalized trades from Phase 4H / 4M without modifying their engines."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
PHASE4H = ROOT / "tools/phase4h_oco_pending_entry_v1"
PHASE4M = ROOT / "tools/phase4m_touch_rejection_market_backtester_v1"

for p in (PHASE4D, PHASE4H, PHASE4M):
    sys.path.insert(0, str(p))

from market_engine import run_market_backtest  # noqa: E402
from oco_engine import run_oco_backtest  # noqa: E402
from pool_zone_tracker import LifecycleIndex, anchor_from_signal, zone_status_at_bar  # noqa: E402
from signal_provider import load_all_signals  # noqa: E402
from strategy_rules import plan_long, plan_short  # noqa: E402
from exit_simulator import simulate_exit  # noqa: E402


@dataclass
class NormalizedTrade:
    strategy_variant: str
    trade_id: str
    candidate_id: str
    direction: str
    signal_utc: str
    entry_utc: str
    exit_utc: str | None
    entry_price: float
    exit_price: float | None
    entry_pool_lower: float
    entry_pool_upper: float
    entry_pool_origin_ids: str
    tp_pool_bottom: float | None
    tp_pool_top: float | None
    tp_pool_origin_ids: str
    stop_loss_price: float
    take_profit_price: float
    exit_reason: str
    execution_ambiguity: str


def _signal_meta(candidate_id: str) -> dict[str, Any]:
    phase2a = ROOT / "results/phase2a_5m_pd1_depth2_short_candidates_v1"
    prof_path = phase2a / "candidate_landscape_profiles" / f"{candidate_id}.json"
    if prof_path.is_file():
        prof = json.loads(prof_path.read_text())
        cand = prof.get("candidate") or {}
        return {
            "signal_utc": cand.get("signal_close_utc", ""),
            "origins": "|".join(cand.get("upper_pool_origins") or []),
            "landscape_intervals": cand.get("landscape_intervals") or [],
            "pool_lower": float(cand.get("upper_pool_lower") or 0),
            "pool_upper": float(cand.get("upper_pool_upper") or 0),
        }
    return {"signal_utc": "", "origins": "", "landscape_intervals": [], "pool_lower": 0.0, "pool_upper": 0.0}


def _tp_meta_short(entry: float, intervals: list[dict], tp_price: float) -> tuple[float | None, float | None, str]:
    for iv in intervals:
        if iv.get("side") != "lower":
            continue
        if int(iv.get("depth") or 0) != 2:
            continue
        top = float(iv["top_price"])
        if abs(top - tp_price) < 1e-8:
            return float(iv["bottom_price"]), top, "|".join(iv.get("origins") or [])
    return None, tp_price, ""


def _tp_meta_long(uppers: list[dict], tp_price: float) -> tuple[float | None, float | None, str]:
    for iv in uppers:
        if int(iv.get("depth") or 0) != 2:
            continue
        bot = float(iv["bottom_price"])
        if abs(bot - tp_price) < 1e-8:
            return bot, float(iv["top_price"]), "|".join(iv.get("origins") or [])
    return tp_price, None, ""


def collect_oco_trades(cfg: dict, prices: dict, signals: list) -> list[NormalizedTrade]:
    out = run_oco_backtest(cfg, signals, prices, lifecycle=LifecycleIndex.load())
    events = out["oco_order_events"]
    open_by_trade: dict[str, dict] = {}
    fill_by_candidate: dict[str, dict] = {}
    for e in events:
        if e.get("event_type") == "ORDER_FILLED":
            fill_by_candidate[e["candidate_id"]] = e
        if e.get("event_type") == "POSITION_OPEN":
            note = e.get("note") or ""
            m = re.search(r"sl=([\d.eE+-]+)\s+tp=([\d.eE+-]+)", note)
            sl, tp = (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
            open_by_trade[e["trade_id"]] = {
                **e,
                "sl_price": sl,
                "tp_price": tp,
            }

    audit = {r["trade_id"]: r for r in out["oco_fill_audit"]}
    trades: list[NormalizedTrade] = []
    sig_map = {s.candidate_id: s for s in signals}

    for tid, op in open_by_trade.items():
        cid = op["candidate_id"]
        sig = sig_map.get(cid)
        fill = fill_by_candidate.get(cid, {})
        aud = audit.get(tid, {})
        direction = op["direction"]
        entry = float(fill.get("limit_price") or 0)
        sl = float(op["sl_price"])
        tp = float(op["tp_price"])
        if aud:
            exit_reason = aud.get("exit_reason") or "POSITION_OPEN_AT_DATA_END"
            exit_utc = aud.get("exit_utc") or None
        else:
            exit_reason = "POSITION_OPEN_AT_DATA_END"
            exit_utc = None
        exit_price = None
        amb = "NONE"
        if exit_reason in ("AMBIGUOUS_INTRABAR_EXIT", "AMBIGUOUS_ENTRY_EXIT_SEQUENCE"):
            amb = "AMBIGUOUS_SL_TP_SEQUENCE"
        elif exit_reason == "SL":
            exit_price = sl
        elif exit_reason == "TP":
            exit_price = tp
        elif exit_reason == "POSITION_OPEN_AT_DATA_END":
            exit_utc = None

        pool_l = sig.pool_lower if sig else 0.0
        pool_u = sig.pool_upper if sig else 0.0
        origins = sig.pool_identity if sig else ""
        tp_bot, tp_top, tp_orig = (None, tp, "")
        if sig and direction == "SHORT":
            tp_bot, tp_top, tp_orig = _tp_meta_short(entry, sig.landscape_intervals, tp)
        elif sig and direction == "LONG":
            tp_bot, tp_top, tp_orig = _tp_meta_long(sig.upper_tp_rows, tp)

        trades.append(
            NormalizedTrade(
                strategy_variant="LIMIT_OCO_4H",
                trade_id=tid,
                candidate_id=cid,
                direction=direction,
                signal_utc=sig.signal_close_utc if sig else "",
                entry_utc=fill.get("event_utc") or aud.get("fill_utc") or "",
                exit_utc=exit_utc,
                entry_price=entry,
                exit_price=exit_price,
                entry_pool_lower=pool_l,
                entry_pool_upper=pool_u,
                entry_pool_origin_ids=origins,
                tp_pool_bottom=tp_bot,
                tp_pool_top=tp_top,
                tp_pool_origin_ids=tp_orig,
                stop_loss_price=sl,
                take_profit_price=tp,
                exit_reason=exit_reason,
                execution_ambiguity=amb,
            )
        )
    return trades


def collect_market_trades(cfg: dict, prices: dict, signals: list) -> list[NormalizedTrade]:
    from market_engine import WatchSpec

    watches = [
        WatchSpec(
            watch_key=s.candidate_id,
            candidate_id=s.candidate_id,
            direction=s.direction,
            watch_from_utc=s.signal_close_utc,
            pool_lower=s.pool_lower,
            pool_upper=s.pool_upper,
            pool_identity=s.pool_identity,
            landscape_intervals=s.landscape_intervals,
            upper_tp_rows=s.upper_tp_rows,
        )
        for s in signals
    ]
    out = run_market_backtest(
        cfg,
        watches,
        prices,
        LifecycleIndex.load(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_status_at_bar,
        anchor_from_signal,
    )
    sig_map = {s.candidate_id: s for s in signals}
    trades: list[NormalizedTrade] = []
    for row in out["simulated_market_trades"]:
        cid = row["candidate_id"]
        sig = sig_map.get(cid)
        meta = _signal_meta(cid)
        direction = row["direction"]
        entry = float(row["entry_price"])
        sl = float(row["sl_price"])
        tp = float(row["tp_price"])
        exit_reason = row.get("exit_reason") or "POSITION_OPEN_AT_DATA_END"
        exit_price = row.get("exit_price")
        exit_price_f = float(exit_price) if exit_price not in ("", None) else None
        amb = "NONE"
        if exit_reason in ("AMBIGUOUS_INTRABAR_EXIT", "AMBIGUOUS_ENTRY_EXIT_SEQUENCE"):
            amb = "AMBIGUOUS_SL_TP_SEQUENCE"
        elif exit_reason == "SL" and exit_price_f is not None:
            pass
        elif exit_reason == "TP" and exit_price_f is not None:
            pass
        elif exit_reason == "POSITION_OPEN_AT_DATA_END":
            exit_price_f = None

        tp_bot, tp_top, tp_orig = (None, tp, "")
        intervals = sig.landscape_intervals if sig else meta["landscape_intervals"]
        if direction == "SHORT":
            tp_bot, tp_top, tp_orig = _tp_meta_short(entry, intervals, tp)
        elif sig:
            tp_bot, tp_top, tp_orig = _tp_meta_long(sig.upper_tp_rows, tp)

        trades.append(
            NormalizedTrade(
                strategy_variant="TOUCH_REJECTION_MARKET_4M",
                trade_id=row["trade_id"],
                candidate_id=cid,
                direction=direction,
                signal_utc=meta.get("signal_utc") or row.get("rejection_confirm_utc", ""),
                entry_utc=row["fill_utc"],
                exit_utc=row.get("exit_utc") or None,
                entry_price=entry,
                exit_price=exit_price_f,
                entry_pool_lower=sig.pool_lower if sig else meta["pool_lower"],
                entry_pool_upper=sig.pool_upper if sig else meta["pool_upper"],
                entry_pool_origin_ids=sig.pool_identity if sig else meta.get("origins", ""),
                tp_pool_bottom=tp_bot,
                tp_pool_top=tp_top,
                tp_pool_origin_ids=tp_orig,
                stop_loss_price=sl,
                take_profit_price=tp,
                exit_reason=exit_reason,
                execution_ambiguity=amb,
            )
        )
    return trades
