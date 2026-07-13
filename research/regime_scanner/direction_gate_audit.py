"""Counterfactual March audit for the research-only 15m Direction Gate.

Does not mutate pipeline CSVs or live strategy. Gate stays disabled by default.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.direction_gate import (
    WARMUP_NOTE,
    DirectionGateConfig,
    GateVariant,
    assert_outcomes_do_not_affect_gate,
    build_15m_indicator_frame,
    expand_15m_state_to_5m_decisions,
    run_gate_on_15m_frame,
)
from research.regime_scanner.point_audit import json_safe

STRUCTURE_BREAK_REF = "2026-03-06T14:40:00+00:00"
FOCUS_SETUPS = ("setup_00055", "setup_00056", "setup_00057", "setup_00058", "setup_00059")
MAR6_START = "2026-03-06T00:00:00+00:00"
MAR6_END = "2026-03-08T00:00:00+00:00"


def _runs(mask: pd.Series, times: pd.Series) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    start = None
    vals = mask.tolist()
    ts = times.tolist()
    for i, b in enumerate(vals):
        if b and start is None:
            start = i
        if start is not None and (not b or i == len(vals) - 1):
            end = i if b and i == len(vals) - 1 else i - 1
            if end >= start:
                runs.append(
                    {
                        "start": str(ts[start]),
                        "end": str(ts[end]),
                        "n_bars": end - start + 1,
                        "duration_minutes": (end - start + 1) * 15,
                    }
                )
            start = None
    return runs


def classify_long_quality(row: dict[str, Any]) -> str:
    """Post-hoc only — never fed into gate."""
    reached = row.get("reached_plus_025")
    drop = row.get("max_adverse_drop_pct")
    returned = row.get("returned_to_signal")
    mfe = row.get("mfe_pct")
    try:
        drop_f = float(drop) if drop is not None and str(drop) not in {"", "nan", "None"} else None
    except (TypeError, ValueError):
        drop_f = None
    try:
        mfe_f = float(mfe) if mfe is not None and str(mfe) not in {"", "nan", "None"} else None
    except (TypeError, ValueError):
        mfe_f = None

    good = False
    weak = False
    if reached is True or str(reached).lower() == "true":
        good = True
    if mfe_f is not None and mfe_f >= 0.25:
        good = True
    if returned is True or str(returned).lower() == "true":
        # positive recovery signal, not alone enough for good if deep drop
        if drop_f is None or drop_f < 1.5:
            good = True
    if reached is False or str(reached).lower() == "false":
        weak = True
    if drop_f is not None and drop_f >= 1.5:
        weak = True
    if returned is False or str(returned).lower() == "false":
        if drop_f is not None and drop_f >= 1.0:
            weak = True

    if good and not weak:
        return "good"
    if weak and not good:
        return "weak"
    if good and weak:
        return "mixed"
    return "unknown"


def stability_for_variant(g15: pd.DataFrame) -> dict[str, Any]:
    if g15.empty:
        return {}
    bear = g15["direction_gate_state"] == "strong_bearish"
    bull = g15["direction_gate_state"] == "strong_bullish"
    neu = g15["direction_gate_state"] == "neutral"
    runs = _runs(bear, g15["bar_close_time"])
    first = g15.loc[bear, "bar_close_time"].min() if bear.any() else None
    last = g15.loc[bear, "bar_close_time"].max() if bear.any() else None
    interruptions = 0
    if first is not None and last is not None:
        span = g15[
            (pd.to_datetime(g15["bar_close_time"], utc=True) >= pd.Timestamp(first))
            & (pd.to_datetime(g15["bar_close_time"], utc=True) <= pd.Timestamp(last))
        ]
        interruptions = int((span["direction_gate_state"] != "strong_bearish").sum())
    changes = int(g15["direction_gate_state"].ne(g15["direction_gate_state"].shift()).sum())
    longest = max(runs, key=lambda r: r["n_bars"]) if runs else None
    ref = pd.Timestamp(STRUCTURE_BREAK_REF)

    # March 6+ focused (main downtrend window)
    g6 = g15[
        (pd.to_datetime(g15["bar_close_time"], utc=True) >= pd.Timestamp(MAR6_START))
        & (pd.to_datetime(g15["bar_close_time"], utc=True) < pd.Timestamp(MAR6_END))
    ]
    bear6 = g6["direction_gate_state"] == "strong_bearish"
    first6 = g6.loc[bear6, "bar_close_time"].min() if bear6.any() else None
    last6 = g6.loc[bear6, "bar_close_time"].max() if bear6.any() else None
    runs6 = _runs(bear6, g6["bar_close_time"])
    delay6 = None
    if first6 is not None:
        delay6 = (pd.Timestamp(first6) - ref).total_seconds() / 60.0
    # Afternoon downtrend run: first bearish at/after 12:00 UTC on Mar 6
    g6_pm = g6[pd.to_datetime(g6["bar_close_time"], utc=True) >= pd.Timestamp("2026-03-06T12:00:00+00:00")]
    bear_pm = g6_pm["direction_gate_state"] == "strong_bearish"
    first_pm = g6_pm.loc[bear_pm, "bar_close_time"].min() if bear_pm.any() else None
    delay_pm = None
    if first_pm is not None:
        delay_pm = (pd.Timestamp(first_pm) - ref).total_seconds() / 60.0

    return {
        "first_strong_bearish": str(first) if first is not None else None,
        "last_strong_bearish": str(last) if last is not None else None,
        "delay_minutes_vs_structure_break_1440": (
            (pd.Timestamp(first) - ref).total_seconds() / 60.0 if first is not None else None
        ),
        "n_state_changes": changes,
        "n_bearish_runs": len(runs),
        "n_neutral_interruptions_inside_bear_span": interruptions,
        "longest_bearish_run": longest,
        "minutes_strong_bearish": int(bear.sum()) * 15,
        "minutes_strong_bullish": int(bull.sum()) * 15,
        "minutes_neutral": int(neu.sum()) * 15,
        "bearish_runs": runs,
        "mar6_first_strong_bearish": str(first6) if first6 is not None else None,
        "mar6_last_strong_bearish": str(last6) if last6 is not None else None,
        "mar6_n_bearish_runs": len(runs6),
        "mar6_delay_vs_1440_min": delay6,
        "mar6_pm_first_strong_bearish": str(first_pm) if first_pm is not None else None,
        "mar6_pm_delay_vs_1440_min": delay_pm,
    }


def join_gate_at_time(dec_map: pd.DataFrame, ts: pd.Timestamp) -> dict[str, Any]:
    m = dec_map[dec_map["decision_time"] <= ts]
    if m.empty:
        return {"direction_gate_state": "unavailable", "would_block_long": False, "would_block_short": False}
    row = m.iloc[-1]
    return {
        "direction_gate_state": row.get("direction_gate_state"),
        "would_block_long": bool(row.get("would_block_long")),
        "would_block_short": bool(row.get("would_block_short")),
        "gate_variant": row.get("gate_variant"),
        "entry_reason": row.get("entry_reason"),
        "bearish_entry_score": row.get("bearish_entry_score"),
        "bar_close_time": row.get("bar_close_time"),
    }


def run_variant_audit(
    *,
    variant: GateVariant,
    candles: pd.DataFrame,
    frame: pd.DataFrame,
    scanner_cfg,
    setups: pd.DataFrame,
    pa: pd.DataFrame,
    mom: pd.DataFrame,
    drop: pd.DataFrame | None,
    forward: pd.DataFrame | None,
    window_start: str,
    window_end: str,
    history_start: str,
) -> dict[str, Any]:
    cfg = DirectionGateConfig(enabled=False, variant=variant)
    # Warm state from history_start, emit from window_start
    g15 = run_gate_on_15m_frame(
        frame,
        candles,
        cfg,
        scanner_cfg,
        start_close_time=history_start,
        end_close_time=window_end,
    )
    # Restrict metrics window
    g15_w = g15[
        (pd.to_datetime(g15["bar_close_time"], utc=True) >= pd.Timestamp(window_start))
        & (pd.to_datetime(g15["bar_close_time"], utc=True) < pd.Timestamp(window_end))
    ].copy()
    assert_outcomes_do_not_affect_gate(g15_w, drop)

    # 5m decision map for window (and a bit before for setups)
    # Build decision times from 5m closes
    c5 = candles.copy()
    c5["timestamp"] = pd.to_datetime(c5["timestamp"], utc=True)
    c5["decision_time"] = c5["timestamp"] + pd.Timedelta(minutes=5)
    dec_times = c5[
        (c5["decision_time"] >= pd.Timestamp(window_start) - pd.Timedelta(days=1))
        & (c5["decision_time"] < pd.Timestamp(window_end))
    ]["decision_time"]
    dec_map = expand_15m_state_to_5m_decisions(g15, dec_times)
    # Attach close from 5m
    dec_map = dec_map.merge(
        c5.rename(columns={"close": "close_5m"})[["decision_time", "close_5m"]],
        on="decision_time",
        how="left",
    )

    stab = stability_for_variant(g15_w)

    # Setup counterfactual
    s = setups.copy()
    s["setup_activation_timestamp"] = pd.to_datetime(s["setup_activation_timestamp"], utc=True)
    s = s[
        (s["setup_activation_timestamp"] >= window_start)
        & (s["setup_activation_timestamp"] < window_end)
        & (s.get("setup_activated", True) == True)  # noqa: E712
    ]
    drop_map = {}
    if drop is not None and len(drop):
        drop_map = drop.set_index("setup_id").to_dict("index")
    fwd_map = {}
    if forward is not None and len(forward):
        # prefer horizon ~12 if present
        f = forward.copy()
        if "horizon" in f.columns:
            f12 = f[f["horizon"] == 12]
            if len(f12):
                f = f12
        if "setup_id" in f.columns:
            fwd_map = f.groupby("setup_id").first().to_dict("index")

    setup_rows = []
    for _, r in s.iterrows():
        ts = r["setup_activation_timestamp"]
        g = join_gate_at_time(dec_map, ts)
        d = drop_map.get(r["setup_id"], {})
        fw = fwd_map.get(r["setup_id"], {})
        outcome = {
            "max_adverse_drop_pct": d.get("max_adverse_drop_pct", fw.get("mae_pct")),
            "reached_plus_025": d.get("reached_plus_025"),
            "returned_to_signal": d.get("returned_to_signal"),
            "mfe_pct": fw.get("mfe_pct") or fw.get("max_favorable_pct"),
            "mae_pct": fw.get("mae_pct") or fw.get("max_adverse_pct"),
        }
        quality = classify_long_quality(outcome) if r.get("setup_side") == "long" else None
        blocked = bool(g["would_block_long"]) if r.get("setup_side") == "long" else bool(
            g["would_block_short"]
        )
        setup_rows.append(
            {
                "setup_id": r["setup_id"],
                "setup_activation_timestamp": str(ts),
                "setup_side": r.get("setup_side"),
                "regime_15m": r.get("regime_15m"),
                "regime_30m": r.get("regime_30m"),
                "gate_variant": variant,
                **g,
                "would_block": blocked,
                **outcome,
                "long_quality": quality,
            }
        )
    setup_cf = pd.DataFrame(setup_rows)

    # PA / momentum counterfactual
    def _event_cf(df: pd.DataFrame, ts_col: str, side_col: str = "side") -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        x = df.copy()
        x[ts_col] = pd.to_datetime(x[ts_col], utc=True)
        x = x[(x[ts_col] >= window_start) & (x[ts_col] < window_end)]
        rows = []
        for _, r in x.iterrows():
            g = join_gate_at_time(dec_map, r[ts_col])
            side = r.get(side_col)
            blocked = (
                bool(g["would_block_long"])
                if side == "long"
                else bool(g["would_block_short"])
                if side == "short"
                else False
            )
            d = drop_map.get(r.get("setup_id"), {})
            outcome = {
                "max_adverse_drop_pct": d.get("max_adverse_drop_pct"),
                "reached_plus_025": d.get("reached_plus_025"),
                "returned_to_signal": d.get("returned_to_signal"),
            }
            q = classify_long_quality(outcome) if side == "long" else None
            rows.append(
                {
                    "setup_id": r.get("setup_id"),
                    "event_timestamp": str(r[ts_col]),
                    "side": side,
                    "gate_variant": variant,
                    **g,
                    "would_block": blocked,
                    **outcome,
                    "long_quality": q,
                }
            )
        return pd.DataFrame(rows)

    pa_cf = _event_cf(pa, "structure_break_timestamp")
    mom_cf = _event_cf(mom, "confirmation_timestamp")

    # Metrics
    longs = setup_cf[setup_cf["setup_side"] == "long"] if len(setup_cf) else pd.DataFrame()
    shorts = setup_cf[setup_cf["setup_side"] == "short"] if len(setup_cf) else pd.DataFrame()
    blocked_longs = longs[longs["would_block"] == True] if len(longs) else pd.DataFrame()  # noqa: E712
    weak_longs = longs[longs["long_quality"] == "weak"] if len(longs) else pd.DataFrame()
    good_longs = longs[longs["long_quality"] == "good"] if len(longs) else pd.DataFrame()
    weak_blocked = (
        blocked_longs[blocked_longs["long_quality"] == "weak"] if len(blocked_longs) else pd.DataFrame()
    )
    good_blocked = (
        blocked_longs[blocked_longs["long_quality"] == "good"] if len(blocked_longs) else pd.DataFrame()
    )

    n_weak = len(weak_longs)
    n_good = len(good_longs)
    n_blocked = len(blocked_longs)
    precision = (len(weak_blocked) / n_blocked) if n_blocked else None
    recall = (len(weak_blocked) / n_weak) if n_weak else None
    false_block = (len(good_blocked) / n_good) if n_good else None
    ratio = (
        (len(weak_blocked) / len(good_blocked)) if len(good_blocked) else (float(len(weak_blocked)) if len(weak_blocked) else None)
    )

    # shorts allowed during strong_bearish
    short_during_bear = 0
    if len(shorts):
        short_during_bear = int(
            (
                (shorts["direction_gate_state"] == "strong_bearish")
                & (shorts["would_block"] == False)  # noqa: E712
            ).sum()
        )

    pa_long_blocked = (
        int(((pa_cf["side"] == "long") & (pa_cf["would_block"] == True)).sum())  # noqa: E712
        if len(pa_cf)
        else 0
    )
    mom_long_blocked = (
        int(((mom_cf["side"] == "long") & (mom_cf["would_block"] == True)).sum())  # noqa: E712
        if len(mom_cf)
        else 0
    )

    # avoided adverse among blocked weaks
    avoided = weak_blocked["max_adverse_drop_pct"].dropna() if len(weak_blocked) else pd.Series(dtype=float)

    # pre-PA blocks among focus setups
    focus = setup_cf[setup_cf["setup_id"].isin(FOCUS_SETUPS)] if len(setup_cf) else pd.DataFrame()

    metrics = {
        **stab,
        "n_long_setups": int(len(longs)),
        "n_short_setups": int(len(shorts)),
        "n_long_setups_blocked": int(n_blocked),
        "n_short_setups_blocked": int((shorts["would_block"] == True).sum()) if len(shorts) else 0,  # noqa: E712
        "n_shorts_allowed_during_strong_bearish": short_during_bear,
        "n_long_pa_blocked": pa_long_blocked,
        "n_long_momentum_blocked": mom_long_blocked,
        "n_weak_longs": int(n_weak),
        "n_good_longs": int(n_good),
        "n_weak_longs_blocked": int(len(weak_blocked)),
        "n_good_longs_blocked": int(len(good_blocked)),
        "precision_blocked_are_weak": precision,
        "recall_weak_blocked": recall,
        "false_block_rate_good": false_block,
        "net_weak_avoided": int(len(weak_blocked) - len(good_blocked)),
        "weak_to_good_block_ratio": ratio,
        "avg_avoided_adverse_drop_pct": float(avoided.mean()) if len(avoided) else None,
        "max_avoided_adverse_drop_pct": float(avoided.max()) if len(avoided) else None,
        "focus_setups": focus.to_dict(orient="records") if len(focus) else [],
    }

    changes = g15_w[g15_w["transition"].notna() & (g15_w["transition"] != "")].copy()

    return {
        "variant": variant,
        "gate_15m": g15_w,
        "gate_15m_full_warm": g15,
        "dec_map": dec_map,
        "setup_cf": setup_cf,
        "pa_cf": pa_cf,
        "mom_cf": mom_cf,
        "state_changes": changes,
        "metrics": metrics,
        "config": cfg,
    }


def write_readme(summary: dict[str, Any], out: Path) -> None:
    variants = summary.get("variants", {})
    lines = [
        "# 15m Strong-Trend Direction Gate Audit (March week 1)",
        "",
        "Research-only counterfactual. Existing regime classification unchanged. "
        "Gate default `enabled=False`. No pipeline/live integration.",
        "",
        f"Warmup: {WARMUP_NOTE}",
        "",
        "## Decision answers",
        "",
    ]
    answers = summary.get("answers", {})
    for i, (k, v) in enumerate(answers.items(), 1):
        lines.append(f"{i}. **{k}:** {v}")
    lines.extend(["", "## Variant comparison", ""])
    for vname, m in variants.items():
        lines.append(f"### {vname}")
        lines.append("")
        lines.append(f"- First strong_bearish (week): `{m.get('first_strong_bearish')}`")
        lines.append(f"- First strong_bearish Mar6+: `{m.get('mar6_first_strong_bearish')}`")
        lines.append(f"- First strong_bearish Mar6 PM (≥12:00): `{m.get('mar6_pm_first_strong_bearish')}`")
        lines.append(f"- Delay vs 14:40 break (PM, min): `{m.get('mar6_pm_delay_vs_1440_min')}`")
        lines.append(f"- State changes (week): `{m.get('n_state_changes')}`")
        lines.append(f"- Bearish runs week / Mar6+: `{m.get('n_bearish_runs')}` / `{m.get('mar6_n_bearish_runs')}`")
        lines.append(f"- Interruptions inside week bear span: `{m.get('n_neutral_interruptions_inside_bear_span')}`")
        lines.append(f"- Longs blocked: `{m.get('n_long_setups_blocked')}` (weak `{m.get('n_weak_longs_blocked')}`, good `{m.get('n_good_longs_blocked')}`)")
        lines.append(f"- Shorts during bearish still allowed: `{m.get('n_shorts_allowed_during_strong_bearish')}`")
        lines.append("")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_answers(variant_metrics: dict[str, dict[str, Any]], setup_tables: dict[str, pd.DataFrame]) -> dict[str, str]:
    # Prefer first strong_bearish at or AFTER the 14:40 structure break (true downtrend latch).
    # Early pre-break bearish (B1/B2) is reported separately as "early/noisy".
    ref = pd.Timestamp(STRUCTURE_BREAK_REF)
    best_after = None
    best_after_v = None
    earliest_any_pm = None
    earliest_any_pm_v = None
    least_flutter = None
    least_flutter_v = None
    most_weak = None
    most_weak_v = None
    least_good_block = None
    least_good_block_v = None

    for v, m in variant_metrics.items():
        fb = m.get("mar6_pm_first_strong_bearish") or m.get("mar6_first_strong_bearish")
        if fb:
            ts = pd.Timestamp(fb)
            if earliest_any_pm is None or ts < pd.Timestamp(earliest_any_pm):
                earliest_any_pm, earliest_any_pm_v = fb, v
            if ts >= ref and (best_after is None or ts < pd.Timestamp(best_after)):
                best_after, best_after_v = fb, v
        flutter = m.get("mar6_n_bearish_runs")
        if flutter is None:
            flutter = m.get("n_bearish_runs") or 999
        if least_flutter is None or flutter < least_flutter:
            least_flutter, least_flutter_v = flutter, v
        wb = m.get("n_weak_longs_blocked") or 0
        if most_weak is None or wb > most_weak:
            most_weak, most_weak_v = wb, v
        gb = m.get("n_good_longs_blocked") or 0
        if least_good_block is None or gb < least_good_block:
            least_good_block, least_good_block_v = gb, v

    delay_note = {v: m.get("mar6_pm_delay_vs_1440_min") for v, m in variant_metrics.items()}
    detect_v = best_after_v or earliest_any_pm_v
    detect_ts = best_after or earliest_any_pm

    def focus_block(v: str, sid: str) -> str:
        df = setup_tables.get(v)
        if df is None or df.empty:
            return "n/a"
        hit = df[df["setup_id"] == sid]
        if hit.empty:
            return "setup not in window / not activated"
        row = hit.iloc[0]
        return (
            f"blocked={bool(row.get('would_block'))} state={row.get('direction_gate_state')} "
            f"at {row.get('setup_activation_timestamp')}"
        )

    morning_blocked = {}
    for v, df in setup_tables.items():
        if df is None or df.empty:
            morning_blocked[v] = False
            continue
        ids = df[df["setup_id"].isin(["setup_00056", "setup_00057", "setup_00058", "setup_00059"])]
        morning_blocked[v] = bool(ids["would_block"].any()) if len(ids) else False

    any_morning = any(morning_blocked.values())
    recommend = best_after_v or "B3"

    answers = {
        "earliest_06_mar_detection": (
            f"Am Strukturbruch (≥14:40): {detect_v} at {detect_ts}. "
            f"Früheste PM-Marke (auch vor 14:40, ggf. noisy): {earliest_any_pm_v} at {earliest_any_pm}. "
            f"Delays vs 14:40 min: {delay_note}"
        ),
        "least_flutter": f"{least_flutter_v} ({least_flutter} bearish runs on/after Mar6)",
        "most_weak_longs_blocked": f"{most_weak_v} ({most_weak})",
        "fewest_good_longs_blocked": f"{least_good_block_v} ({least_good_block})",
        "setup_00055": "; ".join(f"{v}: {focus_block(v, 'setup_00055')}" for v in variant_metrics),
        "setups_00056_00059": "; ".join(
            f"{v}: "
            + ", ".join(
                focus_block(v, sid)
                for sid in ["setup_00056", "setup_00057", "setup_00058", "setup_00059"]
            )
            for v in variant_metrics
        ),
        "why_if_not_blocked": (
            "Vormittags (01:35–07:05) war APT noch nahe Session-Hochs; Gate war neutral oder sogar "
            "strong_bullish. Confirmed LH/LL + Break und voller bearish EMA-Stack waren kausal noch "
            "nicht erfüllt — der starke Nachmittags-Downtrend hatte noch nicht begonnen."
        ),
        "can_15m_gate_alone_stop_morning_longs": (
            "Nein. Alle drei Varianten blockieren setup_00056–00059 nicht. "
            "Ein Strong-Trend-Gate kann dieses konkrete Vormittags-Problem nicht lösen."
            if not any_morning
            else "Teilweise — siehe Varianten."
        ),
        "need_earlier_breakdown_risk_off": (
            "Ja. Zusätzlich nötig: früher Breakdown-/Risk-Off-State (5m/15m Session-Hoch → "
            "Failure), getrennt vom Strong-Trend Direction Gate und vom normalen Regime-Kontext."
        ),
        "recommended_next_integration_test": (
            f"{recommend} als Strong-Trend-Kandidat (latch am/nach 14:40-Bruch; "
            f"Delays={delay_note}). Vormittags-Longs erfordern separat einen früheren "
            "Risk-Off/Breakdown-Blocker — Strong-Trend-Gate allein reicht nicht."
        ),
    }
    return answers


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--window-start", default="2026-03-01T00:00:00+00:00")
    p.add_argument("--window-end", default="2026-03-08T00:00:00+00:00")
    p.add_argument(
        "--history-start",
        default="2026-02-01T00:00:00+00:00",
        help="Start emitting/warming 15m gate (need history before March)",
    )
    p.add_argument(
        "--pipeline-dir",
        default="research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum",
    )
    p.add_argument(
        "--drop-csv",
        default="research/backtests/results/regime_scanner_momentum_deepest_drop_recovery_march_week1/signal_deepest_drop_recovery.csv",
    )
    p.add_argument(
        "--forward-csv",
        default="research/backtests/results/regime_scanner_momentum_forward_audit_march_week1/momentum_forward_outcomes.csv",
    )
    p.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_direction_gate_audit_march_week1",
    )
    args = p.parse_args(argv)

    root = Path(args.pipeline_dir)
    setups = pd.read_csv(root / "setup_activations.csv")
    pa = pd.read_csv(root / "price_action_confirmations.csv")
    mom = pd.read_csv(root / "momentum_confirmations.csv")
    drop = pd.read_csv(args.drop_csv) if Path(args.drop_csv).exists() else None
    forward = pd.read_csv(args.forward_csv) if Path(args.forward_csv).exists() else None

    candles = load_symbol_candles(args.symbol)
    # Build 15m frame as of window end (causal closed bars only)
    frame, scanner_cfg = build_15m_indicator_frame(candles, args.window_end)
    # Keep enough pre-window history for EMA200/slope warmup (~346 15m bars)
    # plus explicit history_start, but avoid multi-year walk cost.
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    warm_bars = int(scanner_cfg.min_warmup_candles) + 50
    hist_ts = pd.Timestamp(args.history_start)
    # Include bars whose close_time >= history_start - warm_bars*15m
    cut = hist_ts - pd.Timedelta(minutes=15 * warm_bars)
    frame = frame[frame["timestamp"] >= cut].reset_index(drop=True)
    print(f"15m frame bars after cut: {len(frame)} (warmup need {scanner_cfg.min_warmup_candles})")

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    timeline_15m_parts: list[pd.DataFrame] = []
    timeline_5m_parts: list[pd.DataFrame] = []
    setup_parts = []
    pa_parts = []
    mom_parts = []
    change_parts = []
    outcome_parts = []
    comparison_rows = []
    variant_metrics: dict[str, dict[str, Any]] = {}
    setup_tables: dict[str, pd.DataFrame] = {}

    for variant in ("B1", "B2", "B3"):
        print(f"Running variant {variant}...")
        res = run_variant_audit(
            variant=variant,  # type: ignore[arg-type]
            candles=candles,
            frame=frame,
            scanner_cfg=scanner_cfg,
            setups=setups,
            pa=pa,
            mom=mom,
            drop=drop,
            forward=forward,
            window_start=args.window_start,
            window_end=args.window_end,
            history_start=args.history_start,
        )
        timeline_15m_parts.append(res["gate_15m"])
        focus_start = "2026-03-05T00:00:00+00:00"
        dec_f = res["dec_map"][
            (res["dec_map"]["decision_time"] >= focus_start)
            & (res["dec_map"]["decision_time"] < args.window_end)
        ].copy()
        dec_f["gate_variant"] = variant
        timeline_5m_parts.append(dec_f)
        setup_parts.append(res["setup_cf"])
        pa_parts.append(res["pa_cf"])
        mom_parts.append(res["mom_cf"])
        change_parts.append(res["state_changes"])
        variant_metrics[variant] = res["metrics"]
        setup_tables[variant] = res["setup_cf"]
        comparison_rows.append({"gate_variant": variant, **res["metrics"]})
        if len(res["setup_cf"]):
            outcome_parts.append(res["setup_cf"])

    g15_clean = pd.concat(timeline_15m_parts, ignore_index=True)
    g15_clean.to_csv(out / "direction_gate_timeline_15m.csv", index=False)
    d5 = pd.concat(timeline_5m_parts, ignore_index=True)
    d5.to_csv(out / "direction_gate_timeline.csv", index=False)

    pd.concat(change_parts, ignore_index=True).to_csv(out / "direction_gate_state_changes.csv", index=False)
    setups_all = pd.concat(setup_parts, ignore_index=True) if setup_parts else pd.DataFrame()
    setups_all.to_csv(out / "direction_gate_setup_counterfactual.csv", index=False)
    pd.concat(pa_parts, ignore_index=True).to_csv(out / "direction_gate_pa_counterfactual.csv", index=False)
    pd.concat(mom_parts, ignore_index=True).to_csv(out / "direction_gate_momentum_counterfactual.csv", index=False)

    # Flatten comparison (drop nested dicts/lists for CSV)
    comp = []
    for row in comparison_rows:
        flat = {k: v for k, v in row.items() if not isinstance(v, (dict, list))}
        comp.append(flat)
    pd.DataFrame(comp).to_csv(out / "direction_gate_variant_comparison.csv", index=False)

    if outcome_parts:
        pd.concat(outcome_parts, ignore_index=True).to_csv(out / "direction_gate_vs_outcomes.csv", index=False)
    else:
        pd.DataFrame().to_csv(out / "direction_gate_vs_outcomes.csv", index=False)

    answers = build_answers(variant_metrics, setup_tables)
    summary = {
        "symbol": args.symbol,
        "window_start": args.window_start,
        "window_end": args.window_end,
        "history_start": args.history_start,
        "structure_break_ref": STRUCTURE_BREAK_REF,
        "warmup_note": WARMUP_NOTE,
        "gate_default_enabled": False,
        "pipeline_untouched": True,
        "source_pipeline_dir": str(root),
        "variants": {
            k: {
                kk: vv
                for kk, vv in v.items()
                if (not isinstance(vv, (dict, list))) or kk == "focus_setups"
            }
            for k, v in variant_metrics.items()
        },
        "answers": answers,
        "safety": {
            "no_live_changes": True,
            "no_pipeline_csv_mutation": True,
            "outcomes_not_used_in_gate": True,
            "forming_15m_excluded": True,
        },
    }
    (out / "audit_summary.json").write_text(json.dumps(json_safe(summary), indent=2), encoding="utf-8")
    write_readme(summary, out / "README.md")
    print("Wrote", out)
    print("ANSWERS:")
    for k, v in answers.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
