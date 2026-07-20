"""Hindsight crash-event finder for Emergency-Lock Phase C.

Event selection is explicitly non-causal research labelling
(``selection_type = hindsight_selected_stress_event``). Strategy simulation
must receive only ``simulation_start_index`` / window length — never the
future low timestamp or drop path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from .config import EmergencyLockRecoveryConfig, validate_phase_c_config

SELECTION_TYPE = "hindsight_selected_stress_event"


def _ts_iso(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


@dataclass
class CrashEventCandidate:
    candidate_id: str
    symbol: str
    timeframe: str
    selection_type: str
    peak_index: int
    peak_timestamp: str | None
    peak_price: float
    low_index: int
    low_timestamp: str | None
    low_price: float
    max_drop_pct: float
    bars_peak_to_low: int
    qualified_10_pct: bool
    qualified_12_5_pct: bool
    qualified_15_pct: bool
    simulation_start_index: int
    simulation_end_index: int
    window_truncated_at_data_end: bool
    kept: bool = True
    event_id: str | None = None
    deduped_into_event_id: str | None = None
    dedupe_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CrashEvent:
    event_id: str
    symbol: str
    timeframe: str
    selection_type: str
    peak_index: int
    peak_timestamp: str | None
    peak_price: float
    low_index: int
    low_timestamp: str | None
    low_price: float
    max_drop_pct: float
    bars_peak_to_low: int
    qualified_10_pct: bool
    qualified_12_5_pct: bool
    qualified_15_pct: bool
    simulation_start_index: int
    simulation_end_index: int
    window_truncated_at_data_end: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventFinderResult:
    raw_candidates: list[CrashEventCandidate] = field(default_factory=list)
    events: list[CrashEvent] = field(default_factory=list)
    dedupe_rows: list[dict[str, Any]] = field(default_factory=list)


def _peak_price(candle: dict[str, Any], source: str) -> float:
    if source == "high":
        return float(candle["high"])
    raise ValueError(f"unsupported peak source: {source}")


def _drop_price(candle: dict[str, Any], source: str) -> float:
    if source == "low":
        return float(candle["low"])
    raise ValueError(f"unsupported drop source: {source}")


def _is_local_peak(
    highs: Sequence[float],
    index: int,
    lookback: int,
) -> bool:
    """Peak if high equals the max over ``[index-lookback, index]`` (inclusive)."""
    left = max(0, index - int(lookback))
    window = highs[left : index + 1]
    return float(highs[index]) >= max(window) - 1e-15


def drop_bucket(max_drop_pct: float) -> str:
    """Assign crash-depth bucket labels used in Phase C aggregates."""
    d = float(max_drop_pct)
    if d >= 0.15:
        return ">=15%"
    if d >= 0.125:
        return "12.5–15%"
    if d >= 0.10:
        return "10–12.5%"
    return "<10%"


def find_raw_crash_candidates(
    candles: Sequence[dict[str, Any]],
    cfg: EmergencyLockRecoveryConfig,
) -> list[CrashEventCandidate]:
    """Scan local peaks and measure forward drawdowns (hindsight labelling)."""
    validate_phase_c_config(cfg)
    n = len(candles)
    if n < 2:
        return []

    highs = [_peak_price(c, cfg.event_peak_source) for c in candles]
    lows = [_drop_price(c, cfg.event_drop_source) for c in candles]
    min_threshold = min(float(t) for t in cfg.event_drop_thresholds)
    candidates: list[CrashEventCandidate] = []
    cand_seq = 0

    for peak_i in range(n - 1):
        if not _is_local_peak(highs, peak_i, cfg.event_peak_lookback_bars):
            continue
        peak_price = highs[peak_i]
        if peak_price <= 0.0:
            continue
        search_end = min(peak_i + int(cfg.event_max_drop_bars), n - 1)
        if search_end <= peak_i:
            continue
        # Subsequent low strictly after the peak bar.
        low_i = peak_i + 1
        low_price = lows[low_i]
        for j in range(peak_i + 1, search_end + 1):
            if lows[j] < low_price:
                low_price = lows[j]
                low_i = j
        drop_pct = (peak_price - low_price) / peak_price
        if drop_pct + 1e-15 < min_threshold:
            continue

        start_i = peak_i + int(cfg.event_entry_offset_bars)
        if start_i >= n:
            continue
        desired_end = low_i + int(cfg.event_post_low_bars)
        end_i = min(desired_end, n - 1)
        truncated = desired_end > (n - 1)

        cand_seq += 1
        candidates.append(
            CrashEventCandidate(
                candidate_id=f"cand_{cand_seq:05d}",
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
                selection_type=SELECTION_TYPE,
                peak_index=peak_i,
                peak_timestamp=_ts_iso(candles[peak_i]["timestamp"]),
                peak_price=float(peak_price),
                low_index=low_i,
                low_timestamp=_ts_iso(candles[low_i]["timestamp"]),
                low_price=float(low_price),
                max_drop_pct=float(drop_pct),
                bars_peak_to_low=int(low_i - peak_i),
                qualified_10_pct=drop_pct + 1e-15 >= 0.10,
                qualified_12_5_pct=drop_pct + 1e-15 >= 0.125,
                qualified_15_pct=drop_pct + 1e-15 >= 0.15,
                simulation_start_index=start_i,
                simulation_end_index=end_i,
                window_truncated_at_data_end=truncated,
            )
        )
    return candidates


def _windows_overlap(a: CrashEventCandidate, b: CrashEventCandidate) -> bool:
    return not (
        a.simulation_end_index < b.peak_index or b.simulation_end_index < a.peak_index
    )


def _shared_drawdown_leg(a: CrashEventCandidate, b: CrashEventCandidate) -> bool:
    """True when candidates describe the same selloff leg."""
    if a.low_index == b.low_index:
        return True
    # Overlapping peak→low intervals with nearby lows.
    a0, a1 = a.peak_index, a.low_index
    b0, b1 = b.peak_index, b.low_index
    intervals_overlap = not (a1 < b0 or b1 < a0)
    if intervals_overlap and abs(a.low_index - b.low_index) <= 48:
        return True
    if _windows_overlap(a, b) and abs(a.low_price - b.low_price) / max(
        a.low_price, b.low_price, 1e-12
    ) <= 0.01:
        return True
    return False


def _rank_key(c: CrashEventCandidate) -> tuple[float, float, int]:
    # Prefer higher peak, larger drawdown, earlier peak.
    return (-float(c.peak_price), -float(c.max_drop_pct), int(c.peak_index))


def dedupe_crash_candidates(
    candidates: Sequence[CrashEventCandidate],
    cfg: EmergencyLockRecoveryConfig,
) -> EventFinderResult:
    """Greedy dedupe on shared drawdown legs with cooldown / separation."""
    ordered = sorted(candidates, key=_rank_key)
    kept: list[CrashEventCandidate] = []
    dedupe_rows: list[dict[str, Any]] = []
    event_seq = 0

    for cand in ordered:
        conflict: CrashEventCandidate | None = None
        reason = None
        for k in kept:
            if abs(cand.peak_index - k.peak_index) < int(cfg.event_min_separation_bars):
                conflict = k
                reason = "min_separation_bars"
                break
            if abs(cand.peak_index - k.peak_index) < int(cfg.event_cooldown_bars) and (
                _shared_drawdown_leg(cand, k) or _windows_overlap(cand, k)
            ):
                conflict = k
                reason = "cooldown_shared_or_overlap"
                break
            if _shared_drawdown_leg(cand, k):
                conflict = k
                reason = "shared_drawdown_leg"
                break
        if conflict is not None:
            cand.kept = False
            cand.deduped_into_event_id = conflict.event_id
            cand.dedupe_reason = reason
            dedupe_rows.append(
                {
                    "candidate_id": cand.candidate_id,
                    "kept": False,
                    "deduped_into_event_id": conflict.event_id,
                    "dedupe_reason": reason,
                    "peak_index": cand.peak_index,
                    "low_index": cand.low_index,
                    "max_drop_pct": cand.max_drop_pct,
                    "peak_price": cand.peak_price,
                }
            )
            continue

        event_seq += 1
        event_id = f"evt_{event_seq:04d}"
        cand.kept = True
        cand.event_id = event_id
        kept.append(cand)
        dedupe_rows.append(
            {
                "candidate_id": cand.candidate_id,
                "kept": True,
                "deduped_into_event_id": None,
                "dedupe_reason": None,
                "event_id": event_id,
                "peak_index": cand.peak_index,
                "low_index": cand.low_index,
                "max_drop_pct": cand.max_drop_pct,
                "peak_price": cand.peak_price,
            }
        )

    # Stable chronological order for simulation.
    kept_sorted = sorted(kept, key=lambda c: (c.peak_index, c.low_index))
    events = [
        CrashEvent(
            event_id=str(c.event_id),
            symbol=c.symbol,
            timeframe=c.timeframe,
            selection_type=c.selection_type,
            peak_index=c.peak_index,
            peak_timestamp=c.peak_timestamp,
            peak_price=c.peak_price,
            low_index=c.low_index,
            low_timestamp=c.low_timestamp,
            low_price=c.low_price,
            max_drop_pct=c.max_drop_pct,
            bars_peak_to_low=c.bars_peak_to_low,
            qualified_10_pct=c.qualified_10_pct,
            qualified_12_5_pct=c.qualified_12_5_pct,
            qualified_15_pct=c.qualified_15_pct,
            simulation_start_index=c.simulation_start_index,
            simulation_end_index=c.simulation_end_index,
            window_truncated_at_data_end=c.window_truncated_at_data_end,
        )
        for c in kept_sorted
    ]
    return EventFinderResult(
        raw_candidates=list(candidates),
        events=events,
        dedupe_rows=dedupe_rows,
    )


def find_crash_events(
    candles: Sequence[dict[str, Any]],
    cfg: EmergencyLockRecoveryConfig,
) -> EventFinderResult:
    raw = find_raw_crash_candidates(candles, cfg)
    return dedupe_crash_candidates(raw, cfg)
