"""C2 all-112 fills evaluation (offline research-only).

Compares BASE_ORIGINAL (D2 h24 horizon exit), B0, M1, and C2 on the same
112 APT fills. Management activates only when a V_1LAG local protected break
occurs at or before the original horizon exit; otherwise the original exit
is kept unchanged.

No new entries, no new timeouts, no -0.25R, no runtime changes, no commit.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5d_apt_raw_audit import build_apt_d1_frame
from research.regime_scanner.pullback_entry_c3_5d_protected_break_exit_management_audit import (
    PathEvents,
    apply_fees_pct,
    bar_ohlc,
    pnl_pct_from_price,
    pnl_r_from_pct,
    reclaim_local,
    risk_unit,
    simulate_B0,
    simulate_M1_local_reclaim,
)
from research.regime_scanner.pullback_entry_c3_5d_protected_carry_audit import (
    SetupCarry,
    assign_effective_levels,
    ensure_ohlc,
    first_close_break_bar,
    load_setups,
)
from research.regime_scanner.pullback_entry_c3_5d_reclaim_fallback_audit import (
    simulate_reclaim_fallback,
)

PHASE = "C3.5D_C2_ALL_112"
DEFAULT_APT_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
DEFAULT_OUT = DEFAULT_APT_DIR / "reclaim_fallback"
HORIZON_BARS = 24  # D2: fill .. fill+(HORIZON_BARS-1)
FEE_BPS = (0, 10, 20)
FILL_MODES = ("close_only", "conservative_intrabar")
CANDIDATES = ("BASE_ORIGINAL", "B0", "M1", "C2")
EPS = 1e-12


@dataclass
class FillContext:
    setup: SetupCarry
    original_exit_bar: int
    original_exit_price: float
    original_exit_time: Any
    original_pnl_gross_pct: float
    local_break_bar: int | None
    effective_break_bar: int | None
    local: float
    effective: float
    r_unit: float
    carry_source: int | None
    leg_id: int
    delayed_full: bool
    h24_local: bool
    h24_delayed: bool
    activatable: bool


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _mean(xs: list[float]) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.mean(vals)) if vals else None


def _median(xs: list[float]) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.median(vals)) if vals else None


def _q(xs: list[float], q: float) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.quantile(vals, q)) if vals else None


def original_exit_bar(fill_bar: int) -> int:
    return int(fill_bar) + (HORIZON_BARS - 1)


def build_contexts(setups: list[SetupCarry], ohlc: pd.DataFrame) -> list[FillContext]:
    assign_effective_levels(setups, ohlc)
    data_end = int(ohlc.index.max())
    out: list[FillContext] = []
    for s in setups:
        local = float(s.local_protected)
        eff = float(s.effective_by_variant["V_1LAG"])
        oe = min(original_exit_bar(s.fill_bar), data_end)
        if oe not in ohlc.index:
            oe = int(min(ohlc.index, key=lambda b: abs(int(b) - oe)))
        _, _, oc = bar_ohlc(ohlc, oe)
        orig_pnl = pnl_pct_from_price(side=s.side, entry=s.entry_price, exit_px=oc)
        ts = ohlc.loc[oe, "timestamp"] if "timestamp" in ohlc.columns else None

        lb = first_close_break_bar(
            ohlc, side=s.side, fill_bar=s.fill_bar, end_bar=data_end, level=local
        )
        eb = first_close_break_bar(
            ohlc, side=s.side, fill_bar=s.fill_bar, end_bar=data_end, level=eff
        )
        h24_cap = original_exit_bar(s.fill_bar)
        h24_local = lb is not None and int(lb) <= h24_cap
        delayed_full = lb is not None and (eb is None or int(eb) > int(lb))
        h24_delayed = bool(
            h24_local and delayed_full and lb is not None and (eb is None or int(eb) > int(lb))
        )
        activatable = lb is not None and int(lb) <= oe

        out.append(
            FillContext(
                setup=s,
                original_exit_bar=oe,
                original_exit_price=float(oc),
                original_exit_time=ts,
                original_pnl_gross_pct=float(orig_pnl),
                local_break_bar=lb,
                effective_break_bar=eb,
                local=local,
                effective=eff,
                r_unit=risk_unit(entry=s.entry_price, local=local, atr=_finite(s.atr)),
                carry_source=s.carry_origin_by_variant.get("V_1LAG"),
                leg_id=int(s.leg_id_by_variant.get("V_1LAG") or 0),
                delayed_full=bool(delayed_full),
                h24_local=bool(h24_local),
                h24_delayed=bool(h24_delayed),
                activatable=bool(activatable),
            )
        )
    return out


def _path_events(ctx: FillContext) -> PathEvents:
    s = ctx.setup
    eb = ctx.effective_break_bar
    lb = ctx.local_break_bar
    # Only future effective breaks matter inside the management window.
    # If effective==local, first break bar equals local break; that is not a
    # later hard stop and must not collapse the timeout window to empty.
    if eb is not None and lb is not None and int(eb) <= int(lb):
        eb = None
    if eb is not None and int(eb) > ctx.original_exit_bar:
        eb = None
    return PathEvents(
        setup_id=s.setup_id,
        direction=s.direction,
        side=s.side,
        fill_bar=s.fill_bar,
        fill_timestamp=s.fill_timestamp,
        entry_price=s.entry_price,
        atr=_finite(s.atr),
        local=ctx.local,
        effective=ctx.effective,
        carry_source_setup_id=ctx.carry_source,
        leg_id=ctx.leg_id,
        r_unit=ctx.r_unit,
        local_break_bar=ctx.local_break_bar,
        effective_break_bar=eb,
        data_end=ctx.original_exit_bar,
    )


def simulate_candidate(
    ohlc: pd.DataFrame,
    ctx: FillContext,
    *,
    candidate: str,
    fill_mode: str,
) -> dict[str, Any]:
    if candidate == "BASE_ORIGINAL" or not ctx.activatable:
        return {
            "candidate": candidate,
            "candidate_activated": False,
            "managed_exit_bar": ctx.original_exit_bar,
            "managed_exit_price": ctx.original_exit_price,
            "managed_exit_reason": "original_exit",
            "bars_held_after_local": None,
            "pnl_gross_pct": ctx.original_pnl_gross_pct,
        }

    ev = _path_events(ctx)
    assert ev.local_break_bar is not None
    lb = int(ev.local_break_bar)

    if candidate == "B0":
        res = simulate_B0(ohlc, ev, fill_mode)
        exit_bar, exit_px, pnl, reason = res.exit_bar, res.exit_price, res.pnl_pct_gross, "local_break"
    elif candidate == "M1":
        res = simulate_M1_local_reclaim(ohlc, ev, fill_mode)
        exit_bar, exit_px, pnl, reason = res.exit_bar, res.exit_price, res.pnl_pct_gross, res.exit_reason
        if reason not in ("local_reclaim", "effective_break"):
            exit_bar = ctx.original_exit_bar
            exit_px = ctx.original_exit_price
            pnl = ctx.original_pnl_gross_pct
            reason = "horizon_end"
    elif candidate == "C2":
        res = simulate_reclaim_fallback(
            ohlc, ev, name="C2", timeout_bars=3, uses_minus_025r=False, fill_mode=fill_mode
        )
        exit_bar, exit_px, pnl, reason = res.exit_bar, res.exit_price, res.pnl_pct_gross, res.exit_reason
        if reason in ("data_end",):
            reason = "horizon_end"
        if exit_bar is not None and int(exit_bar) > ctx.original_exit_bar:
            exit_bar = ctx.original_exit_bar
            exit_px = ctx.original_exit_price
            pnl = ctx.original_pnl_gross_pct
            reason = "horizon_end"
    else:
        raise ValueError(candidate)

    return {
        "candidate": candidate,
        "candidate_activated": True,
        "managed_exit_bar": exit_bar,
        "managed_exit_price": exit_px,
        "managed_exit_reason": reason,
        "bars_held_after_local": (int(exit_bar) - lb) if exit_bar is not None else None,
        "pnl_gross_pct": float(pnl),
    }


def path_moves(
    ohlc: pd.DataFrame,
    *,
    side: int,
    local_break_bar: int,
    local_break_price: float,
    exit_bar: int,
) -> dict[str, float]:
    fav, adv = [], []
    for bi in range(int(local_break_bar), int(exit_bar) + 1):
        if bi not in ohlc.index:
            continue
        h, l, _ = bar_ohlc(ohlc, bi)
        if side > 0:
            fav.append(pnl_pct_from_price(side=side, entry=local_break_price, exit_px=h))
            adv.append(pnl_pct_from_price(side=side, entry=local_break_price, exit_px=l))
        else:
            fav.append(pnl_pct_from_price(side=side, entry=local_break_price, exit_px=l))
            adv.append(pnl_pct_from_price(side=side, entry=local_break_price, exit_px=h))
    _, _, ec = bar_ohlc(ohlc, int(exit_bar))
    return {
        "max_favorable_move_after_local_pct": float(np.max(fav)) if fav else float("nan"),
        "max_adverse_move_after_local_pct": float(np.min(adv)) if adv else float("nan"),
        "close_move_local_to_exit_pct": pnl_pct_from_price(
            side=side, entry=local_break_price, exit_px=ec
        ),
    }


def enrich_row(
    ctx: FillContext,
    ohlc: pd.DataFrame,
    sim: dict[str, Any],
    *,
    fee_bps: float,
    fill_mode: str,
    by_cand: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    s = ctx.setup
    net = apply_fees_pct(sim["pnl_gross_pct"], fee_bps)
    orig_net = apply_fees_pct(ctx.original_pnl_gross_pct, fee_bps)
    b0_net = apply_fees_pct(by_cand["B0"]["pnl_gross_pct"], fee_bps)
    m1_net = apply_fees_pct(by_cand["M1"]["pnl_gross_pct"], fee_bps)

    lb = ctx.local_break_bar
    lb_px = lb_pnl = lb_r = lb_time = None
    if lb is not None:
        _, _, c = bar_ohlc(ohlc, int(lb))
        lb_px = c
        lb_pnl = pnl_pct_from_price(side=s.side, entry=s.entry_price, exit_px=c)
        lb_r = pnl_r_from_pct(lb_pnl, entry=s.entry_price, r_unit=ctx.r_unit)
        lb_time = ohlc.loc[int(lb), "timestamp"] if "timestamp" in ohlc.columns else None

    rec_bar = rec_px = None
    if lb is not None and ctx.activatable:
        for bi in range(int(lb) + 1, int(ctx.original_exit_bar) + 1):
            if bi not in ohlc.index:
                continue
            _, _, c = bar_ohlc(ohlc, bi)
            if reclaim_local(side=s.side, close=c, local=ctx.local):
                rec_bar, rec_px = bi, c
                break

    eb = ctx.effective_break_bar
    eb_px = None
    eb_in = eb is not None and int(eb) <= ctx.original_exit_bar
    if eb_in:
        _, _, eb_px = bar_ohlc(ohlc, int(eb))

    exit_bar = sim["managed_exit_bar"]
    exit_time = (
        ohlc.loc[int(exit_bar), "timestamp"]
        if exit_bar is not None and "timestamp" in ohlc.columns
        else None
    )

    path = {
        "max_favorable_move_after_local_pct": float("nan"),
        "max_adverse_move_after_local_pct": float("nan"),
        "close_move_local_to_exit_pct": float("nan"),
    }
    if sim["candidate_activated"] and lb is not None and exit_bar is not None and lb_px is not None:
        path = path_moves(
            ohlc,
            side=s.side,
            local_break_bar=int(lb),
            local_break_price=float(lb_px),
            exit_bar=int(exit_bar),
        )

    imp_o = net - orig_net
    imp_b0 = net - b0_net
    imp_m1 = net - m1_net

    return {
        "symbol": "APTUSDT",
        "setup_id": s.setup_id,
        "direction": s.direction,
        "entry_bar": s.fill_bar,
        "entry_time": str(s.fill_timestamp),
        "entry_price": s.entry_price,
        "original_exit_bar": ctx.original_exit_bar,
        "original_exit_time": str(ctx.original_exit_time) if ctx.original_exit_time is not None else None,
        "original_exit_price": ctx.original_exit_price,
        "original_exit_reason": "horizon_end",
        "original_gross_pnl_pct": ctx.original_pnl_gross_pct,
        "original_net_pnl_pct": orig_net,
        "original_net_pnl_r": pnl_r_from_pct(orig_net, entry=s.entry_price, r_unit=ctx.r_unit),
        "local_break_happened": lb is not None,
        "local_break_within_original_horizon": ctx.activatable,
        "local_break_bar": lb,
        "local_break_time": str(lb_time) if lb_time is not None else None,
        "local_break_price": lb_px,
        "local_break_pnl_pct": lb_pnl,
        "local_break_pnl_r": lb_r,
        "local_reclaim_happened": rec_bar is not None,
        "local_reclaim_bar": rec_bar,
        "local_reclaim_price": rec_px,
        "effective_break_happened": bool(eb_in),
        "effective_break_bar": int(eb) if eb_in else None,
        "effective_break_price": eb_px,
        "local_protected_level": ctx.local,
        "effective_protected_level": ctx.effective,
        "carry_source_setup_id": ctx.carry_source,
        "delayed_full_path": ctx.delayed_full,
        "h24_local_break": ctx.h24_local,
        "h24_delayed": ctx.h24_delayed,
        "candidate": sim["candidate"],
        "candidate_activated": bool(sim["candidate_activated"]),
        "managed_exit_bar": exit_bar,
        "managed_exit_time": str(exit_time) if exit_time is not None else None,
        "managed_exit_price": sim["managed_exit_price"],
        "managed_exit_reason": sim["managed_exit_reason"],
        "bars_held_after_local": sim["bars_held_after_local"],
        "gross_pnl_pct": sim["pnl_gross_pct"],
        "net_pnl_pct": net,
        "net_pnl_r": pnl_r_from_pct(net, entry=s.entry_price, r_unit=ctx.r_unit),
        "improvement_vs_original_pct": imp_o,
        "improvement_vs_original_r": pnl_r_from_pct(imp_o, entry=s.entry_price, r_unit=ctx.r_unit),
        "improvement_vs_B0_pct": imp_b0,
        "improvement_vs_B0_r": pnl_r_from_pct(imp_b0, entry=s.entry_price, r_unit=ctx.r_unit),
        "improvement_vs_M1_pct": imp_m1,
        "improvement_vs_M1_r": pnl_r_from_pct(imp_m1, entry=s.entry_price, r_unit=ctx.r_unit),
        "profitable": bool(net > EPS),
        "loss_trade": bool(net < -EPS),
        "better_than_original": bool(imp_o > EPS),
        "equal_to_original": bool(abs(imp_o) <= EPS),
        "worse_than_original": bool(imp_o < -EPS),
        "better_than_B0": bool(imp_b0 > EPS),
        "equal_to_B0": bool(abs(imp_b0) <= EPS),
        "worse_than_B0": bool(imp_b0 < -EPS),
        "better_than_M1": bool(imp_m1 > EPS),
        "equal_to_M1": bool(abs(imp_m1) <= EPS),
        "worse_than_M1": bool(imp_m1 < -EPS),
        "fee_bps": fee_bps,
        "fill_semantics": fill_mode,
        **path,
    }


def strategy_metrics(df: pd.DataFrame, *, label: str) -> dict[str, Any]:
    g = df.sort_values(["entry_bar", "setup_id"]).reset_index(drop=True)
    n = len(g)
    if n == 0:
        return {"label": label, "n_total": 0}
    pnl = g["net_pnl_pct"].astype(float)
    wins = pnl[pnl > EPS]
    losses = pnl[pnl < -EPS]
    max_cl = max_cw = cur_l = cur_w = 0
    for v in pnl.tolist():
        if v < -EPS:
            cur_l += 1
            cur_w = 0
            max_cl = max(max_cl, cur_l)
        elif v > EPS:
            cur_w += 1
            cur_l = 0
            max_cw = max(max_cw, cur_w)
        else:
            cur_l = cur_w = 0
    cum = np.cumsum(pnl.values)
    peak = np.maximum.accumulate(cum)
    dd = cum - peak
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(losses.sum()) if len(losses) else 0.0
    pf = (gp / abs(gl)) if gl < 0 else (float("inf") if gp > 0 else None)
    hold = g["bars_held_after_local"].dropna().astype(float)
    return {
        "label": label,
        "candidate": str(g["candidate"].iloc[0]),
        "n_total": n,
        "n_candidate_activated": int(g["candidate_activated"].sum()),
        "n_candidate_not_activated": int((~g["candidate_activated"].astype(bool)).sum()),
        "profitable_trades": int((pnl > EPS).sum()),
        "losing_trades": int((pnl < -EPS).sum()),
        "breakeven_trades": int((pnl.abs() <= EPS).sum()),
        "win_rate": float((pnl > EPS).mean()),
        "mean_net_pnl_pct": _mean(pnl.tolist()),
        "median_net_pnl_pct": _median(pnl.tolist()),
        "sum_net_pnl_pct": float(pnl.sum()),
        "mean_net_pnl_r": _mean(g["net_pnl_r"].tolist()),
        "median_net_pnl_r": _median(g["net_pnl_r"].tolist()),
        "sum_net_pnl_r": float(np.nansum(g["net_pnl_r"].astype(float))),
        "profit_factor": pf,
        "gross_profit": gp,
        "gross_loss": gl,
        "average_win_pct": _mean(wins.tolist()) if len(wins) else None,
        "average_loss_pct": _mean(losses.tolist()) if len(losses) else None,
        "median_win_pct": _median(wins.tolist()) if len(wins) else None,
        "median_loss_pct": _median(losses.tolist()) if len(losses) else None,
        "best_trade_pct": float(pnl.max()),
        "worst_trade_pct": float(pnl.min()),
        "max_drawdown_trade_sequence_pct": float(dd.min()),
        "max_consecutive_losses": max_cl,
        "max_consecutive_wins": max_cw,
        "p10_pnl_pct": _q(pnl.tolist(), 0.10),
        "p25_pnl_pct": _q(pnl.tolist(), 0.25),
        "p75_pnl_pct": _q(pnl.tolist(), 0.75),
        "p90_pnl_pct": _q(pnl.tolist(), 0.90),
        "mean_hold_bars": _mean(hold.tolist()),
        "median_hold_bars": _median(hold.tolist()),
    }


def comparison_block(c2: pd.DataFrame, col: str, name: str) -> dict[str, Any]:
    imp = c2[col].astype(float)
    return {
        "vs": name,
        "better": int((imp > EPS).sum()),
        "equal": int((imp.abs() <= EPS).sum()),
        "worse": int((imp < -EPS).sum()),
        "mean_improvement": _mean(imp.tolist()),
        "median_improvement": _median(imp.tolist()),
        "sum_improvement": float(imp.sum()),
        "largest_improvement": float(imp.max()) if len(imp) else None,
        "largest_deterioration": float(imp.min()) if len(imp) else None,
    }


def activation_categories(c2_act: pd.DataFrame, orig: pd.DataFrame) -> dict[str, Any]:
    m = c2_act.merge(
        orig[["setup_id", "net_pnl_pct"]].rename(columns={"net_pnl_pct": "orig_net"}),
        on="setup_id",
    )
    c = m["net_pnl_pct"].astype(float)
    o = m["orig_net"].astype(float)
    return {
        "n_local_break_activated": len(m),
        "n_local_reclaim_exit": int((m["managed_exit_reason"] == "local_reclaim").sum()),
        "n_timeout_exit": int((m["managed_exit_reason"] == "timeout").sum()),
        "n_effective_break_exit": int((m["managed_exit_reason"] == "effective_break").sum()),
        "n_horizon_end_exit": int((m["managed_exit_reason"] == "horizon_end").sum()),
        "n_local_break_exit_reason": int((m["managed_exit_reason"] == "local_break").sum()),
        "c2_positive_net": int((c > EPS).sum()),
        "c2_negative_net": int((c < -EPS).sum()),
        "rescued_neg_to_pos": int(((o < -EPS) & (c > EPS)).sum()),
        "damaged_pos_to_neg": int(((o > EPS) & (c < -EPS)).sum()),
        "loss_reduced": int(((o < -EPS) & (c < -EPS) & (c > o + EPS)).sum()),
        "loss_worsened": int(((o < -EPS) & (c < -EPS) & (c < o - EPS)).sum()),
        "gain_improved": int(((o > EPS) & (c > EPS) & (c > o + EPS)).sum()),
        "gain_cut": int(((o > EPS) & (c > EPS) & (c < o - EPS)).sum()),
    }


def build_recommendation(**kw: Any) -> dict[str, Any]:
    n_act = int(kw["n_act"])
    c2_sum = float(kw["c2_sum"])
    orig_sum = float(kw["orig_sum"])
    c2_med = kw["c2_med"]
    orig_med = kw["orig_med"]
    better_o = int(kw["better_o"])
    worse_o = int(kw["worse_o"])
    rescued = int(kw["rescued"])
    damaged = int(kw["damaged"])
    loss_reduced = int(kw["loss_reduced"])
    c2_wins = int(kw["c2_wins"])
    orig_wins = int(kw["orig_wins"])

    improves_total = (c2_sum > orig_sum + EPS) and (
        c2_med is not None and orig_med is not None and float(c2_med) >= float(orig_med) - EPS
    )
    improves_subset = better_o > worse_o
    reduces_losses = loss_reduced > damaged
    more_profitable = c2_wins > orig_wins

    if damaged > 0 and c2_sum <= orig_sum + EPS:
        status = "REJECT_C2"
        reason = (
            f"C2 converts {damaged} winner(s) to losers and does not improve total system PnL."
        )
        multi = False
    elif improves_total and damaged == 0 and n_act >= 15:
        status = "PROMISING_FOR_MULTI_SYMBOL_VALIDATION"
        reason = "C2 improves total-system sum/median without winner→loser conversions."
        multi = True
    elif improves_total and damaged == 0:
        status = "RESEARCH_ONLY"
        reason = (
            f"C2 modestly improves total PnL on APT (activated n={n_act}) without "
            "winner→loser conversions; still too small for runtime."
        )
        multi = n_act >= 10
    elif improves_subset and not improves_total:
        status = "RESEARCH_ONLY"
        reason = (
            "C2 helps some activated local-break cases but does not improve the full 112-trade system."
        )
        multi = False
    else:
        status = "REJECT_C2"
        reason = "C2 does not improve the full 112-trade system under close_only/10bps."
        multi = False

    return {
        "recommended_status": status,
        "sample_size_total": int(kw["n_total"]),
        "sample_size_local_break": n_act,
        "sample_size_local_break_ever": int(kw["n_lb_ever"]),
        "sample_size_c2_activated": n_act,
        "best_candidate": "C2",
        "runtime_change_recommended": False,
        "multi_symbol_validation_recommended": multi,
        "c2_improves_total_system": bool(improves_total),
        "c2_improves_activated_subset": bool(improves_subset),
        "c2_reduces_losses": bool(reduces_losses),
        "c2_creates_more_profitable_trades": bool(more_profitable),
        "rescued_neg_to_pos": rescued,
        "damaged_pos_to_neg": damaged,
        "reason": reason,
        "sample_size_warning": n_act < 30,
        "v1lag_semantics_unchanged": True,
        "historical_maxmin_chain_used": False,
        "uses_future_information": False,
        "activation_rule": "local_break_bar <= original_h24_exit_bar; else original exit unchanged",
        "original_exit_definition": f"D2 horizon close at fill_bar+{HORIZON_BARS - 1}",
        "phase": PHASE,
    }


def run_audit(
    *,
    apt_dir: Path = DEFAULT_APT_DIR,
    output_dir: Path = DEFAULT_OUT,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    apt_dir = Path(apt_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fills = pd.read_csv(apt_dir / "fills.csv")
    if frame is None:
        frame, _, meta = build_apt_d1_frame()
    else:
        meta = {}
    ohlc = ensure_ohlc(frame)
    setups = load_setups(fills)
    contexts = build_contexts(setups, ohlc)

    rows: list[dict[str, Any]] = []
    for ctx in contexts:
        for fill_mode in FILL_MODES:
            by_cand = {
                cand: simulate_candidate(ohlc, ctx, candidate=cand, fill_mode=fill_mode)
                for cand in CANDIDATES
            }
            for fee in FEE_BPS:
                for cand in CANDIDATES:
                    rows.append(
                        enrich_row(
                            ctx,
                            ohlc,
                            by_cand[cand],
                            fee_bps=float(fee),
                            fill_mode=fill_mode,
                            by_cand=by_cand,
                        )
                    )

    per = pd.DataFrame(rows).sort_values(
        ["setup_id", "candidate", "fill_semantics", "fee_bps"]
    ).reset_index(drop=True)

    primary = per[(per.fill_semantics == "close_only") & (per.fee_bps == 10)].copy()
    base = primary[primary.candidate == "BASE_ORIGINAL"].drop_duplicates("setup_id")
    n_total = int(base.setup_id.nunique())
    n_lb_ever = int(base["local_break_happened"].sum())
    n_lb_within = int(base["local_break_within_original_horizon"].sum())
    n_no_lb = n_total - n_lb_ever
    n_full_delayed = int(base["delayed_full_path"].sum())
    n_h24_delayed = int(base["h24_delayed"].sum())

    summaries = []
    for cand in CANDIDATES:
        g = primary[primary.candidate == cand]
        summaries.append(strategy_metrics(g, label=f"all_112_{cand}"))
        if cand != "BASE_ORIGINAL":
            summaries.append(
                strategy_metrics(g[g.candidate_activated == True], label=f"activated_{cand}")  # noqa: E712
            )
        summaries.append(strategy_metrics(g[g.direction == "long"], label=f"long_{cand}"))
        summaries.append(strategy_metrics(g[g.direction == "short"], label=f"short_{cand}"))
    summary_df = pd.DataFrame(summaries)

    c2 = primary[primary.candidate == "C2"].copy()
    orig = primary[primary.candidate == "BASE_ORIGINAL"].copy()
    b0 = primary[primary.candidate == "B0"].copy()
    m1 = primary[primary.candidate == "M1"].copy()

    cmp_o = comparison_block(c2, "improvement_vs_original_pct", "ORIGINAL")
    cmp_b0 = comparison_block(c2, "improvement_vs_B0_pct", "B0")
    cmp_m1 = comparison_block(c2, "improvement_vs_M1_pct", "M1")

    c2_act = c2[c2.candidate_activated == True].copy()  # noqa: E712
    cats = activation_categories(c2_act, orig)

    exit_rows = []
    for reason, g in c2.groupby(c2["managed_exit_reason"].fillna("na")):
        exit_rows.append(
            {
                "exit_reason": reason,
                "n": len(g),
                "win_rate": float((g.net_pnl_pct > EPS).mean()),
                "mean_net_pnl_pct": _mean(g.net_pnl_pct.tolist()),
                "median_net_pnl_pct": _median(g.net_pnl_pct.tolist()),
                "mean_improvement_vs_original_pct": _mean(g.improvement_vs_original_pct.tolist()),
                "median_improvement_vs_original_pct": _median(g.improvement_vs_original_pct.tolist()),
                "better_vs_original": int((g.improvement_vs_original_pct > EPS).sum()),
                "equal_vs_original": int((g.improvement_vs_original_pct.abs() <= EPS).sum()),
                "worse_vs_original": int((g.improvement_vs_original_pct < -EPS).sum()),
                "mean_favorable_move_pct": _mean(g.max_favorable_move_after_local_pct.tolist()),
                "median_favorable_move_pct": _median(g.max_favorable_move_after_local_pct.tolist()),
                "mean_adverse_move_pct": _mean(g.max_adverse_move_after_local_pct.tolist()),
                "median_adverse_move_pct": _median(g.max_adverse_move_after_local_pct.tolist()),
            }
        )
    exit_df = pd.DataFrame(exit_rows)

    ls_rows = []
    for d in ("long", "short"):
        for cand, g in [("BASE_ORIGINAL", orig), ("B0", b0), ("M1", m1), ("C2", c2)]:
            gd = g[g.direction == d]
            ls_rows.append(
                {
                    "direction": d,
                    "candidate": cand,
                    "n": len(gd),
                    "win_rate": float((gd.net_pnl_pct > EPS).mean()) if len(gd) else None,
                    "mean_pnl": _mean(gd.net_pnl_pct.tolist()),
                    "median_pnl": _median(gd.net_pnl_pct.tolist()),
                    "sum_pnl": float(gd.net_pnl_pct.sum()) if len(gd) else 0.0,
                }
            )
        cd = c2[c2.direction == d]
        ls_rows.append(
            {
                "direction": d,
                "candidate": "C2_vs_ORIGINAL",
                "n": len(cd),
                "better": int((cd.improvement_vs_original_pct > EPS).sum()),
                "equal": int((cd.improvement_vs_original_pct.abs() <= EPS).sum()),
                "worse": int((cd.improvement_vs_original_pct < -EPS).sum()),
                "median_improvement": _median(cd.improvement_vs_original_pct.tolist()),
                "sum_improvement": float(cd.improvement_vs_original_pct.sum()),
                "worst_deterioration": float(cd.improvement_vs_original_pct.min()) if len(cd) else None,
            }
        )
    ls_df = pd.DataFrame(ls_rows)

    path_rows = []
    groups = [
        ("all_activated", c2_act),
        ("local_reclaim", c2_act[c2_act.managed_exit_reason == "local_reclaim"]),
        ("timeout", c2_act[c2_act.managed_exit_reason == "timeout"]),
        ("effective_break", c2_act[c2_act.managed_exit_reason == "effective_break"]),
        ("long", c2_act[c2_act.direction == "long"]),
        ("short", c2_act[c2_act.direction == "short"]),
    ]
    for group_name, g in groups:
        if g.empty:
            continue
        for metric in (
            "max_favorable_move_after_local_pct",
            "max_adverse_move_after_local_pct",
            "close_move_local_to_exit_pct",
            "net_pnl_pct",
            "improvement_vs_original_pct",
        ):
            s = g[metric].astype(float)
            path_rows.append(
                {
                    "group": group_name,
                    "metric": metric,
                    "n": len(g),
                    "mean": _mean(s.tolist()),
                    "median": _median(s.tolist()),
                    "min": float(s.min()),
                    "max": float(s.max()),
                    "p25": _q(s.tolist(), 0.25),
                    "p75": _q(s.tolist(), 0.75),
                    "p90": _q(s.tolist(), 0.90),
                }
            )
    path_df = pd.DataFrame(path_rows)

    tail_df = pd.concat(
        [
            c2.nlargest(5, "improvement_vs_original_pct").assign(tail_kind="best_vs_original"),
            c2.nsmallest(5, "improvement_vs_original_pct").assign(tail_kind="worst_vs_original"),
        ],
        ignore_index=True,
    )

    sens_rows = []
    for fee in FEE_BPS:
        for mode in FILL_MODES:
            g = per[(per.candidate == "C2") & (per.fee_bps == fee) & (per.fill_semantics == mode)]
            o = per[(per.candidate == "BASE_ORIGINAL") & (per.fee_bps == fee) & (per.fill_semantics == mode)]
            sens_rows.append(
                {
                    "fee_bps": fee,
                    "fill_semantics": mode,
                    "c2_sum": float(g.net_pnl_pct.sum()),
                    "orig_sum": float(o.net_pnl_pct.sum()),
                    "c2_median": _median(g.net_pnl_pct.tolist()),
                    "orig_median": _median(o.net_pnl_pct.tolist()),
                    "delta_sum": float(g.net_pnl_pct.sum() - o.net_pnl_pct.sum()),
                    "better": int((g.improvement_vs_original_pct > EPS).sum()),
                    "worse": int((g.improvement_vs_original_pct < -EPS).sum()),
                    "equal": int((g.improvement_vs_original_pct.abs() <= EPS).sum()),
                }
            )
    sens_df = pd.DataFrame(sens_rows)

    c2_m = strategy_metrics(c2, label="C2")
    o_m = strategy_metrics(orig, label="ORIGINAL")
    b0_m = strategy_metrics(b0, label="B0")
    m1_m = strategy_metrics(m1, label="M1")

    rec = build_recommendation(
        n_total=n_total,
        n_lb_ever=n_lb_ever,
        n_act=n_lb_within,
        c2_sum=float(c2_m["sum_net_pnl_pct"]),
        orig_sum=float(o_m["sum_net_pnl_pct"]),
        c2_med=c2_m["median_net_pnl_pct"],
        orig_med=o_m["median_net_pnl_pct"],
        better_o=cmp_o["better"],
        worse_o=cmp_o["worse"],
        rescued=cats["rescued_neg_to_pos"],
        damaged=cats["damaged_pos_to_neg"],
        loss_reduced=cats["loss_reduced"],
        c2_wins=c2_m["profitable_trades"],
        orig_wins=o_m["profitable_trades"],
    )
    rec.update(
        {
            "n_no_local_break_ever": n_no_lb,
            "n_full_path_delayed": n_full_delayed,
            "n_h24_delayed": n_h24_delayed,
            "comparison_vs_original": cmp_o,
            "comparison_vs_b0": cmp_b0,
            "comparison_vs_m1": cmp_m1,
            "activation_categories": cats,
        }
    )

    per.to_csv(output_dir / "c2_all_112_per_fill.csv", index=False)
    summary_df.to_csv(output_dir / "c2_all_112_strategy_summary.csv", index=False)
    pd.DataFrame([cmp_o]).to_csv(output_dir / "c2_all_112_comparison_vs_original.csv", index=False)
    pd.DataFrame([cmp_b0]).to_csv(output_dir / "c2_all_112_comparison_vs_b0.csv", index=False)
    pd.DataFrame([cmp_m1]).to_csv(output_dir / "c2_all_112_comparison_vs_m1.csv", index=False)
    pd.DataFrame([cats]).to_csv(output_dir / "c2_all_112_activation_summary.csv", index=False)
    exit_df.to_csv(output_dir / "c2_all_112_exit_reason_summary.csv", index=False)
    ls_df.to_csv(output_dir / "c2_all_112_long_short.csv", index=False)
    path_df.to_csv(output_dir / "c2_all_112_path_statistics.csv", index=False)
    tail_df.to_csv(output_dir / "c2_all_112_tail_cases.csv", index=False)
    sens_df.to_csv(output_dir / "c2_all_112_fee_fill_sensitivity.csv", index=False)
    (output_dir / "c2_all_112_recommendation.json").write_text(
        json.dumps(json_safe(rec), indent=2) + "\n", encoding="utf-8"
    )

    md = f"""# C2 on all 112 APT fills

