"""PRECHECK: detection coverage + future public-trade outcome coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.research.general_market_behavior_v1.coverage import load_clickhouse_env

from ..interval_coverage import _q, check_interval_coverage
from ..timeparse import format_utc_z
from . import MAX_OUTCOME_HORIZON_SECONDS


def check_future_public_trade_coverage(
    *,
    symbol: str,
    feature_end: datetime,
    horizon_seconds: int = MAX_OUTCOME_HORIZON_SECONDS,
) -> dict[str, Any]:
    """Require PT source availability through feature_end + horizon_seconds."""
    load_clickhouse_env()
    symbol = symbol.upper()
    feature_end = feature_end.astimezone(timezone.utc)
    need_through = feature_end + timedelta(seconds=int(horizon_seconds))
    hs = feature_end.strftime("%Y-%m-%d %H:%M:%S")
    he = need_through.strftime("%Y-%m-%d %H:%M:%S")
    tip_raw = _q(
        f"SELECT max(trade_ts) FROM orderbook_analysis.public_trades_canonical "
        f"WHERE symbol='{symbol}' FORMAT TSV"
    ).strip()
    n_future = int(
        _q(
            "SELECT count() FROM orderbook_analysis.public_trades_canonical "
            f"WHERE symbol='{symbol}' AND trade_ts>='{hs}' AND trade_ts<'{he}' FORMAT TSV"
        )
        or 0
    )
    tip = None
    tip_ok = False
    if tip_raw and not tip_raw.startswith("1970"):
        tip = tip_raw.replace(" ", "T")
        if not tip.endswith("Z"):
            tip = tip + "Z" if "+" not in tip else tip
        try:
            tip_dt = datetime.fromisoformat(tip_raw.replace("Z", "+00:00"))
            if tip_dt.tzinfo is None:
                tip_dt = tip_dt.replace(tzinfo=timezone.utc)
            tip_ok = tip_dt >= need_through
            tip = tip_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            tip_ok = False
    ok = tip_ok
    return {
        "ok": ok,
        "symbol": symbol,
        "feature_end": format_utc_z(feature_end),
        "need_through": format_utc_z(need_through),
        "horizon_seconds": int(horizon_seconds),
        "ch_tip_trade_ts": tip,
        "n_trades_in_outcome_future_window": n_future,
        "reason": None if ok else "FUTURE_PUBLIC_TRADE_COVERAGE_INCOMPLETE",
    }


def run_precheck(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    coverage_policy: str = "STRICT_WHOLE_WINDOW",
    focus_ts: datetime | None = None,
) -> dict[str, Any]:
    """Run coverage precheck.

    ``coverage_policy``:
      - ``STRICT_WHOLE_WINDOW`` (default): legacy whole-window fail-closed
      - ``LOCALIZED_EXCLUSION_V1``: usable/excluded spans; OI stale localized
    """
    future_pt = check_future_public_trade_coverage(symbol=symbol, feature_end=end)

    if coverage_policy == "LOCALIZED_EXCLUSION_V1":
        from ..localized_coverage.check import check_localized_coverage

        loc = check_localized_coverage(
            symbol=symbol, start=start, end=end, focus_ts=focus_ts
        )
        loc["future_public_trades"] = future_pt
        ok = bool(loc.get("analysis_ok")) and bool(future_pt.get("ok"))
        missing = list(loc.get("missing_intervals") or [])
        if not future_pt.get("ok"):
            missing = missing + [
                {
                    "source": "PUBLIC_TRADES_OUTCOME_FUTURE",
                    "start": future_pt["feature_end"],
                    "end": future_pt["need_through"],
                    "detail": future_pt.get("reason") or "tip_before_need_through",
                }
            ]
        if focus_ts is not None and (loc.get("focus") or {}).get("status") != "ELIGIBLE":
            missing = missing + [
                {
                    "source": "FOCUS_TS",
                    "start": (loc.get("focus") or {}).get("focus_ts"),
                    "end": (loc.get("focus") or {}).get("focus_ts"),
                    "detail": ",".join((loc.get("focus") or {}).get("reasons") or ["NOT_ELIGIBLE"]),
                    "blocking_interval": (loc.get("focus") or {}).get("blocking_interval"),
                }
            ]
        # Map localized verdict into precheck ok semantics:
        # DATA_COMPLETE / DATA_USABLE_WITH_EXCLUSIONS → ok (if analysis_ok)
        verdict = loc.get("verdict")
        if not future_pt.get("ok"):
            verdict = "DATA_NOT_COMPLETE"
            ok = False
        if focus_ts is not None and not ok:
            verdict = "DATA_NOT_COMPLETE"
        return {
            "ok": ok,
            "coverage": loc.get("coverage") or loc,
            "localized": loc,
            "future_public_trades": future_pt,
            "missing_intervals": missing,
            "verdict": verdict if ok else "DATA_NOT_COMPLETE",
            "coverage_policy": coverage_policy,
            "focus_ts": None if focus_ts is None else focus_ts.isoformat().replace("+00:00", "Z"),
        }

    coverage = check_interval_coverage(symbol=symbol, start=start, end=end)
    ok = coverage.get("verdict") == "DATA_COMPLETE" and bool(future_pt.get("ok"))
    missing = list(coverage.get("missing_intervals") or [])
    if not future_pt.get("ok"):
        missing = missing + [
            {
                "source": "PUBLIC_TRADES_OUTCOME_FUTURE",
                "start": future_pt["feature_end"],
                "end": future_pt["need_through"],
                "detail": future_pt.get("reason") or "tip_before_need_through",
            }
        ]
    return {
        "ok": ok,
        "coverage": coverage,
        "future_public_trades": future_pt,
        "missing_intervals": missing,
        "verdict": "DATA_COMPLETE" if ok else "DATA_NOT_COMPLETE",
        "coverage_policy": "STRICT_WHOLE_WINDOW",
    }
