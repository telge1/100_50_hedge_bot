"""Compare Rule-D blocked vs allowed profitable winners on trade-quality metrics.

Backtest-only research helper. Reads existing continuous-result + coverage-audit
artifacts; does not modify live strategy or hedge backtester logic.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .batch_audit import json_safe
from .trade_list_builder import absolute_start_index, is_positive_closed

RULE_D_BLOCKED_REGIMES = frozenset(
    {"transition", "bullish_trend_with_trend_weakness"}
)

SPEED_BINS = (
    ("sehr_schnell", 0, 12),
    ("schnell", 13, 48),
    ("mittel", 49, 144),
    ("langsam", 145, 576),
    ("sehr_langsam", 577, None),
)

COMPARE_METRICS = (
    "duration_candles",
    "duration_hours",
    "pnl",
    "pnl_per_hour",
    "highest_cycle",
    "cycle_fills_total",
    "refill_count",
    "maximum_adverse_excursion",
    "maximum_favorable_excursion",
    "largest_unrealized_loss",
)

CYCLE_FIRST_RE = re.compile(r"^CYCLE_(\d+)_LONG_ADD$")
CYCLE_SECOND_RE = re.compile(r"^CYCLE_(\d+)_SHORT_REDUCE$")


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _trade_number_from_id(trade_id: str) -> str:
    return str(trade_id).rsplit("_", 1)[-1]


def rule_d_group(combined_regime: object) -> str:
    regime = str(combined_regime or "")
    if regime in RULE_D_BLOCKED_REGIMES:
        return "rule_d_blocked_winner"
    return "rule_d_allowed_winner"


def speed_class(duration_candles: int | None) -> str:
    if duration_candles is None:
        return "unknown"
    n = int(duration_candles)
    for name, low, high in SPEED_BINS:
        if high is None:
            if n >= low:
                return name
        elif low <= n <= high:
            return name
    return "unknown"


def cycle_class(highest_cycle: int | None) -> str:
    if highest_cycle is None:
        return "unknown"
    n = int(highest_cycle)
    if n <= 0:
        return "0_cycles_direct_tp"
    if n == 1:
        return "1_cycle"
    if n == 2:
        return "2_cycles"
    if n == 3:
        return "3_cycles"
    return "4_plus_cycles"


def coverage_audit_path(coverage_dir: Path, trade_id: str) -> Path:
    num = _trade_number_from_id(trade_id)
    return (
        coverage_dir
        / f"APTUSDT_long_continuous_trade_{num}_conservative_live_pnl_coverage_audit.json"
    )


def load_coverage_audit(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"audit_rows": [], "missing": True}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "audit_rows": list(payload.get("audit_rows") or []),
        "metadata": payload.get("metadata") or {},
        "missing": False,
    }


def summarize_coverage_cycles(audit_rows: list[dict[str, Any]]) -> dict[str, Any]:
    first_legs = 0
    second_legs = 0
    highest = 0
    seen_first: set[int] = set()
    seen_second: set[int] = set()
    cycle_loss_pnls: list[float] = []
    events: list[tuple[pd.Timestamp, float, str]] = []

    for row in audit_rows:
        loss_purpose = str(row.get("loss_purpose") or "")
        cover_purpose = str(row.get("cover_purpose") or "")
        cycle_idx = int(row.get("cycle_index") or 0)

        first_match = CYCLE_FIRST_RE.match(loss_purpose)
        if first_match:
            cycle_no = int(first_match.group(1))
            if cycle_no not in seen_first:
                seen_first.add(cycle_no)
                first_legs += 1
            highest = max(highest, cycle_no, cycle_idx)
            loss_pnl = _finite(row.get("loss_pnl"))
            if loss_pnl is not None:
                cycle_loss_pnls.append(loss_pnl)
                ts_raw = row.get("loss_fill_timestamp") or row.get("cover_fill_timestamp")
                if ts_raw:
                    events.append((pd.Timestamp(ts_raw), loss_pnl, "loss"))

            # Count second legs only on genuine CYCLE_N_LONG_ADD rows.
            for token in cover_purpose.split("|"):
                token = token.strip()
                second_match = CYCLE_SECOND_RE.match(token)
                if not second_match:
                    continue
                second_no = int(second_match.group(1))
                if second_no not in seen_second:
                    seen_second.add(second_no)
                    second_legs += 1
                highest = max(highest, second_no, cycle_idx)
                cover_pnl = _finite(row.get("cover_pnl"))
                if cover_pnl is not None:
                    ts_raw = row.get("cover_fill_timestamp") or row.get(
                        "loss_fill_timestamp"
                    )
                    if ts_raw:
                        events.append((pd.Timestamp(ts_raw), cover_pnl, "cover"))

        if cycle_idx > highest and loss_purpose.startswith("CYCLE_"):
            highest = cycle_idx

    events_sorted = sorted(events, key=lambda item: item[0])
    running = 0.0
    mae = 0.0
    mfe = 0.0
    minutes_to_first_pos: float | None = None
    start_ts = events_sorted[0][0] if events_sorted else None
    for ts, delta, _kind in events_sorted:
        running += float(delta)
        mae = min(mae, running)
        mfe = max(mfe, running)
        if minutes_to_first_pos is None and running > 0 and start_ts is not None:
            minutes_to_first_pos = float((ts - start_ts).total_seconds() / 60.0)

    return {
        "highest_cycle": int(highest),
        "cycle_first_legs_filled": int(first_legs),
        "cycle_second_legs_filled": int(second_legs),
        "cycle_fills_total": int(first_legs + second_legs),
        "coverage_mae": float(mae),
        "coverage_mfe": float(mfe),
        "min_cycle_loss_pnl": float(min(cycle_loss_pnls)) if cycle_loss_pnls else 0.0,
        "minutes_to_first_positive_realized": minutes_to_first_pos,
        "has_cycle_activity": bool(first_legs or second_legs or highest),
    }


def estimate_refills(
    *,
    fills_count: int | None,
    cycle_first_legs: int,
    cycle_second_legs: int,
) -> tuple[bool, int]:
    """Estimate refill activity from fill surplus vs structure fills."""
    if fills_count is None:
        return False, 0
    # 2 initial entries + cycle legs + 2 final paired exits
    expected = 2 + int(cycle_first_legs) + int(cycle_second_legs) + 2
    extra = int(fills_count) - expected
    if extra <= 0:
        return False, 0
    # Refill/reload typically adds paired fills.
    refill_count = max(1, extra // 2) if extra >= 1 else 0
    return True, int(refill_count)


def compute_drawdown_excursion(run: dict[str, Any]) -> float:
    """Convert max_drawdown_pct into adverse USDT (negative number)."""
    dd_pct = _finite(run.get("max_drawdown_pct"))
    notional = _finite(run.get("base_notional_usdt")) or 100.0
    if dd_pct is None:
        return 0.0
    return -abs(dd_pct / 100.0 * notional)


def extract_winner_quality_row(
    *,
    regime_row: dict[str, Any],
    run: dict[str, Any],
    coverage: dict[str, Any],
    candle_interval_minutes: int = 5,
) -> dict[str, Any]:
    trade_id = str(regime_row.get("trade_id") or run.get("trade_block_id") or "")
    combined_regime = str(regime_row.get("combined_regime") or "")
    group = rule_d_group(combined_regime)

    start_ts = pd.Timestamp(run.get("start_time"))
    end_ts = pd.Timestamp(run.get("end_time"))
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    else:
        start_ts = start_ts.tz_convert("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")

    duration_candles = int(run.get("candles_processed") or 0)
    duration_minutes = float(duration_candles * candle_interval_minutes)
    duration_hours = duration_minutes / 60.0 if duration_minutes else 0.0

    pnl = _finite(run.get("overall_pnl"))
    if pnl is None:
        pnl = _finite(run.get("realized_pnl"))
    pnl = float(pnl or 0.0)
    pnl_per_hour = float(pnl / duration_hours) if duration_hours > 0 else None

    cycle_info = summarize_coverage_cycles(list(coverage.get("audit_rows") or []))
    highest_cycle = int(cycle_info["highest_cycle"])
    first_legs = int(cycle_info["cycle_first_legs_filled"])
    second_legs = int(cycle_info["cycle_second_legs_filled"])
    fills_count = int(run.get("fills_count") or 0)
    refill_executed, refill_count = estimate_refills(
        fills_count=fills_count,
        cycle_first_legs=first_legs,
        cycle_second_legs=second_legs,
    )

    recovery_activated = bool(run.get("recovery_activated"))
    addon_recovery = bool(run.get("addon_short_recovery_activated"))
    diagnostics = list(run.get("recovery_diagnostic_events") or [])
    diagnostic_flags = {
        str((event or {}).get("purpose") or "") for event in diagnostics
    }
    reload_or_recovery = (
        recovery_activated
        or addon_recovery
        or any("RECOVERY" in flag or "RELOAD" in flag for flag in diagnostic_flags)
    )

    dd_mae = compute_drawdown_excursion(run)
    coverage_mae = float(cycle_info["coverage_mae"])
    coverage_mfe = float(cycle_info["coverage_mfe"])
    # Prefer coverage running PnL when cycles exist; else drawdown / final pnl.
    if cycle_info["has_cycle_activity"]:
        maximum_adverse_excursion = float(min(coverage_mae, dd_mae))
        maximum_favorable_excursion = float(max(coverage_mfe, pnl))
        largest_unrealized_loss = float(
            min(coverage_mae, dd_mae, float(cycle_info["min_cycle_loss_pnl"]))
        )
    else:
        maximum_adverse_excursion = float(dd_mae)
        maximum_favorable_excursion = float(max(pnl, 0.0))
        largest_unrealized_loss = float(dd_mae)

    minutes_to_first_pos = cycle_info.get("minutes_to_first_positive_realized")
    if minutes_to_first_pos is None:
        # Direct TP / no cycle path: treat first positive at close if pnl > 0.
        minutes_to_first_pos = duration_minutes if pnl > 0 else None

    last_fill = run.get("last_fill") or {}
    final_exit_purpose = str(last_fill.get("purpose") or run.get("exit_reason") or "")
    closed_via_normal_tp = highest_cycle == 0 and pnl > 0
    multiple_cycles_required = highest_cycle >= 2

    abs_start = absolute_start_index(run)
    # Prefer absolute index already stored on regime row when present.
    start_index = regime_row.get("start_index")
    if start_index is None:
        start_index = abs_start

    return {
        "trade_id": trade_id,
        "start_index": int(start_index),
        "start_timestamp": start_ts.isoformat(),
        "end_timestamp": end_ts.isoformat(),
        "duration_candles": duration_candles,
        "duration_minutes": duration_minutes,
        "duration_hours": duration_hours,
        "pnl": pnl,
        "pnl_per_hour": pnl_per_hour,
        "rule_d_group": group,
        "combined_regime": combined_regime,
        "highest_cycle": highest_cycle,
        "cycle_first_legs_filled": first_legs,
        "cycle_second_legs_filled": second_legs,
        "cycle_fills_total": int(cycle_info["cycle_fills_total"]),
        "fills_count_total": fills_count,
        "refill_executed": bool(refill_executed),
        "refill_count": int(refill_count),
        "recovery_or_reload_active": bool(reload_or_recovery),
        "maximum_adverse_excursion": maximum_adverse_excursion,
        "maximum_favorable_excursion": maximum_favorable_excursion,
        "largest_unrealized_loss": largest_unrealized_loss,
        "minutes_to_first_positive_unrealized": minutes_to_first_pos,
        "minutes_to_final_close": duration_minutes,
        "final_exit_purpose": final_exit_purpose,
        "closed_via_normal_tp": bool(closed_via_normal_tp),
        "multiple_cycles_required": bool(multiple_cycles_required),
        "speed_class": speed_class(duration_candles),
        "cycle_class": cycle_class(highest_cycle),
        "max_drawdown_pct": _finite(run.get("max_drawdown_pct")),
        "exit_quality": run.get("exit_quality"),
        "final_status": run.get("final_status"),
        "undesirable_slow_or_3plus_cycles": bool(
            duration_candles > 144 or highest_cycle >= 3
        ),
    }


def _stat_block(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "p25": None,
            "p75": None,
            "p90": None,
        }
    series = pd.Series(values, dtype="float64")
    return {
        "count": int(series.shape[0]),
        "mean": float(series.mean()),
        "median": float(series.median()),
        "min": float(series.min()),
        "max": float(series.max()),
        "p25": float(series.quantile(0.25)),
        "p75": float(series.quantile(0.75)),
        "p90": float(series.quantile(0.90)),
    }


def build_group_comparison(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    frame = pd.DataFrame(rows)
    for group_name, group_df in frame.groupby("rule_d_group", dropna=False):
        for metric in COMPARE_METRICS:
            values = [
                float(v)
                for v in group_df[metric].tolist()
                if v is not None and not (isinstance(v, float) and math.isnan(v))
            ]
            stats = _stat_block(values)
            out.append(
                {
                    "rule_d_group": group_name,
                    "metric": metric,
                    **stats,
                }
            )
    return out


def build_speed_distribution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frame = pd.DataFrame(rows)
    out: list[dict[str, Any]] = []
    for group_name, group_df in frame.groupby("rule_d_group", dropna=False):
        n = len(group_df)
        for speed_name, _low, _high in SPEED_BINS:
            subset = group_df[group_df["speed_class"] == speed_name]
            out.append(
                {
                    "rule_d_group": group_name,
                    "speed_class": speed_name,
                    "trade_count": int(len(subset)),
                    "share": float(len(subset) / n) if n else 0.0,
                    "pnl_sum": float(subset["pnl"].sum()) if len(subset) else 0.0,
                    "pnl_mean": float(subset["pnl"].mean()) if len(subset) else None,
                    "duration_median": float(subset["duration_candles"].median())
                    if len(subset)
                    else None,
                }
            )
    return out


def build_cycle_distribution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frame = pd.DataFrame(rows)
    cycle_order = [
        "0_cycles_direct_tp",
        "1_cycle",
        "2_cycles",
        "3_cycles",
        "4_plus_cycles",
    ]
    out: list[dict[str, Any]] = []
    for group_name, group_df in frame.groupby("rule_d_group", dropna=False):
        n = len(group_df)
        for cycle_name in cycle_order:
            subset = group_df[group_df["cycle_class"] == cycle_name]
            out.append(
                {
                    "rule_d_group": group_name,
                    "cycle_class": cycle_name,
                    "trade_count": int(len(subset)),
                    "share": float(len(subset) / n) if n else 0.0,
                    "pnl_sum": float(subset["pnl"].sum()) if len(subset) else 0.0,
                    "pnl_mean": float(subset["pnl"].mean()) if len(subset) else None,
                    "duration_median": float(subset["duration_candles"].median())
                    if len(subset)
                    else None,
                }
            )
    return out


def build_cross_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frame = pd.DataFrame(rows)
    out: list[dict[str, Any]] = []
    grouped = frame.groupby(
        ["rule_d_group", "speed_class", "cycle_class"], dropna=False
    )
    for keys, subset in grouped:
        group_name, speed_name, cycle_name = keys
        out.append(
            {
                "rule_d_group": group_name,
                "speed_class": speed_name,
                "cycle_class": cycle_name,
                "trade_count": int(len(subset)),
                "pnl_sum": float(subset["pnl"].sum()),
                "pnl_median": float(subset["pnl"].median()),
                "duration_median_candles": float(subset["duration_candles"].median()),
            }
        )
    return sorted(
        out,
        key=lambda row: (
            str(row["rule_d_group"]),
            str(row["speed_class"]),
            str(row["cycle_class"]),
        ),
    )


def _group_category_stats(rows: list[dict[str, Any]], group_name: str) -> dict[str, Any]:
    subset = [r for r in rows if r.get("rule_d_group") == group_name]
    n = len(subset)
    if not n:
        return {"trade_count": 0}

    def _share(pred) -> dict[str, float | int]:
        count = sum(1 for r in subset if pred(r))
        return {"count": count, "share": float(count / n)}

    pnls = [float(r["pnl"]) for r in subset]
    pnl_per_hour = [
        float(r["pnl_per_hour"])
        for r in subset
        if r.get("pnl_per_hour") is not None
    ]
    interim_loss = [float(r["largest_unrealized_loss"]) for r in subset]
    durations = [float(r["duration_candles"]) for r in subset]
    cycles = [float(r["highest_cycle"]) for r in subset]
    mae = [float(r["maximum_adverse_excursion"]) for r in subset]

    very_fast = _share(lambda r: r.get("speed_class") in {"sehr_schnell", "schnell"})
    slow = _share(lambda r: r.get("speed_class") in {"langsam", "sehr_langsam"})
    direct_tp = _share(lambda r: r.get("cycle_class") == "0_cycles_direct_tp")
    c1 = _share(lambda r: r.get("cycle_class") == "1_cycle")
    c2 = _share(lambda r: r.get("cycle_class") == "2_cycles")
    c3 = _share(lambda r: r.get("cycle_class") == "3_cycles")
    c4 = _share(lambda r: r.get("cycle_class") == "4_plus_cycles")
    undesirable = _share(lambda r: bool(r.get("undesirable_slow_or_3plus_cycles")))

    return {
        "trade_count": n,
        "pnl_sum": float(sum(pnls)),
        "pnl_mean": float(sum(pnls) / n),
        "pnl_median": float(pd.Series(pnls).median()),
        "pnl_per_hour_mean": float(sum(pnl_per_hour) / len(pnl_per_hour))
        if pnl_per_hour
        else None,
        "pnl_per_hour_median": float(pd.Series(pnl_per_hour).median())
        if pnl_per_hour
        else None,
        "duration_candles_median": float(pd.Series(durations).median()),
        "duration_hours_median": float(pd.Series(durations).median() * 5.0 / 60.0),
        "highest_cycle_median": float(pd.Series(cycles).median()),
        "highest_cycle_mean": float(sum(cycles) / n),
        "mae_median": float(pd.Series(mae).median()),
        "largest_unrealized_loss_mean": float(sum(interim_loss) / n),
        "largest_unrealized_loss_median": float(pd.Series(interim_loss).median()),
        "very_fast_or_fast": very_fast,
        "slow_or_very_slow": slow,
        "direct_tp": direct_tp,
        "cycles_1": c1,
        "cycles_2": c2,
        "cycles_3": c3,
        "cycles_4_plus": c4,
        "undesirable_slow_or_3plus_cycles": undesirable,
        "closed_via_normal_tp_share": float(
            sum(1 for r in subset if r.get("closed_via_normal_tp")) / n
        ),
        "multiple_cycles_required_share": float(
            sum(1 for r in subset if r.get("multiple_cycles_required")) / n
        ),
        "recovery_or_reload_active_count": sum(
            1 for r in subset if r.get("recovery_or_reload_active")
        ),
    }


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = _group_category_stats(rows, "rule_d_blocked_winner")
    allowed = _group_category_stats(rows, "rule_d_allowed_winner")

    answers = {
        "blocked_slower_than_allowed": (
            (blocked.get("duration_candles_median") or 0)
            > (allowed.get("duration_candles_median") or 0)
        ),
        "blocked_more_cycles_than_allowed": (
            (blocked.get("highest_cycle_median") or 0)
            > (allowed.get("highest_cycle_median") or 0)
        ),
        "blocked_larger_interim_loss": (
            (blocked.get("largest_unrealized_loss_median") or 0)
            < (allowed.get("largest_unrealized_loss_median") or 0)
        ),
        "blocked_worse_pnl_per_hour": (
            (blocked.get("pnl_per_hour_median") or 0)
            < (allowed.get("pnl_per_hour_median") or 0)
        ),
        "allowed_mostly_fast_direct_tp": (
            ((allowed.get("very_fast_or_fast") or {}).get("share") or 0) >= 0.5
            and ((allowed.get("direct_tp") or {}).get("share") or 0) >= 0.5
        ),
        "blocked_undesirable_count": (
            blocked.get("undesirable_slow_or_3plus_cycles") or {}
        ).get("count"),
        "allowed_undesirable_count": (
            allowed.get("undesirable_slow_or_3plus_cycles") or {}
        ).get("count"),
    }

    # Qualitative verdict for Rule D as quality filter.
    quality_signal = (
        answers["blocked_slower_than_allowed"]
        or answers["blocked_more_cycles_than_allowed"]
        or answers["blocked_larger_interim_loss"]
        or answers["blocked_worse_pnl_per_hour"]
    )
    if quality_signal and answers["allowed_mostly_fast_direct_tp"]:
        verdict = (
            "partial_quality_signal: blocked winners are on average heavier, "
            "but Rule D still blocks many clean winners and is not a pure quality filter"
        )
    elif quality_signal:
        verdict = (
            "weak_quality_signal: blocked winners look somewhat heavier on some "
            "metrics, but overlap with allowed winners remains large"
        )
    else:
        verdict = (
            "not_a_quality_filter: Rule D largely blocks winners independent of "
            "speed/cycle quality"
        )

    return {
        "trade_count_total": len(rows),
        "blocked_count": blocked.get("trade_count", 0),
        "allowed_count": allowed.get("trade_count", 0),
        "blocked": blocked,
        "allowed": allowed,
        "answers": answers,
        "verdict": verdict,
        "notes": {
            "mae_source": (
                "coverage running realized PnL when cycles exist, else "
                "-max_drawdown_pct/100*notional"
            ),
            "refill_source": "estimated from fills_count surplus vs cycle structure",
            "undesirable_definition": "duration_candles > 144 OR highest_cycle >= 3",
        },
    }


def run_winner_quality_audit(
    *,
    regime_rows_csv: str | Path,
    result_file: str | Path,
    coverage_dir: str | Path,
    candle_interval_minutes: int = 5,
) -> dict[str, Any]:
    regime_df = pd.read_csv(regime_rows_csv)
    result_payload = json.loads(Path(result_file).read_text(encoding="utf-8"))
    runs_by_id = {
        str(run.get("trade_block_id") or run.get("trade_id")): run
        for run in (result_payload.get("runs") or [])
    }
    coverage_root = Path(coverage_dir)

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for _, series in regime_df.iterrows():
        regime_row = series.to_dict()
        trade_id = str(regime_row.get("trade_id") or "")
        try:
            run = runs_by_id.get(trade_id)
            if run is None:
                raise KeyError(f"trade_id not found in result file: {trade_id}")
            if not is_positive_closed(run):
                raise ValueError(f"trade is not positive closed: {trade_id}")
            coverage = load_coverage_audit(coverage_audit_path(coverage_root, trade_id))
            row = extract_winner_quality_row(
                regime_row=regime_row,
                run=run,
                coverage=coverage,
                candle_interval_minutes=candle_interval_minutes,
            )
            rows.append(row)
        except Exception as exc:  # noqa: BLE001 - keep batch complete
            errors.append({"trade_id": trade_id, "error_message": str(exc)})

    summary = build_summary(rows)
    summary["errors"] = errors
    summary["successes"] = len(rows)
    summary["error_count"] = len(errors)

    return {
        "rows": rows,
        "group_comparison": build_group_comparison(rows),
        "speed_distribution": build_speed_distribution(rows),
        "cycle_distribution": build_cycle_distribution(rows),
        "cross_table": build_cross_table(rows),
        "summary": summary,
        "inputs": {
            "regime_rows_csv": str(regime_rows_csv),
            "result_file": str(result_file),
            "coverage_dir": str(coverage_dir),
        },
    }


def format_readme(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    blocked = summary.get("blocked") or {}
    allowed = summary.get("allowed") or {}
    answers = summary.get("answers") or {}
    lines = [
        "# Winner Quality Audit (Rule D)",
        "",
        "Compares profitable closed trades blocked vs allowed by Rule D "
        "(`transition` + `bullish_trend_with_trend_weakness`).",
        "",
        "## Counts",
        "",
        f"- Total winners analyzed: **{summary.get('trade_count_total')}**",
        f"- rule_d_blocked_winner: **{summary.get('blocked_count')}**",
        f"- rule_d_allowed_winner: **{summary.get('allowed_count')}**",
        f"- Successes/errors: **{summary.get('successes')}** / **{summary.get('error_count')}**",
        "",
        "## Key medians",
        "",
        "| Metric | Blocked | Allowed |",
        "|---|---:|---:|",
        f"| Duration (candles) | {blocked.get('duration_candles_median')} | {allowed.get('duration_candles_median')} |",
        f"| Highest cycle | {blocked.get('highest_cycle_median')} | {allowed.get('highest_cycle_median')} |",
        f"| PnL / hour | {blocked.get('pnl_per_hour_median')} | {allowed.get('pnl_per_hour_median')} |",
        f"| MAE | {blocked.get('mae_median')} | {allowed.get('mae_median')} |",
        f"| Largest interim loss | {blocked.get('largest_unrealized_loss_median')} | {allowed.get('largest_unrealized_loss_median')} |",
        "",
        "## Speed / cycle shares",
        "",
        f"- Blocked fast/sehr_schnell: `{blocked.get('very_fast_or_fast')}`",
        f"- Allowed fast/sehr_schnell: `{allowed.get('very_fast_or_fast')}`",
        f"- Blocked slow/sehr_langsam: `{blocked.get('slow_or_very_slow')}`",
        f"- Allowed slow/sehr_langsam: `{allowed.get('slow_or_very_slow')}`",
        f"- Blocked direct TP: `{blocked.get('direct_tp')}`",
        f"- Allowed direct TP: `{allowed.get('direct_tp')}`",
        "",
        "## Answers",
        "",
    ]
    for key, value in answers.items():
        lines.append(f"- `{key}`: **{value}**")
    lines.extend(
        [
            "",
            f"## Verdict",
            "",
            f"{summary.get('verdict')}",
            "",
            "## Notes",
            "",
            f"`{summary.get('notes')}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(payload: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = {
        "rows": out_dir / "winner_trade_quality.csv",
        "group_comparison": out_dir / "winner_group_comparison.csv",
        "speed_distribution": out_dir / "winner_speed_distribution.csv",
        "cycle_distribution": out_dir / "winner_cycle_distribution.csv",
        "cross_table": out_dir / "winner_speed_cycle_cross_table.csv",
        "summary_json": out_dir / "winner_quality_summary.json",
        "readme": out_dir / "README.md",
    }

    pd.DataFrame(payload["rows"]).to_csv(paths["rows"], index=False)
    pd.DataFrame(payload["group_comparison"]).to_csv(
        paths["group_comparison"], index=False
    )
    pd.DataFrame(payload["speed_distribution"]).to_csv(
        paths["speed_distribution"], index=False
    )
    pd.DataFrame(payload["cycle_distribution"]).to_csv(
        paths["cycle_distribution"], index=False
    )
    pd.DataFrame(payload["cross_table"]).to_csv(paths["cross_table"], index=False)
    paths["summary_json"].write_text(
        json.dumps(json_safe(payload["summary"]), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    paths["readme"].write_text(format_readme(payload), encoding="utf-8")
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quality audit of Rule-D blocked vs allowed profitable winners.",
    )
    parser.add_argument(
        "--regime-rows-csv",
        default=(
            "research/backtests/results/regime_scanner_profitable_batch/"
            "profitable_regime_audit_rows.csv"
        ),
    )
    parser.add_argument(
        "--result-file",
        default=(
            "research/backtests/results/full_history_continuous_long_recovery/"
            "APTUSDT_original_hedge_5m_continuous_results.json"
        ),
    )
    parser.add_argument(
        "--coverage-dir",
        default="research/backtests/results/full_history_continuous_long_recovery",
    )
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_winner_quality_audit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    payload = run_winner_quality_audit(
        regime_rows_csv=args.regime_rows_csv,
        result_file=args.result_file,
        coverage_dir=args.coverage_dir,
    )
    paths = write_outputs(payload, args.output_dir)
    summary = payload["summary"]
    print(
        f"Winner quality audit: total={summary.get('trade_count_total')} "
        f"blocked={summary.get('blocked_count')} allowed={summary.get('allowed_count')} "
        f"errors={summary.get('error_count')}"
    )
    print(f"Verdict: {summary.get('verdict')}")
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