## Confirmed sample sizes

| Group | n |
|-------|--:|
| all fills | {n_total} |
| local break ever (full path) | {n_lb_ever} |
| no local break ever | {n_no_lb} |
| local break within original h24 (**C2 activatable**) | {n_lb_within} |
| full-path delayed | {n_full_delayed} |
| h24 delayed | {n_h24_delayed} |

**Activation rule:** C2/B0/M1 change a trade only if `local_break_bar <= original_h24_exit`.
Otherwise the D2 original horizon exit is kept.

## Strategy totals (`close_only`, 10 bps)

| Variant | Wins | Losses | Sum net% | Median net% |
|---------|-----:|-------:|---------:|------------:|
| ORIGINAL | {o_m['profitable_trades']} | {o_m['losing_trades']} | {o_m['sum_net_pnl_pct']:.4f} | {o_m['median_net_pnl_pct']:.4f} |
| B0 | {b0_m['profitable_trades']} | {b0_m['losing_trades']} | {b0_m['sum_net_pnl_pct']:.4f} | {b0_m['median_net_pnl_pct']:.4f} |
| M1 | {m1_m['profitable_trades']} | {m1_m['losing_trades']} | {m1_m['sum_net_pnl_pct']:.4f} | {m1_m['median_net_pnl_pct']:.4f} |
| C2 | {c2_m['profitable_trades']} | {c2_m['losing_trades']} | {c2_m['sum_net_pnl_pct']:.4f} | {c2_m['median_net_pnl_pct']:.4f} |

