"""Central research-only general runner for orderbook_analyse.

Orchestrates full-history Phase 0–5, Higher-Lows audits per replayable
segment, and Phase 6 causal pattern forward outcomes. No live-trading hooks,
no DB writes.
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import PROJECT_ROOT, connect_readonly, parse_utc, utc_now
from orderbook_analyse.full_history_analysis import FullHistoryParams, run_full_history_phase01
from orderbook_analyse.orderbook_price_higher_lows_ceiling_audit import (
    HigherLowParams,
    run_higher_lows_audit_from_state,
)
from orderbook_analyse.orderbook_trade_candidate_audit import AuditParams, prepare_tracker_state
from orderbook_analyse.pattern_outcome_evaluation import (
    OutcomeParams,
    PatternOutcomeError,
    parse_float_list,
    parse_int_list,
    run_pattern_outcome_evaluation,
    validate_outcome_params,
)

logger = logging.getLogger(__name__)

BASELINE_HL_VARIANTS = frozenset({"P0", "P1", "P2"})

EVAL_INPUT_HEADERS = [
    "symbol",
    "segment_id",
    "source_family",
    "pattern_type",
    "variant",
    "event_id",
    "event_time",
    "event_price",
    "side",
    "sequence_id",
    "transition_type",
    "transition_time",
    "armed_pair_id",
    "armed_time",
    "action_time",
    "data_complete",
    "source_output_dir",
]

PHASE_STATUS_HEADERS = [
    "phase",
    "requested",
    "status",
    "runtime_sec",
    "input_rows",
    "output_rows",
    "error_type",
    "error_message",
    "output_path",
]

ERROR_HEADERS = [
    "phase",
    "segment_id",
    "error_type",
    "error_message",
    "details",
]


class GeneralResearchError(ValueError):
    pass


@dataclass
class GeneralResearchParams:
    symbol: str
    start: datetime | None = None
    end: datetime | None = None
    output_dir: Path | None = None
    pattern_timeframe: str = "1m"
    pattern_lookback_bars: int = 5
    max_bar_range_pct: float = 20.0
    warmup_seconds: int = 300
    replay_sample_interval: int = 60
    wall_sample_interval: int = 60
    higher_low_armed_seconds: tuple[int, ...] = (0, 300, 600, 900, 1800)
    max_pullback_duration_seconds: int = 900
    max_pullback_depth_bps: float = 100.0
    log_level: str = "INFO"
    skip_higher_lows: bool = False
    run_pattern_outcomes: bool = True
    skip_pattern_outcomes: bool = False
    outcome_horizons_seconds: tuple[int, ...] = (60, 300, 900, 1800, 3600, 7200)
    outcome_targets_bps: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0)
    outcome_stop_bps: tuple[float, ...] = (25.0, 50.0, 100.0)
    outcome_price_source: str = "mid"
    outcome_min_samples: int = 30
    outcome_bootstrap_iterations: int = 1000
    outcome_random_seed: int = 42
    continue_on_phase_error: bool = False
    overwrite: bool = False
    snapshot_seconds: int = 30


def parse_armed_seconds(raw: str | None) -> tuple[int, ...]:
    text = (raw or "0,300,600,900,1800").strip()
    vals: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        v = int(part)
        if v < 0:
            raise GeneralResearchError(f"higher-low-armed-seconds must be >= 0, got {v}")
        vals.append(v)
    if not vals:
        raise GeneralResearchError("higher-low-armed-seconds must contain at least one value")
    return tuple(vals)


def default_output_dir(symbol: str) -> Path:
    day = utc_now().strftime("%Y%m%d")
    return PROJECT_ROOT / "results" / f"general_research_{symbol}_{day}"


def write_csv_headered(
    path: Path, rows: Sequence[Mapping[str, Any]], headers: Sequence[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(headers), extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({h: row.get(h) for h in headers})


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y"}


def _iso(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _parse_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def prepare_output_dir(path: Path, *, overwrite: bool) -> None:
    if path.exists():
        contents = list(path.iterdir()) if path.is_dir() else ["."]
        if contents and not overwrite:
            raise GeneralResearchError(
                f"output directory already exists and is not empty: {path}. "
                "Pass --overwrite to replace only this directory."
            )
        if overwrite:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    path.mkdir(parents=True, exist_ok=True)


def select_replayable_segments(
    *,
    replay_segments_path: Path,
    replay_results_path: Path | None = None,
) -> list[dict[str, str]]:
    """Return segments that are replayable and (if results exist) REPLAY_OK*."""
    segments = read_csv_rows(replay_segments_path)
    replayable = [s for s in segments if _truthy(s.get("is_replayable"))]
    if replay_results_path is None or not replay_results_path.exists():
        return sorted(replayable, key=lambda r: str(r.get("segment_id") or ""))
    results = read_csv_rows(replay_results_path)
    ok_ids = {
        str(r.get("segment_id"))
        for r in results
        if str(r.get("replay_status") or "").startswith("REPLAY_OK")
    }
    selected = [s for s in replayable if str(s.get("segment_id")) in ok_ids]
    return sorted(selected, key=lambda r: str(r.get("segment_id") or ""))


def _phase_row(
    *,
    phase: str,
    requested: bool,
    status: str,
    runtime_sec: float | None = None,
    input_rows: int | None = None,
    output_rows: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "requested": requested,
        "status": status,
        "runtime_sec": runtime_sec,
        "input_rows": input_rows,
        "output_rows": output_rows,
        "error_type": error_type,
        "error_message": error_message,
        "output_path": output_path,
    }


def build_pattern_eval_rows(
    *,
    symbol: str,
    pattern_candidates_path: Path,
    source_output_dir: Path,
) -> list[dict[str, Any]]:
    rows_out: list[dict[str, Any]] = []
    for r in read_csv_rows(pattern_candidates_path):
        rows_out.append(
            {
                "symbol": r.get("symbol") or symbol,
                "segment_id": r.get("segment_id") or "",
                "source_family": r.get("pattern_family") or "PATTERN_CANDIDATE",
                "pattern_type": r.get("pattern_type") or "",
                "variant": "",
                "event_id": r.get("pattern_id") or "",
                "event_time": r.get("pattern_ts") or "",
                "event_price": r.get("close_price") or r.get("wall_price") or "",
                "side": r.get("pattern_side") or "",
                "sequence_id": r.get("source_wall_sequence_id") or "",
                "transition_type": r.get("source_transition_type") or "",
                "transition_time": r.get("source_transition_ts") or "",
                "armed_pair_id": "",
                "armed_time": "",
                "action_time": "",
                "data_complete": _truthy(r.get("data_complete")),
                "source_output_dir": str(source_output_dir),
            }
        )
    return rows_out


def build_higher_low_eval_rows(
    *,
    symbol: str,
    segment_id: str,
    armed_dir: Path,
    armed_seconds: int,
) -> list[dict[str, Any]]:
    """Aggregate armed HL actions; exclude baseline P0–P2."""
    path = armed_dir / "higher_low_raw_signals.csv"
    rows_out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in read_csv_rows(path):
        variant = str(r.get("variant") or "")
        if variant in BASELINE_HL_VARIANTS:
            continue
        armed_pair_id = str(r.get("armed_pair_id") or "")
        if not armed_pair_id:
            continue
        key = (segment_id, armed_pair_id, variant)
        if key in seen:
            continue
        seen.add(key)
        rows_out.append(
            {
                "symbol": symbol,
                "segment_id": segment_id,
                "source_family": "HIGHER_LOW_ARMED_ACTION",
                "pattern_type": f"HL_{variant}",
                "variant": variant,
                "event_id": r.get("signal_id") or "",
                "event_time": r.get("signal_time") or r.get("action_time") or "",
                "event_price": r.get("signal_price") or "",
                "side": "long",
                "sequence_id": "",
                "transition_type": "",
                "transition_time": "",
                "armed_pair_id": armed_pair_id,
                "armed_time": r.get("armed_time") or "",
                "action_time": r.get("action_time") or "",
                "data_complete": True,
                "source_output_dir": str(armed_dir),
                "_armed_seconds": armed_seconds,
            }
        )
    return rows_out


def check_general_integrity(
    *,
    output_dir: Path,
    full_history_dir: Path,
    eval_rows: Sequence[Mapping[str, Any]],
    phase_status: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    hl_segment_bounds: Mapping[str, tuple[datetime | None, datetime | None]],
    skip_higher_lows: bool,
) -> dict[str, Any]:
    errs: list[str] = []
    warns: list[str] = []

    required = [
        "GENERAL_REPORT.md",
        "general_summary.json",
        "general_phase_status.csv",
        "general_pattern_evaluation_input.csv",
        "general_errors.csv",
    ]
    for name in required:
        if not (output_dir / name).exists():
            errs.append(f"missing required output {name}")

    # Pattern ID uniqueness
    pc_path = full_history_dir / "pattern_candidates.csv"
    if pc_path.exists():
        ids = [r.get("pattern_id") for r in read_csv_rows(pc_path)]
        if len(ids) != len(set(ids)):
            errs.append("duplicate pattern_id in pattern_candidates.csv")
        if int(summary.get("pattern_candidate_count") or 0) != len(ids):
            errs.append("pattern_candidate_count mismatch vs pattern_candidates.csv")

    # HL action integrity from eval rows (unique per armed-window output)
    hl_keys: set[tuple[str, str, str, str]] = set()
    for r in eval_rows:
        if str(r.get("source_family")) != "HIGHER_LOW_ARMED_ACTION":
            continue
        sid = str(r.get("segment_id") or "")
        pair = str(r.get("armed_pair_id") or "")
        variant = str(r.get("variant") or "")
        src = str(r.get("source_output_dir") or "")
        key = (sid, pair, variant, src)
        if key in hl_keys:
            errs.append(f"duplicate HL action key {key}")
        hl_keys.add(key)

        at = _parse_dt(r.get("action_time"))
        armed = _parse_dt(r.get("armed_time"))
        if at is None or armed is None:
            errs.append(f"missing armed/action time for {r.get('event_id')}")
            continue
        if at < armed:
            errs.append(f"action_time < armed_time for {r.get('event_id')}")
        armed_sec = r.get("_armed_seconds")
        if armed_sec is not None and int(armed_sec) > 0:
            # window end inclusive within armed window
            window_end = armed.timestamp() + float(armed_sec)
            if at.timestamp() > window_end + 1e-6:
                errs.append(f"action outside armed window for {r.get('event_id')}")

        bounds = hl_segment_bounds.get(sid)
        if bounds:
            lo, hi = bounds
            et = _parse_dt(r.get("event_time"))
            if et is not None and lo is not None and et < lo:
                errs.append(f"event_time before segment start {r.get('event_id')}")
            if et is not None and hi is not None and et > hi:
                errs.append(f"event_time after segment end {r.get('event_id')}")

    # No HL on non-replayable: all HL eval segment_ids must be in bounds map
    for r in eval_rows:
        if str(r.get("source_family")) != "HIGHER_LOW_ARMED_ACTION":
            continue
        if str(r.get("segment_id")) not in hl_segment_bounds:
            errs.append(
                f"HL action for non-selected segment {r.get('segment_id')}"
            )

    if skip_higher_lows and any(
        str(r.get("source_family")) == "HIGHER_LOW_ARMED_ACTION" for r in eval_rows
    ):
        errs.append("HL actions present despite skip_higher_lows")

    # phase status file row count
    ps_path = output_dir / "general_phase_status.csv"
    if ps_path.exists():
        n = len(read_csv_rows(ps_path))
        if n != len(phase_status):
            errs.append("general_phase_status row count mismatch")

    eval_path = output_dir / "general_pattern_evaluation_input.csv"
    if eval_path.exists():
        n = len(read_csv_rows(eval_path))
        # strip internal keys for comparison
        if n != len(eval_rows):
            errs.append("general_pattern_evaluation_input row count mismatch")

    return {"ok": len(errs) == 0, "errors": errs, "warnings": warns}


def decide_general(
    *,
    integrity_ok: bool,
    gap_count: int,
    hard_failure: bool,
    soft_warnings: bool,
) -> str:
    if hard_failure or not integrity_ok:
        return "GENERAL_ANALYSIS_FAILED"
    if soft_warnings:
        return "GENERAL_ANALYSIS_COMPLETE_WITH_WARNINGS"
    if gap_count > 0:
        return "GENERAL_ANALYSIS_COMPLETE_WITH_GAPS"
    return "GENERAL_ANALYSIS_COMPLETE"


def render_general_report(
    *,
    decision: str,
    summary: Mapping[str, Any],
    phase_status: Sequence[Mapping[str, Any]],
    limitations: Sequence[str],
) -> str:
    lines = [
        "# General Orderbook Research Report",
        "",
        f"**Decision:** `{decision}`",
        "",
        f"- Symbol: `{summary.get('symbol')}`",
        f"- Window: `{summary.get('analysis_start')}` → `{summary.get('analysis_end')}`",
        f"- Full-history decision: `{summary.get('full_history_decision')}`",
        f"- Coverage: {summary.get('coverage_pct')}%",
        f"- Gaps: {summary.get('gap_count')}",
        f"- Segments total/replayable/ok: "
        f"{summary.get('segments_total')} / {summary.get('segments_replayable')} / "
        f"{summary.get('segments_replayed_ok')}",
        f"- Market/wall/pattern ok: "
        f"{summary.get('market_context_ok')} / {summary.get('wall_history_ok')} / "
        f"{summary.get('pattern_candidates_ok')}",
        f"- Pattern candidates: {summary.get('pattern_candidate_count')}",
        f"- Higher-lows segments ok/failed: "
        f"{summary.get('higher_lows_segments_ok')} / {summary.get('higher_lows_segments_failed')}",
        f"- Confirmed lows / HL pairs / armed pairs / armed actions: "
        f"{summary.get('confirmed_low_count_total')} / "
        f"{summary.get('higher_low_pair_count_total')} / "
        f"{summary.get('armed_pair_count_total')} / "
        f"{summary.get('armed_action_count_total')}",
        f"- Phase 6 outcomes requested/ok: "
        f"{summary.get('pattern_outcomes_requested')} / {summary.get('pattern_outcomes_ok')}",
        f"- Phase 6 decision: `{summary.get('pattern_outcome_decision')}`",
        f"- Outcome events complete/incomplete: "
        f"{summary.get('pattern_outcome_complete_count')} / "
        f"{summary.get('pattern_outcome_incomplete_count')}",
        f"- Promising_for_oos groups: {summary.get('pattern_promising_for_oos_count')}",
        "",
        "## Phase status",
        "",
    ]
    for p in phase_status:
        lines.append(
            f"- `{p.get('phase')}`: {p.get('status')} "
            f"(runtime={p.get('runtime_sec')}, out={p.get('output_rows')})"
        )
    lines += ["", "## Limitations", ""]
    for lim in limitations:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def default_run_higher_lows_for_segment(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    segment_out_dir: Path,
    armed_seconds: Sequence[int],
    max_pullback_duration_seconds: int,
    max_pullback_depth_bps: float,
    snapshot_seconds: int = 30,
) -> dict[str, Any]:
    """Load tracker state once, run armed-seconds ablations into subdirs."""
    segment_out_dir.mkdir(parents=True, exist_ok=True)
    db = connect_readonly()
    try:
        state = prepare_tracker_state(
            db=db,
            symbol=symbol,
            start=start,
            end=end,
            params=AuditParams(sample_seconds=snapshot_seconds),
        )
        summaries: dict[str, Any] = {}
        totals = {
            "confirmed_low_count": 0,
            "higher_low_pair_count": 0,
            "armed_pair_count": 0,
            "armed_action_count": 0,
        }
        for armed_s in armed_seconds:
            sub = segment_out_dir / f"armed_{int(armed_s)}s"
            params = HigherLowParams(
                snapshot_seconds=snapshot_seconds,
                max_pullback_duration_seconds=max_pullback_duration_seconds,
                max_pullback_depth_bps=max_pullback_depth_bps,
                higher_low_armed_seconds=int(armed_s),
                symbol=symbol,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            summary = run_higher_lows_audit_from_state(
                snapshots=state["snapshots"],
                transitions=state["transitions"],
                output_dir=sub,
                params=params,
                a2_times=[],
                g5_warning_times=[],
                g5_action_times=[],
                absorption_by_ts={},
            )
            summaries[str(armed_s)] = summary
            integ = summary.get("integrity") or {}
            totals["confirmed_low_count"] += int(integ.get("confirmed_low_count") or 0)
            totals["higher_low_pair_count"] += int(integ.get("higher_low_pair_count") or 0)
            totals["armed_pair_count"] += int(integ.get("armed_pair_count") or 0)
            totals["armed_action_count"] += int(integ.get("armed_action_count") or 0)
        return {"ok": True, "summaries": summaries, "totals": totals, "error": None}
    finally:
        db.close()


FullHistoryRunner = Callable[..., dict[str, Any]]
HigherLowsSegmentRunner = Callable[..., dict[str, Any]]


def run_general_research(
    params: GeneralResearchParams,
    *,
    full_history_runner: FullHistoryRunner | None = None,
    higher_lows_segment_runner: HigherLowsSegmentRunner | None = None,
) -> dict[str, Any]:
    """Run full-history stack + per-segment Higher-Lows + general artifacts."""
    t_all = time.perf_counter()
    symbol = str(params.symbol).upper()
    out_dir = params.output_dir or default_output_dir(symbol)
    prepare_output_dir(out_dir, overwrite=bool(params.overwrite))

    fh_dir = out_dir / "full_history"
    hl_root = out_dir / "higher_lows"
    fh_dir.mkdir(parents=True, exist_ok=True)

    phase_status: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    limitations = [
        "Research-only runner; no live trading, no DB writes, no forward outcomes.",
        "Phase 5 pattern candidates are descriptive only (is_trading_signal=False).",
        "Higher-Lows P0–P2 baselines are excluded from armed-action evaluation input.",
        "general_pattern_evaluation_input.csv prepares Phase 6; no edge is claimed.",
    ]

    fh_runner = full_history_runner or run_full_history_phase01
    hl_runner = higher_lows_segment_runner or default_run_higher_lows_for_segment

    # --- Full history ---
    t0 = time.perf_counter()
    fh_result: dict[str, Any] | None = None
    fh_error: Exception | None = None
    try:
        fh_params = FullHistoryParams(
            symbol=symbol,
            start=params.start,
            end=params.end,
            output_dir=fh_dir,
            run_segment_replay=False,  # auto-enabled via wall/pattern deps
            run_market_context=True,
            run_wall_history=True,
            run_pattern_candidates=True,
            pattern_timeframe=params.pattern_timeframe,
            pattern_lookback_bars=int(params.pattern_lookback_bars),
            max_bar_range_pct=float(params.max_bar_range_pct),
            warmup_seconds=int(params.warmup_seconds),
            replay_sample_interval=int(params.replay_sample_interval),
            wall_sample_interval=int(params.wall_sample_interval),
            log_level=params.log_level,
        )
        fh_result = fh_runner(params=fh_params)
    except Exception as exc:  # noqa: BLE001
        fh_error = exc
        logger.exception("full-history phase failed")
        errors.append(
            {
                "phase": "full_history",
                "segment_id": "",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "details": "",
            }
        )
        if not params.continue_on_phase_error:
            phase_status.append(
                _phase_row(
                    phase="full_history",
                    requested=True,
                    status="FAILED",
                    runtime_sec=time.perf_counter() - t0,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    output_path=str(fh_dir),
                )
            )
            summary = {
                "symbol": symbol,
                "analysis_start": None if params.start is None else params.start.isoformat(),
                "analysis_end": None if params.end is None else params.end.isoformat(),
                "full_history_decision": None,
                "coverage_pct": None,
                "gap_count": 0,
                "segments_total": 0,
                "segments_replayable": 0,
                "segments_replayed_ok": 0,
                "market_context_ok": False,
                "wall_history_ok": False,
                "pattern_candidates_ok": False,
                "pattern_candidate_count": 0,
                "pattern_integrity_error_count": 0,
                "higher_lows_segments_requested": 0,
                "higher_lows_segments_ok": 0,
                "higher_lows_segments_failed": 0,
                "confirmed_low_count_total": 0,
                "higher_low_pair_count_total": 0,
                "armed_pair_count_total": 0,
                "armed_action_count_total": 0,
                "general_integrity_ok": False,
                "decision": "GENERAL_ANALYSIS_FAILED",
            }
            write_csv_headered(out_dir / "general_phase_status.csv", phase_status, PHASE_STATUS_HEADERS)
            write_csv_headered(out_dir / "general_errors.csv", errors, ERROR_HEADERS)
            write_csv_headered(out_dir / "general_pattern_evaluation_input.csv", [], EVAL_INPUT_HEADERS)
            (out_dir / "general_summary.json").write_bytes(
                orjson.dumps(summary, option=orjson.OPT_INDENT_2)
            )
            (out_dir / "GENERAL_REPORT.md").write_text(
                render_general_report(
                    decision="GENERAL_ANALYSIS_FAILED",
                    summary=summary,
                    phase_status=phase_status,
                    limitations=limitations,
                ),
                encoding="utf-8",
            )
            return {
                "decision": "GENERAL_ANALYSIS_FAILED",
                "output_dir": str(out_dir),
                "summary": summary,
                "phase_status": phase_status,
                "errors": errors,
                "integrity": {"ok": False, "errors": [str(exc)], "warnings": []},
            }

    fh_summary = (fh_result or {}).get("summary") or {}
    fh_runtime = time.perf_counter() - t0
    gap_count = int(fh_summary.get("gap_count") or 0)
    fh_decision = str((fh_result or {}).get("decision") or "")
    fh_status = "FAILED" if fh_error or "FAILED" in fh_decision else (
        "OK_WITH_GAPS" if gap_count > 0 or "WITH_GAPS" in fh_decision else "OK"
    )
    phase_status.extend(
        [
            _phase_row(
                phase="data_inventory_gap_detection",
                requested=True,
                status=fh_status if not fh_error else "FAILED",
                runtime_sec=None,
                output_path=str(fh_dir / "data_inventory.csv"),
            ),
            _phase_row(
                phase="replay_segmentation",
                requested=True,
                status=fh_status if not fh_error else "FAILED",
                input_rows=int(fh_summary.get("segment_count") or 0),
                output_rows=int(fh_summary.get("segment_count") or 0),
                output_path=str(fh_dir / "replay_segments.csv"),
            ),
            _phase_row(
                phase="segment_replay",
                requested=True,
                status=fh_status if not fh_error else "FAILED",
                input_rows=int(fh_summary.get("segments_replayable") or fh_summary.get("replayable_segment_count") or 0),
                output_rows=int(fh_summary.get("segments_replay_ok") or 0),
                output_path=str(fh_dir / "segment_replay_results.csv"),
            ),
            _phase_row(
                phase="market_context",
                requested=True,
                status="OK" if fh_summary.get("market_context_ok") else "FAILED",
                output_rows=int(fh_summary.get("timeline_rows_1m") or 0),
                output_path=str(fh_dir / "analysis_timeline_1m.csv"),
            ),
            _phase_row(
                phase="wall_history",
                requested=True,
                status="OK" if fh_summary.get("wall_history_ok") else "FAILED",
                output_rows=int(fh_summary.get("wall_sequences_total") or 0),
                output_path=str(fh_dir / "wall_sequences.csv"),
            ),
            _phase_row(
                phase="pattern_candidates",
                requested=True,
                status="OK" if fh_summary.get("pattern_candidates_ok") else "FAILED",
                output_rows=int(fh_summary.get("pattern_candidate_count") or 0),
                runtime_sec=fh_summary.get("pattern_runtime_sec"),
                output_path=str(fh_dir / "pattern_candidates.csv"),
            ),
            _phase_row(
                phase="full_history",
                requested=True,
                status=fh_status,
                runtime_sec=fh_runtime,
                output_path=str(fh_dir),
                error_type=None if fh_error is None else type(fh_error).__name__,
                error_message=None if fh_error is None else str(fh_error),
            ),
        ]
    )

    # --- Higher lows ---
    hl_requested = not params.skip_higher_lows
    hl_ok = 0
    hl_fail = 0
    hl_requested_n = 0
    confirmed_total = 0
    pairs_total = 0
    armed_pairs_total = 0
    armed_actions_total = 0
    eval_hl_rows: list[dict[str, Any]] = []
    hl_bounds: dict[str, tuple[datetime | None, datetime | None]] = {}
    soft_warnings = bool(fh_error)

    if not hl_requested:
        phase_status.append(
            _phase_row(
                phase="higher_lows_audit",
                requested=False,
                status="NOT_REQUESTED",
                output_path=str(hl_root),
            )
        )
        limitations.append("--skip-higher-lows set; Higher-Lows audit not run.")
    else:
        t_hl = time.perf_counter()
        segments = select_replayable_segments(
            replay_segments_path=fh_dir / "replay_segments.csv",
            replay_results_path=fh_dir / "segment_replay_results.csv",
        )
        hl_requested_n = len(segments)
        if not segments:
            phase_status.append(
                _phase_row(
                    phase="higher_lows_audit",
                    requested=True,
                    status="SKIPPED_NO_DATA",
                    runtime_sec=time.perf_counter() - t_hl,
                    input_rows=0,
                    output_rows=0,
                    output_path=str(hl_root),
                    error_message="no replayable/REPLAY_OK segments",
                )
            )
        else:
            hl_root.mkdir(parents=True, exist_ok=True)
            for seg in segments:
                seg_id = str(seg.get("segment_id") or "")
                seg_start = _parse_dt(seg.get("segment_start_ts"))
                seg_end = _parse_dt(seg.get("segment_end_ts"))
                hl_bounds[seg_id] = (seg_start, seg_end)
                if seg_start is None or seg_end is None:
                    hl_fail += 1
                    soft_warnings = True
                    errors.append(
                        {
                            "phase": "higher_lows_audit",
                            "segment_id": seg_id,
                            "error_type": "MissingSegmentBounds",
                            "error_message": "segment_start_ts/segment_end_ts missing",
                            "details": "",
                        }
                    )
                    if not params.continue_on_phase_error:
                        break
                    continue
                seg_out = hl_root / seg_id
                try:
                    result = hl_runner(
                        symbol=symbol,
                        start=seg_start,
                        end=seg_end,
                        segment_out_dir=seg_out,
                        armed_seconds=params.higher_low_armed_seconds,
                        max_pullback_duration_seconds=params.max_pullback_duration_seconds,
                        max_pullback_depth_bps=params.max_pullback_depth_bps,
                        snapshot_seconds=params.snapshot_seconds,
                    )
                    if not result.get("ok", True):
                        raise RuntimeError(result.get("error") or "higher lows failed")
                    totals = result.get("totals") or {}
                    confirmed_total += int(totals.get("confirmed_low_count") or 0)
                    pairs_total += int(totals.get("higher_low_pair_count") or 0)
                    armed_pairs_total += int(totals.get("armed_pair_count") or 0)
                    armed_actions_total += int(totals.get("armed_action_count") or 0)
                    for armed_s in params.higher_low_armed_seconds:
                        armed_dir = seg_out / f"armed_{int(armed_s)}s"
                        eval_hl_rows.extend(
                            build_higher_low_eval_rows(
                                symbol=symbol,
                                segment_id=seg_id,
                                armed_dir=armed_dir,
                                armed_seconds=int(armed_s),
                            )
                        )
                    hl_ok += 1
                except Exception as exc:  # noqa: BLE001
                    hl_fail += 1
                    soft_warnings = True
                    logger.exception("higher lows failed for %s", seg_id)
                    errors.append(
                        {
                            "phase": "higher_lows_audit",
                            "segment_id": seg_id,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "details": "",
                        }
                    )
                    if not params.continue_on_phase_error:
                        break

            hl_status = (
                "FAILED"
                if hl_fail and not params.continue_on_phase_error
                else ("OK" if hl_fail == 0 else "OK_WITH_GAPS")
            )
            if hl_fail and params.continue_on_phase_error:
                hl_status = "OK_WITH_GAPS"
                soft_warnings = True
            if hl_fail and not params.continue_on_phase_error:
                hl_status = "FAILED"
            phase_status.append(
                _phase_row(
                    phase="higher_lows_audit",
                    requested=True,
                    status=hl_status,
                    runtime_sec=time.perf_counter() - t_hl,
                    input_rows=hl_requested_n,
                    output_rows=hl_ok,
                    output_path=str(hl_root),
                    error_message=None if hl_fail == 0 else f"{hl_fail} segment(s) failed",
                )
            )

    # --- Evaluation input ---
    t_eval = time.perf_counter()
    pattern_rows = build_pattern_eval_rows(
        symbol=symbol,
        pattern_candidates_path=fh_dir / "pattern_candidates.csv",
        source_output_dir=fh_dir,
    )
    eval_rows = pattern_rows + eval_hl_rows
    eval_rows.sort(
        key=lambda r: (
            str(r.get("event_time") or ""),
            str(r.get("source_family") or ""),
            str(r.get("event_id") or ""),
            str(r.get("variant") or ""),
            str(r.get("segment_id") or ""),
        )
    )
    # Persist without internal helper fields
    eval_out = [{k: v for k, v in r.items() if not str(k).startswith("_")} for r in eval_rows]
    write_csv_headered(
        out_dir / "general_pattern_evaluation_input.csv", eval_out, EVAL_INPUT_HEADERS
    )
    phase_status.append(
        _phase_row(
            phase="pattern_evaluation_input",
            requested=True,
            status="OK",
            runtime_sec=time.perf_counter() - t_eval,
            input_rows=len(pattern_rows) + len(eval_hl_rows),
            output_rows=len(eval_out),
            output_path=str(out_dir / "general_pattern_evaluation_input.csv"),
        )
    )

    # --- Phase 6: pattern forward outcomes ---
    po_dir = out_dir / "pattern_outcomes"
    po_requested = bool(params.run_pattern_outcomes) and not bool(params.skip_pattern_outcomes)
    po_summary: dict[str, Any] = {
        "pattern_outcomes_requested": po_requested,
        "pattern_outcomes_ok": False,
        "pattern_outcome_event_count": 0,
        "pattern_outcome_complete_count": 0,
        "pattern_outcome_incomplete_count": 0,
        "pattern_cluster_count": 0,
        "pattern_directional_event_count": 0,
        "pattern_neutral_event_count": 0,
        "pattern_unknown_event_count": 0,
        "pattern_groups_tested": 0,
        "pattern_groups_sufficient": 0,
        "pattern_promising_for_oos_count": 0,
        "pattern_outcome_integrity_error_count": 0,
        "pattern_outcome_decision": None,
    }
    if not po_requested:
        phase_status.append(
            _phase_row(
                phase="pattern_outcomes",
                requested=False,
                status="NOT_REQUESTED",
                output_path=str(po_dir),
            )
        )
        limitations.append("Phase 6 skipped (--skip-pattern-outcomes).")
    else:
        t_po = time.perf_counter()
        try:
            oparams = validate_outcome_params(
                OutcomeParams(
                    horizons_seconds=params.outcome_horizons_seconds,
                    targets_bps=params.outcome_targets_bps,
                    stops_bps=params.outcome_stop_bps,
                    price_source=params.outcome_price_source,
                    min_samples=int(params.outcome_min_samples),
                    bootstrap_iterations=int(params.outcome_bootstrap_iterations),
                    random_seed=int(params.outcome_random_seed),
                )
            )
            po_result = run_pattern_outcome_evaluation(
                general_output_dir=out_dir,
                full_history_dir=fh_dir,
                eval_input_path=out_dir / "general_pattern_evaluation_input.csv",
                output_dir=po_dir,
                params=oparams,
            )
            po_summary.update(
                {
                    "pattern_outcomes_ok": bool(po_result.ok),
                    "pattern_outcome_event_count": int(po_result.summary.get("pattern_outcome_event_count") or 0),
                    "pattern_outcome_complete_count": int(po_result.summary.get("pattern_outcome_complete_count") or 0),
                    "pattern_outcome_incomplete_count": int(po_result.summary.get("pattern_outcome_incomplete_count") or 0),
                    "pattern_cluster_count": int(po_result.summary.get("pattern_cluster_count") or 0),
                    "pattern_directional_event_count": int(po_result.summary.get("pattern_directional_event_count") or 0),
                    "pattern_neutral_event_count": int(po_result.summary.get("pattern_neutral_event_count") or 0),
                    "pattern_unknown_event_count": int(po_result.summary.get("pattern_unknown_event_count") or 0),
                    "pattern_groups_tested": int(po_result.summary.get("pattern_groups_tested") or 0),
                    "pattern_groups_sufficient": int(po_result.summary.get("pattern_groups_sufficient") or 0),
                    "pattern_promising_for_oos_count": int(po_result.summary.get("pattern_promising_for_oos_count") or 0),
                    "pattern_outcome_integrity_error_count": int(
                        po_result.summary.get("pattern_outcome_integrity_error_count") or 0
                    ),
                    "pattern_outcome_decision": po_result.decision,
                }
            )
            st = "OK"
            if not po_result.ok:
                st = "FAILED"
                soft_warnings = True
                if not params.continue_on_phase_error:
                    # hard failure handled below via summary flags
                    pass
            elif "WITH_WARNINGS" in str(po_result.decision):
                st = "OK_WITH_GAPS"
                soft_warnings = True
            elif "WITH_GAPS" in str(po_result.decision):
                st = "OK_WITH_GAPS"
            elif "INSUFFICIENT" in str(po_result.decision):
                # Technical completeness without usable forward samples — not a soft failure
                st = "OK_WITH_GAPS"
                limitations.append(
                    "Phase 6: PATTERN_OUTCOMES_DATA_INSUFFICIENT "
                    "(no complete forward outcomes)."
                )
            phase_status.append(
                _phase_row(
                    phase="pattern_outcomes",
                    requested=True,
                    status=st,
                    runtime_sec=time.perf_counter() - t_po,
                    input_rows=len(eval_out),
                    output_rows=int(po_result.summary.get("pattern_outcome_row_count") or 0),
                    output_path=str(po_dir),
                    error_message=None if po_result.ok else "; ".join((po_result.integrity or {}).get("errors") or [])[:500],
                )
            )
            if not po_result.ok and not params.continue_on_phase_error:
                errors.append(
                    {
                        "phase": "pattern_outcomes",
                        "segment_id": "",
                        "error_type": "PHASE6_FAILED",
                        "error_message": po_result.decision,
                        "details": "",
                    }
                )
        except (PatternOutcomeError, Exception) as exc:  # noqa: BLE001
            soft_warnings = True
            logger.exception("phase 6 pattern outcomes failed")
            errors.append(
                {
                    "phase": "pattern_outcomes",
                    "segment_id": "",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "details": "",
                }
            )
            phase_status.append(
                _phase_row(
                    phase="pattern_outcomes",
                    requested=True,
                    status="FAILED",
                    runtime_sec=time.perf_counter() - t_po,
                    output_path=str(po_dir),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            po_summary["pattern_outcomes_ok"] = False
            po_summary["pattern_outcome_decision"] = "PATTERN_OUTCOMES_FAILED"
            if not params.continue_on_phase_error:
                # will flip hard_failure below
                fh_summary = dict(fh_summary)
                fh_summary["pattern_outcomes_hard_fail"] = True

    hard_failure = bool(fh_error) or (
        hl_requested
        and hl_fail > 0
        and not params.continue_on_phase_error
    )
    # Also fail if pattern/wall/phase6 explicitly failed without continue
    if not params.continue_on_phase_error:
        if fh_summary.get("pattern_candidates_ok") is False:
            hard_failure = True
        if fh_summary.get("wall_history_ok") is False:
            hard_failure = True
        if fh_summary.get("market_context_ok") is False:
            hard_failure = True
        if fh_summary.get("pattern_outcomes_hard_fail"):
            hard_failure = True
        if po_requested and po_summary.get("pattern_outcomes_ok") is False:
            hard_failure = True

    summary: dict[str, Any] = {
        "symbol": symbol,
        "analysis_start": fh_summary.get("analysis_start")
        or (None if params.start is None else params.start.isoformat()),
        "analysis_end": fh_summary.get("analysis_end")
        or (None if params.end is None else params.end.isoformat()),
        "full_history_decision": fh_decision or None,
        "coverage_pct": fh_summary.get("coverage_pct"),
        "gap_count": gap_count,
        "segments_total": int(fh_summary.get("segment_count") or fh_summary.get("segments_total") or 0),
        "segments_replayable": int(
            fh_summary.get("replayable_segment_count")
            or fh_summary.get("segments_replayable")
            or 0
        ),
        "segments_replayed_ok": int(fh_summary.get("segments_replay_ok") or 0),
        "market_context_ok": bool(fh_summary.get("market_context_ok")),
        "wall_history_ok": bool(fh_summary.get("wall_history_ok")),
        "pattern_candidates_ok": bool(fh_summary.get("pattern_candidates_ok")),
        "pattern_candidate_count": int(fh_summary.get("pattern_candidate_count") or 0),
        "pattern_integrity_error_count": int(
            fh_summary.get("pattern_integrity_error_count") or 0
        ),
        "higher_lows_segments_requested": hl_requested_n if hl_requested else 0,
        "higher_lows_segments_ok": hl_ok if hl_requested else 0,
        "higher_lows_segments_failed": hl_fail if hl_requested else 0,
        "confirmed_low_count_total": confirmed_total,
        "higher_low_pair_count_total": pairs_total,
        "armed_pair_count_total": armed_pairs_total,
        "armed_action_count_total": armed_actions_total,
        **po_summary,
        "general_integrity_ok": False,
        "decision": None,
        "runtime_sec_total": time.perf_counter() - t_all,
    }

    write_csv_headered(out_dir / "general_phase_status.csv", phase_status, PHASE_STATUS_HEADERS)
    write_csv_headered(out_dir / "general_errors.csv", errors, ERROR_HEADERS)

    decision = decide_general(
        integrity_ok=True,  # provisional; integrity checked after artifacts exist
        gap_count=gap_count,
        hard_failure=hard_failure,
        soft_warnings=soft_warnings and not hard_failure,
    )
    summary["decision"] = decision

    (out_dir / "GENERAL_REPORT.md").write_text(
        render_general_report(
            decision=decision,
            summary=summary,
            phase_status=phase_status,
            limitations=limitations,
        ),
        encoding="utf-8",
    )
    (out_dir / "general_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )

    integ = check_general_integrity(
        output_dir=out_dir,
        full_history_dir=fh_dir,
        eval_rows=eval_rows,
        phase_status=phase_status,
        summary=summary,
        hl_segment_bounds=hl_bounds,
        skip_higher_lows=params.skip_higher_lows,
    )
    if not integ.get("ok"):
        decision = "GENERAL_ANALYSIS_FAILED"
        summary["decision"] = decision
        summary["general_integrity_ok"] = False
        for e in integ.get("errors") or []:
            errors.append(
                {
                    "phase": "general_integrity",
                    "segment_id": "",
                    "error_type": "INTEGRITY",
                    "error_message": e,
                    "details": "",
                }
            )
        write_csv_headered(out_dir / "general_errors.csv", errors, ERROR_HEADERS)
        (out_dir / "general_summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "GENERAL_REPORT.md").write_text(
            render_general_report(
                decision=decision,
                summary=summary,
                phase_status=phase_status,
                limitations=list(limitations) + list(integ.get("errors") or []),
            ),
            encoding="utf-8",
        )
    else:
        summary["general_integrity_ok"] = True
        summary["decision"] = decide_general(
            integrity_ok=True,
            gap_count=gap_count,
            hard_failure=hard_failure,
            soft_warnings=soft_warnings and not hard_failure,
        )
        decision = summary["decision"]
        (out_dir / "general_summary.json").write_bytes(
            orjson.dumps(summary, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "GENERAL_REPORT.md").write_text(
            render_general_report(
                decision=decision,
                summary=summary,
                phase_status=phase_status,
                limitations=limitations,
            ),
            encoding="utf-8",
        )

    phase_status.append(
        _phase_row(
            phase="general_report",
            requested=True,
            status="OK" if integ.get("ok") else "FAILED",
            runtime_sec=time.perf_counter() - t_all,
            output_path=str(out_dir / "GENERAL_REPORT.md"),
        )
    )
    write_csv_headered(out_dir / "general_phase_status.csv", phase_status, PHASE_STATUS_HEADERS)

    return {
        "decision": decision,
        "output_dir": str(out_dir),
        "summary": summary,
        "phase_status": phase_status,
        "errors": errors,
        "integrity": integ,
        "full_history": fh_result,
        "evaluation_rows": eval_out,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "General research runner: full-history Phase 0–5 + Higher-Lows "
            "per replayable segment (no trading, no DB writes)."
        )
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--pattern-timeframe", default="1m")
    p.add_argument("--pattern-lookback-bars", type=int, default=5)
    p.add_argument("--max-bar-range-pct", type=float, default=20.0)
    p.add_argument("--warmup-seconds", type=int, default=300)
    p.add_argument("--replay-sample-interval", type=int, default=60)
    p.add_argument("--wall-sample-interval", type=int, default=60)
    p.add_argument(
        "--higher-low-armed-seconds",
        default="0,300,600,900,1800",
        help="Comma-separated armed windows in seconds",
    )
    p.add_argument("--max-pullback-duration-seconds", type=int, default=900)
    p.add_argument("--max-pullback-depth-bps", type=float, default=100.0)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--skip-higher-lows", action="store_true")
    p.add_argument(
        "--run-pattern-outcomes",
        action="store_true",
        default=True,
        help="Run Phase 6 pattern forward outcomes (default: on)",
    )
    p.add_argument(
        "--skip-pattern-outcomes",
        action="store_true",
        help="Skip Phase 6 pattern forward outcomes",
    )
    p.add_argument(
        "--outcome-horizons-seconds",
        default="60,300,900,1800,3600,7200",
    )
    p.add_argument("--outcome-targets-bps", default="10,25,50,100")
    p.add_argument("--outcome-stop-bps", default="25,50,100")
    p.add_argument(
        "--outcome-price-source",
        default="mid",
        choices=["mid", "close", "high_low"],
    )
    p.add_argument("--outcome-min-samples", type=int, default=30)
    p.add_argument("--outcome-bootstrap-iterations", type=int, default=1000)
    p.add_argument("--outcome-random-seed", type=int, default=42)
    p.add_argument("--continue-on-phase-error", action="store_true")
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace only the specified general output directory if it exists",
    )
    return p.parse_args(argv)


def params_from_args(args: argparse.Namespace) -> GeneralResearchParams:
    return GeneralResearchParams(
        symbol=str(args.symbol).upper(),
        start=None if not args.start else parse_utc(args.start),
        end=None if not args.end else parse_utc(args.end),
        output_dir=None if not args.output_dir else Path(args.output_dir),
        pattern_timeframe=str(args.pattern_timeframe),
        pattern_lookback_bars=int(args.pattern_lookback_bars),
        max_bar_range_pct=float(args.max_bar_range_pct),
        warmup_seconds=int(args.warmup_seconds),
        replay_sample_interval=int(args.replay_sample_interval),
        wall_sample_interval=int(args.wall_sample_interval),
        higher_low_armed_seconds=parse_armed_seconds(args.higher_low_armed_seconds),
        max_pullback_duration_seconds=int(args.max_pullback_duration_seconds),
        max_pullback_depth_bps=float(args.max_pullback_depth_bps),
        log_level=str(args.log_level),
        skip_higher_lows=bool(args.skip_higher_lows),
        run_pattern_outcomes=bool(args.run_pattern_outcomes) and not bool(args.skip_pattern_outcomes),
        skip_pattern_outcomes=bool(args.skip_pattern_outcomes),
        outcome_horizons_seconds=parse_int_list(
            args.outcome_horizons_seconds, default=(60, 300, 900, 1800, 3600, 7200)
        ),
        outcome_targets_bps=parse_float_list(args.outcome_targets_bps, default=(10, 25, 50, 100)),
        outcome_stop_bps=parse_float_list(args.outcome_stop_bps, default=(25, 50, 100)),
        outcome_price_source=str(args.outcome_price_source),
        outcome_min_samples=int(args.outcome_min_samples),
        outcome_bootstrap_iterations=int(args.outcome_bootstrap_iterations),
        outcome_random_seed=int(args.outcome_random_seed),
        continue_on_phase_error=bool(args.continue_on_phase_error),
        overwrite=bool(args.overwrite),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        params = params_from_args(args)
        result = run_general_research(params)
    except GeneralResearchError as exc:
        logger.error("%s", exc)
        return 2
    payload = {
        "decision": result.get("decision"),
        "output_dir": result.get("output_dir"),
        "summary": result.get("summary"),
        "integrity_ok": (result.get("integrity") or {}).get("ok"),
    }
    import sys

    sys.stdout.buffer.write(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    sys.stdout.write("\n")
    return 0 if (result.get("integrity") or {}).get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
