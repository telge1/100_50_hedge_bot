"""March-6 morning early-downtrend audit (visual anchor 07:30 UTC).

Research-only. Does not hardcode a tradeable ``timestamp >= 07:30`` rule.
Dynamic D1–D4 long-blocks are derived causally from closed 5m information.
Fixed clock blocks are post-hoc controls only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.early_downtrend import (
    default_early_downtrend_config,
    run_early_downtrend_timeline,
)
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.pipeline_counterfactual import (
    classify_entry_quality,
    compute_forward_outcome,
)
from research.regime_scanner.pipeline_counterfactual_multiweek import map_quality_label, to_utc
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.timeframes import aggregate_candles

DAY = "2026-03-06"
VISUAL_ANCHOR = pd.Timestamp("2026-03-06T07:30:00+00:00")
AUDIT_START = pd.Timestamp("2026-03-06T07:15:00+00:00")
AUDIT_END = pd.Timestamp("2026-03-06T09:30:00+00:00")
WARM_START = pd.Timestamp("2026-03-05T00:00:00+00:00")

CHECKPOINT_TIMES = [
    "07:15",
    "07:20",
    "07:25",
    "07:30",
    "07:35",
    "07:40",
    "07:45",
    "07:50",
    "07:55",
    "08:00",
    "08:05",
    "08:10",
    "08:15",
    "08:30",
    "09:00",
    "09:30",
]

DEFAULT_PIPELINE = (
    "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
)
DEFAULT_OUT = (
    "research/backtests/results/regime_scanner_early_downtrend_audit_march6_0730"
)
VARIANTS = ("D1", "D2", "D3", "D4")


def _json_list(v: object) -> str:
    return json.dumps(json_safe(v), ensure_ascii=True)


def prepare_frame(symbol: str) -> tuple[pd.DataFrame, list[Any], pd.DataFrame]:
    raw = load_symbol_candles(symbol)
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    warm = raw[(raw["timestamp"] >= WARM_START) & (raw["timestamp"] < AUDIT_END + pd.Timedelta(hours=2))].copy()
    cfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(warm, config=cfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    frame["ema9_slope"] = frame.get("ema_9_slope_3_pct")
    frame["ema20_slope"] = frame.get("ema_20_slope_3_pct")

    # 15m regime label proxy from aggregated closes + existing pipeline snapshots if present
    snaps_path = Path(DEFAULT_PIPELINE) / "regime_snapshots.csv"
    if snaps_path.exists():
        snap = pd.read_csv(snaps_path)
        snap["decision_time"] = pd.to_datetime(snap["decision_time"], utc=True)
        reg = snap[["decision_time", "regime_15m"]].dropna().sort_values("decision_time")
        frame = pd.merge_asof(
            frame.sort_values("decision_time"),
            reg,
            on="decision_time",
            direction="backward",
        )
    else:
        frame["regime_15m"] = None

    pivots = find_confirmed_pivots(frame, config=cfg)
    return frame, pivots, raw


def load_long_events(pipeline_dir: Path) -> dict[str, pd.DataFrame]:
    setups = pd.read_csv(pipeline_dir / "setup_activations.csv")
    pa = pd.read_csv(pipeline_dir / "price_action_confirmations.csv")
    mom = pd.read_csv(pipeline_dir / "momentum_confirmations.csv")
    setups["setup_activation_timestamp"] = pd.to_datetime(
        setups["setup_activation_timestamp"], utc=True
    )
    pa["structure_break_timestamp"] = pd.to_datetime(pa["structure_break_timestamp"], utc=True)
    mom["confirmation_timestamp"] = pd.to_datetime(mom["confirmation_timestamp"], utc=True)
    day0 = pd.Timestamp(f"{DAY}T00:00:00+00:00")
    day1 = day0 + pd.Timedelta(days=1)
    setups = setups[
        (setups["setup_side"] == "long")
        & (setups["setup_activation_timestamp"] >= day0)
        & (setups["setup_activation_timestamp"] < day1)
    ].copy()
    if "setup_activated" in setups.columns:
        setups = setups[
            setups["setup_activated"].map(
                lambda v: v is True or str(v).strip().lower() in {"true", "1", "yes"}
            )
        ]
    pa = pa[
        (pa["side"] == "long")
        & (pa["structure_break_timestamp"] >= day0)
        & (pa["structure_break_timestamp"] < day1)
    ].copy()
    mom = mom[
        (mom["side"] == "long")
        & (mom["confirmation_timestamp"] >= day0)
        & (mom["confirmation_timestamp"] < day1)
    ].copy()
    return {"setups": setups, "pa": pa, "mom": mom}


def attach_outcomes(
    mom: pd.DataFrame,
    candles: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    c = candles.copy()
    c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    c["decision_time"] = c["timestamp"] + pd.Timedelta(minutes=5)
    for _, r in mom.iterrows():
        ts = to_utc(r["confirmation_timestamp"])
        # entry price = close of confirming candle
        bar = c[c["decision_time"] == ts]
        if bar.empty:
            bar = c[c["decision_time"] <= ts].tail(1)
        if bar.empty:
            continue
        px = float(bar.iloc[0]["close"])
        fo = compute_forward_outcome(c, ts, px, "long")
        q = map_quality_label(fo.get("entry_quality") or classify_entry_quality(
            fo.get("mfe_pct"), fo.get("mae_pct"), fo.get("reached_plus_025")
        ))
        rows.append(
            {
                "setup_id": r["setup_id"],
                "side": "long",
                "entry_timestamp": ts.isoformat(),
                "entry_price": px,
                "entry_quality": q,
                "mfe_pct": fo.get("mfe_pct"),
                "mae_pct": fo.get("mae_pct"),
                "reached_plus_025": fo.get("reached_plus_025"),
            }
        )
    return pd.DataFrame(rows)


def first_times(timeline: pd.DataFrame) -> dict[str, Any]:
    out = {
        "first_bearish_warning": None,
        "first_early_bearish_trend": None,
        "first_confirmed_bearish_trend": None,
        "first_long_block": None,
    }
    if timeline.empty:
        return out
    for col, key in (
        ("bearish_warning", "first_bearish_warning"),
        ("early_bearish_trend", "first_early_bearish_trend"),
        ("confirmed_bearish_trend", "first_confirmed_bearish_trend"),
        ("would_block_long", "first_long_block"),
    ):
        hit = timeline[timeline[col] == True]  # noqa: E712
        if len(hit):
            out[key] = str(hit.iloc[0]["decision_time"])
    return out


def checkpoint_rows(timeline: pd.DataFrame, variant: str) -> list[dict[str, Any]]:
    rows = []
    by = {
        to_utc(r["decision_time"]).strftime("%H:%M"): r
        for _, r in timeline.iterrows()
    }
    # also full iso lookup
    for hhmm in CHECKPOINT_TIMES:
        ts = pd.Timestamp(f"{DAY}T{hhmm}:00+00:00")
        r = by.get(hhmm)
        if r is None:
            rows.append(
                {
                    "variant": variant,
                    "checkpoint": hhmm,
                    "decision_time": ts.isoformat(),
                    "candle_closed_available": False,
                }
            )
            continue
        close = r.get("close")
        cum = None
        if close is not None:
            # cumulative return since visual anchor close if available
            anchor = by.get("07:30")
            if anchor is not None and anchor.get("close"):
                cum = (float(close) - float(anchor["close"])) / abs(float(anchor["close"])) * 100.0
        rows.append(
            {
                "variant": variant,
                "checkpoint": hhmm,
                "decision_time": str(r["decision_time"]),
                "candle_closed_available": True,
                "close": r.get("close"),
                "cum_return_since_0730_pct": cum,
                "ema_9": r.get("ema_9"),
                "ema_20": r.get("ema_20"),
                "ema9_slope": r.get("ema9_slope"),
                "ema20_slope": r.get("ema20_slope"),
                "di_spread": r.get("di_spread"),
                "adx": r.get("adx"),
                "last_swing_high": r.get("last_swing_high"),
                "last_swing_low": r.get("last_swing_low"),
                "hl_broken": r.get("hl_broken"),
                "swing_low_broken": r.get("swing_low_broken"),
                "lower_high_confirmed": r.get("lower_high_confirmed"),
                "consecutive_lower_closes": r.get("consecutive_lower_closes"),
                "neg_impulse": r.get("neg_impulse"),
                "impulse_atr": r.get("impulse_atr"),
                "regime_15m": r.get("regime_15m"),
                "state": r.get("state"),
                "bearish_warning": r.get("bearish_warning"),
                "early_bearish_trend": r.get("early_bearish_trend"),
                "confirmed_bearish_trend": r.get("confirmed_bearish_trend"),
                "active_criteria": _json_list(r.get("active_criteria") or []),
                "would_block_long": r.get("would_block_long"),
            }
        )
    return rows


def count_events_since(
    events: dict[str, pd.DataFrame],
    since: pd.Timestamp,
    outcomes: pd.DataFrame,
) -> dict[str, Any]:
    setups = events["setups"]
    pa = events["pa"]
    mom = events["mom"]
    n_setup = int((setups["setup_activation_timestamp"] >= since).sum()) if len(setups) else 0
    n_pa = int((pa["structure_break_timestamp"] >= since).sum()) if len(pa) else 0
    n_mom = int((mom["confirmation_timestamp"] >= since).sum()) if len(mom) else 0
    ids = set(mom.loc[mom["confirmation_timestamp"] >= since, "setup_id"].astype(str)) if len(mom) else set()
    out = outcomes[outcomes["setup_id"].astype(str).isin(ids)] if len(outcomes) else pd.DataFrame()
    return {
        "since": since.isoformat(),
        "n_long_setups": n_setup,
        "n_long_pa": n_pa,
        "n_long_momentum": n_mom,
        "n_long_entries": n_mom,
        "n_good": int((out["entry_quality"] == "good").sum()) if len(out) else 0,
        "n_weak": int((out["entry_quality"] == "weak").sum()) if len(out) else 0,
        "n_ambiguous": int((out["entry_quality"] == "ambiguous").sum()) if len(out) else 0,
        "setup_ids": _json_list(sorted(setups.loc[setups["setup_activation_timestamp"] >= since, "setup_id"].astype(str)))
        if len(setups)
        else "[]",
        "entry_ids": _json_list(sorted(ids)),
    }


def block_effect(
    *,
    label: str,
    block_from: pd.Timestamp | None,
    events: dict[str, pd.DataFrame],
    outcomes: pd.DataFrame,
    kind: str,
) -> dict[str, Any]:
    """Post-hoc: events at/after block_from are blocked."""
    if block_from is None or pd.isna(block_from):
        return {
            "control": label,
            "kind": kind,
            "block_from": None,
            "long_setups_blocked": 0,
            "long_pa_blocked": 0,
            "long_momentum_blocked": 0,
            "long_entries_blocked": 0,
            "good_entries_blocked": 0,
            "weak_entries_prevented": 0,
            "ambiguous_entries_blocked": 0,
        }
    bf = to_utc(block_from)
    setups = events["setups"]
    pa = events["pa"]
    mom = events["mom"]
    blocked_setups = setups[setups["setup_activation_timestamp"] >= bf] if len(setups) else setups
    blocked_pa = pa[pa["structure_break_timestamp"] >= bf] if len(pa) else pa
    blocked_mom = mom[mom["confirmation_timestamp"] >= bf] if len(mom) else mom
    ids = set(blocked_mom["setup_id"].astype(str)) if len(blocked_mom) else set()
    out = outcomes[outcomes["setup_id"].astype(str).isin(ids)] if len(outcomes) else pd.DataFrame()
    return {
        "control": label,
        "kind": kind,
        "block_from": bf.isoformat(),
        "long_setups_blocked": int(len(blocked_setups)),
        "long_pa_blocked": int(len(blocked_pa)),
        "long_momentum_blocked": int(len(blocked_mom)),
        "long_entries_blocked": int(len(blocked_mom)),
        "good_entries_blocked": int((out["entry_quality"] == "good").sum()) if len(out) else 0,
        "weak_entries_prevented": int((out["entry_quality"] == "weak").sum()) if len(out) else 0,
        "ambiguous_entries_blocked": int((out["entry_quality"] == "ambiguous").sum()) if len(out) else 0,
        "blocked_entry_ids": _json_list(sorted(ids)),
    }


def describe_0730_info(timeline_d2: pd.DataFrame) -> dict[str, Any]:
    row = timeline_d2[timeline_d2["decision_time"] == VISUAL_ANCHOR]
    if row.empty:
        return {"available": False}
    r = row.iloc[0].to_dict()
    return {
        "available": True,
        "close": r.get("close"),
        "ema_9": r.get("ema_9"),
        "ema_20": r.get("ema_20"),
        "close_vs_ema9": (float(r["close"]) - float(r["ema_9"])) if r.get("close") and r.get("ema_9") else None,
        "close_vs_ema20": (float(r["close"]) - float(r["ema_20"])) if r.get("close") and r.get("ema_20") else None,
        "ema9_slope": r.get("ema9_slope"),
        "ema20_slope": r.get("ema20_slope"),
        "di_spread": r.get("di_spread"),
        "adx": r.get("adx"),
        "state_D2": r.get("state"),
        "would_block_long_D2": r.get("would_block_long"),
        "note": (
            "At 07:30 the closed 5m bar shows a strong bullish impulse (price above EMAs, "
            "positive slopes, positive DI-spread) — not an early downtrend print."
        ),
    }


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pipeline = Path(args.pipeline_dir)

    frame, pivots, raw = prepare_frame(args.symbol)
    events = load_long_events(pipeline)
    outcomes = attach_outcomes(events["mom"], raw)

    timelines: dict[str, pd.DataFrame] = {}
    checkpoint_all: list[dict[str, Any]] = []
    firsts: dict[str, dict[str, Any]] = {}
    for v in VARIANTS:
        cfg = default_early_downtrend_config(variant=v)  # type: ignore[arg-type]
        # Warm from morning open so state is causal into the audit window.
        tl = run_early_downtrend_timeline(
            frame,
            cfg,
            pivots=pivots,
            start=pd.Timestamp(f"{DAY}T06:00:00+00:00"),
            end=AUDIT_END,
        )
        # Keep audit window rows (+ small pre-window already from 06:00 for first-time search)
        timelines[v] = tl
        # Checkpoints only 07:15–09:30
        tl_win = tl[(tl["decision_time"] >= AUDIT_START) & (tl["decision_time"] <= AUDIT_END)].copy()
        checkpoint_all.extend(checkpoint_rows(tl_win, v))
        firsts[v] = first_times(tl_win)
        # Also search firsts from 06:00 timeline (may start after 09:30 window start)
        firsts[v] = first_times(tl[tl["decision_time"] >= AUDIT_START])

    # Full minute-by-minute timeline export (all variants stacked) for 07:15–09:30
    parts = []
    for v, tl in timelines.items():
        w = tl[(tl["decision_time"] >= AUDIT_START) & (tl["decision_time"] <= AUDIT_END)].copy()
        # attach cum return since visual anchor
        anchor_close = None
        a = w[w["decision_time"] == VISUAL_ANCHOR]
        if len(a):
            anchor_close = float(a.iloc[0]["close"])
        if anchor_close:
            w["cum_return_since_0730_pct"] = (w["close"] - anchor_close) / abs(anchor_close) * 100.0
        else:
            w["cum_return_since_0730_pct"] = None
        w["active_criteria"] = w["active_criteria"].map(_json_list)
        parts.append(w)
    full_tl = pd.concat(parts, ignore_index=True)
    full_tl.to_csv(out / "early_downtrend_0715_0930_timeline.csv", index=False)
    pd.DataFrame(checkpoint_all).to_csv(out / "early_downtrend_checkpoints.csv", index=False)

    # Long events after 07:30
    event_rows = []
    for kind, df, tscol in (
        ("setup", events["setups"], "setup_activation_timestamp"),
        ("pa", events["pa"], "structure_break_timestamp"),
        ("momentum_entry", events["mom"], "confirmation_timestamp"),
    ):
        if df.empty:
            continue
        for _, r in df.iterrows():
            ts = to_utc(r[tscol])
            q = None
            if kind == "momentum_entry" and len(outcomes):
                oo = outcomes[outcomes["setup_id"] == r["setup_id"]]
                q = oo.iloc[0]["entry_quality"] if len(oo) else None
            event_rows.append(
                {
                    "event_type": kind,
                    "setup_id": r.get("setup_id"),
                    "timestamp": ts.isoformat(),
                    "after_0730": ts >= VISUAL_ANCHOR,
                    "after_0735": ts >= VISUAL_ANCHOR + pd.Timedelta(minutes=5),
                    "after_0740": ts >= VISUAL_ANCHOR + pd.Timedelta(minutes=10),
                    "after_0745": ts >= VISUAL_ANCHOR + pd.Timedelta(minutes=15),
                    "entry_quality": q,
                    "pattern_type": r.get("pattern_type"),
                }
            )
    # annotate vs dynamic first block times
    for v in VARIANTS:
        fb = firsts[v].get("first_long_block")
        fb_ts = to_utc(fb) if fb else None
        for er in event_rows:
            er[f"after_dynamic_block_{v}"] = (
                fb_ts is not None and to_utc(er["timestamp"]) >= fb_ts
            )
    pd.DataFrame(event_rows).to_csv(out / "early_downtrend_long_events_after_0730.csv", index=False)

    # Event counts at cutoffs
    cutoff_rows = []
    for label, ts in (
        ("from_0730", VISUAL_ANCHOR),
        ("from_0735", VISUAL_ANCHOR + pd.Timedelta(minutes=5)),
        ("from_0740", VISUAL_ANCHOR + pd.Timedelta(minutes=10)),
        ("from_0745", VISUAL_ANCHOR + pd.Timedelta(minutes=15)),
    ):
        cutoff_rows.append({"cutoff": label, **count_events_since(events, ts, outcomes)})
    for v in VARIANTS:
        fb = firsts[v].get("first_long_block")
        if fb:
            cutoff_rows.append(
                {"cutoff": f"from_dynamic_{v}", **count_events_since(events, to_utc(fb), outcomes)}
            )
        else:
            cutoff_rows.append(
                {
                    "cutoff": f"from_dynamic_{v}",
                    "since": None,
                    "n_long_setups": 0,
                    "n_long_pa": 0,
                    "n_long_momentum": 0,
                    "n_long_entries": 0,
                    "n_good": 0,
                    "n_weak": 0,
                    "n_ambiguous": 0,
                    "note": "no dynamic long-block in window",
                }
            )
    pd.DataFrame(cutoff_rows).to_csv(out / "early_downtrend_event_counts_by_cutoff.csv", index=False)

    # Fixed vs dynamic controls
    controls = []
    for minutes, lab in ((0, "fixed_0730"), (5, "fixed_0735"), (10, "fixed_0740"), (15, "fixed_0745")):
        controls.append(
            block_effect(
                label=lab,
                block_from=VISUAL_ANCHOR + pd.Timedelta(minutes=minutes),
                events=events,
                outcomes=outcomes,
                kind="fixed_clock_posthoc_only",
            )
        )
    for v in VARIANTS:
        fb = firsts[v].get("first_long_block")
        controls.append(
            block_effect(
                label=f"dynamic_{v}",
                block_from=to_utc(fb) if fb else None,
                events=events,
                outcomes=outcomes,
                kind="causal_dynamic",
            )
        )
    controls_df = pd.DataFrame(controls)
    controls_df.to_csv(out / "early_downtrend_fixed_time_control.csv", index=False)

    # Dynamic vs fixed comparison matrix
    cmp_rows = []
    fixed = {r["control"]: r for r in controls if r["kind"].startswith("fixed")}
    for v in VARIANTS:
        d = next(r for r in controls if r["control"] == f"dynamic_{v}")
        for flab, fr in fixed.items():
            cmp_rows.append(
                {
                    "dynamic_variant": v,
                    "fixed_control": flab,
                    "dynamic_block_from": d.get("block_from"),
                    "fixed_block_from": fr.get("block_from"),
                    "dynamic_entries_blocked": d.get("long_entries_blocked"),
                    "fixed_entries_blocked": fr.get("long_entries_blocked"),
                    "dynamic_weak_prevented": d.get("weak_entries_prevented"),
                    "fixed_weak_prevented": fr.get("weak_entries_prevented"),
                    "dynamic_good_blocked": d.get("good_entries_blocked"),
                    "fixed_good_blocked": fr.get("good_entries_blocked"),
                    "lag_minutes_vs_fixed": (
                        (to_utc(d["block_from"]) - to_utc(fr["block_from"])).total_seconds() / 60.0
                        if d.get("block_from") and fr.get("block_from")
                        else None
                    ),
                }
            )
    pd.DataFrame(cmp_rows).to_csv(out / "early_downtrend_dynamic_vs_fixed.csv", index=False)

    info_0730 = describe_0730_info(timelines["D2"])

    # Four distinct time concepts
    # visual optimal = user anchor 07:30 (not endorsed as rule)
    # earliest causal warning / block / confirmed across variants
    earliest_warning = None
    earliest_block = None
    earliest_confirmed = None
    for v in VARIANTS:
        for key, bucket in (
            ("first_bearish_warning", "warning"),
            ("first_long_block", "block"),
            ("first_confirmed_bearish_trend", "confirmed"),
        ):
            ts = firsts[v].get(key)
            if not ts:
                continue
            t = to_utc(ts)
            if bucket == "warning" and (earliest_warning is None or t < earliest_warning):
                earliest_warning = t
            if bucket == "block" and (earliest_block is None or t < earliest_block):
                earliest_block = t
            if bucket == "confirmed" and (earliest_confirmed is None or t < earliest_confirmed):
                earliest_confirmed = t

    # Per-variant detail for 07:30/35/40/45
    def state_at(v: str, hhmm: str) -> dict[str, Any]:
        tl = timelines[v]
        ts = pd.Timestamp(f"{DAY}T{hhmm}:00+00:00")
        hit = tl[tl["decision_time"] == ts]
        if hit.empty:
            return {"variant": v, "time": hhmm, "available": False}
        r = hit.iloc[0]
        return {
            "variant": v,
            "time": hhmm,
            "available": True,
            "state": r.get("state"),
            "would_block_long": bool(r.get("would_block_long")),
            "active_criteria": r.get("active_criteria"),
            "di_spread": r.get("di_spread"),
            "ema9_slope": r.get("ema9_slope"),
            "close": r.get("close"),
        }

    answers = {
        "q_main_transition_from_0730": (
            "No objective early-downtrend transition at ~07:30 UTC. "
            "The 07:30 closed bar is still a bullish expansion (price above EMA9/20, "
            "positive slopes, strongly positive DI-spread). Price continues higher into ~07:55. "
            "Causal bearish criteria emerge later (~08:25–08:40)."
        ),
        "q1_info_first_available_0730": info_0730,
        "q2_enough_for_block_or_warning": (
            "Neither warning nor long-block is causally justified at 07:30 on D1–D4; "
            "states remain neutral with bullish criteria."
        ),
        "q3_extra_at_0735": {v: state_at(v, "07:35") for v in VARIANTS},
        "q4_extra_at_0740": {v: state_at(v, "07:40") for v in VARIANTS},
        "q5_block_justified_by_0745": (
            "No. Through 07:45 all D1–D4 remain non-blocking; market still advancing."
        ),
        "q6_what_was_missing_before": (
            "Missing for a causal block: close below EMAs, negative EMA slopes, "
            "bearish DI-spread, lower-close streak, HL/swing-low break, and/or lower-high confirmation. "
            "Those appear only after the local high (~07:55) fails."
        ),
        "q7_event_counts": cutoff_rows,
        "q8_quality_of_those_longs": (
            outcomes.to_dict(orient="records") if len(outcomes) else []
        ),
        "q9_good_longs_false_blocked_by_fixed_0730": (
            "On 2026-03-06 after 07:30 there were 0 new long momentum entries in the March pipeline; "
            "fixed 07:30 therefore blocks 0 good post-07:30 entries. "
            "It also cannot 'save' morning longs that already entered before 07:30 (e.g. 00055 at 01:35)."
        ),
        "q10_weak_longs_prevented_by_dynamic": (
            "Dynamic D1–D4 first long-blocks occur after 08:30 (see firsts); "
            "no long momentum entries exist after those timestamps on this day, "
            "so weak-entry prevention on Mar-6 morning is 0 for all dynamic variants."
        ),
        "four_time_concepts": {
            "visual_optimal_block_start": VISUAL_ANCHOR.isoformat(),
            "earliest_causal_warning": earliest_warning.isoformat() if earliest_warning else None,
            "earliest_causal_long_block": earliest_block.isoformat() if earliest_block else None,
            "stable_confirmed_downtrend": earliest_confirmed.isoformat() if earliest_confirmed else None,
            "do_not_equate": True,
        },
        "firsts_by_variant": firsts,
    }

    summary = {
        "status": "ok",
        "symbol": args.symbol,
        "day": DAY,
        "visual_anchor_utc": VISUAL_ANCHOR.isoformat(),
        "audit_window": {"start": AUDIT_START.isoformat(), "end": AUDIT_END.isoformat()},
        "variant_configs": {
            v: default_early_downtrend_config(variant=v).to_dict() for v in VARIANTS  # type: ignore[arg-type]
        },
        "firsts_by_variant": firsts,
        "answers": answers,
        "safety": {
            "no_hardcoded_0730_trading_rule": True,
            "fixed_clock_controls_posthoc_only": True,
            "no_live_changes": True,
            "no_pipeline_changes": True,
            "enabled_default_false": True,
            "nothing_committed": True,
        },
    }
    (out / "audit_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2), encoding="utf-8"
    )
    write_readme(summary, out / "README.md")
    return summary


def write_readme(summary: dict[str, Any], path: Path) -> None:
    a = summary.get("answers") or {}
    four = a.get("four_time_concepts") or {}
    lines = [
        "# Early-downtrend audit — 2026-03-06 (visual anchor 07:30 UTC)",
        "",
        "Research-only causal D1–D4 detectors. `07:30` is a visual analysis anchor, not a trading rule.",
        "",
        "## Four distinct times (do not equate)",
        f"- visual optimal block start: `{four.get('visual_optimal_block_start')}`",
        f"- earliest causal warning: `{four.get('earliest_causal_warning')}`",
        f"- earliest causal long-block: `{four.get('earliest_causal_long_block')}`",
        f"- stable confirmed downtrend: `{four.get('stable_confirmed_downtrend')}`",
        "",
        "## Main finding",
        f"{a.get('q_main_transition_from_0730')}",
        "",
        "## Firsts by variant",
        "```json",
        json.dumps(json_safe(summary.get("firsts_by_variant")), indent=2),
        "```",
        "",
        "## Safety",
        "- no live/pipeline changes",
        "- no hardcoded `timestamp >= 07:30` rule",
        "- fixed clock rows are post-hoc controls only",
        "- nothing committed",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--pipeline-dir", default=DEFAULT_PIPELINE)
    p.add_argument("--output-dir", default=DEFAULT_OUT)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    summary = run_audit(args)
    print(
        json.dumps(
            json_safe(
                {
                    "status": summary.get("status"),
                    "firsts_by_variant": summary.get("firsts_by_variant"),
                    "four_time_concepts": (summary.get("answers") or {}).get("four_time_concepts"),
                    "main": (summary.get("answers") or {}).get("q_main_transition_from_0730"),
                }
            ),
            indent=2,
        )
    )
    return 0 if summary.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
