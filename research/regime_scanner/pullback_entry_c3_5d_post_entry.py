"""Phase C3.5D D2 — causal post-entry telemetry (research-only, additive).

Observes fills from D1 continuation entries. Does **not** classify WARNING /
EARLY_FAILURE / STRUCTURE_INVALIDATED, does not close positions, and does not
modify D1 / C3.5 / C3.4B.

Fill-bar semantics
------------------
* Trigger on closed bar ``t`` (READY breakout).
* Fill at open of bar ``t+1``.
* Monitor starts on fill bar ``t+1``; that bar's high/low/close are included in
  MFE/MAE (after the open fill). Trigger-bar excursions are never used.

HTF alignment (descriptive, stricter than G1)
---------------------------------------------
* ``htf_alignment_lost``: current closed HTF major != trade direction (+1/-1).
  Neutral therefore counts as alignment lost after fill.
* ``htf_major_flip_confirmed``: HTF major transitions to the opposite direction
  (long → -1, short → +1), tracked as a distinct event.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import _finite
from research.regime_scanner.pullback_entry_c3_5d_continuation import (
    ARMING_MODE,
    BEARISH,
    BULLISH,
    NEUTRAL,
    ContinuationD1Config,
    apply_continuation_d1,
    default_d1_config,
    read_htf_major,
    read_ltf_major,
)

PHASE_D2 = "C3.5D_D2"
DEFAULT_POST_ENTRY_HORIZON_BARS = 24

# Forbidden severity labels in D2 outputs
_FORBIDDEN_SEVERITY = ("WARNING", "EARLY_FAILURE", "STRUCTURE_INVALIDATED")


@dataclass(frozen=True)
class PostEntryD2Config:
    post_entry_horizon_bars: int = DEFAULT_POST_ENTRY_HORIZON_BARS
    htf_major_col: str = "htf_major_direction"
    htf_missing_as_neutral: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": PHASE_D2,
            "post_entry_horizon_bars": self.post_entry_horizon_bars,
            "htf_major_col": self.htf_major_col,
            "htf_missing_as_neutral": self.htf_missing_as_neutral,
            "fill_bar_mfe_mae": "includes fill-bar high/low after open fill",
            "no_severity_classification": True,
        }


@dataclass
class FillSnapshot:
    setup_id: int
    direction: str  # long | short
    side: int
    arming_type: str
    trigger_bar: int
    trigger_timestamp: Any
    fill_bar: int
    fill_timestamp: Any | None
    entry_price: float
    setup_protected_level: float | None
    entry_protected_level: float | None
    entry_protected_side: str | None
    frozen_breakout_level: float | None
    frozen_pullback_high: float | None
    frozen_pullback_low: float | None
    frozen_prior_swing_high: float | None
    frozen_prior_swing_low: float | None
    frozen_micro_swing_high: float | None
    frozen_micro_swing_low: float | None
    frozen_atr_14: float | None
    frozen_ltf_major_at_fill: int | None = None
    frozen_htf_major_at_fill: int | None = None
    atr_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "direction": self.direction,
            "side": self.side,
            "arming_type": self.arming_type,
            "trigger_bar": self.trigger_bar,
            "trigger_timestamp": self.trigger_timestamp,
            "fill_bar": self.fill_bar,
            "fill_timestamp": self.fill_timestamp,
            "entry_price": self.entry_price,
            "setup_protected_level": self.setup_protected_level,
            "entry_protected_level": self.entry_protected_level,
            "entry_protected_side": self.entry_protected_side,
            "frozen_breakout_level": self.frozen_breakout_level,
            "frozen_pullback_high": self.frozen_pullback_high,
            "frozen_pullback_low": self.frozen_pullback_low,
            "frozen_prior_swing_high": self.frozen_prior_swing_high,
            "frozen_prior_swing_low": self.frozen_prior_swing_low,
            "frozen_micro_swing_high": self.frozen_micro_swing_high,
            "frozen_micro_swing_low": self.frozen_micro_swing_low,
            "frozen_atr_14": self.frozen_atr_14,
            "frozen_ltf_major_at_fill": self.frozen_ltf_major_at_fill,
            "frozen_htf_major_at_fill": self.frozen_htf_major_at_fill,
            "atr_available": self.atr_available,
        }


@dataclass
class ContinuationPostEntryRuntime:
    snap: FillSnapshot
    active: bool = True
    bars_since_fill: int = -1  # incremented at start of each step; 0 on fill bar
    mfe_price: float = 0.0
    mae_price: float = 0.0  # <= 0
    underwater_bars_consecutive: int = 0
    underwater_bars_total: int = 0
    # sticky ever / is flags
    breakout_level_is_lost: bool = False
    breakout_level_ever_lost: bool = False
    breakout_level_ever_reclaimed: bool = False
    entry_pullback_extreme_is_broken: bool = False
    entry_pullback_extreme_ever_broken: bool = False
    entry_protected_level_is_broken: bool = False
    entry_protected_level_ever_broken: bool = False
    micro_counter_bos_ever: bool = False
    ltf_major_alignment_is_lost: bool = False
    ltf_major_alignment_ever_lost: bool = False
    htf_alignment_is_lost: bool = False
    htf_alignment_ever_lost: bool = False
    htf_major_flip_ever: bool = False
    prev_htf_major: int | None = None
    # first-event bars
    first_breakout_lost_bar: int | None = None
    first_reclaim_bar: int | None = None
    first_pullback_extreme_broken_bar: int | None = None
    first_entry_protected_broken_bar: int | None = None
    first_micro_counter_bos_bar: int | None = None
    first_ltf_alignment_lost_bar: int | None = None
    first_htf_alignment_lost_bar: int | None = None
    first_htf_flip_bar: int | None = None
    monitor_end_reason: str | None = None
    timeline: list[dict[str, Any]] = field(default_factory=list)


def _side_from_entry(e: Mapping[str, Any]) -> int:
    if e.get("side") is not None:
        return int(e["side"])
    d = str(e.get("direction") or "").lower()
    if d == "long":
        return BULLISH
    if d == "short":
        return BEARISH
    return 0


def _atr_ok(atr: float | None) -> bool:
    return atr is not None and math.isfinite(float(atr)) and float(atr) > 0


def _safe_div_atr(num: float, atr: float | None) -> float:
    if not _atr_ok(atr):
        return float("nan")
    return float(num) / float(atr)


def _micro_counter_bos(row: Mapping[str, Any], *, side: int) -> bool:
    """Existing causal edges only — internal/micro against the trade."""
    if side > 0:
        return bool(row.get("arm_edge_internal_bear")) or bool(row.get("internal_bos_down"))
    if side < 0:
        return bool(row.get("arm_edge_internal_bull")) or bool(row.get("internal_bos_up"))
    return False


def excursion_signed(side: int, entry: float, high: float, low: float) -> tuple[float, float]:
    """Return (favorable_price, adverse_signed) for this bar. adverse_signed <= 0 when adverse."""
    if side > 0:
        fav = high - entry
        adv = low - entry  # negative if low < entry
    else:
        fav = entry - low
        adv = entry - high  # negative if high > entry
    return float(fav), float(adv)


def build_pending_snapshot_from_entry(entry: Mapping[str, Any]) -> FillSnapshot:
    """Snapshot from D1 entry at trigger time; fill majors/atr finalized on fill bar."""
    side = _side_from_entry(entry)
    atr_trig = entry.get("frozen_atr_14_at_trigger", entry.get("atr_14"))
    atr_v = _finite(atr_trig) if atr_trig is not None else float("nan")
    return FillSnapshot(
        setup_id=int(entry["setup_id"]),
        direction="long" if side > 0 else "short",
        side=side,
        arming_type=str(entry.get("arming_type") or ARMING_MODE),
        trigger_bar=int(entry["trigger_bar"]),
        trigger_timestamp=entry.get("trigger_timestamp"),
        fill_bar=int(entry["fill_bar"]),
        fill_timestamp=None,
        entry_price=float(entry["entry_price"]),
        setup_protected_level=(
            float(entry["setup_protected_level"])
            if entry.get("setup_protected_level") is not None
            and pd.notna(entry.get("setup_protected_level"))
            else None
        ),
        entry_protected_level=(
            float(entry["entry_protected_level"])
            if entry.get("entry_protected_level") is not None
            and pd.notna(entry.get("entry_protected_level"))
            else None
        ),
        entry_protected_side=entry.get("entry_protected_side"),
        frozen_breakout_level=(
            float(entry["frozen_breakout_level"])
            if entry.get("frozen_breakout_level") is not None
            and pd.notna(entry.get("frozen_breakout_level"))
            else None
        ),
        frozen_pullback_high=(
            float(entry["frozen_pullback_high"])
            if entry.get("frozen_pullback_high") is not None
            and pd.notna(entry.get("frozen_pullback_high"))
            else None
        ),
        frozen_pullback_low=(
            float(entry["frozen_pullback_low"])
            if entry.get("frozen_pullback_low") is not None
            and pd.notna(entry.get("frozen_pullback_low"))
            else None
        ),
        frozen_prior_swing_high=(
            float(entry["frozen_prior_swing_high"])
            if entry.get("frozen_prior_swing_high") is not None
            and pd.notna(entry.get("frozen_prior_swing_high"))
            else None
        ),
        frozen_prior_swing_low=(
            float(entry["frozen_prior_swing_low"])
            if entry.get("frozen_prior_swing_low") is not None
            and pd.notna(entry.get("frozen_prior_swing_low"))
            else None
        ),
        frozen_micro_swing_high=(
            float(entry["frozen_micro_swing_high"])
            if entry.get("frozen_micro_swing_high") is not None
            and pd.notna(entry.get("frozen_micro_swing_high"))
            else None
        ),
        frozen_micro_swing_low=(
            float(entry["frozen_micro_swing_low"])
            if entry.get("frozen_micro_swing_low") is not None
            and pd.notna(entry.get("frozen_micro_swing_low"))
            else None
        ),
        frozen_atr_14=atr_v if math.isfinite(atr_v) else None,
        atr_available=_atr_ok(atr_v if math.isfinite(atr_v) else None),
    )


def _finalize_fill_snapshot(
    snap: FillSnapshot,
    fill_row: Mapping[str, Any],
    *,
    htf_col: str,
    htf_missing_as_neutral: bool,
) -> FillSnapshot:
    """Freeze fill-bar majors and prefer fill-bar ATR when valid."""
    atr_fill = _finite(fill_row.get("atr_14"))
    atr = atr_fill if _atr_ok(atr_fill) else snap.frozen_atr_14
    ltf = read_ltf_major(fill_row)
    # local HTF read (avoid needing full ContinuationD1Config)
    if htf_col not in fill_row or fill_row.get(htf_col) is None or (
        isinstance(fill_row.get(htf_col), float) and math.isnan(fill_row.get(htf_col))  # type: ignore[arg-type]
    ):
        htf = NEUTRAL if htf_missing_as_neutral else 0
    else:
        try:
            htf = int(fill_row.get(htf_col) or 0)
        except (TypeError, ValueError):
            htf = NEUTRAL
    return FillSnapshot(
        setup_id=snap.setup_id,
        direction=snap.direction,
        side=snap.side,
        arming_type=snap.arming_type,
        trigger_bar=snap.trigger_bar,
        trigger_timestamp=snap.trigger_timestamp,
        fill_bar=snap.fill_bar,
        fill_timestamp=fill_row.get("timestamp"),
        entry_price=snap.entry_price,
        setup_protected_level=snap.setup_protected_level,
        entry_protected_level=snap.entry_protected_level,
        entry_protected_side=snap.entry_protected_side,
        frozen_breakout_level=snap.frozen_breakout_level,
        frozen_pullback_high=snap.frozen_pullback_high,
        frozen_pullback_low=snap.frozen_pullback_low,
        frozen_prior_swing_high=snap.frozen_prior_swing_high,
        frozen_prior_swing_low=snap.frozen_prior_swing_low,
        frozen_micro_swing_high=snap.frozen_micro_swing_high,
        frozen_micro_swing_low=snap.frozen_micro_swing_low,
        frozen_atr_14=atr if _atr_ok(atr) else None,
        frozen_ltf_major_at_fill=ltf,
        frozen_htf_major_at_fill=htf,
        atr_available=_atr_ok(atr),
    )


def step_post_entry(
    mon: ContinuationPostEntryRuntime,
    row: Mapping[str, Any],
    *,
    cfg: PostEntryD2Config,
    prev_htf: int | None = None,
) -> dict[str, Any]:
    """One closed fill-bar-or-later step. Does not end on structure breaks."""
    if not mon.active:
        raise RuntimeError("step_post_entry on inactive monitor")

    snap = mon.snap
    side = snap.side
    entry = snap.entry_price
    bar_i = int(row.get("bar_index", 0))
    high = _finite(row.get("high"))
    low = _finite(row.get("low"))
    close = _finite(row.get("close"))

    mon.bars_since_fill += 1
    fav, adv = excursion_signed(side, entry, high, low)
    # cumulative
    if fav > mon.mfe_price:
        mon.mfe_price = fav
    if adv < mon.mae_price:
        mon.mae_price = adv

    if side > 0:
        signed_close = (close - entry) / entry if entry else float("nan")
        signed_close_atr = _safe_div_atr(close - entry, snap.frozen_atr_14)
        underwater_now = close < entry
    else:
        signed_close = (entry - close) / entry if entry else float("nan")
        signed_close_atr = _safe_div_atr(entry - close, snap.frozen_atr_14)
        underwater_now = close > entry

    if underwater_now:
        mon.underwater_bars_consecutive += 1
        mon.underwater_bars_total += 1
    else:
        mon.underwater_bars_consecutive = 0

    # --- breakout lost / reclaim (close-only, strict) ---
    brk = snap.frozen_breakout_level
    breakout_level_lost_event = False
    breakout_level_reclaimed_event = False
    if brk is not None and math.isfinite(brk):
        if side > 0:
            is_lost = close < float(brk)
        else:
            is_lost = close > float(brk)
        if is_lost and not mon.breakout_level_is_lost:
            breakout_level_lost_event = True
            if mon.first_breakout_lost_bar is None:
                mon.first_breakout_lost_bar = bar_i
            mon.breakout_level_ever_lost = True
        if (not is_lost) and mon.breakout_level_is_lost:
            breakout_level_reclaimed_event = True
            if mon.first_reclaim_bar is None:
                mon.first_reclaim_bar = bar_i
            mon.breakout_level_ever_reclaimed = True
        mon.breakout_level_is_lost = is_lost
    else:
        is_lost = False
        mon.breakout_level_is_lost = False

    # --- pullback extreme ---
    pb_broken_event = False
    if side > 0 and snap.frozen_pullback_low is not None and math.isfinite(snap.frozen_pullback_low):
        pb_broken = close < float(snap.frozen_pullback_low)
    elif side < 0 and snap.frozen_pullback_high is not None and math.isfinite(snap.frozen_pullback_high):
        pb_broken = close > float(snap.frozen_pullback_high)
    else:
        pb_broken = False
    if pb_broken and not mon.entry_pullback_extreme_is_broken:
        pb_broken_event = True
        if mon.first_pullback_extreme_broken_bar is None:
            mon.first_pullback_extreme_broken_bar = bar_i
        mon.entry_pullback_extreme_ever_broken = True
    mon.entry_pullback_extreme_is_broken = pb_broken

    # --- entry protected ---
    prot_event = False
    elvl = snap.entry_protected_level
    if elvl is not None and math.isfinite(elvl):
        if side > 0:
            prot_broken = close < float(elvl)
        else:
            prot_broken = close > float(elvl)
    else:
        prot_broken = False
    if prot_broken and not mon.entry_protected_level_is_broken:
        prot_event = True
        if mon.first_entry_protected_broken_bar is None:
            mon.first_entry_protected_broken_bar = bar_i
        mon.entry_protected_level_ever_broken = True
    mon.entry_protected_level_is_broken = prot_broken

    # --- micro counter BOS ---
    micro_now = _micro_counter_bos(row, side=side)
    micro_event = False
    if micro_now and not mon.micro_counter_bos_ever:
        micro_event = True
        mon.micro_counter_bos_ever = True
        mon.first_micro_counter_bos_bar = bar_i
    elif micro_now:
        micro_event = False  # already ever; still report current via micro_counter_bos

    # --- LTF alignment ---
    ltf = read_ltf_major(row)
    want = BULLISH if side > 0 else BEARISH
    ltf_lost = ltf != want
    ltf_event = False
    if ltf_lost and not mon.ltf_major_alignment_is_lost:
        ltf_event = True
        if mon.first_ltf_alignment_lost_bar is None:
            mon.first_ltf_alignment_lost_bar = bar_i
        mon.ltf_major_alignment_ever_lost = True
    mon.ltf_major_alignment_is_lost = ltf_lost

    # --- HTF alignment / flip ---
    if cfg.htf_major_col not in row or row.get(cfg.htf_major_col) is None or (
        isinstance(row.get(cfg.htf_major_col), float) and math.isnan(row.get(cfg.htf_major_col))  # type: ignore[arg-type]
    ):
        htf = NEUTRAL if cfg.htf_missing_as_neutral else 0
    else:
        try:
            htf = int(row.get(cfg.htf_major_col) or 0)
        except (TypeError, ValueError):
            htf = NEUTRAL

    htf_lost = htf != want
    htf_align_event = False
    if htf_lost and not mon.htf_alignment_is_lost:
        htf_align_event = True
        if mon.first_htf_alignment_lost_bar is None:
            mon.first_htf_alignment_lost_bar = bar_i
        mon.htf_alignment_ever_lost = True
    mon.htf_alignment_is_lost = htf_lost

    # Flip confirmed: transition onto opposite major (first time only).
    opposite = BEARISH if side > 0 else BULLISH
    prev = prev_htf if prev_htf is not None else mon.prev_htf_major
    htf_flip_event = False
    if htf == opposite and prev is not None and int(prev) != opposite and not mon.htf_major_flip_ever:
        htf_flip_event = True
        mon.htf_major_flip_ever = True
        mon.first_htf_flip_bar = bar_i
    mon.prev_htf_major = htf

    mfe_atr = _safe_div_atr(mon.mfe_price, snap.frozen_atr_14)
    mae_atr = _safe_div_atr(mon.mae_price, snap.frozen_atr_14)

    row_out = {
        "setup_id": snap.setup_id,
        "direction": snap.direction,
        "bar_index": bar_i,
        "timestamp": row.get("timestamp"),
        "bars_since_fill": mon.bars_since_fill,
        "entry_price": entry,
        "signed_close_return": signed_close,
        "signed_close_return_atr": signed_close_atr,
        "mfe_price": mon.mfe_price,
        "mae_price": mon.mae_price,
        "mfe_atr": mfe_atr,
        "mae_atr": mae_atr,
        "atr_available": bool(snap.atr_available),
        "underwater_now": underwater_now,
        "underwater_bars_consecutive": mon.underwater_bars_consecutive,
        "underwater_bars_total": mon.underwater_bars_total,
        "breakout_level_is_lost": mon.breakout_level_is_lost,
        "breakout_level_ever_lost": mon.breakout_level_ever_lost,
        "breakout_level_lost_event": breakout_level_lost_event,
        "breakout_level_reclaimed_event": breakout_level_reclaimed_event,
        "breakout_level_lost": mon.breakout_level_ever_lost,  # alias: ever lost
        "breakout_level_reclaimed": mon.breakout_level_ever_reclaimed,
        "entry_pullback_extreme_is_broken": mon.entry_pullback_extreme_is_broken,
        "entry_pullback_extreme_ever_broken": mon.entry_pullback_extreme_ever_broken,
        "entry_pullback_extreme_broken": mon.entry_pullback_extreme_ever_broken,
        "entry_pullback_extreme_broken_event": pb_broken_event,
        "entry_protected_level_is_broken": mon.entry_protected_level_is_broken,
        "entry_protected_level_ever_broken": mon.entry_protected_level_ever_broken,
        "entry_protected_level_broken": mon.entry_protected_level_ever_broken,
        "entry_protected_level_broken_event": prot_event,
        "micro_counter_bos": mon.micro_counter_bos_ever,
        "micro_counter_bos_now": micro_now,
        "micro_counter_bos_event": micro_event,
        "ltf_major_alignment_is_lost": mon.ltf_major_alignment_is_lost,
        "ltf_major_alignment_ever_lost": mon.ltf_major_alignment_ever_lost,
        "ltf_major_alignment_lost": mon.ltf_major_alignment_ever_lost,
        "ltf_major_alignment_lost_event": ltf_event,
        "htf_alignment_is_lost": mon.htf_alignment_is_lost,
        "htf_alignment_ever_lost": mon.htf_alignment_ever_lost,
        "htf_alignment_lost": mon.htf_alignment_ever_lost,
        "htf_alignment_lost_event": htf_align_event,
        "htf_major_flip_confirmed": mon.htf_major_flip_ever,
        "htf_major_flip_confirmed_event": htf_flip_event,
        "ltf_major_direction": ltf,
        "htf_major_direction": htf,
        # frozen refs (immutable)
        "setup_protected_level": snap.setup_protected_level,
        "entry_protected_level": snap.entry_protected_level,
        "frozen_breakout_level": snap.frozen_breakout_level,
        "frozen_pullback_high": snap.frozen_pullback_high,
        "frozen_pullback_low": snap.frozen_pullback_low,
        "frozen_atr_14": snap.frozen_atr_14,
        "frozen_ltf_major_at_fill": snap.frozen_ltf_major_at_fill,
        "frozen_htf_major_at_fill": snap.frozen_htf_major_at_fill,
        "monitor_active": True,
        "monitor_end_reason": None,
    }
    # Guard: no severity labels
    for bad in _FORBIDDEN_SEVERITY:
        if bad in row_out:
            raise RuntimeError(f"D2 must not emit {bad}")
    mon.timeline.append(row_out)
    return row_out


def apply_post_entry_telemetry(
    frame: pd.DataFrame,
    entries: Sequence[Mapping[str, Any]],
    *,
    cfg: PostEntryD2Config | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run parallel post-entry monitors keyed by setup_id.

    Returns (timeline, fill_summary, event_summary).
    """
    cfg = cfg or PostEntryD2Config()
    df = frame.reset_index(drop=True).copy()
    if "bar_index" not in df.columns:
        df["bar_index"] = np.arange(len(df))

    pending: dict[int, FillSnapshot] = {}
    for e in entries:
        if e.get("entry_price") is None or e.get("fill_bar") is None:
            continue
        snap = build_pending_snapshot_from_entry(e)
        sid = snap.setup_id
        if sid in pending:
            raise RuntimeError(f"duplicate pending fill setup_id={sid}")
        pending[sid] = snap

    active: dict[int, ContinuationPostEntryRuntime] = {}
    finished: list[ContinuationPostEntryRuntime] = []
    all_rows: list[dict[str, Any]] = []

    for i in range(len(df)):
        row = df.iloc[i].to_dict()
        bi = int(row["bar_index"])

        # Activate monitors whose fill_bar == this bar
        for sid, snap in list(pending.items()):
            if int(snap.fill_bar) == bi:
                if sid in active:
                    raise RuntimeError(f"cannot overwrite active monitor setup_id={sid}")
                finalized = _finalize_fill_snapshot(
                    snap,
                    row,
                    htf_col=cfg.htf_major_col,
                    htf_missing_as_neutral=cfg.htf_missing_as_neutral,
                )
                mon = ContinuationPostEntryRuntime(snap=finalized)
                mon.prev_htf_major = finalized.frozen_htf_major_at_fill
                active[sid] = mon
                del pending[sid]

        # Step active monitors
        to_finish: list[int] = []
        for sid, mon in active.items():
            prev_htf = mon.prev_htf_major
            out = step_post_entry(mon, row, cfg=cfg, prev_htf=prev_htf)
            all_rows.append(out)
            # End conditions: horizon (bars_since_fill is 0-based on fill bar)
            if mon.bars_since_fill + 1 >= int(cfg.post_entry_horizon_bars):
                mon.active = False
                mon.monitor_end_reason = "horizon_reached"
                out["monitor_active"] = False
                out["monitor_end_reason"] = "horizon_reached"
                to_finish.append(sid)
        for sid in to_finish:
            finished.append(active.pop(sid))

    # Data end: close remaining
    for sid, mon in list(active.items()):
        mon.active = False
        mon.monitor_end_reason = "data_end"
        if mon.timeline:
            mon.timeline[-1]["monitor_active"] = False
            mon.timeline[-1]["monitor_end_reason"] = "data_end"
            # patch last all_rows entry for this setup if needed
            for r in reversed(all_rows):
                if int(r["setup_id"]) == sid and r.get("monitor_end_reason") is None:
                    r["monitor_active"] = False
                    r["monitor_end_reason"] = "data_end"
                    break
        finished.append(mon)
    active.clear()

    # Any pending never filled (fill bar beyond data)
    for sid, snap in pending.items():
        # no monitor started
        pass

    timeline = pd.DataFrame(all_rows)
    fill_summary = _build_fill_summary(finished)
    event_summary = _build_event_summary(finished)
    return timeline, fill_summary, event_summary


