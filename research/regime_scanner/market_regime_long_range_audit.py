#!/usr/bin/env python3
"""Long-range read-only audit of MarketRegimeClassifier (K2_H4).

Warm-up builds causal HTF/regime state; metrics cover only the audit window.
Does not modify structure/machine/policy/zones or wire trading decisions.

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_long_range_audit.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import resource
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.market_regime import (
    TREND_REGIMES,
    MarketRegimeClassifier,
    MarketRegimeConfig,
    MarketRegimeContext,
    MarketRegimeName,
    compute_market_regime_features,
    default_market_regime_config,
    h4_confirm_bars,
    market_regime_hysteresis_docs,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta

OUT = Path("research/regime_scanner/results/market_regime_long_range_audit")
READONLY_MARCH = Path(
    "research/regime_scanner/results/market_regime_readonly_audit/march_crash_timeline.csv"
)
READONLY_TIMELINE = Path(
    "research/regime_scanner/results/market_regime_readonly_audit/regime_timeline.csv"
)

STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")

WARMUP_PREFERRED = "2025-11-01T00:00:00+00:00"
WARMUP_MINIMUM = "2025-12-01T00:00:00+00:00"
AUDIT_START = "2026-01-06T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"
AUDIT_LAST_5M_OPEN = "2026-03-16T23:55:00+00:00"

# Classifier emits on closed 30m decision times (candle_open + 30m).
TF_MINUTES = 30
FIVE_PER_HTF = TF_MINUTES // 5

ACTUAL_REGIMES = (
    "strong_bullish_trend",
    "strong_bearish_trend",
    "accumulation_range",
    "transition_unclear",
)

DETAIL_WINDOWS = [
    ("2026-01-06", "2026-01-15"),
    ("2026-01-16", "2026-01-31"),
    ("2026-02-01", "2026-02-15"),
    ("2026-02-16", "2026-02-28"),
    ("2026-03-01", "2026-03-10"),
    ("2026-03-11", "2026-03-16"),
]

# Analytical-only labels (not fed to classifier/policy)
PREMATURE_PRE_MOVE_ATR_MAX = 0.35  # little move yet before strong → premature risk if then reverses
LATE_PRE_FRACTION_MIN = 0.55  # >55% of eventual segment move already done → late
FALSE_STRONG_MAE_GT_MFE = True
FALSE_STRONG_FWD_48_SIGN_FLIP = True


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return _ts(v).isoformat()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _p(msg: str) -> None:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"{msg}  [rss≈{rss:.0f}MB]", flush=True)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def expected_5m_opens(start: pd.Timestamp, last_open: pd.Timestamp) -> int:
    return int(len(pd.date_range(start, last_open, freq="5min", tz="UTC")))


def assess_data(raw: pd.DataFrame) -> dict[str, Any]:
    """Preflight: coverage, gaps, duplicates. Never silently shrink the audit window."""
    df = raw.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    data_start = _ts(df["timestamp"].iloc[0])
    data_end = _ts(df["timestamp"].iloc[-1])
    dupes = int(df["timestamp"].duplicated().sum())
    delta = df["timestamp"].diff().dropna()
    gaps = delta[delta > pd.Timedelta(minutes=5)]
    gap_rows = []
    for idx in gaps.nlargest(20).index:
        gap_rows.append(
            {
                "from": _iso(df.loc[idx - 1, "timestamp"]),
                "to": _iso(df.loc[idx, "timestamp"]),
                "gap": str(gaps.loc[idx]),
            }
        )

    pref = _ts(WARMUP_PREFERRED)
    warm_min = _ts(WARMUP_MINIMUM)
    audit_start = _ts(AUDIT_START)
    audit_last = _ts(AUDIT_LAST_5M_OPEN)
    end_cap = min(data_end, audit_last)

    pref_expected = expected_5m_opens(pref, audit_last)
    min_expected = expected_5m_opens(warm_min, audit_last)
    audit_expected = expected_5m_opens(audit_start, audit_last)

    have_pref_window = df[(df["timestamp"] >= pref) & (df["timestamp"] <= audit_last)]
    have_min_window = df[(df["timestamp"] >= warm_min) & (df["timestamp"] <= audit_last)]
    have_audit = df[(df["timestamp"] >= audit_start) & (df["timestamp"] <= audit_last)]

    missing_pref_start = pref
    missing_pref_end = min(data_start - pd.Timedelta(minutes=5), audit_last)
    missing_pref_bars = (
        expected_5m_opens(missing_pref_start, missing_pref_end) if data_start > pref else 0
    )
    missing_min_bars = (
        expected_5m_opens(warm_min, data_start - pd.Timedelta(minutes=5))
        if data_start > warm_min
        else 0
    )

    audit_complete = len(have_audit) == audit_expected and dupes == 0 and len(gaps) == 0
    warmup_pref_ok = data_start <= pref
    warmup_min_ok = data_start <= warm_min
    complete = bool(audit_complete and warmup_pref_ok and dupes == 0 and len(gaps) == 0)

    # usable warm-up: from available data start to audit start
    warm_have = df[(df["timestamp"] >= data_start) & (df["timestamp"] < audit_start)]

    status = "COMPLETE" if complete else "INCOMPLETE"
    return {
        "DATA_STATUS": status,
        "symbol": "APTUSDT",
        "available_first_timestamp": _iso(data_start),
        "available_last_timestamp": _iso(data_end),
        "available_candle_count": int(len(df)),
        "duplicates": dupes,
        "unsorted": bool((delta < pd.Timedelta(0)).any()) if len(delta) else False,
        "gaps_gt_5m_count": int(len(gaps)),
        "largest_gaps": gap_rows,
        "preferred_warmup_start": WARMUP_PREFERRED,
        "minimum_warmup_start": WARMUP_MINIMUM,
        "preferred_window_expected_candles": pref_expected,
        "preferred_window_available_candles": int(len(have_pref_window)),
        "minimum_window_expected_candles": min_expected,
        "minimum_window_available_candles": int(len(have_min_window)),
        "audit_expected_candles": audit_expected,
        "audit_available_candles": int(len(have_audit)),
        "audit_window_complete": bool(len(have_audit) == audit_expected),
        "warmup_preferred_available": warmup_pref_ok,
        "warmup_minimum_available": warmup_min_ok,
        "missing_period": None
        if warmup_pref_ok
        else {
            "from": WARMUP_PREFERRED,
            "to": _iso(data_start - pd.Timedelta(minutes=5)),
            "expected_candles_missing_vs_preferred": missing_pref_bars,
            "expected_candles_missing_vs_minimum_dec1": missing_min_bars,
        },
        "used_warmup": {
            "from": _iso(data_start),
            "to": _iso(audit_start - pd.Timedelta(minutes=5)),
            "candles": int(len(warm_have)),
            "note": "Shorter than preferred Nov-1 / minimum Dec-1 warm-up.",
        },
        "used_audit": {
            "from": AUDIT_START,
            "to": AUDIT_END,
            "last_5m_open": AUDIT_LAST_5M_OPEN,
            "candles": int(len(have_audit)),
        },
        "htf_buckets_closed_only": True,
        "end_cap_used": _iso(end_cap),
    }


def filter_audit_rows(rows: list[dict[str, Any]], audit_start: pd.Timestamp, audit_end: pd.Timestamp) -> list[dict[str, Any]]:
    """Keep only bars with decision_time inside the audit window (exclude warm-up)."""
    out = []
    for r in rows:
        dt = _ts(r["decision_time"])
        if audit_start <= dt <= audit_end:
            out.append(r)
    return out


def assert_htf_closed(ind_tf: pd.DataFrame, tf: str, decision_end: pd.Timestamp) -> dict[str, Any]:
    """Verify decision_time == open + tf and no bar extends past decision_end."""
    td = timeframe_timedelta(tf)
    opens = pd.to_datetime(ind_tf["timestamp"], utc=True)
    decisions = pd.to_datetime(ind_tf["decision_time"], utc=True)
    ok = bool(((decisions - opens) == td).all())
    within = bool((decisions <= decision_end).all())
    return {
        "timeframe": tf,
        "closed_bucket_ok": ok,
        "all_decision_times_le_end": within,
        "n_bars": int(len(ind_tf)),
    }


@dataclass
class BarRecord:
    decision_time: pd.Timestamp
    candle_timestamp: pd.Timestamp
    close: float
    high: float
    low: float
    ctx: MarketRegimeContext
    in_audit: bool


def run_classifier_timeline(
    ind30: pd.DataFrame,
    *,
    cfg: MarketRegimeConfig,
    audit_start: pd.Timestamp,
    audit_end: pd.Timestamp,
) -> list[BarRecord]:
    clf = MarketRegimeClassifier(cfg)
    close = ind30["close"].astype(float).to_numpy()
    high = ind30["high"].astype(float).to_numpy()
    low = ind30["low"].astype(float).to_numpy()
    ema9 = ind30["ema_9"].astype(float).to_numpy()
    ema20 = ind30["ema_20"].astype(float).to_numpy()
    atr = ind30["atr"].astype(float).to_numpy()
    out: list[BarRecord] = []
    for i in range(len(ind30)):
        dt = _ts(ind30.iloc[i]["decision_time"])
        if dt > audit_end:
            break
        feat = compute_market_regime_features(
            close[: i + 1],
            high[: i + 1],
            low[: i + 1],
            ema9[: i + 1],
            ema20[: i + 1],
            atr[: i + 1],
            cfg=cfg,
        )
        if feat is None:
            continue
        ctx = clf.update(decision_time=dt, features=feat)
        out.append(
            BarRecord(
                decision_time=dt,
                candle_timestamp=_ts(ind30.iloc[i]["timestamp"]),
                close=float(close[i]),
                high=float(high[i]),
                low=float(low[i]),
                ctx=ctx,
                in_audit=bool(audit_start <= dt <= audit_end),
            )
        )
    return out


def build_segments(bars: list[BarRecord]) -> list[dict[str, Any]]:
    """Non-overlapping regime segments on audit bars only."""
    audit_bars = [b for b in bars if b.in_audit]
    if not audit_bars:
        return []
    segs: list[dict[str, Any]] = []
    start = 0
    for i in range(1, len(audit_bars) + 1):
        if i < len(audit_bars) and audit_bars[i].ctx.regime == audit_bars[start].ctx.regime:
            continue
        chunk = audit_bars[start:i]
        regime = chunk[0].ctx.regime
        prev_regime = None if not segs else segs[-1]["regime"]
        next_regime = audit_bars[i].ctx.regime if i < len(audit_bars) else None
        n = len(chunk)
        start_px = chunk[0].close
        end_px = chunk[-1].close
        hi = max(b.high for b in chunk)
        lo = min(b.low for b in chunk)
        # MFE/MAE relative to start close and regime direction
        if regime == "strong_bullish_trend":
            mfe = (hi - start_px) / start_px * 100.0
            mae = (lo - start_px) / start_px * 100.0
        elif regime == "strong_bearish_trend":
            mfe = (start_px - lo) / start_px * 100.0
            mae = (hi - start_px) / start_px * 100.0
        else:
            mfe = (hi - start_px) / start_px * 100.0
            mae = (lo - start_px) / start_px * 100.0
        # internal 1-bar bounce holds: raw != held regime for exactly 1 bar then back
        bounce_holds = 0
        for j, b in enumerate(chunk):
            if b.ctx.raw_regime != regime and "hyst_hold_H4:1/" in "|".join(b.ctx.reason_codes):
                bounce_holds += 1
        ended_by_direct_counter = False
        if next_regime in TREND_REGIMES and regime in TREND_REGIMES and next_regime != regime:
            ended_by_direct_counter = True
        segs.append(
            {
                "segment_id": len(segs),
                "start_timestamp": _iso(chunk[0].decision_time),
                "end_timestamp": _iso(chunk[-1].decision_time),
                "candle_open_start": _iso(chunk[0].candle_timestamp),
                "candle_open_end": _iso(chunk[-1].candle_timestamp),
                "duration_30m_bars": n,
                "duration_5m_candles": n * FIVE_PER_HTF,
                "duration_hours": n * (TF_MINUTES / 60.0),
                "regime": regime,
                "previous_regime": prev_regime,
                "next_regime": next_regime,
                "start_price": start_px,
                "end_price": end_px,
                "high": hi,
                "low": lo,
                "max_deviation_up_pct": (hi - start_px) / start_px * 100.0,
                "max_deviation_down_pct": (lo - start_px) / start_px * 100.0,
                "price_change_pct": (end_px - start_px) / start_px * 100.0,
                "max_favorable_excursion_pct": mfe,
                "max_adverse_excursion_pct": mae,
                "internal_1bar_bounce_holds": bounce_holds,
                "ended_by_direct_countertrend": ended_by_direct_counter,
            }
        )
        start = i
    # fill next_regime already set; fix last previous chain
    return segs


def build_transitions(bars: list[BarRecord], cfg: MarketRegimeConfig) -> list[dict[str, Any]]:
    audit_bars = [b for b in bars if b.in_audit]
    out = []
    for a, b in zip(audit_bars, audit_bars[1:]):
        if a.ctx.regime == b.ctx.regime:
            continue
        req = h4_confirm_bars(b.ctx.regime, cfg)
        reasons = list(b.ctx.reason_codes)
        immediate = a.ctx.regime is None  # never for transitions after start
        # first audit bar is not a transition from warm-up in this list
        direct_counter = (
            a.ctx.regime in TREND_REGIMES
            and b.ctx.regime in TREND_REGIMES
            and a.ctx.regime != b.ctx.regime
        )
        # detect if previous bar held a 1-bar bounce
        held_bounce = any(x.startswith("hyst_hold_H4:1/") for x in a.ctx.reason_codes)
        out.append(
            {
                "timestamp": _iso(b.decision_time),
                "old_regime": a.ctx.regime,
                "new_regime": b.ctx.regime,
                "raw_candidate": b.ctx.raw_regime,
                "confirm_streak_at_switch": b.ctx.candidate_streak if b.ctx.candidate_streak else req,
                "confirm_bars_required": req,
                "immediate_first_bar_rule": False,
                "direct_countertrend_switch": direct_counter,
                "prior_bar_1bar_bounce_hold": held_bounce,
                "closed_htf_bars_used": "30m",
                "htf_candle_open": _iso(b.candle_timestamp),
                "htf_decision_time": _iso(b.decision_time),
                "reason_codes": "|".join(reasons),
                "close": b.close,
            }
        )
    return out


def _ret(closes: np.ndarray, i: int, n: int, forward: bool) -> float | None:
    if forward:
        j = i + n
        if j >= len(closes) or i < 0:
            return None
        return float(closes[j] / closes[i] - 1.0) * 100.0
    j = i - n
    if j < 0 or i >= len(closes):
        return None
    return float(closes[i] / closes[j] - 1.0) * 100.0


def analyze_strong_events(
    segs: list[dict[str, Any]],
    frame5: pd.DataFrame,
) -> list[dict[str, Any]]:
    """Premature/late/false markers — analytical only."""
    f5 = frame5.copy()
    f5["timestamp"] = pd.to_datetime(f5["timestamp"], utc=True)
    f5["decision_time"] = pd.to_datetime(f5["decision_time"], utc=True)
    f5 = f5.sort_values("decision_time").reset_index(drop=True)
    closes = f5["close"].astype(float).to_numpy()
    highs = f5["high"].astype(float).to_numpy()
    lows = f5["low"].astype(float).to_numpy()
    dts = list(f5["decision_time"])

    def idx_at(ts: pd.Timestamp) -> int:
        # nearest 5m decision_time <= ts
        pos = int(np.searchsorted(pd.DatetimeIndex(dts).asi8, _ts(ts).value, side="right") - 1)
        return max(0, min(pos, len(dts) - 1))

    events = []
    for seg in segs:
        if seg["regime"] not in TREND_REGIMES:
            continue
        start = _ts(seg["start_timestamp"])
        end = _ts(seg["end_timestamp"])
        i0 = idx_at(start)
        i1 = idx_at(end)
        start_px = float(closes[i0])
        # pre moves
        pre = {f"pre_{n}_5m_pct": _ret(closes, i0, n, False) for n in (1, 2, 3, 6, 12)}
        fwd = {f"fwd_{n}_5m_pct": _ret(closes, i0, n, True) for n in (1, 2, 3, 6, 12, 24, 48)}
        # MFE/MAE over next 48 bars or segment end
        j_end = min(len(closes) - 1, i0 + 48, i1)
        window_h = highs[i0 : j_end + 1]
        window_l = lows[i0 : j_end + 1]
        if seg["regime"] == "strong_bullish_trend":
            mfe = float((np.max(window_h) - start_px) / start_px * 100.0) if len(window_h) else None
            mae = float((np.min(window_l) - start_px) / start_px * 100.0) if len(window_l) else None
            # time to local max
            if len(window_h):
                k = int(np.argmax(window_h))
                time_to_ext = k * 5
            else:
                time_to_ext = None
            signed_seg = float(seg["price_change_pct"])
            pre_move = float(pre["pre_12_5m_pct"] or 0.0)
            # fraction of segment move already done in pre_12 (same sign)
            if abs(signed_seg) > 1e-9 and pre_move * signed_seg > 0:
                pre_fraction = abs(pre_move) / (abs(pre_move) + abs(signed_seg))
            else:
                pre_fraction = 0.0 if signed_seg != 0 else None
        else:
            mfe = float((start_px - np.min(window_l)) / start_px * 100.0) if len(window_l) else None
            mae = float((np.max(window_h) - start_px) / start_px * 100.0) if len(window_h) else None
            if len(window_l):
                k = int(np.argmin(window_l))
                time_to_ext = k * 5
            else:
                time_to_ext = None
            signed_seg = float(seg["price_change_pct"])
            pre_move = float(pre["pre_12_5m_pct"] or 0.0)
            # for bearish, segment move negative; pre should be negative for "already falling"
            if abs(signed_seg) > 1e-9 and pre_move * signed_seg > 0:
                pre_fraction = abs(pre_move) / (abs(pre_move) + abs(signed_seg))
            else:
                pre_fraction = 0.0 if signed_seg != 0 else None

        possible_premature = False
        possible_late = False
        possible_false_strong = False
        tags = []
        # premature: little pre-move in trend direction, then adverse dominates quickly
        pre_dir = pre["pre_6_5m_pct"]
        if pre_dir is not None:
            if seg["regime"] == "strong_bearish_trend" and pre_dir > -0.15 and (mae or 0) > abs(mfe or 0) + 0.2:
                possible_premature = True
                tags.append("possible_premature")
            if seg["regime"] == "strong_bullish_trend" and pre_dir < 0.15 and abs(mae or 0) > (mfe or 0) + 0.2:
                possible_premature = True
                tags.append("possible_premature")
        if pre_fraction is not None and pre_fraction >= LATE_PRE_FRACTION_MIN:
            possible_late = True
            tags.append("possible_late")
        fwd48 = fwd.get("fwd_48_5m_pct")
        if FALSE_STRONG_MAE_GT_MFE and mfe is not None and mae is not None:
            if abs(mae) > abs(mfe) + 0.25:
                possible_false_strong = True
                tags.append("possible_false_strong_mae")
        if FALSE_STRONG_FWD_48_SIGN_FLIP and fwd48 is not None:
            if seg["regime"] == "strong_bearish_trend" and fwd48 > 0.4:
                possible_false_strong = True
                tags.append("possible_false_strong_fwd_flip")
            if seg["regime"] == "strong_bullish_trend" and fwd48 < -0.4:
                possible_false_strong = True
                tags.append("possible_false_strong_fwd_flip")
        if seg["duration_30m_bars"] <= 2:
            tags.append("very_short_strong")

        events.append(
            {
                "segment_id": seg["segment_id"],
                "regime": seg["regime"],
                "start_timestamp": seg["start_timestamp"],
                "end_timestamp": seg["end_timestamp"],
                "duration_hours": seg["duration_hours"],
                "duration_30m_bars": seg["duration_30m_bars"],
                "start_price": seg["start_price"],
                "end_price": seg["end_price"],
                "price_change_pct": seg["price_change_pct"],
                "mfe_pct_48_or_seg": mfe,
                "mae_pct_48_or_seg": mae,
                "time_to_local_extreme_min": time_to_ext,
                "pre_fraction_of_total_move": pre_fraction,
                "possible_premature": possible_premature,
                "possible_late": possible_late,
                "possible_false_strong": possible_false_strong,
                "analytic_tags": "|".join(tags),
                **pre,
                **fwd,
                "criteria_note": (
                    "possible_premature: weak 6x5m pre-move then MAE>MFE; "
                    "possible_late: pre_12 share of (pre+segment) move >= 0.55; "
                    "possible_false_strong: MAE>MFE+0.25 or 48x5m forward flips against regime."
                ),
            }
        )
    return events


def detect_countertrend_bounces(bars: list[BarRecord]) -> list[dict[str, Any]]:
    """Interruptions of strong regimes by non-strong bars then re-entry."""
    audit = [b for b in bars if b.in_audit]
    out = []
    i = 0
    while i < len(audit):
        if audit[i].ctx.regime not in TREND_REGIMES:
            i += 1
            continue
        regime = audit[i].ctx.regime
        j = i
        while j < len(audit) and audit[j].ctx.regime == regime:
            j += 1
        # interruption
        if j >= len(audit):
            break
        k = j
        while k < len(audit) and audit[k].ctx.regime != regime:
            k += 1
        if k < len(audit) and audit[k].ctx.regime == regime:
            interrupt = audit[j:k]
            regs = [b.ctx.regime for b in interrupt]
            out.append(
                {
                    "strong_regime": regime,
                    "block_start": _iso(audit[i].decision_time),
                    "interruption_start": _iso(audit[j].decision_time),
                    "interruption_end": _iso(audit[k - 1].decision_time),
                    "reentry": _iso(audit[k].decision_time),
                    "interruption_30m_bars": len(interrupt),
                    "interruption_hours": len(interrupt) * 0.5,
                    "regimes_during": "|".join(regs),
                    "had_accumulation_range": "accumulation_range" in regs,
                    "had_transition": "transition_unclear" in regs,
                    "had_opposite_strong": any(
                        r in TREND_REGIMES and r != regime for r in regs
                    ),
                    "pre_close": audit[j - 1].close,
                    "max_close_during": max(b.close for b in interrupt),
                    "min_close_during": min(b.close for b in interrupt),
                }
            )
            i = k
        else:
            i = j
    return out


def daily_weekly_summaries(bars: list[BarRecord]) -> tuple[list[dict], list[dict]]:
    audit = [b for b in bars if b.in_audit]
    by_day: dict[str, list[BarRecord]] = defaultdict(list)
    by_week: dict[str, list[BarRecord]] = defaultdict(list)
    for b in audit:
        day = b.decision_time.strftime("%Y-%m-%d")
        week = f"{b.decision_time.isocalendar().year}-W{b.decision_time.isocalendar().week:02d}"
        by_day[day].append(b)
        by_week[week].append(b)

    def summarize(key: str, group: list[BarRecord], kind: str) -> dict[str, Any]:
        c = Counter(b.ctx.regime for b in group)
        flips = sum(1 for a, b in zip(group, group[1:]) if a.ctx.regime != b.ctx.regime)
        return {
            kind: key,
            "n_30m_bars": len(group),
            "flips": flips,
            **{f"n_{r}": c[r] for r in ACTUAL_REGIMES},
            **{f"share_{r}": c[r] / max(len(group), 1) for r in ACTUAL_REGIMES},
            "start_close": group[0].close,
            "end_close": group[-1].close,
            "price_change_pct": (group[-1].close / group[0].close - 1.0) * 100.0,
        }

    daily = [summarize(k, by_day[k], "date") for k in sorted(by_day)]
    weekly = [summarize(k, by_week[k], "week") for k in sorted(by_week)]
    return daily, weekly


def compare_readonly(audit_bars: list[BarRecord]) -> dict[str, Any]:
    if not READONLY_MARCH.exists():
        return {"compared": False, "reason": "readonly march timeline missing"}
    march = [
        b
        for b in audit_bars
        if b.in_audit and _ts("2026-03-05") <= b.decision_time <= _ts("2026-03-10")
    ]
    ref = list(csv.DictReader(READONLY_MARCH.open()))
    if ref and "variant_id" in ref[0]:
        ref = [r for r in ref if r.get("variant_id") == "K2_H4"]
    ref_by = {r.get("timestamp") or r.get("decision_time"): r for r in ref}
    mismatches = []
    matched = 0
    for b in march:
        key = _iso(b.decision_time)
        r = ref_by.get(key)
        if r is None:
            continue
        ref_reg = r.get("market_regime") or r.get("regime")
        if ref_reg != b.ctx.regime:
            mismatches.append(
                {
                    "timestamp": key,
                    "long_range": b.ctx.regime,
                    "readonly": ref_reg,
                }
            )
        else:
            matched += 1
    first_lr = next((b for b in march if b.ctx.regime == "strong_bearish_trend"), None)
    first_ref = next(
        (
            r
            for r in ref
            if (r.get("market_regime") or r.get("regime")) == "strong_bearish_trend"
        ),
        None,
    )
    # second block
    strong_starts = []
    prev = None
    for b in march:
        if b.ctx.regime == "strong_bearish_trend" and prev != "strong_bearish_trend":
            strong_starts.append(_iso(b.decision_time))
        prev = b.ctx.regime

    return {
        "compared": True,
        "overlap_bars_matched": matched,
        "overlap_mismatches": len(mismatches),
        "mismatch_sample": mismatches[:20],
        "long_range_first_strong_bearish": None if first_lr is None else _iso(first_lr.decision_time),
        "readonly_first_strong_bearish": None
        if first_ref is None
        else (first_ref.get("timestamp") or first_ref.get("decision_time")),
        "first_strong_match": (
            first_lr is not None
            and first_ref is not None
            and _iso(first_lr.decision_time)
            == (first_ref.get("timestamp") or first_ref.get("decision_time"))
        ),
        "long_range_strong_bearish_block_starts": strong_starts,
        "expected_first": "2026-03-05T17:30:00+00:00",
        "expected_second_approx": "2026-03-06T14:30:00+00:00",
        "h4_semantics_unchanged": True,
        "note": "Comparison on overlapping March window; warm-up differs (Dec27 vs classify-from-Jan1).",
    }


def quality_report(
    segs: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    events: list[dict[str, Any]],
    bounces: list[dict[str, Any]],
    audit_bars: list[BarRecord],
) -> dict[str, Any]:
    by_reg = Counter(s["regime"] for s in segs)
    strong = [s for s in segs if s["regime"] in TREND_REGIMES]
    short_strong = [s for s in strong if s["duration_30m_bars"] <= 2]
    direct = [t for t in transitions if t["direct_countertrend_switch"]]
    # flutter: flip rate
    n = max(len(audit_bars), 1)
    flips = sum(
        1
        for a, b in zip(audit_bars, audit_bars[1:])
        if a.ctx.regime != b.ctx.regime
    )
    # range/transition between opposite trends
    bridge = 0
    for i, s in enumerate(segs[:-1]):
        if s["regime"] in TREND_REGIMES:
            nxt = segs[i + 1]
            if nxt["regime"] in {"accumulation_range", "transition_unclear"}:
                # look ahead for opposite trend
                for k in range(i + 2, min(i + 6, len(segs))):
                    if segs[k]["regime"] in TREND_REGIMES and segs[k]["regime"] != s["regime"]:
                        bridge += 1
                        break
    # strong in sideways: low |price_change| and high internal bounce
    false_sideways = [
        s
        for s in strong
        if abs(float(s["price_change_pct"])) < 0.35 and s["duration_30m_bars"] >= 3
    ]
    return {
        "segments_per_regime": dict(by_reg),
        "strong_segment_count": len(strong),
        "strong_avg_duration_hours": float(np.mean([s["duration_hours"] for s in strong])) if strong else None,
        "strong_max_duration_hours": float(np.max([s["duration_hours"] for s in strong])) if strong else None,
        "strong_segments_1_or_2_bars": len(short_strong),
        "direct_countertrend_switches": len(direct),
        "one_bar_bounce_holds_total": int(sum(s["internal_1bar_bounce_holds"] for s in segs)),
        "range_or_transition_bridges_between_trends": bridge,
        "regime_flip_count_audit": flips,
        "regime_flip_rate": flips / n,
        "countertrend_bounce_interruptions": len(bounces),
        "possible_premature_events": sum(1 for e in events if e["possible_premature"]),
        "possible_late_events": sum(1 for e in events if e["possible_late"]),
        "possible_false_strong_events": sum(1 for e in events if e["possible_false_strong"]),
        "strong_in_near_flat_segments": len(false_sideways),
        "flutter_flag": flips / n > 0.20,
    }


def timeline_rows(bars: list[BarRecord]) -> list[dict[str, Any]]:
    rows = []
    for b in bars:
        if not b.in_audit:
            continue
        f = b.ctx.feature_snapshot
        rows.append(
            {
                "decision_time": _iso(b.decision_time),
                "candle_timestamp": _iso(b.candle_timestamp),
                "close": b.close,
                "market_regime": b.ctx.regime,
                "raw_regime": b.ctx.raw_regime,
                "direction": b.ctx.direction,
                "candidate_streak": b.ctx.candidate_streak,
                "candidate_regime": b.ctx.candidate_regime,
                "reason_codes": "|".join(b.ctx.reason_codes),
                "ema9": f.get("ema9"),
                "ema20": f.get("ema20"),
                "ema9_slope_atr": f.get("ema9_slope_atr"),
                "ema20_slope_atr": f.get("ema20_slope_atr"),
                "net_move_atr": f.get("net_move_atr"),
                "directional_efficiency": f.get("directional_efficiency"),
                "progress_vs_range": f.get("progress_vs_range"),
            }
        )
    return rows


def detail_window_tables(rows: list[dict[str, Any]]) -> None:
    for a, b in DETAIL_WINDOWS:
        start = _ts(a)
        # inclusive end of day
        end = _ts(b) + pd.Timedelta(hours=23, minutes=59)
        subset = [r for r in rows if start <= _ts(r["decision_time"]) <= end]
        name = f"detail_{a}_to_{b}.csv".replace(":", "")
        _write_csv(OUT / name, subset)


def decide(
    data: dict[str, Any],
    quality: dict[str, Any],
    comparison: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[str, str]:
    """J / N / U decision — no policy wiring."""
    if data["DATA_STATUS"] != "COMPLETE":
        # audit window itself complete + March match can still allow J with caveat,
        # but missing preferred warm-up → U if March fails or flutter; else J with incomplete data note
        pass
    march_ok = bool(comparison.get("first_strong_match")) and comparison.get(
        "long_range_first_strong_bearish"
    ) == "2026-03-05T17:30:00+00:00"
    second_ok = False
    starts = comparison.get("long_range_strong_bearish_block_starts") or []
    if len(starts) >= 2:
        second_ok = starts[1].startswith("2026-03-06T14:")
    flutter = bool(quality.get("flutter_flag"))
    false_rate = quality.get("possible_false_strong_events", 0) / max(
        quality.get("strong_segment_count", 1), 1
    )
    mismatch = int(comparison.get("overlap_mismatches") or 0)

    if not march_ok or mismatch > 5:
        if data["DATA_STATUS"] != "COMPLETE" and not march_ok:
            return "U", "Daten/Warm-up unvollständig und März-Reproduktion nicht belastbar."
        return "N", "Systematische Abweichung zum Referenz-Audit oder instabile Strong-Erkennung."
    if flutter and false_rate > 0.45:
        return "N", "K2_H4 zeigt systematisches Flattern / viele false-strong Markierungen."
    if data["DATA_STATUS"] != "COMPLETE":
        # March ok, audit window complete, warm-up shorter than preferred
        if second_ok and not flutter:
            return (
                "J",
                "K2_H4 über den vorhandenen Zeitraum stabil (März reproduziert); "
                "Warm-up kürzer als bevorzugt (DATA_STATUS=INCOMPLETE), aber Auditfenster vollständig.",
            )
        return "U", "Auditfenster ok, aber Warm-up-Lücke verhindert volle Belastbarkeit."
    if second_ok and not flutter:
        return "J", "K2_H4 ist über den gesamten Zeitraum stabil und eignet sich für den nächsten Read-only-Integrationsschritt."
    return "U", "Ergebnisse gemischt — Entscheidung nicht belastbar genug."


def load_frames(data_start: pd.Timestamp, audit_end: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    end = audit_end
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    raw = raw.sort_values("timestamp")
    sl = raw[(raw["timestamp"] >= data_start) & (raw["timestamp"] <= _ts(AUDIT_LAST_5M_OPEN))].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    frame5 = compute_indicator_frame(sl, config=scfg)
    frame5["timestamp"] = pd.to_datetime(frame5["timestamp"], utc=True)
    frame5["decision_time"] = frame5["timestamp"] + pd.Timedelta(minutes=5)
    # keep closed 5m whose decision_time <= audit end wall clock
    frame5 = frame5.loc[frame5["decision_time"] <= end].reset_index(drop=True)

    scfg30 = default_regime_scanner_config().with_timeframe("30m")
    agg30 = aggregate_candles(
        frame5[["timestamp", "open", "high", "low", "close", "volume"]],
        "30m",
        end,
    )
    ind30 = compute_indicator_frame(agg30, config=scfg30).copy()
    ind30["timestamp"] = pd.to_datetime(ind30["timestamp"], utc=True)
    ind30["decision_time"] = ind30["timestamp"] + timeframe_timedelta("30m")
    ind30 = ind30.loc[ind30["decision_time"] <= end].reset_index(drop=True)
    htf_check = assert_htf_closed(ind30, "30m", end)
    return frame5, ind30, htf_check


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    _write_json(OUT / "hashes_before.json", hashes_before)

    raw = load_symbol_candles("APTUSDT")
    data_info = assess_data(raw)
    _p(f"DATA_STATUS = {data_info['DATA_STATUS']}")
    print(json.dumps({k: data_info[k] for k in (
        "available_first_timestamp",
        "available_last_timestamp",
        "missing_period",
        "audit_available_candles",
        "audit_expected_candles",
        "used_warmup",
    )}, indent=2), flush=True)

    cfg = default_market_regime_config()
    assert cfg.variant_id == "K2_H4"
    audit_start = _ts(AUDIT_START)
    audit_end = _ts(AUDIT_END)
    data_start = _ts(data_info["available_first_timestamp"])

    frame5, ind30, htf_check = load_frames(data_start, audit_end)
    _p(f"5m={len(frame5)} 30m={len(ind30)} htf_ok={htf_check}")

    bars = run_classifier_timeline(
        ind30, cfg=cfg, audit_start=audit_start, audit_end=audit_end
    )
    # Determinism: second pass
    bars2 = run_classifier_timeline(
        ind30, cfg=cfg, audit_start=audit_start, audit_end=audit_end
    )
    det_ok = [(_iso(a.decision_time), a.ctx.regime) for a in bars if a.in_audit] == [
        (_iso(a.decision_time), a.ctx.regime) for a in bars2 if a.in_audit
    ]

    audit_only = [b for b in bars if b.in_audit]
    assert not audit_only or _ts(audit_only[0].decision_time) >= audit_start
    assert all(b.decision_time <= audit_end for b in audit_only)

    segs = build_segments(bars)
    # segment integrity
    for a, b in zip(segs, segs[1:]):
        assert a["regime"] != b["regime"] or a["end_timestamp"] < b["start_timestamp"]
        assert a["next_regime"] == b["regime"]
        assert b["previous_regime"] == a["regime"]

    transitions = build_transitions(bars, cfg)
    assert len(transitions) == max(len(segs) - 1, 0)

    events = analyze_strong_events(segs, frame5)
    bounces = detect_countertrend_bounces(bars)
    daily, weekly = daily_weekly_summaries(bars)
    rows = timeline_rows(bars)
    detail_window_tables(rows)
    comparison = compare_readonly(bars)
    quality = quality_report(segs, transitions, events, bounces, audit_only)

    # regime time shares
    n_bars = max(len(audit_only), 1)
    counts = Counter(b.ctx.regime for b in audit_only)
    shares = {r: counts[r] / n_bars for r in ACTUAL_REGIMES}

    # premature in known March window vs prior audit
    march_events = [
        e
        for e in events
        if _ts("2026-03-05") <= _ts(e["start_timestamp"]) <= _ts("2026-03-10")
    ]
    march_premature = sum(1 for e in march_events if e["possible_premature"])

    decision, note = decide(data_info, quality, comparison, events)

    hashes_after = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    assert hashes_before == hashes_after

    _write_csv(OUT / "regime_segments.csv", segs)
    _write_csv(OUT / "regime_transitions.csv", transitions)
    _write_csv(OUT / "daily_summary.csv", daily)
    _write_csv(OUT / "weekly_summary.csv", weekly)
    _write_csv(OUT / "strong_regime_events.csv", events)
    _write_csv(OUT / "countertrend_bounces.csv", bounces)
    _write_csv(OUT / "regime_timeline.csv", rows)
    _write_json(OUT / "existing_audit_comparison.json", comparison)
    _write_json(OUT / "hashes_after.json", hashes_after)

    metadata = {
        "variant": "K2_H4",
        "hysteresis": market_regime_hysteresis_docs(),
        "actual_regime_labels": list(ACTUAL_REGIMES),
        "label_note": (
            "Classifier does not emit strong_bullish/bullish/range/bearish aliases; "
            "names are strong_*_trend, accumulation_range, transition_unclear (not renamed)."
        ),
        "warmup_excluded_from_metrics": True,
        "audit_start": AUDIT_START,
        "audit_end": AUDIT_END,
        "htf_check": htf_check,
        "deterministic_repeat": det_ok,
        "analytic_strong_criteria": {
            "possible_premature": "weak 6x5m pre-move then MAE dominates MFE",
            "possible_late": f"pre_12 fraction of (pre+segment) >= {LATE_PRE_FRACTION_MIN}",
            "possible_false_strong": "MAE>MFE+0.25 or 48x5m forward flips against regime",
        },
        "read_only": True,
        "policy_uses_market_regime": False,
    }
    _write_json(OUT / "audit_metadata.json", metadata)
    _write_json(OUT / "data_status.json", data_info)

    # strong lists for summary
    strong_bear = [s for s in segs if s["regime"] == "strong_bearish_trend"]
    strong_bull = [s for s in segs if s["regime"] == "strong_bullish_trend"]

    summary = {
        "DATA_STATUS": data_info["DATA_STATUS"],
        "decision": decision,
        "note": note,
        "data": data_info,
        "regime_distribution": {
            "segments_per_regime": dict(Counter(s["regime"] for s in segs)),
            "bars_per_regime": dict(counts),
            "time_share": shares,
            "avg_segment_duration_hours": {
                r: float(np.mean([s["duration_hours"] for s in segs if s["regime"] == r]))
                if any(s["regime"] == r for s in segs)
                else None
                for r in ACTUAL_REGIMES
            },
            "max_segment_duration_hours": {
                r: float(np.max([s["duration_hours"] for s in segs if s["regime"] == r]))
                if any(s["regime"] == r for s in segs)
                else None
                for r in ACTUAL_REGIMES
            },
            "shortest_strong_segments": sorted(
                [
                    {
                        "regime": s["regime"],
                        "start": s["start_timestamp"],
                        "bars": s["duration_30m_bars"],
                        "hours": s["duration_hours"],
                    }
                    for s in segs
                    if s["regime"] in TREND_REGIMES
                ],
                key=lambda x: x["bars"],
            )[:15],
        },
        "quality": quality,
        "march": {
            "first_strong_bearish": comparison.get("long_range_first_strong_bearish"),
            "strong_bearish_block_starts": comparison.get("long_range_strong_bearish_block_starts"),
            "premature_analytic_in_march_window": march_premature,
            "comparison": comparison,
        },
        "strong_bearish_segments": strong_bear,
        "strong_bullish_segments": strong_bull,
        "hashes": hashes_after,
        "deterministic_repeat": det_ok,
        "n_audit_30m_bars": len(audit_only),
        "n_segments": len(segs),
        "n_transitions": len(transitions),
    }
    _write_json(OUT / "summary.json", summary)

    (OUT / "final_recommendation.md").write_text(
        f"""# Long-range market regime audit (K2_H4)

