#!/usr/bin/env python3
"""Read-only macro-context audit for existing K2_H4 strong segments.

Compares M1 (2h), M2 (4h), M3 (2h+4h) macro labels against local 30m strong
segments from trend_chart_review.csv. Does not modify market_regime.py.

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_macro_context_audit.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import resource
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.market_regime import (
    TREND_REGIMES,
    MarketRegimeClassifier,
    compute_market_regime_features,
    default_market_regime_config,
)
from research.regime_scanner.point_audit import json_safe

OUT = Path("research/regime_scanner/results/market_regime_macro_context_audit")
TRENDS = Path(
    "research/regime_scanner/results/market_regime_strong_quality_audit/trend_chart_review.csv"
)

STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")
MARKET = Path("research/regime_scanner/market_regime.py")

LOAD_START = "2025-12-27T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"

# Focus window called out in the brief
FOCUS_BEAR_START = "2026-01-29T00:00:00+00:00"
FOCUS_BEAR_END = "2026-02-06T23:59:00+00:00"

HTF_MINUTES = {"2h": 120, "4h": 240}


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
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if x != x:
        return default
    return x


def side_of(regime: str | None) -> str:
    if regime == "strong_bullish_trend":
        return "bullish"
    if regime == "strong_bearish_trend":
        return "bearish"
    if regime == "accumulation_range":
        return "range"
    return "transition"


def local_side(direction: str) -> str:
    if direction == "UPTREND":
        return "bullish"
    if direction == "DOWNTREND":
        return "bearish"
    return "neutral"


def aggregate_closed_htf(
    candles_5m: pd.DataFrame,
    minutes: int,
    end_wall: pd.Timestamp,
) -> pd.DataFrame:
    """Causal HTF aggregation (audit-local; does not extend timeframes.py).

    Only complete buckets whose close <= end_wall are emitted.
    timestamp = bucket open; decision_time = open + minutes.
    """
    df = candles_5m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp")
    # only 5m bars that can belong to closed buckets by end_wall
    df = df.loc[df["timestamp"] < end_wall].copy()
    if df.empty:
        return pd.DataFrame()

    opens = df["timestamp"]
    bucket = opens.dt.floor(f"{minutes}min")
    df = df.assign(bucket=bucket)
    rows = []
    delta = pd.Timedelta(minutes=minutes)
    need = minutes // 5
    for b_open, g in df.groupby("bucket", sort=True):
        b_open = _ts(b_open)
        b_close = b_open + delta
        if b_close > end_wall:
            continue  # incomplete / not yet closed
        if len(g) < need:
            continue  # incomplete bucket
        # require contiguous coverage
        expected = pd.date_range(b_open, periods=need, freq="5min", tz="UTC")
        have = set(pd.to_datetime(g["timestamp"], utc=True))
        if any(t not in have for t in expected):
            continue
        g2 = g.set_index("timestamp").reindex(expected)
        rows.append(
            {
                "timestamp": b_open,
                "decision_time": b_close,
                "open": float(g2["open"].iloc[0]),
                "high": float(g2["high"].max()),
                "low": float(g2["low"].min()),
                "close": float(g2["close"].iloc[-1]),
                "volume": float(g2["volume"].fillna(0).sum()),
            }
        )
    return pd.DataFrame(rows)


def run_htf_regime_timeline(ind: pd.DataFrame) -> list[dict[str, Any]]:
    """Apply unchanged K2_H4 classifier on HTF closed bars (read-only usage)."""
    cfg = default_market_regime_config()
    clf = MarketRegimeClassifier(cfg)
    close = ind["close"].astype(float).to_numpy()
    high = ind["high"].astype(float).to_numpy()
    low = ind["low"].astype(float).to_numpy()
    ema9 = ind["ema_9"].astype(float).to_numpy()
    ema20 = ind["ema_20"].astype(float).to_numpy()
    atr = ind["atr"].astype(float).to_numpy()
    out: list[dict[str, Any]] = []
    for i in range(len(ind)):
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
        dt = _ts(ind.iloc[i]["decision_time"])
        ctx = clf.update(decision_time=dt, features=feat)
        out.append(
            {
                "decision_time": dt,
                "candle_open": _ts(ind.iloc[i]["timestamp"]),
                "regime": ctx.regime,
                "close": float(close[i]),
                "high": float(high[i]),
                "low": float(low[i]),
            }
        )
    return out


def asof_index(timeline: list[dict[str, Any]], t: pd.Timestamp) -> int | None:
    """Last closed HTF bar with decision_time <= t."""
    lo, hi = 0, len(timeline) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if timeline[mid]["decision_time"] <= t:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def regime_run_stats(timeline: list[dict[str, Any]], idx: int) -> dict[str, Any]:
    """Age and price change of current HTF regime ending at idx."""
    cur = timeline[idx]["regime"]
    j = idx
    while j > 0 and timeline[j - 1]["regime"] == cur:
        j -= 1
    start_px = timeline[j]["close"]
    end_px = timeline[idx]["close"]
    age = idx - j + 1
    return {
        "macro_trend_age_bars": age,
        "macro_trend_start": _iso(timeline[j]["decision_time"]),
        "macro_trend_price_change_pct": (end_px / start_px - 1.0) * 100.0 if start_px else 0.0,
        "macro_run_start_close": start_px,
    }


def combine_m3(r2: str | None, r4: str | None) -> str:
    s2, s4 = side_of(r2), side_of(r4)
    if s2 == "bullish" and s4 == "bullish":
        return "strong_bullish_trend"
    if s2 == "bearish" and s4 == "bearish":
        return "strong_bearish_trend"
    if {s2, s4} == {"bullish", "bearish"}:
        return "transition_unclear"
    if s2 in {"bullish", "bearish"} and s4 in {"range", "transition"}:
        return r2 or "transition_unclear"
    if s4 in {"bullish", "bearish"} and s2 in {"range", "transition"}:
        return r4 or "transition_unclear"
    if s2 == "range" and s4 == "range":
        return "accumulation_range"
    return "transition_unclear"


def post_macro_continuation(
    *,
    local_end: pd.Timestamp,
    macro_side: str,
    frame5: pd.DataFrame,
    hours: float = 12.0,
) -> bool:
    """True if price resumes macro direction after the local segment."""
    if macro_side not in {"bullish", "bearish"}:
        return False
    f = frame5
    # decision_time column
    dts = pd.to_datetime(f["decision_time"], utc=True)
    i0 = int(np.searchsorted(dts.to_numpy(dtype="datetime64[ns]"), np.datetime64(local_end.to_datetime64()), side="right") - 1)
    if i0 < 0 or i0 >= len(f) - 2:
        return False
    start_px = float(f.iloc[i0]["close"])
    # look ahead ~hours on 5m
    n = int(hours * 12)
    i1 = min(len(f) - 1, i0 + n)
    end_px = float(f.iloc[i1]["close"])
    chg = (end_px / start_px - 1.0) * 100.0
    if macro_side == "bearish":
        return chg <= -0.4
    return chg >= 0.4


def classify_relation(
    *,
    local: str,
    macro: str,
    macro_during: str,
    structure_broken: bool,
    bounce_like_size: bool,
    post_continues: bool,
    prev_regime: str,
) -> tuple[str, str]:
    """Return (classification, reason)."""
    ls, ms = local_side(local), side_of(macro)
    ms_d = side_of(macro_during)

    if ms in {"range", "transition"} and ms_d in {"range", "transition"}:
        return "range_impulse", f"macro={macro}/{macro_during}; local={local} treated as range impulse"

    aligned = ls == ms and ls in {"bullish", "bearish"}
    counter = ls in {"bullish", "bearish"} and ms in {"bullish", "bearish"} and ls != ms

    if aligned and not structure_broken:
        if prev_regime in {"accumulation_range", "transition_unclear", ""}:
            return "aligned_breakout", f"local {ls} aligned with macro {ms}; exit from {prev_regime or 'n/a'}"
        return "aligned_continuation", f"local {ls} continues macro {ms}"

    if counter:
        if structure_broken or (side_of(macro_during) == ls):
            return (
                "possible_macro_reversal",
                f"local {ls} vs macro {ms}; HTF structure appears broken or flipped during segment",
            )
        if bounce_like_size and post_continues:
            return (
                "countertrend_bounce",
                f"local {ls} against intact macro {ms}; size limited; macro resumes after",
            )
        if bounce_like_size:
            return (
                "countertrend_bounce",
                f"local {ls} against intact macro {ms}; bounce-sized move, structure not broken",
            )
        if post_continues:
            return (
                "countertrend_bounce",
                f"local {ls} against macro {ms}; macro price path resumes after segment",
            )
        return (
            "unclear",
            f"local {ls} against macro {ms}; not clearly bounce nor confirmed reversal",
        )

    if aligned and structure_broken:
        return "unclear", "aligned start but macro flipped during segment"

    return "unclear", f"local={local} macro={macro} during={macro_during}"


def analyze_segment(
    seg: dict[str, Any],
    *,
    variant: str,
    timeline: list[dict[str, Any]],
    timeline_2h: list[dict[str, Any]] | None,
    timeline_4h: list[dict[str, Any]] | None,
    frame5: pd.DataFrame,
    htf_label: str,
) -> dict[str, Any]:
    start = _ts(seg["trend_start_utc"])
    end_close = _ts(seg["trend_end_close_utc"])
    local = seg["trend_direction"]
    ls = local_side(local)

    def regime_at(tl: list[dict[str, Any]], t: pd.Timestamp) -> tuple[str | None, int | None]:
        i = asof_index(tl, t)
        if i is None:
            return None, None
        return tl[i]["regime"], i

    if variant == "M3":
        r2, i2 = regime_at(timeline_2h or [], start)
        r4, i4 = regime_at(timeline_4h or [], start)
        macro = combine_m3(r2, r4)
        # during: combine mid/end
        r2e, _ = regime_at(timeline_2h or [], end_close)
        r4e, _ = regime_at(timeline_4h or [], end_close)
        macro_during = combine_m3(r2e, r4e)
        # structure broken if either HTF flips to local side
        s2s, s2e = side_of(r2), side_of(r2e)
        s4s, s4e = side_of(r4), side_of(r4e)
        structure_broken = (s2e == ls and s2s != ls) or (s4e == ls and s4s != ls)
        # age/price from 4h if available else 2h
        stats_src = timeline_4h if timeline_4h else timeline_2h
        stats_idx = i4 if i4 is not None else i2
        stats = (
            regime_run_stats(stats_src, stats_idx)
            if stats_src is not None and stats_idx is not None
            else {
                "macro_trend_age_bars": None,
                "macro_trend_start": None,
                "macro_trend_price_change_pct": None,
                "macro_run_start_close": None,
            }
        )
        macro_at_detail = f"2h={r2};4h={r4}"
    else:
        r0, i0 = regime_at(timeline, start)
        macro = r0 or "transition_unclear"
        r_end, i_end = regime_at(timeline, end_close)
        macro_during = r_end or macro
        structure_broken = side_of(r0) in {"bullish", "bearish"} and side_of(r_end) == ls and side_of(r0) != ls
        stats = (
            regime_run_stats(timeline, i0)
            if i0 is not None
            else {
                "macro_trend_age_bars": None,
                "macro_trend_start": None,
                "macro_trend_price_change_pct": None,
                "macro_run_start_close": None,
            }
        )
        macro_at_detail = macro

    ms = side_of(macro)
    aligned = ls == ms and ls in {"bullish", "bearish"}
    counter = ls in {"bullish", "bearish"} and ms in {"bullish", "bearish"} and ls != ms

    # bounce-sized: short duration or small |move| vs macro run, or 2-bar / noise labels
    hours = _f(seg.get("duration_hours"))
    chg = abs(_f(seg.get("price_change_pct")))
    mfe = abs(_f(seg.get("mfe_pct")))
    labels = str(seg.get("existing_labels") or "")
    quality = str(seg.get("quality_classification") or "")
    bars = int(float(seg.get("duration_30m_bars") or 0))
    bounce_like = (
        bars <= 2
        or hours <= 3.0
        or chg < 1.5
        or mfe < 2.0
        or "two_bar_strong" in labels
        or quality in {"likely_noise_strong", "useful_but_short_strong", "sideways_suspicious_strong"}
    )

    post = post_macro_continuation(
        local_end=end_close, macro_side=ms, frame5=frame5, hours=12.0
    )
    classification, reason = classify_relation(
        local=local,
        macro=macro,
        macro_during=macro_during,
        structure_broken=structure_broken,
        bounce_like_size=bounce_like,
        post_continues=post,
        prev_regime=str(seg.get("previous_regime") or ""),
    )

    ctx_start = start - pd.Timedelta(hours=12)
    ctx_end = end_close + pd.Timedelta(hours=12)

    return {
        "variant": variant,
        "review_id": seg["review_id"],
        "local_start_utc": _iso(start),
        "local_end_utc": _iso(end_close),
        "local_direction": local,
        "local_regime": (
            "strong_bullish_trend" if local == "UPTREND" else "strong_bearish_trend"
        ),
        "macro_timeframe": htf_label,
        "macro_regime_at_start": macro,
        "macro_regime_during_segment": macro_during,
        "macro_regime": macro,
        "macro_detail": macro_at_detail,
        "local_side": ls,
        "macro_side": ms,
        "aligned_with_macro": aligned,
        "countertrend_to_macro": counter,
        "likely_bounce_inside_macro": classification == "countertrend_bounce",
        "macro_structure_broken": structure_broken,
        "macro_trend_age_bars": stats.get("macro_trend_age_bars"),
        "macro_trend_start": stats.get("macro_trend_start"),
        "macro_trend_price_change_pct": stats.get("macro_trend_price_change_pct"),
        "post_macro_continuation": post,
        "classification": classification,
        "reason": reason,
        "duration_30m_bars": bars,
        "duration_hours": hours,
        "price_change_pct": _f(seg.get("price_change_pct")),
        "mfe_pct": _f(seg.get("mfe_pct")),
        "mae_pct": _f(seg.get("mae_pct")),
        "quality_classification": quality,
        "existing_labels": labels,
        "previous_regime": seg.get("previous_regime"),
        "next_regime": seg.get("next_regime"),
        "review_priority": seg.get("review_priority"),
        "context_start_utc": _iso(ctx_start),
        "context_end_utc": _iso(ctx_end),
        "in_focus_jan29_feb6": bool(
            _ts(FOCUS_BEAR_START) <= start <= _ts(FOCUS_BEAR_END)
            or _ts(FOCUS_BEAR_START) <= end_close <= _ts(FOCUS_BEAR_END)
        ),
        "is_two_bar": bars <= 2,
        "is_sideways_suspicious": quality == "sideways_suspicious_strong",
        "is_likely_noise": quality == "likely_noise_strong",
    }


def variant_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter(r["classification"] for r in rows)
    n = max(len(rows), 1)
    short_unclear = sum(
        1
        for r in rows
        if r["is_two_bar"] and r["classification"] == "unclear"
    )
    # macro stability proxy: share of time local aligned among non-range macros
    aligned_n = sum(1 for r in rows if r["aligned_with_macro"])
    counter_n = sum(1 for r in rows if r["countertrend_to_macro"])
    bounce_n = sum(1 for r in rows if r["classification"] == "countertrend_bounce")
    rev_n = sum(1 for r in rows if r["classification"] == "possible_macro_reversal")
    # delay proxy for reversals: age of macro at reversal start
    rev_ages = [
        float(r["macro_trend_age_bars"])
        for r in rows
        if r["classification"] == "possible_macro_reversal" and r["macro_trend_age_bars"] is not None
    ]
    focus = [r for r in rows if r["in_focus_jan29_feb6"]]
    focus_up_as_bounce = sum(
        1
        for r in focus
        if r["local_direction"] == "UPTREND" and r["classification"] == "countertrend_bounce"
    )
    focus_up = sum(1 for r in focus if r["local_direction"] == "UPTREND")
    return {
        "n_segments": len(rows),
        "n_countertrend_to_macro": counter_n,
        "n_aligned_with_macro": aligned_n,
        "n_countertrend_bounce": bounce_n,
        "n_possible_macro_reversal": rev_n,
        "n_aligned_breakout": c["aligned_breakout"],
        "n_aligned_continuation": c["aligned_continuation"],
        "n_range_impulse": c["range_impulse"],
        "n_unclear": c["unclear"],
        "n_two_bar_still_unclear": short_unclear,
        "share_bounce_of_countertrend": (bounce_n / counter_n) if counter_n else None,
        "share_reversal_of_countertrend": (rev_n / counter_n) if counter_n else None,
        "median_macro_age_at_reversal_bars": float(np.median(rev_ages)) if rev_ages else None,
        "focus_jan29_feb6_uptrends": focus_up,
        "focus_jan29_feb6_up_as_bounce": focus_up_as_bounce,
        "classification_counts": dict(c),
    }


def decide(comp: list[dict[str, Any]]) -> tuple[str, str]:
    """J / N / U from variant comparison."""
    by = {r["variant"]: r for r in comp}
    # Prefer a variant that converts many countertrends to bounces without exploding reversals
    best = None
    best_score = -1e9
    for v, m in by.items():
        counter = m["n_countertrend_to_macro"] or 0
        bounce = m["n_countertrend_bounce"] or 0
        rev = m["n_possible_macro_reversal"] or 0
        unclear = m["n_unclear"] or 0
        focus_up = m["focus_jan29_feb6_uptrends"] or 0
        focus_b = m["focus_jan29_feb6_up_as_bounce"] or 0
        bounce_share = (bounce / counter) if counter else 0.0
        focus_share = (focus_b / focus_up) if focus_up else 0.0
        # delay: high median age at reversal is bad if many reversals
        delay = m.get("median_macro_age_at_reversal_bars") or 0.0
        score = (
            40 * bounce_share
            + 25 * focus_share
            + 10 * (1.0 - min(1.0, unclear / max(m["n_segments"], 1)))
            - 15 * (rev / max(counter, 1))
            - 0.5 * delay
        )
        if score > best_score:
            best_score = score
            best = v

    m = by[best] if best else {}
    bounce_share = m.get("share_bounce_of_countertrend") or 0
    focus_up = m.get("focus_jan29_feb6_uptrends") or 0
    focus_b = m.get("focus_jan29_feb6_up_as_bounce") or 0
    rev = m.get("n_possible_macro_reversal") or 0
    counter = m.get("n_countertrend_to_macro") or 1
    delay = m.get("median_macro_age_at_reversal_bars")

    if bounce_share >= 0.45 and (focus_up == 0 or focus_b / focus_up >= 0.4) and rev / counter <= 0.45:
        return (
            "J",
            f"Macro-Kontext ({best}) trennt lokale Bounces sinnvoll von möglichen Reversals "
            f"(bounce_share={bounce_share:.2f}, focus_up_bounce={focus_b}/{focus_up}).",
        )
    if bounce_share < 0.25 or (delay is not None and delay >= 8 and rev >= 8):
        return (
            "N",
            f"Macro-Kontext erzeugt zu wenig Bounce-Trennung oder bindet zu lange an alte Trends "
            f"(best={best}, bounce_share={bounce_share:.2f}, median_age_rev={delay}).",
        )
    return (
        "U",
        f"Ergebnis nicht eindeutig (best={best}, bounce_share={bounce_share:.2f}, "
        f"reversals={rev}, unclear={m.get('n_unclear')}). Noch keine Variante übernehmen.",
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
        "market_regime.py": _md5(MARKET),
    }
    _write_json(OUT / "hashes_before.json", hashes_before)

    if not TRENDS.exists():
        raise SystemExit(f"missing {TRENDS}")

    segments = _read_csv(TRENDS)
    _p(f"loaded {len(segments)} strong segments")

    end_wall = _ts(AUDIT_END)
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    raw = raw.sort_values("timestamp")
    sl = raw[(raw["timestamp"] >= _ts(LOAD_START)) & (raw["timestamp"] <= _ts("2026-03-16 23:55:00+00:00"))]

    scfg = default_regime_scanner_config()
    # 5m frame for post-continuation price path
    frame5 = compute_indicator_frame(sl, config=scfg)
    frame5["timestamp"] = pd.to_datetime(frame5["timestamp"], utc=True)
    frame5["decision_time"] = frame5["timestamp"] + pd.Timedelta(minutes=5)
    frame5 = frame5.loc[frame5["decision_time"] <= end_wall].reset_index(drop=True)

    ohlcv5 = frame5[["timestamp", "open", "high", "low", "close", "volume"]].copy()

    agg2 = aggregate_closed_htf(ohlcv5, 120, end_wall)
    agg4 = aggregate_closed_htf(ohlcv5, 240, end_wall)
    _p(f"htf bars 2h={len(agg2)} 4h={len(agg4)}")

    ind2 = compute_indicator_frame(agg2, config=scfg).copy()
    ind2["timestamp"] = pd.to_datetime(ind2["timestamp"], utc=True)
    ind2["decision_time"] = pd.to_datetime(agg2["decision_time"], utc=True).to_numpy()

    ind4 = compute_indicator_frame(agg4, config=scfg).copy()
    ind4["timestamp"] = pd.to_datetime(ind4["timestamp"], utc=True)
    ind4["decision_time"] = pd.to_datetime(agg4["decision_time"], utc=True).to_numpy()

    tl2 = run_htf_regime_timeline(ind2)
    tl4 = run_htf_regime_timeline(ind4)
    _p(f"htf regimes 2h={len(tl2)} 4h={len(tl4)}")

    all_rows: list[dict[str, Any]] = []
    for seg in segments:
        all_rows.append(
            analyze_segment(
                seg,
                variant="M1",
                timeline=tl2,
                timeline_2h=tl2,
                timeline_4h=tl4,
                frame5=frame5,
                htf_label="2h",
            )
        )
        all_rows.append(
            analyze_segment(
                seg,
                variant="M2",
                timeline=tl4,
                timeline_2h=tl2,
                timeline_4h=tl4,
                frame5=frame5,
                htf_label="4h",
            )
        )
        all_rows.append(
            analyze_segment(
                seg,
                variant="M3",
                timeline=tl2,
                timeline_2h=tl2,
                timeline_4h=tl4,
                frame5=frame5,
                htf_label="2h+4h",
            )
        )

    _write_csv(OUT / "strong_segments_with_macro_context.csv", all_rows)

    # Prefer M3 for chart-facing exports (document in metadata); still keep all variants in main CSV
    primary = [r for r in all_rows if r["variant"] == "M3"]
    _write_csv(OUT / "countertrend_bounces.csv", [r for r in primary if r["classification"] == "countertrend_bounce"])
    _write_csv(
        OUT / "aligned_strong_segments.csv",
        [r for r in primary if r["classification"] in {"aligned_breakout", "aligned_continuation"}],
    )
    _write_csv(
        OUT / "possible_macro_reversals.csv",
        [r for r in primary if r["classification"] == "possible_macro_reversal"],
    )

    chart_rows = [
        {
            "review_id": r["review_id"],
            "variant": r["variant"],
            "local_start_utc": r["local_start_utc"],
            "local_end_utc": r["local_end_utc"],
            "local_direction": r["local_direction"],
            "macro_regime": r["macro_regime"],
            "macro_timeframe": r["macro_timeframe"],
            "classification": r["classification"],
            "reason": r["reason"],
            "context_start_utc": r["context_start_utc"],
            "context_end_utc": r["context_end_utc"],
            "aligned_with_macro": r["aligned_with_macro"],
            "countertrend_to_macro": r["countertrend_to_macro"],
            "likely_bounce_inside_macro": r["likely_bounce_inside_macro"],
            "macro_structure_broken": r["macro_structure_broken"],
            "quality_classification": r["quality_classification"],
            "in_focus_jan29_feb6": r["in_focus_jan29_feb6"],
        }
        for r in all_rows
    ]
    _write_csv(OUT / "chart_review_macro_context.csv", chart_rows)

    # Human text: focus + two-bar + noise + sideways for M3
    focus_lines = ["MACRO CONTEXT CHART REVIEW (primary variant M3)", ""]
    interesting = [
        r
        for r in primary
        if r["in_focus_jan29_feb6"]
        or r["is_two_bar"]
        or r["is_sideways_suspicious"]
        or r["is_likely_noise"]
        or r["classification"] in {"countertrend_bounce", "possible_macro_reversal"}
    ]
    interesting.sort(key=lambda r: r["local_start_utc"])
    for r in interesting:
        focus_lines += [
            "=" * 60,
            f"{r['review_id']}  {r['local_direction']} @ {r['local_start_utc']} -> {r['local_end_utc']}",
            f"macro({r['macro_timeframe']}): {r['macro_regime']}  during={r['macro_regime_during_segment']}",
            f"class: {r['classification']}",
            f"reason: {r['reason']}",
            f"quality: {r['quality_classification']}  labels: {r['existing_labels']}",
            f"chart context: {r['context_start_utc']} -> {r['context_end_utc']}",
            f"flags: aligned={r['aligned_with_macro']} counter={r['countertrend_to_macro']} "
            f"bounce={r['likely_bounce_inside_macro']} broken={r['macro_structure_broken']}",
            "=" * 60,
            "",
        ]
    (OUT / "chart_review_macro_context.txt").write_text("\n".join(focus_lines), encoding="utf-8")

    metrics = []
    for v in ("M1", "M2", "M3"):
        m = variant_metrics([r for r in all_rows if r["variant"] == v])
        m["variant"] = v
        metrics.append(m)
    _write_csv(
        OUT / "variant_comparison.csv",
        [
            {
                "variant": m["variant"],
                **{k: v for k, v in m.items() if k not in {"variant", "classification_counts"}},
                "classification_counts": json.dumps(m["classification_counts"]),
            }
            for m in metrics
        ],
    )

    decision, note = decide(metrics)

    hashes_after = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
        "market_regime.py": _md5(MARKET),
    }
    assert hashes_before == hashes_after
    _write_json(OUT / "hashes_after.json", hashes_after)

    meta = {
        "read_only": True,
        "market_regime_unchanged": True,
        "variants": {
            "M1": "macro from closed 2h K2_H4 classifier",
            "M2": "macro from closed 4h K2_H4 classifier",
            "M3": "combine 2h+4h (agree strong / conflict→transition / one strong+neutral→strong)",
        },
        "no_lookahead": True,
        "htf_aggregation": "audit-local complete buckets only; close<=asof",
        "classifications": [
            "aligned_breakout",
            "aligned_continuation",
            "countertrend_bounce",
            "possible_macro_reversal",
            "range_impulse",
            "unclear",
        ],
        "primary_chart_variant": "M3",
        "focus_window": {"start": FOCUS_BEAR_START, "end": FOCUS_BEAR_END},
        "n_source_segments": len(segments),
    }
    _write_json(OUT / "audit_metadata.json", meta)

    # special counts
    two_bar = [r for r in primary if r["is_two_bar"]]
    sideways = [r for r in primary if r["is_sideways_suspicious"]]
    noise = [r for r in primary if r["is_likely_noise"]]
    focus_up = [
        r
        for r in primary
        if r["in_focus_jan29_feb6"] and r["local_direction"] == "UPTREND"
    ]

    summary = {
        "decision": decision,
        "note": note,
        "variant_metrics": metrics,
        "special": {
            "two_bar_n": len(two_bar),
            "two_bar_class_counts": dict(Counter(r["classification"] for r in two_bar)),
            "sideways_suspicious_n": len(sideways),
            "sideways_class_counts": dict(Counter(r["classification"] for r in sideways)),
            "likely_noise_n": len(noise),
            "noise_class_counts": dict(Counter(r["classification"] for r in noise)),
            "focus_jan29_feb6_uptrends": [
                {
                    "review_id": r["review_id"],
                    "start": r["local_start_utc"],
                    "end": r["local_end_utc"],
                    "macro": r["macro_regime"],
                    "class": r["classification"],
                }
                for r in focus_up
            ],
        },
        "hashes": hashes_after,
        "policy_wired": False,
    }
    _write_json(OUT / "summary.json", summary)

    (OUT / "final_recommendation.md").write_text(
        f"""# Macro-context audit (read-only)

**Decision: {decision}** — {note}

## Variants

{json.dumps(metrics, indent=2)}

## Focus 29.01–06.02 uptrends (M3)

{json.dumps(summary['special']['focus_jan29_feb6_uptrends'], indent=2)}

## Hashes unchanged

{json.dumps(hashes_after, indent=2)}

No variant adopted into classifier.
""",
        encoding="utf-8",
    )
    (OUT / "README.md").write_text(
        f"Read-only macro context audit. Decision={decision}. See summary.json.\n",
        encoding="utf-8",
    )
    _p(f"DONE decision={decision}")
    print(note)


if __name__ == "__main__":
    main()
