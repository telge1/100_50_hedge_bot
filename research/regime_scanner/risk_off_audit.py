"""Research-only March-week Breakdown / Risk-Off audit CLI.

Counterfactual only. Does not mutate pipeline CSVs or live strategy.
Risk-Off stays disabled by default in ``RiskOffConfig``; this audit walks
variants R1–R4 against existing pipeline + B3 artifacts and joins outcomes
only after the timeline is complete.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.risk_off import (
    RiskOffConfig,
    RiskVariant,
    blocking_layer,
    run_risk_off_timeline,
    would_block_short,
)

FOCUS_SETUPS = ("setup_00055", "setup_00056", "setup_00057", "setup_00058", "setup_00059")
VARIANTS: tuple[RiskVariant, ...] = ("R1", "R2", "R3", "R4")
B3_REF_STRONG_BEARISH = "2026-03-06T14:45:00+00:00"
FALSE_OFF_ADVERSE_THRESH_PCT = 0.5
FALSE_OFF_FORWARD_BARS = 12
MISSED_DROP_THRESH_PCT = 1.5


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [p for p in parts if p is not None and len(p)]
    if not nonempty:
        return pd.DataFrame()
    return pd.concat(nonempty, ignore_index=True)


def classify_long_quality(row: dict[str, Any]) -> str:
    """Post-hoc only — never fed into risk state."""
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


def _to_utc(ts: object) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _truthy(v: object) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    return str(v).strip().lower() in {"true", "1", "yes"}


def _is_risk_off_long(state: object) -> bool:
    return str(state or "") in {"long_risk_off", "covered_by_strong_bearish"}


def _is_elevated_long(state: object) -> bool:
    return str(state or "") == "long_risk_elevated"


def _is_b3_strong_bearish(state: object) -> bool:
    return str(state or "").strip().lower() == "strong_bearish"


def build_regime_15m_by_decision(snapshots: pd.DataFrame) -> pd.DataFrame:
    df = snapshots.copy()
    df["decision_time"] = pd.to_datetime(df["decision_time"], utc=True)
    return (
        df[["decision_time", "regime_15m"]]
        .dropna(subset=["decision_time"])
        .sort_values("decision_time")
        .drop_duplicates("decision_time", keep="last")
    )


def build_b3_by_decision(
    b3_15m: pd.DataFrame,
    decision_times: pd.Series,
) -> pd.DataFrame:
    """Expand B3 15m closes onto 5m decision_times via merge_asof on bar_close_time."""
    g = b3_15m.copy()
    g["bar_close_time"] = pd.to_datetime(g["bar_close_time"], utc=True)
    g = g.sort_values("bar_close_time")
    left = pd.DataFrame({"decision_time": pd.to_datetime(decision_times, utc=True)}).sort_values(
        "decision_time"
    )
    merged = pd.merge_asof(
        left,
        g.rename(columns={"direction_gate_state": "b3_state"})[
            ["bar_close_time", "b3_state"]
        ].sort_values("bar_close_time"),
        left_on="decision_time",
        right_on="bar_close_time",
        direction="backward",
    )
    return merged[["decision_time", "b3_state"]].drop_duplicates("decision_time", keep="last")


def join_risk_at_time(timeline: pd.DataFrame, ts: pd.Timestamp) -> dict[str, Any]:
    """Last closed risk row with decision_time <= ts (merge_asof backward)."""
    if timeline is None or timeline.empty or pd.isna(ts):
        return {
            "decision_time": None,
            "risk_state": "unavailable",
            "long_risk_score": None,
            "b3_state": None,
            "would_block_long": False,
            "blocking_layer_long": "none",
            "momentum_quality_long": None,
            "entry_reason": None,
            "long_risk_reason": None,
        }
    t = timeline[timeline["decision_time"] <= ts]
    if t.empty:
        return {
            "decision_time": None,
            "risk_state": "unavailable",
            "long_risk_score": None,
            "b3_state": None,
            "would_block_long": False,
            "blocking_layer_long": "none",
            "momentum_quality_long": None,
            "entry_reason": None,
            "long_risk_reason": None,
        }
    row = t.iloc[-1]
    return {
        "decision_time": str(row.get("decision_time")),
        "risk_state": row.get("risk_state"),
        "long_risk_score": row.get("long_risk_score"),
        "b3_state": row.get("b3_state"),
        "would_block_long": bool(row.get("would_block_long")),
        "blocking_layer_long": row.get("blocking_layer_long"),
        "momentum_quality_long": row.get("momentum_quality_long"),
        "entry_reason": row.get("entry_reason"),
        "long_risk_reason": row.get("long_risk_reason"),
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("close"),
        "ret_1": row.get("ret_1"),
        "ret_2": row.get("ret_2"),
        "ret_3": row.get("ret_3"),
        "ret_4": row.get("ret_4"),
    }


def confirm_decision_times(
    decision_index: pd.DatetimeIndex,
    after_ts: pd.Timestamp,
    n: int = 3,
) -> list[pd.Timestamp | None]:
    """First/second/third closed 5m decision_times strictly after ``after_ts``."""
    after = _to_utc(after_ts)
    later = [t for t in decision_index if t > after]
    out: list[pd.Timestamp | None] = []
    for i in range(n):
        out.append(later[i] if i < len(later) else None)
    return out


def _abort_reason(risk: dict[str, Any], stage: str) -> str | None:
    if _is_b3_strong_bearish(risk.get("b3_state")):
        if stage.startswith("confirm"):
            return "STRONG_BEARISH_DURING_CONFIRMATION"
        return f"b3_strong_bearish_at_{stage}"
    if _is_risk_off_long(risk.get("risk_state")) or _truthy(risk.get("would_block_long")):
        layer = risk.get("blocking_layer_long") or blocking_layer(
            risk.get("risk_state"), risk.get("b3_state"), "long"
        )
        return f"risk_block_{layer}_at_{stage}"
    return None


def evaluate_confirmation_window(
    *,
    setup_id: str,
    side: str,
    setup_ts: pd.Timestamp,
    pa_ts: pd.Timestamp | None,
    timeline: pd.DataFrame,
    decision_index: pd.DatetimeIndex,
    variant: str,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    """Counterfactual 2–3 candle confirmation walk for one long setup."""
    anchor = pa_ts if pa_ts is not None else setup_ts
    anchor_label = "pa" if pa_ts is not None else "setup_no_pa"
    confirms = confirm_decision_times(decision_index, anchor, n=3)

    risk_setup = join_risk_at_time(timeline, setup_ts)
    risk_pa = join_risk_at_time(timeline, pa_ts) if pa_ts is not None else {}
    risk_c = [join_risk_at_time(timeline, t) if t is not None else {} for t in confirms]

    # Window size: elevated at PA (or setup if no PA) → 3 candles; else 2.
    base_state = risk_pa.get("risk_state") if pa_ts is not None else risk_setup.get("risk_state")
    window_size = 3 if _is_elevated_long(base_state) else 2
    # Mid-window elevation also extends requirement.
    for rc in risk_c[:2]:
        if _is_elevated_long(rc.get("risk_state")):
            window_size = 3

    stages: list[tuple[str, pd.Timestamp | None, dict[str, Any]]] = [
        ("setup", setup_ts, risk_setup),
    ]
    if pa_ts is not None:
        stages.append(("pa", pa_ts, risk_pa))
    for i, (ct, rc) in enumerate(zip(confirms, risk_c), start=1):
        stages.append((f"confirm_{i}", ct, rc))

    abort_ts = None
    abort_reason = None
    abort_stage = None
    abort_candle_index = None
    for stage, ts, risk in stages:
        if ts is None or not risk:
            continue
        reason = _abort_reason(risk, stage)
        if reason:
            abort_ts = str(ts)
            abort_reason = reason
            abort_stage = stage
            if stage.startswith("confirm_"):
                try:
                    abort_candle_index = int(stage.split("_")[1])
                except (IndexError, ValueError):
                    abort_candle_index = None
            elif stage == "setup":
                abort_candle_index = 0
            elif stage == "pa":
                abort_candle_index = 0
            break

    def _allowed_after(n: int) -> tuple[bool, str | None]:
        if window_size > n:
            return False, None
        ct = confirms[n - 1] if n - 1 < len(confirms) else None
        if ct is None:
            return False, None
        if abort_stage in {"setup", "pa"}:
            return False, None
        if abort_candle_index is not None and abort_stage and str(abort_stage).startswith("confirm_"):
            if int(abort_candle_index) <= n:
                return False, None
        risk = risk_c[n - 1]
        if _is_risk_off_long(risk.get("risk_state")) or _is_b3_strong_bearish(risk.get("b3_state")):
            return False, None
        if _truthy(risk.get("would_block_long")):
            return False, None
        return True, str(ct)

    entry2, entry2_ts = _allowed_after(2)
    entry3, entry3_ts = _allowed_after(3)

    scores = [
        risk_setup.get("long_risk_score"),
        risk_pa.get("long_risk_score") if pa_ts is not None else None,
        *[rc.get("long_risk_score") for rc in risk_c],
    ]
    score_nums = []
    for s in scores:
        try:
            if s is not None and str(s) not in {"", "nan", "None"}:
                score_nums.append(float(s))
        except (TypeError, ValueError):
            pass
    score_change = None
    if len(score_nums) >= 2:
        score_change = score_nums[-1] - score_nums[0]

    quality = classify_long_quality(outcome) if side == "long" else None
    block_risk = _is_risk_off_long(risk_setup.get("risk_state")) or any(
        _is_risk_off_long(r.get("risk_state")) for r in ([risk_pa] if pa_ts is not None else []) + risk_c
    )
    block_b3 = _is_b3_strong_bearish(risk_setup.get("b3_state")) or any(
        _is_b3_strong_bearish(r.get("b3_state")) for r in ([risk_pa] if pa_ts is not None else []) + risk_c
    )
    # Would block before planned entry (through required window)
    would_block_seq = abort_stage is not None and (
        abort_stage in {"setup", "pa"}
        or (abort_candle_index is not None and abort_candle_index <= window_size)
    )

    outcome_class = None
    if quality == "weak" and would_block_seq:
        outcome_class = "weak_correctly_blocked"
    elif quality == "good" and would_block_seq:
        outcome_class = "good_falsely_blocked"
    elif quality == "weak" and not would_block_seq:
        outcome_class = "weak_not_blocked"
    elif quality == "good" and not would_block_seq:
        outcome_class = "good_correctly_allowed"
    elif quality in {None, "unknown", "mixed"}:
        outcome_class = "insufficient_outcome_coverage"

    first_elev = None
    first_off = None
    for stage, ts, risk in stages:
        if ts is None:
            continue
        if first_elev is None and _is_elevated_long(risk.get("risk_state")):
            first_elev = str(ts)
        if first_off is None and _is_risk_off_long(risk.get("risk_state")):
            first_off = str(ts)

    return {
        "setup_id": setup_id,
        "setup_side": side,
        "risk_variant": variant,
        "setup_activation_timestamp": str(setup_ts),
        "pa_structure_break_timestamp": str(pa_ts) if pa_ts is not None else None,
        "confirmation_anchor": anchor_label,
        "confirm_1_timestamp": str(confirms[0]) if confirms[0] is not None else None,
        "confirm_2_timestamp": str(confirms[1]) if confirms[1] is not None else None,
        "confirm_3_timestamp": str(confirms[2]) if confirms[2] is not None else None,
        "risk_state_at_setup": risk_setup.get("risk_state"),
        "risk_score_at_setup": risk_setup.get("long_risk_score"),
        "b3_state_at_setup": risk_setup.get("b3_state"),
        "momentum_quality_at_setup": risk_setup.get("momentum_quality_long"),
        "risk_state_at_pa": risk_pa.get("risk_state") if pa_ts is not None else None,
        "risk_score_at_pa": risk_pa.get("long_risk_score") if pa_ts is not None else None,
        "b3_state_at_pa": risk_pa.get("b3_state") if pa_ts is not None else None,
        "momentum_quality_at_pa": risk_pa.get("momentum_quality_long") if pa_ts is not None else None,
        "risk_state_confirm_1": risk_c[0].get("risk_state"),
        "risk_score_confirm_1": risk_c[0].get("long_risk_score"),
        "b3_state_confirm_1": risk_c[0].get("b3_state"),
        "momentum_quality_confirm_1": risk_c[0].get("momentum_quality_long"),
        "risk_state_confirm_2": risk_c[1].get("risk_state"),
        "risk_score_confirm_2": risk_c[1].get("long_risk_score"),
        "b3_state_confirm_2": risk_c[1].get("b3_state"),
        "momentum_quality_confirm_2": risk_c[1].get("momentum_quality_long"),
        "risk_state_confirm_3": risk_c[2].get("risk_state"),
        "risk_score_confirm_3": risk_c[2].get("long_risk_score"),
        "b3_state_confirm_3": risk_c[2].get("b3_state"),
        "momentum_quality_confirm_3": risk_c[2].get("momentum_quality_long"),
        "risk_score_change_during_confirmation": score_change,
        "confirmation_window_size": window_size,
        "confirmation_aborted": abort_reason is not None,
        "abort_timestamp": abort_ts,
        "abort_reason": abort_reason,
        "abort_stage": abort_stage,
        "confirmation_candle_index": abort_candle_index,
        "entry_allowed_after_2": entry2,
        "entry_allowed_after_3": entry3,
        "entry_timestamp_after_2": entry2_ts,
        "entry_timestamp_after_3": entry3_ts,
        "would_block_by_risk_off": bool(block_risk),
        "would_block_by_strong_trend": bool(block_b3),
        "would_block_by_either": bool(block_risk or block_b3 or would_block_seq),
        "would_block_before_entry": bool(would_block_seq),
        "blocking_layer_at_setup": risk_setup.get("blocking_layer_long"),
        "first_elevated_in_path": first_elev,
        "first_risk_off_in_path": first_off,
        "long_quality": quality,
        "outcome_class": outcome_class,
        "max_adverse_drop_pct": outcome.get("max_adverse_drop_pct"),
        "reached_plus_025": outcome.get("reached_plus_025"),
        "returned_to_signal": outcome.get("returned_to_signal"),
        "mfe_pct": outcome.get("mfe_pct"),
        "mae_pct": outcome.get("mae_pct"),
    }


def build_outcome_maps(
    drop: pd.DataFrame | None,
    forward: pd.DataFrame | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    drop_map: dict[str, dict[str, Any]] = {}
    if drop is not None and len(drop) and "setup_id" in drop.columns:
        drop_map = drop.set_index("setup_id").to_dict("index")
    fwd_map: dict[str, dict[str, Any]] = {}
    if forward is not None and len(forward) and "setup_id" in forward.columns:
        f = forward.copy()
        if "horizon" in f.columns:
            f12 = f[f["horizon"] == 12]
            if len(f12):
                f = f12
        fwd_map = f.groupby("setup_id").first().to_dict("index")
    return drop_map, fwd_map


def outcome_for_setup(
    setup_id: str,
    drop_map: dict[str, dict[str, Any]],
    fwd_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    d = drop_map.get(setup_id, {})
    fw = fwd_map.get(setup_id, {})
    return {
        "max_adverse_drop_pct": d.get("max_adverse_drop_pct", fw.get("mae_pct")),
        "reached_plus_025": d.get("reached_plus_025"),
        "returned_to_signal": d.get("returned_to_signal"),
        "mfe_pct": fw.get("mfe_pct") or fw.get("max_favorable_pct"),
        "mae_pct": fw.get("mae_pct") or fw.get("max_adverse_pct"),
        "adverse_extreme_age": d.get("adverse_extreme_age"),
        "later_favorable_age": d.get("later_favorable_age"),
    }


def setup_counterfactual(
    setups: pd.DataFrame,
    timeline: pd.DataFrame,
    drop_map: dict[str, dict[str, Any]],
    fwd_map: dict[str, dict[str, Any]],
    variant: str,
    window_start: str,
    window_end: str,
) -> pd.DataFrame:
    s = setups.copy()
    s["setup_activation_timestamp"] = pd.to_datetime(s["setup_activation_timestamp"], utc=True)
    s = s[
        (s["setup_activation_timestamp"] >= window_start)
        & (s["setup_activation_timestamp"] < window_end)
    ]
    if "setup_activated" in s.columns:
        s = s[s["setup_activated"].map(_truthy)]
    rows = []
    for _, r in s.iterrows():
        ts = r["setup_activation_timestamp"]
        risk = join_risk_at_time(timeline, ts)
        outcome = outcome_for_setup(r["setup_id"], drop_map, fwd_map)
        side = r.get("setup_side")
        quality = classify_long_quality(outcome) if side == "long" else None
        if side == "long":
            blocked = bool(risk.get("would_block_long"))
        elif side == "short":
            blocked = bool(would_block_short(risk.get("risk_state"), b3_state=risk.get("b3_state")))
        else:
            blocked = False
        rows.append(
            {
                "setup_id": r["setup_id"],
                "setup_activation_timestamp": str(ts),
                "setup_side": side,
                "regime_15m": r.get("regime_15m"),
                "regime_30m": r.get("regime_30m"),
                "risk_variant": variant,
                "risk_state": risk.get("risk_state"),
                "long_risk_score": risk.get("long_risk_score"),
                "b3_state": risk.get("b3_state"),
                "blocking_layer_long": risk.get("blocking_layer_long"),
                "momentum_quality_long": risk.get("momentum_quality_long"),
                "entry_reason": risk.get("entry_reason"),
                "long_risk_reason": risk.get("long_risk_reason"),
                "would_block": blocked,
                "would_block_long": risk.get("would_block_long"),
                "would_block_by_risk_off": _is_risk_off_long(risk.get("risk_state")),
                "would_block_by_b3": _is_b3_strong_bearish(risk.get("b3_state")),
                **outcome,
                "long_quality": quality,
            }
        )
    return pd.DataFrame(rows)


def event_counterfactual(
    events: pd.DataFrame,
    ts_col: str,
    timeline: pd.DataFrame,
    drop_map: dict[str, dict[str, Any]],
    variant: str,
    window_start: str,
    window_end: str,
    side_col: str = "side",
) -> pd.DataFrame:
    if events is None or events.empty:
        return pd.DataFrame()
    x = events.copy()
    x[ts_col] = pd.to_datetime(x[ts_col], utc=True)
    x = x[(x[ts_col] >= window_start) & (x[ts_col] < window_end)]
    rows = []
    for _, r in x.iterrows():
        risk = join_risk_at_time(timeline, r[ts_col])
        side = r.get(side_col)
        outcome = outcome_for_setup(str(r.get("setup_id")), drop_map, {})
        q = classify_long_quality(outcome) if side == "long" else None
        if side == "long":
            blocked = bool(risk.get("would_block_long"))
        elif side == "short":
            blocked = bool(would_block_short(risk.get("risk_state"), b3_state=risk.get("b3_state")))
        else:
            blocked = False
        rows.append(
            {
                "setup_id": r.get("setup_id"),
                "event_timestamp": str(r[ts_col]),
                "side": side,
                "risk_variant": variant,
                "risk_state": risk.get("risk_state"),
                "long_risk_score": risk.get("long_risk_score"),
                "b3_state": risk.get("b3_state"),
                "would_block_long": risk.get("would_block_long"),
                "blocking_layer_long": risk.get("blocking_layer_long"),
                "momentum_quality_long": risk.get("momentum_quality_long"),
                "would_block": blocked,
                "would_block_by_risk_off": _is_risk_off_long(risk.get("risk_state")),
                "would_block_by_b3": _is_b3_strong_bearish(risk.get("b3_state")),
                **outcome,
                "long_quality": q,
            }
        )
    return pd.DataFrame(rows)


def state_change_rows(timeline: pd.DataFrame) -> pd.DataFrame:
    if timeline.empty:
        return pd.DataFrame()
    t = timeline.copy()
    mask = t["transition"].notna() & (t["transition"].astype(str) != "") & (t["transition"].astype(str) != "None")
    return t.loc[mask].copy()


def risk_off_periods(timeline: pd.DataFrame) -> list[dict[str, Any]]:
    """Contiguous runs where long is hard-blocked by risk_off / covered."""
    if timeline.empty:
        return []
    t = timeline.sort_values("decision_time").reset_index(drop=True)
    off = t["risk_state"].map(_is_risk_off_long)
    runs: list[dict[str, Any]] = []
    start = None
    for i, flag in enumerate(off.tolist()):
        if flag and start is None:
            start = i
        if start is not None and (not flag or i == len(off) - 1):
            end = i if flag and i == len(off) - 1 else i - 1
            if end >= start:
                runs.append(
                    {
                        "start_idx": start,
                        "end_idx": end,
                        "start": str(t.iloc[start]["decision_time"]),
                        "end": str(t.iloc[end]["decision_time"]),
                        "n_bars": end - start + 1,
                        "entry_close": t.iloc[start].get("close"),
                        "entry_reason": t.iloc[start].get("entry_reason"),
                        "risk_variant": t.iloc[start].get("risk_variant"),
                    }
                )
            start = None
    return runs


def false_risk_off_table(timeline: pd.DataFrame) -> pd.DataFrame:
    """Risk-off periods whose next N bars max adverse from entry close < thresh."""
    runs = risk_off_periods(timeline)
    if not runs or timeline.empty:
        return pd.DataFrame()
    t = timeline.sort_values("decision_time").reset_index(drop=True)
    closes = t["close"].tolist()
    rows = []
    for run in runs:
        i0 = int(run["start_idx"])
        entry = run.get("entry_close")
        try:
            entry_f = float(entry) if entry is not None else None
        except (TypeError, ValueError):
            entry_f = None
        max_adverse = None
        if entry_f is not None and entry_f != 0.0:
            end = min(len(closes) - 1, i0 + FALSE_OFF_FORWARD_BARS)
            lows = []
            for j in range(i0, end + 1):
                c = closes[j]
                try:
                    cf = float(c)
                except (TypeError, ValueError):
                    continue
                lows.append((entry_f - cf) / abs(entry_f) * 100.0)
            if lows:
                max_adverse = max(lows)
        is_false = max_adverse is not None and max_adverse < FALSE_OFF_ADVERSE_THRESH_PCT
        rows.append(
            {
                **{k: v for k, v in run.items() if k not in {"start_idx", "end_idx"}},
                "forward_bars": FALSE_OFF_FORWARD_BARS,
                "max_adverse_from_entry_pct": max_adverse,
                "false_risk_off": is_false,
                "threshold_pct": FALSE_OFF_ADVERSE_THRESH_PCT,
            }
        )
    return pd.DataFrame(rows)


def missed_adverse_table(
    confirm_df: pd.DataFrame,
    drop_thresh: float = MISSED_DROP_THRESH_PCT,
) -> pd.DataFrame:
    """Long setups with deep drop that never hit risk_off before entry window."""
    if confirm_df.empty:
        return pd.DataFrame()
    c = confirm_df[confirm_df["setup_side"] == "long"].copy()
    rows = []
    for _, r in c.iterrows():
        try:
            drop = float(r["max_adverse_drop_pct"]) if r.get("max_adverse_drop_pct") is not None else None
        except (TypeError, ValueError):
            drop = None
        if drop is None or drop < drop_thresh:
            continue
        states = [
            r.get("risk_state_at_setup"),
            r.get("risk_state_at_pa"),
            r.get("risk_state_confirm_1"),
            r.get("risk_state_confirm_2"),
            r.get("risk_state_confirm_3"),
        ]
        had_off = any(_is_risk_off_long(s) for s in states)
        if had_off:
            continue
        rows.append(
            {
                "setup_id": r.get("setup_id"),
                "risk_variant": r.get("risk_variant"),
                "max_adverse_drop_pct": drop,
                "long_quality": r.get("long_quality"),
                "risk_state_at_setup": r.get("risk_state_at_setup"),
                "risk_state_at_pa": r.get("risk_state_at_pa"),
                "confirmation_aborted": r.get("confirmation_aborted"),
                "would_block_by_b3": r.get("would_block_by_strong_trend"),
                "never_risk_off_before_entry": True,
                "note": "deep_adverse_without_prior_risk_off",
            }
        )
    return pd.DataFrame(rows)


def two_vs_three_comparison(confirm_df: pd.DataFrame) -> pd.DataFrame:
    if confirm_df.empty:
        return pd.DataFrame()
    rows = []
    for variant, g in confirm_df.groupby("risk_variant"):
        longs = g[g["setup_side"] == "long"]
        # Force compare: what if always 2 vs always 3 (ignoring elevated extension)
        n = len(longs)
        allowed2 = int(longs["entry_allowed_after_2"].map(_truthy).sum()) if n else 0
        allowed3 = int(longs["entry_allowed_after_3"].map(_truthy).sum()) if n else 0
        aborted_c2 = int(
            (
                (longs["abort_stage"] == "confirm_2")
                | ((longs["confirmation_candle_index"] == 2) & longs["confirmation_aborted"].map(_truthy))
            ).sum()
        ) if n else 0
        aborted_c3 = int(
            (
                (longs["abort_stage"] == "confirm_3")
                | ((longs["confirmation_candle_index"] == 3) & longs["confirmation_aborted"].map(_truthy))
            ).sum()
        ) if n else 0
        weak = longs[longs["long_quality"] == "weak"]
        good = longs[longs["long_quality"] == "good"]
        weak_saved_by_3 = 0
        good_delayed_by_3 = 0
        for _, r in longs.iterrows():
            # saved by 3: allowed after 2 conceptually but aborted at confirm_3 / not allowed after 2 with window=3
            if r.get("long_quality") == "weak" and not _truthy(r.get("entry_allowed_after_2")) and not _truthy(
                r.get("entry_allowed_after_3")
            ):
                if r.get("abort_stage") == "confirm_3":
                    weak_saved_by_3 += 1
            if r.get("long_quality") == "good" and int(r.get("confirmation_window_size") or 2) == 3:
                if _truthy(r.get("entry_allowed_after_3")) and not _truthy(r.get("entry_allowed_after_2")):
                    good_delayed_by_3 += 1
        rows.append(
            {
                "risk_variant": variant,
                "n_long_setups": n,
                "n_entry_allowed_after_2": allowed2,
                "n_entry_allowed_after_3": allowed3,
                "n_aborted_at_confirm_2": aborted_c2,
                "n_aborted_at_confirm_3": aborted_c3,
                "n_weak": int(len(weak)),
                "n_good": int(len(good)),
                "n_weak_aborted_at_confirm_3": weak_saved_by_3,
                "n_good_delayed_by_third_candle": good_delayed_by_3,
                "pct_elevated_window_3": float(
                    (longs["confirmation_window_size"] == 3).mean() * 100.0
                )
                if n
                else None,
            }
        )
    return pd.DataFrame(rows)


def variant_metrics(
    *,
    variant: str,
    timeline: pd.DataFrame,
    setup_cf: pd.DataFrame,
    confirm_df: pd.DataFrame,
    false_off: pd.DataFrame,
    missed: pd.DataFrame,
) -> dict[str, Any]:
    longs = setup_cf[setup_cf["setup_side"] == "long"] if len(setup_cf) else pd.DataFrame()
    blocked_risk = longs[longs["would_block_by_risk_off"].map(_truthy)] if len(longs) else pd.DataFrame()
    blocked_b3 = longs[longs["would_block_by_b3"].map(_truthy)] if len(longs) else pd.DataFrame()
    blocked_either = longs[
        longs["would_block_by_risk_off"].map(_truthy) | longs["would_block_by_b3"].map(_truthy)
    ] if len(longs) else pd.DataFrame()
    weak = longs[longs["long_quality"] == "weak"] if len(longs) else pd.DataFrame()
    good = longs[longs["long_quality"] == "good"] if len(longs) else pd.DataFrame()
    weak_blocked = blocked_either[blocked_either["long_quality"] == "weak"] if len(blocked_either) else pd.DataFrame()
    good_blocked = blocked_either[blocked_either["long_quality"] == "good"] if len(blocked_either) else pd.DataFrame()
    n_blocked = len(blocked_either)
    n_weak = len(weak)
    n_good = len(good)
    precision = (len(weak_blocked) / n_blocked) if n_blocked else None
    recall = (len(weak_blocked) / n_weak) if n_weak else None
    false_block = (len(good_blocked) / n_good) if n_good else None

    off_mask = timeline["risk_state"].map(_is_risk_off_long) if len(timeline) else pd.Series(dtype=bool)
    pct_time_off = float(off_mask.mean() * 100.0) if len(off_mask) else None
    elev_mask = timeline["risk_state"].map(_is_elevated_long) if len(timeline) else pd.Series(dtype=bool)
    changes = state_change_rows(timeline)
    runs = risk_off_periods(timeline)
    mean_hold = float(pd.Series([r["n_bars"] for r in runs]).mean()) if runs else None
    n_activations = len(runs)

    # B3 overlap: bars where risk_off AND b3 strong_bearish
    if len(timeline):
        both = (
            timeline["risk_state"].map(_is_risk_off_long)
            & timeline["b3_state"].map(_is_b3_strong_bearish)
        )
        overlap_pct = float(both.mean() * 100.0)
        risk_only = (
            timeline["risk_state"].map(_is_risk_off_long)
            & ~timeline["b3_state"].map(_is_b3_strong_bearish)
        )
        risk_only_pct = float(risk_only.mean() * 100.0)
    else:
        overlap_pct = None
        risk_only_pct = None

    focus = confirm_df[confirm_df["setup_id"].isin(FOCUS_SETUPS)] if len(confirm_df) else pd.DataFrame()
    focus_detail = {}
    for sid in FOCUS_SETUPS:
        hit = focus[focus["setup_id"] == sid]
        if hit.empty:
            focus_detail[sid] = None
            continue
        r = hit.iloc[0].to_dict()
        focus_detail[sid] = {
            "first_elevated": r.get("first_elevated_in_path"),
            "first_risk_off": r.get("first_risk_off_in_path"),
            "abort_stage": r.get("abort_stage"),
            "abort_reason": r.get("abort_reason"),
            "would_block_before_entry": r.get("would_block_before_entry"),
            "entry_allowed_after_2": r.get("entry_allowed_after_2"),
            "entry_allowed_after_3": r.get("entry_allowed_after_3"),
            "risk_state_at_setup": r.get("risk_state_at_setup"),
            "long_quality": r.get("long_quality"),
        }

    # Additional benefit vs B3 alone on focus: blocked by risk_off but not b3
    add_vs_b3 = 0
    if len(focus):
        add_vs_b3 = int(
            (
                focus["would_block_by_risk_off"].map(_truthy)
                & ~focus["would_block_by_strong_trend"].map(_truthy)
                & focus["would_block_before_entry"].map(_truthy)
            ).sum()
        )

    n_false_off = int(false_off["false_risk_off"].map(_truthy).sum()) if len(false_off) else 0
    n_missed = len(missed) if missed is not None else 0

    return {
        "risk_variant": variant,
        "n_long_setups": int(len(longs)),
        "n_long_blocked_by_risk_off": int(len(blocked_risk)),
        "n_long_blocked_by_b3": int(len(blocked_b3)),
        "n_long_blocked_by_either": int(n_blocked),
        "n_weak_longs": int(n_weak),
        "n_good_longs": int(n_good),
        "n_weak_longs_blocked": int(len(weak_blocked)),
        "n_good_longs_blocked": int(len(good_blocked)),
        "precision_blocked_are_weak": precision,
        "recall_weak_blocked": recall,
        "false_block_rate_good": false_block,
        "pct_time_long_risk_off": pct_time_off,
        "pct_time_long_risk_elevated": float(elev_mask.mean() * 100.0) if len(elev_mask) else None,
        "n_risk_off_activations": n_activations,
        "mean_hold_bars": mean_hold,
        "n_state_changes": int(len(changes)),
        "pct_overlap_risk_off_and_b3": overlap_pct,
        "pct_risk_off_without_b3": risk_only_pct,
        "n_false_risk_off_periods": n_false_off,
        "n_missed_adverse_moves": n_missed,
        "focus_additional_blocks_vs_b3_alone": add_vs_b3,
        "focus_setups": focus_detail,
    }


def target_setup_timeline(
    timelines: dict[str, pd.DataFrame],
    setups: pd.DataFrame,
    pa: pd.DataFrame,
    confirm_df: pd.DataFrame,
    primary: str = "R4",
) -> pd.DataFrame:
    """±60m around each focus setup; all variants, primary flagged."""
    s = setups.set_index("setup_id")
    pa_map = {}
    if len(pa):
        p = pa.copy()
        p["structure_break_timestamp"] = pd.to_datetime(p["structure_break_timestamp"], utc=True)
        pa_map = p.groupby("setup_id")["structure_break_timestamp"].first().to_dict()
    conf_map = {}
    if len(confirm_df):
        for _, r in confirm_df.iterrows():
            conf_map[(r["setup_id"], r["risk_variant"])] = r

    parts = []
    for sid in FOCUS_SETUPS:
        if sid not in s.index:
            continue
        setup_ts = _to_utc(s.loc[sid]["setup_activation_timestamp"])
        lo = setup_ts - pd.Timedelta(minutes=60)
        hi = setup_ts + pd.Timedelta(minutes=60)
        pa_ts = pa_map.get(sid)
        for variant, tl in timelines.items():
            sub = tl[(tl["decision_time"] >= lo) & (tl["decision_time"] <= hi)].copy()
            if sub.empty:
                continue
            conf = conf_map.get((sid, variant), {})
            c1 = conf.get("confirm_1_timestamp")
            c2 = conf.get("confirm_2_timestamp")
            c3 = conf.get("confirm_3_timestamp")

            def _stage(dt: pd.Timestamp) -> str:
                if abs((dt - setup_ts).total_seconds()) < 1:
                    return "setup"
                if pa_ts is not None and abs((dt - _to_utc(pa_ts)).total_seconds()) < 1:
                    return "pa"
                for label, raw in (("confirm_1", c1), ("confirm_2", c2), ("confirm_3", c3)):
                    if raw and abs((dt - _to_utc(raw)).total_seconds()) < 1:
                        return label
                return "path"

            def _cidx(dt: pd.Timestamp) -> int | None:
                for i, raw in enumerate((c1, c2, c3), start=1):
                    if raw and abs((dt - _to_utc(raw)).total_seconds()) < 1:
                        return i
                return None

            sub = sub.copy()
            sub["setup_id"] = sid
            sub["is_primary_variant"] = variant == primary
            sub["pipeline_stage"] = [_stage(d) for d in sub["decision_time"]]
            sub["confirmation_candle_index"] = [_cidx(d) for d in sub["decision_time"]]
            sub["combined_blocking_layer"] = sub.apply(
                lambda r: blocking_layer(r.get("risk_state"), r.get("b3_state"), "long"),
                axis=1,
            )
            parts.append(sub)
    return _concat(parts)


def risk_off_vs_b3_table(
    timelines: dict[str, pd.DataFrame],
    confirm_df: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for variant, tl in timelines.items():
        if tl.empty:
            continue
        off = tl["risk_state"].map(_is_risk_off_long)
        b3 = tl["b3_state"].map(_is_b3_strong_bearish)
        focus = confirm_df[confirm_df["risk_variant"] == variant] if len(confirm_df) else pd.DataFrame()
        focus = focus[focus["setup_id"].isin(FOCUS_SETUPS)] if len(focus) else focus
        rows.append(
            {
                "risk_variant": variant,
                "n_bars": int(len(tl)),
                "n_bars_risk_off": int(off.sum()),
                "n_bars_b3_strong_bearish": int(b3.sum()),
                "n_bars_both": int((off & b3).sum()),
                "n_bars_risk_off_only": int((off & ~b3).sum()),
                "n_bars_b3_only": int((~off & b3).sum()),
                "focus_n_blocked_risk_off_not_b3": int(
                    (
                        focus["would_block_by_risk_off"].map(_truthy)
                        & ~focus["would_block_by_strong_trend"].map(_truthy)
                    ).sum()
                )
                if len(focus)
                else 0,
                "focus_n_blocked_b3": int(focus["would_block_by_strong_trend"].map(_truthy).sum())
                if len(focus)
                else 0,
                "focus_n_blocked_either": int(focus["would_block_by_either"].map(_truthy).sum())
                if len(focus)
                else 0,
            }
        )
    return pd.DataFrame(rows)


def build_answers(
    metrics: dict[str, dict[str, Any]],
    confirm_df: pd.DataFrame,
    two_vs_three: pd.DataFrame,
) -> dict[str, str]:
    """Honest answers to the 26 closing questions — do not force a winner."""

    def focus_block(variant: str, sid: str) -> dict[str, Any]:
        m = metrics.get(variant, {}).get("focus_setups", {}) or {}
        return m.get(sid) or {}

    def any_variant_blocks(sid: str) -> list[str]:
        hits = []
        for v in VARIANTS:
            d = focus_block(v, sid)
            if d and _truthy(d.get("would_block_before_entry")):
                hits.append(v)
        return hits

    answers: dict[str, str] = {}
    for i, sid in enumerate(FOCUS_SETUPS, start=1):
        hits = any_variant_blocks(sid)
        detail = {v: focus_block(v, sid) for v in VARIANTS}
        if hits:
            answers[f"q{i}_causal_risk_off_before_{sid}"] = (
                f"Yes for variant(s) {hits}. Detail: "
                + "; ".join(
                    f"{v}: abort={detail[v].get('abort_stage')}/{detail[v].get('abort_reason')} "
                    f"off={detail[v].get('first_risk_off')} elev={detail[v].get('first_elevated')} "
                    f"setup_state={detail[v].get('risk_state_at_setup')}"
                    for v in VARIANTS
                )
            )
        else:
            answers[f"q{i}_causal_risk_off_before_{sid}"] = (
                "No variant produced a causal long risk-off / hard block before the planned "
                f"2–3 candle entry window for {sid}. "
                + "; ".join(
                    f"{v}: setup_state={detail[v].get('risk_state_at_setup')} "
                    f"abort={detail[v].get('abort_stage')}"
                    for v in VARIANTS
                    if detail[v]
                )
            )

    # Q6 trigger reasons across focus
    triggers = []
    if len(confirm_df):
        f = confirm_df[confirm_df["setup_id"].isin(FOCUS_SETUPS)]
        for _, r in f.iterrows():
            if r.get("abort_reason"):
                triggers.append(
                    f"{r['risk_variant']}/{r['setup_id']}: {r.get('abort_reason')} "
                    f"({r.get('entry_reason') or r.get('long_risk_reason') or ''})"
                )
    answers["q6_what_market_change_triggered"] = (
        "; ".join(triggers[:40]) if triggers else "No focus abort triggers observed."
    )

    # Earliest first_risk_off among focus
    earliest_v, earliest_ts = None, None
    for v, m in metrics.items():
        for sid, d in (m.get("focus_setups") or {}).items():
            if not d:
                continue
            ts = d.get("first_risk_off") or d.get("first_elevated")
            if not ts:
                continue
            t = _to_utc(ts)
            if earliest_ts is None or t < earliest_ts:
                earliest_ts, earliest_v = t, v
    answers["q7_earliest_variant"] = (
        f"{earliest_v} first elevated/off at {earliest_ts}" if earliest_v else "None of R1–R4 elevated/off on focus path."
    )

    # Lowest false-block rate
    best_fb, best_fb_v = None, None
    for v, m in metrics.items():
        fb = m.get("false_block_rate_good")
        if fb is None:
            continue
        if best_fb is None or fb < best_fb:
            best_fb, best_fb_v = fb, v
    answers["q8_lowest_false_block_rate"] = (
        f"{best_fb_v} ({best_fb})" if best_fb_v is not None else "Insufficient good-long coverage to rank."
    )

    # Additional benefit vs B3
    benefit = {v: m.get("focus_additional_blocks_vs_b3_alone", 0) for v, m in metrics.items()}
    answers["q9_benefit_vs_b3"] = (
        f"Focus setups additionally blocked by risk_off (not B3): {benefit}. "
        "B3 alone does not block morning focus longs; any risk_off block on 00055–00059 is incremental."
        if any(benefit.values())
        else f"No focus incremental blocks vs B3: {benefit}. Risk-Off did not add focus protection beyond B3 this week."
    )

    def setups_at_stage(stage_pred) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {v: [] for v in VARIANTS}
        if confirm_df.empty:
            return out
        f = confirm_df[confirm_df["setup_id"].isin(FOCUS_SETUPS)]
        for _, r in f.iterrows():
            if stage_pred(r):
                out[str(r["risk_variant"])].append(str(r["setup_id"]))
        return out

    answers["q10_blocked_at_setup"] = str(
        setups_at_stage(lambda r: r.get("abort_stage") == "setup")
    )
    answers["q11_blocked_at_pa"] = str(
        setups_at_stage(lambda r: r.get("abort_stage") == "pa")
    )
    answers["q12_blocked_at_confirm_1"] = str(
        setups_at_stage(lambda r: r.get("abort_stage") == "confirm_1")
    )
    answers["q13_blocked_at_confirm_2"] = str(
        setups_at_stage(lambda r: r.get("abort_stage") == "confirm_2")
    )
    answers["q14_blocked_at_confirm_3"] = str(
        setups_at_stage(lambda r: r.get("abort_stage") == "confirm_3")
    )

    # 2 vs 3 candles
    tvt = two_vs_three.to_dict(orient="records") if len(two_vs_three) else []
    answers["q15_is_two_candle_window_enough"] = (
        "Depends on variant; see two_vs_three table. "
        f"Summary: {tvt}. If few aborts occur at confirm_3, two candles may suffice for this week."
    )
    answers["q16_third_candle_measurable_benefit"] = (
        "Measurable only if weak setups abort at confirm_3 while goods are not broadly delayed. "
        f"Counts: {tvt}."
    )
    answers["q17_enter_after_2_when_neutral"] = (
        "Yes as research default: when risk_state is normal and B3 not strong_bearish, "
        "keep the existing 2-candle momentum confirmation (confirmation_window_size=2)."
    )
    answers["q18_require_third_when_elevated"] = (
        "Reasonable counterfactual rule: long_risk_elevated extends confirmation to 3 candles "
        "without hard-blocking. Validate that goods are not systematically delayed."
    )
    answers["q19_abort_entire_confirm_on_risk_off"] = (
        "Yes — long_risk_off / covered / B3 strong_bearish should abort the running confirmation "
        "immediately; do not reopen from the same sequence."
    )

    # When risk-off appears relative to PA on focus
    before_pa = after_pa = 0
    if len(confirm_df):
        f = confirm_df[confirm_df["setup_id"].isin(FOCUS_SETUPS)]
        for _, r in f.iterrows():
            fo = r.get("first_risk_off_in_path")
            pa = r.get("pa_structure_break_timestamp")
            if not fo:
                continue
            if pa and _to_utc(fo) <= _to_utc(pa):
                before_pa += 1
            else:
                after_pa += 1
    answers["q20_risk_off_before_pa_or_during_momentum"] = (
        f"Focus path first_risk_off counts: before_or_at_pa={before_pa}, after_pa_or_no_pa={after_pa}. "
        "Prefer detecting before entry; morning focus often has no PA (00056/57/59)."
    )
    answers["q21_elevated_only_tighten_or_block"] = (
        "Prefer tighten (3 candles / stricter momentum quality) rather than hard-block on elevated alone; "
        "hard-block reserved for long_risk_off."
    )
    answers["q22_only_risk_off_should_block"] = (
        "Yes for the Risk-Off layer: only long_risk_off (and covered) hard-block; "
        "elevated does not. B3 strong_bearish remains a separate hard block."
    )

    # R2 dedicated failed-breakout?
    r2 = metrics.get("R2", {})
    answers["q23_need_dedicated_failed_breakout_blocker"] = (
        f"R2 focus additional vs B3={r2.get('focus_additional_blocks_vs_b3_alone')}, "
        f"false_block={r2.get('false_block_rate_good')}, "
        f"missed={r2.get('n_missed_adverse_moves')}. "
        "If R2 uniquely catches focus failures without locking the week, a dedicated FBO filter is worth a follow-up; "
        "otherwise keep it as a score component inside R4."
    )

    # Weak momentum?
    answers["q24_weak_momentum_confirmation_root_cause"] = (
        "Possibly. Several focus longs lack PA confirmation entirely (00056/57/59); "
        "when PA exists, evaluate momentum_quality_* on confirm candles. "
        "If risk never elevates but candle quality falls, the weak link is confirmation quality, not Risk-Off."
    )

    # Recommend next variant — only if criteria met
    recommend = None
    recommend_notes = []
    for v, m in metrics.items():
        focus_hits = m.get("focus_additional_blocks_vs_b3_alone") or 0
        fb = m.get("false_block_rate_good")
        pct_off = m.get("pct_time_long_risk_off") or 0
        if focus_hits >= 1 and (fb is None or fb <= 0.5) and pct_off < 25:
            recommend_notes.append(
                f"{v}: focus_extra={focus_hits} false_block={fb} pct_off={pct_off}"
            )
            if recommend is None or pct_off < (metrics.get(recommend, {}).get("pct_time_long_risk_off") or 999):
                recommend = v
    if recommend:
        answers["q25_recommended_variant_for_next_test"] = (
            f"{recommend} tentatively for a pipeline counterfactual "
            f"(meets focus-block + not-locking-week heuristics). Candidates: {recommend_notes}."
        )
    else:
        answers["q25_recommended_variant_for_next_test"] = (
            "None. No R1–R4 variant clearly meets the decision rules "
            "(focus causal block + not locking normal regimes + incremental vs B3) on this week. "
            f"Raw: { {v: {k: metrics[v].get(k) for k in ('focus_additional_blocks_vs_b3_alone','false_block_rate_good','pct_time_long_risk_off','n_missed_adverse_moves')} for v in metrics} }"
        )

    # Pipeline weak stage
    focus_blocked_any = any(any_variant_blocks(sid) for sid in FOCUS_SETUPS)
    if not focus_blocked_any:
        answers["q26_pipeline_weak_stage_if_no_variant"] = (
            "Primary weakness appears upstream of Risk-Off: morning long setups activate under "
            "neutral/bullish-weak 15m context without a failed-breakout / session-distribution filter, "
            "and several focus IDs never reach PA. Next focus: setup quality + PA arming + confirmation "
            "candle quality — not forcing a Risk-Off latch that was not causally present."
        )
    else:
        answers["q26_pipeline_weak_stage_if_no_variant"] = (
            "At least one variant blocked some focus setups; still audit setup/PA/momentum stages "
            "for residual leaks and false blocks before production integration."
        )
    return answers


def write_readme(summary: dict[str, Any], out: Path) -> None:
    lines = [
        "# 5m Breakdown / Risk-Off Audit (March week 1)",
        "",
        "Research-only counterfactual. Separate from regime classification and from B3 Strong-Trend Gate. "
        "`RiskOffConfig.enabled` remains False by default; this audit does not mutate pipeline CSVs or live code.",
        "",
        f"Symbol: `{summary.get('symbol')}`",
        f"Window: `{summary.get('window_start')}` → `{summary.get('window_end')}`",
        f"History/warmup start: `{summary.get('history_start')}`",
        f"B3 strong_bearish reference: `{B3_REF_STRONG_BEARISH}`",
        "",
        "## Decision answers (26)",
        "",
    ]
    answers = summary.get("answers", {})
    for i, (k, v) in enumerate(answers.items(), 1):
        lines.append(f"{i}. **{k}:** {v}")
        lines.append("")
    lines.extend(["## Variant metrics (flat)", ""])
    for vname, m in (summary.get("variants") or {}).items():
        lines.append(f"### {vname}")
        lines.append("")
        for key in (
            "n_long_setups",
            "n_long_blocked_by_risk_off",
            "n_long_blocked_by_b3",
            "n_long_blocked_by_either",
            "precision_blocked_are_weak",
            "recall_weak_blocked",
            "false_block_rate_good",
            "pct_time_long_risk_off",
            "n_risk_off_activations",
            "mean_hold_bars",
            "n_state_changes",
            "pct_overlap_risk_off_and_b3",
            "pct_risk_off_without_b3",
            "n_false_risk_off_periods",
            "n_missed_adverse_moves",
            "focus_additional_blocks_vs_b3_alone",
        ):
            lines.append(f"- {key}: `{m.get(key)}`")
        lines.append("")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.pipeline_dir)
    setups = pd.read_csv(root / "setup_activations.csv")
    pa = pd.read_csv(root / "price_action_confirmations.csv")
    mom = pd.read_csv(root / "momentum_confirmations.csv")
    snapshots = pd.read_csv(root / "regime_snapshots.csv")
    drop = pd.read_csv(args.drop_csv) if Path(args.drop_csv).exists() else None
    forward = pd.read_csv(args.forward_csv) if Path(args.forward_csv).exists() else None
    drop_map, fwd_map = build_outcome_maps(drop, forward)

    b3_path = Path(args.b3_timeline_csv)
    b3_15m = pd.read_csv(b3_path)
    b3_15m = b3_15m[b3_15m["gate_variant"] == "B3"].copy()

    candles = load_symbol_candles(args.symbol)
    c5 = candles.copy()
    c5["timestamp"] = pd.to_datetime(c5["timestamp"], utc=True)
    hist = _to_utc(args.history_start)
    end = _to_utc(args.window_end)
    # Slice with warmup history
    c5 = c5[(c5["timestamp"] >= hist - pd.Timedelta(days=1)) & (c5["timestamp"] < end)].copy()
    c5["decision_time"] = c5["timestamp"] + pd.Timedelta(minutes=5)

    regime_by = build_regime_15m_by_decision(snapshots)
    decision_times = c5[
        (c5["decision_time"] >= hist) & (c5["decision_time"] < end)
    ]["decision_time"]
    b3_by = build_b3_by_decision(b3_15m, decision_times)

    pa_by_setup: dict[str, pd.Timestamp] = {}
    if len(pa):
        p = pa.copy()
        p["structure_break_timestamp"] = pd.to_datetime(p["structure_break_timestamp"], utc=True)
        for sid, g in p.groupby("setup_id"):
            pa_by_setup[str(sid)] = _to_utc(g["structure_break_timestamp"].iloc[0])

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    timeline_parts: list[pd.DataFrame] = []
    change_parts: list[pd.DataFrame] = []
    setup_parts: list[pd.DataFrame] = []
    pa_parts: list[pd.DataFrame] = []
    mom_parts: list[pd.DataFrame] = []
    confirm_parts: list[pd.DataFrame] = []
    false_parts: list[pd.DataFrame] = []
    missed_parts: list[pd.DataFrame] = []
    timelines: dict[str, pd.DataFrame] = {}
    metrics: dict[str, dict[str, Any]] = {}
    comparison_rows: list[dict[str, Any]] = []

    decision_index = pd.DatetimeIndex(
        sorted(c5[(c5["decision_time"] >= args.window_start) & (c5["decision_time"] < end)]["decision_time"])
    )

    for variant in VARIANTS:
        print(f"Running Risk-Off variant {variant}...")
        cfg = RiskOffConfig(enabled=True, variant=variant)
        tl = run_risk_off_timeline(
            c5,
            cfg,
            regime_15m_by_decision=regime_by,
            b3_by_decision=b3_by,
            start=args.window_start,
            end=args.window_end,
        )
        if not tl.empty:
            tl["decision_time"] = pd.to_datetime(tl["decision_time"], utc=True)
        timelines[variant] = tl
        timeline_parts.append(tl)
        change_parts.append(state_change_rows(tl))

        setup_cf = setup_counterfactual(
            setups, tl, drop_map, fwd_map, variant, args.window_start, args.window_end
        )
        setup_parts.append(setup_cf)
        pa_cf = event_counterfactual(
            pa, "structure_break_timestamp", tl, drop_map, variant, args.window_start, args.window_end
        )
        pa_parts.append(pa_cf)
        mom_cf = event_counterfactual(
            mom, "confirmation_timestamp", tl, drop_map, variant, args.window_start, args.window_end
        )
        mom_parts.append(mom_cf)

        # Confirmation windows for all long setups in window
        s = setups.copy()
        s["setup_activation_timestamp"] = pd.to_datetime(s["setup_activation_timestamp"], utc=True)
        s = s[
            (s["setup_activation_timestamp"] >= args.window_start)
            & (s["setup_activation_timestamp"] < args.window_end)
        ]
        if "setup_activated" in s.columns:
            s = s[s["setup_activated"].map(_truthy)]
        conf_rows = []
        for _, r in s.iterrows():
            if r.get("setup_side") != "long":
                continue
            sid = str(r["setup_id"])
            outcome = outcome_for_setup(sid, drop_map, fwd_map)
            conf_rows.append(
                evaluate_confirmation_window(
                    setup_id=sid,
                    side="long",
                    setup_ts=_to_utc(r["setup_activation_timestamp"]),
                    pa_ts=pa_by_setup.get(sid),
                    timeline=tl,
                    decision_index=decision_index,
                    variant=variant,
                    outcome=outcome,
                )
            )
        confirm_df = pd.DataFrame(conf_rows)
        confirm_parts.append(confirm_df)

        false_off = false_risk_off_table(tl)
        false_off["risk_variant"] = variant
        false_parts.append(false_off)
        missed = missed_adverse_table(confirm_df)
        missed_parts.append(missed)

        m = variant_metrics(
            variant=variant,
            timeline=tl,
            setup_cf=setup_cf,
            confirm_df=confirm_df,
            false_off=false_off,
            missed=missed,
        )
        metrics[variant] = m
        comparison_rows.append({k: v for k, v in m.items() if not isinstance(v, (dict, list))})

    # Write outputs
    timeline_all = _concat(timeline_parts)
    timeline_all.to_csv(out / "risk_off_timeline.csv", index=False)
    _concat(change_parts).to_csv(out / "risk_off_state_changes.csv", index=False)

    setups_all = _concat(setup_parts)
    setups_all.to_csv(out / "risk_off_setup_counterfactual.csv", index=False)
    _concat(pa_parts).to_csv(out / "risk_off_pa_counterfactual.csv", index=False)
    _concat(mom_parts).to_csv(out / "risk_off_momentum_counterfactual.csv", index=False)

    confirm_all = _concat(confirm_parts)
    confirm_all.to_csv(out / "risk_off_confirmation_windows.csv", index=False)
    confirm_all.to_csv(out / "risk_off_vs_outcomes.csv", index=False)

    pd.DataFrame(comparison_rows).to_csv(out / "risk_off_variant_comparison.csv", index=False)

    vs_b3 = risk_off_vs_b3_table(timelines, confirm_all)
    vs_b3.to_csv(out / "risk_off_vs_b3.csv", index=False)

    tgt_tl = target_setup_timeline(timelines, setups, pa, confirm_all, primary="R4")
    tgt_tl.to_csv(out / "target_setups_00055_00059_timeline.csv", index=False)

    focus_confirm = (
        confirm_all[confirm_all["setup_id"].isin(FOCUS_SETUPS)].copy() if len(confirm_all) else pd.DataFrame()
    )
    focus_confirm.to_csv(out / "target_setups_confirmation_analysis.csv", index=False)

    false_all = _concat(false_parts)
    if len(false_all) and "false_risk_off" in false_all.columns:
        false_all[false_all["false_risk_off"].map(_truthy)].to_csv(
            out / "false_risk_off_periods.csv", index=False
        )
    else:
        false_all.to_csv(out / "false_risk_off_periods.csv", index=False)

    missed_all = _concat(missed_parts)
    missed_all.to_csv(out / "missed_adverse_moves.csv", index=False)

    tvt = two_vs_three_comparison(confirm_all)
    tvt.to_csv(out / "two_vs_three_confirmation_comparison.csv", index=False)

    answers = build_answers(metrics, confirm_all, tvt)
    summary = {
        "symbol": args.symbol,
        "window_start": args.window_start,
        "window_end": args.window_end,
        "history_start": args.history_start,
        "pipeline_dir": str(root),
        "b3_timeline_csv": str(b3_path),
        "drop_csv": args.drop_csv if drop is not None else None,
        "forward_csv": args.forward_csv if forward is not None else None,
        "focus_setups": list(FOCUS_SETUPS),
        "b3_strong_bearish_ref": B3_REF_STRONG_BEARISH,
        "risk_off_default_enabled": False,
        "pipeline_untouched": True,
        "variants": {
            k: {
                kk: vv
                for kk, vv in v.items()
                if (not isinstance(vv, (dict, list))) or kk == "focus_setups"
            }
            for k, v in metrics.items()
        },
        "answers": answers,
        "safety": {
            "no_live_changes": True,
            "no_pipeline_csv_mutation": True,
            "outcomes_not_used_in_risk_state": True,
            "b3_gate_unchanged": True,
        },
    }
    (out / "audit_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2), encoding="utf-8"
    )
    write_readme(summary, out / "README.md")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--window-start", default="2026-03-01T00:00:00+00:00")
    p.add_argument("--window-end", default="2026-03-08T00:00:00+00:00")
    p.add_argument("--history-start", default="2026-02-15T00:00:00+00:00")
    p.add_argument(
        "--pipeline-dir",
        default="research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum",
    )
    p.add_argument(
        "--b3-timeline-csv",
        default="research/backtests/results/regime_scanner_direction_gate_audit_march_week1/direction_gate_timeline_15m.csv",
    )
    p.add_argument(
        "--drop-csv",
        default="research/backtests/results/regime_scanner_momentum_deepest_drop_recovery_march_week1/signal_deepest_drop_recovery.csv",
    )
    p.add_argument(
        "--forward-csv",
        default="research/backtests/results/regime_scanner_momentum_forward_audit_march_week1/signal_forward_outcomes.csv",
    )
    p.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_risk_off_audit_march_week1",
    )
    args = p.parse_args(argv)
    summary = run_audit(args)
    print("Wrote", args.output_dir)
    print("ANSWERS:")
    for k, v in (summary.get("answers") or {}).items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
