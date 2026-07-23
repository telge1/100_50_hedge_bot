"""TEM in-trade structure-break telemetry (research-only).

Monitors an already-opened TEM long after entry. Emits scanner telemetry only —
no bot, order, freeze, lock, or exit side effects.

v2: multiple independent break episodes after reclaim (STRUCTURE_AT_RISK),
armed from frozen entry floors and last reclaim level — not only live PL / BOS edges.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.market_structure_c3_4b import (
    RESEARCH_MATRIX,
    ProtectedStructureConfig,
    apply_protected_structure,
)
from research.regime_scanner.market_structure_c3_4d_ema_context import (
    attach_structure_ema_relation,
    compute_c3_4d_ema_context,
    guard_decision,
)
from research.regime_scanner.pullback_entry_c3_5 import (
    asof_htf_context,
    attach_structure_edges,
    enrich_indicators,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    aggregate_complete_from_5m,
)
from research.regime_scanner.timeframes import aggregate_candles

SIGNAL_VERSION = "tem_structure_break_v2_multi_episode"


class ScannerState(str, Enum):
    ENTRY_EVALUATED = "ENTRY_EVALUATED"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    ENTRY_ALLOWED = "ENTRY_ALLOWED"
    ENTRY_WEAK_ALLOW = "ENTRY_WEAK_ALLOW"
    STRUCTURE_INTACT = "STRUCTURE_INTACT"
    STRUCTURE_WARNING = "STRUCTURE_WARNING"
    STRUCTURE_AT_RISK = "STRUCTURE_AT_RISK"
    BREAK_PENDING = "BREAK_PENDING"
    BREAK_CONFIRMED = "BREAK_CONFIRMED"
    RECLAIM_PENDING = "RECLAIM_PENDING"
    RECLAIMED = "RECLAIMED"
    LONG_THESIS_INVALIDATED = "LONG_THESIS_INVALIDATED"


@dataclass
class EntryDecision:
    decision: str  # ALLOW | WEAK_ALLOW | BLOCK
    reasons: list[str]
    major_5m: int
    ema_regime: int
    m30_major: int
    h4_major: int
    g1_long: str
    protected_low_5m: float | None
    protected_high_5m: float | None
    protected_low_1h: float | None
    protected_low_4h: float | None
    ema_stack: str
    price_vs_ema200_pct: float | None


@dataclass
class FrozenLevels:
    entry_bar: int
    entry_timestamp: str
    entry_price: float
    side: str
    protected_low_5m: float | None
    protected_high_5m: float | None
    protected_low_1h: float | None
    protected_low_4h: float | None
    major_5m_at_entry: int
    h4_major_at_entry: int


@dataclass
class MonitorRuntime:
    state: ScannerState = ScannerState.ENTRY_EVALUATED
    frozen: FrozenLevels | None = None
    decision: EntryDecision | None = None
    warning_ts: str | None = None
    warning_bar: int | None = None
    warning_kind: str | None = None
    first_1h_break_ts: str | None = None
    first_1h_break_bar: int | None = None
    first_4h_break_ts: str | None = None
    first_4h_break_bar: int | None = None
    first_5m_frozen_break_ts: str | None = None
    first_5m_frozen_break_bar: int | None = None
    break_pending_ts: str | None = None
    break_confirmed_ts: str | None = None
    reclaim_deadline_4h_open: pd.Timestamp | None = None
    reclaim_ts: str | None = None
    invalidated_ts: str | None = None
    broken_level: float | None = None
    break_timeframe: str | None = None
    break_kind: str | None = None
    # Multi-episode state (v2)
    break_cycle_id: int = 0
    ever_broken: bool = False
    active_break_level: float | None = None
    last_reclaim_level: float | None = None
    major_lost_emitted: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)


def candles_to_frame(candles: Sequence[Any]) -> pd.DataFrame:
    rows = [
        {
            "timestamp": getattr(c, "timestamp", c["timestamp"] if isinstance(c, dict) else None),
            "open": float(getattr(c, "open", c["open"] if isinstance(c, dict) else 0)),
            "high": float(getattr(c, "high", c["high"] if isinstance(c, dict) else 0)),
            "low": float(getattr(c, "low", c["low"] if isinstance(c, dict) else 0)),
            "close": float(getattr(c, "close", c["close"] if isinstance(c, dict) else 0)),
            "volume": float(
                getattr(c, "volume", 0.0)
                if not isinstance(c, dict)
                else (c.get("volume") or 0.0)
            ),
        }
        for c in candles
    ]
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.reset_index(drop=True)


def _finite(x: Any) -> float | None:
    try:
        if x is None or (isinstance(x, float) and not np.isfinite(x)):
            return None
        v = float(x)
        if not np.isfinite(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _ema_stack(row: Mapping[str, Any]) -> str:
    vals = [_finite(row.get(k)) for k in ("ema_9", "ema_20", "ema_59", "ema_200")]
    if any(v is None or v <= 0 for v in vals):
        return "unknown"
    e9, e20, e59, e200 = vals  # type: ignore[misc]
    if e9 > e20 > e59 > e200:
        return "bullish_aligned"
    if e9 < e20 < e59 < e200:
        return "bearish_aligned"
    return "mixed"


def build_5m_trace(frame_5m: pd.DataFrame) -> pd.DataFrame:
    decision = pd.to_datetime(frame_5m["timestamp"], utc=True).iloc[-1] + pd.Timedelta(minutes=5)
    htf30 = aggregate_candles(frame_5m, "30m", decision)
    htf4h = aggregate_complete_from_5m(frame_5m, "4h", decision_time=decision)
    trace = prepare_research_frame(frame_5m, ohlcv_30m=htf30)
    if htf4h is not None and not htf4h.empty:
        f4 = attach_structure_edges(enrich_indicators(htf4h))
        trace = asof_htf_context(trace, f4, tf_minutes=240, prefix="h4")
    ema = compute_c3_4d_ema_context(frame_5m)
    for col in ema.columns:
        if col not in trace.columns and col != "timestamp" and len(ema) == len(trace):
            trace[col] = ema[col].to_numpy()
    trace = attach_structure_ema_relation(trace, ema if len(ema) == len(trace) else None)
    feat = enrich_indicators(frame_5m)
    cfg = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    struct = apply_protected_structure(feat, cfg)
    for col in (
        "protected_low",
        "protected_high",
        "major_direction",
        "protected_structure_state",
        "close_break_protected_down",
        "close_break_protected_up",
        "arm_edge_external_bear",
        "arm_edge_external_bull",
        "external_bos_down",
        "external_bos_up",
    ):
        if col in struct.columns:
            trace[col] = struct[col].to_numpy()
    if "decision_time" not in trace.columns:
        trace["decision_time"] = pd.to_datetime(trace["timestamp"], utc=True) + pd.Timedelta(
            minutes=5
        )
    return trace.reset_index(drop=True)


def build_htf_structure_frame(frame_5m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    decision = pd.to_datetime(frame_5m["timestamp"], utc=True).iloc[-1] + pd.Timedelta(minutes=5)
    htf = aggregate_complete_from_5m(frame_5m, timeframe, decision_time=decision)
    if htf.empty:
        return htf
    feat = enrich_indicators(htf)
    cfg = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    struct = apply_protected_structure(feat, cfg)
    minutes = {"1h": 60, "4h": 240}[timeframe]
    struct["htf_close_decision"] = pd.to_datetime(struct["timestamp"], utc=True) + pd.Timedelta(
        minutes=minutes
    )
    struct["timeframe"] = timeframe
    return struct.reset_index(drop=True)


def _htf_close_decisions_ns(htf: pd.DataFrame) -> np.ndarray:
    close_dec = pd.to_datetime(htf["htf_close_decision"], utc=True)
    return close_dec.astype("int64").to_numpy()


def lookup_last_closed_htf(
    htf: pd.DataFrame,
    *,
    decision_time: pd.Timestamp,
    close_dec_ns: np.ndarray | None = None,
) -> dict[str, Any] | None:
    if htf is None or htf.empty or "htf_close_decision" not in htf.columns:
        return None
    td = pd.Timestamp(decision_time)
    if td.tzinfo is None:
        td = td.tz_localize("UTC")
    else:
        td = td.tz_convert("UTC")
    arr = close_dec_ns if close_dec_ns is not None else _htf_close_decisions_ns(htf)
    if arr.size == 0:
        return None
    idx = int(np.searchsorted(arr, np.int64(td.value), side="right") - 1)
    if idx < 0:
        return None
    row = htf.iloc[idx]
    return {
        "index": idx,
        "timestamp": str(row["timestamp"]),
        "close_decision": str(row["htf_close_decision"]),
        "close": _finite(row.get("close")),
        "protected_low": _finite(row.get("protected_low")),
        "protected_high": _finite(row.get("protected_high")),
        "major_direction": int(_finite(row.get("major_direction")) or 0),
        "close_break_protected_down": bool(row.get("close_break_protected_down")),
        "external_bos_down": bool(row.get("external_bos_down") or row.get("arm_edge_external_bear")),
    }


def decide_entry_long(row_5m: Mapping[str, Any], h1: dict | None, h4: dict | None) -> EntryDecision:
    maj = int(_finite(row_5m.get("major_direction")) or 0)
    ema_r = int(_finite(row_5m.get("ema_regime_direction")) or 0)
    m30 = int(_finite(row_5m.get("m30_major_direction")) or 0)
    h4m = int((h4 or {}).get("major_direction") or _finite(row_5m.get("h4_major_direction")) or 0)
    g1 = guard_decision("long", maj, ema_r, "G1")
    reasons: list[str] = []
    if g1 == "block":
        reasons.append(f"G1_block major={maj}")
        decision = "BLOCK"
    elif maj > 0 and ema_r > 0 and h4m > 0 and m30 >= 0:
        reasons.append("1h/5m/4h aligned bullish")
        decision = "ALLOW"
    elif maj > 0 and ema_r > 0 and h4m < 0:
        reasons.append("LTF/EMA bullish recovery inside bearish 4h major")
        decision = "WEAK_ALLOW"
    elif maj > 0 and ema_r > 0:
        reasons.append("LTF/EMA bullish; 4h not confirmed bullish")
        decision = "WEAK_ALLOW"
    elif maj <= 0:
        reasons.append(f"5m major not bullish ({maj})")
        decision = "BLOCK"
    else:
        reasons.append("mixed context")
        decision = "WEAK_ALLOW"
    return EntryDecision(
        decision=decision,
        reasons=reasons,
        major_5m=maj,
        ema_regime=ema_r,
        m30_major=m30,
        h4_major=h4m,
        g1_long=g1,
        protected_low_5m=_finite(row_5m.get("protected_low")),
        protected_high_5m=_finite(row_5m.get("protected_high")),
        protected_low_1h=(h1 or {}).get("protected_low"),
        protected_low_4h=(h4 or {}).get("protected_low"),
        ema_stack=_ema_stack(row_5m),
        price_vs_ema200_pct=_finite(row_5m.get("close_vs_ema_200_pct")),
    )


def _emit(rt: MonitorRuntime, event: str, **payload: Any) -> None:
    row = {"event": event, "state": rt.state.value, "break_cycle_id": rt.break_cycle_id, **payload}
    rt.events.append(row)


def _evaluate_4h_break(
    rt: MonitorRuntime,
    *,
    h4: dict[str, Any],
) -> tuple[bool, float | None, str]:
    """Return (is_break, level, kind) for a newly closed 4h bar.

    Priority (first match wins for kind/level; any match arms):
    1. Live structure edge / live protected-low close-break
    2. Re-break of last reclaim level (new episode after reclaim)
    3. Close below frozen entry 4h / 1h floors (structural invalidation refs)
    """
    assert rt.frozen is not None
    close = h4.get("close")
    if close is None:
        return False, None, ""

    live_pl = h4.get("protected_low")
    bos = bool(h4.get("external_bos_down") or h4.get("close_break_protected_down"))
    if live_pl is not None and float(close) < float(live_pl):
        return True, float(live_pl), "protected_low_4h_close_break"
    if bos:
        level = live_pl if live_pl is not None else float(close)
        return True, float(level), "external_bearish_bos"

    # Independent episode refs — survive live PL NaN after major flip
    if rt.last_reclaim_level is not None and float(close) < float(rt.last_reclaim_level):
        return True, float(rt.last_reclaim_level), "rebreak_last_reclaim_level"

    frozen_4h = rt.frozen.protected_low_4h
    if frozen_4h is not None and float(close) < float(frozen_4h):
        return True, float(frozen_4h), "frozen_entry_protected_low_4h"

    frozen_1h = rt.frozen.protected_low_1h
    if frozen_1h is not None and float(close) < float(frozen_1h):
        return True, float(frozen_1h), "frozen_entry_protected_low_1h"

    return False, None, ""


def step_monitor(
    rt: MonitorRuntime,
    *,
    bar_i: int,
    row_5m: Mapping[str, Any],
    h1: dict | None,
    h4: dict | None,
    prev_h4_idx: int | None,
) -> int | None:
    """Advance one closed 5m bar. Returns updated last seen 4h index."""
    assert rt.frozen is not None
    ts = str(row_5m.get("timestamp"))
    close = float(row_5m["close"])
    decision_time = pd.Timestamp(row_5m.get("decision_time") or ts)
    if decision_time.tzinfo is None:
        decision_time = decision_time.tz_localize("UTC")

    frozen_pl = rt.frozen.protected_low_5m
    h1_pl = rt.frozen.protected_low_1h

    brk_5m = frozen_pl is not None and close < float(frozen_pl)
    live_h1_pl = (h1 or {}).get("protected_low")
    level_1h = h1_pl if h1_pl is not None else live_h1_pl
    brk_1h = False
    if h1 is not None and level_1h is not None and h1.get("close") is not None:
        brk_1h = float(h1["close"]) < float(level_1h)

    new_h4_idx = (h4 or {}).get("index") if h4 else None
    h4_just_closed = new_h4_idx is not None and new_h4_idx != prev_h4_idx
    brk_4h = False
    brk_4h_level: float | None = None
    brk_4h_kind = ""
    if h4_just_closed and h4 is not None and rt.invalidated_ts is None:
        brk_4h, brk_4h_level, brk_4h_kind = _evaluate_4h_break(rt, h4=h4)

    maj = int(_finite(row_5m.get("major_direction")) or 0)
    major_lost = maj < 0 and rt.frozen.major_5m_at_entry > 0

    # Soft state normalization between episodes
    if rt.state == ScannerState.RECLAIMED:
        rt.state = ScannerState.STRUCTURE_AT_RISK
        _emit(
            rt,
            "STRUCTURE_AT_RISK",
            bar=bar_i,
            timestamp=ts,
            signal_available_ts=ts,
            reason="post_reclaim_thesis_not_fully_restored",
            last_reclaim_level=rt.last_reclaim_level,
        )
    elif rt.state in {
        ScannerState.ENTRY_ALLOWED,
        ScannerState.ENTRY_WEAK_ALLOW,
    }:
        rt.state = ScannerState.STRUCTURE_INTACT

    if brk_5m and rt.first_5m_frozen_break_bar is None:
        rt.first_5m_frozen_break_bar = bar_i
        rt.first_5m_frozen_break_ts = ts
        if rt.warning_bar is None:
            rt.warning_bar = bar_i
            rt.warning_ts = ts
            rt.warning_kind = "entry_protected_low_5m_close_break"
            if rt.state in {
                ScannerState.STRUCTURE_INTACT,
                ScannerState.STRUCTURE_AT_RISK,
                ScannerState.ENTRY_ALLOWED,
                ScannerState.ENTRY_WEAK_ALLOW,
            }:
                rt.state = ScannerState.STRUCTURE_WARNING
            _emit(
                rt,
                "STRUCTURE_WARNING",
                bar=bar_i,
                timestamp=ts,
                signal_available_ts=ts,
                level=frozen_pl,
                kind=rt.warning_kind,
                timeframe="5m",
            )

    if brk_1h and rt.first_1h_break_bar is None and h1 is not None:
        avail = str(h1.get("close_decision") or ts)
        rt.first_1h_break_bar = bar_i
        rt.first_1h_break_ts = avail
        if rt.warning_bar is None:
            rt.warning_bar = bar_i
            rt.warning_ts = avail
            rt.warning_kind = "protected_low_1h_close_break"
        if rt.state in {
            ScannerState.STRUCTURE_INTACT,
            ScannerState.STRUCTURE_AT_RISK,
            ScannerState.ENTRY_ALLOWED,
            ScannerState.ENTRY_WEAK_ALLOW,
        }:
            rt.state = ScannerState.STRUCTURE_WARNING
        _emit(
            rt,
            "BREAK_1H",
            bar=bar_i,
            timestamp=ts,
            signal_available_ts=avail,
            level=level_1h,
            kind="protected_low_1h_close_break",
            timeframe="1h",
        )

    # Arm a new independent 4h break episode
    can_arm = rt.state in {
        ScannerState.STRUCTURE_INTACT,
        ScannerState.STRUCTURE_WARNING,
        ScannerState.STRUCTURE_AT_RISK,
        ScannerState.RECLAIMED,
        ScannerState.ENTRY_ALLOWED,
        ScannerState.ENTRY_WEAK_ALLOW,
    }
    if brk_4h and h4 is not None and rt.invalidated_ts is None and can_arm:
        avail = str(h4.get("close_decision") or ts)
        rt.break_cycle_id += 1
        rt.ever_broken = True
        if rt.first_4h_break_bar is None:
            rt.first_4h_break_bar = bar_i
            rt.first_4h_break_ts = avail
        rt.break_pending_ts = avail
        rt.active_break_level = brk_4h_level
        rt.broken_level = brk_4h_level
        rt.break_timeframe = "4h"
        rt.break_kind = brk_4h_kind
        rt.state = ScannerState.BREAK_PENDING
        rt.reclaim_deadline_4h_open = pd.Timestamp(h4["close_decision"])
        _emit(
            rt,
            "BREAK_PENDING_4H",
            bar=bar_i,
            timestamp=ts,
            signal_available_ts=avail,
            level=rt.active_break_level,
            kind=rt.break_kind,
            timeframe="4h",
            first_break=int(rt.break_cycle_id == 1),
        )

    # Resolve pending episode on subsequent 4h closes
    if (
        rt.state == ScannerState.BREAK_PENDING
        and h4_just_closed
        and h4 is not None
        and rt.reclaim_deadline_4h_open is not None
    ):
        close_dec = pd.Timestamp(h4["close_decision"])
        if close_dec > pd.Timestamp(rt.reclaim_deadline_4h_open):
            level = rt.active_break_level
            c = h4.get("close")
            reclaimed = level is not None and c is not None and float(c) >= float(level)
            if reclaimed:
                rt.state = ScannerState.RECLAIMED
                rt.reclaim_ts = str(h4.get("close_decision"))
                rt.last_reclaim_level = float(level) if level is not None else rt.last_reclaim_level
                rt.active_break_level = None
                rt.reclaim_deadline_4h_open = None
                _emit(
                    rt,
                    "RECLAIMED",
                    bar=bar_i,
                    timestamp=ts,
                    signal_available_ts=rt.reclaim_ts,
                    level=level,
                    timeframe="4h",
                    note="does_not_restore_full_long_thesis",
                )
            else:
                rt.state = ScannerState.BREAK_CONFIRMED
                rt.break_confirmed_ts = str(h4.get("close_decision"))
                rt.state = ScannerState.LONG_THESIS_INVALIDATED
                rt.invalidated_ts = rt.break_confirmed_ts
                _emit(
                    rt,
                    "LONG_THESIS_INVALIDATED",
                    bar=bar_i,
                    timestamp=ts,
                    signal_available_ts=rt.invalidated_ts,
                    confirmation_ts=rt.break_confirmed_ts,
                    level=level,
                    kind="4h_break_reclaim_failure",
                    timeframe="4h",
                )

    if (
        major_lost
        and not rt.major_lost_emitted
        and rt.state
        in {
            ScannerState.STRUCTURE_WARNING,
            ScannerState.STRUCTURE_AT_RISK,
            ScannerState.BREAK_PENDING,
            ScannerState.STRUCTURE_INTACT,
        }
        and rt.invalidated_ts is None
    ):
        rt.major_lost_emitted = True
        _emit(
            rt,
            "MAJOR_ALIGNMENT_LOST_5M",
            bar=bar_i,
            timestamp=ts,
            signal_available_ts=ts,
            major_direction=maj,
        )

    snap = {
        "bar": bar_i,
        "timestamp": ts,
        "decision_time": str(decision_time),
        "state": rt.state.value,
        "break_cycle_id": rt.break_cycle_id,
        "close": close,
        "major_5m": maj,
        "protected_low_5m_live": _finite(row_5m.get("protected_low")),
        "frozen_protected_low_5m": frozen_pl,
        "brk_5m_frozen": int(brk_5m),
        "h1_close": None if h1 is None else h1.get("close"),
        "h1_protected_low": None if h1 is None else h1.get("protected_low"),
        "h1_major": None if h1 is None else h1.get("major_direction"),
        "brk_1h": int(brk_1h),
        "h4_close": None if h4 is None else h4.get("close"),
        "h4_protected_low": None if h4 is None else h4.get("protected_low"),
        "h4_major": None if h4 is None else h4.get("major_direction"),
        "brk_4h": int(brk_4h),
        "brk_4h_kind": brk_4h_kind or None,
        "active_break_level": rt.active_break_level,
        "last_reclaim_level": rt.last_reclaim_level,
        "h4_just_closed": int(bool(h4_just_closed)),
    }
    rt.timeline.append(snap)
    return int(new_h4_idx) if new_h4_idx is not None else prev_h4_idx


def run_in_trade_monitor(
    *,
    frame_5m: pd.DataFrame,
    entry_bar: int,
    entry_price: float,
    side: str = "long",
    end_bar: int | None = None,
    trace: pd.DataFrame | None = None,
    h1_frame: pd.DataFrame | None = None,
    h4_frame: pd.DataFrame | None = None,
) -> MonitorRuntime:
    """Run frozen v2 monitor. Optional prebuilt frames are for eval speed only — identical logic."""
    if side != "long":
        raise ValueError("v1 supports long primary only")
    if trace is None:
        trace = build_5m_trace(frame_5m)
    if h1_frame is None:
        h1_frame = build_htf_structure_frame(frame_5m, "1h")
    if h4_frame is None:
        h4_frame = build_htf_structure_frame(frame_5m, "4h")
    if entry_bar < 0 or entry_bar >= len(trace):
        raise IndexError(f"entry_bar {entry_bar} out of range n={len(trace)}")

    rt = MonitorRuntime()
    row0 = trace.iloc[entry_bar]
    dec_t = pd.Timestamp(row0["decision_time"])
    h1_ns = _htf_close_decisions_ns(h1_frame) if not h1_frame.empty else np.array([], dtype=np.int64)
    h4_ns = _htf_close_decisions_ns(h4_frame) if not h4_frame.empty else np.array([], dtype=np.int64)
    h1_e = lookup_last_closed_htf(h1_frame, decision_time=dec_t, close_dec_ns=h1_ns)
    h4_e = lookup_last_closed_htf(h4_frame, decision_time=dec_t, close_dec_ns=h4_ns)
    decision = decide_entry_long(row0, h1_e, h4_e)
    rt.decision = decision
    rt.frozen = FrozenLevels(
        entry_bar=entry_bar,
        entry_timestamp=str(row0["timestamp"]),
        entry_price=float(entry_price),
        side=side,
        protected_low_5m=decision.protected_low_5m,
        protected_high_5m=decision.protected_high_5m,
        protected_low_1h=decision.protected_low_1h,
        protected_low_4h=decision.protected_low_4h,
        major_5m_at_entry=decision.major_5m,
        h4_major_at_entry=decision.h4_major,
    )
    _emit(
        rt,
        "ENTRY_EVALUATED",
        bar=entry_bar,
        timestamp=str(row0["timestamp"]),
        decision=decision.decision,
        reasons=list(decision.reasons),
        frozen=asdict(rt.frozen),
    )
    if decision.decision == "BLOCK":
        rt.state = ScannerState.ENTRY_BLOCKED
    elif decision.decision == "WEAK_ALLOW":
        rt.state = ScannerState.ENTRY_WEAK_ALLOW
    else:
        rt.state = ScannerState.ENTRY_ALLOWED
    _emit(rt, rt.state.value, bar=entry_bar, timestamp=str(row0["timestamp"]))

    last = (len(trace) - 1) if end_bar is None else min(int(end_bar), len(trace) - 1)
    prev_h4 = h4_e["index"] if h4_e else None
    for bar_i in range(entry_bar + 1, last + 1):
        row = trace.iloc[bar_i]
        dt = pd.Timestamp(row["decision_time"])
        h1 = lookup_last_closed_htf(h1_frame, decision_time=dt, close_dec_ns=h1_ns)
        h4 = lookup_last_closed_htf(h4_frame, decision_time=dt, close_dec_ns=h4_ns)
        prev_h4 = step_monitor(rt, bar_i=bar_i, row_5m=row, h1=h1, h4=h4, prev_h4_idx=prev_h4)
    return rt


def find_bar_by_timestamp(frame_5m: pd.DataFrame, ts: str) -> int:
    target = pd.Timestamp(ts)
    if target.tzinfo is None:
        target = target.tz_localize("UTC")
    else:
        target = target.tz_convert("UTC")
    stamps = pd.to_datetime(frame_5m["timestamp"], utc=True)
    hits = np.where(stamps == target)[0]
    if len(hits) == 0:
        deltas = (stamps - target).abs()
        return int(deltas.argmin())
    return int(hits[0])
