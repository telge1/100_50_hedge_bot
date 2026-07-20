"""C3.5D protected-level carry-forward policy audit (offline, research-only).

Primary policy: **one-level lag** (V_1LAG):
  effective[n] = local[n-1] of the immediately previous valid same-direction
  same-leg setup (NOT max/min over the whole chain).

Historical comparison still includes outer max/min chain carry (V1/V2/V4_*).

No D1/D2/C3.4B mutation, no D3 runtime, no live bot, no exit integration.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5d_apt_raw_audit import build_apt_d1_frame
from research.regime_scanner.trend_pine_export import (
    build_pine_header,
    validate_pine_script,
)

PHASE = "C3.5D_PROTECTED_CARRY_AUDIT"
DEFAULT_APT_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
DEFAULT_OUT = DEFAULT_APT_DIR / "protected_carry"
PINE_OUT = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/pine_exit_levels"
)
MAIN_PINE = "C3_5D_APT_protected_carry_audit.pine"

HORIZONS = (("h24", 24), ("h48", 48), ("h96", 96), ("full", None))
# Recommended: V0, V_1LAG, V_2LAG. Historical max/min: V1, V2, V4_*.
PRIMARY_VARIANTS = ("V0", "V_1LAG", "V_2LAG")
HIST_VARIANTS = ("V1", "V2", "V4_24", "V4_48", "V4_96")
VARIANTS = PRIMARY_VARIANTS + HIST_VARIANTS


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _safe_rate(n: int, d: int) -> float | None:
    return None if d <= 0 else float(n) / float(d)


def _median(xs: Sequence[float]) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.median(vals)) if vals else None


def _quantile(xs: Sequence[float], q: float) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.quantile(vals, q)) if vals else None


def signed_return_pct(*, side: int, entry: float, close: float) -> float:
    if side > 0:
        return (close / entry - 1.0) * 100.0
    return (1.0 - close / entry) * 100.0


def combine_protected_hist(*, side: int, prev_eff: float | None, local: float) -> float:
    """HISTORICAL outer-only chain carry (NOT used by V_1LAG). Short max / long min."""
    if prev_eff is None or not math.isfinite(prev_eff):
        return float(local)
    if side > 0:
        return float(min(prev_eff, local))
    return float(max(prev_eff, local))


# Back-compat alias for older tests / imports.
combine_protected = combine_protected_hist


def close_breaks_protected(*, side: int, close: float, level: float) -> bool:
    if side > 0:
        return close < level
    return close > level


@dataclass
class LagLegState:
    """Queue of (setup_id, local_protected) within one structure leg."""

    side: int | None = None
    leg_id: int = 0
    history: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class HistCarryState:
    side: int | None = None
    effective: float | None = None
    origin_setup_id: int | None = None
    origin_fill_bar: int | None = None
    leg_id: int = 0


@dataclass
class SetupCarry:
    setup_id: int
    direction: str
    side: int
    fill_bar: int
    fill_timestamp: Any
    entry_price: float
    local_protected: float
    atr: float
    frozen_ltf: int
    frozen_htf: int
    effective_by_variant: dict[str, float] = field(default_factory=dict)
    previous_local_by_variant: dict[str, float | None] = field(default_factory=dict)
    pending_local_by_variant: dict[str, float] = field(default_factory=dict)
    carry_origin_by_variant: dict[str, int | None] = field(default_factory=dict)
    carry_depth_by_variant: dict[str, int] = field(default_factory=dict)
    carried_from_prev_by_variant: dict[str, bool] = field(default_factory=dict)
    reset_reason_by_variant: dict[str, str | None] = field(default_factory=dict)
    same_leg_by_variant: dict[str, bool] = field(default_factory=dict)
    leg_id_by_variant: dict[str, int] = field(default_factory=dict)


def ensure_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    f = frame.copy()
    if "bar_index" not in f.columns:
        f = f.reset_index(drop=True)
        f["bar_index"] = np.arange(len(f), dtype=int)
    return f.set_index("bar_index", drop=False)


def major_flip_against(
    ohlc: pd.DataFrame,
    *,
    start_bar: int,
    end_bar: int,
    side: int,
    col: str,
) -> bool:
    if col not in ohlc.columns:
        return False
    lo = int(start_bar) + 1
    hi = int(end_bar)
    if hi < lo:
        return False
    sub = ohlc.loc[lo:hi, col]
    vals = pd.to_numeric(sub, errors="coerce").fillna(0).astype(int)
    opp = -1 if side > 0 else 1
    return bool((vals == opp).any())


def same_structure_leg(
    ohlc: pd.DataFrame,
    prev: SetupCarry,
    curr: SetupCarry,
) -> tuple[bool, str | None]:
    """Same direction + same LTF at fill; reset on LTF/HTF adverse flip between fills."""
    if prev.side != curr.side:
        return False, "direction_change"
    if prev.frozen_ltf != curr.frozen_ltf:
        return False, "ltf_major_changed_at_fill"
    if curr.frozen_ltf != 0 and curr.frozen_ltf != curr.side:
        return False, "ltf_against_trade_at_fill"
    if major_flip_against(
        ohlc, start_bar=prev.fill_bar, end_bar=curr.fill_bar, side=curr.side, col="ltf_major_direction"
    ):
        return False, "ltf_flip_between_fills"
    if major_flip_against(
        ohlc, start_bar=prev.fill_bar, end_bar=curr.fill_bar, side=curr.side, col="htf_major_direction"
    ):
        return False, "htf_flip_between_fills"
    return True, None


def _apply_lag_assignment(
    s: SetupCarry,
    *,
    variant: str,
    lag: int,
    st: LagLegState,
    reset: str | None,
    continue_leg: bool,
) -> None:
    """effective = local of setup lag steps back in current leg history (before append)."""
    if not continue_leg:
        if st.side is not None:
            st.leg_id += 1
        st.side = s.side
        st.history = []

    prev_local: float | None = st.history[-1][1] if st.history else None
    if not st.history:
        eff = float(s.local_protected)
        source = s.setup_id
        depth = 0
    elif len(st.history) < lag:
        # not enough depth yet: stay on first local of this leg
        source = st.history[0][0]
        eff = float(st.history[0][1])
        depth = min(len(st.history), lag)
    else:
        source = st.history[-lag][0]
        eff = float(st.history[-lag][1])
        depth = lag

    carried = source != s.setup_id
    s.effective_by_variant[variant] = eff
    s.previous_local_by_variant[variant] = prev_local
    s.pending_local_by_variant[variant] = float(s.local_protected)
    s.carry_origin_by_variant[variant] = source
    s.carry_depth_by_variant[variant] = int(depth if carried else 0)
    s.carried_from_prev_by_variant[variant] = carried
    s.reset_reason_by_variant[variant] = None if continue_leg else reset
    s.same_leg_by_variant[variant] = bool(continue_leg)
    s.leg_id_by_variant[variant] = st.leg_id

    st.side = s.side
    st.history.append((s.setup_id, float(s.local_protected)))


def assign_effective_levels(
    setups: list[SetupCarry],
    ohlc: pd.DataFrame,
) -> None:
    """Populate effective levels. V_1LAG/V_2LAG = one/two-level lag; V1/V2/V4 = hist max/min."""
    lag_states: dict[str, LagLegState] = {
        "V_1LAG": LagLegState(),
        "V_2LAG": LagLegState(),
    }
    hist_states: dict[str, HistCarryState] = {v: HistCarryState() for v in HIST_VARIANTS}

    for s in setups:
        # --- V0 ---
        s.effective_by_variant["V0"] = float(s.local_protected)
        s.previous_local_by_variant["V0"] = None
        s.pending_local_by_variant["V0"] = float(s.local_protected)
        s.carry_origin_by_variant["V0"] = s.setup_id
        s.carry_depth_by_variant["V0"] = 0
        s.carried_from_prev_by_variant["V0"] = False
        s.reset_reason_by_variant["V0"] = "v0_local_is_effective"
        s.same_leg_by_variant["V0"] = False
        s.leg_id_by_variant["V0"] = 0

        # --- lag variants (require same structure leg) ---
        for variant, lag in (("V_1LAG", 1), ("V_2LAG", 2)):
            st = lag_states[variant]
            reset: str | None = None
            continue_leg = False
            if st.side is None:
                reset = "first"
            elif st.side != s.side:
                reset = "direction_change"
            else:
                prev_same = next(
                    (x for x in reversed(setups) if x.fill_bar < s.fill_bar and x.side == s.side),
                    None,
                )
                # Prefer last history member as structural predecessor when present
                if st.history:
                    prev_id = st.history[-1][0]
                    prev_same = next((x for x in setups if x.setup_id == prev_id), prev_same)
                if prev_same is None:
                    reset = "no_prev_same_dir"
                else:
                    ok, why = same_structure_leg(ohlc, prev_same, s)
                    if ok:
                        continue_leg = True
                    else:
                        reset = why
            _apply_lag_assignment(
                s, variant=variant, lag=lag, st=st, reset=reset, continue_leg=continue_leg
            )

        # --- historical max/min chain (comparison only) ---
        for v in HIST_VARIANTS:
            st = hist_states[v]
            reset = None
            allow = False
            if st.side is None:
                reset = "first"
            elif st.side != s.side:
                reset = "direction_change"
            else:
                if v == "V1":
                    allow = True
                elif v == "V2":
                    prev_same = next(
                        (x for x in reversed(setups) if x.fill_bar < s.fill_bar and x.side == s.side),
                        None,
                    )
                    if prev_same is None:
                        reset = "no_prev_same_dir"
                    else:
                        ok, why = same_structure_leg(ohlc, prev_same, s)
                        if ok:
                            allow = True
                        else:
                            reset = why
                elif v.startswith("V4_"):
                    max_age = int(v.split("_")[1])
                    if st.origin_fill_bar is None:
                        reset = "missing_origin"
                    elif s.fill_bar - int(st.origin_fill_bar) > max_age:
                        reset = f"max_age_{max_age}"
                    else:
                        allow = True

            if allow and st.effective is not None:
                eff = combine_protected_hist(side=s.side, prev_eff=st.effective, local=s.local_protected)
                carried = abs(eff - s.local_protected) > 1e-12
                origin = st.origin_setup_id if carried else s.setup_id
                if not carried:
                    st.origin_setup_id = s.setup_id
                    st.origin_fill_bar = s.fill_bar
                    origin = s.setup_id
                elif abs(eff - float(st.effective)) > 1e-12:
                    st.origin_setup_id = s.setup_id
                    st.origin_fill_bar = s.fill_bar
                    origin = s.setup_id
                s.effective_by_variant[v] = eff
                s.previous_local_by_variant[v] = float(st.effective)
                s.pending_local_by_variant[v] = float(s.local_protected)
                s.carry_origin_by_variant[v] = origin
                s.carry_depth_by_variant[v] = -1  # historical chain; depth undefined
                s.carried_from_prev_by_variant[v] = carried
                s.reset_reason_by_variant[v] = None
                s.same_leg_by_variant[v] = True
                s.leg_id_by_variant[v] = st.leg_id
                st.effective = eff
                st.side = s.side
            else:
                if reset not in (None, "first") or st.side != s.side:
                    st.leg_id += 1
                elif reset == "first":
                    st.leg_id = 0
                st.side = s.side
                st.effective = float(s.local_protected)
                st.origin_setup_id = s.setup_id
                st.origin_fill_bar = s.fill_bar
                s.effective_by_variant[v] = float(s.local_protected)
                s.previous_local_by_variant[v] = None
                s.pending_local_by_variant[v] = float(s.local_protected)
                s.carry_origin_by_variant[v] = s.setup_id
                s.carry_depth_by_variant[v] = 0
                s.carried_from_prev_by_variant[v] = False
                s.reset_reason_by_variant[v] = reset
                s.same_leg_by_variant[v] = False
                s.leg_id_by_variant[v] = st.leg_id


def path_end(fill_bar: int, data_end: int, n_bars: int | None) -> int:
    if n_bars is None:
        return int(data_end)
    return int(min(data_end, fill_bar + n_bars - 1))


def first_close_break_bar(
    ohlc: pd.DataFrame,
    *,
    side: int,
    fill_bar: int,
    end_bar: int,
    level: float,
) -> int | None:
    for bi in range(int(fill_bar), int(end_bar) + 1):
        if bi not in ohlc.index:
            continue
        c = float(ohlc.loc[bi, "close"])
        if close_breaks_protected(side=side, close=c, level=level):
            return int(bi)
    return None


def post_break_metrics(
    ohlc: pd.DataFrame,
    *,
    side: int,
    entry: float,
    atr: float,
    fill_bar: int,
    break_bar: int | None,
    end_bar: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "broke": break_bar is not None,
        "break_bar": break_bar,
        "bars_fill_to_break": (int(break_bar - fill_bar) if break_bar is not None else None),
        "signed_pct_at_break": float("nan"),
        "loss_atr_at_break": float("nan"),
        "entry_close_recovery_after_break": False,
        "bars_to_entry_close_after_break": None,
        "max_plus_after_entry_close_pct": float("nan"),
        "mfe_pct_after_break": float("nan"),
        "mae_pct_path": float("nan"),
        "final_signed_pct": float("nan"),
        "add_adverse_after_break_pct": float("nan"),
    }
    path = ohlc.loc[int(fill_bar) : int(end_bar)]
    if path.empty:
        return out
    closes = path["close"].astype(float)
    highs = path["high"].astype(float)
    lows = path["low"].astype(float)
    if side > 0:
        out["mae_pct_path"] = float((float(lows.min()) / entry - 1.0) * 100.0)
        out["mfe_pct_after_break"] = float("nan")
    else:
        out["mae_pct_path"] = float((1.0 - float(highs.max()) / entry) * 100.0)

    last_c = float(closes.iloc[-1])
    out["final_signed_pct"] = float(signed_return_pct(side=side, entry=entry, close=last_c))

    if break_bar is None:
        # favorable MFE on whole path
        if side > 0:
            out["mfe_pct_after_break"] = float((float(highs.max()) / entry - 1.0) * 100.0)
        else:
            out["mfe_pct_after_break"] = float((1.0 - float(lows.min()) / entry) * 100.0)
        return out

    br = ohlc.loc[int(break_bar)]
    close_b = float(br["close"])
    signed = signed_return_pct(side=side, entry=entry, close=close_b)
    out["signed_pct_at_break"] = float(signed)
    atr_v = _finite(atr)
    out["loss_atr_at_break"] = (
        float(signed / 100.0 * entry / atr_v) if atr_v > 0 else float("nan")
    )

    post = ohlc.loc[int(break_bar) + 1 : int(end_bar)]
    if post.empty:
        return out
    ph = post["high"].astype(float).to_numpy()
    pl = post["low"].astype(float).to_numpy()
    pc = post["close"].astype(float).to_numpy()
    pbars = post.index.astype(int).to_numpy()
    if side > 0:
        add = (float(np.min(pl)) - close_b) / entry * 100.0
        mfe = (float(np.max(ph)) / entry - 1.0) * 100.0
        ec = np.where(pc >= entry)[0]
    else:
        add = (close_b - float(np.max(ph))) / entry * 100.0
        mfe = (1.0 - float(np.min(pl)) / entry) * 100.0
        ec = np.where(pc <= entry)[0]
    out["add_adverse_after_break_pct"] = float(add)
    out["mfe_pct_after_break"] = float(mfe)
    if len(ec):
        out["entry_close_recovery_after_break"] = True
        out["bars_to_entry_close_after_break"] = int(pbars[int(ec[0])] - break_bar)
        i0 = int(ec[0])
        if side > 0:
            out["max_plus_after_entry_close_pct"] = float((float(np.max(ph[i0:])) / entry - 1.0) * 100.0)
        else:
            out["max_plus_after_entry_close_pct"] = float((1.0 - float(np.min(pl[i0:])) / entry) * 100.0)
    return out


def evaluate_setup_variant(
    s: SetupCarry,
    ohlc: pd.DataFrame,
    *,
    variant: str,
) -> dict[str, Any]:
    local = float(s.local_protected)
    eff = float(s.effective_by_variant[variant])
    data_end = int(ohlc.index.max())
    row: dict[str, Any] = {
        "variant": variant,
        "setup_id": s.setup_id,
        "direction": s.direction,
        "side": s.side,
        "fill_bar": s.fill_bar,
        "fill_timestamp": str(s.fill_timestamp),
        "entry_price": s.entry_price,
        "local_protected_level": local,
        "local_protected": local,  # alias
        "previous_local_protected_level": s.previous_local_by_variant.get(variant),
        "pending_local_protected_level": s.pending_local_by_variant.get(variant, local),
        "effective_protected_level": eff,
        "effective_protected": eff,  # alias
        "carry_source_setup_id": s.carry_origin_by_variant.get(variant),
        "carry_origin_setup_id": s.carry_origin_by_variant.get(variant),  # alias
        "carry_depth": s.carry_depth_by_variant.get(variant, 0),
        "carried_from_prev": bool(s.carried_from_prev_by_variant.get(variant)),
        "reset_reason": s.reset_reason_by_variant.get(variant),
        "same_leg": bool(s.same_leg_by_variant.get(variant)),
        "leg_id": s.leg_id_by_variant.get(variant),
        "effective_wider_than_local": abs(eff - local) > 1e-12,
        "dist_local_to_effective_pct": (
            abs(eff - local) / s.entry_price * 100.0 if s.entry_price else float("nan")
        ),
        "policy_note": (
            "local_break=warning; effective_break=hard_invalidation"
            if variant in ("V_1LAG", "V_2LAG", "V0")
            else "historical_maxmin_chain"
        ),
    }

    for scope, n in HORIZONS:
        end = path_end(s.fill_bar, data_end, n)
        local_br = first_close_break_bar(
            ohlc, side=s.side, fill_bar=s.fill_bar, end_bar=end, level=local
        )
        eff_br = first_close_break_bar(
            ohlc, side=s.side, fill_bar=s.fill_bar, end_bar=end, level=eff
        )
        loc_m = post_break_metrics(
            ohlc,
            side=s.side,
            entry=s.entry_price,
            atr=s.atr,
            fill_bar=s.fill_bar,
            break_bar=local_br,
            end_bar=end,
        )
        eff_m = post_break_metrics(
            ohlc,
            side=s.side,
            entry=s.entry_price,
            atr=s.atr,
            fill_bar=s.fill_bar,
            break_bar=eff_br,
            end_bar=end,
        )
        # delayed / avoided relative to local
        avoided = bool(loc_m["broke"] and not eff_m["broke"])
        delayed = bool(
            loc_m["broke"]
            and eff_m["broke"]
            and eff_m["break_bar"] is not None
            and loc_m["break_bar"] is not None
            and int(eff_m["break_bar"]) > int(loc_m["break_bar"])
        )
        bars_gap = None
        if delayed:
            bars_gap = int(eff_m["break_bar"]) - int(loc_m["break_bar"])  # type: ignore[arg-type]

        # rescued: local broke, effective not (or delayed), then entry recovered within scope after local break
        rescued = False
        if loc_m["broke"] and (avoided or delayed):
            # recovery after local break on path to end
            rescued = bool(loc_m["entry_close_recovery_after_break"]) and (
                _finite(loc_m["max_plus_after_entry_close_pct"]) > 0
                or _finite(loc_m["mfe_pct_after_break"]) > 0
            )
            # stricter: recovered to entry after local break
            rescued = bool(loc_m["entry_close_recovery_after_break"])

        # worse: carried past local break, then effective break with more adverse signed than local break
        worse = False
        extra_mae = float("nan")
        if delayed and loc_m["broke"] and eff_m["broke"]:
            extra_mae = _finite(eff_m["signed_pct_at_break"]) - _finite(loc_m["signed_pct_at_break"])
            # more negative = worse for the position
            worse = bool(extra_mae < -1e-12) and (not bool(eff_m["entry_close_recovery_after_break"]))

        # research PnL: exit at local vs effective break close; if never break, final close
        def exit_pnl(m: dict[str, Any]) -> float:
            if m["broke"]:
                return float(m["signed_pct_at_break"])
            return float(m["final_signed_pct"])

        pref = f"{scope}__"
        row.update(
            {
                f"{pref}local_broke": bool(loc_m["broke"]),
                f"{pref}effective_broke": bool(eff_m["broke"]),
                f"{pref}local_break_bar": loc_m["break_bar"],
                f"{pref}effective_break_bar": eff_m["break_bar"],
                f"{pref}bars_local_to_effective_break": bars_gap,
                f"{pref}avoided_effective_break": avoided,
                f"{pref}delayed_effective_break": delayed,
                f"{pref}rescued": rescued,
                f"{pref}carry_worse": worse,
                f"{pref}local_signed_at_break": loc_m["signed_pct_at_break"],
                f"{pref}effective_signed_at_break": eff_m["signed_pct_at_break"],
                f"{pref}extra_loss_local_to_effective_pct": extra_mae,
                f"{pref}add_adverse_after_local_pct": loc_m["add_adverse_after_break_pct"],
                f"{pref}add_adverse_after_effective_pct": eff_m["add_adverse_after_break_pct"],
                f"{pref}entry_rec_after_local": bool(loc_m["entry_close_recovery_after_break"]),
                f"{pref}entry_rec_after_effective": bool(eff_m["entry_close_recovery_after_break"]),
                f"{pref}mfe_after_local_pct": loc_m["mfe_pct_after_break"],
                f"{pref}pnl_exit_local_pct": exit_pnl(loc_m),
                f"{pref}pnl_exit_effective_pct": exit_pnl(eff_m),
                f"{pref}pnl_delta_effective_minus_local_pct": exit_pnl(eff_m) - exit_pnl(loc_m),
            }
        )
    return row


def load_setups(fills: pd.DataFrame) -> list[SetupCarry]:
    f = fills.sort_values("fill_bar").reset_index(drop=True)
    out: list[SetupCarry] = []
    for _, r in f.iterrows():
        side = int(r["side"])
        out.append(
            SetupCarry(
                setup_id=int(r["setup_id"]),
                direction=str(r["direction"]),
                side=side,
                fill_bar=int(r["fill_bar"]),
                fill_timestamp=r["fill_timestamp"],
                entry_price=float(r["entry_price"]),
                local_protected=float(r["entry_protected_level"]),
                atr=_finite(r.get("frozen_atr_14")),
                frozen_ltf=int(r.get("frozen_ltf_major_at_fill") or 0),
                frozen_htf=int(r.get("frozen_htf_major_at_fill") or 0),
            )
        )
    return out


def summarize_variant(df: pd.DataFrame, *, variant: str, group: str) -> dict[str, Any]:
    sub = df[df["variant"] == variant]
    if group in ("long", "short"):
        sub = sub[sub["direction"] == group]
    n = len(sub)
    row: dict[str, Any] = {
        "variant": variant,
        "group": group,
        "n_setups": n,
        "n_carried": int(sub["carried_from_prev"].sum()) if n else 0,
        "n_effective_wider": int(sub["effective_wider_than_local"].sum()) if n else 0,
    }
    for scope, _ in HORIZONS:
        pref = f"{scope}__"
        if n == 0:
            continue
        local_n = int(sub[f"{pref}local_broke"].sum())
        eff_n = int(sub[f"{pref}effective_broke"].sum())
        avoided = int(sub[f"{pref}avoided_effective_break"].sum())
        delayed = int(sub[f"{pref}delayed_effective_break"].sum())
        rescued = int(sub[f"{pref}rescued"].sum())
        worse = int(sub[f"{pref}carry_worse"].sum())
        delta = sub[f"{pref}pnl_delta_effective_minus_local_pct"].astype(float)
        extra = sub.loc[sub[f"{pref}delayed_effective_break"], f"{pref}extra_loss_local_to_effective_pct"]
        row.update(
            {
                f"{pref}local_breaks": local_n,
                f"{pref}effective_breaks": eff_n,
                f"{pref}avoided_exits": avoided,
                f"{pref}delayed_exits": delayed,
                f"{pref}rescued": rescued,
                f"{pref}carry_worse": worse,
                f"{pref}entry_rec_after_local_rate": _safe_rate(
                    int(sub[f"{pref}entry_rec_after_local"].sum()), max(local_n, 1)
                )
                if local_n
                else None,
                f"{pref}median_bars_local_to_effective": _median(
                    sub.loc[sub[f"{pref}delayed_effective_break"], f"{pref}bars_local_to_effective_break"]
                    .dropna()
                    .astype(float)
                    .tolist()
                ),
                f"{pref}median_extra_loss_pct": _median(extra.astype(float).tolist()),
                f"{pref}p75_extra_loss_pct": _quantile(extra.astype(float).tolist(), 0.25),  # more neg
                f"{pref}worst_extra_loss_pct": float(extra.min()) if len(extra) else None,
                f"{pref}median_pnl_delta_pct": _median(delta.tolist()),
                f"{pref}mean_pnl_delta_pct": float(np.mean(delta)) if n else None,
                f"{pref}total_pnl_delta_pct": float(delta.sum()) if n else None,
            }
        )
    return row


def build_recommendation(summary: pd.DataFrame) -> list[dict[str, Any]]:
    """Compact recommendation rows using h24 + full as primary scopes."""
    rows = []
    for v in VARIANTS:
        s = summary[(summary["variant"] == v) & (summary["group"] == "all")]
        if s.empty:
            continue
        r = s.iloc[0]
        avoided = int(r.get("h24__avoided_exits") or 0) + int(r.get("h24__delayed_exits") or 0)
        rescued = int(r.get("h24__rescued") or 0)
        worse = int(r.get("h24__carry_worse") or 0)
        extra = r.get("h24__median_extra_loss_pct")
        pnl = r.get("h24__median_pnl_delta_pct")
        full_rescued = int(r.get("full__rescued") or 0)
        # heuristic score
        score = rescued * 2 + avoided - worse * 2
        if pnl is not None and math.isfinite(float(pnl)):
            score += 1 if float(pnl) > 0 else (-1 if float(pnl) < 0 else 0)
        verdict = "neutral"
        if v == "V0":
            verdict = "baseline"
        elif v == "V_1LAG":
            verdict = "recommended"
        elif v == "V_2LAG":
            verdict = "comparison_only"
        elif v in HIST_VARIANTS:
            verdict = "historical_maxmin_not_recommended"
        elif rescued >= worse and (pnl is None or float(pnl) >= -0.05):
            verdict = "promising"
        elif worse > rescued:
            verdict = "risky"
        rows.append(
            {
                "variant": v,
                "avoided_or_delayed_h24": avoided,
                "rescued_h24": rescued,
                "rescued_full": full_rescued,
                "carry_worse_h24": worse,
                "median_extra_mae_h24_pct": extra,
                "median_pnl_delta_h24_pct": pnl,
                "score_heuristic": score,
                "verdict": verdict,
            }
        )
    return rows


def _pine_float(x: Any) -> str:
    v = _finite(x)
    return "na" if not math.isfinite(v) else repr(float(v))


def _pine_int(x: Any) -> str:
    return str(int(x))


def _ts(ts: Any) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return f"timestamp({t.year}, {t.month}, {t.day}, {t.hour}, {t.minute})"



def build_carry_pine(per: pd.DataFrame, *, variant: str = "V_1LAG") -> str:
    """Visualize local / previous / effective for one-level-lag."""
    sub = per[per["variant"] == variant].sort_values("fill_bar").reset_index(drop=True)
    if sub.empty:
        raise RuntimeError(f"no rows for variant {variant}")
    n = len(sub)
    sides = sub["side"].astype(int).tolist()
    prev_vals = []
    for _, r in sub.iterrows():
        pv = r.get("previous_local_protected_level")
        prev_vals.append(_finite(pv) if pd.notna(pv) else float("nan"))

    lines = [
        *build_pine_header(f"C3.5D Protected Carry Audit ({variant})"),
        "// RESEARCH ONLY — ONE-LEVEL-LAG: effective[n]=local[n-1] (same leg).",
        "// NOT max/min chain carry. Local break=warning; effective break=hard invalidation.",
        f"// Variant={variant}. No live exits.",
        f"nSetups = {n}",
        'maxVisible = input.int(20, "Max visible trades", minval=1, maxval=112)',
        'lineHorizonBars = input.int(96, "Line length (bars)", minval=8, maxval=500)',
        'dirFilter = input.string("all", "Direction", options=["all", "long", "short"])',
        'showFilter = input.string("all", "Show", options=["all", "break_only", "carried_only"])',
        'showLocal = input.bool(true, "Show LOCAL protected (red thin)")',
        'showPrevious = input.bool(true, "Show PREVIOUS local (orange dashed)")',
        'showEffective = input.bool(true, "Show EFFECTIVE protected (maroon thick)")',
        'showEntryMarkers = input.bool(true, "Show entry markers")',
        'showBreakMarkers = input.bool(true, "Show LOCAL/EFFECTIVE break markers")',
        "",
        f"setupIds = array.from({', '.join(_pine_int(x) for x in sub['setup_id'])})",
        f"sides = array.from({', '.join(_pine_int(x) for x in sides)})",
        f"fillTimes = array.from({', '.join(_ts(x) for x in sub['fill_timestamp'])})",
        f"entryPx = array.from({', '.join(_pine_float(x) for x in sub['entry_price'])})",
        f"localProt = array.from({', '.join(_pine_float(x) for x in sub['local_protected_level'])})",
        f"prevProt = array.from({', '.join(_pine_float(x) for x in prev_vals)})",
        f"effProt = array.from({', '.join(_pine_float(x) for x in sub['effective_protected_level'])})",
        f"carried = array.from({', '.join('1' if bool(x) else '0' for x in sub['carried_from_prev'])})",
        f"carryDepth = array.from({', '.join(_pine_int(x) for x in sub['carry_depth'])})",
        f"sameLeg = array.from({', '.join('1' if bool(x) else '0' for x in sub['same_leg'])})",
        f"originIds = array.from({', '.join(_pine_int(x) if pd.notna(x) else '0' for x in sub['carry_source_setup_id'])})",
        f"hasLocalBrH24 = array.from({', '.join('1' if bool(x) else '0' for x in sub['h24__local_broke'])})",
        f"hasEffBrH24 = array.from({', '.join('1' if bool(x) else '0' for x in sub['h24__effective_broke'])})",
        "",
        "var line[] localLines = array.new_line()",
        "var line[] prevLines = array.new_line()",
        "var line[] effLines = array.new_line()",
        "var label[] labs = array.new_label()",
        "var bool drawn = false",
        "barOffsetMs = timeframe.in_seconds() * 1000",
        "",
        "clearAll() =>",
        "    if array.size(localLines) > 0",
        "        for j = 0 to array.size(localLines) - 1",
        "            line.delete(array.get(localLines, j))",
        "        array.clear(localLines)",
        "    if array.size(prevLines) > 0",
        "        for j = 0 to array.size(prevLines) - 1",
        "            line.delete(array.get(prevLines, j))",
        "        array.clear(prevLines)",
        "    if array.size(effLines) > 0",
        "        for j = 0 to array.size(effLines) - 1",
        "            line.delete(array.get(effLines, j))",
        "        array.clear(effLines)",
        "    if array.size(labs) > 0",
        "        for j = 0 to array.size(labs) - 1",
        "            label.delete(array.get(labs, j))",
        "        array.clear(labs)",
        "",
        "passDir(i) =>",
        "    side = array.get(sides, i)",
        "    dirFilter == 'all' or (dirFilter == 'long' and side > 0) or (dirFilter == 'short' and side < 0)",
        "",
        "passShow(i) =>",
        "    showFilter == 'all' or (showFilter == 'break_only' and (array.get(hasLocalBrH24, i) == 1 or array.get(hasEffBrH24, i) == 1)) or (showFilter == 'carried_only' and array.get(carried, i) == 1)",
        "",
        "drawSetup(i) =>",
        "    if passDir(i) and passShow(i)",
        "        t0 = array.get(fillTimes, i)",
        "        t1 = t0 + lineHorizonBars * barOffsetMs",
        "        side = array.get(sides, i)",
        "        ep = array.get(entryPx, i)",
        "        loc = array.get(localProt, i)",
        "        prv = array.get(prevProt, i)",
        "        eff = array.get(effProt, i)",
        "        sid = array.get(setupIds, i)",
        "        src = array.get(originIds, i)",
        "        longSide = side > 0",
        "        if showEntryMarkers",
        "            et = longSide ? 'LONG ENTRY #' + str.tostring(sid) : 'SHORT ENTRY #' + str.tostring(sid)",
        "            array.push(labs, label.new(t0, ep, et, xloc=xloc.bar_time, style=longSide ? label.style_label_up : label.style_label_down, color=longSide ? color.teal : color.fuchsia, textcolor=color.white, size=size.small))",
        "        if showLocal and not na(loc)",
        "            array.push(localLines, line.new(t0, loc, t1, loc, xloc=xloc.bar_time, color=color.red, width=1, style=line.style_solid))",
        "            array.push(labs, label.new(t0 + barOffsetMs, loc, 'LOCAL PROT', xloc=xloc.bar_time, style=label.style_none, textcolor=color.red, size=size.tiny))",
        "        if showPrevious and not na(prv)",
        "            array.push(prevLines, line.new(t0, prv, t1, prv, xloc=xloc.bar_time, color=color.orange, width=1, style=line.style_dashed))",
        "        if showEffective and not na(eff)",
        "            array.push(effLines, line.new(t0, eff, t1, eff, xloc=xloc.bar_time, color=color.maroon, width=2, style=line.style_solid))",
        "            if array.get(carried, i) == 1",
        "                array.push(labs, label.new(t0, eff, 'EFFECTIVE PROT from #' + str.tostring(src), xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.maroon, 15), textcolor=color.white, size=size.tiny))",
        "            else",
        "                array.push(labs, label.new(t0, eff, 'EFFECTIVE PROT (=local)', xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.maroon, 40), textcolor=color.white, size=size.tiny))",
        "        if showBreakMarkers and array.get(hasLocalBrH24, i) == 1",
        "            array.push(labs, label.new(t0 + 2 * barOffsetMs, loc, 'LOCAL BREAK', xloc=xloc.bar_time, style=label.style_label_down, color=color.orange, textcolor=color.black, size=size.tiny))",
        "        if showBreakMarkers and array.get(hasEffBrH24, i) == 1",
        "            array.push(labs, label.new(t0 + 4 * barOffsetMs, eff, 'EFFECTIVE BREAK', xloc=xloc.bar_time, style=label.style_label_down, color=color.red, textcolor=color.white, size=size.tiny))",
        "",
        "if barstate.islastconfirmedhistory",
        "    if not drawn",
        "        clearAll()",
        "        startIdx = math.max(0, nSetups - maxVisible)",
        "        if startIdx <= nSetups - 1",
        "            for i = startIdx to nSetups - 1",
        "                drawSetup(i)",
        "        drawn := true",
        "",
        "plot(array.get(setupIds, nSetups - 1), 'setup_id', display=display.data_window)",
        "plot(array.get(sides, nSetups - 1), 'direction_side', display=display.data_window)",
        "plot(array.get(localProt, nSetups - 1), 'local_protected', display=display.data_window)",
        "plot(array.get(effProt, nSetups - 1), 'effective_protected', display=display.data_window)",
        "plot(array.get(originIds, nSetups - 1), 'carry_source_setup_id', display=display.data_window)",
        "plot(array.get(sameLeg, nSetups - 1), 'same_leg', display=display.data_window)",
        "plot(array.get(carryDepth, nSetups - 1), 'carry_depth', display=display.data_window)",
        "",
    ]
    text_out = "\n".join(lines) + "\n"
    validate_pine_script(text_out)
    return text_out


def run_audit(
    *,
    apt_dir: Path = DEFAULT_APT_DIR,
    output_dir: Path = DEFAULT_OUT,
    pine_dir: Path = PINE_OUT,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    apt_dir = Path(apt_dir)
    output_dir = Path(output_dir)
    pine_dir = Path(pine_dir)
    if output_dir.resolve() == apt_dir.resolve():
        raise RuntimeError("refusing to write into apt_audit root")
    output_dir.mkdir(parents=True, exist_ok=True)
    pine_dir.mkdir(parents=True, exist_ok=True)

    fills = pd.read_csv(apt_dir / "fills.csv")
    if frame is None:
        frame, _, meta = build_apt_d1_frame()
    else:
        meta = {}
    ohlc = ensure_ohlc(frame)
    setups = load_setups(fills)
    assign_effective_levels(setups, ohlc)

    rows = []
    for s in setups:
        for v in VARIANTS:
            rows.append(evaluate_setup_variant(s, ohlc, variant=v))
    per = pd.DataFrame(rows)

    summary_rows = []
    for v in VARIANTS:
        for g in ("all", "long", "short"):
            summary_rows.append(summarize_variant(per, variant=v, group=g))
    summary = pd.DataFrame(summary_rows)
    rec = pd.DataFrame(build_recommendation(summary))

    chain_rows = []
    by_id = {s.setup_id: s for s in setups}
    for s in setups:
        for v in ("V_1LAG", "V_2LAG", "V1", "V2", "V4_48"):
            chain_rows.append(
                {
                    "variant": v,
                    "setup_id": s.setup_id,
                    "direction": s.direction,
                    "fill_timestamp": str(s.fill_timestamp),
                    "local_protected_level": s.local_protected,
                    "previous_local_protected_level": s.previous_local_by_variant.get(v),
                    "effective_protected_level": s.effective_by_variant[v],
                    "carry_source_setup_id": s.carry_origin_by_variant.get(v),
                    "carry_depth": s.carry_depth_by_variant.get(v),
                    "carried_from_prev": s.carried_from_prev_by_variant.get(v),
                    "same_leg": s.same_leg_by_variant.get(v),
                    "reset_reason": s.reset_reason_by_variant.get(v),
                    "source_local": (
                        by_id[int(s.carry_origin_by_variant[v])].local_protected
                        if s.carry_origin_by_variant.get(v) in by_id
                        else None
                    ),
                }
            )
    chains = pd.DataFrame(chain_rows)

    per.to_csv(output_dir / "protected_carry_per_fill.csv", index=False)
    summary.to_csv(output_dir / "protected_carry_variant_summary.csv", index=False)
    rec.to_csv(output_dir / "protected_carry_recommendation.csv", index=False)
    chains.to_csv(output_dir / "protected_carry_chains.csv", index=False)

    v1lag = per[per["variant"] == "V_1LAG"].copy()
    v1lag[
        [
            "setup_id",
            "direction",
            "local_protected_level",
            "previous_local_protected_level",
            "effective_protected_level",
            "carry_source_setup_id",
            "carry_depth",
            "same_leg",
            "reset_reason",
            "h24__local_broke",
            "h24__effective_broke",
            "h24__avoided_effective_break",
            "h24__delayed_effective_break",
            "h24__rescued",
            "h24__carry_worse",
            "h24__pnl_delta_effective_minus_local_pct",
            "full__rescued",
            "full__carry_worse",
        ]
    ].to_csv(output_dir / "protected_carry_v1lag_warning_vs_invalidation.csv", index=False)

    pine = build_carry_pine(per, variant="V_1LAG")
    pine_path = pine_dir / MAIN_PINE
    pine_path.write_text(pine, encoding="utf-8")

    example = chains[(chains["variant"] == "V_1LAG") & (chains["direction"] == "short")].tail(20)

    audit = {
        "phase": PHASE,
        "status": "OK",
        "n_fills": len(setups),
        "variants": list(VARIANTS),
        "primary_policy": "V_1LAG",
        "definitions": {
            "V0": "effective=local always",
            "V_1LAG": "effective[n]=local[n-1] same-leg same-direction; carry_depth 0|1; NO max/min chain",
            "V_2LAG": "effective[n]=local[n-2] (comparison)",
            "V1/V2/V4_*": "HISTORICAL max/min outer chain — not recommended",
            "local_break": "warning",
            "effective_break": "hard invalidation",
        },
        "recommendation": rec.to_dict(orient="records"),
        "example_short_v1lag_tail": example.to_dict(orient="records"),
        "pine_path": str(pine_path),
        "output_dir": str(output_dir),
        "data_meta": {k: meta[k] for k in meta if k != "frame15_meta"} if meta else {},
        "no_runtime_change": True,
        "no_live_bot": True,
        "no_commit": True,
        "parent_artifacts_unmodified": True,
        "v1lag_uses_maxmin_chain": False,
    }
    (output_dir / "protected_carry_summary.json").write_text(
        json.dumps(json_safe(audit), indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "README.md").write_text(
        "\n".join(
            [
                "# C3.5D Protected Carry-Forward Audit",
                "",
                "Primary: **V_1LAG** one-level lag (`effective = previous local`).",
                "Not max/min chain carry.",
                "",
                f"- Fills: `{len(setups)}`",
                f"- Pine: `{pine_path}`",
                "",
                "No runtime/bot changes. Parent APT artifacts not mutated.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return audit


def main() -> None:
    p = argparse.ArgumentParser(description="C3.5D protected carry-forward audit")
    p.add_argument("--apt-dir", type=Path, default=DEFAULT_APT_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--pine-dir", type=Path, default=PINE_OUT)
    args = p.parse_args()
    audit = run_audit(apt_dir=args.apt_dir, output_dir=args.output_dir, pine_dir=args.pine_dir)
    print(json.dumps(json_safe(audit), indent=2))


if __name__ == "__main__":
    main()
