"""Candidate- and window-level coverage semantics (research-only; MISSING ≠ NEUTRAL)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

# Window-level OI: FULL only when span covers nearly the whole research window.
OI_FULL_RATIO = 0.90
OI_SPAN_EDGE_TOLERANCE = timedelta(days=1)

# Local feature windows for core sources at decision_at
LOCAL_LOOKBACK_MINUTES = 60
LOCAL_OB_MIN_MINUTES = 5
LOCAL_TRADES_MIN_MINUTES = 1
LOCAL_OB_STALE_MINUTES = 5


def _utc(dt: datetime | str | None) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_oi_window(
    *,
    oi_minutes: int,
    expected_minutes: int,
    oi_first_ts: datetime | str | None,
    oi_last_ts: datetime | str | None,
    window_start: datetime | str,
    window_end: datetime | str,
) -> dict[str, Any]:
    """Window-level OI: never mark FULL merely because a sub-window has rows."""
    start, end = _utc(window_start), _utc(window_end)
    assert start is not None and end is not None
    first, last = _utc(oi_first_ts), _utc(oi_last_ts)
    ratio = (oi_minutes / expected_minutes) if expected_minutes else 0.0
    if oi_minutes <= 0 or first is None or last is None:
        status = "MISSING"
    else:
        spans_start = first <= start + OI_SPAN_EDGE_TOLERANCE
        spans_end = last >= end - OI_SPAN_EDGE_TOLERANCE
        if ratio >= OI_FULL_RATIO and spans_start and spans_end:
            status = "FULL"
        else:
            status = "PARTIAL"
    return {
        "oi_first_ts": first.isoformat() if first else None,
        "oi_last_ts": last.isoformat() if last else None,
        "oi_expected_minutes": int(expected_minutes),
        "oi_minutes": int(oi_minutes),
        "oi_coverage_ratio": round(ratio, 6),
        "oi_window_status": status,
        # Never advertise partial OI as global VALID
        "oi_status": status if status != "FULL" else "VALID",
        "oi_treated_as": "MISSING" if status == "MISSING" else status,
    }


def oi_status_at_decision(
    decision_at: datetime | str,
    *,
    oi_first_ts: datetime | str | None,
    oi_last_ts: datetime | str | None,
    has_rows_in_feature_window: bool | None = None,
) -> str:
    """Per-candidate OI: before feed start → MISSING; after last / gap → MISSING/STALE."""
    dec = _utc(decision_at)
    first, last = _utc(oi_first_ts), _utc(oi_last_ts)
    assert dec is not None
    if first is None or last is None:
        return "MISSING"
    if dec < first:
        return "MISSING"
    if dec > last:
        return "MISSING"
    if has_rows_in_feature_window is False:
        return "STALE"
    if has_rows_in_feature_window is None:
        return "VALID"
    return "VALID" if has_rows_in_feature_window else "STALE"


def classify_liq_feed(
    *,
    liq_feed_first_ts: datetime | str | None,
    liq_feed_last_ts: datetime | str | None,
    liquidation_events: int,
    window_start: datetime | str,
    window_end: datetime | str,
    inferred_from_events: bool = True,
) -> dict[str, Any]:
    """Separate feed coverage from event count. Event count alone ≠ coverage proof."""
    start, end = _utc(window_start), _utc(window_end)
    assert start is not None and end is not None
    first, last = _utc(liq_feed_first_ts), _utc(liq_feed_last_ts)
    n = int(liquidation_events)
    if first is None or last is None or n < 0:
        feed_status = "MISSING"
    else:
        spans_start = first <= start + OI_SPAN_EDGE_TOLERANCE
        spans_end = last >= end - OI_SPAN_EDGE_TOLERANCE
        if spans_start and spans_end:
            feed_status = "FULL"
        else:
            feed_status = "PARTIAL"
    return {
        "liq_feed_first_ts": first.isoformat() if first else None,
        "liq_feed_last_ts": last.isoformat() if last else None,
        "liq_feed_coverage_status": feed_status,
        "liquidation_events": n,
        "liq_feed_inferred_from_events": inferred_from_events,
        # Global preflight must not say VALID for partial feed
        "liquidations_status": feed_status if feed_status != "FULL" else "VALID",
        "liq_treated_as": "MISSING" if feed_status == "MISSING" else feed_status,
    }


def liq_status_at_decision(
    decision_at: datetime | str,
    *,
    liq_feed_first_ts: datetime | str | None,
    liq_feed_last_ts: datetime | str | None,
    events_in_feature_window: int,
) -> str:
    """MISSING if feed inactive; VALID_EMPTY if feed ok but zero events; VALID_DATA if events."""
    dec = _utc(decision_at)
    first, last = _utc(liq_feed_first_ts), _utc(liq_feed_last_ts)
    assert dec is not None
    if first is None or last is None:
        return "MISSING"
    if dec < first:
        return "MISSING"
    if dec > last:
        return "MISSING"
    if int(events_in_feature_window) <= 0:
        return "VALID_EMPTY"
    return "VALID_DATA"


def local_series_status(
    df: pd.DataFrame | None,
    *,
    time_col: str,
    decision_at: datetime | str,
    lookback_minutes: int = LOCAL_LOOKBACK_MINUTES,
    min_points: int = 1,
    stale_minutes: int = LOCAL_OB_STALE_MINUTES,
) -> str:
    """Local causal window check: MISSING / STALE / INSUFFICIENT / VALID. No future rows."""
    dec = _utc(decision_at)
    assert dec is not None
    if df is None or df.empty:
        return "MISSING"
    col = time_col if time_col in df.columns else ("minute" if "minute" in df.columns else "open_time")
    tcol = pd.to_datetime(df[col])
    if getattr(tcol.dt, "tz", None) is not None:
        dec_ts = pd.Timestamp(dec)
        # never use rows after decision
        hist = df.loc[tcol <= dec_ts]
        t_hist = pd.to_datetime(hist[col])
    else:
        dec_naive = dec.replace(tzinfo=None)
        hist = df.loc[tcol <= pd.Timestamp(dec_naive)]
        t_hist = pd.to_datetime(hist[col])
    if hist.empty:
        return "MISSING"
    last = t_hist.max()
    last_dt = last.to_pydatetime()
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    else:
        last_dt = last_dt.astimezone(timezone.utc)
    if (dec - last_dt).total_seconds() > stale_minutes * 60:
        return "STALE"
    win_start = dec - timedelta(minutes=lookback_minutes)
    if getattr(t_hist.dt, "tz", None) is not None:
        local = hist.loc[(t_hist >= pd.Timestamp(win_start)) & (t_hist <= pd.Timestamp(dec))]
    else:
        local = hist.loc[
            (t_hist >= pd.Timestamp(win_start.replace(tzinfo=None)))
            & (t_hist <= pd.Timestamp(dec.replace(tzinfo=None)))
        ]
    if len(local) < min_points:
        return "INSUFFICIENT"
    return "VALID"


def refine_coverage_dict(
    cov: dict[str, Any],
    *,
    decision_at: datetime | str,
    feed_meta: dict[str, Any] | None,
    trades_1m: pd.DataFrame | None = None,
    ob_1m: pd.DataFrame | None = None,
    oi_1m: pd.DataFrame | None = None,
    liq: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Overlay feed-span + local-gap semantics onto assess_coverage output."""
    out = dict(cov)
    meta = feed_meta or {}
    dec = _utc(decision_at)
    assert dec is not None

    # Local core sources
    trades_local = local_series_status(
        trades_1m, time_col="minute", decision_at=dec, min_points=LOCAL_TRADES_MIN_MINUTES
    )
    ob_local = local_series_status(
        ob_1m, time_col="minute", decision_at=dec, min_points=LOCAL_OB_MIN_MINUTES
    )
    out["public_trades_cross_local"] = {"status": trades_local}
    out["orderbook_ob200_v3_local"] = {"status": ob_local}
    if trades_local in ("MISSING", "STALE", "INSUFFICIENT"):
        prev = dict(out.get("public_trades_cross") or {})
        prev["status"] = trades_local if trades_local != "INSUFFICIENT" else "MISSING"
        prev["local_status"] = trades_local
        out["public_trades_cross"] = prev
    if ob_local in ("MISSING", "STALE", "INSUFFICIENT"):
        prev = dict(out.get("orderbook_ob200_v3") or {})
        prev["status"] = ob_local if ob_local != "INSUFFICIENT" else "MISSING"
        prev["local_status"] = ob_local
        out["orderbook_ob200_v3"] = prev

    # OI feed span
    oi_first = meta.get("oi_first_ts")
    oi_last = meta.get("oi_last_ts")
    has_oi = None
    if oi_1m is not None and not oi_1m.empty:
        mcol = "minute" if "minute" in oi_1m.columns else oi_1m.columns[0]
        tcol = pd.to_datetime(oi_1m[mcol])
        pre = dec - timedelta(minutes=LOCAL_LOOKBACK_MINUTES)
        if getattr(tcol.dt, "tz", None) is not None:
            sl = oi_1m.loc[(tcol >= pd.Timestamp(pre)) & (tcol <= pd.Timestamp(dec))]
        else:
            sl = oi_1m.loc[
                (tcol >= pd.Timestamp(pre.replace(tzinfo=None)))
                & (tcol <= pd.Timestamp(dec.replace(tzinfo=None)))
            ]
        has_oi = len(sl) >= 2
    oi_st = oi_status_at_decision(
        dec, oi_first_ts=oi_first, oi_last_ts=oi_last, has_rows_in_feature_window=has_oi
    )
    oi_rec = dict(out.get("open_interest") or {})
    oi_rec["status"] = oi_st
    oi_rec["feed_first_ts"] = oi_first
    oi_rec["feed_last_ts"] = oi_last
    oi_rec["never_neutral"] = True
    out["open_interest"] = oi_rec

    # Liquidations feed span
    liq_first = meta.get("liq_feed_first_ts")
    liq_last = meta.get("liq_feed_last_ts")
    n_events = 0
    if liq is not None and not liq.empty and "event_time" in liq.columns:
        tcol = pd.to_datetime(liq["event_time"])
        pre = dec - timedelta(minutes=LOCAL_LOOKBACK_MINUTES)
        if getattr(tcol.dt, "tz", None) is not None:
            sl = liq.loc[(tcol >= pd.Timestamp(pre)) & (tcol < pd.Timestamp(dec))]
        else:
            sl = liq.loc[
                (tcol >= pd.Timestamp(pre.replace(tzinfo=None)))
                & (tcol < pd.Timestamp(dec.replace(tzinfo=None)))
            ]
        n_events = len(sl)
    liq_st = liq_status_at_decision(
        dec,
        liq_feed_first_ts=liq_first,
        liq_feed_last_ts=liq_last,
        events_in_feature_window=n_events,
    )
    # Map VALID_DATA → VALID for downstream that expects VALID; keep VALID_EMPTY distinct
    mapped = "VALID" if liq_st == "VALID_DATA" else liq_st
    liq_rec = dict(out.get("liquidations") or {})
    liq_rec["status"] = mapped
    liq_rec["liq_semantic"] = liq_st
    liq_rec["feed_first_ts"] = liq_first
    liq_rec["feed_last_ts"] = liq_last
    liq_rec["never_neutral"] = True
    out["liquidations"] = liq_rec

    # Core insufficiency from local OB/trades gaps
    core_local_bad = trades_local in ("MISSING", "STALE", "INSUFFICIENT") or ob_local in (
        "MISSING",
        "STALE",
        "INSUFFICIENT",
    )
    out["core_local_insufficient"] = core_local_bad
    return out


