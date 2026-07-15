"""Batch regime audit for resolved blocker / loss-trade start times.

Backtest-only. Uses ``decision_time_after_close_utc`` (never start candle open)
as the causal decision timestamp for the multi-timeframe regime scanner.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

import pandas as pd

from .config import RegimeScannerConfig, default_regime_scanner_config
from .data_loader import load_symbol_candles
from .point_audit import build_point_audit, json_safe, parse_decision_time
from .timeframes import parse_timeframes
from .trade_list_builder import extract_trades_from_result_file

DECISION_TIME_COLUMN = "decision_time_after_close_utc"
REQUIRED_INPUT_COLUMNS = (
    "trade_id",
    DECISION_TIME_COLUMN,
)

STRICT_ALLOWED_REGIMES = frozenset({"strong_bullish_trend", "bullish_trend"})
STRICT_BLOCKED_REGIMES = frozenset(
    {
        "bullish_trend_with_trend_weakness",
        "transition",
        "neutral",
        "bearish_trend",
        "bearish_trend_with_trend_weakness",
        "strong_bearish_trend",
        "unavailable",
    }
)

FILTER_RULES: dict[str, dict[str, Any]] = {
    "A_block_bullish_trend_with_trend_weakness": {
        "label": "A",
        "description": "block only bullish_trend_with_trend_weakness",
        "blocked_regimes": frozenset({"bullish_trend_with_trend_weakness"}),
        "mode": "block_list",
    },
    "B_block_transition": {
        "label": "B",
        "description": "block only transition",
        "blocked_regimes": frozenset({"transition"}),
        "mode": "block_list",
    },
    "C_block_bearish_trend_with_trend_weakness": {
        "label": "C",
        "description": "block only bearish_trend_with_trend_weakness",
        "blocked_regimes": frozenset({"bearish_trend_with_trend_weakness"}),
        "mode": "block_list",
    },
    "D_block_transition_and_bullish_weakness": {
        "label": "D",
        "description": "block transition + bullish_trend_with_trend_weakness",
        "blocked_regimes": frozenset({"transition", "bullish_trend_with_trend_weakness"}),
        "mode": "block_list",
    },
    "E_block_transition_and_bearish_weakness": {
        "label": "E",
        "description": "block transition + bearish_trend_with_trend_weakness",
        "blocked_regimes": frozenset({"transition", "bearish_trend_with_trend_weakness"}),
        "mode": "block_list",
    },
    "F_strict_allow_bullish_only": {
        "label": "F",
        "description": "allow only bullish_trend or strong_bullish_trend",
        "allowed_regimes": STRICT_ALLOWED_REGIMES,
        "mode": "allow_list",
    },
}


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _bool(value: object) -> bool:
    return bool(value)


def load_blocker_csv(path: str | Path) -> pd.DataFrame:
    """Load the resolved blocker CSV and validate required columns."""
    frame = pd.read_csv(path)
    missing = [c for c in REQUIRED_INPUT_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"input CSV missing required columns: {missing}")
    return frame


def _alignment_flags(ema_order: object) -> dict[str, bool]:
    text = str(ema_order or "")
    return {
        "full_bullish_alignment": text == "EMA9 > EMA20 > EMA59 > EMA200",
        "full_bearish_alignment": text == "EMA9 < EMA20 < EMA59 < EMA200",
    }


def _signal_codes_from_tf(tf_payload: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    retest = tf_payload.get("retest_high_candidate") or tf_payload.get(
        "developing_structural_exhaustion"
    )
    if isinstance(retest, dict):
        for item in retest.get("signals") or []:
            code = str(item.get("code") or "")
            if code:
                codes.add(code)
    for bucket in (
        "equal_high_retest_exhaustion",
        "lower_high_momentum_weakness",
    ):
        for item in tf_payload.get(bucket) or []:
            for sig in item.get("signals") or []:
                code = str(sig.get("code") or "")
                if code:
                    codes.add(code)
    for item in (tf_payload.get("last_bar_rollover") or {}).get("signals") or []:
        metric = str(item.get("metric") or item.get("code") or "")
        if metric:
            codes.add(metric)
    for item in tf_payload.get("weakening_signals") or []:
        metric = str(item.get("metric") or "")
        if "LAST_BAR_ROLLOVER" in metric:
            codes.add(metric)
    return codes


def _has_developing_equal_high(tf_payload: dict[str, Any]) -> bool:
    item = tf_payload.get("developing_structural_exhaustion")
    if not isinstance(item, dict):
        return False
    status = str(item.get("confirmation_status") or "")
    return status == "developing_equal_high_exhaustion" or (
        (item.get("structure") or {}).get("structure_type") == "equal_high_exhaustion"
        and not item.get("is_confirmed_pivot")
    )


def _has_confirmed_equal_high(tf_payload: dict[str, Any]) -> bool:
    for item in tf_payload.get("equal_high_retest_exhaustion") or []:
        status = str(item.get("confirmation_status") or "")
        if "confirmed_equal_high" in status:
            return True
    return False


def _count_divergences(tf_payload: dict[str, Any], prefix: str) -> int:
    count = 0
    for item in tf_payload.get("confirmed_divergences") or []:
        if str(item.get("status") or "").startswith(prefix):
            count += 1
    return count


def _rollover_signal_list(tf_payload: dict[str, Any]) -> list[str]:
    rollover = tf_payload.get("last_bar_rollover") or {}
    out: list[str] = []
    mapping = (
        ("adx_last_bar_rollover", "ADX_LAST_BAR_ROLLOVER"),
        ("plus_di_last_bar_rollover", "PLUS_DI_LAST_BAR_ROLLOVER"),
        ("di_spread_last_bar_rollover", "DI_SPREAD_LAST_BAR_ROLLOVER"),
        ("atr_pct_last_bar_rollover", "ATR_PERCENT_LAST_BAR_ROLLOVER"),
        ("multi_metric_last_bar_rollover", "MULTI_METRIC_LAST_BAR_ROLLOVER"),
    )
    for key, code in mapping:
        if rollover.get(key):
            out.append(code)
    for item in rollover.get("signals") or []:
        metric = str(item.get("metric") or "")
        if metric and metric not in out:
            out.append(metric)
    return out


def extract_trade_row(
    *,
    input_row: dict[str, Any],
    audit: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Flatten one trade audit into a summary row."""
    trade_id = input_row.get("trade_id")
    base: dict[str, Any] = {
        "trade_id": trade_id,
        "start_index": input_row.get("start_index"),
        "start_candle_open_utc": input_row.get("start_candle_open_utc"),
        "decision_time_after_close_utc": input_row.get(DECISION_TIME_COLUMN),
        "category": input_row.get("category"),
        "pnl": _finite(input_row.get("pnl")),
        "status": "error" if error else "success",
        "error_message": error,
        "input_trade_status": input_row.get("status"),
    }

    timeframes = ("5m", "15m", "30m")
    for tf in timeframes:
        base[f"last_closed_candle_{tf}"] = None
        base[f"regime_{tf}"] = "unavailable"
        base[f"full_bullish_alignment_{tf}"] = False
        base[f"full_bearish_alignment_{tf}"] = False
        base[f"developing_equal_high_exhaustion_{tf}"] = False
        base[f"confirmed_equal_high_exhaustion_{tf}"] = False
        base[f"multi_metric_equal_high_exhaustion_{tf}"] = False
        base[f"last_bar_rollover_signals_{tf}"] = []
        base[f"confirmed_bearish_divergences_{tf}"] = 0
        base[f"confirmed_bullish_divergences_{tf}"] = 0
        base[f"adx_{tf}"] = None
        base[f"atr_pct_{tf}"] = None
        base[f"plus_di_{tf}"] = None
        base[f"minus_di_{tf}"] = None
        base[f"di_spread_{tf}"] = None
        base[f"close_vs_ema20_atr_{tf}"] = None
        base[f"close_vs_ema59_atr_{tf}"] = None
        base[f"close_vs_ema200_atr_{tf}"] = None

    base["combined_regime"] = "unavailable"
    base["combined_confidence"] = None
    base["reason_codes"] = []
    base["multi_timeframe_trend_weakness"] = False

    if error or audit is None:
        return base

    by_tf = audit.get("by_timeframe") or {}
    # Single-TF audits store flat fields without by_timeframe.
    if not by_tf and audit.get("timeframe"):
        by_tf = {str(audit.get("timeframe")): audit}
    elif not by_tf and "5m" in (audit.get("timeframes") or ["5m"]):
        # Flat 5m-only payload.
        by_tf = {"5m": audit}

    for tf, tf_payload in by_tf.items():
        align = _alignment_flags(tf_payload.get("ema_order"))
        codes = _signal_codes_from_tf(tf_payload)
        oe = (tf_payload.get("overextension") or {}).get("close_vs_ema_atr_units") or {}
        last = tf_payload.get("last_closed_candle") or {}
        regime = (tf_payload.get("regime_summary") or {}).get("regime") or "unavailable"
        base[f"last_closed_candle_{tf}"] = last.get("timestamp")
        base[f"regime_{tf}"] = regime
        base[f"full_bullish_alignment_{tf}"] = align["full_bullish_alignment"]
        base[f"full_bearish_alignment_{tf}"] = align["full_bearish_alignment"]
        base[f"developing_equal_high_exhaustion_{tf}"] = _has_developing_equal_high(tf_payload)
        base[f"confirmed_equal_high_exhaustion_{tf}"] = _has_confirmed_equal_high(tf_payload)
        base[f"multi_metric_equal_high_exhaustion_{tf}"] = (
            "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION" in codes
        )
        base[f"last_bar_rollover_signals_{tf}"] = _rollover_signal_list(tf_payload)
        base[f"confirmed_bearish_divergences_{tf}"] = _count_divergences(
            tf_payload, "confirmed_bearish"
        )
        base[f"confirmed_bullish_divergences_{tf}"] = _count_divergences(
            tf_payload, "confirmed_bullish"
        )
        base[f"adx_{tf}"] = _finite(tf_payload.get("adx"))
        base[f"atr_pct_{tf}"] = _finite(tf_payload.get("atr_pct"))
        base[f"plus_di_{tf}"] = _finite(tf_payload.get("plus_di"))
        base[f"minus_di_{tf}"] = _finite(tf_payload.get("minus_di"))
        base[f"di_spread_{tf}"] = _finite(tf_payload.get("di_spread"))
        base[f"close_vs_ema20_atr_{tf}"] = _finite(oe.get("ema_20"))
        base[f"close_vs_ema59_atr_{tf}"] = _finite(oe.get("ema_59"))
        base[f"close_vs_ema200_atr_{tf}"] = _finite(oe.get("ema_200"))

    combined = audit.get("combined_regime") or audit.get("regime_summary") or {}
    reason_codes = [str(r.get("code")) for r in (combined.get("reason_codes") or []) if r.get("code")]
    base["combined_regime"] = combined.get("regime") or "unavailable"
    base["combined_confidence"] = combined.get("confidence")
    base["reason_codes"] = reason_codes
    base["multi_timeframe_trend_weakness"] = "MULTI_TIMEFRAME_TREND_WEAKNESS" in reason_codes
    return base