def _build_fill_summary(monitors: Sequence[ContinuationPostEntryRuntime]) -> pd.DataFrame:
    rows = []
    for mon in monitors:
        s = mon.snap
        rows.append(
            {
                "setup_id": s.setup_id,
                "direction": s.direction,
                "trigger_bar": s.trigger_bar,
                "fill_bar": s.fill_bar,
                "entry_price": s.entry_price,
                "setup_protected_level": s.setup_protected_level,
                "entry_protected_level": s.entry_protected_level,
                "frozen_breakout_level": s.frozen_breakout_level,
                "frozen_atr_14": s.frozen_atr_14,
                "atr_available": s.atr_available,
                "max_mfe_atr": _safe_div_atr(mon.mfe_price, s.frozen_atr_14),
                "max_mae_atr": _safe_div_atr(mon.mae_price, s.frozen_atr_14),
                "max_mfe_price": mon.mfe_price,
                "max_mae_price": mon.mae_price,
                "bars_underwater_total": mon.underwater_bars_total,
                "first_breakout_lost_bar": mon.first_breakout_lost_bar,
                "first_reclaim_bar": mon.first_reclaim_bar,
                "first_pullback_extreme_broken_bar": mon.first_pullback_extreme_broken_bar,
                "first_entry_protected_broken_bar": mon.first_entry_protected_broken_bar,
                "first_micro_counter_bos_bar": mon.first_micro_counter_bos_bar,
                "first_ltf_alignment_lost_bar": mon.first_ltf_alignment_lost_bar,
                "first_htf_alignment_lost_bar": mon.first_htf_alignment_lost_bar,
                "first_htf_flip_bar": mon.first_htf_flip_bar,
                "monitor_end_reason": mon.monitor_end_reason,
                "n_timeline_bars": len(mon.timeline),
            }
        )
    return pd.DataFrame(rows)