## C2 vs ORIGINAL (all 112)

- better / equal / worse: **{cmp_o['better']} / {cmp_o['equal']} / {cmp_o['worse']}**
- median improvement: **{cmp_o['median_improvement']}**
- sum improvement: **{cmp_o['sum_improvement']:.4f}**

## Activated exits

- reclaim / timeout / effective / horizon: **{cats['n_local_reclaim_exit']} / {cats['n_timeout_exit']} / {cats['n_effective_break_exit']} / {cats['n_horizon_end_exit']}**
- rescued (orig− → C2+): **{cats['rescued_neg_to_pos']}**
- damaged (orig+ → C2−): **{cats['damaged_pos_to_neg']}**
- loss reduced / worsened: **{cats['loss_reduced']} / {cats['loss_worsened']}**
- gain improved / cut: **{cats['gain_improved']} / {cats['gain_cut']}**

## Recommendation

`{rec['recommended_status']}` — runtime_change={rec['runtime_change_recommended']}

{rec['reason']}

```text
Keine neuen Trades.
Keine neue Parametersuche.
C2 unverändert.
Keine Runtime-/Bot-Änderung.
Keine Änderung an V_1LAG.
Kein historischer max/min-Chain-Carry.
Kein Lookahead.
Kein Commit.
```
"""
    (output_dir / "c2_all_112_plain_language_summary.md").write_text(md + "\n", encoding="utf-8")

    audit = {
        "phase": PHASE,
        "status": "OK",
        "n_total": n_total,
        "n_local_break_ever": n_lb_ever,
        "n_no_local_break": n_no_lb,
        "n_activatable_within_horizon": n_lb_within,
        "n_full_path_delayed": n_full_delayed,
        "n_h24_delayed": n_h24_delayed,
        "recommendation": rec,
        "metrics": {"ORIGINAL": o_m, "B0": b0_m, "M1": m1_m, "C2": c2_m},
        "output_dir": str(output_dir),
        "data_meta": {k: meta[k] for k in meta if k != "frame15_meta"} if meta else {},
    }
    (output_dir / "c2_all_112_audit_summary.json").write_text(
        json.dumps(json_safe(audit), indent=2) + "\n", encoding="utf-8"
    )
    return audit


def main() -> None:
    p = argparse.ArgumentParser(description="C2 all-112 fills evaluation")
    p.add_argument("--apt-dir", type=Path, default=DEFAULT_APT_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    audit = run_audit(apt_dir=args.apt_dir, output_dir=args.output_dir)
    print(json.dumps(json_safe(audit), indent=2))


if __name__ == "__main__":
    main()
