"""Multi-coin baseline-blocker price-staging audit helpers (research-only, 1000/500).

Reuses the APT T3 research prototype profiles unchanged (including ``only_cycles=(4,)``).
Part A: isolated blocker starts. No live/runtime mutation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from research.backtests.apt_baseline_blocker_root_cause import (
    _active_exit_at_local_candle,
    _candle_high,
    _purpose,
)
from research.backtests.apt_t3_short_reduce_price_staging_lab import (
    BOUNCE_HIGH as APT_BOUNCE_HIGH,
)
from research.backtests.backtest_report import BacktestResult
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.inventory_mtm_freeze import inventory_mtm_usdt, safe_float
from research.backtests.long_add_multistart_metrics import analyze_trade, normalize_trade_status
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    FILL_MODEL,
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
)

# Re-export for CLI convenience
__all_reexport = ("FULL_HISTORY_CANDLE_LIMIT",)
from research.backtests.safe_cycle_boundary_freeze import detect_invalid_partial_cycle
from research.backtests.second_leg_price_staging import (
    SecondLegPriceStagingConfig,
    resolve_profile,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = (
    ROOT / "research/backtests/results/multicoin_blocker_price_staging_1000_500_20260721"
)
DEFAULT_BASELINE = BASELINE_DIR
PROTECTED = (
    BASELINE_DIR,
    ROOT / "research/backtests/results/safe_cycle_boundary_freeze_audit_20260720",
    ROOT / "research/backtests/results/long_baseline_1000_500_stage_tp_audit_20260721",
    ROOT / "research/backtests/results/apt_baseline_blocker_root_cause_20260721",
    ROOT / "research/backtests/results/apt_t3_stage_tp_size_comparison_20260721",
    ROOT / "research/backtests/results/second_leg_price_staging_code_audit_20260721",
    ROOT / "research/backtests/results/apt_t3_short_reduce_price_staging_lab_20260721",
)

LONG_NOTIONAL = 1000.0
SHORT_NOTIONAL = 500.0
SCALE_TO_100 = 100.0 / LONG_NOTIONAL  # normalize metrics to 100 USDT long

# APTUSDT T3 S1000 prototype expectations (apt_t3_short_reduce_price_staging_lab_20260721)
APT_PROTOTYPE = {
    "coin": "APTUSDT",
    "trade_number": 3,
    "start_index": 570,
    "legacy": {
        "trade_flat": False,
        "final_mtm": -308.19357690378547,
        "exit_at_bounce": 2.096,
        "bounce_reaches": False,
        "mtm_tol": 1.0,
        "exit_tol": 0.01,
    },
    "linear4": {
        "trade_flat": True,
        "final_mtm": 5.22428700288499,
        "exit_at_bounce": 1.9062,
        "bounce_reaches": True,
        "mtm_tol": 0.5,
        "exit_tol": 0.02,
    },
    "conservative3": {
        "trade_flat": True,
        "final_mtm": 3.3125465962549985,
        "exit_at_bounce": 1.8988,
        "bounce_reaches": True,
        "mtm_tol": 0.5,
        "exit_tol": 0.02,
    },
    "small_early4": {
        "trade_flat": True,
        "final_mtm": 3.799987705755047,
        "exit_at_bounce": 1.9048,
        "bounce_reaches": True,
        "mtm_tol": 0.5,
        "exit_tol": 0.02,
    },
}

CYCLE_SR_RE = re.compile(r"^CYCLE_(\d+)_SHORT_REDUCE$")
CYCLE_LA_RE = re.compile(r"^CYCLE_(\d+)_LONG_ADD$")


def assert_output_dir_safe(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    for protected in PROTECTED:
        if resolved == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output dir: {output_dir}")


def parse_profiles(spec: str) -> list[SecondLegPriceStagingConfig]:
    names = [p.strip() for p in str(spec or "").split(",") if p.strip()]
    if not names:
        names = ["legacy"]
    return [resolve_profile(n) for n in names]


def run_isolated_blocker(
    *,
    coin: str,
    candles: list[Any],
    start_index: int,
    staging_config: SecondLegPriceStagingConfig,
    trade_number: int | None = None,
) -> BacktestResult:
    window = candles[int(start_index) :]
    result = run_historical_backtest(
        coin.upper(),
        "long",
        window,
        config_source="live",
        fill_model=FILL_MODEL,
        tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
        long_fill_distance_pct=LONG_FILL_DISTANCE_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
        base_notional_usdt=LONG_NOTIONAL,
        initial_notional_usdt=LONG_NOTIONAL,
        absolute_trade_start_index=int(start_index),
        second_leg_price_staging_config=staging_config if staging_config.enabled else None,
    )
    result.start_index = int(start_index)
    if trade_number is not None:
        result.trade_number = int(trade_number)
    return result


def _staged_intents(result: BacktestResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for intent in result.intent_log or []:
        purpose = str(intent.get("purpose") or "")
        m = CYCLE_SR_RE.match(purpose)
        if not m:
            continue
        meta = dict(intent.get("metadata_excerpt") or {})
        if not (meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp")):
            continue
        rows.append(
            {
                "cycle": int(m.group(1)),
                "stage_index": int(meta.get("stage_index") or 0),
                "trigger": safe_float(intent.get("trigger_price")),
                "qty": safe_float(intent.get("qty")),
                "candle": intent.get("candle_index"),
            }
        )
    return rows


def _sr_fills(result: BacktestResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fill in result.fill_log or []:
        purpose = _purpose(fill)
        m = CYCLE_SR_RE.match(purpose)
        if not m:
            continue
        meta = dict(fill.get("metadata_excerpt") or {})
        rows.append(
            {
                "cycle": int(m.group(1)),
                "stage_index": meta.get("stage_index"),
                "local_candle": int(fill.get("candle_index") or 0),
                "fill_price": safe_float(fill.get("fill_price")),
                "qty": safe_float(fill.get("qty")),
                "pnl": safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl")),
                "research_staged": bool(
                    meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp")
                ),
            }
        )
    return rows


def analyze_blocker_run(
    *,
    coin: str,
    trade_number: int,
    start_index: int,
    profile: str,
    result: BacktestResult,
    candles: list[Any],
    baseline_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    window = candles[int(start_index) :]
    analysis = analyze_trade(
        result,
        variant=f"{coin}:{profile}",
        long_add_pct=0.5,
        target_profit_usdt=TARGET_PROFIT_USDT,
        window_candles=window,
        valid=True,
        skip_reason="ok",
    )
    status = normalize_trade_status(result)
    flat = status == "closed"
    final_mtm = safe_float(analysis.get("mtm_pnl"))
    invalid = detect_invalid_partial_cycle(dict(result.final_strategy_state_excerpt or {}))
    undercoverage = int(safe_float(analysis.get("undercoverage")))

    staged_intents = _staged_intents(result)
    sr_fills = _sr_fills(result)
    staged_fills = [f for f in sr_fills if f.get("research_staged") or f.get("stage_index") is not None]
    planned_stages = len({(r["cycle"], r["stage_index"]) for r in staged_intents})
    distinct_triggers = len({round(r["trigger"], 6) for r in staged_intents if r["trigger"] > 0})
    staging_activated = planned_stages >= 2 and distinct_triggers >= 2
    fallback_single = bool(staged_intents) and planned_stages <= 1

    # If no research staging intents but enabled profile: likely reduce_stage_count→1 / no C4
    order_log = list(result.order_log or [])

    # Exit path around first staged (or first SR) fill
    first_fill = None
    for f in sr_fills:
        if staging_activated and not f.get("research_staged") and f.get("stage_index") is None:
            continue
        first_fill = f
        break
    if first_fill is None and sr_fills:
        first_fill = sr_fills[0]

    exit_before = None
    exit_after_first = None
    strongest_drop = None
    first_stage_fill_candle = None
    if first_fill is not None:
        first_stage_fill_candle = int(first_fill["local_candle"])
        exit_before = _active_exit_at_local_candle(
            order_log, local_candle=max(first_stage_fill_candle - 1, 0)
        )
        exit_after_first = _active_exit_at_local_candle(
            order_log, local_candle=first_stage_fill_candle
        )
        # track min exit after any subsequent SR fill
        exits_after = []
        for f in sr_fills:
            ex = _active_exit_at_local_candle(order_log, local_candle=int(f["local_candle"]))
            if ex is not None:
                exits_after.append(ex)
        if exit_before is not None and exits_after:
            strongest_drop = float(exit_before) - min(exits_after)

    # Bounce reachability: after exit is lowered (or after long_add), does later high reach exit?
    bounce_reaches = False
    bounce_ref_high = None
    exit_at_bounce_window = exit_after_first or exit_before
    scan_from = first_stage_fill_candle
    if scan_from is None:
        # fall back: first LONG_ADD of max cycle / cycle 4
        for fill in result.fill_log or []:
            if CYCLE_LA_RE.match(_purpose(fill)):
                scan_from = int(fill.get("candle_index") or 0)
                exit_at_bounce_window = _active_exit_at_local_candle(order_log, local_candle=scan_from)
    if scan_from is not None and exit_at_bounce_window is not None:
        max_high = 0.0
        end_local = int(result.candles_processed or 0)
        for local in range(int(scan_from) + 1, end_local + 1):
            abs_i = int(start_index) + local
            if abs_i >= len(candles):
                break
            max_high = max(max_high, _candle_high(candles[abs_i]))
            if max_high + 1e-9 >= float(exit_at_bounce_window):
                bounce_reaches = True
                break
        bounce_ref_high = max_high

    # APT-specific bounce high check for parity with apt_t3 lab
    apt_bounce_exit = None
    apt_bounce_reaches = None
    if coin.upper() == "APTUSDT" and int(start_index) == 570:
        long_local = None
        for fill in result.fill_log or []:
            if _purpose(fill) == "CYCLE_4_LONG_ADD":
                long_local = int(fill.get("candle_index") or 0)
                break
        bounce_local = None
        end_local = max(int(result.candles_processed or 0), 5000)
        if long_local is not None:
            for local in range(long_local + 1, end_local + 1):
                abs_i = int(start_index) + local
                if abs_i >= len(candles):
                    break
                if _candle_high(candles[abs_i]) + 1e-9 >= APT_BOUNCE_HIGH:
                    bounce_local = local
                    break
        if bounce_local is not None:
            apt_bounce_exit = _active_exit_at_local_candle(order_log, local_candle=bounce_local)
            # If already flat before bounce candle, fall back to last known exit after stage fills.
            if apt_bounce_exit is None:
                apt_bounce_exit = exit_after_first or exit_before
        else:
            # Trade may have flattened when price first reached the lowered exit (before
            # the recorded bounce-high candle). Use post-stage exit vs bounce ref.
            apt_bounce_exit = exit_after_first or exit_before
        if apt_bounce_exit is not None:
            apt_bounce_reaches = bool(APT_BOUNCE_HIGH + 1e-9 >= float(apt_bounce_exit))
        elif flat and exit_after_first is not None:
            apt_bounce_exit = exit_after_first
            apt_bounce_reaches = bool(APT_BOUNCE_HIGH + 1e-9 >= float(exit_after_first))

    # Worst MTM along SR/long_add fills (coarse)
    worst_mtm = None
    cum = 0.0
    for fill in result.fill_log or []:
        purpose = _purpose(fill)
        if not (CYCLE_LA_RE.match(purpose) or CYCLE_SR_RE.match(purpose) or "EXIT" in purpose):
            continue
        local = int(fill.get("candle_index") or 0)
        abs_i = int(start_index) + local
        mark = (
            float(getattr(candles[abs_i], "close", 0.0) or 0.0)
            if abs_i < len(candles)
            else safe_float(fill.get("fill_price"))
        )
        cum += safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"))
        mtm = inventory_mtm_usdt(
            realized=cum,
            long_qty=safe_float(fill.get("long_qty_after")),
            long_avg=safe_float(fill.get("long_avg_after")),
            short_qty=safe_float(fill.get("short_qty_after")),
            short_avg=safe_float(fill.get("short_avg_after")),
            mark=mark,
        )
        if worst_mtm is None or mtm < worst_mtm:
            worst_mtm = mtm

    duration = int(result.candles_processed or analysis.get("duration_candles") or 0)
    max_cycle = int(analysis.get("max_cycle") or result.cycles_seen or 0)

    return {
        "coin": coin.upper(),
        "trade_number": int(trade_number),
        "start_index": int(start_index),
        "profile": profile,
        "baseline_100_50_mtm": safe_float((baseline_row or {}).get("mtm_pnl")),
        "baseline_100_50_status": str((baseline_row or {}).get("status") or ""),
        "status": status,
        "trade_flat": int(flat),
        "final_mtm": final_mtm,
        "realized_pnl": safe_float(result.realized_pnl),
        "worst_mtm": worst_mtm if worst_mtm is not None else final_mtm,
        "max_cycle": max_cycle,
        "duration_candles": duration,
        "planned_stages": planned_stages,
        "distinct_triggers": distinct_triggers,
        "filled_stages": len(staged_fills) if staging_activated else len(sr_fills),
        "staging_activated": int(staging_activated),
        "fallback_single_stage": int(fallback_single or (profile != "legacy" and not staging_activated)),
        "first_stage_fill_candle": first_stage_fill_candle,
        "exit_before_first_stage": exit_before,
        "exit_after_first_stage": exit_after_first,
        "strongest_exit_drop": strongest_drop,
        "bounce_ref_high": bounce_ref_high,
        "exit_at_bounce_window": exit_at_bounce_window,
        "bounce_reaches_exit": int(bounce_reaches),
        "apt_bounce_exit": apt_bounce_exit,
        "apt_bounce_reaches": apt_bounce_reaches,
        "gross_exposure": safe_float(analysis.get("max_total_notional")),
        "net_exposure": safe_float(analysis.get("max_abs_net_exposure")),
        "invalid_partial": int(bool(invalid)),
        "undercoverage": undercoverage,
        "same_candle_cascade": int(safe_float(analysis.get("same_candle_long_add_short_reduce"))),
        "final_mtm_per_100": final_mtm * SCALE_TO_100,
        "worst_mtm_per_100": (worst_mtm if worst_mtm is not None else final_mtm) * SCALE_TO_100,
        "gross_exposure_per_100": safe_float(analysis.get("max_total_notional")) * SCALE_TO_100,
        "error": result.error,
        "exit_reason": result.exit_reason,
    }


def classify_vs_legacy(
    *,
    staged: dict[str, Any],
    legacy: dict[str, Any],
) -> dict[str, Any]:
    """Classify staged outcome vs same-size M0 legacy control."""
    if staged.get("error") or legacy.get("error"):
        cls = "path_not_comparable"
    elif int(staged.get("invalid_partial") or 0) or int(staged.get("undercoverage") or 0):
        cls = "new_invalid_or_undercovered"
    else:
        s_flat = bool(int(staged.get("trade_flat") or 0))
        s_mtm = safe_float(staged.get("final_mtm"))
        l_mtm = safe_float(legacy.get("final_mtm"))
        improvement = s_mtm - l_mtm
        if s_flat and s_mtm > 0:
            cls = "closed_positive"
        elif s_flat and s_mtm <= 0 and improvement > 1e-6:
            cls = "closed_negative_but_improved"
        elif s_flat and improvement <= 1e-6:
            # closed but not better than legacy open mtm — still note as closed_negative
            cls = "closed_negative_but_improved" if improvement >= -1e-6 else "path_not_comparable"
        elif (not s_flat) and improvement > 1e-6:
            cls = "still_open_improved"
        elif (not s_flat) and improvement < -1e-6:
            cls = "still_open_worse"
        else:
            cls = "still_open_improved"  # unchanged open

    s_mtm = safe_float(staged.get("final_mtm"))
    l_mtm = safe_float(legacy.get("final_mtm"))
    improvement = s_mtm - l_mtm
    dur_s = int(staged.get("duration_candles") or 0)
    dur_l = int(legacy.get("duration_candles") or 0)
    avoided_duration = max(dur_l - dur_s, 0) if bool(int(staged.get("trade_flat") or 0)) else 0

    return {
        "classification": cls,
        "improvement_usdt": improvement,
        "improvement_per_100": improvement * SCALE_TO_100,
        "avoided_blocker_duration_candles": avoided_duration,
        "legacy_final_mtm": l_mtm,
        "staged_final_mtm": s_mtm,
        "legacy_flat": int(legacy.get("trade_flat") or 0),
        "staged_flat": int(staged.get("trade_flat") or 0),
    }


def check_apt_prototype_parity(rows_by_profile: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Abort gate: APTUSDT T3 @1000/500 must match the existing lab within tolerance."""
    checks: dict[str, Any] = {}
    ok = True
    for profile, expect in APT_PROTOTYPE.items():
        if profile in ("coin", "trade_number", "start_index"):
            continue
        row = rows_by_profile.get(profile)
        if row is None:
            checks[profile] = {"ok": False, "error": "missing_row"}
            ok = False
            continue
        flat_ok = bool(int(row.get("trade_flat") or 0)) == bool(expect["trade_flat"])
        mtm = safe_float(row.get("final_mtm"))
        mtm_ok = abs(mtm - float(expect["final_mtm"])) <= float(expect["mtm_tol"])
        # Prefer APT-specific bounce fields when present
        exit_val = row.get("apt_bounce_exit")
        if exit_val is None:
            exit_val = row.get("exit_at_bounce_window")
        exit_ok = True
        if expect.get("exit_at_bounce") is not None and exit_val is not None:
            exit_ok = abs(safe_float(exit_val) - float(expect["exit_at_bounce"])) <= float(
                expect["exit_tol"]
            )
        reach = row.get("apt_bounce_reaches")
        if reach is None:
            reach = bool(int(row.get("bounce_reaches_exit") or 0))
        reach_ok = bool(reach) == bool(expect["bounce_reaches"])
        profile_ok = flat_ok and mtm_ok and exit_ok and reach_ok
        checks[profile] = {
            "ok": profile_ok,
            "flat": [bool(int(row.get("trade_flat") or 0)), expect["trade_flat"], flat_ok],
            "final_mtm": [mtm, expect["final_mtm"], mtm_ok],
            "exit_at_bounce": [exit_val, expect["exit_at_bounce"], exit_ok],
            "bounce_reaches": [bool(reach), expect["bounce_reaches"], reach_ok],
        }
        ok = ok and profile_ok
    return {"ok": ok, "checks": checks, "reference": "apt_t3_short_reduce_price_staging_lab_20260721"}


