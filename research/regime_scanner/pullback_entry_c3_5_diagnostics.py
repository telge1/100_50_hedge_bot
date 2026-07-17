"""C3.5 diagnostics: lifecycle, opposite-arm, ready-age, focus case, pine labels.

Research-only. Does not modify C3.4B. O0/R0 preserve baseline A6 behaviour.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    RESEARCH_VARIANTS,
    PullbackEntryConfig,
    apply_pullback_entry,
    compute_entry_outcomes,
    config_hash,
)
from research.regime_scanner.pullback_entry_c3_5_pine import (
    build_pullback_entry_pine,
)
from research.regime_scanner.trend_pine_export import validate_pine_script

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/diagnostics"
)

OPPOSITE_VARIANTS: tuple[tuple[str, str], ...] = (
    ("O0", "none"),
    ("O1", "trigger_bar"),
    ("O2", "since_ready"),
    ("O3", "lookback_1"),
    ("O4", "lookback_2"),
    ("O5", "lookback_3"),
)

READY_VARIANTS: tuple[tuple[str, int | None], ...] = (
    ("R0", None),
    ("R1", 1),
    ("R2", 2),
    ("R3", 3),
    ("R5", 5),
)

READY_BUCKETS: tuple[tuple[str, int | None, int | None], ...] = (
    ("0", 0, 0),
    ("1", 1, 1),
    ("2", 2, 2),
    ("3", 3, 3),
    ("4-5", 4, 5),
    ("6-10", 6, 10),
    (">10", 11, None),
)


def _variant(name: str) -> PullbackEntryConfig:
    for cfg in RESEARCH_VARIANTS:
        if cfg.name == name:
            return cfg
    raise KeyError(name)


def baseline_a6() -> PullbackEntryConfig:
    return _variant("A6")


def with_opposite(mode: str, *, name: str) -> PullbackEntryConfig:
    base = baseline_a6().to_dict()
    base["name"] = name
    base["opposite_veto_mode"] = mode
    return PullbackEntryConfig(**base)


def with_ready_age(max_ready: int | None, *, name: str) -> PullbackEntryConfig:
    base = baseline_a6().to_dict()
    base["name"] = name
    base["max_ready_age_bars"] = max_ready
    return PullbackEntryConfig(**base)


def terminal_label_tag(outcome: str, reason: str | None) -> str:
    """Short ignore/terminal tag for Pine labels."""
    r = str(reason or "")
    if outcome == "never_reached_pullback":
        return "NO_PB"
    if outcome == "never_reached_ready":
        return "NO_READY"
    if outcome == "no_breakout":
        return "NO_BREAK"
    if outcome == "superseded_by_opposite":
        return "OPP"
    if outcome == "ready_expired":
        return "READY_OLD"
    if outcome in {"timed_out"} or r == "max_age":
        return "TIME"
    if "atr" in r or r in {"entry_too_far_from_ema", "move_since_arm_too_large", "breakout_candle_too_large"}:
        return "ATR"
    if "15m" in r or "30m" in r or "mtf" in r.lower():
        return "MTF"
    if "flip" in r or "structure_flipped" in r:
        return "FLIP"
    if "swing_high" in r or "prior_swing_high" in r:
        return "HIGH"
    if "swing_low" in r or "prior_swing_low" in r:
        return "LOW"
    if outcome == "filtered":
        return "FILTER"
    if outcome == "rejected":
        return "REJ"
    if outcome == "invalidated":
        return "X"
    if outcome == "entered":
        return "OK"
    return "X"


def summarize_terminal_outcomes(lifecycles: Sequence[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for direction in ("short", "long", "all"):
        subset = [x for x in lifecycles if direction == "all" or x.get("direction") == direction]
        c = Counter(str(x.get("terminal_outcome") or "missing") for x in subset)
        n = len(subset)
        for outcome, cnt in sorted(c.items()):
            rows.append(
                {
                    "direction": direction,
                    "terminal_outcome": outcome,
                    "count": cnt,
                    "share": (cnt / n) if n else None,
                }
            )
    return pd.DataFrame(rows)


def _summary_from_outcomes(outcomes: list[dict[str, Any]], *, n_entries_baseline: int | None = None) -> dict[str, Any]:
    if not outcomes:
        return {
            "n_entries": 0,
            "fake_rate": None,
            "mean_mfe": None,
            "mean_mae": None,
            "mean_fwd_10": None,
            "profit_factor_proxy": None,
            "signal_loss_vs_baseline": None if n_entries_baseline is None else 1.0,
        }
    fake = [bool(o.get("is_fake")) for o in outcomes]
    mfe = [float(o["mfe"]) for o in outcomes if o.get("mfe") is not None]
    mae = [float(o["mae"]) for o in outcomes if o.get("mae") is not None]
    fwd10 = [float(o["fwd_ret_10"]) for o in outcomes if o.get("fwd_ret_10") is not None]
    wins = [x for x in fwd10 if x > 0]
    losses = [x for x in fwd10 if x <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None
    n = len(outcomes)
    return {
        "n_entries": n,
        "fake_rate": float(np.mean(fake)),
        "mean_mfe": float(np.mean(mfe)) if mfe else None,
        "mean_mae": float(np.mean(mae)) if mae else None,
        "mean_fwd_10": float(np.mean(fwd10)) if fwd10 else None,
        "profit_factor_proxy": float(pf) if pf is not None else None,
        "signal_loss_vs_baseline": (
            None if n_entries_baseline in (None, 0) else float(1.0 - n / n_entries_baseline)
        ),
    }


def run_opposite_arm_audit(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """O0–O5 comparison + per-entry opposite-arm detail on O0."""
    base_cfg = with_opposite("none", name="O0")
    tl0, entries0, lives0 = apply_pullback_entry(frame, base_cfg, return_lifecycles=True)
    out0 = compute_entry_outcomes(frame, entries0, fee_bps_per_side=base_cfg.fee_bps_per_side)
    n0 = len(out0)
    by_bar = {int(e["bar_index"]): e for e in out0}

    detail_rows = []
    for life in lives0:
        if not life.get("entry_created"):
            continue
        trig = life.get("trigger_bar")
        if trig is None:
            continue
        oc = by_bar.get(int(trig), {})
        opp_bar = life.get("opposite_arm_bar")
        detail_rows.append(
            {
                "setup_id": life.get("setup_id"),
                "direction": life.get("direction"),
                "ready_bar": life.get("ready_bar"),
                "trigger_bar": trig,
                "fill_bar": life.get("fill_bar"),
                "opposite_arm_seen": life.get("opposite_arm_seen"),
                "opposite_arm_bar": opp_bar,
                "opposite_arm_type": life.get("opposite_arm_type"),
                "opp_to_trigger": (int(trig) - int(opp_bar)) if opp_bar is not None else None,
                "opp_to_fill": (
                    (int(life["fill_bar"]) - int(opp_bar))
                    if opp_bar is not None and life.get("fill_bar") is not None
                    else None
                ),
                "mfe": oc.get("mfe"),
                "mae": oc.get("mae"),
                "fwd_ret_3": oc.get("fwd_ret_3"),
                "fwd_ret_5": oc.get("fwd_ret_5"),
                "fwd_ret_10": oc.get("fwd_ret_10"),
                "reversal_within_3": oc.get("reversal_within_3"),
                "reversal_within_5": oc.get("reversal_within_5"),
                "reversal_within_10": oc.get("reversal_within_10"),
                "t1.0_s1.0_target_first": oc.get("t1.0_s1.0_target_first"),
                "t1.0_s1.0_stop_first": oc.get("t1.0_s1.0_stop_first"),
                "is_fake": oc.get("is_fake"),
            }
        )
    detail = pd.DataFrame(detail_rows)

    cmp_rows = []
    for name, mode in OPPOSITE_VARIANTS:
        cfg = with_opposite(mode, name=name)
        _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
        outcomes = compute_entry_outcomes(frame, entries, fee_bps_per_side=cfg.fee_bps_per_side)
        summ = _summary_from_outcomes(outcomes, n_entries_baseline=n0)
        # Compare removed vs baseline by trigger bars
        kept = {int(e["bar_index"]) for e in entries}
        removed = [o for o in out0 if int(o["bar_index"]) not in kept]
        removed_fake = [o for o in removed if o.get("is_fake")]
        removed_good = [o for o in removed if not o.get("is_fake")]
        veto_term = sum(1 for x in lives if x.get("terminal_outcome") == "superseded_by_opposite")
        cmp_rows.append(
            {
                "variant": name,
                "opposite_veto_mode": mode,
                "config_hash": config_hash(cfg),
                **summ,
                "n_removed": len(removed),
                "n_removed_fake": len(removed_fake),
                "n_removed_good": len(removed_good),
                "n_superseded_by_opposite": veto_term,
                "n_entries_with_opp_arm_o0": int(detail["opposite_arm_seen"].fillna(False).sum())
                if not detail.empty
                else 0,
            }
        )
    return pd.DataFrame(cmp_rows), detail


def _ready_bucket(age: int | None) -> str:
    if age is None:
        return "unknown"
    a = int(age)
    for label, lo, hi in READY_BUCKETS:
        if hi is None:
            if a >= int(lo):
                return label
        elif int(lo) <= a <= int(hi):
            return label
    return "unknown"


def run_ready_age_audit(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """R0–R5 comparison + bucket stats on R0 entries."""
    base_cfg = with_ready_age(None, name="R0")
    _tl0, entries0, _lives0 = apply_pullback_entry(frame, base_cfg, return_lifecycles=True)
    out0 = compute_entry_outcomes(frame, entries0, fee_bps_per_side=base_cfg.fee_bps_per_side)
    n0 = len(out0)

    bucket_rows = []
    for o in out0:
        age = o.get("ready_age_at_entry")
        if age is None:
            # derive from lifecycle if missing
            age = None
        bucket_rows.append(
            {
                "bar_index": o.get("bar_index"),
                "direction": "short" if int(o.get("side") or 0) < 0 else "long",
                "ready_age": age,
                "bucket": _ready_bucket(int(age) if age is not None else None),
                "mfe": o.get("mfe"),
                "mae": o.get("mae"),
                "fwd_ret_10": o.get("fwd_ret_10"),
                "is_fake": o.get("is_fake"),
            }
        )
    # Enrich ready_age from lives if needed
    _tl, _e, lives = apply_pullback_entry(frame, base_cfg, return_lifecycles=True)
    life_by_trig = {int(x["trigger_bar"]): x for x in lives if x.get("trigger_bar") is not None}
    for row in bucket_rows:
        bi = int(row["bar_index"])
        if row["ready_age"] is None and bi in life_by_trig:
            life = life_by_trig[bi]
            ready = life.get("ready_bar")
            trig = life.get("trigger_bar")
            if ready is not None and trig is not None:
                row["ready_age"] = int(trig) - int(ready)
                row["bucket"] = _ready_bucket(row["ready_age"])
    buckets = pd.DataFrame(bucket_rows)
    bucket_summary = []
    if not buckets.empty:
        for label, _lo, _hi in READY_BUCKETS:
            sub = buckets[buckets["bucket"] == label]
            if sub.empty:
                bucket_summary.append(
                    {"bucket": label, "n_entries": 0, "fake_rate": None, "mean_mfe": None, "mean_mae": None, "mean_fwd_10": None}
                )
                continue
            bucket_summary.append(
                {
                    "bucket": label,
                    "n_entries": int(len(sub)),
                    "fake_rate": float(sub["is_fake"].mean()),
                    "mean_mfe": float(sub["mfe"].mean()),
                    "mean_mae": float(sub["mae"].mean()),
                    "mean_fwd_10": float(sub["fwd_ret_10"].dropna().mean()) if sub["fwd_ret_10"].notna().any() else None,
                }
            )

    cmp_rows = []
    for name, mx in READY_VARIANTS:
        cfg = with_ready_age(mx, name=name)
        _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
        outcomes = compute_entry_outcomes(frame, entries, fee_bps_per_side=cfg.fee_bps_per_side)
        summ = _summary_from_outcomes(outcomes, n_entries_baseline=n0)
        kept = {int(e["bar_index"]) for e in entries}
        removed = [o for o in out0 if int(o["bar_index"]) not in kept]
        removed_fake = [o for o in removed if o.get("is_fake")]
        removed_good = [o for o in removed if not o.get("is_fake")]
        cmp_rows.append(
            {
                "variant": name,
                "max_ready_age_bars": mx,
                "config_hash": config_hash(cfg),
                **summ,
                "n_removed": len(removed),
                "n_removed_fake": len(removed_fake),
                "n_removed_good": len(removed_good),
                "n_ready_expired": sum(1 for x in lives if x.get("terminal_outcome") == "ready_expired"),
            }
        )
    return pd.DataFrame(cmp_rows), pd.DataFrame(bucket_summary)


def build_focus_case(
    frame: pd.DataFrame,
    *,
    start_ts: str,
    end_ts: str,
    cfg: PullbackEntryConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Bar-by-bar focus window with long/short lifecycle explanation."""
    cfg = cfg or baseline_a6()
    t0 = pd.Timestamp(start_ts, tz="UTC")
    t1 = pd.Timestamp(end_ts, tz="UTC")
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    # Warmup context: run full frame SM but export only window rows
    timeline, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    outcomes = compute_entry_outcomes(frame, entries, fee_bps_per_side=cfg.fee_bps_per_side)
    out_by_bar = {int(o["bar_index"]): o for o in outcomes}

    mask = (pd.to_datetime(timeline["timestamp"], utc=True) >= t0) & (
        pd.to_datetime(timeline["timestamp"], utc=True) <= t1
    )
    win = timeline.loc[mask].copy()

    # Dual virtual tracks: only one active setup in SM; annotate active long/short ids from lives
    life_rows = []
    for life in lives:
        arm = life.get("armed_bar")
        term = life.get("terminal_bar")
        if arm is None:
            continue
        # overlap with window bars
        life_rows.append(life)

    rows = []
    for _, r in win.iterrows():
        bi = int(r["bar_index"])
        active_long = None
        active_short = None
        for life in life_rows:
            arm = int(life["armed_bar"])
            term = int(life["terminal_bar"]) if life.get("terminal_bar") is not None else 10**12
            if arm <= bi <= term:
                if life.get("direction") == "long":
                    active_long = life["setup_id"]
                else:
                    active_short = life["setup_id"]
        ev = str(r.get("events") or "")
        fr = frame.loc[frame["bar_index"] == bi].iloc[0] if "bar_index" in frame.columns else frame.iloc[bi]
        rows.append(
            {
                "timestamp": r.get("timestamp"),
                "bar_index": bi,
                "setup_id_long": active_long,
                "setup_id_short": active_short,
                "long_state": r.get("entry_state") if int(r.get("entry_side") or 0) > 0 or "long_" in ev else (
                    "IDLE" if active_long is None else r.get("entry_state")
                ),
                "short_state": r.get("entry_state") if int(r.get("entry_side") or 0) < 0 or "short_" in ev else (
                    "IDLE" if active_short is None else r.get("entry_state")
                ),
                "entry_state": r.get("entry_state"),
                "events": ev or None,
                "arm_edge": "short_armed" in ev or "long_armed" in ev,
                "pb_edge": "pullback" in ev,
                "ready_edge": "ready" in ev and "expired" not in ev,
                "trigger_edge": bool(r.get("entry_signal")),
                "fill_edge": False,  # marked below
                "opposite_arm_seen": r.get("opposite_arm_seen"),
                "ready_age": r.get("ready_age"),
                "breakout_level": r.get("breakout_level"),
                "terminal_outcome": r.get("terminal_outcome"),
                "terminal_reason": r.get("terminal_reason"),
                "ema_9": fr.get("ema_9"),
                "ema_20": fr.get("ema_20"),
                "ema_50": fr.get("ema_50"),
                "atr_14": fr.get("atr_14"),
                "structure_edge": (
                    "bear"
                    if fr.get("arm_edge_external_bear")
                    else ("bull" if fr.get("arm_edge_external_bull") else None)
                ),
                "entry_accepted": bool(r.get("entry_signal")),
                "entry_rejected": "break_rejected" in ev,
            }
        )
    focus = pd.DataFrame(rows)
    # Mark fill bars = trigger+1 for entries in window
    for e in entries:
        trig = int(e["bar_index"])
        fill = trig + 1
        if fill in set(focus["bar_index"]):
            focus.loc[focus["bar_index"] == fill, "fill_edge"] = True

    # Setup summary overlapping window
    summaries = []
    for life in life_rows:
        arm_ts = None
        if life.get("armed_bar") is not None:
            hit = frame.loc[frame["bar_index"] == int(life["armed_bar"])]
            if not hit.empty:
                arm_ts = hit.iloc[0].get("timestamp")
        term_ts = None
        if life.get("terminal_bar") is not None:
            hit = frame.loc[frame["bar_index"] == int(life["terminal_bar"])]
            if not hit.empty:
                term_ts = hit.iloc[0].get("timestamp")
        # overlap window?
        arm_b = int(life["armed_bar"])
        term_b = int(life["terminal_bar"]) if life.get("terminal_bar") is not None else arm_b
        win_bars = set(focus["bar_index"])
        if not any(arm_b <= b <= term_b for b in win_bars):
            continue
        trig = life.get("trigger_bar")
        oc = out_by_bar.get(int(trig), {}) if trig is not None else {}
        summaries.append(
            {
                **life,
                "armed_timestamp": arm_ts,
                "terminal_timestamp": term_ts,
                "mfe": oc.get("mfe"),
                "mae": oc.get("mae"),
                "fwd_ret_10": oc.get("fwd_ret_10"),
                "is_fake": oc.get("is_fake"),
                "ignore_tag": terminal_label_tag(
                    str(life.get("terminal_outcome") or ""), life.get("terminal_reason")
                ),
            }
        )
    setup_summary = pd.DataFrame(summaries)

    # Variant decisions for setups in window (O/R would they block?)
    decisions = []
    for life in summaries:
        if not life.get("entry_created"):
            decisions.append(
                {
                    "setup_id": life["setup_id"],
                    "direction": life["direction"],
                    "baseline_entered": False,
                    "terminal_outcome": life.get("terminal_outcome"),
                    "O0": "n/a",
                    "O1": "n/a",
                    "O2": "n/a",
                    "O3": "n/a",
                    "O4": "n/a",
                    "O5": "n/a",
                    "R0": "n/a",
                    "R1": "n/a",
                    "R2": "n/a",
                    "R3": "n/a",
                    "R5": "n/a",
                    "ready_age_at_trigger": None,
                    "note": "ignored_baseline",
                }
            )
            continue
        trig = int(life["trigger_bar"])
        ready = life.get("ready_bar")
        ready_age = (trig - int(ready)) if ready is not None else None
        opp = life.get("opposite_arm_bar")
        row = {
            "setup_id": life["setup_id"],
            "direction": life["direction"],
            "baseline_entered": True,
            "terminal_outcome": "entered",
            "ready_age_at_trigger": ready_age,
            "opposite_arm_bar": opp,
            "note": "",
        }
        for name, mode in OPPOSITE_VARIANTS:
            if mode == "none" or opp is None:
                row[name] = "allow"
                continue
            opp_i = int(opp)
            if mode == "trigger_bar":
                row[name] = "veto" if opp_i == trig else "allow"
            elif mode == "since_ready":
                row[name] = "veto" if ready is not None and int(ready) <= opp_i <= trig else "allow"
            elif mode == "lookback_1":
                row[name] = "veto" if trig - opp_i <= 1 else "allow"
            elif mode == "lookback_2":
                row[name] = "veto" if trig - opp_i <= 2 else "allow"
            elif mode == "lookback_3":
                row[name] = "veto" if trig - opp_i <= 3 else "allow"
            else:
                row[name] = "allow"
        for name, mx in READY_VARIANTS:
            if mx is None or ready_age is None:
                row[name] = "allow"
            else:
                # expire when ready_age > max at start of bar; trigger on bar with age==ready_age
                row[name] = "expire" if ready_age > int(mx) else "allow"
        decisions.append(row)
    return focus, setup_summary, pd.DataFrame(decisions)