def listing_audit(
    *,
    earliest_candle_unbounded: datetime | str | None,
    window_start: datetime | str,
    window_bounded_first_ts: datetime | str | None = None,
) -> dict[str, Any]:
    """Listing must not be inferred solely from [start,end) first_ts."""
    start = _utc(window_start)
    assert start is not None
    earliest = _utc(earliest_candle_unbounded)
    _ = _utc(window_bounded_first_ts)  # retained for audits / callers

    if earliest is None:
        return {
            "listing_status": "UNKNOWN",
            "listing_first_ts": None,
            "listing_limited": None,
            "listing_limited_known": False,
            "listing_note": "earliest_candle_unavailable",
        }

    # Exact equality to window start is unreliable (common when probes are window-scoped).
    if earliest == start:
        return {
            "listing_status": "UNKNOWN",
            "listing_first_ts": earliest.isoformat(),
            "listing_limited": None,
            "listing_limited_known": False,
            "listing_note": "earliest_equals_window_start_unreliable",
        }

    limited = earliest > start + timedelta(days=1)
    return {
        "listing_status": "KNOWN",
        "listing_first_ts": earliest.isoformat(),
        "listing_limited": limited,
        "listing_limited_known": True,
        "listing_note": None if not limited else f"listed_after_window_start:{earliest.isoformat()}",
    }


def outcome_horizon_complete(
    candles_1m: pd.DataFrame | None,
    *,
    entry_at: datetime | str,
    horizon_min: int,
) -> bool:
    """True iff 1m path covers the full [entry, entry+horizon) window."""
    entry = _utc(entry_at)
    assert entry is not None
    if candles_1m is None or candles_1m.empty:
        return False
    horizon_end = entry + timedelta(minutes=int(horizon_min))
    tcol = pd.to_datetime(candles_1m["open_time"])
    if getattr(tcol.dt, "tz", None) is not None:
        mask = (tcol >= pd.Timestamp(entry)) & (tcol < pd.Timestamp(horizon_end))
    else:
        mask = (tcol >= pd.Timestamp(entry.replace(tzinfo=None))) & (
            tcol < pd.Timestamp(horizon_end.replace(tzinfo=None))
        )
    path = candles_1m.loc[mask]
    if path.empty:
        return False
    # Need a bar for each minute of the horizon (allow tiny gaps ≤0 for strictness: count)
    return int(len(path)) >= int(horizon_min)