def summarize_profile(rows: list[dict[str, Any]], *, profile: str) -> dict[str, Any]:
    n = len(rows)
    flat = [r for r in rows if int(r.get("trade_flat") or 0)]
    open_rows = [r for r in rows if not int(r.get("trade_flat") or 0)]
    pos = [r for r in flat if safe_float(r.get("final_mtm")) > 0]
    neg = [r for r in flat if safe_float(r.get("final_mtm")) <= 0]
    mtms = [safe_float(r.get("final_mtm")) for r in rows]
    worsts = [safe_float(r.get("worst_mtm")) for r in rows]
    gross = [safe_float(r.get("gross_exposure")) for r in rows]
    nets = [safe_float(r.get("net_exposure")) for r in rows]
    durs = [int(r.get("duration_candles") or 0) for r in flat]
    improved = sum(1 for r in rows if safe_float(r.get("improvement_usdt")) > 1e-6)
    worsened = sum(1 for r in rows if safe_float(r.get("improvement_usdt")) < -1e-6)

    def _med(vals: list[float]) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else 0.5 * (s[mid - 1] + s[mid])

    return {
        "profile": profile,
        "n_blockers": n,
        "closed": len(flat),
        "still_open": len(open_rows),
        "closed_positive": len(pos),
        "closed_negative": len(neg),
        "sum_closed_pnl": sum(safe_float(r.get("final_mtm")) for r in flat),
        "sum_open_mtm": sum(safe_float(r.get("final_mtm")) for r in open_rows),
        "total_mtm": sum(mtms),
        "median_final_mtm": _med(mtms),
        "worst_final_mtm": min(mtms) if mtms else None,
        "median_worst_mtm": _med(worsts),
        "worst_worst_mtm": min(worsts) if worsts else None,
        "avg_gross_exposure": (sum(gross) / len(gross)) if gross else None,
        "max_gross_exposure": max(gross) if gross else None,
        "avg_net_exposure": (sum(nets) / len(nets)) if nets else None,
        "max_net_exposure": max(nets) if nets else None,
        "median_time_to_flat": _med([float(d) for d in durs]),
        "fallback_single_count": sum(1 for r in rows if int(r.get("fallback_single_stage") or 0)),
        "staging_activated_count": sum(1 for r in rows if int(r.get("staging_activated") or 0)),
        "invalid_partial_sum": sum(int(r.get("invalid_partial") or 0) for r in rows),
        "undercoverage_sum": sum(int(r.get("undercoverage") or 0) for r in rows),
        "coins_improved_vs_legacy": improved,
        "coins_worsened_vs_legacy": worsened,
        "total_mtm_per_100": sum(mtms) * SCALE_TO_100,
        "worst_final_mtm_per_100": (min(mtms) * SCALE_TO_100) if mtms else None,
        "max_gross_exposure_per_100": (max(gross) * SCALE_TO_100) if gross else None,
        "closed_positive_class": sum(1 for r in rows if r.get("classification") == "closed_positive"),
        "closed_negative_improved_class": sum(
            1 for r in rows if r.get("classification") == "closed_negative_but_improved"
        ),
        "still_open_improved_class": sum(
            1 for r in rows if r.get("classification") == "still_open_improved"
        ),
        "still_open_worse_class": sum(
            1 for r in rows if r.get("classification") == "still_open_worse"
        ),
        "new_invalid_class": sum(
            1 for r in rows if r.get("classification") == "new_invalid_or_undercovered"
        ),
        "path_not_comparable_class": sum(
            1 for r in rows if r.get("classification") == "path_not_comparable"
        ),
    }


def write_case_markdown(path: Path, title: str, rows: list[dict[str, Any]], *, limit: int = 15) -> None:
    lines = [f"# {title}", ""]
    if not rows:
        lines.append("_none_")
    for r in rows[:limit]:
        lines.append(
            f"- **{r.get('coin')}** / {r.get('profile')}: class=`{r.get('classification')}` "
            f"flat={r.get('trade_flat')} mtm={r.get('final_mtm')} "
            f"Δ={r.get('improvement_usdt')} stages={r.get('planned_stages')}/"
            f"{r.get('filled_stages')} activated={r.get('staging_activated')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
