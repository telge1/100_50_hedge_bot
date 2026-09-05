"""F2 event-chain discovery from frozen F1 discovery artifacts.

This module traces descriptive post-flush sequences:

FLUSH -> IMPACT_COMPRESSION -> L2_RECOVERY -> PRICE_RECLAIM

It performs no ClickHouse access, no F1 recomputation, no threshold search,
no trade simulation and no profitability analysis.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.contracts import (
    L2_RECOVERY_RELATION,
    L2_SIDE_BY_DIRECTION,
    ORDERBOOK_CARRIED_FORWARD_POLICY,
)

FORMAT_VERSION = "oi_liq_impact_l2_event_chain/v1"
DEFAULT_HORIZON_MINUTES = 60
DIRECTIONS = ("LONG", "SHORT")

PHASE_FLUSH = "FLUSH"
PHASE_SEARCH_COMPRESSION = "SEARCH_COMPRESSION"
PHASE_SEARCH_RECOVERY = "SEARCH_RECOVERY"
PHASE_SEARCH_RECLAIM = "SEARCH_RECLAIM"
PHASE_COMPLETE = "COMPLETE"
PHASE_ABORTED = "ABORTED"

STAGE_FLUSH_ONLY = "FLUSH_ONLY"
STAGE_COMPRESSION_ONLY = "COMPRESSION_ONLY"
STAGE_L2_RECOVERY_ONLY = "L2_RECOVERY_ONLY"
STAGE_PRICE_RECLAIM = "PRICE_RECLAIM"

TERMINATION_TIMEOUT = "TIMEOUT"
TERMINATION_TECHNICAL_GAP_ABORT = "TECHNICAL_GAP_ABORT"
TERMINATION_IMPACT_DATA_ABORT = "IMPACT_DATA_ABORT"
TERMINATION_L2_DATA_ABORT = "L2_DATA_ABORT"
TERMINATION_WINDOW_END = "WINDOW_END"
TERMINATION_STAGE_NOT_REACHED = "STAGE_NOT_REACHED"
TERMINATION_COMPLETE = "COMPLETE"

EPISODE_COLUMNS = (
    "candidate_id",
    "symbol",
    "direction",
    "flush_minute",
    "flush_decision_at",
    "pre_flush_level",
    "flush_open",
    "flush_high",
    "flush_low",
    "flush_close",
    "flush_price_displacement_pct",
    "flush_oi_delta_pct_1m",
    "flush_liquidation_count",
    "flush_liquidation_notional",
    "flush_aggressive_notional",
    "flush_impact_per_aggressive_notional",
    "flush_genuine_support_depth_l50_mean",
    "flush_genuine_opposing_depth_l50_mean",
    "flush_ob_genuine_seconds",
    "flush_ob_carried_forward_seconds",
    "flush_technical_gap",
    "compression_minute",
    "compression_decision_at",
    "minutes_flush_to_compression",
    "compression_aggressive_notional",
    "compression_previous_aggressive_notional",
    "compression_impact_per_aggressive_notional",
    "compression_previous_impact_per_aggressive_notional",
    "compression_impact_delta",
    "compression_impact_ratio",
    "recovery_minute",
    "recovery_decision_at",
    "minutes_compression_to_recovery",
    "recovery_directional_depth_change",
    "recovery_directional_imbalance_change",
    "recovery_directional_net_add",
    "recovery_directional_net_add_change",
    "recovery_confirmed_by",
    "reclaim_minute",
    "reclaim_decision_at",
    "reclaim_close",
    "minutes_recovery_to_reclaim",
    "minutes_flush_to_reclaim",
    "min_distance_before_reclaim",
    "failed_approach_count",
    "stage_reached",
    "termination_reason",
    "horizon_minutes",
    "observed_minutes",
    "overlap_with_other_episodes",
    "overlapping_episode_count",
)

TIMELINE_COLUMNS = (
    "candidate_id",
    "symbol",
    "direction",
    "relative_minute",
    "minute",
    "decision_at",
    "phase_before",
    "phase_after",
    "transition_reason",
    "close",
    "pre_flush_level",
    "distance_to_pre_flush_level",
    "impact_compression_observed",
    "l2_recovery_observed",
    "directional_depth_change",
    "directional_imbalance_change",
    "directional_net_add_change",
    "technical_gap",
    "candle_present",
    "trades_present",
    "oi_state_valid",
    "ob_genuine_seconds",
    "ob_carried_forward_seconds",
    "aggressive_notional",
    "impact_per_aggressive_notional",
    "previous_impact_per_aggressive_notional",
    "oi_delta_pct_1m",
    "liquidation_count",
    "liquidation_notional",
)

OUTCOME_SIDECAR_COLUMNS = (
    "candidate_id",
    "symbol",
    "direction",
    "decision_at",
    "entry_at",
    "entry_price",
    "label_horizon_minutes",
    "mfe_pct",
    "mae_pct",
    "forward_return_pct",
    "label_status",
)


class EventChainError(Exception):
    """Raised when F2 event-chain discovery cannot proceed safely."""


@dataclass(frozen=True)
class LoadedF1Artifacts:
    input_dir: Path
    manifest: dict[str, Any]
    minute_features: pd.DataFrame
    flush_candidates: pd.DataFrame
    labels_sidecar: pd.DataFrame | None
    input_hashes: dict[str, str]


@dataclass(frozen=True)
class EventChainRunResult:
    episode_count: int
    output_dir: Path
    summary: dict[str, Any]


def _as_utc(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _minute_text(value: pd.Timestamp) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    if value is None:
        return False
    return bool(value)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    fieldnames: tuple[str, ...],
) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fieldnames})
    _atomic_write(path, buffer.getvalue())


def _write_json(path: Path, value: object) -> None:
    _atomic_write(
        path,
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
    )


def _public_trade_semantics(minute_features: pd.DataFrame) -> dict[str, object]:
    """Document whether F1 can distinguish missing trades from zero activity."""
    if "trades_present" not in minute_features.columns:
        return {
            "distinguishable": False,
            "reason": "minute_features.csv lacks trades_present",
        }
    missing = minute_features["trades_present"].map(_bool) == False  # noqa: E712
    zero_activity = (
        minute_features["trades_present"].map(_bool)
        & (minute_features["trade_count"].fillna(0) == 0)
        & (minute_features["aggressive_notional"].fillna(0) == 0)
    )
    return {
        "distinguishable": True,
        "missing_trade_rows": int(missing.sum()),
        "zero_activity_rows": int(zero_activity.sum()),
        "policy": (
            "trades_present=False means missing public-trade aggregate; "
            "trades_present=True with zero trade_count/aggressive_notional "
            "means genuine zero activity"
        ),
    }


def load_f1_artifacts(
    input_dir: Path,
    *,
    include_labels: bool = True,
) -> LoadedF1Artifacts:
    input_dir = input_dir.resolve()
    manifest_path = input_dir / "discovery_manifest.json"
    features_path = input_dir / "minute_features.csv"
    candidates_path = input_dir / "flush_candidates.csv"
    for path in (manifest_path, features_path, candidates_path):
        if not path.is_file():
            raise EventChainError(f"missing required F1 artifact: {path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    minute_features = pd.read_csv(features_path)
    flush_candidates = pd.read_csv(candidates_path)
    labels_sidecar = None
    labels_path = input_dir / "labels_sidecar.csv"
    if include_labels and labels_path.is_file():
        labels_sidecar = pd.read_csv(labels_path)

    expected_candidates = int(manifest.get("counts", {}).get("candidate_rows", -1))
    if expected_candidates >= 0 and len(flush_candidates) != expected_candidates:
        raise EventChainError(
            "flush_candidates.csv row count does not match discovery_manifest.json"
        )

    trade_semantics = _public_trade_semantics(minute_features)
    if not trade_semantics["distinguishable"]:
        raise EventChainError(
            "cannot distinguish missing public trades from zero activity; "
            f"{trade_semantics['reason']}"
        )

    hashes = {
        "discovery_manifest.json": _sha256(manifest_path),
        "minute_features.csv": _sha256(features_path),
        "flush_candidates.csv": _sha256(candidates_path),
    }
    if labels_sidecar is not None:
        hashes["labels_sidecar.csv"] = _sha256(labels_path)

    return LoadedF1Artifacts(
        input_dir=input_dir,
        manifest=manifest,
        minute_features=minute_features,
        flush_candidates=flush_candidates,
        labels_sidecar=labels_sidecar,
        input_hashes=hashes,
    )


def _feature_lookup(minute_features: pd.DataFrame) -> dict[tuple[str, str, str], pd.Series]:
    lookup: dict[tuple[str, str, str], pd.Series] = {}
    for _, row in minute_features.iterrows():
        key = (str(row["symbol"]), str(row["minute"]), str(row["direction"]))
        if key in lookup:
            raise EventChainError(f"duplicate minute feature row for {key}")
        lookup[key] = row
    return lookup


def _recovery_confirmed_by(row: pd.Series) -> str:
    branches: list[str] = []
    depth = _number(row.get("directional_depth_change"))
    imbalance = _number(row.get("directional_imbalance_change"))
    net_add = _number(row.get("directional_net_add_change"))
    if depth is not None and depth > 0:
        branches.append("DEPTH")
    if imbalance is not None and imbalance > 0:
        branches.append("IMBALANCE")
    if net_add is not None and net_add > 0:
        branches.append("NET_ADD")
    return "+".join(branches)


def _distance_to_level(direction: str, close: float | None, level: float | None) -> float | None:
    if close is None or level is None:
        return None
    if direction == "LONG":
        return level - close
    return close - level


def _reclaim_met(direction: str, close: float | None, level: float | None) -> bool:
    if close is None or level is None:
        return False
    if direction == "LONG":
        return close >= level
    return close <= level


def _previous_aggressive(row: pd.Series) -> float | None:
    aggressive = _number(row.get("aggressive_notional"))
    change = _number(row.get("aggressive_notional_change"))
    if aggressive is None or change is None:
        return None
    return aggressive - change


def _impact_descriptives(
    impact: float | None, previous_impact: float | None
) -> tuple[float | None, float | None]:
    if impact is None or previous_impact is None:
        return None, None
    delta = impact - previous_impact
    ratio = impact / previous_impact if previous_impact != 0 else None
    return delta, ratio


def _episode_window_end(flush_minute: pd.Timestamp, horizon_minutes: int) -> pd.Timestamp:
    return flush_minute + pd.Timedelta(minutes=horizon_minutes)


def _rows_overlap(
    start_a: pd.Timestamp, end_a: pd.Timestamp, start_b: pd.Timestamp, end_b: pd.Timestamp
) -> bool:
    return start_a <= end_b and start_b <= end_a


def build_episode(
    candidate: Mapping[str, object],
    lookup: Mapping[tuple[str, str, str], pd.Series],
    *,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    symbol = str(candidate["symbol"])
    direction = str(candidate["direction"])
    candidate_id = str(candidate["candidate_id"])
    flush_minute = _as_utc(str(candidate["minute"]))
    flush_key = (symbol, str(candidate["minute"]), direction)
    flush_row = lookup.get(flush_key)
    if flush_row is None:
        raise EventChainError(f"missing flush minute feature row for {flush_key}")
    if not _bool(flush_row.get("directional_flush_observed")):
        raise EventChainError(
            f"candidate {candidate_id} is not a directional flush observation"
        )

    pre_flush_level = _number(flush_row.get("previous_close"))
    episode: dict[str, object] = {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "direction": direction,
        "flush_minute": str(candidate["minute"]),
        "flush_decision_at": str(candidate["decision_at"]),
        "pre_flush_level": pre_flush_level,
        "flush_open": _number(flush_row.get("open")),
        "flush_high": _number(flush_row.get("high")),
        "flush_low": _number(flush_row.get("low")),
        "flush_close": _number(flush_row.get("close")),
        "flush_price_displacement_pct": _number(
            flush_row.get("price_displacement_pct")
        ),
        "flush_oi_delta_pct_1m": _number(flush_row.get("oi_delta_pct_1m")),
        "flush_liquidation_count": int(_number(flush_row.get("liquidation_count")) or 0),
        "flush_liquidation_notional": _number(
            flush_row.get("liquidation_notional")
        ),
        "flush_aggressive_notional": _number(flush_row.get("aggressive_notional")),
        "flush_impact_per_aggressive_notional": _number(
            flush_row.get("impact_per_aggressive_notional")
        ),
        "flush_genuine_support_depth_l50_mean": _number(
            flush_row.get("genuine_support_depth_l50_mean")
        ),
        "flush_genuine_opposing_depth_l50_mean": _number(
            flush_row.get("genuine_opposing_depth_l50_mean")
        ),
        "flush_ob_genuine_seconds": int(
            _number(flush_row.get("ob_genuine_seconds")) or 0
        ),
        "flush_ob_carried_forward_seconds": int(
            _number(flush_row.get("ob_carried_forward_seconds")) or 0
        ),
        "flush_technical_gap": _bool(flush_row.get("technical_gap")),
        "compression_minute": None,
        "compression_decision_at": None,
        "minutes_flush_to_compression": None,
        "compression_aggressive_notional": None,
        "compression_previous_aggressive_notional": None,
        "compression_impact_per_aggressive_notional": None,
        "compression_previous_impact_per_aggressive_notional": None,
        "compression_impact_delta": None,
        "compression_impact_ratio": None,
        "recovery_minute": None,
        "recovery_decision_at": None,
        "minutes_compression_to_recovery": None,
        "recovery_directional_depth_change": None,
        "recovery_directional_imbalance_change": None,
        "recovery_directional_net_add": None,
        "recovery_directional_net_add_change": None,
        "recovery_confirmed_by": None,
        "reclaim_minute": None,
        "reclaim_decision_at": None,
        "reclaim_close": None,
        "minutes_recovery_to_reclaim": None,
        "minutes_flush_to_reclaim": None,
        "min_distance_before_reclaim": None,
        "failed_approach_count": None,
        "stage_reached": STAGE_FLUSH_ONLY,
        "termination_reason": TERMINATION_TIMEOUT,
        "horizon_minutes": horizon_minutes,
        "observed_minutes": 0,
        "overlap_with_other_episodes": False,
        "overlapping_episode_count": 0,
    }

    timeline: list[dict[str, object]] = []
    phase = PHASE_SEARCH_COMPRESSION
    observed = 0
    termination = TERMINATION_TIMEOUT
    stage = STAGE_FLUSH_ONLY

    compression_relative: int | None = None
    recovery_relative: int | None = None

    best_distance: float | None = None
    failed_approaches = 0

    for relative in range(1, horizon_minutes + 1):
        minute = flush_minute + pd.Timedelta(minutes=relative)
        minute_text = _minute_text(minute)
        key = (symbol, minute_text, direction)
        row = lookup.get(key)
        phase_before = phase
        transition_reason = "SCAN"

        if row is None:
            phase = PHASE_ABORTED
            termination = TERMINATION_WINDOW_END
            transition_reason = TERMINATION_WINDOW_END
            timeline.append(
                _timeline_row(
                    candidate_id,
                    symbol,
                    direction,
                    relative,
                    minute_text,
                    None,
                    phase_before,
                    phase,
                    transition_reason,
                    None,
                    pre_flush_level,
                    None,
                )
            )
            break

        observed += 1
        close = _number(row.get("close"))
        distance = _distance_to_level(direction, close, pre_flush_level)

        if _bool(row.get("technical_gap")):
            phase = PHASE_ABORTED
            termination = TERMINATION_TECHNICAL_GAP_ABORT
            transition_reason = TERMINATION_TECHNICAL_GAP_ABORT
            timeline.append(
                _timeline_row(
                    candidate_id,
                    symbol,
                    direction,
                    relative,
                    minute_text,
                    row,
                    phase_before,
                    phase,
                    transition_reason,
                    close,
                    pre_flush_level,
                    distance,
                )
            )
            break

        if not _bool(row.get("candle_present")):
            phase = PHASE_ABORTED
            termination = TERMINATION_TECHNICAL_GAP_ABORT
            transition_reason = "MISSING_CANDLE"
            timeline.append(
                _timeline_row(
                    candidate_id,
                    symbol,
                    direction,
                    relative,
                    minute_text,
                    row,
                    phase_before,
                    phase,
                    transition_reason,
                    close,
                    pre_flush_level,
                    distance,
                )
            )
            break

        if phase == PHASE_SEARCH_COMPRESSION:
            if not _bool(row.get("trades_present")):
                phase = PHASE_ABORTED
                termination = TERMINATION_IMPACT_DATA_ABORT
                transition_reason = TERMINATION_IMPACT_DATA_ABORT
                timeline.append(
                    _timeline_row(
                        candidate_id,
                        symbol,
                        direction,
                        relative,
                        minute_text,
                        row,
                        phase_before,
                        phase,
                        transition_reason,
                        close,
                        pre_flush_level,
                        distance,
                    )
                )
                break

            if _bool(row.get("impact_compression_observed")):
                compression_relative = relative
                impact = _number(row.get("impact_per_aggressive_notional"))
                previous_impact = _number(
                    row.get("previous_impact_per_aggressive_notional")
                )
                delta, ratio = _impact_descriptives(impact, previous_impact)
                episode.update(
                    {
                        "compression_minute": minute_text,
                        "compression_decision_at": str(row.get("decision_at")),
                        "minutes_flush_to_compression": relative,
                        "compression_aggressive_notional": _number(
                            row.get("aggressive_notional")
                        ),
                        "compression_previous_aggressive_notional": _previous_aggressive(
                            row
                        ),
                        "compression_impact_per_aggressive_notional": impact,
                        "compression_previous_impact_per_aggressive_notional": previous_impact,
                        "compression_impact_delta": delta,
                        "compression_impact_ratio": ratio,
                        "stage_reached": STAGE_COMPRESSION_ONLY,
                        "termination_reason": TERMINATION_TIMEOUT,
                    }
                )
                phase = PHASE_SEARCH_RECOVERY
                transition_reason = "COMPRESSION_CONFIRMED"
            elif relative == horizon_minutes:
                phase = PHASE_COMPLETE
                termination = TERMINATION_TIMEOUT
                transition_reason = TERMINATION_TIMEOUT

        elif phase == PHASE_SEARCH_RECOVERY:
            if int(_number(row.get("ob_genuine_seconds")) or 0) <= 0:
                phase = PHASE_ABORTED
                termination = TERMINATION_L2_DATA_ABORT
                transition_reason = TERMINATION_L2_DATA_ABORT
                timeline.append(
                    _timeline_row(
                        candidate_id,
                        symbol,
                        direction,
                        relative,
                        minute_text,
                        row,
                        phase_before,
                        phase,
                        transition_reason,
                        close,
                        pre_flush_level,
                        distance,
                    )
                )
                break

            if compression_relative is not None and relative <= compression_relative:
                transition_reason = "SKIP_SAME_MINUTE_AS_COMPRESSION"
            elif _bool(row.get("l2_recovery_observed")):
                recovery_relative = relative
                episode.update(
                    {
                        "recovery_minute": minute_text,
                        "recovery_decision_at": str(row.get("decision_at")),
                        "minutes_compression_to_recovery": relative
                        - compression_relative,
                        "recovery_directional_depth_change": _number(
                            row.get("directional_depth_change")
                        ),
                        "recovery_directional_imbalance_change": _number(
                            row.get("directional_imbalance_change")
                        ),
                        "recovery_directional_net_add": _number(
                            row.get("directional_net_add")
                        ),
                        "recovery_directional_net_add_change": _number(
                            row.get("directional_net_add_change")
                        ),
                        "recovery_confirmed_by": _recovery_confirmed_by(row),
                        "stage_reached": STAGE_L2_RECOVERY_ONLY,
                        "termination_reason": TERMINATION_TIMEOUT,
                    }
                )
                phase = PHASE_SEARCH_RECLAIM
                transition_reason = "L2_RECOVERY_CONFIRMED"
                best_distance = None
                failed_approaches = 0
            elif relative == horizon_minutes:
                phase = PHASE_COMPLETE
                termination = TERMINATION_TIMEOUT
                transition_reason = TERMINATION_TIMEOUT

        elif phase == PHASE_SEARCH_RECLAIM:
            if recovery_relative is not None and relative <= recovery_relative:
                transition_reason = "SKIP_SAME_MINUTE_AS_RECOVERY"
            elif _reclaim_met(direction, close, pre_flush_level):
                episode.update(
                    {
                        "reclaim_minute": minute_text,
                        "reclaim_decision_at": str(row.get("decision_at")),
                        "reclaim_close": close,
                        "minutes_recovery_to_reclaim": relative - recovery_relative,
                        "minutes_flush_to_reclaim": relative,
                        "min_distance_before_reclaim": best_distance,
                        "failed_approach_count": failed_approaches,
                        "stage_reached": STAGE_PRICE_RECLAIM,
                        "termination_reason": TERMINATION_COMPLETE,
                    }
                )
                phase = PHASE_COMPLETE
                termination = TERMINATION_COMPLETE
                transition_reason = "PRICE_RECLAIM_CONFIRMED"
            else:
                if distance is not None:
                    if best_distance is None or distance < best_distance:
                        if best_distance is not None:
                            failed_approaches += 1
                        best_distance = distance
                if relative == horizon_minutes:
                    episode.update(
                        {
                            "min_distance_before_reclaim": best_distance,
                            "failed_approach_count": failed_approaches,
                            "stage_reached": STAGE_L2_RECOVERY_ONLY,
                            "termination_reason": TERMINATION_TIMEOUT,
                        }
                    )
                    phase = PHASE_COMPLETE
                    termination = TERMINATION_TIMEOUT
                    transition_reason = TERMINATION_TIMEOUT

        timeline.append(
            _timeline_row(
                candidate_id,
                symbol,
                direction,
                relative,
                minute_text,
                row,
                phase_before,
                phase,
                transition_reason,
                close,
                pre_flush_level,
                distance,
            )
        )

        if phase in {PHASE_COMPLETE, PHASE_ABORTED}:
            break

    if phase == PHASE_ABORTED:
        episode["termination_reason"] = termination
    elif phase == PHASE_COMPLETE and termination == TERMINATION_TIMEOUT:
        episode["termination_reason"] = TERMINATION_TIMEOUT
    elif phase == PHASE_COMPLETE and termination == TERMINATION_COMPLETE:
        episode["termination_reason"] = TERMINATION_COMPLETE

    episode["observed_minutes"] = observed
    return episode, timeline


def _timeline_row(
    candidate_id: str,
    symbol: str,
    direction: str,
    relative_minute: int,
    minute: str,
    row: pd.Series | None,
    phase_before: str,
    phase_after: str,
    transition_reason: str,
    close: float | None,
    pre_flush_level: float | None,
    distance: float | None,
) -> dict[str, object]:
    if row is None:
        return {
            "candidate_id": candidate_id,
            "symbol": symbol,
            "direction": direction,
            "relative_minute": relative_minute,
            "minute": minute,
            "decision_at": None,
            "phase_before": phase_before,
            "phase_after": phase_after,
            "transition_reason": transition_reason,
            "close": close,
            "pre_flush_level": pre_flush_level,
            "distance_to_pre_flush_level": distance,
            "impact_compression_observed": None,
            "l2_recovery_observed": None,
            "directional_depth_change": None,
            "directional_imbalance_change": None,
            "directional_net_add_change": None,
            "technical_gap": None,
            "candle_present": None,
            "trades_present": None,
            "oi_state_valid": None,
            "ob_genuine_seconds": None,
            "ob_carried_forward_seconds": None,
            "aggressive_notional": None,
            "impact_per_aggressive_notional": None,
            "previous_impact_per_aggressive_notional": None,
            "oi_delta_pct_1m": None,
            "liquidation_count": None,
            "liquidation_notional": None,
        }
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "direction": direction,
        "relative_minute": relative_minute,
        "minute": minute,
        "decision_at": row.get("decision_at"),
        "phase_before": phase_before,
        "phase_after": phase_after,
        "transition_reason": transition_reason,
        "close": close,
        "pre_flush_level": pre_flush_level,
        "distance_to_pre_flush_level": distance,
        "impact_compression_observed": _bool(row.get("impact_compression_observed")),
        "l2_recovery_observed": _bool(row.get("l2_recovery_observed")),
        "directional_depth_change": _number(row.get("directional_depth_change")),
        "directional_imbalance_change": _number(
            row.get("directional_imbalance_change")
        ),
        "directional_net_add_change": _number(row.get("directional_net_add_change")),
        "technical_gap": _bool(row.get("technical_gap")),
        "candle_present": _bool(row.get("candle_present")),
        "trades_present": _bool(row.get("trades_present")),
        "oi_state_valid": _bool(row.get("oi_state_valid")),
        "ob_genuine_seconds": int(_number(row.get("ob_genuine_seconds")) or 0),
        "ob_carried_forward_seconds": int(
            _number(row.get("ob_carried_forward_seconds")) or 0
        ),
        "aggressive_notional": _number(row.get("aggressive_notional")),
        "impact_per_aggressive_notional": _number(
            row.get("impact_per_aggressive_notional")
        ),
        "previous_impact_per_aggressive_notional": _number(
            row.get("previous_impact_per_aggressive_notional")
        ),
        "oi_delta_pct_1m": _number(row.get("oi_delta_pct_1m")),
        "liquidation_count": int(_number(row.get("liquidation_count")) or 0),
        "liquidation_notional": _number(row.get("liquidation_notional")),
    }


def _quantiles(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"q25": None, "median": None, "q75": None}
    ordered = sorted(values)
    if len(ordered) == 1:
        value = float(ordered[0])
        return {"q25": value, "median": value, "q75": value}
    return {
        "q25": float(statistics.quantiles(ordered, n=4)[0]),
        "median": float(statistics.median(ordered)),
        "q75": float(statistics.quantiles(ordered, n=4)[2]),
    }


def _stage_funnel(episodes: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for direction in DIRECTIONS:
        subset = [ep for ep in episodes if ep["direction"] == direction]
        flushes = len(subset)
        compression = sum(
            1
            for ep in subset
            if ep.get("compression_minute") is not None
            and ep.get("compression_minute") != ""
        )
        recovery = sum(
            1
            for ep in subset
            if ep.get("recovery_minute") is not None and ep.get("recovery_minute") != ""
        )
        reclaim = sum(1 for ep in subset if ep["stage_reached"] == STAGE_PRICE_RECLAIM)
        aborts = sum(
            1
            for ep in subset
            if str(ep["termination_reason"]).endswith("_ABORT")
            or ep["termination_reason"] == TERMINATION_WINDOW_END
        )
        timeouts = sum(
            1 for ep in subset if ep["termination_reason"] == TERMINATION_TIMEOUT
        )
        flush_to_compression = [
            float(ep["minutes_flush_to_compression"])
            for ep in subset
            if ep.get("minutes_flush_to_compression") is not None
        ]
        compression_to_recovery = [
            float(ep["minutes_compression_to_recovery"])
            for ep in subset
            if ep.get("minutes_compression_to_recovery") is not None
        ]
        recovery_to_reclaim = [
            float(ep["minutes_recovery_to_reclaim"])
            for ep in subset
            if ep.get("minutes_recovery_to_reclaim") is not None
        ]
        flush_to_reclaim = [
            float(ep["minutes_flush_to_reclaim"])
            for ep in subset
            if ep.get("minutes_flush_to_reclaim") is not None
        ]
        row = {
            "direction": direction,
            "flushes": flushes,
            "later_compression": compression,
            "later_l2_recovery": recovery,
            "later_price_reclaim": reclaim,
            "aborts": aborts,
            "timeouts": timeouts,
            "compression_rate_of_flushes": compression / flushes if flushes else None,
            "recovery_rate_of_compression": recovery / compression if compression else None,
            "reclaim_rate_of_recovery": reclaim / recovery if recovery else None,
            "reclaim_rate_of_flushes": reclaim / flushes if flushes else None,
            "all_three_rate_of_flushes": reclaim / flushes if flushes else None,
        }
        row.update(
            {
                f"minutes_flush_to_compression_{key}": value
                for key, value in _quantiles(flush_to_compression).items()
            }
        )
        row.update(
            {
                f"minutes_compression_to_recovery_{key}": value
                for key, value in _quantiles(compression_to_recovery).items()
            }
        )
        row.update(
            {
                f"minutes_recovery_to_reclaim_{key}": value
                for key, value in _quantiles(recovery_to_reclaim).items()
            }
        )
        row.update(
            {
                f"minutes_flush_to_reclaim_{key}": value
                for key, value in _quantiles(flush_to_reclaim).items()
            }
        )
        rows.append(row)
    return rows


def _mark_overlaps(episodes: list[dict[str, object]]) -> None:
    windows: list[tuple[int, pd.Timestamp, pd.Timestamp]] = []
    for index, episode in enumerate(episodes):
        start = _as_utc(str(episode["flush_minute"]))
        end = _episode_window_end(start, int(episode["horizon_minutes"]))
        windows.append((index, start, end))
    for index, start, end in windows:
        overlaps = [
            other
            for other, other_start, other_end in windows
            if other != index and _rows_overlap(start, end, other_start, other_end)
        ]
        episodes[index]["overlap_with_other_episodes"] = bool(overlaps)
        episodes[index]["overlapping_episode_count"] = len(overlaps)


def _simultaneous_active_stats(
    episodes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    events: list[tuple[pd.Timestamp, int]] = []
    for episode in episodes:
        flush = _as_utc(str(episode["flush_minute"]))
        start = flush + pd.Timedelta(minutes=1)
        end = flush + pd.Timedelta(minutes=int(episode["horizon_minutes"]))
        events.append((start, 1))
        events.append((end + pd.Timedelta(minutes=1), -1))
    events.sort(key=lambda item: (item[0], -item[1]))
    active = 0
    peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    overlap_count = sum(
        1 for episode in episodes if _bool(episode.get("overlap_with_other_episodes"))
    )
    return {
        "overlapping_episode_count": overlap_count,
        "overlap_rate": overlap_count / len(episodes) if episodes else None,
        "peak_simultaneous_active_episodes": peak,
    }


def _summary(
    episodes: Sequence[Mapping[str, object]],
    funnel: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    termination_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    for episode in episodes:
        reason = str(episode["termination_reason"])
        stage = str(episode["stage_reached"])
        termination_counts[reason] = termination_counts.get(reason, 0) + 1
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    return {
        "episode_count": len(episodes),
        "stage_counts": stage_counts,
        "termination_counts": termination_counts,
        "funnel_by_direction": list(funnel),
        "overlap": _simultaneous_active_stats(episodes),
        "reclaim_event_rate": (
            stage_counts.get(STAGE_PRICE_RECLAIM, 0) / len(episodes) if episodes else None
        ),
        "profitability_claim": False,
    }


def _pick_examples(episodes: Sequence[Mapping[str, object]]) -> dict[str, list[str]]:
    ordered = sorted(episodes, key=lambda ep: str(ep["candidate_id"]))
    buckets: dict[str, list[str]] = {
        "complete_long": [],
        "complete_short": [],
        "flush_only": [],
        "compression_only": [],
        "l2_recovery_only": [],
        "quality_abort": [],
    }
    for episode in ordered:
        stage = str(episode["stage_reached"])
        direction = str(episode["direction"])
        reason = str(episode["termination_reason"])
        candidate_id = str(episode["candidate_id"])
        if stage == STAGE_PRICE_RECLAIM and direction == "LONG":
            buckets["complete_long"].append(candidate_id)
        elif stage == STAGE_PRICE_RECLAIM and direction == "SHORT":
            buckets["complete_short"].append(candidate_id)
        elif stage == STAGE_FLUSH_ONLY:
            buckets["flush_only"].append(candidate_id)
        elif stage == STAGE_COMPRESSION_ONLY:
            buckets["compression_only"].append(candidate_id)
        elif stage == STAGE_L2_RECOVERY_ONLY:
            buckets["l2_recovery_only"].append(candidate_id)
        elif reason.endswith("_ABORT") or reason == TERMINATION_WINDOW_END:
            buckets["quality_abort"].append(candidate_id)
    return {
        "complete_long": buckets["complete_long"][:3],
        "complete_short": buckets["complete_short"][:3],
        "flush_only": buckets["flush_only"][:2],
        "compression_only": buckets["compression_only"][:2],
        "l2_recovery_only": buckets["l2_recovery_only"][:2],
        "quality_abort": buckets["quality_abort"][:2],
    }


def _examples_markdown(
    examples: Mapping[str, Sequence[str]],
    episodes: Mapping[str, Mapping[str, object]],
    timeline_rows: Sequence[Mapping[str, object]],
) -> str:
    by_candidate: dict[str, list[Mapping[str, object]]] = {}
    for row in timeline_rows:
        by_candidate.setdefault(str(row["candidate_id"]), []).append(row)

    lines = [
        "# BTC F2 Event Chain Examples",
        "",
        "Deterministic examples selected by `candidate_id` order.",
        "No selection by MFE, MAE, forward return or profitability.",
        "",
    ]
    section_titles = {
        "complete_long": "Complete LONG chains",
        "complete_short": "Complete SHORT chains",
        "flush_only": "FLUSH_ONLY",
        "compression_only": "COMPRESSION_ONLY",
        "l2_recovery_only": "L2_RECOVERY_ONLY",
        "quality_abort": "Quality aborts",
    }
    for key, title in section_titles.items():
        lines.append(f"## {title}")
        lines.append("")
        ids = list(examples.get(key, ()))
        if not ids:
            lines.append("_No example available in this run._")
            lines.append("")
            continue
        for candidate_id in ids:
            episode = episodes[candidate_id]
            lines.append(f"### {candidate_id}")
            lines.append("")
            lines.append(
                f"- direction: {episode['direction']}\n"
                f"- flush_minute: {episode['flush_minute']}\n"
                f"- stage_reached: {episode['stage_reached']}\n"
                f"- termination_reason: {episode['termination_reason']}\n"
                f"- pre_flush_level: {episode['pre_flush_level']}"
            )
            lines.append("")
            lines.append(
                "| minute | close | distance | oi_delta_pct | liq_count | "
                "aggressive_notional | impact | compression | depth_chg | "
                "imb_chg | net_add_chg | recovery | phase_after | transition |"
            )
            lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- | --- | --- |")
            for row in by_candidate.get(candidate_id, []):
                lines.append(
                    "| {minute} | {close} | {distance} | {oi} | {liq} | {aggr} | "
                    "{impact} | {compression} | {depth} | {imb} | {net} | {recovery} | "
                    "{phase} | {transition} |".format(
                        minute=row.get("minute"),
                        close=row.get("close"),
                        distance=row.get("distance_to_pre_flush_level"),
                        oi=row.get("oi_delta_pct_1m"),
                        liq=row.get("liquidation_count"),
                        aggr=row.get("aggressive_notional"),
                        impact=row.get("impact_per_aggressive_notional"),
                        compression=row.get("impact_compression_observed"),
                        depth=row.get("directional_depth_change"),
                        imb=row.get("directional_imbalance_change"),
                        net=row.get("directional_net_add_change"),
                        recovery=row.get("l2_recovery_observed"),
                        phase=row.get("phase_after"),
                        transition=row.get("transition_reason"),
                    )
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_outcome_sidecar(
    episodes: Sequence[Mapping[str, object]],
    labels: pd.DataFrame | None,
) -> list[dict[str, object]]:
    if labels is None:
        return []
    label_lookup = {
        str(row["candidate_id"]): row for _, row in labels.iterrows()
    }
    rows: list[dict[str, object]] = []
    for episode in episodes:
        candidate_id = str(episode["candidate_id"])
        label = label_lookup.get(candidate_id)
        if label is None:
            raise EventChainError(f"missing label sidecar row for {candidate_id}")
        rows.append({column: label.get(column) for column in OUTCOME_SIDECAR_COLUMNS})
    rows.sort(key=lambda row: str(row["candidate_id"]))
    return rows


def run_event_chain_discovery(
    *,
    input_dir: Path,
    output_dir: Path,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    include_outcomes: bool = True,
) -> EventChainRunResult:
    if horizon_minutes <= 0:
        raise EventChainError("horizon_minutes must be positive")

    loaded = load_f1_artifacts(input_dir, include_labels=include_outcomes)
    lookup = _feature_lookup(loaded.minute_features)
    candidates = loaded.flush_candidates.sort_values(
        ["minute", "direction", "candidate_id"]
    ).to_dict(orient="records")

    episodes: list[dict[str, object]] = []
    timeline_rows: list[dict[str, object]] = []
    for candidate in candidates:
        episode, timeline = build_episode(
            candidate,
            lookup,
            horizon_minutes=horizon_minutes,
        )
        episodes.append(episode)
        timeline_rows.extend(timeline)

    _mark_overlaps(episodes)
    funnel = _stage_funnel(episodes)
    summary = _summary(episodes, funnel)
    examples = _pick_examples(episodes)
    episode_lookup = {str(ep["candidate_id"]): ep for ep in episodes}

    trade_semantics = _public_trade_semantics(loaded.minute_features)
    manifest = {
        "format_version": FORMAT_VERSION,
        "input_dir": str(loaded.input_dir),
        "input_hashes": loaded.input_hashes,
        "f1_format_version": loaded.manifest.get("format_version"),
        "symbol": loaded.manifest.get("symbols"),
        "analysis_horizon_minutes": horizon_minutes,
        "horizon_semantics": (
            "scan strictly after flush minute decision/close; maximum 60 completed "
            "1m minutes; no minute after horizon end"
        ),
        "sequence_policy": [
            "FLUSH minute is state only",
            "IMPACT_COMPRESSION must occur in a later completed minute",
            "L2_RECOVERY must occur at least one minute after compression",
            "PRICE_RECLAIM must occur at least one minute after recovery",
        ],
        "reclaim_definition": {
            "pre_flush_level": "previous_close at flush minute",
            "LONG": "later completed close >= pre_flush_level",
            "SHORT": "later completed close <= pre_flush_level",
            "price_field": "close only; high/low never confirm reclaim",
        },
        "quality_abort_rules": [
            TERMINATION_TECHNICAL_GAP_ABORT,
            TERMINATION_IMPACT_DATA_ABORT,
            TERMINATION_L2_DATA_ABORT,
            TERMINATION_WINDOW_END,
        ],
        "public_trade_semantics": trade_semantics,
        "overlap_policy": "overlapping episodes are retained and marked; no suppression",
        "threshold_search": False,
        "profitability_claim": False,
        "labels_policy": (
            "labels_sidecar.csv may be copied to event_outcomes_sidecar.csv only; "
            "it never influences transitions"
        ),
        "orderbook_contract": loaded.manifest.get("orderbook_contract"),
        "direction_contract": loaded.manifest.get("direction_contract"),
        "l2_recovery_relation": L2_RECOVERY_RELATION,
        "l2_side_by_direction": L2_SIDE_BY_DIRECTION,
        "carried_forward_policy": ORDERBOOK_CARRIED_FORWARD_POLICY,
        "counts": {
            "episodes": len(episodes),
            "timeline_rows": len(timeline_rows),
        },
        "examples": examples,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "event_episodes.csv", episodes, fieldnames=EPISODE_COLUMNS)
    _write_csv(
        output_dir / "event_timeline.csv", timeline_rows, fieldnames=TIMELINE_COLUMNS
    )
    _write_csv(
        output_dir / "stage_funnel.csv",
        funnel,
        fieldnames=tuple(funnel[0].keys()) if funnel else ("direction",),
    )
    _write_json(output_dir / "event_chain_manifest.json", manifest)
    _write_json(output_dir / "event_chain_summary.json", summary)
    examples_md = _examples_markdown(examples, episode_lookup, timeline_rows)
    _atomic_write(output_dir / "event_chain_examples.md", examples_md)

    if include_outcomes and loaded.labels_sidecar is not None:
        sidecar = _build_outcome_sidecar(episodes, loaded.labels_sidecar)
        _write_csv(
            output_dir / "event_outcomes_sidecar.csv",
            sidecar,
            fieldnames=OUTCOME_SIDECAR_COLUMNS,
        )

    return EventChainRunResult(
        episode_count=len(episodes),
        output_dir=output_dir,
        summary=summary,
    )
