"""Optional orderflow enrichment windows (measure-only; missing ≠ zero)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .models import SetupDirection, SweepEvent


def _status(n: int, expected: int | None = None) -> str:
    if n <= 0:
        return "MISSING"
    if expected and n < expected * 0.9:
        return "PARTIAL"
    return "VALID"


def enrich_event_orderflow(
    event: SweepEvent,
    *,
    trades_1m: pd.DataFrame | None,
    ob_1m: pd.DataFrame | None,
    oi_1m: pd.DataFrame | None,
    liq: pd.DataFrame | None,
    windows_minutes: tuple[int, ...] = (5, 15),
) -> SweepEvent:
    """Attach coverage-aware orderflow features around contact/entry times."""
    cov: dict[str, Any] = {
        "trades": _status(0 if trades_1m is None or trades_1m.empty else len(trades_1m)),
        "orderbook": _status(0 if ob_1m is None or ob_1m.empty else len(ob_1m)),
        "oi": _status(0 if oi_1m is None or oi_1m.empty else len(oi_1m)),
        "liquidations": (
            "MISSING"
            if liq is None
            else ("VALID" if len(liq) else "EMPTY_TABLE_SLICE")
        ),
        "liquidations_note": (
            "EMPTY_TABLE_SLICE means no rows in window — not proof of zero market liquidations"
            if liq is not None and len(liq) == 0
            else None
        ),
    }
    event.coverage = cov
    if event.t_entry is None and event.t_first_touch is None:
        return event

    t0 = event.t_entry or event.t_first_touch
    assert t0 is not None
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)

    feats: dict[str, Any] = {}
    for w in windows_minutes:
        pre_a, pre_b = t0 - timedelta(minutes=w), t0
        dur_a, dur_b = t0, t0 + timedelta(minutes=w)
        feats[f"pre_{w}m"] = _window_feats(trades_1m, ob_1m, oi_1m, liq, pre_a, pre_b, event.setup_direction)
        feats[f"during_{w}m"] = _window_feats(trades_1m, ob_1m, oi_1m, liq, dur_a, dur_b, event.setup_direction)

    # Explicit audit windows: before contact / during sweep / after until confirmation
    conf_t = event.t_reclaim_or_reject or event.t_earliest_entry
    if conf_t is not None and conf_t.tzinfo is None:
        conf_t = conf_t.replace(tzinfo=timezone.utc)
    sweep_end = conf_t or (t0 + timedelta(minutes=15))
    feats["before_contact"] = _window_feats(
        trades_1m, ob_1m, oi_1m, liq, t0 - timedelta(minutes=15), t0, event.setup_direction
    )
    feats["during_sweep"] = _window_feats(trades_1m, ob_1m, oi_1m, liq, t0, sweep_end, event.setup_direction)
    if conf_t is not None:
        feats["after_sweep_to_confirmation"] = _window_feats(
            trades_1m, ob_1m, oi_1m, liq, t0, conf_t, event.setup_direction
        )
    else:
        feats["after_sweep_to_confirmation"] = {
            "status": "INCONCLUSIVE",
            "note": "No confirmation time — window not defined",
            "trades_status": "INCONCLUSIVE",
            "ob_status": "INCONCLUSIVE",
            "oi_status": "INCONCLUSIVE",
            "liq_status": "INCONCLUSIVE",
        }
    event.features["orderflow"] = feats
    return event


def _naive_ts(dt: datetime) -> pd.Timestamp:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return pd.Timestamp(dt.replace(tzinfo=None))


def _ts_col(df: pd.DataFrame, mcol: str) -> pd.Series:
    col = mcol if mcol in df.columns else ("minute" if "minute" in df.columns else "open_time")
    ts = pd.to_datetime(df[col])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts


def source_window_status(
    df: pd.DataFrame | None,
    mcol: str,
    a: datetime,
    b: datetime,
    *,
    bar_open: datetime | None = None,
    bar_close: datetime | None = None,
    window_role: str | None = None,
) -> tuple[str, pd.DataFrame]:
    """Classify source availability for half-open window [a, b)."""
    if df is None or df.empty:
        return "MISSING", pd.DataFrame()
    col = mcol if mcol in df.columns else ("minute" if "minute" in df.columns else "open_time")
    ts = _ts_col(df, col)
    a_ts, b_ts = _naive_ts(a), _naive_ts(b)
    min_ts = ts.min()
    if window_role in ("baseline", "pre") and bar_open is not None:
        if min_ts > _naive_ts(bar_open):
            return "MISSING", pd.DataFrame()
    elif window_role == "cross" and bar_close is not None:
        if min_ts >= _naive_ts(bar_close):
            return "MISSING", pd.DataFrame()
    elif min_ts > a_ts:
        return "MISSING", pd.DataFrame()
    sl = df[(ts >= a_ts) & (ts < b_ts)]
    if sl.empty:
        return "EMPTY_WINDOW", sl
    return "VALID", sl


def _window_feats(
    trades: pd.DataFrame | None,
    ob: pd.DataFrame | None,
    oi: pd.DataFrame | None,
    liq: pd.DataFrame | None,
    a: datetime,
    b: datetime,
    direction: SetupDirection,
    *,
    bar_open: datetime | None = None,
    bar_close: datetime | None = None,
    window_role: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"window_start": a.isoformat(), "window_end": b.isoformat()}
    if window_role:
        out["window_role"] = window_role
    kw = {"bar_open": bar_open, "bar_close": bar_close, "window_role": window_role}

    st, sl = source_window_status(trades, "minute", a, b, **kw)
    if st == "VALID":
        bn = float(sl["buy_notional"].sum()) if "buy_notional" in sl else None
        sn = float(sl["sell_notional"].sum()) if "sell_notional" in sl else None
        out["buy_notional"] = bn
        out["sell_notional"] = sn
        out["trade_count"] = int(sl["trade_count"].sum()) if "trade_count" in sl else len(sl)
        if bn is not None and sn is not None and (bn + sn) > 0:
            out["taker_buy_ratio"] = bn / (bn + sn)
            out["delta"] = bn - sn
    out["trades_status"] = st

    st, sl = source_window_status(ob, "minute", a, b, **kw)
    if st == "VALID" and len(sl) and "imbalance_l50" in sl.columns:
        out["imbalance_l50_mean"] = float(sl["imbalance_l50"].mean())
        out["spread_bps_mean"] = float(sl["spread_bps"].mean()) if "spread_bps" in sl else None
        out["ob_status"] = "VALID"
    else:
        out["ob_status"] = st if st != "VALID" else "EMPTY_WINDOW"

    st, sl = source_window_status(oi, "minute", a, b, **kw)
    if st == "VALID" and "open_interest" in sl.columns and len(sl) >= 2:
        out["oi_change"] = float(sl["open_interest"].iloc[-1] - sl["open_interest"].iloc[0])
        out["oi_status"] = "VALID"
    else:
        out["oi_status"] = st if st != "VALID" else "EMPTY_WINDOW"

    if liq is not None:
        if liq.empty:
            out["liq_status"] = "EMPTY_TABLE_SLICE"
            out["liq_long_notional"] = None
            out["liq_short_notional"] = None
        else:
            st, sl = source_window_status(liq, "event_time", a, b, **kw)
            if st == "VALID":
                out["liq_long_notional"] = float(
                    sl.loc[sl["side"] == "LIQUIDATED_LONG", "notional"].sum()
                ) if "side" in sl.columns else None
                out["liq_short_notional"] = float(
                    sl.loc[sl["side"] == "LIQUIDATED_SHORT", "notional"].sum()
                ) if "side" in sl.columns else None
                out["liq_status"] = "VALID"
            else:
                out["liq_status"] = st
                out["liq_long_notional"] = None
                out["liq_short_notional"] = None
    else:
        out["liq_status"] = "MISSING"
    out["setup_direction"] = direction.value
    return out
