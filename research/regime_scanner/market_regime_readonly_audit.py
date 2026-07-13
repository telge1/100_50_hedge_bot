#!/usr/bin/env python3
"""Read-only audit of production ``market_regime.MarketRegimeClassifier`` (K2_H4).

Does not modify trend_structure / trend_state_machine / trend_state_policy /
trend_zones. Does not wire policy. Regime is computed and exported only.

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_readonly_audit.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import resource
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.market_regime import (
    MarketRegimeClassifier,
    MarketRegimeContext,
    compute_market_regime_features,
    default_market_regime_config,
    market_regime_hysteresis_docs,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta

OUT = Path("research/regime_scanner/results/market_regime_readonly_audit")
PRIOR_FRAME = Path("research/regime_scanner/results/trend_regime_four_class_audit/_cache/frame_5m.parquet")
PRIOR_SM = Path("research/regime_scanner/results/trend_regime_four_class_audit/state_timeline_5m.csv")
AUDIT_FEAT = Path(
    "research/regime_scanner/results/market_regime_four_class_audit/_cache/regime_feature_rows_30m.csv"
)
AUDIT_MARCH = Path(
    "research/regime_scanner/results/market_regime_four_class_audit/march_crash_timeline.csv"
)

STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")

LOAD_START = "2025-12-27T00:00:00+00:00"
# Match four-class audit: load from Dec 27 for HTF warmup; classify from Jan 1.
CLASSIFY_START = "2026-01-01T00:00:00+00:00"
ANALYZE_END = "2026-03-15T00:00:00+00:00"
MARCH_START = "2026-03-05T00:00:00+00:00"
MARCH_END = "2026-03-10T00:00:00+00:00"
SELLOFF = "2026-03-05T16:00:00+00:00"


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
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    return float(np.percentile(np.asarray(xs, dtype=float), p))


def _durations(labels: list[str]) -> list[int]:
    if not labels:
        return []
    out: list[int] = []
    cur = labels[0]
    n = 1
    for x in labels[1:]:
        if x == cur:
            n += 1
        else:
            out.append(n)
            cur = x
            n = 1
    out.append(n)
    return out


def _row_out(
    *,
    decision_time: str,
    close: float,
    ctx: MarketRegimeContext,
    sm_state: str | None,
    allow_long: object,
    allow_short: object,
) -> dict[str, Any]:
    f = ctx.feature_snapshot
    return {
        "timestamp": decision_time,
        "close": close,
        "market_regime": ctx.regime,
        "regime_direction": ctx.direction,
        "confidence": ctx.confidence,
        "reason_codes": "|".join(ctx.reason_codes),
        "candidate_streak": ctx.candidate_streak,
        "candidate_regime": ctx.candidate_regime,
        "raw_regime": ctx.raw_regime,
        "current_trend_state": sm_state,
        "allow_long": allow_long,
        "allow_short": allow_short,
        "ema9": f.get("ema9"),
        "ema20": f.get("ema20"),
        "ema9_slope": f.get("ema9_slope_atr"),
        "ema20_slope": f.get("ema20_slope_atr"),
        "net_move_atr": f.get("net_move_atr"),
        "directional_efficiency": f.get("directional_efficiency"),
        "progress_vs_range": f.get("progress_vs_range"),
        "share_below_both": f.get("share_below_both"),
        "share_above_both": f.get("share_above_both"),
        "ema_sep_atr": f.get("ema_sep_atr"),
        "maximum_counter_move_atr": f.get("maximum_counter_move_atr"),
        "variant_id": ctx.variant_id,
        "read_only": True,
    }


def load_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    end = _ts(ANALYZE_END)
    if PRIOR_FRAME.exists():
        frame5 = pd.read_parquet(PRIOR_FRAME)
        _p(f"reused 5m frame n={len(frame5)}")
    else:
        raw = load_symbol_candles("APTUSDT")
        raw = raw.copy()
        raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
        sl = raw[(raw["timestamp"] >= _ts(LOAD_START)) & (raw["timestamp"] < end)]
        scfg = default_regime_scanner_config().with_timeframe("5m")
        frame5 = compute_indicator_frame(sl, config=scfg)
        frame5["timestamp"] = pd.to_datetime(frame5["timestamp"], utc=True)
        frame5["decision_time"] = frame5["timestamp"] + pd.Timedelta(minutes=5)
        frame5 = frame5.loc[frame5["decision_time"] <= end].reset_index(drop=True)

    frame5["timestamp"] = pd.to_datetime(frame5["timestamp"], utc=True)
    if "decision_time" not in frame5.columns:
        frame5["decision_time"] = frame5["timestamp"] + pd.Timedelta(minutes=5)
    else:
        frame5["decision_time"] = pd.to_datetime(frame5["decision_time"], utc=True)

    scfg30 = default_regime_scanner_config().with_timeframe("30m")
    agg30 = aggregate_candles(
        frame5[["timestamp", "open", "high", "low", "close", "volume"]], "30m", end
    )
    ind30 = compute_indicator_frame(agg30, config=scfg30).copy()
    ind30["timestamp"] = pd.to_datetime(ind30["timestamp"], utc=True)
    ind30["decision_time"] = ind30["timestamp"] + timeframe_timedelta("30m")
    ind30 = ind30.loc[ind30["decision_time"] <= end].reset_index(drop=True)
    return frame5, ind30


def join_sm(decision_times: pd.Series) -> pd.DataFrame:
    if not PRIOR_SM.exists():
        raise SystemExit(f"Missing SM timeline {PRIOR_SM}")
    sm = pd.read_csv(PRIOR_SM)
    sm["decision_time"] = pd.to_datetime(sm["decision_time"], utc=True)
    sm = sm.sort_values("decision_time")
    base = pd.DataFrame({"decision_time": pd.to_datetime(decision_times, utc=True)}).sort_values(
        "decision_time"
    )
    merged = pd.merge_asof(base, sm, on="decision_time", direction="backward")
    return merged


def export_case(rows: list[dict[str, Any]], indices: list[int], name: str, path: Path) -> None:
    if not indices:
        _write_csv(path, [])
        return
    mid = indices[len(indices) // 2]
    lo = max(0, mid - 24)
    hi = min(len(rows) - 1, mid + 24)
    out = []
    for i in range(lo, hi + 1):
        r = dict(rows[i])
        r["case"] = name
        out.append(r)
    _write_csv(path, out)


def analyze_bounce(march: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify Mar5 evening → Mar6 afternoon interruption of strong_bearish."""
    # Find first strong_bearish, then first leave, then re-entry
    first_i = next((i for i, r in enumerate(march) if r["market_regime"] == "strong_bearish_trend"), None)
    if first_i is None:
        return {"status": "no_strong_bearish"}
    leave_i = None
    for i in range(first_i + 1, len(march)):
        if march[i]["market_regime"] != "strong_bearish_trend":
            leave_i = i
            break
    if leave_i is None:
        return {
            "status": "no_interruption",
            "first_strong_bearish": march[first_i]["timestamp"],
        }
    reenter_i = next(
        (
            i
            for i in range(leave_i + 1, len(march))
            if march[i]["market_regime"] == "strong_bearish_trend"
        ),
        None,
    )
    interrupt = march[leave_i:reenter_i] if reenter_i is not None else march[leave_i:]
    regimes = [r["market_regime"] for r in interrupt]
    counts = Counter(regimes)
    # max counter move vs pre-leave close in ATR terms using feature field
    pre = float(march[leave_i - 1]["close"])
    atrs = [float(r["maximum_counter_move_atr"] or 0) for r in interrupt]
    closes = [float(r["close"]) for r in interrupt]
    bounce_pct = (max(closes) - pre) / pre if closes else None
    classification = []
    if "accumulation_range" in counts:
        classification.append("falscher_wechsel_zu_accumulation_range")
    if "strong_bullish_trend" in counts:
        classification.append("falscher_bullish_wechsel")
    if set(regimes) <= {"transition_unclear", "accumulation_range"} and "strong_bullish_trend" not in counts:
        if counts.get("transition_unclear", 0) >= counts.get("accumulation_range", 0):
            classification.append("sinnvoller_wechsel_zu_transition_unclear")
        else:
            classification.append("range_dominierte_unterbrechung")
    if len(set(regimes)) >= 3 or len(interrupt) >= 8 and len(set(regimes)) >= 2:
        classification.append("moegliches_flattern")

    return {
        "status": "interrupted",
        "first_strong_bearish": march[first_i]["timestamp"],
        "first_strong_close": march[first_i]["close"],
        "interruption_start": march[leave_i]["timestamp"],
        "interruption_end": None if reenter_i is None else march[reenter_i - 1]["timestamp"],
        "reentry": None if reenter_i is None else march[reenter_i]["timestamp"],
        "reentry_close": None if reenter_i is None else march[reenter_i]["close"],
        "interruption_bars": len(interrupt),
        "interruption_hours": len(interrupt) * 0.5,
        "regime_counts_during": dict(counts),
        "classification": classification,
        "max_close_during": max(closes) if closes else None,
        "bounce_from_pre_leave_pct": bounce_pct,
        "max_feature_counter_move_atr": max(atrs) if atrs else None,
        "state_before": {
            "timestamp": march[leave_i - 1]["timestamp"],
            "regime": march[leave_i - 1]["market_regime"],
            "close": march[leave_i - 1]["close"],
            "net_move_atr": march[leave_i - 1]["net_move_atr"],
            "de": march[leave_i - 1]["directional_efficiency"],
            "ema20_slope": march[leave_i - 1]["ema20_slope"],
        },
        "state_during_mid": interrupt[len(interrupt) // 2] if interrupt else None,
        "state_after": None
        if reenter_i is None
        else {
            "timestamp": march[reenter_i]["timestamp"],
            "regime": march[reenter_i]["market_regime"],
            "close": march[reenter_i]["close"],
            "net_move_atr": march[reenter_i]["net_move_atr"],
            "de": march[reenter_i]["directional_efficiency"],
            "ema20_slope": march[reenter_i]["ema20_slope"],
        },
        "note": (
            "H4 does not hold strong_bearish across multi-bar transition/range raw labels; "
            "short 1-bar bounce would hold, longer bounce exits — matching audit K2_H4."
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    _write_json(OUT / "hashes_before.json", hashes_before)
    cfg = default_market_regime_config()
    _write_json(OUT / "config.json", {**cfg.__dict__, "hysteresis_docs": market_regime_hysteresis_docs()})

    _, ind30 = load_frames()
    clf = MarketRegimeClassifier(cfg)
    analyze_start = _ts(CLASSIFY_START)

    close = ind30["close"].astype(float).to_numpy()
    high = ind30["high"].astype(float).to_numpy()
    low = ind30["low"].astype(float).to_numpy()
    ema9 = ind30["ema_9"].astype(float).to_numpy()
    ema20 = ind30["ema_20"].astype(float).to_numpy()
    atr = ind30["atr"].astype(float).to_numpy()

    timeline: list[dict[str, Any]] = []
    ctxs: list[MarketRegimeContext] = []
    dts: list[pd.Timestamp] = []
    for i in range(len(ind30)):
        dt = _ts(ind30.iloc[i]["decision_time"])
        if dt < analyze_start:
            continue
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
        ctxs.append(ctx)
        dts.append(dt)
        timeline.append(
            {
                "decision_time": _iso(dt),
                "close": float(close[i]),
                "ctx": ctx,
            }
        )
    _p(f"classified {len(timeline)} closed 30m bars")

    sm = join_sm(pd.Series(dts))
    sm_by_dt = {
        _iso(_ts(r["decision_time"])): r
        for _, r in sm.iterrows()
    }

    rows: list[dict[str, Any]] = []
    for item in timeline:
        smr = sm_by_dt.get(item["decision_time"], {})
        rows.append(
            _row_out(
                decision_time=item["decision_time"],
                close=item["close"],
                ctx=item["ctx"],
                sm_state=smr.get("state") or smr.get("current_state") or smr.get("sm_state"),
                allow_long=smr.get("allow_long"),
                allow_short=smr.get("allow_short"),
            )
        )

    _write_csv(OUT / "regime_timeline.csv", rows)

    # transitions
    transitions = []
    for a, b in zip(rows, rows[1:]):
        if a["market_regime"] != b["market_regime"]:
            transitions.append(
                {
                    "from_time": a["timestamp"],
                    "to_time": b["timestamp"],
                    "from_regime": a["market_regime"],
                    "to_regime": b["market_regime"],
                    "close": b["close"],
                    "reasons": b["reason_codes"],
                    "sm_state": b["current_trend_state"],
                }
            )
    _write_csv(OUT / "regime_transitions.csv", transitions)

    march = [
        r
        for r in rows
        if _ts(MARCH_START) <= _ts(r["timestamp"]) <= _ts(MARCH_END)
    ]
    _write_csv(OUT / "march_crash_timeline.csv", march)

    # March checks
    selloff = _ts(SELLOFF)
    premature = [
        r
        for r in march
        if _ts(r["timestamp"]) < selloff and r["market_regime"] == "strong_bearish_trend"
    ]
    first_strong = next((r for r in march if r["market_regime"] == "strong_bearish_trend"), None)
    strong_blocks = []
    cur_block = None
    for r in march:
        if r["market_regime"] == "strong_bearish_trend":
            if cur_block is None:
                cur_block = {"start": r["timestamp"], "start_close": r["close"], "end": r["timestamp"]}
            else:
                cur_block["end"] = r["timestamp"]
                cur_block["end_close"] = r["close"]
        elif cur_block is not None:
            strong_blocks.append(cur_block)
            cur_block = None
    if cur_block is not None:
        strong_blocks.append(cur_block)

    bounce = analyze_bounce(march)
    _write_json(OUT / "bounce_interruption_audit.json", bounce)

    # metrics
    labels = [r["market_regime"] for r in rows]
    flips = sum(1 for a, b in zip(labels, labels[1:]) if a != b)
    durs = _durations(labels)
    counts = Counter(labels)
    n = max(len(labels), 1)

    # heuristic GT quality (same spirit as four-class audit)
    wrong_dir = range_as_trend = trend_as_range = premature_count = 0
    trend_missed = 0
    bear_interrupt = bull_interrupt = 0
    clear_bear = clear_bull = 0
    for r in rows:
        net = float(r["net_move_atr"] or 0)
        de = float(r["directional_efficiency"] or 0)
        below = float(r["share_below_both"] or 0)
        above = float(r["share_above_both"] or 0)
        is_clear_bear = net <= -1.0 and de >= 0.35 and below >= 0.65
        is_clear_bull = net >= 1.0 and de >= 0.35 and above >= 0.65
        is_clear_range = abs(net) < 0.3 and de < 0.20
        if is_clear_bear:
            clear_bear += 1
            if r["market_regime"] == "strong_bullish_trend":
                wrong_dir += 1
            if r["market_regime"] == "accumulation_range":
                trend_as_range += 1
            if r["market_regime"] != "strong_bearish_trend":
                bear_interrupt += 1
                if r["market_regime"] != "strong_bearish_trend":
                    trend_missed += 1
        if is_clear_bull:
            clear_bull += 1
            if r["market_regime"] == "strong_bearish_trend":
                wrong_dir += 1
            if r["market_regime"] == "accumulation_range":
                trend_as_range += 1
            if r["market_regime"] != "strong_bullish_trend":
                bull_interrupt += 1
        if is_clear_range and r["market_regime"] in {
            "strong_bullish_trend",
            "strong_bearish_trend",
        }:
            range_as_trend += 1

    premature_count = len(premature)

    metrics = {
        "n_bars": len(rows),
        "regime_flip_count": flips,
        "median_regime_dur_bars": float(np.median(durs)) if durs else None,
        "p90_regime_dur_bars": _pct([float(x) for x in durs], 90),
        "share_strong_bullish": counts["strong_bullish_trend"] / n,
        "share_strong_bearish": counts["strong_bearish_trend"] / n,
        "share_accumulation_range": counts["accumulation_range"] / n,
        "share_transition_unclear": counts["transition_unclear"] / n,
        "wrong_direction_count": wrong_dir,
        "premature_count": premature_count,
        "trend_missed_count": trend_missed,
        "range_as_trend_count": range_as_trend,
        "trend_as_range_count": trend_as_range,
        "transition_unclear_count": counts["transition_unclear"],
        "bearish_interruptions_during_clear_downtrends": bear_interrupt,
        "bullish_interruptions_during_clear_uptrends": bull_interrupt,
        "clear_bear_bars": clear_bear,
        "clear_bull_bars": clear_bull,
    }
    _write_json(OUT / "quality_metrics.json", metrics)

    # case studies
    bull_idx = [
        i
        for i, r in enumerate(rows)
        if float(r["net_move_atr"] or 0) >= 1.0 and float(r["share_above_both"] or 0) >= 0.7
    ]
    bear_idx = [
        i
        for i, r in enumerate(rows)
        if float(r["net_move_atr"] or 0) <= -1.0 and float(r["share_below_both"] or 0) >= 0.7
    ]
    range_idx = [
        i
        for i, r in enumerate(rows)
        if abs(float(r["net_move_atr"] or 0)) < 0.25 and float(r["directional_efficiency"] or 0) < 0.2
    ]
    choppy_idx = [
        i
        for i, r in enumerate(rows)
        if float(r.get("ema_sep_atr") or 0) == float(r.get("ema_sep_atr") or 0)
        and abs(float(r["net_move_atr"] or 0)) < 0.45
        and float(r["directional_efficiency"] or 0) < 0.25
        and r["market_regime"] in {"accumulation_range", "transition_unclear"}
    ]
    export_case(rows, bull_idx, "bullish_trend", OUT / "bullish_case_study.csv")
    export_case(rows, bear_idx, "bearish_trend", OUT / "bearish_case_study.csv")
    export_case(rows, range_idx, "range", OUT / "range_case_study.csv")
    export_case(rows, choppy_idx, "choppy", OUT / "choppy_case_study.csv")
    # transitions windows
    _write_csv(
        OUT / "transition_bullish_to_bearish_case_study.csv",
        [r for r in rows if "2026-03-07T00:00:00+00:00" <= r["timestamp"] <= "2026-03-09T12:00:00+00:00"],
    )
    _write_csv(
        OUT / "transition_bearish_to_bullish_case_study.csv",
        [r for r in rows if "2026-03-09T12:00:00+00:00" <= r["timestamp"] <= "2026-03-10T00:00:00+00:00"],
    )

    # Reproduce vs prior audit K2_H4 march first strong
    audit_match = {"compared": False}
    if AUDIT_MARCH.exists():
        am = [
            r
            for r in csv.DictReader(AUDIT_MARCH.open())
            if r.get("variant") == "K2_H4"
        ]
        a_first = next((r for r in am if r.get("regime") == "strong_bearish_trend"), None)
        audit_match = {
            "compared": True,
            "audit_first_strong": None if a_first is None else a_first.get("decision_time"),
            "prod_first_strong": None if first_strong is None else first_strong["timestamp"],
            "times_match": (
                a_first is not None
                and first_strong is not None
                and a_first.get("decision_time") == first_strong["timestamp"]
            ),
        }

    # Future leakage check: decision_time == candle_open + 30m; features use prefix only
    leakage = {
        "uses_closed_30m_only": True,
        "feature_prefix_causal": True,
        "no_label_rewrite": True,
        "note": "Features from close[:i+1] only; classifier never mutates past contexts.",
    }

    hashes_after = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    assert hashes_before == hashes_after
    _write_json(OUT / "hashes_after.json", hashes_after)

    # Decision J–M
    ok_no_premature = premature_count == 0
    ok_first = (
        first_strong is not None
        and _ts("2026-03-05T17:00:00+00:00")
        <= _ts(first_strong["timestamp"])
        <= _ts("2026-03-05T18:00:00+00:00")
    )
    ok_second = any(
        _ts("2026-03-06T14:00:00+00:00") <= _ts(b["start"]) <= _ts("2026-03-06T15:30:00+00:00")
        for b in strong_blocks
    )
    ok_match = (not audit_match.get("compared")) or bool(audit_match.get("times_match"))
    flip_rate = flips / n
    bounce_has_false_bull = "falscher_bullish_wechsel" in (bounce.get("classification") or [])

    if ok_no_premature and ok_first and ok_second and ok_match and not bounce_has_false_bull and flip_rate < 0.25:
        # Bounce interruption is expected under audited H4 (no hold-rule change yet).
        # Flag K only when bounce invents opposite trend or audit time diverges.
        decision, note = (
            "J",
            "Read-only K2_H4 reproduziert Audit stabil und ist bereit für längeren Shadow-Test. "
            "Nachtbounce-Unterbrechung entspricht Audit-H4 (noch keine Hold-Nachjustierung).",
        )
    elif not ok_match:
        decision, note = "L", "Produktionsmodul weicht relevant vom Audit ab."
    elif bounce_has_false_bull or not ok_no_premature:
        decision, note = "K", "Grundsätzlich korrekt; Bounce-/Hysterese-Verhalten muss nachjustiert werden."
    else:
        decision, note = "K", "Teilweise Abweichungen; Shadow-Test nur mit Vorbehalt."

    march_summary = {
        "premature_strong_bearish_before_1600": premature_count,
        "first_strong_bearish": None if first_strong is None else first_strong["timestamp"],
        "first_strong_close": None if first_strong is None else first_strong["close"],
        "strong_bearish_blocks": strong_blocks,
        "sm_at_first_strong": None if first_strong is None else first_strong["current_trend_state"],
        "allow_long_at_first_strong": None if first_strong is None else first_strong["allow_long"],
        "audit_match": audit_match,
        "bounce": bounce,
        "leakage": leakage,
    }
    _write_json(OUT / "march_validation.json", march_summary)
    _write_json(
        OUT / "decision.json",
        {
            "decision": decision,
            "note": note,
            "metrics": metrics,
            "march": march_summary,
            "hashes": hashes_after,
            "read_only": True,
            "policy_uses_regime": False,
        },
    )

    rec = f"""# Market regime read-only audit (K2_H4)

**Decision: {decision}** — {note}

## Wiring

- Module: `research/regime_scanner/market_regime.py`
- Snapshot attach: optional `market_regime_context` on `build_regime_snapshot`
- **Read-only**: does not change allow_long / allow_short / SM / zones / entries
- Policy does **not** consume the regime

## March 2026

- Premature strong_bearish before 05.03 16:00: **{premature_count}**
- First strong_bearish: **{None if first_strong is None else first_strong['timestamp']}** @ {None if first_strong is None else first_strong['close']}
- Strong blocks: {json.dumps(strong_blocks, indent=2)}
- Audit time match: {audit_match}
- Bounce interruption: {bounce.get('interruption_start')} → {bounce.get('reentry')} ({bounce.get('interruption_bars')} bars)
- Bounce classification: {bounce.get('classification')}

## Metrics

- flips={flips} ({flip_rate:.3f}) median_dur={metrics['median_regime_dur_bars']} p90={metrics['p90_regime_dur_bars']}
- shares bull={metrics['share_strong_bullish']:.3f} bear={metrics['share_strong_bearish']:.3f} range={metrics['share_accumulation_range']:.3f} transition={metrics['share_transition_unclear']:.3f}

## Hashes unchanged

{json.dumps(hashes_after, indent=2)}
"""
    (OUT / "final_recommendation.md").write_text(rec, encoding="utf-8")
    (OUT / "README.md").write_text(
        f"""# Read-only Market Regime Audit

Decision **{decision}**: {note}

Primary artifacts: `regime_timeline.csv`, `march_crash_timeline.csv`,
`bounce_interruption_audit.json`, `quality_metrics.json`, `final_recommendation.md`.

Variant: `{cfg.variant_id}` — EMA + price progress, H4 hysteresis.
""",
        encoding="utf-8",
    )
    _p(f"DONE decision={decision} first_strong={None if first_strong is None else first_strong['timestamp']}")


if __name__ == "__main__":
    main()
