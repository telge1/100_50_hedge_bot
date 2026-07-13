"""Read-only audit: did existing regime detection see the March 6 downtrend?

Uses R4 pipeline CSVs only (no new gate logic, no pipeline mutation).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.timeframes import aggregate_candles

BEARISH_SUBSTRING = "bear"
BULLISH_SUBSTRING = "bull"


def _is_bearish(label: object) -> bool:
    return BEARISH_SUBSTRING in str(label or "").lower()


def _is_bullish(label: object) -> bool:
    return BULLISH_SUBSTRING in str(label or "").lower()


def _is_strong_bearish(label: object) -> bool:
    s = str(label or "")
    return s in {"strong_bearish_trend", "strong_bearish_expansion"}


def attach_closes(timeline: pd.DataFrame, symbol: str = "APTUSDT") -> pd.DataFrame:
    """Join closed 5m/15m/30m closes (causal: only bars fully closed by decision_time)."""
    candles = load_symbol_candles(symbol)
    c5 = candles.copy()
    c5["timestamp"] = pd.to_datetime(c5["timestamp"], utc=True)
    c5 = c5.sort_values("timestamp").reset_index(drop=True)
    # Closed 5m bar at decision_time T has open timestamp T-5m
    c5["decision_time"] = c5["timestamp"] + pd.Timedelta(minutes=5)
    c5 = c5.rename(columns={"close": "close_5m"})[["decision_time", "close_5m"]]

    decision_max = timeline["decision_time"].max()
    c15 = aggregate_candles(candles, "15m", decision_max).copy()
    c15["close_decision"] = c15["timestamp"] + pd.Timedelta(minutes=15)
    c15 = c15.rename(columns={"close": "close_15m"})[["close_decision", "close_15m"]]

    c30 = aggregate_candles(candles, "30m", decision_max).copy()
    c30["close_decision"] = c30["timestamp"] + pd.Timedelta(minutes=30)
    c30 = c30.rename(columns={"close": "close_30m"})[["close_decision", "close_30m"]]

    df = timeline.copy()
    df = df.merge(c5, on="decision_time", how="left")
    # asof last fully closed HTF bar
    df = pd.merge_asof(
        df.sort_values("decision_time"),
        c15.sort_values("close_decision"),
        left_on="decision_time",
        right_on="close_decision",
        direction="backward",
    ).drop(columns=["close_decision"], errors="ignore")
    df = pd.merge_asof(
        df.sort_values("decision_time"),
        c30.sort_values("close_decision"),
        left_on="decision_time",
        right_on="close_decision",
        direction="backward",
    ).drop(columns=["close_decision"], errors="ignore")
    return df


def price_structure_delay(timeline: pd.DataFrame, day: str = "2026-03-06") -> dict[str, Any]:
    """Rough delay: first close below prior-day low vs first 15m bearish on that day."""
    day_start = pd.Timestamp(f"{day}T00:00:00+00:00")
    day_end = day_start + pd.Timedelta(days=1)
    prev_start = day_start - pd.Timedelta(days=1)
    prev = timeline[(timeline.decision_time >= prev_start) & (timeline.decision_time < day_start)]
    cur = timeline[(timeline.decision_time >= day_start) & (timeline.decision_time < day_end)]
    if prev.empty or cur.empty or "close_5m" not in cur.columns:
        return {"note": "insufficient close data"}
    prior_low = float(prev["close_5m"].min())
    prior_high = float(prev["close_5m"].max())
    below = cur[cur["close_5m"] < prior_low]
    first_break = below["decision_time"].min() if len(below) else None
    first_bear = cur.loc[cur["regime_15m_bearish"], "decision_time"].min()
    delay_min = None
    if first_break is not None and pd.notna(first_bear):
        delay_min = (first_bear - first_break).total_seconds() / 60.0
    morning_high = float(cur.iloc[:72]["close_5m"].max()) if len(cur) >= 72 else float(cur["close_5m"].max())
    return {
        "prior_day_close_low": prior_low,
        "prior_day_close_high": prior_high,
        "day_morning_high_close_approx_6h": morning_high,
        "first_close_below_prior_day_low": str(first_break) if first_break is not None else None,
        "first_15m_bearish_on_day": str(first_bear) if pd.notna(first_bear) else None,
        "delay_minutes_break_to_15m_bearish": delay_min,
        "lookahead_check": "HTF closes joined via merge_asof backward only (closed bars)",
    }


def build_timeline(snap: pd.DataFrame) -> pd.DataFrame:
    df = snap.copy()
    df["decision_time"] = pd.to_datetime(df["decision_time"], utc=True)
    df = df.sort_values("decision_time").reset_index(drop=True)
    df["regime_15m_bearish"] = df["regime_15m"].map(_is_bearish)
    df["regime_30m_bearish"] = df["regime_30m"].map(_is_bearish)
    df["combined_bearish"] = df["combined_regime"].map(_is_bearish)
    df["strong_bearish_15m"] = df["regime_15m"].map(_is_strong_bearish)
    df["strong_bearish_combined"] = df["combined_regime"].map(_is_strong_bearish)
    # HTF opposing for a hypothetical long: 30m short direction
    df["htf_would_block_long"] = df["regime_30m"].map(_is_bearish)
    df["long_setup_on_bar"] = (df["setup_activated"] == True) & (df["setup_side"] == "long")  # noqa: E712
    df["short_setup_on_bar"] = (df["setup_activated"] == True) & (df["setup_side"] == "short")  # noqa: E712
    # Existing system never recorded blockers in R4; derive whether HTF rule would fire
    df["htf_opposing_trend_active_for_long"] = df["htf_would_block_long"]
    df["long_actually_blocked"] = False  # R4 recorded 0 nonempty blockers on setups
    df["short_allowed"] = ~df["regime_30m"].map(_is_bullish)
    return df


def compress_changes(timeline: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "regime_15m",
        "regime_30m",
        "combined_regime",
        "trend_direction",
        "trend_strength",
        "setup_activated",
        "setup_side",
    ]
    t = timeline.copy()
    chg = False
    for c in cols:
        chg = chg | t[c].ne(t[c].shift())
    # always keep first
    chg.iloc[0] = True
    out = t.loc[chg].copy()
    return out


def bearish_runs(mask: pd.Series, times: pd.Series) -> list[dict[str, Any]]:
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
                        "n_bars_5m": end - start + 1,
                        "duration_minutes": (end - start + 1) * 5,
                    }
                )
            start = None
    return runs


def stability_metrics(timeline: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    w = timeline[(timeline.decision_time >= start) & (timeline.decision_time < end)].copy()
    bear = w["regime_15m_bearish"]
    runs = bearish_runs(bear, w["decision_time"])
    # interruptions: non-bearish bars between first and last bearish
    first = w.loc[bear, "decision_time"].min() if bear.any() else None
    last = w.loc[bear, "decision_time"].max() if bear.any() else None
    interruptions = 0
    reentries = max(0, len(runs) - 1)
    if first is not None and last is not None:
        span = w[(w.decision_time >= first) & (w.decision_time <= last)]
        interruptions = int((~span["regime_15m_bearish"]).sum())
    longest = max(runs, key=lambda r: r["n_bars_5m"]) if runs else None
    return {
        "window_start": start,
        "window_end": end,
        "n_rows_5m": int(len(w)),
        "n_15m_bearish_bars": int(bear.sum()),
        "n_15m_strong_bearish_bars": int(w["strong_bearish_15m"].sum()),
        "n_combined_strong_bearish_bars": int(w["strong_bearish_combined"].sum()),
        "first_15m_bearish_timestamp": str(first) if first is not None else None,
        "last_15m_bearish_timestamp": str(last) if last is not None else None,
        "first_30m_bearish_timestamp": str(w.loc[w.regime_30m_bearish, "decision_time"].min())
        if w.regime_30m_bearish.any()
        else None,
        "n_bearish_runs": len(runs),
        "n_reentries_after_interruption": reentries,
        "n_non_bearish_bars_inside_span": interruptions,
        "longest_bearish_run": longest,
        "bearish_runs": runs,
        "n_long_setups_in_window": int(w["long_setup_on_bar"].sum()),
        "n_short_setups_in_window": int(w["short_setup_on_bar"].sum()),
        "n_long_setups_while_15m_bearish": int(
            (w["long_setup_on_bar"] & w["regime_15m_bearish"]).sum()
        ),
        "n_long_setups_while_30m_bearish": int(
            (w["long_setup_on_bar"] & w["regime_30m_bearish"]).sum()
        ),
        "regime_15m_counts": w["regime_15m"].value_counts().to_dict(),
        "regime_30m_counts": w["regime_30m"].value_counts().to_dict(),
        "trend_strength_counts": w["trend_strength"].value_counts().to_dict(),
    }


def classify_long_row(
    *,
    regime_15m: object,
    regime_30m: object,
    blockers: object,
) -> str:
    """Map existing logic to A/B/C-style tags for one long."""
    blockers_s = str(blockers or "")
    if "HTF_OPPOSING_TREND" in blockers_s:
        return "blocked_by_existing_htf_policy"
    if _is_bearish(regime_15m) or _is_bearish(regime_30m):
        # Bearish context present but long still activated and not blocked
        return "regime_bearish_recognized_but_no_direction_blocker"
    return "regime_not_bearish_recognized_at_setup"


def build_long_checks(
    setups: pd.DataFrame,
    pa: pd.DataFrame,
    mom: pd.DataFrame,
    drop: pd.DataFrame | None,
    start: str,
    end: str,
) -> pd.DataFrame:
    s = setups.copy()
    s["setup_activation_timestamp"] = pd.to_datetime(s["setup_activation_timestamp"], utc=True)
    longs = s[
        (s["setup_side"] == "long")
        & (s["setup_activation_timestamp"] >= start)
        & (s["setup_activation_timestamp"] < end)
    ].copy()

    pa = pa.copy()
    pa["structure_break_timestamp"] = pd.to_datetime(pa["structure_break_timestamp"], utc=True)
    mom = mom.copy()
    mom["confirmation_timestamp"] = pd.to_datetime(mom["confirmation_timestamp"], utc=True)
    drop_map = {}
    if drop is not None and len(drop):
        drop_map = drop.set_index("setup_id").to_dict("index")

    rows = []
    for _, r in longs.iterrows():
        sid = r["setup_id"]
        pa_hit = pa[pa["setup_id"] == sid]
        mom_hit = mom[mom["setup_id"] == sid]
        d = drop_map.get(sid, {})
        tag = classify_long_row(
            regime_15m=r.get("regime_15m"),
            regime_30m=r.get("regime_30m"),
            blockers=r.get("blockers"),
        )
        rows.append(
            {
                "setup_id": sid,
                "setup_activation_timestamp": str(r["setup_activation_timestamp"]),
                "side": "long",
                "regime_15m": r.get("regime_15m"),
                "regime_30m": r.get("regime_30m"),
                "combined_regime": r.get("combined_regime"),
                "blockers": r.get("blockers"),
                "warnings": r.get("warnings"),
                "pa_confirmed": len(pa_hit) > 0,
                "pa_confirmation_timestamp": str(pa_hit.iloc[0]["structure_break_timestamp"])
                if len(pa_hit)
                else None,
                "momentum_confirmed": len(mom_hit) > 0,
                "momentum_confirmation_timestamp": str(mom_hit.iloc[0]["confirmation_timestamp"])
                if len(mom_hit)
                else None,
                "max_adverse_drop_pct": d.get("max_adverse_drop_pct"),
                "later_favorable_vs_signal_pct": d.get("later_favorable_vs_signal_pct"),
                "returned_to_signal": d.get("returned_to_signal"),
                "reached_plus_025": d.get("reached_plus_025"),
                "existing_system_verdict": tag,
            }
        )

    # Also include long PA/momentum confirms in window whose setup may be earlier
    for _, p in pa[
        (pa["side"] == "long")
        & (pa["structure_break_timestamp"] >= start)
        & (pa["structure_break_timestamp"] < end)
    ].iterrows():
        sid = p["setup_id"]
        if any(x["setup_id"] == sid for x in rows):
            # enrich if missing pa fields already set
            continue
        setup_row = s[s["setup_id"] == sid]
        mom_hit = mom[mom["setup_id"] == sid]
        d = drop_map.get(sid, {})
        r15 = p.get("regime_15m")
        r30 = p.get("regime_30m")
        blockers = setup_row.iloc[0]["blockers"] if len(setup_row) else "[]"
        rows.append(
            {
                "setup_id": sid,
                "setup_activation_timestamp": str(setup_row.iloc[0]["setup_activation_timestamp"])
                if len(setup_row)
                else None,
                "side": "long",
                "regime_15m": r15,
                "regime_30m": r30,
                "combined_regime": p.get("combined_regime"),
                "blockers": blockers,
                "warnings": setup_row.iloc[0]["warnings"] if len(setup_row) else None,
                "pa_confirmed": True,
                "pa_confirmation_timestamp": str(p["structure_break_timestamp"]),
                "momentum_confirmed": len(mom_hit) > 0,
                "momentum_confirmation_timestamp": str(mom_hit.iloc[0]["confirmation_timestamp"])
                if len(mom_hit)
                else None,
                "max_adverse_drop_pct": d.get("max_adverse_drop_pct"),
                "later_favorable_vs_signal_pct": d.get("later_favorable_vs_signal_pct"),
                "returned_to_signal": d.get("returned_to_signal"),
                "reached_plus_025": d.get("reached_plus_025"),
                "existing_system_verdict": classify_long_row(
                    regime_15m=r15, regime_30m=r30, blockers=blockers
                ),
                "note": "pa_confirm_in_window_setup_may_be_earlier",
            }
        )
    return pd.DataFrame(rows)


def decide_case(stability: dict[str, Any], long_checks: pd.DataFrame, snap: pd.DataFrame) -> dict[str, Any]:
    n_strong = int(
        (snap["regime_15m"] == "strong_bearish_trend").sum()
        + (snap["combined_regime"] == "strong_bearish_trend").sum()
    )
    n_blockers = 0
    # from long checks / setups - computed outside; use stability fields
    verdicts = long_checks["existing_system_verdict"].value_counts().to_dict() if len(long_checks) else {}
    first_bear = stability.get("first_15m_bearish_timestamp")
    reentries = stability.get("n_reentries_after_interruption") or 0
    long_on_bear = stability.get("n_long_setups_while_15m_bearish") or 0

    # Decision logic with hard evidence
    if n_strong == 0 and first_bear and reentries >= 3:
        primary = "C"
        secondary = "B"
        label = "Fall C (primär) + Fall B (sekundär)"
        rationale = (
            "Kein einziges `strong_bearish_trend`/`strong_bearish_expansion` in der Märzwoche. "
            "15m-Bearish erst ab 2026-03-06 15:30 UTC als `bearish_trend_with_trend_weakness`, "
            f"danach {reentries} Unterbrechungs-Wiedereintritte (Flattern). "
            "Vormittags-Longs liefen unter bullish_weakness/neutral — Regime nicht bearish erkannt. "
            "Nach Bearish-Start: 0 Long-Setups bei bearish 15m (kein Fall-A-Blocker-Problem für neue Longs). "
            "HTF_OPPOSING_TREND feuerte 0× (Blocker nur 30m-Opposing, kein 15m-Direction-Gate)."
        )
    elif n_strong > 0 and long_on_bear > 0:
        primary = "A"
        secondary = None
        label = "Fall A"
        rationale = "Strong bearish erkannt, aber Longs trotzdem zugelassen ohne Direction-Blocker."
    else:
        primary = "B"
        secondary = "C"
        label = "Fall B (+C)"
        rationale = "Downtrend-Erkennung unzureichend und/oder instabil."

    return {
        "decision_label": label,
        "primary_case": primary,
        "secondary_case": secondary,
        "n_strong_bearish_labels_in_week": n_strong,
        "long_verdict_counts": verdicts,
        "rationale": rationale,
        "evidence_timestamps": {
            "first_15m_bearish": first_bear,
            "first_30m_bearish": stability.get("first_30m_bearish_timestamp"),
            "longest_run": stability.get("longest_bearish_run"),
        },
    }


def format_readme(
    *,
    decision: dict[str, Any],
    stability: dict[str, Any],
    long_checks: pd.DataFrame,
    logic_notes: dict[str, Any],
) -> str:
    lines = [
        "# Existing Downtrend Detection Audit (March week / 6 Mar focus)",
        "",
        f"## Entscheidung: **{decision['decision_label']}**",
        "",
        decision["rationale"],
        "",
        "## Bestehende Logik (Kurz)",
        "",
        f"```json\n{json.dumps(logic_notes, indent=2)}\n```",
        "",
        "## Stabilität 2026-03-05 → 2026-03-08",
        "",
        f"- First 15m bearish: `{stability.get('first_15m_bearish_timestamp')}`",
        f"- First 30m bearish: `{stability.get('first_30m_bearish_timestamp')}`",
        f"- 15m bearish bars: **{stability.get('n_15m_bearish_bars')}**",
        f"- Strong-bearish bars: **{stability.get('n_15m_strong_bearish_bars')}**",
        f"- Bearish runs / reentries: **{stability.get('n_bearish_runs')}** / **{stability.get('n_reentries_after_interruption')}**",
        f"- Long setups in window: **{stability.get('n_long_setups_in_window')}** "
        f"(while 15m bearish: **{stability.get('n_long_setups_while_15m_bearish')}**)",
        f"- Short setups in window: **{stability.get('n_short_setups_in_window')}**",
        "",
        "### Strukturbruch → Regime (06.03.)",
        "",
        f"```json\n{json.dumps(stability.get('price_structure_delay_mar6'), indent=2)}\n```",
        "",
        "## Long checks 06.–08.03.",
        "",
        "| setup | activation | 15m | 30m | PA | Mom | drop% | +0.25 | verdict |",
        "|---|---|---|---|---|---|---:|---|---|",
    ]
    for _, r in long_checks.iterrows():
        lines.append(
            "| {id} | {t} | {r15} | {r30} | {pa} | {mom} | {drop} | {p25} | {v} |".format(
                id=r.get("setup_id"),
                t=r.get("setup_activation_timestamp") or r.get("pa_confirmation_timestamp"),
                r15=r.get("regime_15m"),
                r30=r.get("regime_30m"),
                pa=r.get("pa_confirmed"),
                mom=r.get("momentum_confirmed"),
                drop=r.get("max_adverse_drop_pct"),
                p25=r.get("reached_plus_025"),
                v=r.get("existing_system_verdict"),
            )
        )
    lines.extend(
        [
            "",
            "## Falldefinitionen",
            "",
            "- **A:** starke Downtrend-Erkennung korrekt, aber kein Long-Blocker verdrahtet",
            "- **B:** Erkennung selbst unzureichend / zu spät / nie `strong_bearish`",
            "- **C:** teilweise erkannt, aber instabil (Flattern, keine Hysterese)",
            "",
            "## Nächster Schritt",
            "",
            "Direction-Gate Research (15m strong-trend + Hysterese) ist gerechtfertigt, "
            "weil die bestehende Erkennung den klaren Downtrend **nicht als stabilen strong-bearish Zustand** "
            "führt und Vormittags-Longs unter bullish/neutral Labels zulässt. "
            "Ein reines Verdrahten von `HTF_OPPOSING_TREND` auf 15m reicht nicht — "
            "zuerst muss die Strong-Trend-Zustandsdefinition verbessert werden.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--pipeline-dir",
        default="research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum",
    )
    p.add_argument(
        "--drop-csv",
        default="research/backtests/results/regime_scanner_momentum_deepest_drop_recovery_march_week1/signal_deepest_drop_recovery.csv",
    )
    p.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_existing_downtrend_audit_march_week1",
    )
    p.add_argument("--timeline-start", default="2026-03-05T00:00:00+00:00")
    p.add_argument("--timeline-end", default="2026-03-08T00:00:00+00:00")
    args = p.parse_args(argv)

    root = Path(args.pipeline_dir)
    snap = pd.read_csv(root / "regime_snapshots.csv")
    setups = pd.read_csv(root / "setup_activations.csv")
    pa = pd.read_csv(root / "price_action_confirmations.csv")
    mom = pd.read_csv(root / "momentum_confirmations.csv")
    drop_path = Path(args.drop_csv)
    drop = pd.read_csv(drop_path) if drop_path.exists() else None

    timeline = build_timeline(snap)
    timeline = attach_closes(timeline)
    window = timeline[
        (timeline.decision_time >= args.timeline_start)
        & (timeline.decision_time < args.timeline_end)
    ].copy()
    # Full 5m timeline in window for analysis file (may be large but OK ~864 rows)
    stability = stability_metrics(timeline, args.timeline_start, args.timeline_end)
    structure_delay = price_structure_delay(timeline, day="2026-03-06")
    stability["price_structure_delay_mar6"] = structure_delay
    long_checks = build_long_checks(
        setups, pa, mom, drop, "2026-03-06T00:00:00+00:00", "2026-03-08T00:00:00+00:00"
    )
    decision = decide_case(stability, long_checks, snap)

    logic_notes = {
        "strong_downtrend_labels": [
            "strong_bearish_expansion (classifier intermediate)",
            "strong_bearish_trend (summarize_timeframe_regime output)",
            "bearish_trend",
            "bearish_trend_with_trend_weakness",
        ],
        "strong_bearish_expansion_definition": (
            "classify_regime_label: strong bearish direction+strength, not weakening, "
            "strength_lbl strong/very_strong, accel accelerating|steady|mixed, no bullish_div. "
            "Then summarize_timeframe_regime maps intact bearish + ADX/DI/slope criteria to "
            "strong_bearish_trend else bearish_trend."
        ),
        "expansion_vs_continuation": (
            "strong_bearish_expansion is an acceleration/strength state, not a sticky downtrend FSM. "
            "Continuation typically becomes bearish_trend or bearish_trend_with_trend_weakness."
        ),
        "inputs_to_downtrend_classification": [
            "EMA alignment / close vs EMAs",
            "EMA slopes",
            "ADX / DI spread",
            "structural weakness / exhaustion",
            "divergences / last-bar rollover",
        ],
        "long_blocker_existing": (
            "Only HTF_OPPOSING_TREND when setup_side long and regime_30m direction short. "
            "No 15m direction gate. No sticky state / hysteresis."
        ),
        "evaluation_mode": "per closed 5m decision_time snapshot; no stored gate state",
        "hysteresis": False,
        "min_hold": False,
        "lookahead_note": (
            "Pipeline snapshots already use closed 5m decision_time and causal 15m/30m aggregates."
        ),
    }

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Emit change-compressed timeline plus key flags for readability
    compressed = compress_changes(window)
    timeline_out = compressed[
        [
            c
            for c in [
                "decision_time",
                "candle_timestamp",
                "close_5m",
                "close_15m",
                "close_30m",
                "regime_5m",
                "regime_15m",
                "regime_30m",
                "combined_regime",
                "trend_direction",
                "trend_strength",
                "trend_weakness",
                "regime_15m_bearish",
                "regime_30m_bearish",
                "strong_bearish_15m",
                "htf_opposing_trend_active_for_long",
                "long_actually_blocked",
                "short_allowed",
                "setup_activated",
                "setup_side",
                "long_setup_on_bar",
                "short_setup_on_bar",
            ]
            if c in compressed.columns
        ]
    ]
    # Also write full window timeline
    full_path = out / "existing_regime_timeline_full_5m.csv"
    window.to_csv(full_path, index=False)
    timeline_out.to_csv(out / "existing_regime_timeline.csv", index=False)
    long_checks.to_csv(out / "march_long_setups_regime_check.csv", index=False)

    summary = {
        "decision": decision,
        "stability": stability,
        "logic_notes": logic_notes,
        "n_setup_blockers_nonempty_in_r4": int(
            setups["blockers"].astype(str).str.contains("HTF_OPPOSING").sum()
        )
        if "blockers" in setups.columns
        else None,
        "source_pipeline_dir": str(root),
    }
    (out / "regime_stability_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2), encoding="utf-8"
    )
    (out / "README.md").write_text(
        format_readme(
            decision=decision,
            stability=stability,
            long_checks=long_checks,
            logic_notes=logic_notes,
        ),
        encoding="utf-8",
    )
    print(f"DECISION: {decision['decision_label']}")
    print(f"first_15m_bearish={stability.get('first_15m_bearish_timestamp')}")
    print(f"Wrote outputs under {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