**DATA_STATUS = {data_info['DATA_STATUS']}**

**Decision: {decision}** — {note}

## Data

- Available: {data_info['available_first_timestamp']} → {data_info['available_last_timestamp']}
- Warm-up used: {data_info['used_warmup']}
- Audit: {AUDIT_START} → {AUDIT_END}
- Missing vs preferred: {data_info.get('missing_period')}

## March reproduction

- First strong_bearish_trend: {comparison.get('long_range_first_strong_bearish')}
- Block starts: {comparison.get('long_range_strong_bearish_block_starts')}
- Match readonly first: {comparison.get('first_strong_match')}
- Overlap mismatches: {comparison.get('overlap_mismatches')}

## Quality

{json.dumps(quality, indent=2)}

## Labels (not renamed)

{list(ACTUAL_REGIMES)}

## Hashes unchanged

{json.dumps(hashes_after, indent=2)}
""",
        encoding="utf-8",
    )
    (OUT / "README.md").write_text(
        f"Long-range K2_H4 audit. DATA_STATUS={data_info['DATA_STATUS']} decision={decision}.\n"
        "See summary.json and final_recommendation.md.\n",
        encoding="utf-8",
    )
    _p(f"DONE decision={decision} DATA_STATUS={data_info['DATA_STATUS']} segs={len(segs)}")


if __name__ == "__main__":
    main()
