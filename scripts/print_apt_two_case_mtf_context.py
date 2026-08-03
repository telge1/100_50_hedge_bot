#!/usr/bin/env python3
"""Read-only causal two-case audit: APT_002 vs APT_003 5m PL breaks vs known 1h/4h context.

Reads only existing artefacts. No scanner, no DB, no writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MTF_DIR = ROOT / "results" / "trend_scanner_multitimeframe_structure"
DEEP_DIR = ROOT / "results" / "c3_protected_low_event_driven_decision_deep_dive"
CATALOG_DIR = ROOT / "results" / "c3_protected_low_historical_event_catalog"
TZ_EAT = timezone(timedelta(hours=3))

CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "APT_002",
        "cluster": "BLK_APTUSDT_002",
        "event_id_deep": "APTUSDT_BLK_APTUSDT_002_PL_BREAK",
        "event_id_catalog": "APTUSDT_PL_20260731T023000_0p5689",
        "symbol": "APTUSDT",
        "pl": 0.5689,
        "break_candle_open": "2026-07-31T02:25:00Z",
        "signal_available_at": "2026-07-31T02:30:00Z",
        "expected_outcome": "BREAKDOWN_CONFIRMED",
    },
    {
        "case_id": "APT_003",
        "cluster": "BLK_APTUSDT_003",
        "event_id_deep": "APTUSDT_BLK_APTUSDT_003_PL_BREAK",
        "event_id_catalog": "APTUSDT_PL_20260802T035500_0p5613",
        "symbol": "APTUSDT",
        "pl": 0.5613,
        "break_candle_open": "2026-08-02T03:50:00Z",
        "signal_available_at": "2026-08-02T03:55:00Z",
        "expected_outcome": "RECLAIM_CONFIRMED",
    },
)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


def _fmt_utc(ts: Any) -> str:
    t = _parse_ts(ts)
    return t.strftime("%Y-%m-%d %H:%M:%SZ") if t else "n/a"


def _fmt_eat(ts: Any) -> str:
    t = _parse_ts(ts)
    return t.astimezone(TZ_EAT).strftime("%Y-%m-%d %H:%M EAT") if t else "n/a"


def _bps(price: float | None, level: float | None) -> float | None:
    if price is None or level is None or level == 0 or pd.isna(price) or pd.isna(level):
        return None
    return (float(price) - float(level)) / float(level) * 10_000.0


def _asof_row(df: pd.DataFrame, *, symbol: str, signal: datetime) -> pd.Series | None:
    g = df[(df["symbol"] == symbol) & (df["available_at"] <= signal)].sort_values(
        "available_at"
    )
    if g.empty:
        return None
    return g.iloc[-1]


def _last_break(
    breaks: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    signal: datetime,
) -> dict[str, Any] | None:
    g = breaks[
        (breaks["symbol"] == symbol)
        & (breaks["timeframe"] == timeframe)
        & (breaks["available_at"] <= signal)
    ].sort_values("available_at")
    if g.empty:
        return None
    r = g.iloc[-1]
    known = _parse_ts(r["available_at"])
    return {
        "level": float(r["level"]),
        "candle_open": _parse_ts(r["candle_open_ts"]),
        "known_at": known,
        "minutes_before_signal": (signal - known).total_seconds() / 60.0 if known else None,
        "close": float(r["close"]) if pd.notna(r.get("close")) else None,
        "choch": bool(r["choch"]) if "choch" in r.index and pd.notna(r["choch"]) else None,
    }


def _tf_snapshot(
    row: pd.Series | None,
    *,
    timeframe: str,
    signal: datetime,
    signal_price: float | None,
    last_pl_break: dict[str, Any] | None,
    last_ph_break: dict[str, Any] | None,
) -> dict[str, Any]:
    if row is None:
        return {
            "timeframe": timeframe,
            "available": False,
            "future_violation": False,
        }
    avail = _parse_ts(row["available_at"])
    open_ts = _parse_ts(row["timestamp"] if "timestamp" in row.index else row.get("candle_open_ts"))
    pl = float(row["protected_low"]) if pd.notna(row.get("protected_low")) else None
    ph = float(row["protected_high"]) if pd.notna(row.get("protected_high")) else None
    future = bool(avail is not None and avail > signal)
    return {
        "timeframe": timeframe,
        "available": True,
        "candle_open": open_ts,
        "available_at": avail,
        "lag_minutes_to_signal": (signal - avail).total_seconds() / 60.0 if avail else None,
        "future_violation": future,
        "trend_direction": int(row["trend_direction"])
        if pd.notna(row.get("trend_direction"))
        else (int(row["major_direction"]) if pd.notna(row.get("major_direction")) else None),
        "trend_state": str(row["trend_state"])
        if pd.notna(row.get("trend_state"))
        else str(row.get("protected_structure_state")),
        "trend_segment_id": row.get("trend_segment_id"),
        "protected_low": pl,
        "protected_high": ph,
        "close_break_protected_down": bool(row.get("close_break_protected_down"))
        if pd.notna(row.get("close_break_protected_down"))
        else None,
        "close_break_protected_up": bool(row.get("close_break_protected_up"))
        if pd.notna(row.get("close_break_protected_up"))
        else None,
        "bearish_choch": bool(row.get("bearish_choch"))
        if pd.notna(row.get("bearish_choch"))
        else None,
        "bullish_choch": bool(row.get("bullish_choch"))
        if pd.notna(row.get("bullish_choch"))
        else None,
        "external_bos_down": bool(row.get("external_bos_down"))
        if pd.notna(row.get("external_bos_down"))
        else None,
        "external_bos_up": bool(row.get("external_bos_up"))
        if pd.notna(row.get("external_bos_up"))
        else None,
        "close": float(row["close"]) if pd.notna(row.get("close")) else None,
        "distance_signal_to_pl_bps": _bps(signal_price, pl),
        "distance_signal_to_ph_bps": _bps(signal_price, ph),
        "last_pl_break": last_pl_break,
        "last_ph_break": last_ph_break,
    }


def classify_alignment(ctx: dict[str, Any]) -> tuple[str, str]:
    """Descriptive HTF alignment at 5m signal time (no new thresholds)."""
    s5, s1, s4 = ctx["tf"]["5m"], ctx["tf"]["1h"], ctx["tf"]["4h"]
    if not s1.get("available") or not s4.get("available"):
        return "HTF_UNAVAILABLE", "1h or 4h as-of row missing"

    # Bearish damage proxies (existing fields only)
    h1_pl_active = s1.get("protected_low") is not None
    h1_pl_breaking = bool(s1.get("close_break_protected_down"))
    h1_bearish_major = s1.get("trend_direction") == -1
    h1_bullish_major = s1.get("trend_direction") == 1
    h4_bearish = s4.get("trend_direction") == -1 or str(s4.get("trend_state") or "").startswith(
        "bearish"
    )
    h4_bullish = s4.get("trend_direction") == 1 or str(s4.get("trend_state") or "").startswith(
        "bullish"
    )
    five_bearish = bool(s5.get("close_break_protected_down")) or bool(s5.get("bearish_choch"))

    # Same-level: 5m breaking the still-active 1h PL
    same_pl = (
        s5.get("protected_low") is not None
        and s1.get("protected_low") is not None
        and abs(float(s5["protected_low"]) - float(s1["protected_low"])) < 1e-9
        and not h1_pl_breaking
    )

    h1_internal_bullish = "bullish_internal" in str(s1.get("trend_state") or "")

    if (
        five_bearish
        and h1_bearish_major
        and h4_bearish
        and (h1_pl_breaking or not h1_pl_active)
        and not h1_internal_bullish
    ):
        return (
            "FULL_BEARISH_ALIGNMENT",
            "5m PL-break with 1h already bearish-major and 4h bearish",
        )
    if five_bearish and same_pl and (h1_bullish_major or not h1_pl_breaking):
        return (
            "5M_BEARISH_AGAINST_1H",
            "5m breaks active 1h Protected Low that is not yet close-broken on 1h",
        )
    if five_bearish and h4_bullish and not h4_bearish:
        return "5M_BEARISH_AGAINST_4H", "5m bearish break while 4h still bullish-major"
    if five_bearish and h1_bearish_major and not h4_bearish and h4_bullish:
        return (
            "5M_BEARISH_1H_BEARISH_4H_INTACT",
            "5m+1h bearish while 4h still bullish/intact",
        )
    if five_bearish and (
        h1_bearish_major != h4_bearish
        or h1_internal_bullish
        or (h4_bearish and h1_bullish_major)
    ):
        return (
            "HTF_MIXED",
            f"1h state={s1.get('trend_state')} dir={s1.get('trend_direction')}; "
            f"4h state={s4.get('trend_state')} dir={s4.get('trend_direction')}",
        )
    return "HTF_MIXED", "default mixed / inconclusive HTF read"


def load_outcome(case: dict[str, Any]) -> dict[str, Any]:
    points = pd.read_csv(DEEP_DIR / "decision_points.csv")
    row = points[points["event_id"] == case["event_id_deep"]].iloc[0]
    failed = pd.read_csv(DEEP_DIR / "failed_reclaim_attempts.csv")
    failed = failed[failed["event_id"] == case["event_id_deep"]].sort_values("attempt_ts")
    fwd = pd.read_csv(DEEP_DIR / "candidate_forward_price_behavior.csv")
    fwd = fwd[fwd["event_id"] == case["event_id_deep"]]

    first_failed = None
    if not failed.empty:
        first_failed = {
            "attempt_ts": str(failed.iloc[0]["attempt_ts"]),
            "high_price": float(failed.iloc[0]["high_price"]),
            "rebreak_ts": str(failed.iloc[0]["rebreak_ts"]),
            "n_attempts_logged": int(len(failed)),
        }

    mfe_mae = {}
    if not fwd.empty:
        # prefer confirm / breakdown_confirm / decision_confirm row
        pref = fwd[
            fwd["candidate_type"].astype(str).str.contains("confirm", case=False, na=False)
        ]
        use = pref.iloc[0] if not pref.empty else fwd.iloc[0]
        mfe_mae = {
            "candidate_type": str(use["candidate_type"]),
            "candidate_ts": str(use["candidate_ts"]),
            "mfe_bps_15m": float(use["mfe_bps_900s"]) if pd.notna(use.get("mfe_bps_900s")) else None,
            "mae_bps_15m": float(use["mae_bps_900s"]) if pd.notna(use.get("mae_bps_900s")) else None,
            "mfe_bps_60m": float(use["mfe_bps_3600s"])
            if pd.notna(use.get("mfe_bps_3600s"))
            else None,
            "mae_bps_60s_proxy_note": "deep-dive CSV has mae through 900s; 60m MFE from mfe_bps_3600s",
        }

    # catalog 15m/60m if present
    cat_fwd = pd.read_csv(CATALOG_DIR / "candidate_forward_returns.csv")
    cat_fwd = cat_fwd[
        (cat_fwd["event_id"] == case["event_id_catalog"]) & (cat_fwd["horizon_m"].isin([15, 60]))
    ]
    catalog_mfe = []
    for _, r in cat_fwd.iterrows():
        if str(r["candidate_type"]) not in {"aggressive", "decision_confirm", "breakdown_confirm"}:
            if str(r["candidate_type"]) not in {"aggressive", "conservative"}:
                continue
        catalog_mfe.append(
            {
                "side": r["side"],
                "candidate_type": r["candidate_type"],
                "horizon_m": int(r["horizon_m"]),
                "mfe_bps": float(r["mfe_bps"]) if pd.notna(r["mfe_bps"]) else None,
                "mae_bps": float(r["mae_bps"]) if pd.notna(r["mae_bps"]) else None,
            }
        )

    reclaim_detail = None
    if case["expected_outcome"] == "RECLAIM_CONFIRMED":
        # confirm basis from case md is already known; pull first reclaim from decision_points
        reclaim_detail = {
            "first_reclaim_ts": str(row.get("first_reclaim_ts")),
            "retest_note": "confirm_basis.retest=rebreak_then_return_hold_60s (from event md)",
        }

    return {
        "outcome": str(row["decision_type"]),
        "decision_ts": str(row["decision_ts"]),
        "minutes_after_break": float(row["minutes_after_break"]),
        "price_at_decision": float(row["price_at_decision"]),
        "distance_from_level_bps": float(row["distance_from_level_bps"]),
        "state_before": str(row["state_before_decision"]),
        "first_reclaim_ts": str(row["first_reclaim_ts"])
        if pd.notna(row.get("first_reclaim_ts"))
        else None,
        "first_failed_reclaim": first_failed,
        "reclaim_detail": reclaim_detail,
        "forward_deep_dive": mfe_mae,
        "forward_catalog_aggressive": [
            x for x in catalog_mfe if x["candidate_type"] == "aggressive"
        ],
    }


def nearest_htf_level(ctx: dict[str, Any]) -> dict[str, Any]:
    """Closest active HTF PL/PH to signal price (descriptive)."""
    price = ctx["signal_price"]
    cands: list[dict[str, Any]] = []
    for tf in ("1h", "4h"):
        snap = ctx["tf"][tf]
        for kind, key in (("PL", "protected_low"), ("PH", "protected_high")):
            lvl = snap.get(key)
            if lvl is None:
                continue
            dist = _bps(price, float(lvl))
            cands.append(
                {
                    "timeframe": tf,
                    "kind": kind,
                    "level": float(lvl),
                    "distance_bps": dist,
                    "abs_bps": abs(dist) if dist is not None else None,
                }
            )
    if not cands:
        return {"label": "none", "level": None, "distance_bps": None}
    cands.sort(key=lambda x: x["abs_bps"] if x["abs_bps"] is not None else 1e18)
    best = cands[0]
    return {
        "label": f"{best['timeframe']} {best['kind']} {best['level']}",
        "level": best["level"],
        "distance_bps": best["distance_bps"],
        "timeframe": best["timeframe"],
        "kind": best["kind"],
    }


def analyze_case(
    case: dict[str, Any],
    *,
    s5: pd.DataFrame,
    s1: pd.DataFrame,
    s4: pd.DataFrame,
    pl_breaks: pd.DataFrame,
    ph_breaks: pd.DataFrame,
) -> dict[str, Any]:
    signal = _parse_ts(case["signal_available_at"])
    assert signal is not None
    symbol = case["symbol"]

    r5 = _asof_row(s5, symbol=symbol, signal=signal)
    r1 = _asof_row(s1, symbol=symbol, signal=signal)
    r4 = _asof_row(s4, symbol=symbol, signal=signal)
    signal_price = float(r5["close"]) if r5 is not None and pd.notna(r5.get("close")) else None

    tf: dict[str, Any] = {}
    for timeframe, row, src in (("5m", r5, s5), ("1h", r1, s1), ("4h", r4, s4)):
        last_pl = _last_break(pl_breaks, symbol=symbol, timeframe=timeframe, signal=signal)
        last_ph = _last_break(ph_breaks, symbol=symbol, timeframe=timeframe, signal=signal)
        tf[timeframe] = _tf_snapshot(
            row,
            timeframe=timeframe,
            signal=signal,
            signal_price=signal_price,
            last_pl_break=last_pl,
            last_ph_break=last_ph,
        )

    ctx = {
        "case": case,
        "signal_available_at": signal,
        "signal_price": signal_price,
        "tf": tf,
    }
    alignment, alignment_reason = classify_alignment(ctx)
    outcome = load_outcome(case)
    nearest = nearest_htf_level(ctx)

    # Explicit Q helpers
    h1 = tf["1h"]
    h4 = tf["4h"]
    pl_same = (
        h1.get("protected_low") is not None
        and abs(float(h1["protected_low"]) - float(case["pl"])) < 1e-9
    )
    answers = {
        "1h_bearish_aligned_at_signal": bool(
            h1.get("trend_direction") == -1
            or h1.get("close_break_protected_down")
            or (h1.get("protected_low") is None and "bearish" in str(h1.get("trend_state")))
        ),
        "1h_pl_still_active_same_as_5m": pl_same and not bool(h1.get("close_break_protected_down")),
        "4h_bearish_aligned_at_signal": bool(
            h4.get("trend_direction") == -1 or "bearish" in str(h4.get("trend_state"))
        ),
        "1h_pl_already_broken_before_signal": bool(
            h1.get("last_pl_break") is not None
            and h1["last_pl_break"]["minutes_before_signal"] is not None
            and h1["last_pl_break"]["minutes_before_signal"] > 0
            and h1.get("protected_low") is None
        ),
        "4h_pl_already_broken_before_signal": bool(
            h4.get("last_pl_break") is not None and h4.get("protected_low") is None
        ),
    }

    return {
        **ctx,
        "alignment": alignment,
        "alignment_reason": alignment_reason,
        "nearest_htf_level": nearest,
        "outcome": outcome,
        "answers": answers,
        "causality": {
            "no_future_1h": not bool(h1.get("future_violation")),
            "no_future_4h": not bool(h4.get("future_violation")),
            "1h_available_at": _fmt_utc(h1.get("available_at")),
            "4h_available_at": _fmt_utc(h4.get("available_at")),
            "1h_lag_min": h1.get("lag_minutes_to_signal"),
            "4h_lag_min": h4.get("lag_minutes_to_signal"),
        },
    }


def _print_tf(snap: dict[str, Any]) -> None:
    print(f"### {snap['timeframe']}")
    if not snap.get("available"):
        print("- unavailable\n")
        return
    print(f"- candle_open: `{_fmt_utc(snap['candle_open'])}` / `{_fmt_eat(snap['candle_open'])}`")
    print(f"- available_at: `{_fmt_utc(snap['available_at'])}` / `{_fmt_eat(snap['available_at'])}`")
    print(f"- lag to 5m signal: `{snap.get('lag_minutes_to_signal'):.1f} min`")
    print(f"- future_violation: `{snap.get('future_violation')}`")
    print(f"- trend_direction: `{snap.get('trend_direction')}`")
    print(f"- trend_state: `{snap.get('trend_state')}`")
    print(f"- trend_segment_id: `{snap.get('trend_segment_id')}`")
    print(f"- protected_low: `{snap.get('protected_low')}`")
    print(f"- protected_high: `{snap.get('protected_high')}`")
    print(f"- close_break_protected_down/up: `{snap.get('close_break_protected_down')}` / `{snap.get('close_break_protected_up')}`")
    print(f"- bearish_choch / bullish_choch: `{snap.get('bearish_choch')}` / `{snap.get('bullish_choch')}`")
    print(f"- external_bos_down/up: `{snap.get('external_bos_down')}` / `{snap.get('external_bos_up')}`")
    print(f"- dist signal→PL/PH bps: `{snap.get('distance_signal_to_pl_bps')}` / `{snap.get('distance_signal_to_ph_bps')}`")
    for label, key in (("last PL break", "last_pl_break"), ("last PH break", "last_ph_break")):
        b = snap.get(key)
        if not b:
            print(f"- {label}: none")
            continue
        print(
            f"- {label}: level=`{b['level']}` open=`{_fmt_utc(b['candle_open'])}` "
            f"known=`{_fmt_utc(b['known_at'])}` ({b['minutes_before_signal']:.1f} min before signal)"
        )
    print()


def print_report(results: list[dict[str, Any]]) -> None:
    by_id = {r["case"]["case_id"]: r for r in results}
    a, b = by_id["APT_002"], by_id["APT_003"]

    print("# APT two-case MTF context audit (read-only)\n")
    print("## Sources\n")
    print("- Outcomes: `c3_protected_low_event_driven_decision_deep_dive/` (+ catalog forward returns)")
    print("- Structure: `trend_scanner_multitimeframe_structure/structure_states_{5m,1h,4h}.parquet`")
    print("- Breaks: `protected_{low,high}_break_events.csv`")
    print("- As-of: last row with `available_at <= signal_available_at` (no HTF backfill)\n")
    print("## Field mapping\n")
    print("- `trend_direction` ← `trend_direction` / fallback `major_direction` (1 bull sticky, -1 bear sticky)")
    print("- `trend_state` ← `trend_state` / `protected_structure_state`")
    print("- breaks ← rising-edge CSV with `require_choch=True`\n")

    for r in results:
        c = r["case"]
        print(f"## {c['case_id']} — {c['cluster']} → `{r['outcome']['outcome']}`\n")
        print(f"- 5m PL: `{c['pl']}`")
        print(f"- break candle open: `{c['break_candle_open']}`")
        print(f"- signal known: `{c['signal_available_at']}` / `{_fmt_eat(c['signal_available_at'])}`")
        print(f"- signal close price: `{r['signal_price']}`")
        print(f"- alignment: **`{r['alignment']}`** — {r['alignment_reason']}")
        print(
            f"- causality: no_future_1h=`{r['causality']['no_future_1h']}` "
            f"no_future_4h=`{r['causality']['no_future_4h']}` "
            f"1h_lag=`{r['causality']['1h_lag_min']}` 4h_lag=`{r['causality']['4h_lag_min']}`"
        )
        print(f"- nearest HTF level: `{r['nearest_htf_level']['label']}` "
              f"(dist `{r['nearest_htf_level']['distance_bps']}` bps)\n")
        for tf in ("5m", "1h", "4h"):
            _print_tf(r["tf"][tf])

        o = r["outcome"]
        print("### Stored later outcome (not used for HTF class)\n")
        print(f"- decision: `{o['outcome']}` @ `{o['decision_ts']}` ({o['minutes_after_break']:.2f} min)")
        print(f"- confirm price: `{o['price_at_decision']}` ({o['distance_from_level_bps']:.2f} bps vs PL)")
        print(f"- state_before: `{o['state_before']}`")
        print(f"- first_reclaim_ts: `{o['first_reclaim_ts']}`")
        if o.get("first_failed_reclaim"):
            fr = o["first_failed_reclaim"]
            print(
                f"- first failed reclaim: `{fr['attempt_ts']}` high=`{fr['high_price']}` "
                f"rebreak=`{fr['rebreak_ts']}` (n={fr['n_attempts_logged']})"
            )
        if o.get("forward_deep_dive"):
            fd = o["forward_deep_dive"]
            print(
                f"- deep-dive MFE/MAE: type=`{fd.get('candidate_type')}` "
                f"MFE15=`{fd.get('mfe_bps_15m')}` MAE15=`{fd.get('mae_bps_15m')}` "
                f"MFE60=`{fd.get('mfe_bps_60m')}`"
            )
        if o.get("forward_catalog_aggressive"):
            print(f"- catalog aggressive forward: `{o['forward_catalog_aggressive']}`")
        print()

    # Comparison table
    print("## Direct comparison\n")
    rows = [
        ("5m Signal known", a["case"]["signal_available_at"], b["case"]["signal_available_at"]),
        ("5m PL", a["case"]["pl"], b["case"]["pl"]),
        ("1h Trend-State", a["tf"]["1h"].get("trend_state"), b["tf"]["1h"].get("trend_state")),
        ("1h trend_direction", a["tf"]["1h"].get("trend_direction"), b["tf"]["1h"].get("trend_direction")),
        ("1h PL", a["tf"]["1h"].get("protected_low"), b["tf"]["1h"].get("protected_low")),
        ("1h PH", a["tf"]["1h"].get("protected_high"), b["tf"]["1h"].get("protected_high")),
        (
            "letzter 1h PL-Break",
            (
                f"{a['tf']['1h']['last_pl_break']['level']} @ {_fmt_utc(a['tf']['1h']['last_pl_break']['known_at'])}"
                if a["tf"]["1h"].get("last_pl_break")
                else "none"
            ),
            (
                f"{b['tf']['1h']['last_pl_break']['level']} @ {_fmt_utc(b['tf']['1h']['last_pl_break']['known_at'])}"
                if b["tf"]["1h"].get("last_pl_break")
                else "none"
            ),
        ),
        ("4h Trend-State", a["tf"]["4h"].get("trend_state"), b["tf"]["4h"].get("trend_state")),
        ("4h trend_direction", a["tf"]["4h"].get("trend_direction"), b["tf"]["4h"].get("trend_direction")),
        ("4h PL", a["tf"]["4h"].get("protected_low"), b["tf"]["4h"].get("protected_low")),
        ("4h PH", a["tf"]["4h"].get("protected_high"), b["tf"]["4h"].get("protected_high")),
        (
            "letzter 4h PL-Break",
            (
                f"{a['tf']['4h']['last_pl_break']['level']} @ {_fmt_utc(a['tf']['4h']['last_pl_break']['known_at'])}"
                if a["tf"]["4h"].get("last_pl_break")
                else "none"
            ),
            (
                f"{b['tf']['4h']['last_pl_break']['level']} @ {_fmt_utc(b['tf']['4h']['last_pl_break']['known_at'])}"
                if b["tf"]["4h"].get("last_pl_break")
                else "none"
            ),
        ),
        ("Alignment", a["alignment"], b["alignment"]),
        ("nächstes HTF-Level", a["nearest_htf_level"]["label"], b["nearest_htf_level"]["label"]),
        ("Abstand zum HTF-Level (bps)", a["nearest_htf_level"]["distance_bps"], b["nearest_htf_level"]["distance_bps"]),
        ("spätere Entscheidung", a["outcome"]["outcome"], b["outcome"]["outcome"]),
        ("Confirm nach Minuten", a["outcome"]["minutes_after_break"], b["outcome"]["minutes_after_break"]),
    ]
    print("| Merkmal | APT_002 Breakdown | APT_003 Reclaim |")
    print("|---|---|---|")
    for name, va, vb in rows:
        print(f"| {name} | {va} | {vb} |")
    print()

    print("## Answers\n")
    print(
        "1. **War APT_002 bereits auf 1h bearish beschädigt?** "
        f"**Nein (noch nicht bestätigt).** Zum Signal `02:30Z` war der 1h-PL "
        f"`{a['tf']['1h'].get('protected_low')}` **identisch** mit dem 5m-Break-Level und "
        f"**nicht** `close_break_protected_down` auf 1h. 1h-State=`{a['tf']['1h'].get('trend_state')}`, "
        f"trend_direction=`{a['tf']['1h'].get('trend_direction')}` (noch sticky-bull). "
        "Der 5m-Bruch war der **erste** Angriff auf diesen 1h-PL; 1h-Break folgt erst "
        f"`{_fmt_utc(b['tf']['1h']['last_pl_break']['known_at']) if b['tf']['1h'].get('last_pl_break') else 'n/a'}` "
        "(sichtbar aus APT_003-Vorgeschichte: 1h PL 0.5689 broken @ 2026-07-31 05:00Z)."
    )
    print()
    print(
        "2. **War APT_003 auf 1h oder 4h noch strukturell unterstützt?** "
        f"**Teilweise / gemischt.** 4h war bereits `bearish_structure` (dir=-1), PH=`{b['tf']['4h'].get('protected_high')}` "
        f"— nicht bullish-unterstützend. 1h hatte **keinen** aktiven PL mehr; State=`{b['tf']['1h'].get('trend_state')}` "
        f"bei trend_direction=`{b['tf']['1h'].get('trend_direction')}` und PH=`{b['tf']['1h'].get('protected_high')}` "
        "(= altes 0.5689 als Resistance). Das ist eher **Reparatur/Internal-Bullish innerhalb bearish-major**, "
        "nicht frische 1h-PL-Unterstützung unter dem Preis."
    )
    print()
    print(
        "3. **Erklärt der HTF-Kontext den Outcome-Unterschied?** "
        "**Plausibel teilweise ja auf 1h, nicht über 4h.** Beide Fälle haben denselben 4h-Bearish-Rahmen "
        f"(PH `{a['tf']['4h'].get('protected_high')}`). Der Unterschied liegt im **1h**: "
        "APT_002 bricht einen **noch aktiven** 1h-PL (Lead 5m→1h); APT_003 bricht einen **neueren tieferen** "
        "5m-PL, nachdem 1h-PL 0.5689 schon ~47h zuvor gebrochen war und 1h `bullish_internal_break` zeigt. "
        "Das ordnet Breakdown vs. späteren Reclaim **kontextuell** ein, ersetzt aber keine 5m-Flow-Bestätigung."
    )
    print()
    print(
        "4. **Oder waren beide HTF-Kontexte ähnlich und der Unterschied entstand erst im 5m-Preis-/Tradeflow?** "
        "**4h ähnlich, 1h unterschiedlich; Confirm selbst ist 5m.** "
        f"APT_002 Confirm in `{a['outcome']['minutes_after_break']:.0f} min` nach mehreren Failed-Reclaims; "
        f"APT_003 Confirm in `{b['outcome']['minutes_after_break']:.1f} min` mit stabilem Reclaim "
        f"(`{b['outcome']['first_reclaim_ts']}`). Die Entscheidungssekunde bleibt price+trades auf 5m; "
        "HTF liefert den Regime-Rahmen, nicht den Trigger."
    )
    print()

    print("## Chart timestamps\n")
    for r in results:
        c = r["case"]
        print(f"### {c['case_id']}")
        print(
            f"- 5m break open `{c['break_candle_open']}` (`{_fmt_eat(c['break_candle_open'])}`); "
            f"known `{c['signal_available_at']}` (`{_fmt_eat(c['signal_available_at'])}`)"
        )
        for tf in ("1h", "4h"):
            s = r["tf"][tf]
            lp = s.get("last_pl_break") or {}
            print(
                f"- {tf}: open `{_fmt_utc(s.get('candle_open'))}` known `{_fmt_utc(s.get('available_at'))}` "
                f"(`{_fmt_eat(s.get('available_at'))}`); PL=`{s.get('protected_low')}` PH=`{s.get('protected_high')}`; "
                f"last PL-break open `{_fmt_utc(lp.get('candle_open'))}` known `{_fmt_utc(lp.get('known_at'))}`"
            )
        print()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="APT_002 vs APT_003 causal MTF context (read-only)")
    p.add_argument("--mtf-dir", type=Path, default=MTF_DIR)
    p.add_argument("--json-summary", action="store_true", help="Also print machine-readable summary JSON")
    args = p.parse_args(argv)

    mtf = args.mtf_dir
    for req in (
        mtf / "structure_states_5m.parquet",
        mtf / "structure_states_1h.parquet",
        mtf / "structure_states_4h.parquet",
        DEEP_DIR / "decision_points.csv",
    ):
        if not req.exists():
            print(f"ERROR: missing {req}", file=sys.stderr)
            return 1

    s5 = pd.read_parquet(mtf / "structure_states_5m.parquet")
    s1 = pd.read_parquet(mtf / "structure_states_1h.parquet")
    s4 = pd.read_parquet(mtf / "structure_states_4h.parquet")
    for df in (s5, s1, s4):
        df["available_at"] = pd.to_datetime(df["available_at"], utc=True)

    pl_breaks = pd.read_csv(mtf / "protected_low_break_events.csv")
    ph_breaks = pd.read_csv(mtf / "protected_high_break_events.csv")
    for df in (pl_breaks, ph_breaks):
        df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
        df["candle_open_ts"] = pd.to_datetime(df["candle_open_ts"], utc=True)

    results = [
        analyze_case(case, s5=s5, s1=s1, s4=s4, pl_breaks=pl_breaks, ph_breaks=ph_breaks)
        for case in CASES
    ]
    print_report(results)

    if args.json_summary:
        def _ser(x: Any) -> Any:
            if isinstance(x, datetime):
                return x.isoformat().replace("+00:00", "Z")
            if isinstance(x, dict):
                return {k: _ser(v) for k, v in x.items()}
            if isinstance(x, list):
                return [_ser(v) for v in x]
            return x

        slim = []
        for r in results:
            slim.append(
                _ser(
                    {
                        "case_id": r["case"]["case_id"],
                        "alignment": r["alignment"],
                        "alignment_reason": r["alignment_reason"],
                        "causality": r["causality"],
                        "tf": r["tf"],
                        "nearest_htf_level": r["nearest_htf_level"],
                        "outcome": r["outcome"],
                        "answers": r["answers"],
                    }
                )
            )
        print("\n## JSON summary\n")
        print(json.dumps(slim, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
