"""V2 outcomes use trade_side (never fade_side as generic direction)."""

from __future__ import annotations

from typing import Sequence

from obfull_research_engine.mp_edge_event_study_v1.schema import MidTick
from obfull_research_engine.mp_edge_event_study_v1.util import NS, bps_signed

from .params import PilotParamsV2
from .schema import EventV2, OutcomeV2


def _mfe_mae(trade_side: str, trigger_price: float, path: Sequence[float]) -> tuple[float, float]:
    mfe = mae = 0.0
    for px in path:
        if trade_side == "SHORT":
            fav = bps_signed(trigger_price - px, trigger_price)
            unfav = bps_signed(px - trigger_price, trigger_price)
        elif trade_side == "LONG":
            fav = bps_signed(px - trigger_price, trigger_price)
            unfav = bps_signed(trigger_price - px, trigger_price)
        else:
            return 0.0, 0.0
        mfe = max(mfe, fav)
        mae = max(mae, unfav)
    return mfe, mae


def _tpsl_first(
    trade_side: str,
    trigger_price: float,
    path: Sequence[tuple[int, float]],
    tp_bps: float,
    sl_bps: float,
) -> str:
    tp_hit = sl_hit = None
    for ts, px in path:
        if trade_side == "SHORT":
            tp_m = bps_signed(trigger_price - px, trigger_price)
            sl_m = bps_signed(px - trigger_price, trigger_price)
        else:
            tp_m = bps_signed(px - trigger_price, trigger_price)
            sl_m = bps_signed(trigger_price - px, trigger_price)
        if tp_hit is None and tp_m >= tp_bps:
            tp_hit = ts
        if sl_hit is None and sl_m >= sl_bps:
            sl_hit = ts
        if tp_hit is not None and sl_hit is not None:
            break
    if tp_hit is None and sl_hit is None:
        return "NEITHER"
    if tp_hit is not None and sl_hit is not None and tp_hit == sl_hit:
        return "AMBIGUOUS"
    if tp_hit is not None and (sl_hit is None or tp_hit < sl_hit):
        return "TP"
    if sl_hit is not None and (tp_hit is None or sl_hit < tp_hit):
        return "SL"
    return "AMBIGUOUS"


def pair_key(tp: float, sl: float) -> str:
    return f"tp{int(tp)}_sl{int(sl)}"


def compute_outcomes_v2(
    events: Sequence[EventV2],
    mids: Sequence[MidTick],
    *,
    params: PilotParamsV2,
    end_ns: int,
) -> list[OutcomeV2]:
    valid = [t for t in mids if t.valid]
    rows: list[OutcomeV2] = []
    for ev in events:
        invalid_note = ev.label_price_only == "TRUE_BREAK"  # flag for V1 comparison docs
        if (
            ev.trigger_ts_ns is None
            or ev.trigger_price is None
            or ev.label_price_only == "UNRESOLVED"
            or not ev.trade_side
        ):
            for h in params.outcome_horizons_s:
                rows.append(
                    OutcomeV2(
                        event_id=ev.event_id,
                        label_price_only=ev.label_price_only,
                        event_role=ev.event_role,
                        fade_side=ev.fade_side,
                        break_side=ev.break_side,
                        trade_side=ev.trade_side,
                        trade_side_reason=ev.trade_side_reason,
                        trigger_ts_ns=ev.trigger_ts_ns,
                        trigger_price=ev.trigger_price,
                        trigger_reason=ev.trigger_reason,
                        is_censored=True,
                        censor_reason=ev.censor_reason or "NO_TRIGGER",
                        horizon_s=int(h),
                        mfe_bps_gross=None,
                        mae_bps_gross=None,
                        outcome_status="NO_TRIGGER" if ev.trigger_ts_ns is None else "CENSORED",
                        tp_sl_results={},
                        v1_true_break_outcomes_invalid=True,
                    )
                )
            continue

        trig = ev.trigger_ts_ns
        tprice = float(ev.trigger_price)
        for h in params.outcome_horizons_s:
            horizon_end = trig + int(h) * NS
            if horizon_end > end_ns:
                rows.append(
                    OutcomeV2(
                        event_id=ev.event_id,
                        label_price_only=ev.label_price_only,
                        event_role=ev.event_role,
                        fade_side=ev.fade_side,
                        break_side=ev.break_side,
                        trade_side=ev.trade_side,
                        trade_side_reason=ev.trade_side_reason,
                        trigger_ts_ns=trig,
                        trigger_price=tprice,
                        trigger_reason=ev.trigger_reason,
                        is_censored=True,
                        censor_reason="OUTCOME_PAST_WINDOW_END",
                        horizon_s=int(h),
                        mfe_bps_gross=None,
                        mae_bps_gross=None,
                        outcome_status="CENSORED",
                        tp_sl_results={
                            pair_key(tp, sl): "CENSORED" for tp, sl in params.tpsl_pairs
                        },
                        v1_true_break_outcomes_invalid=True,
                    )
                )
                continue
            path = [(t.ts_ns, t.mid) for t in valid if trig < t.ts_ns <= horizon_end]
            prices = [p for _, p in path]
            mfe, mae = _mfe_mae(ev.trade_side, tprice, prices)
            tpsl = {
                pair_key(tp, sl): _tpsl_first(ev.trade_side, tprice, path, tp, sl)
                for tp, sl in params.tpsl_pairs
            }
            rows.append(
                OutcomeV2(
                    event_id=ev.event_id,
                    label_price_only=ev.label_price_only,
                    event_role=ev.event_role,
                    fade_side=ev.fade_side,
                    break_side=ev.break_side,
                    trade_side=ev.trade_side,
                    trade_side_reason=ev.trade_side_reason,
                    trigger_ts_ns=trig,
                    trigger_price=tprice,
                    trigger_reason=ev.trigger_reason,
                    is_censored=False,
                    censor_reason="",
                    horizon_s=int(h),
                    mfe_bps_gross=float(mfe),
                    mae_bps_gross=float(mae),
                    outcome_status="OK",
                    tp_sl_results=tpsl,
                    v1_true_break_outcomes_invalid=True,  # documents V1 invalidation globally
                )
            )
    return rows