def explain_focus_case(setup_summary: pd.DataFrame, decisions: pd.DataFrame) -> str:
    lines = ["# Focus case explanation", ""]
    if setup_summary.empty:
        return "No setups in focus window."
    for _, s in setup_summary.sort_values("armed_bar").iterrows():
        tag = s.get("ignore_tag")
        lines.append(
            f"- setup {s['setup_id']} {s['direction']}: outcome={s.get('terminal_outcome')} "
            f"reason={s.get('terminal_reason')} tag={tag} "
            f"arm={s.get('armed_bar')} pb={s.get('pullback_bar')} ready={s.get('ready_bar')} "
            f"trig={s.get('trigger_bar')} opp={s.get('opposite_arm_bar')}"
        )
    entered = setup_summary[setup_summary["entry_created"] == True]  # noqa: E712
    ignored = setup_summary[setup_summary["entry_created"] != True]
    if not ignored.empty:
        left = ignored.iloc[0]
        lines.append("")
        lines.append(
            f"Left ignored setup: id={left['setup_id']} {left['direction']} ended as "
            f"{left.get('terminal_outcome')} ({left.get('terminal_reason')}) → label {left.get('ignore_tag')}."
        )
    if not entered.empty:
        long_e = entered[entered["direction"] == "long"]
        short_e = entered[entered["direction"] == "short"]
        if not long_e.empty:
            L = long_e.iloc[0]
            d = decisions[decisions["setup_id"] == L["setup_id"]]
            lines.append("")
            lines.append(
                f"Long accepted: id={L['setup_id']} ready→trigger age="
                f"{(int(L['trigger_bar'])-int(L['ready_bar'])) if L.get('ready_bar') is not None else None}; "
                f"fake={L.get('is_fake')} fwd10={L.get('fwd_ret_10')}."
            )
            if not d.empty:
                dd = d.iloc[0]
                veto_modes = [k for k in ("O1", "O2", "O3", "O4", "O5") if dd.get(k) == "veto"]
                exp_modes = [k for k in ("R1", "R2", "R3", "R5") if dd.get(k) == "expire"]
                lines.append(
                    f"Opposite veto would block under: {veto_modes or 'none'}; "
                    f"ready expiry would block under: {exp_modes or 'none'}."
                )
        if not short_e.empty:
            S = short_e.iloc[-1]
            lines.append(
                f"Later short accepted: id={S['setup_id']} reason={S.get('terminal_reason')} "
                f"fake={S.get('is_fake')} fwd10={S.get('fwd_ret_10')}."
            )
    return "\n".join(lines) + "\n"


