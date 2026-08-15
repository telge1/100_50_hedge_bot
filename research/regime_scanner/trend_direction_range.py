"""Historical range trend-direction runner (reuse trend_direction_at logic).

Decision window: start <= decision_time <= end (inclusive).
Each decision uses only candles with close_time <= decision_time.
Scanner runs once on the chronological series up to end; per-T results are
prefix slices of that causal structure series.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.timeframes import ensure_utc_timestamp
from research.regime_scanner.trend_direction_at import (
    DEFAULT_WARMUP_BARS,
    SOURCE_TIMEFRAME,
    TrendDirectionAtError,
    TrendDirectionResult,
    _iso_z,
    decide_from_structure,
    normalize_symbol,
    parse_decision_timestamp,
    run_c34b_on_ohlcv,
)

STEP_MINUTES = {"5m": 5}
TIMELINE_COLUMNS = [
    "symbol",
    "decision_time_utc",
    "last_5m_open_utc",
    "last_5m_close_utc",
    "close_price",
    "direction",
    "direction_since_utc",
    "structure_event",
    "reason",
    "major_direction",
    "protected_structure_state",
    "causality_pass",
]
TRANSITION_KEYS = ("direction", "structure_event", "reason", "protected_structure_state")


@dataclass
class RangeRunResult:
    symbol: str
    exchange: str
    start_utc: str
    end_utc: str
    step: str
    timeframe: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    runtime_seconds: float = 0.0
    decision_inclusive: str = "start <= decision_time <= end"
    display_rows: list[dict[str, Any]] | None = None

    def output_rows(self) -> list[dict[str, Any]]:
        if self.display_rows is not None:
            return self.display_rows
        return self.rows

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["output_rows"] = self.output_rows()
        return d


def parse_step(step: str) -> int:
    key = str(step).strip().lower()
    if key not in STEP_MINUTES:
        raise TrendDirectionAtError(
            "UNSUPPORTED_STEP",
            f"step must be one of {sorted(STEP_MINUTES)}, got {step!r}",
        )
    return STEP_MINUTES[key]


def build_decision_times(
    start: pd.Timestamp, end: pd.Timestamp, *, step_minutes: int
) -> pd.DatetimeIndex:
    """Inclusive decision grid on step boundaries (candle-close aligned)."""
    start = ensure_utc_timestamp(start)
    end = ensure_utc_timestamp(end)
    if start >= end:
        raise TrendDirectionAtError(
            "INVALID_RANGE",
            f"start must be < end ({_iso_z(start)} >= {_iso_z(end)})",
        )
    freq = f"{int(step_minutes)}min"
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    start_offset = int((start - epoch).total_seconds())
    step_sec = step_minutes * 60
    aligned_start = start
    if start_offset % step_sec != 0:
        aligned_start = start + pd.Timedelta(seconds=(step_sec - (start_offset % step_sec)))
    if aligned_start > end:
        raise TrendDirectionAtError(
            "INVALID_RANGE",
            f"no decision times in [{_iso_z(start)}, {_iso_z(end)}] after step align",
        )
    return pd.date_range(start=aligned_start, end=end, freq=freq, inclusive="both")


def load_mysql_5m_through(
    *,
    symbol: str,
    end_time: pd.Timestamp,
    exchange: str = "bybit",
    env_file: str | None = None,
) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    """Load closed 5m candles with close_time <= end_time (one MySQL full-series read)."""
    from research.regime_scanner.candle_sources import (
        MySQLCandleSource,
        load_regime_db_env_file,
    )

    if env_file:
        load_regime_db_env_file(Path(env_file))
    else:
        load_regime_db_env_file()

    src = MySQLCandleSource(exchange_default=exchange)
    try:
        full = src.load_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe="5m",
            closed_only=True,
        )
        if full.empty:
            raise TrendDirectionAtError(
                "SYMBOL_NOT_FOUND",
                f"no 5m candles in MySQL for {exchange} {symbol}",
            )
        full = full.copy()
        full["timestamp"] = pd.to_datetime(full["timestamp"], utc=True)
        if "close_time" not in full.columns:
            full["close_time"] = full["timestamp"] + pd.Timedelta(minutes=5)
        else:
            full["close_time"] = pd.to_datetime(full["close_time"], utc=True)

        first_open = ensure_utc_timestamp(full["timestamp"].iloc[0])
        data_last_close = ensure_utc_timestamp(full["close_time"].iloc[-1])
        end = ensure_utc_timestamp(end_time)
        asof = full.loc[full["close_time"] <= end].copy()
        if asof.empty:
            raise TrendDirectionAtError(
                "NO_CLOSED_CANDLE",
                f"no closed 5m candle with close_time <= {_iso_z(end)}",
            )
        return asof, first_open, data_last_close
    finally:
        src.close()


def _candles_to_ohlcv(candles: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(candles["timestamp"], utc=True),
            "open": pd.to_numeric(candles["open"], errors="coerce"),
            "high": pd.to_numeric(candles["high"], errors="coerce"),
            "low": pd.to_numeric(candles["low"], errors="coerce"),
            "close": pd.to_numeric(candles["close"], errors="coerce"),
            "volume": pd.to_numeric(candles["volume"], errors="coerce"),
        }
    )


def result_to_timeline_row(
    result: TrendDirectionResult, *, close_price: float | None
) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "decision_time_utc": result.requested_at_utc,
        "last_5m_open_utc": result.last_available_5m_open_utc,
        "last_5m_close_utc": result.last_available_5m_close_utc,
        "close_price": close_price,
        "direction": result.direction,
        "direction_since_utc": result.direction_since_utc,
        "structure_event": result.structure_event,
        "reason": result.reason,
        "major_direction": result.major_direction,
        "protected_structure_state": result.protected_structure_state,
        "causality_pass": bool(result.causality_pass),
    }


def is_transition(prev: dict[str, Any] | None, cur: dict[str, Any]) -> bool:
    if prev is None:
        return True
    return any(prev.get(k) != cur.get(k) for k in TRANSITION_KEYS)


def filter_transitions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for row in rows:
        if is_transition(prev, row):
            out.append(row)
            prev = row
    return out


def build_summary(rows: list[dict[str, Any]], *, runtime_seconds: float) -> dict[str, Any]:
    n = len(rows)
    bull = sum(1 for r in rows if r.get("direction") == "BULLISH")
    bear = sum(1 for r in rows if r.get("direction") == "BEARISH")
    unc = sum(1 for r in rows if r.get("direction") == "UNCLEAR")
    causality_failures = sum(1 for r in rows if not r.get("causality_pass", True))

    transitions = filter_transitions(rows)
    dir_changes: list[dict[str, Any]] = []
    prev = None
    for row in rows:
        if prev is None or row.get("direction") != prev.get("direction"):
            dir_changes.append(row)
            prev = row

    durations_min: list[float] = []
    for i in range(len(dir_changes) - 1):
        a = pd.Timestamp(dir_changes[i]["decision_time_utc"])
        b = pd.Timestamp(dir_changes[i + 1]["decision_time_utc"])
        durations_min.append((b - a).total_seconds() / 60.0)

    matrix = {
        "BULLISH->UNCLEAR": 0,
        "UNCLEAR->BEARISH": 0,
        "BEARISH->UNCLEAR": 0,
        "UNCLEAR->BULLISH": 0,
        "BULLISH->BEARISH": 0,
        "BEARISH->BULLISH": 0,
    }
    direct_flips = 0
    whipsaw_le_30 = 0
    for i in range(1, len(dir_changes)):
        a = dir_changes[i - 1]["direction"]
        b = dir_changes[i]["direction"]
        key = f"{a}->{b}"
        if key in matrix:
            matrix[key] += 1
        if key in {"BULLISH->BEARISH", "BEARISH->BULLISH"}:
            direct_flips += 1
        lag = (
            pd.Timestamp(dir_changes[i]["decision_time_utc"])
            - pd.Timestamp(dir_changes[i - 1]["decision_time_utc"])
        ).total_seconds() / 60.0
        if lag <= 30:
            whipsaw_le_30 += 1

    short_le_15 = sum(1 for d in durations_min if d <= 15)

    def _pct(x: int) -> float:
        return (100.0 * x / n) if n else 0.0

    return {
        "total_rows": n,
        "bullish_rows": bull,
        "bearish_rows": bear,
        "unclear_rows": unc,
        "bullish_pct": round(_pct(bull), 4),
        "bearish_pct": round(_pct(bear), 4),
        "unclear_pct": round(_pct(unc), 4),
        "direction_transitions": max(0, len(dir_changes) - 1),
        "state_field_transitions": max(0, len(transitions) - 1) if transitions else 0,
        "average_state_duration_minutes": (
            round(float(pd.Series(durations_min).mean()), 4) if durations_min else None
        ),
        "median_state_duration_minutes": (
            round(float(pd.Series(durations_min).median()), 4) if durations_min else None
        ),
        "short_states_le_15m": short_le_15,
        "whipsaw_transitions_le_30m": whipsaw_le_30,
        "direct_bull_bear_flips": direct_flips,
        "direct_flips_note": (
            "BULLISH<->BEARISH without UNCLEAR is unexpected under current mapping"
        ),
        "transition_matrix": matrix,
        "causality_failures": causality_failures,
        "runtime_seconds": round(float(runtime_seconds), 4),
        "decision_inclusive": "start <= decision_time <= end",
        "forward_returns": "not_implemented_v1",
    }


def query_trend_direction_range(
    *,
    symbol: str,
    start: object,
    end: object,
    step: str = "5m",
    exchange: str = "bybit",
    timeframe: str = "5m",
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    env_file: str | None = None,
    candles: pd.DataFrame | None = None,
    transitions_only: bool = False,
) -> RangeRunResult:
    """Efficient range query: one load, one C3.4B pass, prefix decisions."""
    t0 = time.perf_counter()
    sym = normalize_symbol(symbol)
    tf = str(timeframe).strip().lower()
    if tf != "5m":
        raise TrendDirectionAtError(
            "UNSUPPORTED_TIMEFRAME",
            f"primary direction timeframe must be 5m, got {timeframe!r}",
        )
    step_min = parse_step(step)
    start_ts, _ = parse_decision_timestamp(start)
    end_ts, _ = parse_decision_timestamp(end)
    decisions = build_decision_times(start_ts, end_ts, step_minutes=step_min)

    if candles is None:
        candles, first_open, data_last_close = load_mysql_5m_through(
            symbol=sym,
            end_time=end_ts,
            exchange=exchange,
            env_file=env_file,
        )
        if start_ts < first_open:
            raise TrendDirectionAtError(
                "TIMESTAMP_BEFORE_DATA",
                f"start {_iso_z(start_ts)} is before first candle open {_iso_z(first_open)}",
            )
        if end_ts > data_last_close:
            raise TrendDirectionAtError(
                "TIMESTAMP_AFTER_DATA",
                f"end {_iso_z(end_ts)} is after last closed candle {_iso_z(data_last_close)}",
            )
    else:
        frame = candles.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        if "close_time" not in frame.columns:
            frame["close_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
        else:
            frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
        frame = frame.loc[frame["close_time"] <= end_ts].copy()
        if frame.empty:
            raise TrendDirectionAtError(
                "NO_CLOSED_CANDLE",
                f"no closed 5m candle with close_time <= {_iso_z(end_ts)}",
            )
        candles = frame

    ohlcv = _candles_to_ohlcv(candles)
    structure = run_c34b_on_ohlcv(ohlcv)
    closes = pd.to_datetime(structure["candle_close_ts"], utc=True)
    close_ns = closes.astype("int64").to_numpy()

    rows: list[dict[str, Any]] = []
    for decision in decisions:
        decision = ensure_utc_timestamp(decision)
        key_ns = int(decision.value)
        idx = int(close_ns.searchsorted(key_ns, side="right") - 1)
        if idx < 0:
            raise TrendDirectionAtError(
                "NO_CLOSED_CANDLE",
                f"no closed 5m candle with close_time <= {_iso_z(decision)}",
            )
        prefix = structure.iloc[: idx + 1]
        last_close = ensure_utc_timestamp(prefix.iloc[-1]["candle_close_ts"])
        if last_close > decision:
            raise TrendDirectionAtError(
                "LOOKAHEAD_VIOLATION",
                f"last candle close {last_close} > decision {decision}",
            )
        result = decide_from_structure(
            prefix,
            decision_time=decision,
            symbol=sym,
            exchange=exchange,
            timestamp_assumed_utc=False,
            warmup_bars=warmup_bars,
            include_htf=False,
        )
        close_price = (
            float(prefix.iloc[-1]["close"]) if pd.notna(prefix.iloc[-1]["close"]) else None
        )
        rows.append(result_to_timeline_row(result, close_price=close_price))

    transitions = filter_transitions(rows)
    runtime = time.perf_counter() - t0
    summary = build_summary(rows, runtime_seconds=runtime)
    summary["transitions_only"] = bool(transitions_only)
    summary["output_rows"] = len(transitions if transitions_only else rows)

    return RangeRunResult(
        symbol=sym,
        exchange=exchange,
        start_utc=_iso_z(start_ts) or "",
        end_utc=_iso_z(end_ts) or "",
        step=f"{step_min}m",
        timeframe=SOURCE_TIMEFRAME,
        rows=rows,
        transitions=transitions,
        summary=summary,
        runtime_seconds=runtime,
        display_rows=transitions if transitions_only else None,
    )


def default_run_dir(base: Path | None = None) -> Path:
    root = base or Path("results/trend_direction_range")
    root.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = root / f"run_{stamp}"
    if path.exists():
        i = 1
        while (root / f"run_{stamp}_{i}").exists():
            i += 1
        path = root / f"run_{stamp}_{i}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_range_artifacts(result: RangeRunResult, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    timeline_path = out_dir / "direction_timeline.csv"
    transitions_path = out_dir / "direction_transitions.csv"
    summary_path = out_dir / "summary.json"
    report_path = out_dir / "REPORT.md"

    pd.DataFrame(result.rows, columns=TIMELINE_COLUMNS).to_csv(timeline_path, index=False)
    pd.DataFrame(result.transitions, columns=TIMELINE_COLUMNS).to_csv(
        transitions_path, index=False
    )
    summary_path.write_text(json.dumps(result.summary, indent=2, default=str) + "\n")

    s = result.summary
    matrix = s.get("transition_matrix") or {}
    report = "\n".join(
        [
            "# Trend Direction Range Run",
            "",
            "## Primärentscheidung",
            "",
            "**TREND_DIRECTION_RANGE_RUNNER_READY**",
            "",
            f"- symbol: `{result.symbol}`",
            f"- range: `{result.start_utc}` → `{result.end_utc}` (inclusive decision times)",
            f"- step: `{result.step}`",
            f"- rows (output): {s.get('output_rows')}",
            f"- direction_transitions: {s.get('direction_transitions')}",
            f"- BULLISH/UNCLEAR/BEARISH: {s.get('bullish_rows')}/{s.get('unclear_rows')}/{s.get('bearish_rows')}",
            f"- causality_failures: {s.get('causality_failures')}",
            f"- runtime_seconds: {s.get('runtime_seconds')}",
            f"- direct BULL↔BEAR flips: {s.get('direct_bull_bear_flips')}",
            "",
            "## Transition matrix",
            "",
            *[f"- {k}: {v}" for k, v in matrix.items()],
            "",
            "## Notes",
            "",
            "- Decision window is inclusive: `start <= decision_time <= end`.",
            "- Single C3.4B pass over candles through `end`; each T uses prefix `close_time <= T`.",
            "- Forward returns not implemented in v1 (EX_POST_EVALUATION deferred).",
            "- No MySQL writes; no HTF in default path.",
            "",
        ]
    )
    report_path.write_text(report)
    return {
        "timeline_csv": str(timeline_path),
        "transitions_csv": str(transitions_path),
        "summary_json": str(summary_path),
        "report_md": str(report_path),
    }


def format_range_text(result: RangeRunResult, *, paths: dict[str, str] | None = None) -> str:
    s = result.summary
    lines = [
        f"symbol: {result.symbol}",
        f"range: {result.start_utc} -> {result.end_utc}",
        f"step: {result.step}",
        f"rows: {s.get('total_rows', len(result.rows))}",
        f"transitions: {s.get('direction_transitions')}",
        f"BULLISH: {s.get('bullish_rows')}",
        f"UNCLEAR: {s.get('unclear_rows')}",
        f"BEARISH: {s.get('bearish_rows')}",
        f"causality_failures: {s.get('causality_failures')}",
        f"runtime_seconds: {s.get('runtime_seconds')}",
    ]
    if paths:
        lines.append(f"timeline_csv: {paths.get('timeline_csv')}")
        lines.append(f"transitions_csv: {paths.get('transitions_csv')}")
    if result.summary.get("transitions_only"):
        lines.append("transitions:")
        for row in result.output_rows():
            lines.append(
                f"  {row['decision_time_utc']}  {row['direction']:<8}  "
                f"{row.get('structure_event')}  {row.get('reason')}"
            )
    return "\n".join(lines)