def _build_event_summary(monitors: Sequence[ContinuationPostEntryRuntime]) -> pd.DataFrame:
    rows = []
    for mon in monitors:
        rows.append(
            {
                "setup_id": mon.snap.setup_id,
                "direction": mon.snap.direction,
                "ever_breakout_lost": mon.breakout_level_ever_lost,
                "ever_reclaimed": mon.breakout_level_ever_reclaimed,
                "ever_pullback_extreme_broken": mon.entry_pullback_extreme_ever_broken,
                "ever_entry_protected_broken": mon.entry_protected_level_ever_broken,
                "ever_micro_counter_bos": mon.micro_counter_bos_ever,
                "ever_ltf_alignment_lost": mon.ltf_major_alignment_ever_lost,
                "ever_htf_alignment_lost": mon.htf_alignment_ever_lost,
                "ever_htf_flip": mon.htf_major_flip_ever,
            }
        )
    return pd.DataFrame(rows)


def content_hash_frames(*frames: pd.DataFrame) -> str:
    h = hashlib.sha256()
    for f in frames:
        if f is None or f.empty:
            h.update(b"empty")
        else:
            h.update(pd.util.hash_pandas_object(f.fillna(""), index=False).values.tobytes())
    return h.hexdigest()


def run_d2_smoke_on_frame(
    frame: pd.DataFrame,
    *,
    d1_cfg: ContinuationD1Config | None = None,
    d2_cfg: PostEntryD2Config | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Deterministic D1→D2 smoke; optional CSV/JSON write."""
    d1_cfg = d1_cfg or default_d1_config()
    d2_cfg = d2_cfg or PostEntryD2Config()
    tl, entries, lives = apply_continuation_d1(frame, d1_cfg, return_lifecycles=True)
    timeline, fill_sum, event_sum = apply_post_entry_telemetry(frame, entries, cfg=d2_cfg)

    # Severity guard
    blob = ",".join(timeline.columns.astype(str)) if not timeline.empty else ""
    for bad in _FORBIDDEN_SEVERITY:
        if bad in blob:
            raise RuntimeError(f"forbidden severity column {bad}")

    meta = {
        "phase": PHASE_D2,
        "n_d1_entries": len(entries),
        "n_timeline_rows": int(len(timeline)),
        "n_fills_monitored": int(len(fill_sum)),
        "d1_config": d1_cfg.to_dict(),
        "d2_config": d2_cfg.to_dict(),
        "content_hash": content_hash_frames(timeline, fill_sum, event_sum),
        "no_WARNING": True,
        "no_EARLY_FAILURE": True,
        "no_STRUCTURE_INVALIDATED_severity": True,
        "no_pine": True,
        "no_live_bot": True,
    }

    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(entries).to_csv(output_dir / "d1_entries.csv", index=False)
        timeline.to_csv(output_dir / "d2_post_entry_timeline.csv", index=False)
        fill_sum.to_csv(output_dir / "d2_fill_summary.csv", index=False)
        event_sum.to_csv(output_dir / "d2_event_summary.csv", index=False)
        (output_dir / "d2_audit_summary.json").write_text(
            json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
        )
        meta["output_dir"] = str(output_dir)
    return meta


def d2_semantics_doc() -> dict[str, Any]:
    return {
        "phase": PHASE_D2,
        "fill_bar": "next open after trigger; H/L/C of fill bar included in MFE/MAE",
        "mfe_atr": ">= 0 cumulative favorable / frozen_atr",
        "mae_atr": "<= 0 cumulative adverse signed / frozen_atr",
        "htf_alignment_lost": "htf_major != trade direction (neutral counts as lost)",
        "htf_major_flip_confirmed": "htf_major transitions to opposite of trade",
        "htf_g1_vs_alignment": (
            "G1 allows neutral at entry; post-fill alignment_lost treats neutral as lost"
        ),
        "monitor_end": ["horizon_reached", "data_end"],
        "does_not_end_on": [
            "entry_protected_broken",
            "pullback_extreme_broken",
            "htf_flip",
            "large_mae",
        ],
        "no_severity_states": list(_FORBIDDEN_SEVERITY),
        "parallel_monitors": "dict keyed by setup_id; no silent overwrite",
    }
