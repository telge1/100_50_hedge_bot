"""C3.5D protected-break exit-management audit (offline, research-only).

Question: after a V_1LAG *local* protected break but *before* the effective
protected break, can a causal managed exit beat immediate local exit (B0)?

Does not mutate V_1LAG semantics, D1/D2, C3.4B, live bot, or parent CSVs.
Oracle paths are research-only and never recommended.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5d_apt_raw_audit import build_apt_d1_frame
from research.regime_scanner.pullback_entry_c3_5d_protected_carry_audit import (
    SetupCarry,
    assign_effective_levels,
    close_breaks_protected,
    ensure_ohlc,
    first_close_break_bar,
    load_setups,
    signed_return_pct,
)
from research.regime_scanner.trend_pine_export import (
    build_pine_header,
    validate_pine_script,
)

PHASE = "C3.5D_PROTECTED_BREAK_EXIT_MANAGEMENT"
DEFAULT_APT_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
DEFAULT_OUT = DEFAULT_APT_DIR / "protected_break_exit_management"
PINE_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/pine_exit_levels"
)
MAIN_PINE = "C3_5D_APT_protected_break_exit_management.pine"

FEE_BPS_RT = (0, 5, 10, 20)
FILL_MODES = ("close_only", "conservative_intrabar")
BE_TARGETS_PCT = (0.0, 0.10, 0.25)
R_TARGETS = (-0.50, -0.25, 0.0, 0.25, 0.50)
TIME_LIMITS = (1, 2, 3, 4, 6, 8, 12)
PARTIAL_FRACS = (0.25, 0.50, 0.75)
STOP_ATRS = (0.10, 0.20, 0.30, 0.50)


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


def _mean(xs: Sequence[float]) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.mean(vals)) if vals else None


def _quantile(xs: Sequence[float], q: float) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.quantile(vals, q)) if vals else None


def risk_unit(*, entry: float, local: float, atr: float) -> float:
    """1R = |entry - local_protected|; fallback ATR."""
    r = abs(entry - local)
    if r > 1e-12:
        return r
    a = _finite(atr)
    return a if a > 0 else 1.0


def pnl_pct_from_price(*, side: int, entry: float, exit_px: float) -> float:
    return float(signed_return_pct(side=side, entry=entry, close=exit_px))


def pnl_r_from_pct(pnl_pct: float, *, entry: float, r_unit: float) -> float:
    if r_unit <= 0 or not math.isfinite(r_unit) or not math.isfinite(entry) or entry == 0:
        return float("nan")
    return float((pnl_pct / 100.0) * entry / r_unit)


def apply_fees_pct(pnl_pct: float, fee_bps_rt: float) -> float:
    return float(pnl_pct - fee_bps_rt / 100.0)  # 10 bps = 0.10%


def favorable_extreme(*, side: int, high: float, low: float) -> float:
    return float(high if side > 0 else low)


def adverse_extreme(*, side: int, high: float, low: float) -> float:
    return float(low if side > 0 else high)


def reclaim_local(*, side: int, close: float, local: float) -> bool:
    # after break (beyond), reclaim = close back to valid side
    if side > 0:
        return close >= local
    return close <= local


def reclaim_entry(*, side: int, close: float, entry: float) -> bool:
    if side > 0:
        return close >= entry
    return close <= entry


def target_hit_pct(
    *,
    side: int,
    entry: float,
    high: float,
    low: float,
    close: float,
    target_pnl_pct: float,
    fill_mode: str,
) -> bool:
    """True if target signed return is reached on this bar."""
    if fill_mode == "close_only":
        return pnl_pct_from_price(side=side, entry=entry, exit_px=close) >= target_pnl_pct - 1e-12
    # conservative_intrabar: use favorable extreme for targets
    ext = favorable_extreme(side=side, high=high, low=low)
    return pnl_pct_from_price(side=side, entry=entry, exit_px=ext) >= target_pnl_pct - 1e-12


def stop_hit(
    *,
    side: int,
    high: float,
    low: float,
    close: float,
    stop_px: float,
    fill_mode: str,
) -> bool:
    if fill_mode == "close_only":
        if side > 0:
            return close <= stop_px
        return close >= stop_px
    # intrabar adverse extreme
    adv = adverse_extreme(side=side, high=high, low=low)
    if side > 0:
        return adv <= stop_px
    return adv >= stop_px


def exit_price_for_event(
    *,
    side: int,
    high: float,
    low: float,
    close: float,
    reason: str,
    fill_mode: str,
    stop_px: float | None = None,
    target_pnl_pct: float | None = None,
    entry: float | None = None,
) -> float:
    """Conservative same-bar: if stop and target both possible, use stop."""
    if fill_mode == "close_only":
        return float(close)
    if reason in ("stop", "tightened_stop") and stop_px is not None:
        return float(stop_px)
    if reason in ("target", "breakeven", "entry_reclaim", "local_reclaim", "r_target") and entry is not None:
        # approximate fill at favorable extreme or target level
        if target_pnl_pct is not None:
            # reconstruct target price from pnl pct
            if side > 0:
                return float(entry * (1.0 + target_pnl_pct / 100.0))
            return float(entry * (1.0 - target_pnl_pct / 100.0))
        return float(favorable_extreme(side=side, high=high, low=low))
    return float(close)


@dataclass
class PathEvents:
    setup_id: int
    direction: str
    side: int
    fill_bar: int
    fill_timestamp: Any
    entry_price: float
    atr: float
    local: float
    effective: float
    carry_source_setup_id: int | None
    leg_id: int
    r_unit: float
    local_break_bar: int | None
    effective_break_bar: int | None
    data_end: int


def build_path_events(
    s: SetupCarry,
    ohlc: pd.DataFrame,
) -> PathEvents | None:
    local = float(s.local_protected)
    eff = float(s.effective_by_variant["V_1LAG"])
    data_end = int(ohlc.index.max())
    local_br = first_close_break_bar(
        ohlc, side=s.side, fill_bar=s.fill_bar, end_bar=data_end, level=local
    )
    if local_br is None:
        return None
    eff_br = first_close_break_bar(
        ohlc, side=s.side, fill_bar=s.fill_bar, end_bar=data_end, level=eff
    )
    return PathEvents(
        setup_id=s.setup_id,
        direction=s.direction,
        side=s.side,
        fill_bar=s.fill_bar,
        fill_timestamp=s.fill_timestamp,
        entry_price=s.entry_price,
        atr=_finite(s.atr),
        local=local,
        effective=eff,
        carry_source_setup_id=s.carry_origin_by_variant.get("V_1LAG"),
        leg_id=int(s.leg_id_by_variant.get("V_1LAG") or 0),
        r_unit=risk_unit(entry=s.entry_price, local=local, atr=_finite(s.atr)),
        local_break_bar=local_br,
        effective_break_bar=eff_br,
        data_end=data_end,
    )


def window_end(ev: PathEvents) -> int:
    """Last bar inclusive for management window (before/at effective break)."""
    if ev.effective_break_bar is not None:
        return int(ev.effective_break_bar)
    return int(ev.data_end)


def scan_reclaims(
    ohlc: pd.DataFrame,
    ev: PathEvents,
) -> dict[str, Any]:
    lb = int(ev.local_break_bar)  # type: ignore[arg-type]
    end = window_end(ev)
    local_rec = None
    entry_rec = None
    for bi in range(lb + 1, end + 1):
        if bi not in ohlc.index:
            continue
        c = float(ohlc.loc[bi, "close"])
        if local_rec is None and reclaim_local(side=ev.side, close=c, local=ev.local):
            local_rec = bi
        if entry_rec is None and reclaim_entry(side=ev.side, close=c, entry=ev.entry_price):
            entry_rec = bi
        if local_rec is not None and entry_rec is not None:
            break
    return {
        "local_reclaim_bar": local_rec,
        "entry_reclaim_bar": entry_rec,
        "local_reclaimed_before_effective": local_rec is not None
        and (ev.effective_break_bar is None or local_rec <= int(ev.effective_break_bar)),
        "entry_reclaimed_before_effective": entry_rec is not None
        and (ev.effective_break_bar is None or entry_rec <= int(ev.effective_break_bar)),
    }


def path_mfe_mae(
    ohlc: pd.DataFrame,
    ev: PathEvents,
    *,
    start_exclusive: int,
    end_inclusive: int,
) -> dict[str, float]:
    bars = [bi for bi in range(start_exclusive + 1, end_inclusive + 1) if bi in ohlc.index]
    if not bars:
        return {
            "mfe_pct": float("nan"),
            "mae_pct": float("nan"),
            "mfe_atr": float("nan"),
            "mae_atr": float("nan"),
            "mfe_r": float("nan"),
            "mae_r": float("nan"),
            "best_close_pnl_pct": float("nan"),
            "best_hl_pnl_pct": float("nan"),
            "best_close_bar": float("nan"),
            "best_hl_bar": float("nan"),
        }
    entry = ev.entry_price
    side = ev.side
    best_close = -1e18
    best_close_bar = bars[0]
    best_hl = -1e18
    best_hl_bar = bars[0]
    mfe = -1e18
    mae = 1e18
    for bi in bars:
        row = ohlc.loc[bi]
        h, l, c = float(row["high"]), float(row["low"]), float(row["close"])
        cp = pnl_pct_from_price(side=side, entry=entry, exit_px=c)
        fp = pnl_pct_from_price(
            side=side, entry=entry, exit_px=favorable_extreme(side=side, high=h, low=l)
        )
        ap = pnl_pct_from_price(
            side=side, entry=entry, exit_px=adverse_extreme(side=side, high=h, low=l)
        )
        if cp > best_close:
            best_close, best_close_bar = cp, bi
        if fp > best_hl:
            best_hl, best_hl_bar = fp, bi
        mfe = max(mfe, fp)
        mae = min(mae, ap)
    atr = ev.atr if ev.atr > 0 else float("nan")
    return {
        "mfe_pct": float(mfe),
        "mae_pct": float(mae),
        "mfe_atr": float(mfe / 100.0 * entry / atr) if atr > 0 else float("nan"),
        "mae_atr": float(mae / 100.0 * entry / atr) if atr > 0 else float("nan"),
        "mfe_r": pnl_r_from_pct(mfe, entry=entry, r_unit=ev.r_unit),
        "mae_r": pnl_r_from_pct(mae, entry=entry, r_unit=ev.r_unit),
        "best_close_pnl_pct": float(best_close),
        "best_hl_pnl_pct": float(best_hl),
        "best_close_bar": float(best_close_bar),
        "best_hl_bar": float(best_hl_bar),
    }


@dataclass
class ExitResult:
    candidate: str
    exit_bar: int | None
    exit_price: float
    exit_reason: str
    pnl_pct_gross: float
    uses_future: bool = False
    meta: dict[str, Any] | None = None


def bar_ohlc(ohlc: pd.DataFrame, bi: int) -> tuple[float, float, float]:
    row = ohlc.loc[bi]
    return float(row["high"]), float(row["low"]), float(row["close"])


def simulate_B0(ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str) -> ExitResult:
    bi = int(ev.local_break_bar)  # type: ignore[arg-type]
    h, l, c = bar_ohlc(ohlc, bi)
    px = exit_price_for_event(side=ev.side, high=h, low=l, close=c, reason="local_break", fill_mode=fill_mode)
    return ExitResult("B0_immediate_local", bi, px, "local_break", pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px))


def simulate_B1(ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str) -> ExitResult:
    if ev.effective_break_bar is None:
        # hold to data end
        bi = int(ev.data_end)
        reason = "data_end"
    else:
        bi = int(ev.effective_break_bar)
        reason = "effective_break"
    h, l, c = bar_ohlc(ohlc, bi)
    px = exit_price_for_event(side=ev.side, high=h, low=l, close=c, reason=reason, fill_mode=fill_mode)
    return ExitResult("B1_effective_break", bi, px, reason, pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px))


def simulate_oracle(ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str) -> ExitResult:
    lb = int(ev.local_break_bar)  # type: ignore[arg-type]
    end = window_end(ev)
    m = path_mfe_mae(ohlc, ev, start_exclusive=lb, end_inclusive=end)
    if fill_mode == "close_only":
        bi = int(m["best_close_bar"]) if math.isfinite(m["best_close_bar"]) else lb
        h, l, c = bar_ohlc(ohlc, bi)
        px = c
        pnl = float(m["best_close_pnl_pct"])
    else:
        bi = int(m["best_hl_bar"]) if math.isfinite(m["best_hl_bar"]) else lb
        h, l, c = bar_ohlc(ohlc, bi)
        px = favorable_extreme(side=ev.side, high=h, low=l)
        pnl = float(m["best_hl_pnl_pct"])
    return ExitResult("B_ORACLE_best_before_effective", bi, px, "oracle_best", pnl, uses_future=True)


def _hold_until(
    ohlc: pd.DataFrame,
    ev: PathEvents,
    *,
    fill_mode: str,
    name: str,
    check: Callable[[int, float, float, float], tuple[bool, str, float | None, float | None]],
    max_bars: int | None = None,
) -> ExitResult:
    """Walk bars after local break until check fires, effective break, or timeout."""
    lb = int(ev.local_break_bar)  # type: ignore[arg-type]
    end = window_end(ev)
    last = end if max_bars is None else min(end, lb + max_bars)
    for bi in range(lb + 1, last + 1):
        if bi not in ohlc.index:
            continue
        # effective break same bar: exit effective (hard)
        if ev.effective_break_bar is not None and bi == int(ev.effective_break_bar):
            h, l, c = bar_ohlc(ohlc, bi)
            px = exit_price_for_event(side=ev.side, high=h, low=l, close=c, reason="effective_break", fill_mode=fill_mode)
            return ExitResult(name, bi, px, "effective_break", pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px))
        h, l, c = bar_ohlc(ohlc, bi)
        hit, reason, stop_px, tgt = check(bi, h, l, c)
        if hit:
            # same-bar effective + target: if this bar is effective break, effective wins (already handled)
            px = exit_price_for_event(
                side=ev.side,
                high=h,
                low=l,
                close=c,
                reason=reason,
                fill_mode=fill_mode,
                stop_px=stop_px,
                target_pnl_pct=tgt,
                entry=ev.entry_price,
            )
            return ExitResult(name, bi, px, reason, pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px))
    # timeout or end
    bi = last if last in ohlc.index else int(ev.local_break_bar)  # type: ignore[arg-type]
    if ev.effective_break_bar is not None and last >= int(ev.effective_break_bar):
        bi = int(ev.effective_break_bar)
        reason = "effective_break"
    elif max_bars is not None and last == lb + max_bars:
        reason = "timeout"
    else:
        reason = "data_end" if ev.effective_break_bar is None else "effective_break"
        if ev.effective_break_bar is not None:
            bi = int(ev.effective_break_bar)
    h, l, c = bar_ohlc(ohlc, bi)
    px = exit_price_for_event(side=ev.side, high=h, low=l, close=c, reason=reason, fill_mode=fill_mode)
    return ExitResult(name, bi, px, reason, pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=px))


def simulate_M1_local_reclaim(ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str) -> ExitResult:
    def check(bi, h, l, c):
        if reclaim_local(side=ev.side, close=c, local=ev.local):
            return True, "local_reclaim", None, None
        return False, "", None, None

    return _hold_until(ohlc, ev, fill_mode=fill_mode, name="M1_local_reclaim", check=check)


def simulate_M2_entry_reclaim(ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str) -> ExitResult:
    def check(bi, h, l, c):
        # close_only reclaim uses close; intrabar may use favorable extreme touching entry
        if fill_mode == "close_only":
            ok = reclaim_entry(side=ev.side, close=c, entry=ev.entry_price)
        else:
            ext = favorable_extreme(side=ev.side, high=h, low=l)
            ok = reclaim_entry(side=ev.side, close=ext, entry=ev.entry_price)
        if ok:
            return True, "entry_reclaim", None, 0.0
        return False, "", None, None

    return _hold_until(ohlc, ev, fill_mode=fill_mode, name="M2_entry_reclaim", check=check)


def simulate_M3_breakeven(ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str, target_pct: float) -> ExitResult:
    name = f"M3_breakeven_{target_pct:.2f}pct"

    def check(bi, h, l, c):
        if target_hit_pct(
            side=ev.side,
            entry=ev.entry_price,
            high=h,
            low=l,
            close=c,
            target_pnl_pct=target_pct,
            fill_mode=fill_mode,
        ):
            return True, "breakeven", None, target_pct
        return False, "", None, None

    return _hold_until(ohlc, ev, fill_mode=fill_mode, name=name, check=check)


def simulate_M4_r_target(ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str, r_tgt: float) -> ExitResult:
    # convert R target to pct
    tgt_pct = (r_tgt * ev.r_unit / ev.entry_price) * 100.0 if ev.entry_price else float("nan")
    name = f"M4_r_{r_tgt:+.2f}".replace("+", "p").replace("-", "m")

    def check(bi, h, l, c):
        if not math.isfinite(tgt_pct):
            return False, "", None, None
        if target_hit_pct(
            side=ev.side,
            entry=ev.entry_price,
            high=h,
            low=l,
            close=c,
            target_pnl_pct=tgt_pct,
            fill_mode=fill_mode,
        ):
            return True, "r_target", None, tgt_pct
        return False, "", None, None

    return _hold_until(ohlc, ev, fill_mode=fill_mode, name=name, check=check)


def simulate_M5_time(
    ohlc: pd.DataFrame,
    ev: PathEvents,
    fill_mode: str,
    max_bars: int,
    recovery: str,
) -> ExitResult:
    """recovery: entry_reclaim | 0R | p0.25R"""
    name = f"M5_t{max_bars}_{recovery}"

    def check(bi, h, l, c):
        if recovery == "entry_reclaim":
            ok = reclaim_entry(side=ev.side, close=c, entry=ev.entry_price)
            if fill_mode != "close_only":
                ok = reclaim_entry(
                    side=ev.side,
                    close=favorable_extreme(side=ev.side, high=h, low=l),
                    entry=ev.entry_price,
                )
            return (ok, "entry_reclaim", None, 0.0) if ok else (False, "", None, None)
        if recovery == "0R":
            tgt = 0.0
        else:
            tgt = (0.25 * ev.r_unit / ev.entry_price) * 100.0
        if target_hit_pct(
            side=ev.side,
            entry=ev.entry_price,
            high=h,
            low=l,
            close=c,
            target_pnl_pct=tgt,
            fill_mode=fill_mode,
        ):
            return True, "target", None, tgt
        return False, "", None, None

    return _hold_until(ohlc, ev, fill_mode=fill_mode, name=name, check=check, max_bars=max_bars)


def simulate_M6_partial(
    ohlc: pd.DataFrame,
    ev: PathEvents,
    fill_mode: str,
    frac: float,
    rest_mode: str,
) -> ExitResult:
    """Reduce frac at local break; rest via M1/M2/B1."""
    b0 = simulate_B0(ohlc, ev, fill_mode)
    if rest_mode == "local_reclaim":
        rest = simulate_M1_local_reclaim(ohlc, ev, fill_mode)
    elif rest_mode == "entry_reclaim":
        rest = simulate_M2_entry_reclaim(ohlc, ev, fill_mode)
    else:
        rest = simulate_B1(ohlc, ev, fill_mode)
    pnl = frac * b0.pnl_pct_gross + (1.0 - frac) * rest.pnl_pct_gross
    name = f"M6_partial_{int(frac*100)}_{rest_mode}"
    return ExitResult(
        name,
        rest.exit_bar,
        rest.exit_price,
        f"partial_{int(frac*100)}_then_{rest.exit_reason}",
        float(pnl),
        meta={"partial_frac": frac, "rest_reason": rest.exit_reason, "local_leg_pnl": b0.pnl_pct_gross},
    )


def simulate_M7_tight_stop(
    ohlc: pd.DataFrame,
    ev: PathEvents,
    fill_mode: str,
    stop_atr: float,
) -> ExitResult:
    """Stop active from local_break+1 (no same-bar stop on break bar)."""
    name = f"M7_stop_{stop_atr:.2f}atr"
    lb = int(ev.local_break_bar)  # type: ignore[arg-type]
    h0, l0, c0 = bar_ohlc(ohlc, lb)
    # stop beyond adverse side of break close by stop_atr * atr
    atr = ev.atr if ev.atr > 0 else 0.0
    dist = stop_atr * atr
    if ev.side > 0:
        stop_px = c0 - dist
    else:
        stop_px = c0 + dist

    def check(bi, h, l, c):
        # stop only from lb+1 already ensured by _hold_until start
        if stop_hit(side=ev.side, high=h, low=l, close=c, stop_px=stop_px, fill_mode=fill_mode):
            # same-bar target not used; if also reclaim, stop wins (conservative)
            return True, "tightened_stop", stop_px, None
        return False, "", None, None

    return _hold_until(ohlc, ev, fill_mode=fill_mode, name=name, check=check)


def simulate_M8_reclaim_confirm(
    ohlc: pd.DataFrame,
    ev: PathEvents,
    fill_mode: str,
    need_closes: int,
) -> ExitResult:
    name = f"M8_reclaim_confirm_{need_closes}"
    streak = 0

    def check(bi, h, l, c):
        nonlocal streak
        if reclaim_local(side=ev.side, close=c, local=ev.local):
            streak += 1
        else:
            streak = 0
        if streak >= need_closes:
            return True, "local_reclaim_confirmed", None, None
        return False, "", None, None

    return _hold_until(ohlc, ev, fill_mode=fill_mode, name=name, check=check)


def all_candidates(ohlc: pd.DataFrame, ev: PathEvents, fill_mode: str) -> list[ExitResult]:
    out: list[ExitResult] = [
        simulate_B0(ohlc, ev, fill_mode),
        simulate_B1(ohlc, ev, fill_mode),
        simulate_oracle(ohlc, ev, fill_mode),
        simulate_M1_local_reclaim(ohlc, ev, fill_mode),
        simulate_M2_entry_reclaim(ohlc, ev, fill_mode),
    ]
    for t in BE_TARGETS_PCT:
        out.append(simulate_M3_breakeven(ohlc, ev, fill_mode, t))
    for r in R_TARGETS:
        out.append(simulate_M4_r_target(ohlc, ev, fill_mode, r))
    for tb in TIME_LIMITS:
        for rec in ("entry_reclaim", "0R", "p0.25R"):
            out.append(simulate_M5_time(ohlc, ev, fill_mode, tb, rec))
    for frac in PARTIAL_FRACS:
        for rest in ("local_reclaim", "entry_reclaim", "effective"):
            out.append(simulate_M6_partial(ohlc, ev, fill_mode, frac, rest))
    for sa in STOP_ATRS:
        out.append(simulate_M7_tight_stop(ohlc, ev, fill_mode, sa))
    for n in (1, 2):
        out.append(simulate_M8_reclaim_confirm(ohlc, ev, fill_mode, n))
    return out


def enrich_fill_row(
    ev: PathEvents,
    ohlc: pd.DataFrame,
    res: ExitResult,
    *,
    fill_mode: str,
    fee_bps: float,
    b0: ExitResult,
    b1: ExitResult,
) -> dict[str, Any]:
    lb = int(ev.local_break_bar)  # type: ignore[arg-type]
    h, l, c = bar_ohlc(ohlc, lb)
    local_px = c
    local_pnl = pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=local_px)
    rec = scan_reclaims(ohlc, ev)
    end = window_end(ev)
    mm = path_mfe_mae(ohlc, ev, start_exclusive=lb, end_inclusive=end)
    eff_pnl = float("nan")
    if ev.effective_break_bar is not None:
        _, _, ec = bar_ohlc(ohlc, int(ev.effective_break_bar))
        eff_pnl = pnl_pct_from_price(side=ev.side, entry=ev.entry_price, exit_px=ec)

    pnl_net = apply_fees_pct(res.pnl_pct_gross, fee_bps)
    b0_net = apply_fees_pct(b0.pnl_pct_gross, fee_bps)
    b1_net = apply_fees_pct(b1.pnl_pct_gross, fee_bps)
    delayed = ev.effective_break_bar is None or int(ev.effective_break_bar) > lb

    ts = None
    if "timestamp" in ohlc.columns and lb in ohlc.index:
        ts = str(ohlc.loc[lb, "timestamp"])

    return {
        "symbol": "APTUSDT",
        "direction": ev.direction,
        "setup_id": ev.setup_id,
        "carry_source_setup_id": ev.carry_source_setup_id,
        "structure_leg_id": ev.leg_id,
        "candidate": res.candidate,
        "fill_semantics": fill_mode,
        "fee_bps_rt": fee_bps,
        "uses_future_information": bool(res.uses_future),
        "entry_bar": ev.fill_bar,
        "entry_time": str(ev.fill_timestamp),
        "entry_price": ev.entry_price,
        "local_protected_level": ev.local,
        "effective_protected_level": ev.effective,
        "local_break_bar": lb,
        "local_break_time": ts,
        "local_break_price": local_px,
        "local_break_pnl_pct": local_pnl,
        "local_break_pnl_atr": local_pnl / 100.0 * ev.entry_price / ev.atr if ev.atr > 0 else float("nan"),
        "local_break_r": pnl_r_from_pct(local_pnl, entry=ev.entry_price, r_unit=ev.r_unit),
        "effective_break_bar": ev.effective_break_bar,
        "effective_break_pnl_pct": eff_pnl,
        "effective_break_r": pnl_r_from_pct(eff_pnl, entry=ev.entry_price, r_unit=ev.r_unit),
        "effective_break_happened": ev.effective_break_bar is not None,
        "bars_local_to_effective": (
            int(ev.effective_break_bar) - lb if ev.effective_break_bar is not None else None
        ),
        "minutes_local_to_effective": (
            (int(ev.effective_break_bar) - lb) * 15 if ev.effective_break_bar is not None else None
        ),
        "delayed_case": delayed,
        "local_reclaimed_before_effective": rec["local_reclaimed_before_effective"],
        "local_reclaim_bar": rec["local_reclaim_bar"],
        "bars_to_local_reclaim": (
            int(rec["local_reclaim_bar"]) - lb if rec["local_reclaim_bar"] is not None else None
        ),
        "entry_reclaimed_before_effective": rec["entry_reclaimed_before_effective"],
        "entry_reclaim_bar": rec["entry_reclaim_bar"],
        "bars_to_entry_reclaim": (
            int(rec["entry_reclaim_bar"]) - lb if rec["entry_reclaim_bar"] is not None else None
        ),
        "mfe_after_local_before_effective_pct": mm["mfe_pct"],
        "mae_after_local_before_effective_pct": mm["mae_pct"],
        "mfe_after_local_before_effective_atr": mm["mfe_atr"],
        "mae_after_local_before_effective_atr": mm["mae_atr"],
        "mfe_after_local_before_effective_r": mm["mfe_r"],
        "mae_after_local_before_effective_r": mm["mae_r"],
        "best_close_pnl_before_effective_pct": mm["best_close_pnl_pct"],
        "best_high_low_pnl_before_effective_pct": mm["best_hl_pnl_pct"],
        "best_exit_bar_before_effective": int(mm["best_close_bar"])
        if math.isfinite(mm["best_close_bar"])
        else None,
        "exit_bar": res.exit_bar,
        "exit_price": res.exit_price,
        "exit_reason": res.exit_reason,
        "pnl_pct_gross": res.pnl_pct_gross,
        "pnl_pct_net": pnl_net,
        "pnl_r_gross": pnl_r_from_pct(res.pnl_pct_gross, entry=ev.entry_price, r_unit=ev.r_unit),
        "pnl_r_net": pnl_r_from_pct(pnl_net, entry=ev.entry_price, r_unit=ev.r_unit),
        "pnl_improvement_vs_local_exit_pct": res.pnl_pct_gross - b0.pnl_pct_gross,
        "pnl_improvement_vs_local_exit_r": pnl_r_from_pct(
            res.pnl_pct_gross - b0.pnl_pct_gross, entry=ev.entry_price, r_unit=ev.r_unit
        ),
        "effective_exit_minus_local_exit_pct": b1.pnl_pct_gross - b0.pnl_pct_gross,
        "improvement_vs_B0_net_pct": pnl_net - b0_net,
        "improvement_vs_B1_net_pct": pnl_net - b1_net,
        "bars_after_local": (int(res.exit_bar) - lb) if res.exit_bar is not None else None,
        "adverse_bucket_at_local": _bucket(local_pnl),
    }


def _bucket(signed_pct: float) -> str:
    s = float(signed_pct)
    if s > -1:
        return "0_to_-1"
    if s > -2:
        return "-1_to_-2"
    if s > -3:
        return "-2_to_-3"
    if s > -5:
        return "-3_to_-5"
    return "worse_than_-5"


def summarize(df: pd.DataFrame, *, scope: str) -> pd.DataFrame:
    rows = []
    for (cand, fee, mode), g in df.groupby(["candidate", "fee_bps_rt", "fill_semantics"]):
        if scope == "delayed":
            g = g[g["delayed_case"] == True]  # noqa: E712
        elif scope == "long":
            g = g[g["direction"] == "long"]
        elif scope == "short":
            g = g[g["direction"] == "short"]
        elif scope != "all":
            continue
        n = len(g)
        if n == 0:
            continue
        imp = g["improvement_vs_B0_net_pct"].astype(float)
        imp_r = g["pnl_improvement_vs_local_exit_r"].astype(float)
        rows.append(
            {
                "candidate": cand,
                "scope": scope,
                "fee_scenario": fee,
                "fill_semantics": mode,
                "n": n,
                "mean_pnl_pct_net": _mean(g["pnl_pct_net"].tolist()),
                "median_pnl_pct_net": _median(g["pnl_pct_net"].tolist()),
                "mean_pnl_r_net": _mean(g["pnl_r_net"].tolist()),
                "median_pnl_r_net": _median(g["pnl_r_net"].tolist()),
                "sum_pnl_r_net": float(np.nansum(g["pnl_r_net"].astype(float))),
                "win_rate": _safe_rate(int((g["pnl_pct_net"] > 0).sum()), n),
                "mean_improvement_vs_B0_pct": _mean(imp.tolist()),
                "median_improvement_vs_B0_pct": _median(imp.tolist()),
                "mean_improvement_vs_B0_r": _mean(imp_r.tolist()),
                "median_improvement_vs_B0_r": _median(imp_r.tolist()),
                "better_than_B0_count": int((imp > 1e-12).sum()),
                "equal_to_B0_count": int((imp.abs() <= 1e-12).sum()),
                "worse_than_B0_count": int((imp < -1e-12).sum()),
                "better_than_B1_count": int((g["improvement_vs_B1_net_pct"] > 1e-12).sum()),
                "worse_than_B1_count": int((g["improvement_vs_B1_net_pct"] < -1e-12).sum()),
                "max_extra_loss_vs_B0_r": float(np.nanmin(imp_r)) if n else None,
                "p90_extra_loss_vs_B0_r": _quantile(imp_r.tolist(), 0.10),
                "mean_bars_after_local": _mean(g["bars_after_local"].dropna().astype(float).tolist()),
                "median_bars_after_local": _median(g["bars_after_local"].dropna().astype(float).tolist()),
                "local_reclaim_exit_count": int(
                    g["exit_reason"].astype(str).str.contains("local_reclaim").sum()
                ),
                "entry_reclaim_exit_count": int(
                    g["exit_reason"].astype(str).str.contains("entry_reclaim").sum()
                ),
                "effective_break_exit_count": int((g["exit_reason"] == "effective_break").sum()),
                "timeout_exit_count": int((g["exit_reason"] == "timeout").sum()),
                "stop_exit_count": int(g["exit_reason"].astype(str).str.contains("stop").sum()),
                "is_oracle": bool(g["uses_future_information"].any()),
            }
        )
    return pd.DataFrame(rows)


def build_recommendation(summary: pd.DataFrame) -> dict[str, Any]:
    # Focus: close_only, 10bps, delayed scope, non-oracle
    sub = summary[
        (summary["fill_semantics"] == "close_only")
        & (summary["fee_scenario"] == 10)
        & (summary["scope"] == "delayed")
        & (summary["is_oracle"] == False)  # noqa: E712
    ].copy()
    if sub.empty:
        sub = summary[(summary["is_oracle"] == False) & (summary["scope"] == "all")]  # noqa: E712
    causal = sub[~sub["candidate"].astype(str).str.startswith("B_ORACLE")].copy()
    best = None
    if not causal.empty and "median_improvement_vs_B0_pct" in causal.columns:
        causal = causal.sort_values(
            ["median_improvement_vs_B0_pct", "mean_improvement_vs_B0_pct", "worse_than_B0_count"],
            ascending=[False, False, True],
        )
        best = causal.iloc[0].to_dict()

    status = "RESEARCH_ONLY"
    reason = (
        "APT sample of local breaks is small; V_1LAG delay window shows limited h24 full saves. "
        "Exit-management candidates need multi-symbol validation before any runtime change."
    )
    if best is not None:
        med = best.get("median_improvement_vs_B0_pct")
        worse = int(best.get("worse_than_B0_count") or 0)
        better = int(best.get("better_than_B0_count") or 0)
        n = int(best.get("n") or 0)
        if n < 15:
            status = "PROMISING_NEEDS_MORE_DATA" if med is not None and float(med) > 0 and better > worse else "RESEARCH_ONLY"
        elif med is not None and float(med) > 0 and better > worse * 2:
            status = "CANDIDATE_FOR_MULTI_SYMBOL_VALIDATION"
        elif med is not None and float(med) <= 0:
            status = "REJECT"

    return {
        "recommended_status": status,
        "best_research_candidate": None if best is None else best.get("candidate"),
        "best_candidate_stats": best,
        "runtime_change_recommended": False,
        "reason": reason,
        "sample_size_warning": True,
        "v1lag_semantics_unchanged": True,
        "uses_future_information": False,
        "oracle_separated": True,
        "phase": PHASE,
    }


def _pine_float(x: Any) -> str:
    v = _finite(x)
    return "na" if not math.isfinite(v) else repr(float(v))


def _pine_int(x: Any) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)) or pd.isna(x):
        return "0"
    return str(int(x))


def _ts(ts: Any) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return f"timestamp({t.year}, {t.month}, {t.day}, {t.hour}, {t.minute})"


def _bars_since(entry_bar: Any, event_bar: Any) -> int:
    if event_bar is None or pd.isna(event_bar):
        return 0
    return int(event_bar) - int(entry_bar)


def build_pine(delayed: pd.DataFrame) -> str:
    """Compact overlay for delayed cases: levels + break/reclaim/exit markers for M2."""
    if delayed is None or delayed.empty or "candidate" not in delayed.columns:
        lines = [
            *build_pine_header("C3.5D Protected Break Exit Management"),
            "// RESEARCH ONLY — empty delayed set.",
            "plot(na, 'setup_id', display=display.data_window)",
            "",
        ]
        text = "\n".join(lines) + "\n"
        validate_pine_script(text)
        return text

    sub = delayed[
        (delayed["candidate"] == "M2_entry_reclaim")
        & (delayed["fill_semantics"] == "close_only")
        & (delayed["fee_bps_rt"] == 10)
    ].drop_duplicates("setup_id").sort_values("entry_bar")
    if sub.empty:
        sub = delayed.drop_duplicates("setup_id").head(20)
    if sub.empty:
        # minimal valid pine for empty research set
        lines = [
            *build_pine_header("C3.5D Protected Break Exit Management"),
            "// RESEARCH ONLY — empty delayed set.",
            "plot(na, 'setup_id', display=display.data_window)",
            "",
        ]
        text = "\n".join(lines) + "\n"
        validate_pine_script(text)
        return text

    n = len(sub)
    max_vis = min(20, max(1, n))
    lines = [
        *build_pine_header("C3.5D Protected Break Exit Management"),
        "// RESEARCH ONLY — local vs effective window + managed exit markers.",
        "// V_1LAG unchanged: effective[n]=local[n-1]. No max/min chain. No live orders.",
        "// Colors: LOCAL=red thin, EFFECTIVE=maroon thick, LOCAL BREAK=red,",
        "// RECLAIM=orange, MANAGED EXIT=blue, EFFECTIVE BREAK=maroon.",
        f"nSetups = {n}",
        f'maxVisible = input.int({max_vis}, "Max visible", minval=1, maxval={max(n, 1)})',
        'lineHorizonBars = input.int(48, "Line bars", minval=4, maxval=200)',
        'showLocal = input.bool(true, "LOCAL protected")',
        'showEffective = input.bool(true, "EFFECTIVE protected")',
        'showMarkers = input.bool(true, "Break/reclaim/exit markers")',
        "",
        f"setupIds = array.from({', '.join(_pine_int(x) for x in sub['setup_id'])})",
        f"sides = array.from({', '.join(_pine_int(1 if d == 'long' else -1) for d in sub['direction'])})",
        f"fillTimes = array.from({', '.join(_ts(x) for x in sub['entry_time'])})",
        f"entryPx = array.from({', '.join(_pine_float(x) for x in sub['entry_price'])})",
        f"localProt = array.from({', '.join(_pine_float(x) for x in sub['local_protected_level'])})",
        f"effProt = array.from({', '.join(_pine_float(x) for x in sub['effective_protected_level'])})",
        f"srcIds = array.from({', '.join(_pine_int(x) for x in sub['carry_source_setup_id'])})",
        f"localBrBarsSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.local_break_bar)) for _, r in sub.iterrows())})",
        f"effBrBarsSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.effective_break_bar)) for _, r in sub.iterrows())})",
        f"localRecBarsSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.local_reclaim_bar)) for _, r in sub.iterrows())})",
        f"entryRecBarsSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.entry_reclaim_bar)) for _, r in sub.iterrows())})",
        f"hasLocalRec = array.from({', '.join('1' if pd.notna(r.local_reclaim_bar) else '0' for _, r in sub.iterrows())})",
        f"hasEntryRec = array.from({', '.join('1' if pd.notna(r.entry_reclaim_bar) else '0' for _, r in sub.iterrows())})",
        f"exitBarsSince = array.from({', '.join(_pine_int(_bars_since(r.entry_bar, r.exit_bar)) for _, r in sub.iterrows())})",
        f"pnlNet = array.from({', '.join(_pine_float(x) for x in sub['pnl_pct_net'])})",
        "",
        "var line[] locL = array.new_line()",
        "var line[] effL = array.new_line()",
        "var label[] labs = array.new_label()",
        "var bool drawn = false",
        "barMs = timeframe.in_seconds() * 1000",
        "clearAll() =>",
        "    if array.size(locL) > 0",
        "        for j = 0 to array.size(locL) - 1",
        "            line.delete(array.get(locL, j))",
        "        array.clear(locL)",
        "    if array.size(effL) > 0",
        "        for j = 0 to array.size(effL) - 1",
        "            line.delete(array.get(effL, j))",
        "        array.clear(effL)",
        "    if array.size(labs) > 0",
        "        for j = 0 to array.size(labs) - 1",
        "            label.delete(array.get(labs, j))",
        "        array.clear(labs)",
        "",
        "drawSetup(i) =>",
        "    t0 = array.get(fillTimes, i)",
        "    t1 = t0 + lineHorizonBars * barMs",
        "    ep = array.get(entryPx, i)",
        "    loc = array.get(localProt, i)",
        "    eff = array.get(effProt, i)",
        "    sid = array.get(setupIds, i)",
        "    side = array.get(sides, i)",
        "    array.push(labs, label.new(t0, ep, (side > 0 ? 'LONG #' : 'SHORT #') + str.tostring(sid), xloc=xloc.bar_time, style=side > 0 ? label.style_label_up : label.style_label_down, color=side > 0 ? color.teal : color.fuchsia, textcolor=color.white, size=size.small))",
        "    if showLocal",
        "        array.push(locL, line.new(t0, loc, t1, loc, xloc=xloc.bar_time, color=color.red, width=1))",
        "    if showEffective",
        "        array.push(effL, line.new(t0, eff, t1, eff, xloc=xloc.bar_time, color=color.maroon, width=2))",
        "        array.push(labs, label.new(t0, eff, 'EFFECTIVE from #' + str.tostring(array.get(srcIds, i)), xloc=xloc.bar_time, style=label.style_label_left, color=color.maroon, textcolor=color.white, size=size.tiny))",
        "    if showMarkers",
        "        tlb = t0 + array.get(localBrBarsSince, i) * barMs",
        "        array.push(labs, label.new(tlb, loc, 'LOCAL BREAK', xloc=xloc.bar_time, style=label.style_label_down, color=color.red, textcolor=color.white, size=size.tiny))",
        "        if array.get(hasLocalRec, i) == 1",
        "            tlr = t0 + array.get(localRecBarsSince, i) * barMs",
        "            array.push(labs, label.new(tlr, loc, 'LOCAL RECLAIM', xloc=xloc.bar_time, style=label.style_label_up, color=color.orange, textcolor=color.black, size=size.tiny))",
        "        if array.get(hasEntryRec, i) == 1",
        "            ter = t0 + array.get(entryRecBarsSince, i) * barMs",
        "            array.push(labs, label.new(ter, ep, 'ENTRY RECLAIM', xloc=xloc.bar_time, style=label.style_label_up, color=color.yellow, textcolor=color.black, size=size.tiny))",
        "        teb = t0 + array.get(effBrBarsSince, i) * barMs",
        "        array.push(labs, label.new(teb, eff, 'EFFECTIVE BREAK', xloc=xloc.bar_time, style=label.style_label_down, color=color.maroon, textcolor=color.white, size=size.tiny))",
        "        tx = t0 + array.get(exitBarsSince, i) * barMs",
        "        array.push(labs, label.new(tx, ep, 'MANAGED EXIT ' + str.tostring(array.get(pnlNet, i), '#.##') + '%', xloc=xloc.bar_time, style=label.style_label_up, color=color.blue, textcolor=color.white, size=size.tiny))",
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
        "plot(array.get(srcIds, nSetups - 1), 'carry_source_setup_id', display=display.data_window)",
        "plot(array.get(localProt, nSetups - 1), 'local_protected', display=display.data_window)",
        "plot(array.get(effProt, nSetups - 1), 'effective_protected', display=display.data_window)",
        "plot(array.get(pnlNet, nSetups - 1), 'managed_exit_pnl_net_pct', display=display.data_window)",
        "",
    ]
    text = "\n".join(lines) + "\n"
    validate_pine_script(text)
    return text


def run_audit(
    *,
    apt_dir: Path = DEFAULT_APT_DIR,
    output_dir: Path = DEFAULT_OUT,
    pine_dir: Path = PINE_DIR,
    frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    apt_dir = Path(apt_dir)
    output_dir = Path(output_dir)
    pine_dir = Path(pine_dir)
    # refuse clobbering sibling research dirs
    for forbidden in ("protected_carry", "protected_break_path"):
        if output_dir.resolve() == (apt_dir / forbidden).resolve():
            raise RuntimeError(f"refusing to write into {forbidden}")
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

    events: list[PathEvents] = []
    for s in setups:
        ev = build_path_events(s, ohlc)
        if ev is not None:
            events.append(ev)

    per_rows: list[dict[str, Any]] = []
    reclaim_rows: list[dict[str, Any]] = []
    recovery_rows: list[dict[str, Any]] = []

    for ev in events:
        for fill_mode in FILL_MODES:
            cands = all_candidates(ohlc, ev, fill_mode)
            by_name = {c.candidate: c for c in cands}
            b0 = by_name["B0_immediate_local"]
            b1 = by_name["B1_effective_break"]
            rec = scan_reclaims(ohlc, ev)
            reclaim_rows.append(
                {
                    "setup_id": ev.setup_id,
                    "direction": ev.direction,
                    "local_break_bar": ev.local_break_bar,
                    "local_reclaim_bar": rec["local_reclaim_bar"],
                    "entry_reclaim_bar": rec["entry_reclaim_bar"],
                    "effective_break_bar": ev.effective_break_bar,
                    "delayed_case": ev.effective_break_bar is None
                    or int(ev.effective_break_bar) > int(ev.local_break_bar),  # type: ignore[arg-type]
                }
            )
            # recovery target hits (research path)
            lb = int(ev.local_break_bar)  # type: ignore[arg-type]
            end = window_end(ev)
            for r_tgt in R_TARGETS:
                tgt_pct = (r_tgt * ev.r_unit / ev.entry_price) * 100.0
                hit_bar = None
                for bi in range(lb + 1, end + 1):
                    if bi not in ohlc.index:
                        continue
                    h, l, c = bar_ohlc(ohlc, bi)
                    if target_hit_pct(
                        side=ev.side,
                        entry=ev.entry_price,
                        high=h,
                        low=l,
                        close=c,
                        target_pnl_pct=tgt_pct,
                        fill_mode="close_only",
                    ):
                        hit_bar = bi
                        break
                recovery_rows.append(
                    {
                        "setup_id": ev.setup_id,
                        "r_target": r_tgt,
                        "target_pnl_pct": tgt_pct,
                        "hit_bar": hit_bar,
                        "bars_after_local": (hit_bar - lb) if hit_bar is not None else None,
                        "hit_before_effective": hit_bar is not None,
                    }
                )
            for fee in FEE_BPS_RT:
                for res in cands:
                    per_rows.append(
                        enrich_fill_row(
                            ev, ohlc, res, fill_mode=fill_mode, fee_bps=float(fee), b0=b0, b1=b1
                        )
                    )

    per = pd.DataFrame(per_rows)
    per = per.sort_values(
        ["setup_id", "candidate", "fill_semantics", "fee_bps_rt"]
    ).reset_index(drop=True)
    summary = pd.concat(
        [summarize(per, scope=s) for s in ("all", "long", "short", "delayed")],
        ignore_index=True,
    )
    vs_local = summary.copy()
    vs_eff = summary.copy()
    delayed = per[per["delayed_case"] == True].copy()  # noqa: E712
    reclaim_df = pd.DataFrame(reclaim_rows)
    recovery_df = pd.DataFrame(recovery_rows)

    partial = summary[summary["candidate"].astype(str).str.startswith("M6_")].copy()
    time_mat = summary[summary["candidate"].astype(str).str.startswith("M5_")].copy()
    fee_sens = summary[
        (summary["scope"] == "delayed")
        & (summary["fill_semantics"] == "close_only")
        & (~summary["candidate"].astype(str).str.startswith("B_ORACLE"))
    ][
        [
            "candidate",
            "fee_scenario",
            "n",
            "median_improvement_vs_B0_pct",
            "mean_improvement_vs_B0_pct",
            "worse_than_B0_count",
            "better_than_B0_count",
        ]
    ].copy()

    # tail risk vs B0
    tail = (
        per[
            (per["fill_semantics"] == "close_only")
            & (per["fee_bps_rt"] == 10)
            & (per["uses_future_information"] == False)  # noqa: E712
            & (per["delayed_case"] == True)  # noqa: E712
        ]
        .sort_values("improvement_vs_B0_net_pct")
        .head(20)
    )

    rec_json = build_recommendation(summary)

    per.to_csv(output_dir / "exit_management_per_fill.csv", index=False)
    summary.to_csv(output_dir / "exit_management_summary.csv", index=False)
    vs_local.to_csv(output_dir / "exit_management_comparison_vs_local.csv", index=False)
    vs_eff.to_csv(output_dir / "exit_management_comparison_vs_effective.csv", index=False)
    delayed.to_csv(output_dir / "delayed_cases_detailed.csv", index=False)
    reclaim_df.to_csv(output_dir / "reclaim_events.csv", index=False)
    recovery_df.to_csv(output_dir / "recovery_target_hits.csv", index=False)
    partial.to_csv(output_dir / "partial_reduce_summary.csv", index=False)
    time_mat.to_csv(output_dir / "time_limit_matrix.csv", index=False)
    fee_sens.to_csv(output_dir / "fee_slippage_sensitivity.csv", index=False)
    tail.to_csv(output_dir / "tail_risk_cases.csv", index=False)
    (output_dir / "recommendation.json").write_text(
        json.dumps(json_safe(rec_json), indent=2) + "\n", encoding="utf-8"
    )

    pine = build_pine(delayed if not delayed.empty else per)
    pine_path = pine_dir / MAIN_PINE
    pine_path.write_text(pine, encoding="utf-8")

    n_local = len(events)
    n_delayed = int(sum(1 for e in events if e.effective_break_bar is None or int(e.effective_break_bar) > int(e.local_break_bar)))  # type: ignore[arg-type]

    # h24 delayed count for report parity with prior finding
    n_h24_delayed = 0
    for e in events:
        lb = int(e.local_break_bar)  # type: ignore[arg-type]
        if lb - e.fill_bar >= 24:
            continue
        if e.effective_break_bar is None or int(e.effective_break_bar) > lb:
            # delayed relative to local within available path; h24 filter: local in first 24
            if lb <= e.fill_bar + 23:
                n_h24_delayed += 1

    readme = "\n".join(
        [
            "# Protected Break Exit Management Audit",
            "",
            "After V_1LAG **local** protected break, can a causal managed exit beat immediate local exit?",
            "",
            "## Semantics",
            "- V_1LAG unchanged: `effective[n]=local[n-1]` (no max/min chain).",
            "- Local break = warning window start; Effective break = hard backstop.",
            "- B0 = exit at local break; B1 = hold to effective; B_ORACLE = research upper bound only.",
            "",
            "## Fill rules",
            "- `close_only`: signals on close; exit at close.",
            "- `conservative_intrabar`: targets on favorable extreme, stops on adverse; same-bar stop wins.",
            "- Stop for M7 active from **next bar** after local break.",
            "",
            f"- Local-break fills: `{n_local}`",
            f"- Delayed (local < effective or no effective): `{n_delayed}`",
            f"- Recommendation: `{rec_json['recommended_status']}` / `{rec_json.get('best_research_candidate')}`",
            "",
            "No runtime/bot change. No commit.",
            "",
        ]
    )
    (output_dir / "README.md").write_text(readme + "\n", encoding="utf-8")

    audit = {
        "phase": PHASE,
        "status": "OK",
        "n_fills_total": len(setups),
        "n_local_break_fills": n_local,
        "n_delayed_full_path": n_delayed,
        "n_h24_local_then_delayed": n_h24_delayed,
        "recommendation": rec_json,
        "pine_path": str(pine_path),
        "output_dir": str(output_dir),
        "fill_semantics_doc": {
            "close_only": "exit/trigger on bar close",
            "conservative_intrabar": "target=favorable extreme; stop=adverse extreme; same-bar stop priority",
            "m7_stop_activation": "from local_break_bar+1",
            "fees": "round-trip bps subtracted from pnl_pct",
            "R_unit": "|entry-local_protected| else ATR",
        },
        "v1lag_semantics_unchanged": True,
        "no_maxmin_chain_carry": True,
        "no_runtime_change": True,
        "no_lookahead_in_recommended": True,
        "no_commit": True,
        "parent_dirs_not_overwritten": ["protected_carry", "protected_break_path"],
        "data_meta": {k: meta[k] for k in meta if k != "frame15_meta"} if meta else {},
    }
    (output_dir / "audit_summary.json").write_text(
        json.dumps(json_safe(audit), indent=2) + "\n", encoding="utf-8"
    )
    return audit


def main() -> None:
    p = argparse.ArgumentParser(description="C3.5D protected-break exit management audit")
    p.add_argument("--apt-dir", type=Path, default=DEFAULT_APT_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--pine-dir", type=Path, default=PINE_DIR)
    args = p.parse_args()
    audit = run_audit(apt_dir=args.apt_dir, output_dir=args.output_dir, pine_dir=args.pine_dir)
    print(json.dumps(json_safe(audit), indent=2))


if __name__ == "__main__":
    main()