def audit_one_trade(
    *,
    input_row: dict[str, Any],
    symbol: str,
    timeframes: str | tuple[str, ...],
    history_candles: int,
    candles: pd.DataFrame | None,
    config: RegimeScannerConfig | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run one trade audit; never raises — errors become row status=error."""
    decision_raw = input_row.get(DECISION_TIME_COLUMN)
    missing = (
        decision_raw is None
        or (isinstance(decision_raw, float) and math.isnan(decision_raw))
        or str(decision_raw).strip().lower() in {"", "nan", "none", "nat"}
    )
    try:
        if not missing and pd.isna(decision_raw):
            missing = True
    except (TypeError, ValueError):
        pass
    if missing:
        return extract_trade_row(
            input_row=input_row,
            audit=None,
            error=f"missing required column value: {DECISION_TIME_COLUMN}",
        )

    try:
        decision_ts = parse_decision_time(str(decision_raw))
    except Exception as exc:  # noqa: BLE001 - batch must continue
        return extract_trade_row(
            input_row=input_row,
            audit=None,
            error=f"invalid decision_time: {exc}",
        )

    try:
        audit = build_point_audit(
            symbol=symbol,
            decision_time=decision_ts,
            data_dir=data_dir,
            config=config,
            candles=candles,
            history_candles=history_candles,
            timeframes=timeframes,
        )
        return extract_trade_row(input_row=input_row, audit=audit, error=None)
    except Exception as exc:  # noqa: BLE001 - batch must continue
        return extract_trade_row(
            input_row=input_row,
            audit=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_batch_audit(
    *,
    input_csv: str | Path,
    symbol: str = "APTUSDT",
    timeframes: str = "5m,15m,30m",
    history_candles: int = 144,
    data_dir: str | Path | None = None,
    candles: pd.DataFrame | None = None,
    config: RegimeScannerConfig | None = None,
    data_source: str = "feather",
) -> dict[str, Any]:
    """Audit all CSV rows and return rows + summary payload."""
    cfg = config or default_regime_scanner_config()
    frame = load_blocker_csv(input_csv)
    requested = parse_timeframes(timeframes)

    # Load full symbol history once; each trade filters causally via decision_time.
    shared_candles = candles
    if shared_candles is None:
        shared_candles = load_symbol_candles(symbol, data_dir=data_dir, config=cfg, data_source=data_source)

    rows: list[dict[str, Any]] = []
    for _, series in frame.iterrows():
        input_row = series.to_dict()
        rows.append(
            audit_one_trade(
                input_row=input_row,
                symbol=symbol,
                timeframes=requested,
                history_candles=history_candles,
                candles=shared_candles,
                config=cfg,
                data_dir=data_dir,
            )
        )

    # Sort by PnL ascending (worst losses first); nulls last.
    def _pnl_key(row: dict[str, Any]) -> tuple[int, float]:
        pnl = row.get("pnl")
        if pnl is None:
            return (1, 0.0)
        return (0, float(pnl))

    rows_sorted = sorted(rows, key=_pnl_key)
    summary = build_batch_summary(rows_sorted)
    return {
        "symbol": str(symbol).upper(),
        "input_csv": str(input_csv),
        "timeframes": list(requested),
        "history_candles": int(history_candles),
        "rows": rows_sorted,
        "summary": summary,
    }


def _regime_stats(rows: list[dict[str, Any]], regime: str) -> dict[str, Any]:
    subset = [r for r in rows if r.get("combined_regime") == regime and r.get("status") == "success"]
    pnls = [float(r["pnl"]) for r in subset if r.get("pnl") is not None]
    total_pnl = float(sum(pnls)) if pnls else 0.0
    avg = float(total_pnl / len(pnls)) if pnls else None
    median = float(statistics.median(pnls)) if pnls else None
    return {
        "count": len(subset),
        "pnl_sum": total_pnl,
        "pnl_avg": avg,
        "pnl_median": median,
    }


def build_batch_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successes = [r for r in rows if r.get("status") == "success"]
    errors = [r for r in rows if r.get("status") == "error"]
    pnls_all = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
    negative = [r for r in rows if r.get("pnl") is not None and float(r["pnl"]) < 0]
    negative_pnl_sum = float(sum(float(r["pnl"]) for r in negative))

    categories = {}
    for r in rows:
        cat = str(r.get("category") or "unknown")
        categories[cat] = categories.get(cat, 0) + 1

    regimes = sorted({str(r.get("combined_regime") or "unavailable") for r in rows})
    by_regime: dict[str, Any] = {}
    for regime in regimes:
        stats = _regime_stats(rows, regime)
        neg_in_regime = [
            r
            for r in rows
            if r.get("combined_regime") == regime
            and r.get("pnl") is not None
            and float(r["pnl"]) < 0
        ]
        regime_neg_sum = float(sum(float(r["pnl"]) for r in neg_in_regime)) if neg_in_regime else 0.0
        stats["share_of_negative_trades"] = (
            float(len(neg_in_regime) / len(negative)) if negative else None
        )
        stats["share_of_total_loss"] = (
            float(regime_neg_sum / negative_pnl_sum) if negative_pnl_sum < 0 else None
        )
        by_regime[regime] = stats

    def _count_flag(key: str) -> int:
        return sum(1 for r in successes if r.get(key))

    top10 = [
        {
            "trade_id": r.get("trade_id"),
            "pnl": r.get("pnl"),
            "combined_regime": r.get("combined_regime"),
            "category": r.get("category"),
            "decision_time_after_close_utc": r.get("decision_time_after_close_utc"),
        }
        for r in rows[:10]
    ]

    named = (
        "bullish_trend_with_trend_weakness",
        "bearish_trend_with_trend_weakness",
        "strong_bullish_trend",
        "strong_bearish_trend",
        "neutral",
        "transition",
        "unavailable",
        "bullish_trend",
        "bearish_trend",
    )
    named_counts = {name: int(by_regime.get(name, {}).get("count") or 0) for name in named}

    return {
        "trade_count": len(rows),
        "successes": len(successes),
        "errors": len(errors),
        "pnl_sum": float(sum(pnls_all)) if pnls_all else 0.0,
        "category_counts": categories,
        "negative_closed_count": int(categories.get("negative_closed", 0)),
        "unfinished_or_stuck_count": int(
            categories.get("unfinished_or_stuck", 0)
            + categories.get("stuck", 0)
            + categories.get("unfinished", 0)
        ),
        "combined_regime_counts": {
            k: int(v.get("count") or 0) for k, v in by_regime.items()
        },
        "pnl_by_combined_regime": by_regime,
        "named_regime_counts": named_counts,
        "developing_equal_high_exhaustion_counts": {
            tf: _count_flag(f"developing_equal_high_exhaustion_{tf}")
            for tf in ("5m", "15m", "30m")
        },
        "confirmed_equal_high_exhaustion_counts": {
            tf: _count_flag(f"confirmed_equal_high_exhaustion_{tf}")
            for tf in ("5m", "15m", "30m")
        },
        "multi_metric_equal_high_exhaustion_counts": {
            tf: _count_flag(f"multi_metric_equal_high_exhaustion_{tf}")
            for tf in ("5m", "15m", "30m")
        },
        "last_bar_rollover_counts": {
            tf: sum(
                1
                for r in successes
                if r.get(f"last_bar_rollover_signals_{tf}")
            )
            for tf in ("5m", "15m", "30m")
        },
        "multi_timeframe_trend_weakness_count": _count_flag("multi_timeframe_trend_weakness"),
        "top_10_largest_losses": top10,
        "trades_sorted_by_pnl_asc": [
            {
                "trade_id": r.get("trade_id"),
                "pnl": r.get("pnl"),
                "combined_regime": r.get("combined_regime"),
                "status": r.get("status"),
            }
            for r in rows
        ],
        "error_rows": [
            {
                "trade_id": r.get("trade_id"),
                "error_message": r.get("error_message"),
                "decision_time_after_close_utc": r.get("decision_time_after_close_utc"),
            }
            for r in errors
        ],
    }


def format_summary_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    title = "Profitable Regime Batch Audit" if payload.get("result_file") else "Blocker Regime Batch Audit"
    lines = [
        f"# {title}",
        "",
        f"- Symbol: `{payload.get('symbol')}`",
        f"- Input: `{payload.get('input_csv') or payload.get('result_file')}`",
        f"- Timeframes: `{', '.join(payload.get('timeframes') or [])}`",
        f"- Trades: **{summary.get('trade_count')}**",
        f"- Successes: **{summary.get('successes')}**",
        f"- Errors: **{summary.get('errors')}**",
        f"- PnL sum: **{summary.get('pnl_sum')}**",
        f"- negative_closed: **{summary.get('negative_closed_count')}**",
        f"- unfinished_or_stuck: **{summary.get('unfinished_or_stuck_count')}**",
        "",
        "## Combined regime distribution",
        "",
    ]
    for regime, count in (summary.get("combined_regime_counts") or {}).items():
        stats = (summary.get("pnl_by_combined_regime") or {}).get(regime) or {}
        lines.append(
            f"- `{regime}`: count={count}, pnl_sum={stats.get('pnl_sum')}, "
            f"avg={stats.get('pnl_avg')}, median={stats.get('pnl_median')}, "
            f"share_neg_trades={stats.get('share_of_negative_trades')}, "
            f"share_total_loss={stats.get('share_of_total_loss')}"
        )
    lines.extend(["", "## Exhaustion / rollover counts", ""])
    for label in (
        "developing_equal_high_exhaustion_counts",
        "confirmed_equal_high_exhaustion_counts",
        "multi_metric_equal_high_exhaustion_counts",
        "last_bar_rollover_counts",
    ):
        lines.append(f"- {label}: `{summary.get(label)}`")
    lines.append(
        f"- multi_timeframe_trend_weakness_count: "
        f"**{summary.get('multi_timeframe_trend_weakness_count')}**"
    )
    if summary.get("strict_long_rule"):
        strict = summary["strict_long_rule"]
        lines.extend(
            [
                "",
                "## Strict long rule (allow only bullish_trend / strong_bullish_trend)",
                "",
                f"- Allowed winners: **{strict.get('allowed_count')}** "
                f"(pnl={strict.get('allowed_pnl_sum')})",
                f"- Blocked winners: **{strict.get('blocked_count')}** "
                f"(forgone pnl={strict.get('blocked_pnl_sum')})",
            ]
        )
    if summary.get("joint_strict_simulation"):
        joint = summary["joint_strict_simulation"]
        lines.extend(
            [
                "",
                "## Joint simulation with blocker trades",
                "",
                f"- Avoided loss: **{joint.get('avoided_negative_pnl')}**",
                f"- Forgone gain: **{joint.get('forgone_positive_pnl')}**",
                f"- Net effect: **{joint.get('net_effect')}**",
                f"- Original total PnL: **{joint.get('original_total_pnl')}**",
                f"- Hypothetical total PnL: **{joint.get('hypothetical_total_pnl')}**",
                f"- Remaining trades: **{joint.get('remaining_trades')}**",
                f"- New winrate: **{joint.get('new_winrate')}**",
            ]
        )
    lines.extend(["", "## Top 10 largest losses / smallest wins", ""])
    for item in summary.get("top_10_largest_losses") or []:
        lines.append(
            f"- `{item.get('trade_id')}` pnl={item.get('pnl')} "
            f"regime=`{item.get('combined_regime')}` "
            f"decision=`{item.get('decision_time_after_close_utc')}`"
        )
    if summary.get("error_rows"):
        lines.extend(["", "## Errors", ""])
        for item in summary["error_rows"]:
            lines.append(
                f"- `{item.get('trade_id')}`: {item.get('error_message')}"
            )
    lines.append("")
    return "\n".join(lines)


def _serialize_row_for_csv(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (list, dict)):
            out[key] = json.dumps(json_safe(value), allow_nan=False)
        else:
            safe = json_safe(value)
            out[key] = safe
    return out


def write_batch_outputs(
    payload: dict[str, Any],
    output_dir: str | Path,
    *,
    prefix: str = "blocker",
) -> dict[str, Path]:
    """Write CSV/JSON/MD artifacts into ``output_dir`` with a filename prefix."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = payload.get("rows") or []
    safe_payload = json_safe(payload)

    csv_path = out_dir / f"{prefix}_regime_audit_rows.csv"
    json_rows_path = out_dir / f"{prefix}_regime_audit_rows.json"
    summary_json_path = out_dir / f"{prefix}_regime_summary.json"
    summary_md_path = out_dir / f"{prefix}_regime_summary.md"

    csv_rows = [_serialize_row_for_csv(r) for r in rows]
    pd.DataFrame(csv_rows).to_csv(csv_path, index=False)

    json_rows_path.write_text(
        json.dumps(json_safe(rows), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    summary_json_path.write_text(
        json.dumps(safe_payload.get("summary"), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    summary_md_path.write_text(format_summary_markdown(safe_payload), encoding="utf-8")

    paths = {
        "csv": csv_path,
        "rows_json": json_rows_path,
        "summary_json": summary_json_path,
        "summary_md": summary_md_path,
    }

    if payload.get("filter_comparison"):
        cmp_csv = out_dir / "regime_filter_rule_comparison.csv"
        cmp_md = out_dir / "regime_filter_rule_comparison.md"
        cmp_rows = payload["filter_comparison"].get("rules") or []
        pd.DataFrame(cmp_rows).to_csv(cmp_csv, index=False)
        cmp_md.write_text(
            format_filter_comparison_markdown(payload["filter_comparison"]),
            encoding="utf-8",
        )
        paths["comparison_csv"] = cmp_csv
        paths["comparison_md"] = cmp_md

    if payload.get("strict_long_rule"):
        strict_path = out_dir / f"{prefix}_strict_long_rule.json"
        strict_path.write_text(
            json.dumps(json_safe(payload["strict_long_rule"]), indent=2, allow_nan=False),
            encoding="utf-8",
        )
        paths["strict_long_rule"] = strict_path

    return paths


def regime_is_blocked(regime: object, rule: dict[str, Any]) -> bool:
    name = str(regime or "unavailable")
    mode = rule.get("mode")
    if mode == "allow_list":
        allowed = rule.get("allowed_regimes") or frozenset()
        return name not in allowed
    blocked = rule.get("blocked_regimes") or frozenset()
    return name in blocked


def evaluate_strict_long_rule(winner_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Simulate allow-only bullish_trend / strong_bullish_trend on winners."""
    allowed = [
        r
        for r in winner_rows
        if r.get("status") == "success"
        and str(r.get("combined_regime")) in STRICT_ALLOWED_REGIMES
    ]
    blocked = [
        r
        for r in winner_rows
        if r.get("status") == "success"
        and str(r.get("combined_regime")) not in STRICT_ALLOWED_REGIMES
    ]
    allowed_pnls = [float(r["pnl"]) for r in allowed if r.get("pnl") is not None]
    blocked_pnls = [float(r["pnl"]) for r in blocked if r.get("pnl") is not None]
    by_regime: dict[str, Any] = {}
    for r in winner_rows:
        if r.get("status") != "success":
            continue
        regime = str(r.get("combined_regime") or "unavailable")
        bucket = by_regime.setdefault(regime, {"count": 0, "pnl_sum": 0.0})
        bucket["count"] += 1
        if r.get("pnl") is not None:
            bucket["pnl_sum"] += float(r["pnl"])
    return {
        "allowed_count": len(allowed),
        "blocked_count": len(blocked),
        "allowed_pnl_sum": float(sum(allowed_pnls)) if allowed_pnls else 0.0,
        "blocked_pnl_sum": float(sum(blocked_pnls)) if blocked_pnls else 0.0,
        "allowed_pnl_avg": float(sum(allowed_pnls) / len(allowed_pnls)) if allowed_pnls else None,
        "blocked_pnl_avg": float(sum(blocked_pnls) / len(blocked_pnls)) if blocked_pnls else None,
        "allowed_pnl_median": float(statistics.median(allowed_pnls)) if allowed_pnls else None,
        "blocked_pnl_median": float(statistics.median(blocked_pnls)) if blocked_pnls else None,
        "pnl_by_combined_regime": by_regime,
        "allowed_regimes": sorted(STRICT_ALLOWED_REGIMES),
        "blocked_regimes": sorted(STRICT_BLOCKED_REGIMES),
    }


def evaluate_filter_rule(
    *,
    rule_key: str,
    rule: dict[str, Any],
    winner_rows: list[dict[str, Any]],
    loser_rows: list[dict[str, Any]],
    original_total_pnl: float,
) -> dict[str, Any]:
    """Evaluate one blocking rule across winners + problem trades."""
    winners = [r for r in winner_rows if r.get("status") == "success"]
    losers = [r for r in loser_rows if r.get("status") == "success"]

    blocked_winners = [r for r in winners if regime_is_blocked(r.get("combined_regime"), rule)]
    blocked_losers = [r for r in losers if regime_is_blocked(r.get("combined_regime"), rule)]
    kept_winners = [r for r in winners if not regime_is_blocked(r.get("combined_regime"), rule)]
    kept_losers = [r for r in losers if not regime_is_blocked(r.get("combined_regime"), rule)]

    blocked_winner_pnl = float(
        sum(float(r["pnl"]) for r in blocked_winners if r.get("pnl") is not None)
    )
    blocked_loser_pnl = float(
        sum(float(r["pnl"]) for r in blocked_losers if r.get("pnl") is not None)
    )
    # Avoided loss is the positive magnitude of blocked negative PnL.
    avoided_loss = float(-blocked_loser_pnl) if blocked_loser_pnl < 0 else 0.0
    forgone_gain = float(blocked_winner_pnl) if blocked_winner_pnl > 0 else 0.0
    net_effect = avoided_loss - forgone_gain
    hypothetical = float(original_total_pnl) + net_effect

    remaining = kept_winners + kept_losers
    remaining_pnls = [float(r["pnl"]) for r in remaining if r.get("pnl") is not None]
    remaining_wins = sum(1 for p in remaining_pnls if p > 0)
    winrate = float(remaining_wins / len(remaining_pnls)) if remaining_pnls else None

    all_blocked = blocked_winners + blocked_losers
    precision = (
        float(len(blocked_losers) / len(all_blocked)) if all_blocked else None
    )
    recall = float(len(blocked_losers) / len(losers)) if losers else None

    return {
        "rule_key": rule_key,
        "label": rule.get("label"),
        "description": rule.get("description"),
        "blocked_winners": len(blocked_winners),
        "blocked_losers": len(blocked_losers),
        "kept_winners": len(kept_winners),
        "kept_losers": len(kept_losers),
        "forgone_gain": forgone_gain,
        "avoided_loss": avoided_loss,
        "net_effect": net_effect,
        "remaining_trades": len(remaining),
        "remaining_winrate": winrate,
        "hypothetical_total_pnl": hypothetical,
        "original_total_pnl": float(original_total_pnl),
        "precision_blocked_are_losers": precision,
        "recall_losers_blocked": recall,
        "blocked_winner_pnl_sum": blocked_winner_pnl,
        "blocked_loser_pnl_sum": blocked_loser_pnl,
    }


def compare_filter_rules(
    *,
    winner_rows: list[dict[str, Any]],
    loser_rows: list[dict[str, Any]],
    original_total_pnl: float,
) -> dict[str, Any]:
    rules_out = [
        evaluate_filter_rule(
            rule_key=key,
            rule=rule,
            winner_rows=winner_rows,
            loser_rows=loser_rows,
            original_total_pnl=original_total_pnl,
        )
        for key, rule in FILTER_RULES.items()
    ]
    best = max(rules_out, key=lambda r: float(r.get("net_effect") or -1e18))
    return {
        "original_total_pnl": float(original_total_pnl),
        "winner_count": sum(1 for r in winner_rows if r.get("status") == "success"),
        "loser_count": sum(1 for r in loser_rows if r.get("status") == "success"),
        "rules": rules_out,
        "best_net_effect_rule": best.get("rule_key"),
        "best_net_effect": best.get("net_effect"),
    }


def format_filter_comparison_markdown(comparison: dict[str, Any]) -> str:
    lines = [
        "# Regime Filter Rule Comparison",
        "",
        f"- Original total PnL: **{comparison.get('original_total_pnl')}**",
        f"- Winners analyzed: **{comparison.get('winner_count')}**",
        f"- Problem/loser trades: **{comparison.get('loser_count')}**",
        f"- Best net-effect rule: **{comparison.get('best_net_effect_rule')}** "
        f"({comparison.get('best_net_effect')})",
        "",
        "| Rule | Blocked winners | Blocked losers | Forgone gain | Avoided loss | Net effect | Remaining | Hyp. PnL | Precision | Recall |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in comparison.get("rules") or []:
        lines.append(
            f"| {row.get('label')}: {row.get('description')} | "
            f"{row.get('blocked_winners')} | {row.get('blocked_losers')} | "
            f"{row.get('forgone_gain')} | {row.get('avoided_loss')} | "
            f"{row.get('net_effect')} | {row.get('remaining_trades')} | "
            f"{row.get('hypothetical_total_pnl')} | "
            f"{row.get('precision_blocked_are_losers')} | "
            f"{row.get('recall_losers_blocked')} |"
        )
    lines.append("")
    return "\n".join(lines)


def load_audit_rows_csv(path: str | Path) -> list[dict[str, Any]]:
    frame = pd.read_csv(path)
    rows: list[dict[str, Any]] = []
    for _, series in frame.iterrows():
        row = series.to_dict()
        # Parse JSON list fields if present.
        for key, value in list(row.items()):
            if isinstance(value, str) and value.startswith("["):
                try:
                    row[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
            elif isinstance(value, float) and math.isnan(value):
                row[key] = None
        rows.append(row)
    return rows


def run_batch_audit_from_trades(
    trades: list[dict[str, Any]],
    *,
    symbol: str,
    timeframes: str | tuple[str, ...] = "5m,15m,30m",
    history_candles: int = 144,
    candles: pd.DataFrame | None = None,
    data_dir: str | Path | None = None,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Audit an already-built trade list."""
    cfg = config or default_regime_scanner_config()
    requested = parse_timeframes(timeframes)
    shared = candles
    if shared is None:
        shared = load_symbol_candles(symbol, data_dir=data_dir, config=cfg)

    rows: list[dict[str, Any]] = []
    for trade in trades:
        rows.append(
            audit_one_trade(
                input_row=trade,
                symbol=symbol,
                timeframes=requested,
                history_candles=history_candles,
                candles=shared,
                config=cfg,
                data_dir=data_dir,
            )
        )

    def _pnl_key(row: dict[str, Any]) -> tuple[int, float]:
        pnl = row.get("pnl")
        if pnl is None:
            return (1, 0.0)
        return (0, float(pnl))

    rows_sorted = sorted(rows, key=_pnl_key)
    summary = build_batch_summary(rows_sorted)
    return {
        "symbol": str(symbol).upper(),
        "timeframes": list(requested),
        "history_candles": int(history_candles),
        "rows": rows_sorted,
        "summary": summary,
    }


def run_profitable_batch_with_comparison(
    *,
    result_file: str | Path,
    blocker_rows_csv: str | Path,
    symbol: str = "APTUSDT",
    timeframes: str = "5m,15m,30m",
    history_candles: int = 144,
    data_dir: str | Path | None = None,
    candles: pd.DataFrame | None = None,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Extract positive trades, audit regimes, and compare filter rules vs blockers."""
    cfg = config or default_regime_scanner_config()
    shared = candles
    if shared is None:
        shared = load_symbol_candles(symbol, data_dir=data_dir, config=cfg)

    extracted = extract_trades_from_result_file(
        result_file,
        candles=shared,
        trade_filter="positive_closed",
    )
    winner_payload = run_batch_audit_from_trades(
        extracted["trades"],
        symbol=symbol,
        timeframes=timeframes,
        history_candles=history_candles,
        candles=shared,
        data_dir=data_dir,
        config=cfg,
    )
    loser_rows = load_audit_rows_csv(blocker_rows_csv)
    original_total = _finite((extracted.get("aggregate") or {}).get("total_pnl"))
    if original_total is None:
        # Fallback: winners + losers from audited rows.
        original_total = float(
            sum(float(r["pnl"]) for r in winner_payload["rows"] if r.get("pnl") is not None)
            + sum(float(r["pnl"]) for r in loser_rows if r.get("pnl") is not None)
        )

    strict = evaluate_strict_long_rule(winner_payload["rows"])
    comparison = compare_filter_rules(
        winner_rows=winner_payload["rows"],
        loser_rows=loser_rows,
        original_total_pnl=float(original_total),
    )
    # Attach joint net simulation for the strict rule (F).
    strict_rule_row = next(
        r for r in comparison["rules"] if r.get("label") == "F"
    )
    joint = {
        "avoided_negative_pnl": strict_rule_row["avoided_loss"],
        "forgone_positive_pnl": strict_rule_row["forgone_gain"],
        "net_effect": strict_rule_row["net_effect"],
        "original_total_pnl": strict_rule_row["original_total_pnl"],
        "hypothetical_total_pnl": strict_rule_row["hypothetical_total_pnl"],
        "remaining_trades": strict_rule_row["remaining_trades"],
        "new_winrate": strict_rule_row["remaining_winrate"],
        "blocked_winners": strict_rule_row["blocked_winners"],
        "blocked_losers": strict_rule_row["blocked_losers"],
    }

    summary = dict(winner_payload["summary"])
    summary["extracted_positive_trade_count"] = extracted["trade_count"]
    summary["source_aggregate"] = extracted.get("aggregate")
    summary["strict_long_rule"] = strict
    summary["joint_strict_simulation"] = joint

    return {
        **winner_payload,
        "input_csv": None,
        "result_file": str(result_file),
        "blocker_rows_csv": str(blocker_rows_csv),
        "extraction": {
            "trade_count": extracted["trade_count"],
            "aggregate": extracted.get("aggregate"),
        },
        "summary": summary,
        "strict_long_rule": strict,
        "joint_strict_simulation": joint,
        "filter_comparison": comparison,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch multi-timeframe regime audit for blocker or profitable trades.",
    )
    parser.add_argument(
        "--input-csv",
        default=None,
        help="Resolved blocker CSV with decision_time_after_close_utc",
    )
    parser.add_argument(
        "--result-file",
        default=None,
        help="Continuous results JSON used to extract trades by filter",
    )
    parser.add_argument(
        "--trade-filter",
        default="positive_closed",
        choices=["positive_closed", "negative_closed", "all_closed"],
        help="Trade selection when --result-file is used",
    )
    parser.add_argument(
        "--blocker-rows-csv",
        default="research/backtests/results/regime_scanner_blocker_batch/blocker_regime_audit_rows.csv",
        help="Existing blocker audit rows for joint filter comparison",
    )
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--timeframes", default="5m,15m,30m")
    parser.add_argument("--history-candles", type=int, default=144)
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_blocker_batch",
    )
    parser.add_argument("--data-dir", default=None)
    parser.add_argument(
        "--data-source",
        choices=("feather", "mysql"),
        default="feather",
        help="Candle source for 5m input (default: feather)",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Output filename prefix (default: blocker or profitable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.result_file:
        if args.trade_filter == "positive_closed":
            payload = run_profitable_batch_with_comparison(
                result_file=args.result_file,
                blocker_rows_csv=args.blocker_rows_csv,
                symbol=args.symbol,
                timeframes=args.timeframes,
                history_candles=args.history_candles,
                data_dir=args.data_dir,
            )
            prefix = args.prefix or "profitable"
        else:
            cfg = default_regime_scanner_config()
            candles = load_symbol_candles(args.symbol, data_dir=args.data_dir, config=cfg)
            extracted = extract_trades_from_result_file(
                args.result_file,
                candles=candles,
                trade_filter=args.trade_filter,
            )
            payload = run_batch_audit_from_trades(
                extracted["trades"],
                symbol=args.symbol,
                timeframes=args.timeframes,
                history_candles=args.history_candles,
                candles=candles,
                data_dir=args.data_dir,
            )
            payload["result_file"] = str(args.result_file)
            prefix = args.prefix or args.trade_filter
    elif args.input_csv:
        payload = run_batch_audit(
            input_csv=args.input_csv,
            symbol=args.symbol,
            timeframes=args.timeframes,
            history_candles=args.history_candles,
            data_dir=args.data_dir,
        )
        prefix = args.prefix or "blocker"
    else:
        parser.error("provide --input-csv or --result-file")

    paths = write_batch_outputs(payload, args.output_dir, prefix=prefix)
    summary = payload["summary"]
    print(
        f"Batch audit complete: trades={summary['trade_count']} "
        f"successes={summary['successes']} errors={summary['errors']}"
    )
    if payload.get("extraction"):
        print(f"Extracted positive trades: {payload['extraction']['trade_count']}")
    if payload.get("strict_long_rule"):
        strict = payload["strict_long_rule"]
        print(
            f"Strict long rule: allowed={strict['allowed_count']} "
            f"blocked={strict['blocked_count']} "
            f"forgone_pnl={strict['blocked_pnl_sum']}"
        )
    if payload.get("joint_strict_simulation"):
        joint = payload["joint_strict_simulation"]
        print(
            f"Joint net effect: avoided={joint['avoided_negative_pnl']} "
            f"forgone={joint['forgone_positive_pnl']} "
            f"net={joint['net_effect']} "
            f"hyp_pnl={joint['hypothetical_total_pnl']}"
        )
    if payload.get("filter_comparison"):
        print(
            f"Best rule: {payload['filter_comparison']['best_net_effect_rule']} "
            f"net={payload['filter_comparison']['best_net_effect']}"
        )
    for key in ("csv", "rows_json", "summary_json", "summary_md", "comparison_csv", "comparison_md"):
        if key in paths:
            print(f"Wrote: {paths[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