def build_diagnostics_pine(*, title: str | None = None) -> str:
    """Diagnostics pine = C3.5 pine with terminal/O/R inputs (already in base export)."""
    text = build_pullback_entry_pine(title=title or "C3.5 Pullback Entry Diagnostics")
    text = text.replace(
        '"C3.5 Pullback Entry Diagnose"',
        '"C3.5 Pullback Entry Diagnostics"',
        1,
    )
    # Ensure diagnostics markers present
    for req in (
        "showTerminalLabels",
        "terminalEdge",
        "NO_PB",
        "oppVeto",
        "useReadyExpiry",
        "showSpan",
        "S X ",
    ):
        if req not in text:
            raise ValueError(f"diagnostics pine missing {req}")
    validate_pine_script(text)
    if "lookahead_on" in text:
        raise ValueError("lookahead_on forbidden")
    if "line.new(" in text:
        raise ValueError("line.new spam forbidden")
    return text


def _default_diagnostics_addon() -> str:
    return r'''
// --- Diagnostics addon: terminal labels + optional span (research) ---
var int setupIdCounter = 0
var int activeSetupId = na
var string lastTermOutcome = "-"
var string lastTermReason = "-"
var int lastTermBar = na
var string lastTermDir = "-"
var string lastTermState = "-"
var bool terminalEdge = false
var string terminalTag = "-"

// Remap research opposite/ready inputs onto existing gate flags where possible.
// Full O/R parity for veto/expiry is implemented in Python diagnostics; Pine mirrors labels.
showSpan = showSetupSpan or showStateBackground
spanArmed = showSpan and (entryState == "SHORT_ARMED" or entryState == "LONG_ARMED")
spanPb = showSpan and (entryState == "SHORT_PULLBACK" or entryState == "LONG_PULLBACK")
spanReady = showSpan and (entryState == "SHORT_READY" or entryState == "LONG_READY")
bgcolor(inFocus and spanArmed ? color.new(color.yellow, 92) : inFocus and spanPb ? color.new(color.orange, 92) : inFocus and spanReady ? color.new(color.purple, 92) : na)

// Detect terminal edges from invalidation / ATR reject / ready expiry proxies already in SM.
terminalEdge := shortInvEdge or longInvEdge
if terminalEdge
    lastTermBar := bar_index
    lastTermDir := shortInvEdge ? "short" : "long"
    lastTermState := shortInvEdge ? "SHORT_*" : "LONG_*"
    lastTermReason := lastInvReason
    lastTermOutcome := lastInvReason == "setup_timeout" ? "timed_out" : lastInvReason == "structure_flipped" ? "invalidated" : lastInvReason == "pullback_too_far" ? "invalidated" : lastInvReason == "swing_high_broken" or lastInvReason == "swing_low_broken" ? "invalidated" : lastInvReason == "ema_reclaim" ? "invalidated" : str.contains(lastInvReason, "15m") or str.contains(lastInvReason, "30m") ? "invalidated" : "invalidated"
    terminalTag := lastInvReason == "setup_timeout" ? "TIME" : lastInvReason == "structure_flipped" ? "FLIP" : lastInvReason == "swing_high_broken" ? "HIGH" : lastInvReason == "swing_low_broken" ? "LOW" : lastInvReason == "pullback_too_far" ? "NO_BREAK" : str.contains(lastInvReason, "15m") or str.contains(lastInvReason, "30m") ? "MTF" : "X"

if atrRejectEdge
    lastTermReason := rejectReason
    terminalTag := "ATR"

idSuffix = showSetupId and not na(activeSetupId) ? " #" + str.tostring(activeSetupId) : ""
fullSuffix = showFullReasonLabels ? " " + lastTermReason : ""

if inFocus and showTerminalLabels and terminalEdge and lastTermDir == "short"
    label.new(bar_index, high, "S X " + terminalTag + idSuffix + fullSuffix, style=label.style_label_down, color=color.new(color.gray, 15), textcolor=color.white, size=size.tiny)
if inFocus and showTerminalLabels and terminalEdge and lastTermDir == "long"
    label.new(bar_index, low, "L X " + terminalTag + idSuffix + fullSuffix, style=label.style_label_up, color=color.new(color.gray, 15), textcolor=color.white, size=size.tiny)

// Extend table with terminal diagnostics
if showTable and barstate.islast
    table.cell(diag, 0, 22, "Last invalidation")
    table.cell(diag, 1, 22, lastInvReason)
    table.cell(diag, 0, 23, "Last entry reason")
    table.cell(diag, 1, 23, lastEntryReason)

var table term = table.new(position.bottom_right, 2, 6, border_width=1)
if showTable and barstate.islast
    table.cell(term, 0, 0, "Last setup id")
    table.cell(term, 1, 0, na(activeSetupId) ? "-" : str.tostring(activeSetupId))
    table.cell(term, 0, 1, "Last direction")
    table.cell(term, 1, 1, lastTermDir)
    table.cell(term, 0, 2, "Last terminal outcome")
    table.cell(term, 1, 2, lastTermOutcome)
    table.cell(term, 0, 3, "Last terminal reason")
    table.cell(term, 1, 3, lastTermReason)
    table.cell(term, 0, 4, "Last terminal bar")
    table.cell(term, 1, 4, na(lastTermBar) ? "-" : str.tostring(lastTermBar))
    table.cell(term, 0, 5, "Last state before term")
    table.cell(term, 1, 5, lastTermState)

// EOF
'''


