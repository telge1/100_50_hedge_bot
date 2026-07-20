"""C3.5D D1/D2 APTUSDT raw-data audit (research-only, descriptive).

Same APTUSDT 15m + closed-only C3.4B 4h HTF path as prior C3.5C/C3.4B/C3.4D audits.
No D3, no severity states, no D1/D2/C3.5C/C3.4B changes, no smoke overwrite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import (
    load_ohlcv_with_warmup,
    required_indicator_warmup_bars,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5c_c34b_4h_trend_audit import build_c34b_htf_frame
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    WARMUP_CALENDAR_DAYS,
    build_extended_tf_frame,
    discover_5m_span,
)
from research.regime_scanner.pullback_entry_c3_5d_continuation import (
    HTF_G1_SEMANTICS_DOC,
    ContinuationD1Config,
    apply_continuation_d1,
    config_hash as d1_config_hash,
    default_d1_config,
    htf_g1_blocks,
)
from research.regime_scanner.pullback_entry_c3_5d_post_entry import (
    DEFAULT_POST_ENTRY_HORIZON_BARS,
    PostEntryD2Config,
    apply_post_entry_telemetry,
    d2_semantics_doc,
)

SYMBOL = "APTUSDT"
TIMEFRAME = "15m"
HTF_TIMEFRAME = "4h"
BAR_MINUTES = 15
PHASE = "C3.5D_D1_D2_APT_RAW_AUDIT"
DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
FORWARD_HORIZONS = (1, 2, 3, 5, 8, 12, 24)
EVENT_COLS = (
    "breakout_level_lost_event",
    "breakout_level_reclaimed_event",
    "entry_pullback_extreme_broken_event",
    "entry_protected_level_broken_event",
    "micro_counter_bos_event",
    "ltf_major_alignment_lost_event",
    "htf_alignment_lost_event",
    "htf_major_flip_confirmed_event",
)
FORBIDDEN_SEVERITY = ("WARNING", "EARLY_FAILURE", "STRUCTURE_INVALIDATED")
SMOKE_NAMES = {
    "d1_entries.csv",
    "d2_post_entry_timeline.csv",
    "d2_fill_summary.csv",
    "d2_event_summary.csv",
    "d2_audit_summary.json",
}


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _safe_rate(n: int, d: int) -> float | None:
    return None if d <= 0 else float(n) / float(d)


def _median(xs: Sequence[Any]) -> float | None:
    vals = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    return float(np.median(vals)) if vals else None


def attach_closed_4h_major(frame15: pd.DataFrame, frame4h: pd.DataFrame) -> pd.DataFrame:
    left = frame15.copy()
    left["ltf_decision_time"] = pd.to_datetime(left["timestamp"], utc=True) + pd.Timedelta(
        minutes=BAR_MINUTES
    )
    if frame4h.empty or "htf_close_decision" not in frame4h.columns:
        left["htf_major_direction"] = 0
        return left.reset_index(drop=True)
    right = frame4h[["htf_close_decision", "major_direction"]].copy()
    right["htf_close_decision"] = pd.to_datetime(right["htf_close_decision"], utc=True)
    right = right.rename(columns={"major_direction": "htf_major_direction"})
    right = right.dropna(subset=["htf_close_decision"]).sort_values("htf_close_decision")
    merged = pd.merge_asof(
        left.sort_values("ltf_decision_time"),
        right,
        left_on="ltf_decision_time",
        right_on="htf_close_decision",
        direction="backward",
    )
    merged["htf_major_direction"] = (
        pd.to_numeric(merged["htf_major_direction"], errors="coerce").fillna(0).astype(int)
    )
    if "bar_index" in merged.columns:
        merged = merged.sort_values("bar_index")
    return merged.reset_index(drop=True)


def build_apt_d1_frame() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    inv = discover_5m_span(SYMBOL)
    if not inv.get("available"):
        raise RuntimeError(f"APTUSDT 5m data missing: {inv}")
    frame15, meta15 = build_extended_tf_frame(
        SYMBOL, timeframe=TIMEFRAME, warmup_calendar_days=WARMUP_CALENDAR_DAYS
    )
    if frame15.empty or not meta15.get("frame_ok"):
        raise RuntimeError(f"empty 15m frame: {meta15}")
    a0 = pd.Timestamp(meta15["analyze_start"])
    a1 = pd.Timestamp(meta15["analyze_end_exclusive"])
    warm_bars = max(required_indicator_warmup_bars(), 400)
    full_5m, warm_meta = load_ohlcv_with_warmup(
        SYMBOL, "5m", analyze_start=a0, analyze_end=a1, warmup_bars=warm_bars
    )
    if full_5m.empty:
        raise RuntimeError("empty 5m warmup load")
    decision = a1 + pd.Timedelta(hours=1)
    frame4h = build_c34b_htf_frame(
        full_5m, HTF_TIMEFRAME, decision=decision, analyze_start=a0, analyze_end_exclusive=a1
    )
    if frame4h.empty:
        raise RuntimeError("empty 4h C3.4B frame")
    frame = attach_closed_4h_major(frame15, frame4h)
    for c in ("arm_edge_internal_bull", "arm_edge_internal_bear", "new_micro_low", "new_micro_high"):
        if c not in frame.columns:
            frame[c] = False
    meta = {
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "htf_timeframe": HTF_TIMEFRAME,
        "data_source": inv.get("data_source"),
        "exchange": inv.get("exchange"),
        "data_start_5m": inv.get("data_start"),
        "data_end_5m": inv.get("data_end"),
        "n_5m_bars": inv.get("n_5m_bars"),
        "warmup_calendar_days": WARMUP_CALENDAR_DAYS,
        "warmup_5m_bars_requested": warm_bars,
        "warmup_meta": warm_meta if isinstance(warm_meta, dict) else {"info": str(warm_meta)},
        "analyze_start": meta15["analyze_start"],
        "analyze_end_exclusive": meta15["analyze_end_exclusive"],
        "analyze_end_inclusive_last_bar": meta15.get("analyze_end_inclusive_last_bar"),
        "n_analyze_bars_15m": int(len(frame)),
        "n_4h_bars": int(len(frame4h)),
        "htf_lookup": "closed_only merge_asof; usable iff htf_close_decision <= ltf_open+15m",
        "c34b_config": "C34B_MATRIX[0] protected_medium",
        "no_silent_fallback": True,
        "frame15_meta": meta15,
    }
    return frame, frame4h, meta


def run_integrity_guards(
    frame: pd.DataFrame,
    timeline_d1: pd.DataFrame,
    entries: Sequence[Mapping[str, Any]],
    lives: Sequence[Mapping[str, Any]],
    timeline_d2: pd.DataFrame,
    fill_sum: pd.DataFrame,
    *,
    horizon: int,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    failed = False

    def _add(name: str, ok: bool, **extra: Any) -> None:
        nonlocal failed
        if not ok:
            failed = True
        checks.append({"check": name, "ok": bool(ok), **extra})

    setup_ids = [int(e["setup_id"]) for e in entries]
    for e in entries:
        tb, fb = int(e["trigger_bar"]), int(e["fill_bar"])
        _add("fill_bar_eq_trigger_plus_1", fb == tb + 1, setup_id=int(e["setup_id"]), trigger_bar=tb, fill_bar=fb)
    _add("unique_setup_ids", len(setup_ids) == len(set(setup_ids)), n=len(setup_ids), n_unique=len(set(setup_ids)))
    trigs = [(int(e["trigger_bar"]), int(e["side"])) for e in entries]
    _add("one_fill_per_trigger_side", len(trigs) == len(set(trigs)), n=len(trigs), n_unique=len(set(trigs)))

    if not timeline_d2.empty and entries:
        bad = missing_fill = 0
        for e in entries:
            sid, tb, fb = int(e["setup_id"]), int(e["trigger_bar"]), int(e["fill_bar"])
            sub = timeline_d2[timeline_d2["setup_id"] == sid]
            if not sub.empty and (sub["bar_index"].astype(int) == tb).any():
                bad += 1
            if sub.empty or not (sub["bar_index"].astype(int) == fb).any():
                missing_fill += 1
        _add("no_trigger_bar_in_post_entry", bad == 0, n_violations=bad)
        _add("fill_bar_in_timeline", missing_fill == 0, n_missing=missing_fill)

    if not fill_sum.empty:
        len_ok = 0
        for _, r in fill_sum.iterrows():
            n = int(r.get("n_timeline_bars") or 0)
            reason = str(r.get("monitor_end_reason") or "")
            if reason == "horizon_reached" and n == horizon:
                len_ok += 1
            elif reason == "data_end" and 0 < n <= horizon:
                len_ok += 1
        _add("timeline_length_matches_end_reason", len_ok == len(fill_sum), n_ok=len_ok, n_fills=len(fill_sum))

    mono_mfe_bad = mono_mae_bad = frozen_bad = 0
    if not timeline_d2.empty:
        for sid, g in timeline_d2.groupby("setup_id"):
            g = g.sort_values("bars_since_fill")
            mfe = g["mfe_price"].astype(float).to_numpy()
            mae = g["mae_price"].astype(float).to_numpy()
            if len(mfe) >= 2 and np.any(np.diff(mfe) < -1e-12):
                mono_mfe_bad += 1
            if len(mae) >= 2 and np.any(np.diff(mae) > 1e-12):
                mono_mae_bad += 1
            for col in ("frozen_breakout_level", "frozen_atr_14", "setup_protected_level", "entry_protected_level"):
                if col in g.columns and g[col].nunique(dropna=False) > 1:
                    frozen_bad += 1
                    break
    _add("mfe_monotone_non_decreasing", mono_mfe_bad == 0, n_violations=mono_mfe_bad)
    _add("mae_monotone_non_increasing", mono_mae_bad == 0, n_violations=mono_mae_bad)
    _add("frozen_levels_immutable", frozen_bad == 0, n_violations=frozen_bad)

    mirror_ok = True
    if not timeline_d2.empty:
        for e in entries:
            sid, side, entry = int(e["setup_id"]), int(e["side"]), float(e["entry_price"])
            fb = int(e["fill_bar"])
            row = frame[frame["bar_index"] == fb]
            sub = timeline_d2[(timeline_d2["setup_id"] == sid) & (timeline_d2["bars_since_fill"] == 0)]
            if row.empty or sub.empty:
                continue
            hi, lo = _finite(row.iloc[0]["high"]), _finite(row.iloc[0]["low"])
            mfe0, mae0 = float(sub.iloc[0]["mfe_price"]), float(sub.iloc[0]["mae_price"])
            if side > 0:
                exp_mfe, exp_mae = max(0.0, hi - entry), min(0.0, lo - entry)
            else:
                exp_mfe, exp_mae = max(0.0, entry - lo), min(0.0, entry - hi)
            if abs(mfe0 - exp_mfe) > 1e-9 or abs(mae0 - exp_mae) > 1e-9:
                mirror_ok = False
                break
    _add("long_short_fill_bar_mfe_mae_mirror", mirror_ok)

    sev_hit = []
    for blob in (timeline_d1, timeline_d2, fill_sum):
        if blob is None or getattr(blob, "empty", True):
            continue
        cols = " ".join(map(str, blob.columns))
        for bad in FORBIDDEN_SEVERITY:
            if bad in cols:
                sev_hit.append(bad)
    _add("no_d3_severity_states", len(sev_hit) == 0, hits=sev_hit)

    htf_ok, htf_checked = True, 0
    if not timeline_d2.empty and "htf_major_direction" in frame.columns:
        for e in entries:
            fb, sid = int(e["fill_bar"]), int(e["setup_id"])
            fr = frame[frame["bar_index"] == fb]
            sub = timeline_d2[(timeline_d2["setup_id"] == sid) & (timeline_d2["bars_since_fill"] == 0)]
            if fr.empty or sub.empty:
                continue
            htf_checked += 1
            if int(fr.iloc[0]["htf_major_direction"]) != int(sub.iloc[0]["frozen_htf_major_at_fill"]):
                htf_ok = False
                break
    _add("htf_frozen_matches_closed_fill_bar", htf_ok, n_checked=htf_checked)

    overlap_bars = 0
    if not timeline_d2.empty:
        vc = timeline_d2.groupby("bar_index")["setup_id"].nunique()
        overlap_bars = int((vc > 1).sum())
    _add("parallel_monitors_supported", True, n_bars_with_gt1_active=overlap_bars)

    return {"passed": not failed, "n_checks": len(checks), "n_failed": sum(1 for c in checks if not c["ok"]), "checks": checks}


def build_entry_funnel(frame: pd.DataFrame, timeline: pd.DataFrame, cfg: ContinuationD1Config) -> pd.DataFrame:
    rows = []
    for side_name, side, begin_col, armed_ev, pb_state, ready_state in (
        ("long", 1, "pullback_begin_long", "long_continuation_armed", "LONG_PULLBACK", "LONG_READY"),
        ("short", -1, "pullback_begin_short", "short_continuation_armed", "SHORT_PULLBACK", "SHORT_READY"),
    ):
        maj = pd.to_numeric(frame["major_direction"], errors="coerce").fillna(0).astype(int)
        htf = pd.to_numeric(frame["htf_major_direction"], errors="coerce").fillna(0).astype(int)
        eligible = int((maj == side).sum())
        g1_ok = ~np.array([htf_g1_blocks(side, int(h)) for h in htf])
        eligible_g1 = int(((maj == side) & g1_ok).sum())
        begin_n = int(timeline[begin_col].fillna(False).astype(bool).sum()) if begin_col in timeline.columns else 0
        armed = int(timeline["events"].fillna("").astype(str).str.contains(armed_ev).sum())
        pullback = int((timeline["entry_state"] == pb_state).sum())
        ready = int((timeline["entry_state"] == ready_state).sum())
        trig = int(
            (
                timeline["entry_signal"].fillna(False).astype(bool)
                & (pd.to_numeric(timeline["entry_side"], errors="coerce") == side)
            ).sum()
        ) if "entry_side" in timeline.columns else 0
        rows.append({
            "direction": side_name,
            "eligible_major_bars": eligible,
            "eligible_major_and_htf_g1_bars": eligible_g1,
            "first_ema_band_touch_begin_flags": begin_n,
            "continuation_armed_events": armed,
            "bars_in_pullback_state": pullback,
            "bars_in_ready_state": ready,
            "triggers": trig,
            "fills": trig,
            "fill_per_eligible_major": _safe_rate(trig, eligible),
            "fill_per_armed": _safe_rate(trig, armed),
        })
    return pd.DataFrame(rows)


def build_pre_entry_invalidations(lives: Sequence[Mapping[str, Any]], timeline: pd.DataFrame) -> pd.DataFrame:
    mapped: Counter[str] = Counter()
    for life in lives:
        if life.get("entry_created"):
            continue
        r = str(life.get("terminal_reason") or "unknown")
        if r == "setup_protected_broken":
            mapped["setup_protected_broken"] += 1
        elif r == "htf_g1_blocked":
            mapped["htf_g1_blocked"] += 1
        elif r == "max_age":
            mapped["max_age"] += 1
        elif r in {"prior_swing_high_broken", "prior_swing_low_broken"}:
            mapped["prior_swing_break"] += 1
        elif r.startswith("ltf_major_flipped") or ("ltf" in r and "flip" in r):
            mapped["ltf_major_flip"] += 1
        else:
            mapped[r] += 1
    filt: Counter[str] = Counter()
    if not timeline.empty and "events" in timeline.columns:
        for s in timeline["events"].fillna("").astype(str):
            for part in str(s).split("|"):
                if part.startswith("break_rejected:"):
                    reason = part.split(":", 1)[1]
                    if "atr" in reason or "anti_chase" in reason:
                        filt["Anti-Chase-Reject"] += 1
                    else:
                        filt["Filter-Reject"] += 1
    n_abort = sum(mapped.values())
    rows = []
    for k, v in sorted(mapped.items(), key=lambda x: (-x[1], x[0])):
        rows.append({"reason": k, "count": v, "share_of_pre_entry_aborts": _safe_rate(v, n_abort), "category": "pre_entry_terminal"})
    for k, v in sorted(filt.items()):
        rows.append({"reason": k, "count": v, "share_of_pre_entry_aborts": None, "category": "break_reject_event"})
    return pd.DataFrame(rows)


def build_fills_table(
    frame: pd.DataFrame,
    entries: Sequence[Mapping[str, Any]],
    lives: Sequence[Mapping[str, Any]],
    fill_sum: pd.DataFrame,
    timeline_d2: pd.DataFrame,
) -> pd.DataFrame:
    life_by_id = {int(L["setup_id"]): L for L in lives}
    sum_by_id = {int(r["setup_id"]): r.to_dict() for _, r in fill_sum.iterrows()} if not fill_sum.empty else {}
    rows = []
    for e in entries:
        sid = int(e["setup_id"])
        side = int(e["side"])
        life = life_by_id.get(sid, {})
        sm = sum_by_id.get(sid, {})
        fb = int(e["fill_bar"])
        fr = frame[frame["bar_index"] == fb]
        fill_ts = fr.iloc[0]["timestamp"] if not fr.empty else None
        atr = _finite(e.get("frozen_atr_14_at_trigger"))
        ltf = htf = None
        if not timeline_d2.empty:
            sub0 = timeline_d2[(timeline_d2["setup_id"] == sid) & (timeline_d2["bars_since_fill"] == 0)]
            if not sub0.empty:
                if pd.notna(sub0.iloc[0].get("frozen_atr_14")):
                    atr = _finite(sub0.iloc[0]["frozen_atr_14"])
                htf = sub0.iloc[0].get("frozen_htf_major_at_fill")
                ltf = sub0.iloc[0].get("frozen_ltf_major_at_fill")
        entry = float(e["entry_price"])
        ema_dist_atr = brk_size_atr = float("nan")
        if not fr.empty:
            r0 = fr.iloc[0]
            e9, e20 = _finite(r0.get("ema_9")), _finite(r0.get("ema_20"))
            if math.isfinite(e9) and math.isfinite(e20) and atr > 0:
                mid = 0.5 * (min(e9, e20) + max(e9, e20))
                ema_dist_atr = (entry - mid) / atr if side > 0 else (mid - entry) / atr
            tb = int(e["trigger_bar"])
            tr = frame[frame["bar_index"] == tb]
            if not tr.empty and atr > 0:
                brk_size_atr = (_finite(tr.iloc[0]["high"]) - _finite(tr.iloc[0]["low"])) / atr
        pb_ext = e.get("frozen_pullback_low") if side > 0 else e.get("frozen_pullback_high")
        htf_i = int(htf or 0)
        rows.append({
            "setup_id": sid,
            "direction": e.get("direction"),
            "side": side,
            "trigger_bar": int(e["trigger_bar"]),
            "fill_bar": fb,
            "trigger_timestamp": e.get("trigger_timestamp"),
            "fill_timestamp": fill_ts,
            "entry_price": entry,
            "setup_protected_level": e.get("setup_protected_level"),
            "entry_protected_level": e.get("entry_protected_level"),
            "frozen_breakout_level": e.get("frozen_breakout_level"),
            "frozen_pullback_extreme": pb_ext,
            "frozen_atr_14": atr,
            "frozen_ltf_major_at_fill": ltf,
            "frozen_htf_major_at_fill": htf,
            "setup_age": life.get("setup_age") or e.get("setup_age"),
            "ready_age": life.get("ready_age") or e.get("ready_age"),
            "armed_bar": life.get("armed_bar"),
            "ready_bar": life.get("ready_bar"),
            "monitor_end_reason": sm.get("monitor_end_reason"),
            "entry_dist_ema_zone_atr": ema_dist_atr,
            "trigger_candle_range_atr": brk_size_atr,
            "htf_label": "bullish" if htf_i > 0 else ("bearish" if htf_i < 0 else "neutral"),
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out["fill_timestamp"] = pd.to_datetime(out["fill_timestamp"], utc=True)
        out = out.sort_values("fill_bar").reset_index(drop=True)
        out["bars_since_prev_fill"] = out["fill_bar"].diff()
        out["fill_month"] = out["fill_timestamp"].dt.to_period("M").astype(str)
        out["fill_week"] = out["fill_timestamp"].dt.to_period("W").astype(str)
    return out


def per_fill_forward(timeline_d2: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    if timeline_d2.empty or fills.empty:
        return pd.DataFrame()
    rows = []
    for _, f in fills.iterrows():
        sid = int(f["setup_id"])
        entry = float(f["entry_price"])
        atr = _finite(f.get("frozen_atr_14"))
        g = timeline_d2[timeline_d2["setup_id"] == sid].sort_values("bars_since_fill")
        if g.empty:
            continue
        first_pos = None
        for _, r in g.iterrows():
            scr = _finite(r.get("signed_close_return"))
            if math.isfinite(scr) and scr > 0:
                first_pos = int(r["bars_since_fill"])
                break
        t_mfe = int(g.loc[g["mfe_price"].astype(float).idxmax(), "bars_since_fill"])
        t_mae = int(g.loc[g["mae_price"].astype(float).idxmin(), "bars_since_fill"])
        hit_mfe05 = hit_mae05 = None
        for _, r in g.iterrows():
            bsf = int(r["bars_since_fill"])
            if atr > 0:
                if hit_mfe05 is None and float(r["mfe_price"]) / atr >= 0.5:
                    hit_mfe05 = bsf
                if hit_mae05 is None and float(r["mae_price"]) / atr <= -0.5:
                    hit_mae05 = bsf
        if hit_mfe05 is None and hit_mae05 is None:
            order = "neither"
        elif hit_mfe05 is not None and (hit_mae05 is None or hit_mfe05 <= hit_mae05):
            order = "mfe_first"
        elif hit_mae05 is not None and (hit_mfe05 is None or hit_mae05 < hit_mfe05):
            order = "mae_first"
        else:
            order = "neither"
        base = {
            "setup_id": sid,
            "direction": f["direction"],
            "bars_to_first_positive_close": first_pos,
            "bars_to_max_mfe": t_mfe,
            "bars_to_max_mae": t_mae,
            "mfe_before_mae_0_5atr": order,
            "final_mfe_price": float(g.iloc[-1]["mfe_price"]),
            "final_mae_price": float(g.iloc[-1]["mae_price"]),
        }
        for h in FORWARD_HORIZONS:
            target = h - 1
            sub = g[g["bars_since_fill"] <= target]
            if sub.empty:
                for k in (f"signed_close_ret_{h}", f"mfe_atr_{h}", f"mae_atr_{h}", f"mfe_pct_{h}", f"mae_pct_{h}"):
                    base[k] = float("nan")
                continue
            r = sub.iloc[-1]
            close_ret = _finite(r.get("signed_close_return"))
            mfe_p, mae_p = float(r["mfe_price"]), float(r["mae_price"])
            base[f"signed_close_ret_{h}"] = close_ret * 100.0 if math.isfinite(close_ret) else float("nan")
            base[f"mfe_atr_{h}"] = mfe_p / atr if atr > 0 else float("nan")
            base[f"mae_atr_{h}"] = mae_p / atr if atr > 0 else float("nan")
            base[f"mfe_pct_{h}"] = (mfe_p / entry) * 100.0 if entry else float("nan")
            base[f"mae_pct_{h}"] = (mae_p / entry) * 100.0 if entry else float("nan")
        mfe8, mae8, ret24 = base.get("mfe_atr_8"), base.get("mae_atr_8"), base.get("signed_close_ret_24")
        if math.isfinite(_finite(mfe8)):
            if mfe8 >= 1.0:
                base["outcome_A_followthrough"] = "strong_mfe8_ge_1atr"
            elif mfe8 >= 0.5:
                base["outcome_A_followthrough"] = "moderate_mfe8_ge_0_5atr"
            elif mfe8 < 0.25:
                base["outcome_A_followthrough"] = "no_early_ft_mfe8_lt_0_25atr"
            else:
                base["outcome_A_followthrough"] = "weak_mfe8_0_25_to_0_5atr"
        else:
            base["outcome_A_followthrough"] = "na"
        if math.isfinite(_finite(mae8)):
            if mae8 > -0.5:
                base["outcome_B_adverse"] = "low_mae8_gt_-0_5atr"
            elif mae8 <= -1.0:
                base["outcome_B_adverse"] = "high_mae8_le_-1atr"
            else:
                base["outcome_B_adverse"] = "mid_mae8"
        else:
            base["outcome_B_adverse"] = "na"
        base["outcome_C_order"] = order
        if math.isfinite(_finite(ret24)):
            base["outcome_D_ret24"] = "positive" if ret24 > 0 else ("negative" if ret24 < 0 else "flat")
        else:
            base["outcome_D_ret24"] = "na"
        rows.append(base)
    return pd.DataFrame(rows)


def summarize_forward(per_fill: pd.DataFrame) -> pd.DataFrame:
    if per_fill.empty:
        return pd.DataFrame()
    rows = []
    for h in FORWARD_HORIZONS:
        mfe = per_fill[f"mfe_atr_{h}"].astype(float)
        mae = per_fill[f"mae_atr_{h}"].astype(float)
        ret = per_fill[f"signed_close_ret_{h}"].astype(float)
        n = int(mfe.notna().sum())
        rows.append({
            "horizon_bars": h,
            "n": n,
            "mean_signed_close_ret_pct": float(ret.mean()) if n else None,
            "median_signed_close_ret_pct": float(ret.median()) if n else None,
            "mean_mfe_atr": float(mfe.mean()) if n else None,
            "median_mfe_atr": float(mfe.median()) if n else None,
            "mean_mae_atr": float(mae.mean()) if n else None,
            "median_mae_atr": float(mae.median()) if n else None,
            "mean_mfe_pct": float(per_fill[f"mfe_pct_{h}"].mean()) if n else None,
            "mean_mae_pct": float(per_fill[f"mae_pct_{h}"].mean()) if n else None,
            "share_mfe_gt_0_25atr": _safe_rate(int((mfe > 0.25).sum()), n),
            "share_mfe_gt_0_5atr": _safe_rate(int((mfe > 0.5).sum()), n),
            "share_mfe_gt_1_0atr": _safe_rate(int((mfe > 1.0).sum()), n),
            "share_mae_lt_-0_25atr": _safe_rate(int((mae < -0.25).sum()), n),
            "share_mae_lt_-0_5atr": _safe_rate(int((mae < -0.5).sum()), n),
            "share_mae_lt_-1_0atr": _safe_rate(int((mae < -1.0).sum()), n),
            "share_ret_positive": _safe_rate(int((ret > 0).sum()), n),
        })
    vc = per_fill["mfe_before_mae_0_5atr"].value_counts(dropna=False)
    for k, v in vc.items():
        rows.append({"horizon_bars": "order_0_5atr", "n": int(v), "order_class": str(k), "share": _safe_rate(int(v), len(per_fill))})
    # timing summaries
    rows.append({
        "horizon_bars": "timing",
        "median_bars_to_first_positive_close": _median(per_fill["bars_to_first_positive_close"]),
        "median_bars_to_max_mfe": _median(per_fill["bars_to_max_mfe"]),
        "median_bars_to_max_mae": _median(per_fill["bars_to_max_mae"]),
        "n": len(per_fill),
    })
    return pd.DataFrame(rows)


def event_rates_table(timeline_d2: pd.DataFrame, fills: pd.DataFrame, per_fill: pd.DataFrame) -> pd.DataFrame:
    if timeline_d2.empty or fills.empty:
        return pd.DataFrame()
    rows = []
    n_fills = len(fills)
    for ev in EVENT_COLS:
        if ev not in timeline_d2.columns:
            continue
        hit_ids = set(timeline_d2.loc[timeline_d2[ev].fillna(False).astype(bool), "setup_id"].astype(int).unique())
        for direction in ("all", "long", "short"):
            dens = n_fills
            ids = hit_ids
            if direction != "all":
                d_ids = set(fills.loc[fills["direction"] == direction, "setup_id"].astype(int))
                ids = hit_ids & d_ids
                dens = int((fills["direction"] == direction).sum())
            bars_to, mfe_before, mae_before, mfe_after, mae_after = [], [], [], [], []
            recover = reclaim = ret24_pos = ret24_n = 0
            for sid in ids:
                g = timeline_d2[timeline_d2["setup_id"] == sid].sort_values("bars_since_fill")
                ev_rows = g[g[ev].fillna(False).astype(bool)]
                if ev_rows.empty:
                    continue
                bsf = int(ev_rows.iloc[0]["bars_since_fill"])
                bars_to.append(bsf)
                atr = _finite(g.iloc[0].get("frozen_atr_14"))
                before = g[g["bars_since_fill"] <= bsf]
                after = g[g["bars_since_fill"] >= bsf]
                if atr > 0 and not before.empty:
                    mfe_before.append(float(before.iloc[-1]["mfe_price"]) / atr)
                    mae_before.append(float(before.iloc[-1]["mae_price"]) / atr)
                if atr > 0 and not after.empty:
                    mfe_after.append(float(after["mfe_price"].astype(float).max()) / atr)
                    mae_after.append(float(after["mae_price"].astype(float).min()) / atr)
                if (after["signed_close_return"].astype(float) > 0).any():
                    recover += 1
                if ev == "breakout_level_lost_event":
                    if g.loc[g["bars_since_fill"] > bsf, "breakout_level_reclaimed_event"].fillna(False).astype(bool).any():
                        reclaim += 1
                pf = per_fill[per_fill["setup_id"] == sid]
                if not pf.empty and math.isfinite(_finite(pf.iloc[0].get("signed_close_ret_24"))):
                    ret24_n += 1
                    if float(pf.iloc[0]["signed_close_ret_24"]) > 0:
                        ret24_pos += 1
            rows.append({
                "event": ev,
                "direction": direction,
                "n_fills_with_event": len(ids),
                "share_of_fills": _safe_rate(len(ids), dens),
                "median_bars_to_event": _median(bars_to),
                "median_mfe_atr_at_event": _median(mfe_before),
                "median_mae_atr_at_event": _median(mae_before),
                "median_max_mfe_atr_after": _median(mfe_after),
                "median_min_mae_atr_after": _median(mae_after),
                "share_recover_over_entry_after": _safe_rate(recover, len(ids)),
                "share_reclaim_after_lost": _safe_rate(reclaim, len(ids)) if ev == "breakout_level_lost_event" else None,
                "share_ret24_positive": _safe_rate(ret24_pos, ret24_n),
            })
    return pd.DataFrame(rows)


def event_paths_table(timeline_d2: pd.DataFrame) -> pd.DataFrame:
    if timeline_d2.empty:
        return pd.DataFrame()
    rows = []
    for sid, g in timeline_d2.groupby("setup_id"):
        g = g.sort_values("bars_since_fill")
        path = []
        for ev in EVENT_COLS:
            if ev not in g.columns:
                continue
            hit = g[g[ev].fillna(False).astype(bool)]
            if not hit.empty:
                path.append((int(hit.iloc[0]["bars_since_fill"]), ev.replace("_event", "")))
        path.sort()
        rows.append({
            "setup_id": int(sid),
            "direction": g.iloc[0]["direction"],
            "event_path": " -> ".join(f"{b}:{n}" for b, n in path) if path else "",
            "n_events": len(path),
        })
    return pd.DataFrame(rows)


def sequence_summary(timeline_d2: pd.DataFrame, per_fill: pd.DataFrame) -> pd.DataFrame:
    seqs = [
        ("breakout_level_lost_event", "breakout_level_reclaimed_event", "lost_then_reclaim"),
        ("breakout_level_lost_event", "entry_pullback_extreme_broken_event", "lost_then_pb_extreme"),
        ("breakout_level_lost_event", "entry_protected_level_broken_event", "lost_then_protected"),
        ("micro_counter_bos_event", "breakout_level_lost_event", "micro_then_lost"),
        ("micro_counter_bos_event", "entry_pullback_extreme_broken_event", "micro_then_pb"),
        ("ltf_major_alignment_lost_event", "entry_protected_level_broken_event", "ltf_lost_then_protected"),
        ("htf_alignment_lost_event", "htf_major_flip_confirmed_event", "htf_align_lost_then_flip"),
    ]
    if timeline_d2.empty:
        return pd.DataFrame()
    rows = []
    setups = timeline_d2["setup_id"].astype(int).unique()
    for a, b, name in seqs:
        gaps, mfe_before, mae_before, mfe_after, mae_after = [], [], [], [], []
        n = recover = ret24_pos = ret24_n = 0
        for sid in setups:
            g = timeline_d2[timeline_d2["setup_id"] == int(sid)].sort_values("bars_since_fill")
            if a not in g.columns or b not in g.columns:
                continue
            ha = g[g[a].fillna(False).astype(bool)]
            hb = g[g[b].fillna(False).astype(bool)]
            if ha.empty or hb.empty:
                continue
            ba, bb = int(ha.iloc[0]["bars_since_fill"]), int(hb.iloc[0]["bars_since_fill"])
            if bb < ba:
                continue
            n += 1
            gaps.append(bb - ba)
            atr = _finite(g.iloc[0].get("frozen_atr_14"))
            before = g[g["bars_since_fill"] <= ba]
            after = g[g["bars_since_fill"] >= bb]
            if atr > 0 and not before.empty:
                mfe_before.append(float(before.iloc[-1]["mfe_price"]) / atr)
                mae_before.append(float(before.iloc[-1]["mae_price"]) / atr)
            if atr > 0 and not after.empty:
                mfe_after.append(float(after["mfe_price"].max()) / atr)
                mae_after.append(float(after["mae_price"].min()) / atr)
            if (g.loc[g["bars_since_fill"] >= bb, "signed_close_return"].astype(float) > 0).any():
                recover += 1
            pf = per_fill[per_fill["setup_id"] == int(sid)]
            if not pf.empty and math.isfinite(_finite(pf.iloc[0].get("signed_close_ret_24"))):
                ret24_n += 1
                if float(pf.iloc[0]["signed_close_ret_24"]) > 0:
                    ret24_pos += 1
        rows.append({
            "sequence": name, "event_a": a, "event_b": b, "n": n,
            "share_of_fills": _safe_rate(n, len(setups)),
            "median_gap_bars": _median(gaps),
            "median_mfe_atr_before_a": _median(mfe_before),
            "median_mae_atr_before_a": _median(mae_before),
            "median_max_mfe_atr_after_b": _median(mfe_after),
            "median_min_mae_atr_after_b": _median(mae_after),
            "share_recover_after": _safe_rate(recover, n),
            "share_ret24_positive": _safe_rate(ret24_pos, ret24_n),
        })
    return pd.DataFrame(rows)


def _first_bar_condition(g: pd.DataFrame, cond_fn) -> int | None:
    for _, r in g.iterrows():
        if cond_fn(r):
            return int(r["bars_since_fill"])
    return None


def raw_condition_recovery(timeline_d2: pd.DataFrame, per_fill: pd.DataFrame) -> pd.DataFrame:
    if timeline_d2.empty:
        return pd.DataFrame()

    def mae_thr(thr: float):
        return lambda r: _finite(r.get("mae_atr")) <= thr and math.isfinite(_finite(r.get("mae_atr")))

    def uw(n: int):
        return lambda r: int(r.get("underwater_bars_consecutive") or 0) >= n

    conditions = [
        ("breakout_level_lost", lambda r: bool(r.get("breakout_level_lost_event"))),
        ("micro_counter_bos", lambda r: bool(r.get("micro_counter_bos_event"))),
        ("ltf_alignment_lost", lambda r: bool(r.get("ltf_major_alignment_lost_event"))),
        ("htf_alignment_lost", lambda r: bool(r.get("htf_alignment_lost_event"))),
        ("pullback_extreme_broken", lambda r: bool(r.get("entry_pullback_extreme_broken_event"))),
        ("entry_protected_broken", lambda r: bool(r.get("entry_protected_level_broken_event"))),
        ("mae_le_-0_25atr", mae_thr(-0.25)),
        ("mae_le_-0_5atr", mae_thr(-0.5)),
        ("mae_le_-0_75atr", mae_thr(-0.75)),
        ("mae_le_-1_0atr", mae_thr(-1.0)),
        ("underwater_consec_ge_2", uw(2)),
        ("underwater_consec_ge_3", uw(3)),
        ("underwater_consec_ge_5", uw(5)),
        ("underwater_consec_ge_8", uw(8)),
    ]
    rows = []
    setups = list(timeline_d2["setup_id"].astype(int).unique())
    n_all = len(setups)
    for name, fn in conditions:
        bars, further_mae = [], []
        n = recover = mfe05 = mfe10 = ret24_pos = ret24_n = 0
        for sid in setups:
            g = timeline_d2[timeline_d2["setup_id"] == sid].sort_values("bars_since_fill")
            b = _first_bar_condition(g, fn)
            if b is None:
                continue
            n += 1
            bars.append(b)
            after = g[g["bars_since_fill"] >= b]
            atr = _finite(g.iloc[0].get("frozen_atr_14"))
            if (after["signed_close_return"].astype(float) > 0).any():
                recover += 1
            if atr > 0:
                max_mfe_after = float(after["mfe_price"].max()) / atr
                min_mae_after = float(after["mae_price"].min()) / atr
                mae_at = float(g[g["bars_since_fill"] == b].iloc[0]["mae_price"]) / atr
                further_mae.append(min_mae_after - mae_at)
                if max_mfe_after >= 0.5:
                    mfe05 += 1
                if max_mfe_after >= 1.0:
                    mfe10 += 1
            pf = per_fill[per_fill["setup_id"] == sid]
            if not pf.empty and math.isfinite(_finite(pf.iloc[0].get("signed_close_ret_24"))):
                ret24_n += 1
                if float(pf.iloc[0]["signed_close_ret_24"]) > 0:
                    ret24_pos += 1
        rows.append({
            "condition": name,
            "n_fills": n,
            "share_of_fills": _safe_rate(n, n_all),
            "median_first_bar": _median(bars),
            "share_later_close_above_entry": _safe_rate(recover, n),
            "share_later_mfe_ge_0_5atr": _safe_rate(mfe05, n),
            "share_later_mfe_ge_1_0atr": _safe_rate(mfe10, n),
            "median_further_mae_atr_after": _median(further_mae),
            "share_ret24_positive": _safe_rate(ret24_pos, ret24_n),
            "note": "underwater isolated observation only" if name.startswith("underwater") else "",
        })
    return pd.DataFrame(rows)


def combination_recovery(timeline_d2: pd.DataFrame, per_fill: pd.DataFrame) -> pd.DataFrame:
    if timeline_d2.empty:
        return pd.DataFrame()

    def has_event(g, col):
        return col in g.columns and g[col].fillna(False).astype(bool).any()

    def first_bsf(g, col):
        hit = g[g[col].fillna(False).astype(bool)]
        return int(hit.iloc[0]["bars_since_fill"]) if not hit.empty else None

    def lost_no_reclaim_2(g):
        if not has_event(g, "breakout_level_lost_event"):
            return False
        b = first_bsf(g, "breakout_level_lost_event")
        window = g[(g["bars_since_fill"] > b) & (g["bars_since_fill"] <= b + 2)]
        return not window["breakout_level_reclaimed_event"].fillna(False).astype(bool).any()

    def lost_and_mae05(g):
        if not has_event(g, "breakout_level_lost_event"):
            return False
        b = first_bsf(g, "breakout_level_lost_event")
        row = g[g["bars_since_fill"] == b].iloc[0]
        return _finite(row.get("mae_atr")) <= -0.5

    def no_mfe_and_lost(g):
        early = g[g["bars_since_fill"] <= 2]
        atr = _finite(g.iloc[0].get("frozen_atr_14"))
        if atr <= 0 or early.empty:
            return False
        if float(early["mfe_price"].max()) / atr >= 0.25:
            return False
        return has_event(g, "breakout_level_lost_event")

    combos = [
        ("breakout_lost_AND_micro_bos", lambda g: has_event(g, "breakout_level_lost_event") and has_event(g, "micro_counter_bos_event")),
        ("breakout_lost_AND_no_reclaim_within_2", lost_no_reclaim_2),
        ("breakout_lost_AND_mae_le_-0_5atr", lost_and_mae05),
        ("no_mfe_0_25atr_in_3bars_AND_breakout_lost", no_mfe_and_lost),
        ("pb_extreme_broken_AND_ltf_align_lost", lambda g: has_event(g, "entry_pullback_extreme_broken_event") and has_event(g, "ltf_major_alignment_lost_event")),
        ("entry_protected_broken_AND_ltf_against", lambda g: has_event(g, "entry_protected_level_broken_event") and has_event(g, "ltf_major_alignment_lost_event")),
        ("htf_align_lost_AND_ltf_align_lost", lambda g: has_event(g, "htf_alignment_lost_event") and has_event(g, "ltf_major_alignment_lost_event")),
    ]
    rows = []
    setups = list(timeline_d2.groupby("setup_id"))
    n_all = len(setups)
    for name, pred in combos:
        n = recover = ret24_pos = ret24_n = long_n = short_n = long_pos = short_pos = 0
        times, mfe_after, mae_after = [], [], []
        for sid, g in setups:
            g = g.sort_values("bars_since_fill")
            if not pred(g):
                continue
            n += 1
            direction = str(g.iloc[0]["direction"])
            if direction == "long":
                long_n += 1
            else:
                short_n += 1
            t = None
            for col in EVENT_COLS:
                if col in g.columns and g[col].fillna(False).astype(bool).any():
                    t0 = first_bsf(g, col)
                    if t0 is not None:
                        t = t0 if t is None else min(t, t0)
            if t is not None:
                times.append(t)
            atr = _finite(g.iloc[0].get("frozen_atr_14"))
            after = g if t is None else g[g["bars_since_fill"] >= t]
            if (after["signed_close_return"].astype(float) > 0).any():
                recover += 1
            if atr > 0 and not after.empty:
                mfe_after.append(float(after["mfe_price"].max()) / atr)
                mae_after.append(float(after["mae_price"].min()) / atr)
            pf = per_fill[per_fill["setup_id"] == int(sid)]
            if not pf.empty and math.isfinite(_finite(pf.iloc[0].get("signed_close_ret_24"))):
                ret24_n += 1
                pos = float(pf.iloc[0]["signed_close_ret_24"]) > 0
                if pos:
                    ret24_pos += 1
                if direction == "long":
                    long_pos += int(pos)
                else:
                    short_pos += int(pos)
        rows.append({
            "combination": name,
            "n": n,
            "coverage": _safe_rate(n, n_all),
            "median_first_related_bar": _median(times),
            "share_later_close_above_entry": _safe_rate(recover, n),
            "median_max_mfe_atr_after": _median(mfe_after),
            "median_min_mae_atr_after": _median(mae_after),
            "share_ret24_positive": _safe_rate(ret24_pos, ret24_n),
            "n_long": long_n,
            "n_short": short_n,
            "share_ret24_pos_long": _safe_rate(long_pos, long_n),
            "share_ret24_pos_short": _safe_rate(short_pos, short_n),
        })
    return pd.DataFrame(rows)


def htf_guard_comparison(fills: pd.DataFrame, per_fill: pd.DataFrame, lives: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame()
    merged = fills.merge(per_fill, on=["setup_id", "direction"], how="left")

    def aligned(row) -> bool:
        return int(row.get("frozen_htf_major_at_fill") or 0) == int(row["side"])

    def neutral(row) -> bool:
        return int(row.get("frozen_htf_major_at_fill") or 0) == 0

    g1, g2, g1_neutral = merged, merged[merged.apply(aligned, axis=1)], merged[merged.apply(neutral, axis=1)]

    def pack(label: str, df: pd.DataFrame) -> dict[str, Any]:
        n = len(df)
        if n == 0:
            return {"variant": label, "n_fills": 0}
        return {
            "variant": label,
            "n_fills": n,
            "n_long": int((df["direction"] == "long").sum()),
            "n_short": int((df["direction"] == "short").sum()),
            "median_mfe_atr_8": float(df["mfe_atr_8"].median()) if "mfe_atr_8" in df else None,
            "median_mae_atr_8": float(df["mae_atr_8"].median()) if "mae_atr_8" in df else None,
            "median_mfe_atr_24": float(df["mfe_atr_24"].median()) if "mfe_atr_24" in df else None,
            "median_mae_atr_24": float(df["mae_atr_24"].median()) if "mae_atr_24" in df else None,
            "share_strong_ft_mfe8_ge_1": _safe_rate(int((df["mfe_atr_8"] >= 1.0).sum()), n) if "mfe_atr_8" in df else None,
            "share_no_early_ft_mfe8_lt_0_25": _safe_rate(int((df["mfe_atr_8"] < 0.25).sum()), n) if "mfe_atr_8" in df else None,
            "share_ret24_positive": _safe_rate(int((df["signed_close_ret_24"] > 0).sum()), n) if "signed_close_ret_24" in df else None,
            "share_high_mae8_le_-1": _safe_rate(int((df["mae_atr_8"] <= -1.0).sum()), n) if "mae_atr_8" in df else None,
        }

    rows = [pack("G1_permissive_actual_fills", g1), pack("G2_strict_aligned_subset", g2), pack("G1_only_neutral_htf_at_fill", g1_neutral)]
    n_arm = len(lives)
    n_arm_g2 = 0
    for L in lives:
        htf = int(L.get("htf_major_at_arm") or 0)
        side = 1 if str(L.get("direction") or "") == "long" else -1
        if htf == side:
            n_arm_g2 += 1
    rows.append({
        "variant": "arm_counts",
        "n_arms_g1": n_arm,
        "n_arms_would_pass_g2_at_arm": n_arm_g2,
        "share_arms_g2": _safe_rate(n_arm_g2, n_arm),
        "n_fills_g1": len(g1),
        "n_fills_g2_subset": len(g2),
        "share_fills_neutral_htf": _safe_rate(len(g1_neutral), len(g1)),
    })
    return pd.DataFrame(rows)


def fill_bar_behavior(fills: pd.DataFrame, timeline_d2: pd.DataFrame, lives: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    rows = []
    ready_on_first = 0
    gaps_arm_ready, gaps_ready_trig, gaps_arm_fill = [], [], []
    for L in lives:
        if not L.get("entry_created"):
            continue
        ab, rb, tb, fb = L.get("armed_bar"), L.get("ready_bar"), L.get("trigger_bar"), L.get("fill_bar")
        if ab is not None and rb is not None:
            gaps_arm_ready.append(int(rb) - int(ab))
            if int(rb) - int(ab) <= 1:
                ready_on_first += 1
        if rb is not None and tb is not None:
            gaps_ready_trig.append(int(tb) - int(rb))
        if ab is not None and fb is not None:
            gaps_arm_fill.append(int(fb) - int(ab))
    n_entered = sum(1 for L in lives if L.get("entry_created"))
    rows.append({
        "metric": "ready_within_1_bar_of_arm",
        "value": ready_on_first,
        "share": _safe_rate(ready_on_first, n_entered),
        "median_arm_to_ready": _median(gaps_arm_ready),
        "median_ready_to_trigger": _median(gaps_ready_trig),
        "median_arm_to_fill": _median(gaps_arm_fill),
    })
    if timeline_d2.empty or fills.empty:
        return pd.DataFrame(rows)
    n = len(fills)
    lost0 = mae025 = mae05 = mae10 = both = 0
    trig_ranges = []
    for _, f in fills.iterrows():
        sid = int(f["setup_id"])
        g0 = timeline_d2[(timeline_d2["setup_id"] == sid) & (timeline_d2["bars_since_fill"] == 0)]
        if g0.empty:
            continue
        r = g0.iloc[0]
        if bool(r.get("breakout_level_is_lost")) or bool(r.get("breakout_level_lost_event")):
            lost0 += 1
        mae, mfe = _finite(r.get("mae_atr")), _finite(r.get("mfe_atr"))
        if mae <= -0.25:
            mae025 += 1
        if mae <= -0.5:
            mae05 += 1
        if mae <= -1.0:
            mae10 += 1
        if mfe > 0 and mae <= -0.5:
            both += 1
        if math.isfinite(_finite(f.get("trigger_candle_range_atr"))):
            trig_ranges.append(float(f["trigger_candle_range_atr"]))
    rows.extend([
        {"metric": "fill_bar_breakout_already_lost", "value": lost0, "share": _safe_rate(lost0, n)},
        {"metric": "fill_bar_mae_le_-0_25atr", "value": mae025, "share": _safe_rate(mae025, n)},
        {"metric": "fill_bar_mae_le_-0_5atr", "value": mae05, "share": _safe_rate(mae05, n)},
        {"metric": "fill_bar_mae_le_-1_0atr", "value": mae10, "share": _safe_rate(mae10, n)},
        {"metric": "fill_bar_pos_mfe_and_mae_le_-0_5atr", "value": both, "share": _safe_rate(both, n)},
        {"metric": "trigger_candle_range_atr", "median": _median(trig_ranges), "mean": float(np.mean(trig_ranges)) if trig_ranges else None},
    ])
    return pd.DataFrame(rows)


def write_readme(path: Path, summary: Mapping[str, Any]) -> None:
    lines = [
        "# C3.5D D1/D2 APTUSDT Raw-Data Audit",
        "",
        "Research-only descriptive audit. No D3 classification. No severity states.",
        "",
        "## Data",
        f"- Symbol: `{summary.get('symbol')}`",
        f"- LTF: `{summary.get('timeframe')}` · HTF: `{summary.get('htf_timeframe')}` (closed-only)",
        f"- Source: `{summary.get('data_source')}`",
        f"- Analyze: `{summary.get('analyze_start')}` → `{summary.get('analyze_end_exclusive')}`",
        f"- Bars: `{summary.get('n_analyze_bars_15m')}` · Warmup calendar days: `{summary.get('warmup_calendar_days')}`",
        f"- `post_entry_horizon_bars`: `{summary.get('post_entry_horizon_bars')}`",
        "",
        "## Integrity",
        f"- Passed: `{summary.get('integrity_passed')}`",
        f"- Checks failed: `{summary.get('integrity_n_failed')}`",
        "",
        "## Fills",
        f"- N fills: `{summary.get('n_fills')}` (long `{summary.get('n_long')}` / short `{summary.get('n_short')}`)",
        "",
        "## Notes",
        "- Smoke CSVs in parent folder were not overwritten.",
        "- Outcome groups A–D are descriptive only, not D3 rules.",
        "- `underwater_bars` observed in isolation only.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(*, output_dir: Path = DEFAULT_OUT, horizon: int = DEFAULT_POST_ENTRY_HORIZON_BARS) -> dict[str, Any]:
    output_dir = Path(output_dir)
    parent = output_dir.parent
    if output_dir.resolve() == parent.resolve():
        raise RuntimeError("refusing to write into smoke parent dir directly")
    output_dir.mkdir(parents=True, exist_ok=True)

    frame, frame4h, data_meta = build_apt_d1_frame()
    d1_cfg = default_d1_config()
    d2_cfg = PostEntryD2Config(post_entry_horizon_bars=horizon)

    tl1, entries, lives = apply_continuation_d1(frame, d1_cfg, return_lifecycles=True)
    tl2, fill_sum, event_sum = apply_post_entry_telemetry(frame, entries, cfg=d2_cfg)

    integrity = run_integrity_guards(frame, tl1, entries, lives, tl2, fill_sum, horizon=horizon)
    (output_dir / "integrity_guards.json").write_text(json.dumps(json_safe(integrity), indent=2) + "\n", encoding="utf-8")

    cfg_doc = {
        **{k: data_meta[k] for k in data_meta if k != "frame15_meta"},
        "phase": PHASE,
        "d1_config": d1_cfg.to_dict(),
        "d1_config_hash": d1_config_hash(d1_cfg),
        "d2_config": d2_cfg.to_dict(),
        "d2_semantics": d2_semantics_doc(),
        "htf_g1": dict(HTF_G1_SEMANTICS_DOC),
        "post_entry_horizon_bars": horizon,
        "forward_horizons": list(FORWARD_HORIZONS),
        "no_d3": True,
        "no_severity_states": True,
        "no_pine": True,
        "no_live_bot": True,
        "smoke_artifacts_preserved": True,
        "parent_smoke_files": sorted(SMOKE_NAMES),
        "frame15_meta": data_meta.get("frame15_meta"),
    }
    (output_dir / "audit_config.json").write_text(json.dumps(json_safe(cfg_doc), indent=2) + "\n", encoding="utf-8")

    if not integrity["passed"]:
        summary = {**cfg_doc, "status": "FAILED_INTEGRITY", "integrity_passed": False, "integrity_n_failed": integrity["n_failed"]}
        (output_dir / "audit_summary.json").write_text(json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8")
        write_readme(output_dir / "README.md", summary)
        return summary

    funnel = build_entry_funnel(frame, tl1, d1_cfg)
    invalidations = build_pre_entry_invalidations(lives, tl1)
    fills = build_fills_table(frame, entries, lives, fill_sum, tl2)
    per_fill = per_fill_forward(tl2, fills)
    fwd_sum = summarize_forward(per_fill)
    ev_rates = event_rates_table(tl2, fills, per_fill)
    ev_paths = event_paths_table(tl2)
    seq = sequence_summary(tl2, per_fill)
    raw_rec = raw_condition_recovery(tl2, per_fill)
    combos = combination_recovery(tl2, per_fill)
    htf_cmp = htf_guard_comparison(fills, per_fill, lives)
    fill_beh = fill_bar_behavior(fills, tl2, lives)

    overlap = int((tl2.groupby("bar_index")["setup_id"].nunique() > 1).sum()) if not tl2.empty else 0

    funnel.to_csv(output_dir / "entry_funnel.csv", index=False)
    invalidations.to_csv(output_dir / "pre_entry_invalidations.csv", index=False)
    fills.to_csv(output_dir / "fills.csv", index=False)
    fwd_sum.to_csv(output_dir / "forward_horizon_summary.csv", index=False)
    per_fill.to_csv(output_dir / "forward_horizon_per_fill.csv", index=False)
    ev_rates.to_csv(output_dir / "event_rates.csv", index=False)
    ev_paths.to_csv(output_dir / "event_paths.csv", index=False)
    seq.to_csv(output_dir / "event_sequence_summary.csv", index=False)
    raw_rec.to_csv(output_dir / "raw_condition_recovery.csv", index=False)
    combos.to_csv(output_dir / "combination_recovery.csv", index=False)
    htf_cmp.to_csv(output_dir / "htf_guard_comparison.csv", index=False)
    fill_beh.to_csv(output_dir / "fill_bar_behavior.csv", index=False)
    tl2.to_csv(output_dir / "d2_timeline_full.csv", index=False)
    fill_sum.to_csv(output_dir / "d2_fill_summary_apt.csv", index=False)
    pd.DataFrame(lives).to_csv(output_dir / "d1_lifecycles.csv", index=False)
    pd.DataFrame(entries).to_csv(output_dir / "d1_entries_apt.csv", index=False)

    n_long = int((fills["direction"] == "long").sum()) if not fills.empty else 0
    n_short = int((fills["direction"] == "short").sum()) if not fills.empty else 0

    high_rec, low_rec = [], []
    if not raw_rec.empty:
        for _, r in raw_rec.iterrows():
            if str(r["condition"]).startswith("underwater"):
                continue
            s = r.get("share_later_mfe_ge_0_5atr")
            if s is None:
                continue
            item = {"condition": r["condition"], "share_later_mfe_ge_0_5atr": s, "n": int(r["n_fills"]), "share_ret24_positive": r.get("share_ret24_positive")}
            if s >= 0.45:
                high_rec.append(item)
            if s <= 0.25 and int(r["n_fills"]) >= 3:
                low_rec.append(item)

    summary = {
        **{k: data_meta[k] for k in data_meta if k != "frame15_meta"},
        "status": "OK",
        "integrity_passed": True,
        "integrity_n_failed": 0,
        "n_fills": int(len(fills)),
        "n_long": n_long,
        "n_short": n_short,
        "n_arms": len(lives),
        "n_pre_entry_aborts": int(sum(1 for L in lives if not L.get("entry_created"))),
        "n_overlap_monitor_bars": overlap,
        "post_entry_horizon_bars": horizon,
        "fill_months": fills["fill_month"].value_counts().to_dict() if not fills.empty else {},
        "htf_at_fill": fills["htf_label"].value_counts().to_dict() if not fills.empty else {},
        "monitor_end_reasons": fills["monitor_end_reason"].value_counts().to_dict() if not fills.empty else {},
        "outcome_A": per_fill["outcome_A_followthrough"].value_counts().to_dict() if not per_fill.empty else {},
        "outcome_B": per_fill["outcome_B_adverse"].value_counts().to_dict() if not per_fill.empty else {},
        "outcome_C": per_fill["outcome_C_order"].value_counts().to_dict() if not per_fill.empty else {},
        "outcome_D": per_fill["outcome_D_ret24"].value_counts().to_dict() if not per_fill.empty else {},
        "high_recovery_conditions_hypotheses_only": high_rec,
        "low_recovery_conditions_hypotheses_only": low_rec,
        "funnel": funnel.to_dict(orient="records"),
        "no_d3_implemented": True,
        "no_commit": True,
        "c35_hash": hashlib.sha256(Path("research/regime_scanner/pullback_entry_c3_5.py").read_bytes()).hexdigest(),
        "c34b_hash": hashlib.sha256(Path("research/regime_scanner/market_structure_c3_4b.py").read_bytes()).hexdigest(),
        "smoke_preserved": [n for n in SMOKE_NAMES if (parent / n).exists()],
    }
    (output_dir / "audit_summary.json").write_text(json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8")
    write_readme(output_dir / "README.md", summary)
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="C3.5D D1/D2 APTUSDT raw-data audit")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--horizon", type=int, default=DEFAULT_POST_ENTRY_HORIZON_BARS)
    args = p.parse_args()
    summary = run_audit(output_dir=args.output_dir, horizon=args.horizon)
    keep = {k: summary[k] for k in summary if k not in {"frame15_meta", "funnel"}}
    print(json.dumps(json_safe(keep), indent=2))
    if summary.get("status") != "OK":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
