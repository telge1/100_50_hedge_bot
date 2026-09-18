"""Gross MFE/MAE and TP/SL-first outcomes from trigger_ts (no fees/slippage)."""

from __future__ import annotations

from typing import Sequence

from .params import PilotParams
from .schema import MidTick, OutcomeRow, TouchEvent
from .util import NS, bps_signed


def _mfe_mae_for_side(
    *,
    fade_side: str,
    trigger_price: float,
    path: Sequence[float],
) -> tuple[float, float]:
    """Direction-normalized MFE/MAE in bps (both >= 0)."""
    mfe = 0.0
    mae = 0.0
    for px in path:
        if fade_side == "SHORT":
            # favorable = down
            fav = bps_signed(trigger_price - px, trigger_price)
            unfav = bps_signed(px - trigger_price, trigger_price)
        else:
            fav = bps_signed(px - trigger_price, trigger_price)
            unfav = bps_signed(trigger_price - px, trigger_price)
        if fav > mfe:
            mfe = fav
        if unfav > mae:
            mae = unfav
    return mfe, mae


def _tpsl_first(
    *,
    fade_side: str,
    trigger_price: float,
    path: Sequence[tuple[int, float]],
    tp_bps: float,
    sl_bps: float,
) -> str:
    """Return TP | SL | NEITHER | AMBIGUOUS | CENSORED(handled by caller)."""
    tp_hit_ns: int | None = None
    sl_hit_ns: int | None = None
    for ts, px in path:
        if fade_side == "SHORT":
            tp_move = bps_signed(trigger_price - px, trigger_price)
            sl_move = bps_signed(px - trigger_price, trigger_price)
        else:
            tp_move = bps_signed(px - trigger_price, trigger_price)
            sl_move = bps_signed(trigger_price - px, trigger_price)
        if tp_hit_ns is None and tp_move >= tp_bps:
            tp_hit_ns = ts
        if sl_hit_ns is None and sl_move >= sl_bps:
            sl_hit_ns = ts
        if tp_hit_ns is not None and sl_hit_ns is not None:
            break
    if tp_hit_ns is None and sl_hit_ns is None:
        return "NEITHER"
    if tp_hit_ns is not None and sl_hit_ns is not None and tp_hit_ns == sl_hit_ns:
        return "AMBIGUOUS"
    if tp_hit_ns is not None and (sl_hit_ns is None or tp_hit_ns < sl_hit_ns):
        return "TP"
    if sl_hit_ns is not None and (tp_hit_ns is None or sl_hit_ns < tp_hit_ns):
        return "SL"
    return "AMBIGUOUS"


def compute_outcomes(
    events: Sequence[TouchEvent],
    mids: Sequence[MidTick],
    *,
    params: PilotParams,
    end_ns: int,
) -> list[OutcomeRow]:
    # index mids for forward scans
    valid = [t for t in mids if t.valid]
    rows: list[OutcomeRow] = []
    for ev in events:
        if ev.trigger_ts_ns is None or ev.trigger_price is None or ev.label == "UNRESOLVED":
            for h in params.outcome_horizons_s:
                rows.append(
                    OutcomeRow(
                        event_id=ev.event_id,
                        label=ev.label,
                        fade_side=ev.fade_side,
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
                    )
                )
            continue

        trig = ev.trigger_ts_ns
        tprice = float(ev.trigger_price)
        for h in params.outcome_horizons_s:
            horizon_end = trig + int(h) * NS
            if horizon_end > end_ns:
                rows.append(
                    OutcomeRow(
                        event_id=ev.event_id,
                        label=ev.label,
                        fade_side=ev.fade_side,
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
                            f"tp{int(tp)}_sl{int(params.sl_target_bps)}": "CENSORED"
                            for tp in params.tp_targets_bps
                        },
                    )
                )
                continue
            path = [(t.ts_ns, t.mid) for t in valid if trig < t.ts_ns <= horizon_end]
            prices = [p for _, p in path]
            mfe, mae = _mfe_mae_for_side(
                fade_side=ev.fade_side, trigger_price=tprice, path=prices
            )
            tpsl = {
                f"tp{int(tp)}_sl{int(params.sl_target_bps)}": _tpsl_first(
                    fade_side=ev.fade_side,
                    trigger_price=tprice,
                    path=path,
                    tp_bps=float(tp),
                    sl_bps=float(params.sl_target_bps),
                )
                for tp in params.tp_targets_bps
            }
            rows.append(
                OutcomeRow(
                    event_id=ev.event_id,
                    label=ev.label,
                    fade_side=ev.fade_side,
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
                )
            )
    return rows