def write_diagnostics_pine(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    text = build_diagnostics_pine()
    path = output_dir / "indicator_pullback_entry_c3_5_diagnostics.pine"
    path.write_text(text, encoding="utf-8")
    return {
        "path": str(path),
        "bytes": len(text.encode()),
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
    }


def choose_focus_window(frame: pd.DataFrame) -> tuple[str, str, str]:
    """Pick a window with opposite-arm / ignored setup + long→short when possible."""
    cfg = baseline_a6()
    _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    long_entries = [e for e in entries if int(e.get("side") or 0) > 0]
    short_entries = [e for e in entries if int(e.get("side") or 0) < 0]
    best = None
    for L in long_entries:
        li = int(L["bar_index"])
        life_l = next((x for x in lives if x.get("trigger_bar") == li), None)
        ignored_before = [
            x
            for x in lives
            if (not x.get("entry_created"))
            and x.get("terminal_bar") is not None
            and 0 <= li - int(x["terminal_bar"]) <= 40
        ]
        for S in short_entries:
            si = int(S["bar_index"])
            if not (0 < si - li <= 50):
                continue
            score = 15 - min(si - li, 15)
            if life_l and life_l.get("opposite_arm_seen"):
                score += 100
                if life_l.get("opposite_arm_bar") == li:
                    score += 40
            if ignored_before:
                score += 35
            if best is None or score > best[0]:
                best = (score, li, si, life_l, ignored_before[:1])
    # Prefer any opposite-arm long even without following short
    if best is None or best[0] < 100:
        for life in lives:
            if life.get("direction") == "long" and life.get("entry_created") and life.get("opposite_arm_seen"):
                li = int(life["trigger_bar"])
                si = li + 20
                score = 120
                if best is None or score > best[0]:
                    best = (score, li, si, life, [])
    if best is None and long_entries:
        li = int(long_entries[0]["bar_index"])
        best = (0, li, li + 30, None, [])
    if best is None:
        return "2026-02-03 00:00:00+00:00", "2026-02-03 06:00:00+00:00", "fallback_event_trace_window"
    _score, li, si, life_l, _ign = best
    start_i = max(int(frame["bar_index"].min()), li - 50)
    end_i = min(int(frame["bar_index"].max()), si + 25)

    def _ts_at_bar(bar: int) -> str:
        hit = frame.loc[frame["bar_index"] == int(bar)]
        val = hit.iloc[0]["timestamp"] if not hit.empty else frame.iloc[min(int(bar), len(frame) - 1)]["timestamp"]
        ts = pd.Timestamp(val)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return str(ts)

    opp = life_l.get("opposite_arm_bar") if life_l else None
    note = f"long_trig={li} short_trig={si} long_opp={opp}"
    return _ts_at_bar(start_i), _ts_at_bar(end_i), note



def run_diagnostics(
    frame: pd.DataFrame,
    *,
    output_dir: Path | None = None,
    focus_start: str | None = None,
    focus_end: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir or DEFAULT_OUT)
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = baseline_a6()
    timeline, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    # Ensure every life has terminal
    missing = [x for x in lives if not x.get("terminal_outcome")]
    if missing:
        raise RuntimeError(f"{len(missing)} setups missing terminal_outcome")

    life_df = pd.DataFrame(lives)
    life_df.to_csv(output_dir / "setup_lifecycle.csv", index=False)
    term_sum = summarize_terminal_outcomes(lives)
    term_sum.to_csv(output_dir / "terminal_outcome_summary.csv", index=False)

    opp_cmp, opp_detail = run_opposite_arm_audit(frame)
    opp_cmp.to_csv(output_dir / "opposite_arm_audit.csv", index=False)
    opp_detail.to_csv(output_dir / "opposite_arm_entry_detail.csv", index=False)

    ready_cmp, ready_buckets = run_ready_age_audit(frame)
    ready_cmp.to_csv(output_dir / "ready_age_audit.csv", index=False)
    ready_buckets.to_csv(output_dir / "ready_age_buckets.csv", index=False)

    if focus_start is None or focus_end is None:
        focus_start, focus_end, focus_note = choose_focus_window(frame)
    else:
        focus_note = "user_provided"
    focus, setup_sum, decisions = build_focus_case(frame, start_ts=focus_start, end_ts=focus_end, cfg=cfg)
    focus.to_csv(output_dir / "focus_case_lifecycle.csv", index=False)
    setup_sum.to_csv(output_dir / "focus_case_setup_summary.csv", index=False)
    decisions.to_csv(output_dir / "focus_case_variant_decisions.csv", index=False)
    explanation = explain_focus_case(setup_sum, decisions)
    (output_dir / "focus_case_explanation.md").write_text(explanation, encoding="utf-8")

    pine_meta = write_diagnostics_pine(output_dir)

    # Baseline reproducibility check O0/R0 vs A6
    a6_n = len(entries)
    o0_n = int(opp_cmp.loc[opp_cmp["variant"] == "O0", "n_entries"].iloc[0])
    r0_n = int(ready_cmp.loc[ready_cmp["variant"] == "R0", "n_entries"].iloc[0])

    manifest = {
        "baseline_variant": "A6",
        "baseline_entries": a6_n,
        "baseline_setups": len(lives),
        "o0_entries": o0_n,
        "r0_entries": r0_n,
        "o0_r0_reproducible": bool(o0_n == a6_n == r0_n),
        "focus_start": focus_start,
        "focus_end": focus_end,
        "focus_note": focus_note,
        "terminal_outcome_counts": term_sum[term_sum["direction"] == "all"]
        .set_index("terminal_outcome")["count"]
        .to_dict(),
        "pine": pine_meta,
        "config_hash_a6": config_hash(cfg),
        "recommendation": _recommend(opp_cmp, ready_cmp, ready_buckets),
    }
    (output_dir / "diagnostics_manifest.json").write_text(
        json.dumps(json_safe(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _recommend(opp_cmp: pd.DataFrame, ready_cmp: pd.DataFrame, ready_buckets: pd.DataFrame) -> dict[str, Any]:
    """Heuristic research recommendation — not a production rule."""
    rec: dict[str, Any] = {"opposite": None, "ready_age": None, "notes": []}
    if not opp_cmp.empty:
        base = opp_cmp[opp_cmp["variant"] == "O0"].iloc[0]
        cands = []
        for _, r in opp_cmp.iterrows():
            if r["variant"] == "O0":
                continue
            # Prefer removing more fakes than goods, limited signal loss
            if r.get("n_removed_fake", 0) > r.get("n_removed_good", 0) and (r.get("signal_loss_vs_baseline") or 0) < 0.25:
                cands.append(r)
        if cands:
            best = max(cands, key=lambda x: (x.get("n_removed_fake", 0) - x.get("n_removed_good", 0), -x.get("signal_loss_vs_baseline", 1)))
            rec["opposite"] = {
                "variant": best["variant"],
                "mode": best["opposite_veto_mode"],
                "removed_fake": int(best["n_removed_fake"]),
                "removed_good": int(best["n_removed_good"]),
                "signal_loss": best.get("signal_loss_vs_baseline"),
                "fake_rate": best.get("fake_rate"),
            }
        else:
            rec["notes"].append("No opposite veto clearly beat O0 on fake-vs-good removal.")
        rec["notes"].append(f"O0 fake_rate={base.get('fake_rate')} n={base.get('n_entries')}")
    if not ready_buckets.empty:
        high = ready_buckets[ready_buckets["bucket"].isin([">10", "6-10"])]
        if not high.empty and high["n_entries"].sum() > 0:
            rec["notes"].append(
                f"Older ready buckets n={int(high['n_entries'].sum())} "
                f"fake≈{float(high['fake_rate'].mean()) if high['fake_rate'].notna().any() else None}"
            )
    if not ready_cmp.empty:
        cands = []
        for _, r in ready_cmp.iterrows():
            if r["variant"] == "R0":
                continue
            if r.get("n_removed_fake", 0) >= r.get("n_removed_good", 0) and (r.get("signal_loss_vs_baseline") or 0) < 0.35:
                cands.append(r)
        if cands:
            best = max(cands, key=lambda x: (x.get("n_removed_fake", 0) - x.get("n_removed_good", 0), -x.get("signal_loss_vs_baseline", 1)))
            rec["ready_age"] = {
                "variant": best["variant"],
                "max_ready_age_bars": best["max_ready_age_bars"],
                "removed_fake": int(best["n_removed_fake"]),
                "removed_good": int(best["n_removed_good"]),
                "signal_loss": best.get("signal_loss_vs_baseline"),
            }
        else:
            rec["notes"].append("No ready-age cap clearly beat R0; keep R0 until more evidence.")
    return rec


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--frame",
        default="research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/research_frame_5m.csv",
    )
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--focus-start", default=None)
    p.add_argument("--focus-end", default=None)
    p.add_argument("--analyze-start", default="2026-02-01")
    p.add_argument("--analyze-end", default="2026-04-30")
    args = p.parse_args()
    frame = pd.read_csv(args.frame, parse_dates=["timestamp"])
    if frame["timestamp"].dt.tz is None:
        frame["timestamp"] = frame["timestamp"].dt.tz_localize("UTC")
    else:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert("UTC")
    if "bar_index" not in frame.columns:
        frame["bar_index"] = np.arange(len(frame))
    a0 = pd.Timestamp(args.analyze_start, tz="UTC")
    a1 = pd.Timestamp(args.analyze_end, tz="UTC") + pd.Timedelta(days=1)
    sub = frame[(frame["timestamp"] >= a0) & (frame["timestamp"] < a1)].reset_index(drop=True)
    # Keep original bar_index from full frame if present
    manifest = run_diagnostics(
        sub,
        output_dir=Path(args.out),
        focus_start=args.focus_start,
        focus_end=args.focus_end,
    )
    print(json.dumps(json_safe(manifest), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
