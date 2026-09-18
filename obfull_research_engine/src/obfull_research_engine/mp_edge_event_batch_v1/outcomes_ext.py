"""Extended outcomes: mark-to-market gross returns + explicit cost scenarios."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from obfull_research_engine.mp_edge_event_study_v1.schema import MidTick
from obfull_research_engine.mp_edge_event_study_v1.util import NS, bps_signed
from obfull_research_engine.mp_edge_event_study_v2.outcomes import _mfe_mae, _tpsl_first, pair_key
from obfull_research_engine.mp_edge_event_study_v2.params import PilotParamsV2
from obfull_research_engine.mp_edge_event_study_v2.schema import EventV2


@dataclass
class OutcomeExt:
    event_id: str
    window_id: str
    label_price_only: str
    event_role: str
    fade_side: str
    break_side: str
    trade_side: str
    trade_side_reason: str
    confluence_class: str
    trigger_ts_ns: int | None
    trigger_price: float | None
    trigger_reason: str
    horizon_s: int
    is_censored: bool
    censor_reason: str
    outcome_status: str
    mfe_bps_gross: float | None
    mae_bps_gross: float | None
    gross_return_bps: float | None
    net_return_bps_0: float | None
    net_return_bps_8: float | None
    net_return_bps_12: float | None
    tp_sl_results: dict[str, str] = field(default_factory=dict)
    semantics_hash: str = ""
    session_utc: str = ""

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in self.tp_sl_results.items():
            d[f"tpsl_{k}"] = v
        return d


def session_utc_for(ts_ns: int) -> str:
    from datetime import datetime, timezone

    hour = datetime.fromtimestamp(ts_ns / NS, tz=timezone.utc).hour
    if 0 <= hour < 8:
        return "Asia"
    if 8 <= hour < 13:
        return "Europe"
    return "US"


def _mid_at_or_after(mids: Sequence[MidTick], ts_ns: int) -> float | None:
    for t in mids:
        if t.valid and t.ts_ns >= ts_ns:
            return t.mid
    return None


def _mid_at_or_before(mids: Sequence[MidTick], ts_ns: int) -> float | None:
    last = None
    for t in mids:
        if not t.valid:
            continue
        if t.ts_ns > ts_ns:
            break
        last = t.mid
    return last


def signed_return_bps(trade_side: str, entry: float, exit_px: float) -> float:
    if trade_side == "LONG":
        return bps_signed(exit_px - entry, entry)
    if trade_side == "SHORT":
        return bps_signed(entry - exit_px, entry)
    return 0.0


def compute_outcomes_ext(
    events: Sequence[EventV2],
    mids: Sequence[MidTick],
    *,
    params: PilotParamsV2,
    end_ns: int,
    window_id: str,
    semantics_hash: str,
    cost_scenarios_bps: Sequence[float] = (0.0, 8.0, 12.0),
) -> list[OutcomeExt]:
    valid = [t for t in mids if t.valid]
    # ensure sorted
    valid = sorted(valid, key=lambda t: t.ts_ns)
    rows: list[OutcomeExt] = []
    costs = {float(c): c for c in cost_scenarios_bps}

    for ev in events:
        sess = session_utc_for(ev.first_touch_ts_ns)
        if (
            ev.trigger_ts_ns is None
            or ev.trigger_price is None
            or ev.label_price_only == "UNRESOLVED"
            or not ev.trade_side
        ):
            for h in params.outcome_horizons_s:
                rows.append(
                    OutcomeExt(
                        event_id=ev.event_id,
                        window_id=window_id,
                        label_price_only=ev.label_price_only,
                        event_role=ev.event_role,
                        fade_side=ev.fade_side,
                        break_side=ev.break_side,
                        trade_side=ev.trade_side,
                        trade_side_reason=ev.trade_side_reason,
                        confluence_class=ev.confluence_class,
                        trigger_ts_ns=ev.trigger_ts_ns,
                        trigger_price=ev.trigger_price,
                        trigger_reason=ev.trigger_reason,
                        horizon_s=int(h),
                        is_censored=True,
                        censor_reason=ev.censor_reason or "NO_TRIGGER",
                        outcome_status="NO_TRIGGER" if ev.trigger_ts_ns is None else "CENSORED",
                        mfe_bps_gross=None,
                        mae_bps_gross=None,
                        gross_return_bps=None,
                        net_return_bps_0=None,
                        net_return_bps_8=None,
                        net_return_bps_12=None,
                        tp_sl_results={},
                        semantics_hash=semantics_hash,
                        session_utc=sess,
                    )
                )
            continue

        trig = int(ev.trigger_ts_ns)
        tprice = float(ev.trigger_price)
        for h in params.outcome_horizons_s:
            horizon_end = trig + int(h) * NS
            if horizon_end > end_ns:
                rows.append(
                    OutcomeExt(
                        event_id=ev.event_id,
                        window_id=window_id,
                        label_price_only=ev.label_price_only,
                        event_role=ev.event_role,
                        fade_side=ev.fade_side,
                        break_side=ev.break_side,
                        trade_side=ev.trade_side,
                        trade_side_reason=ev.trade_side_reason,
                        confluence_class=ev.confluence_class,
                        trigger_ts_ns=trig,
                        trigger_price=tprice,
                        trigger_reason=ev.trigger_reason,
                        horizon_s=int(h),
                        is_censored=True,
                        censor_reason="OUTCOME_PAST_WINDOW_END",
                        outcome_status="CENSORED",
                        mfe_bps_gross=None,
                        mae_bps_gross=None,
                        gross_return_bps=None,
                        net_return_bps_0=None,
                        net_return_bps_8=None,
                        net_return_bps_12=None,
                        tp_sl_results={
                            pair_key(tp, sl): "CENSORED" for tp, sl in params.tpsl_pairs
                        },
                        semantics_hash=semantics_hash,
                        session_utc=sess,
                    )
                )
                continue

            path = [(t.ts_ns, t.mid) for t in valid if trig < t.ts_ns <= horizon_end]
            prices = [p for _, p in path]
            mfe, mae = _mfe_mae(ev.trade_side, tprice, prices)
            exit_px = _mid_at_or_before(valid, horizon_end)
            if exit_px is None:
                gross = None
                status = "CENSORED"
                cens = True
                reason = "NO_EXIT_MID"
            else:
                gross = signed_return_bps(ev.trade_side, tprice, exit_px)
                status = "OK"
                cens = False
                reason = ""
            tpsl = {
                pair_key(tp, sl): _tpsl_first(ev.trade_side, tprice, path, tp, sl)
                for tp, sl in params.tpsl_pairs
            }
            # costs subtracted exactly once from gross (transparent)
            net0 = None if gross is None else gross - 0.0
            net8 = None if gross is None else gross - 8.0
            net12 = None if gross is None else gross - 12.0
            rows.append(
                OutcomeExt(
                    event_id=ev.event_id,
                    window_id=window_id,
                    label_price_only=ev.label_price_only,
                    event_role=ev.event_role,
                    fade_side=ev.fade_side,
                    break_side=ev.break_side,
                    trade_side=ev.trade_side,
                    trade_side_reason=ev.trade_side_reason,
                    confluence_class=ev.confluence_class,
                    trigger_ts_ns=trig,
                    trigger_price=tprice,
                    trigger_reason=ev.trigger_reason,
                    horizon_s=int(h),
                    is_censored=cens,
                    censor_reason=reason,
                    outcome_status=status,
                    mfe_bps_gross=float(mfe) if status == "OK" else None,
                    mae_bps_gross=float(mae) if status == "OK" else None,
                    gross_return_bps=gross,
                    net_return_bps_0=net0,
                    net_return_bps_8=net8,
                    net_return_bps_12=net12,
                    tp_sl_results=tpsl,
                    semantics_hash=semantics_hash,
                    session_utc=sess,
                )
            )
    return rows
