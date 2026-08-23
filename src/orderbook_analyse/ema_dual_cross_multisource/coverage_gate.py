"""Coverage gate — missing data never interpreted as neutral."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ..cluster_sweep_research.feature_enrichment import source_window_status
from .config import EMA_DUAL_CROSS_DEFAULTS, EmaDualCrossConfig
from .timeframes import bar_close as compute_bar_close, timeframe_duration


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _slice_df(df: pd.DataFrame | None, start: datetime, end: datetime, col: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    mcol = col if col in df.columns else ("minute" if "minute" in df.columns else "open_time")
    a = pd.Timestamp(_utc(start).replace(tzinfo=None))
    b = pd.Timestamp(_utc(end).replace(tzinfo=None))
    t = pd.to_datetime(df[mcol])
    if t.dt.tz is not None:
        t = t.dt.tz_convert("UTC").dt.tz_localize(None)
    return df[(t >= a) & (t < b)]


def _ob_stale(last_ts: datetime | None, decision_at: datetime, stale_minutes: int) -> bool:
    if last_ts is None:
        return True
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    return (_utc(decision_at) - _utc(last_ts)).total_seconds() > stale_minutes * 60


def _record_window(
    out: dict[str, Any],
    key: str,
    status: str,
    sl: pd.DataFrame,
    *,
    critical: bool,
    note: str | None = None,
) -> None:
    first_ts = last_ts = None
    if len(sl):
        col = sl.columns[0]
        ts_col = pd.to_datetime(sl[col])
        first_ts = str(ts_col.min())
        last_ts = str(ts_col.max())
    rec: dict[str, Any] = {
        "status": status,
        "row_count": len(sl),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "critical_for_allow": critical,
    }
    if note:
        rec["note"] = note
    out[key] = rec


def assess_coverage(
    *,
    candidate_at: datetime,
    symbol: str,
    candles_df: pd.DataFrame | None,
    trades_1m: pd.DataFrame | None,
    ob_1m: pd.DataFrame | None,
    oi_1m: pd.DataFrame | None,
    liq: pd.DataFrame | None,
    lld_status: str,
    window_report: dict[str, Any] | None = None,
    cfg: EmaDualCrossConfig | None = None,
    timeframe: str = "15m",
    decision_at: datetime | None = None,
) -> dict[str, Any]:
    cfg = cfg or EMA_DUAL_CROSS_DEFAULTS
    bar_open = _utc(candidate_at)
    bar_close_ts = _utc(decision_at) if decision_at else compute_bar_close(bar_open, timeframe)
    pre60 = bar_open - timedelta(minutes=60)
    pre_tf = bar_open - timeframe_duration(timeframe)
    win_kw = {"bar_open": bar_open, "bar_close": bar_close_ts}

    out: dict[str, Any] = {
        "symbol": symbol,
        "candidate_at": bar_open.isoformat(),
        "decision_at": bar_close_ts.isoformat(),
        "bar_close": bar_close_ts.isoformat(),
        "timeframe": timeframe,
        "policy": {
            "require_candles": cfg.require_candles,
            "require_trades_for_allow": cfg.require_trades_for_allow,
            "require_ob_for_allow": cfg.require_ob_for_allow,
            "require_oi_for_allow": cfg.require_oi_for_allow,
            "require_liq_for_allow": cfg.require_liq_for_allow,
            "ob_stale_minutes": cfg.ob_stale_minutes,
            "missing_never_null": True,
        },
    }
    if window_report:
        out["window_report"] = window_report

    candle_sl = _slice_df(candles_df, pre60, bar_close_ts, "open_time")
    candle_status = "MISSING" if candle_sl.empty else "VALID"
    _record_window(out, "candles", candle_status, candle_sl, critical=cfg.require_candles)

    tr_st, tr_sl = source_window_status(trades_1m, "minute", bar_open, bar_close_ts, window_role="cross", **win_kw)
    _record_window(out, "public_trades_cross", tr_st, tr_sl, critical=cfg.require_trades_for_allow)
    tr_pre_st, tr_pre_sl = source_window_status(trades_1m, "minute", pre60, bar_open, window_role="baseline", **win_kw)
    _record_window(out, "public_trades_baseline", tr_pre_st, tr_pre_sl, critical=False)

    ob_st, ob_sl = source_window_status(ob_1m, "minute", bar_open, bar_close_ts, window_role="cross", **win_kw)
    ob_status = ob_st
    ob_stale = False
    if ob_1m is not None and not ob_1m.empty:
        mcol = "minute" if "minute" in ob_1m.columns else "open_time"
        t_dec = pd.Timestamp(_utc(bar_close_ts).replace(tzinfo=None))
        hist = ob_1m[pd.to_datetime(ob_1m[mcol]) <= t_dec]
        if hist.empty:
            ob_status = "MISSING"
        else:
            last_ob = pd.to_datetime(hist.iloc[-1][mcol])
            if last_ob.tzinfo is None:
                last_ob = last_ob.replace(tzinfo=timezone.utc)
            ob_stale = _ob_stale(last_ob.to_pydatetime(), bar_close_ts, cfg.ob_stale_minutes)
            if ob_stale:
                ob_status = "STALE"
            elif ob_st == "MISSING":
                ob_status = "MISSING"
            elif len(ob_sl) == 0:
                ob_status = "EMPTY_WINDOW"
            else:
                ob_status = "VALID"
    _record_window(out, "orderbook_ob200_v3", ob_status, ob_sl, critical=cfg.require_ob_for_allow)
    out["orderbook_ob200_v3"]["stale"] = ob_stale

    oi_st, oi_sl = source_window_status(oi_1m, "minute", pre_tf, bar_open, window_role="pre", **win_kw)
    if oi_st == "VALID" and len(oi_sl) < 2:
        oi_st = "EMPTY_WINDOW"
    _record_window(out, "open_interest", oi_st, oi_sl, critical=cfg.require_oi_for_allow)

    liq_st, liq_sl = source_window_status(liq, "event_time", pre_tf, bar_open, window_role="pre", **win_kw)
    if liq is None:
        liq_st = "MISSING"
    elif liq.empty:
        liq_st = "EMPTY_TABLE_SLICE"
    _record_window(
        out,
        "liquidations",
        liq_st,
        liq_sl,
        critical=cfg.require_liq_for_allow,
        note="EMPTY_WINDOW only when source covers window but no events; MISSING when feed starts after bar_open",
    )

    out["liquidity_locations"] = {
        "status": lld_status,
        "critical_for_allow": False,
    }

    critical_missing = []
    for key in ("candles", "public_trades_cross", "orderbook_ob200_v3", "open_interest", "liquidations"):
        rec = out.get(key) or {}
        if not rec.get("critical_for_allow"):
            continue
        st = rec.get("status")
        if st in ("MISSING", "STALE", "EMPTY_TABLE_SLICE"):
            critical_missing.append(key)
        elif st == "PARTIAL":
            critical_missing.append(key)
    out["critical_missing"] = critical_missing
    partial_sources = [k for k, v in out.items() if isinstance(v, dict) and v.get("status") == "PARTIAL"]
    out["partial_sources"] = partial_sources
    out["coverage_gate"] = "INCONCLUSIVE_DATA" if critical_missing else "PASS"
    return out
