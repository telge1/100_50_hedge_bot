"""Phase C3.5D D1 — Continuation entry in confirmed major (research-only, additive).

D1 scope
--------
* Arm only on **first EMA9/20-band touch** while LTF major is already confirmed
  and aligned (``pullback_begin_long`` / ``pullback_begin_short``).
* Closed-only HTF-G1 guard (block long if HTF major bearish; short if bullish).
* Freeze ``setup_protected_level`` at arming; invalidate pre-entry if broken.
* Reuse C3.5 pullback → READY → breakout → next-open fill filter semantics.
* Freeze ``entry_protected_level`` at fill (snapshot for D2; no severity in D1).
* Post-entry MFE/MAE/events live in ``pullback_entry_c3_5d_post_entry`` (D2).

Not in D1
---------
* D2 post-entry telemetry / MFE-MAE monitor
* WARNING / EARLY_FAILURE / STRUCTURE_INVALIDATED
* Pine / live-bot / recovery wiring
* Alternative ATR-/impulse-extreme pullback definitions (audit-only later)

Does **not** modify ``pullback_entry_c3_5.py``, C3.4B, or C3.5c Pine.
Internal BOS is **not** an arm trigger (may appear only as context columns).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    PullbackEntryConfig,
    SetupRuntime,
    _adx_filters_ok,
    _atr_anti_chase_ok,
    _breakout_long,
    _breakout_short,
    _ema_band,
    _ema_filters_ok,
    _finite,
    _long_rejection_ok,
    _short_rejection_ok,
    _zone_reached_long,
    _zone_reached_short,
    classify_terminal_outcome,
    config_hash as c35_config_hash,
)

# ---------------------------------------------------------------------------
# States / constants
# ---------------------------------------------------------------------------

D1_STATES: tuple[str, ...] = (
    "IDLE",
    "SHORT_CONTINUATION_ARMED",
    "SHORT_PULLBACK",
    "SHORT_READY",
    "SHORT_ENTERED",
    "LONG_CONTINUATION_ARMED",
    "LONG_PULLBACK",
    "LONG_READY",
    "LONG_ENTERED",
)

SHORT_FAMILY = {
    "SHORT_CONTINUATION_ARMED",
    "SHORT_PULLBACK",
    "SHORT_READY",
    "SHORT_ENTERED",
}
LONG_FAMILY = {
    "LONG_CONTINUATION_ARMED",
    "LONG_PULLBACK",
    "LONG_READY",
    "LONG_ENTERED",
}

BEARISH = -1
NEUTRAL = 0
BULLISH = 1

PHASE = "C3.5D_D1"
ARMING_MODE = "continuation_ema_band_first_touch"

# HTF-G1 (D1 baseline) — documented for tests; stricter alignment is audit-only later.
HTF_G1_SEMANTICS_DOC = {
    "block_long": "htf_major_direction == -1 (HTF bearish)",
    "block_short": "htf_major_direction == +1 (HTF bullish)",
    "allow_neutral": True,
    "allow_missing_as_neutral": True,
    "note": (
        "HTF neutral or missing → entry allowed under G1. "
        "Stricter 'must be aligned with trade' is a later audit variant only."
    ),
}

# Future audit-only variants (not implemented in D1):
FUTURE_PULLBACK_BEGIN_VARIANTS_DOC = (
    "atr_distance_from_impulse_extreme",
    "structure_retracement_to_setup_protected",
)


@dataclass(frozen=True)
class ContinuationD1Config:
    """D1 continuation baseline — filters mirror C3.5 A6 unless overridden."""

    name: str = "D1"
    label: str = "continuation_ema_band_first_touch_htf_g1"
    side_mode: str = "both"
    # Nested C3.5 filter config (A6 defaults).
    filters: PullbackEntryConfig = field(
        default_factory=lambda: PullbackEntryConfig(
            name="A6_filters",
            label="reused_a6_filters",
            require_lower_high=True,
            rejection_mode="combined",
            require_ema_direction=True,
            require_ema_slope=True,
            require_adx_di=True,
            require_atr_anti_chase=True,
            ema_zone_mode="band_9_20",
            touch_mode="touch_high_low",
            breakout_mode="break_pullback_extreme",
            entry_price_mode="next_open",
            max_age_bars=24,
            mtf_mode="none",  # HTF-G1 is separate (htf_major_direction column)
        )
    )
    htf_g1_enabled: bool = True
    htf_major_col: str = "htf_major_direction"
    # If HTF column missing: treat as neutral (G1 does not block).
    htf_missing_as_neutral: bool = True
    # Same-bar: first touch arms and enters PULLBACK immediately.
    arm_enters_pullback_same_bar: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["filters"] = self.filters.to_dict()
        d["phase"] = PHASE
        d["arming_mode"] = ARMING_MODE
        d["future_pullback_begin_variants_not_in_d1"] = list(FUTURE_PULLBACK_BEGIN_VARIANTS_DOC)
        return d


def config_hash(cfg: ContinuationD1Config) -> str:
    import json

    raw = json.dumps(json_safe(cfg.to_dict()), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def default_d1_config() -> ContinuationD1Config:
    return ContinuationD1Config()


@dataclass
class ContinuationRuntime:
    state: str = "IDLE"
    side: int = 0
    setup_id: int | None = None
    start_bar: int | None = None
    start_timestamp: Any = None
    armed_price: float | None = None
    pullback_start_bar: int | None = None
    pullback_start_timestamp: Any = None
    pullback_high: float | None = None
    pullback_low: float | None = None
    prior_swing_high: float | None = None
    prior_swing_low: float | None = None
    rejection_bar: int | None = None
    rejection_timestamp: Any = None
    breakout_level: float | None = None
    setup_age: int = 0
    ready_age: int = 0
    invalidation_reason: str | None = None
    entry_reason: str | None = None
    entry_bar: int | None = None
    entry_timestamp: Any = None
    entry_price: float | None = None
    closes_beyond: int = 0
    arming_type: str | None = ARMING_MODE
    last_event: str | None = None
    last_reject_reason: str | None = None
    # D1 dual protected snapshots
    setup_protected_level: float | None = None
    setup_protected_side: str | None = None  # "low" | "high"
    entry_protected_level: float | None = None
    entry_protected_side: str | None = None
    # HTF at arm (frozen for diagnostics)
    htf_major_at_arm: int | None = None
    ltf_major_at_arm: int | None = None
    # Terminal
    terminal_outcome: str | None = None
    terminal_reason: str | None = None
    terminal_state: str | None = None
    terminal_bar: int | None = None
    terminal_setup_id: int | None = None
    terminal_setup_age: int | None = None
    terminal_ready_age: int | None = None
    terminal_direction: str | None = None

    def as_setup_runtime(self) -> SetupRuntime:
        """Duck-compatible view for C3.5 rejection/breakout/anti-chase helpers."""
        rt = SetupRuntime()
        rt.state = self.state
        rt.side = self.side
        rt.setup_id = self.setup_id
        rt.start_bar = self.start_bar
        rt.start_timestamp = self.start_timestamp
        rt.armed_price = self.armed_price
        rt.pullback_start_bar = self.pullback_start_bar
        rt.pullback_start_timestamp = self.pullback_start_timestamp
        rt.pullback_high = self.pullback_high
        rt.pullback_low = self.pullback_low
        rt.prior_swing_high = self.prior_swing_high
        rt.prior_swing_low = self.prior_swing_low
        rt.rejection_bar = self.rejection_bar
        rt.rejection_timestamp = self.rejection_timestamp
        rt.breakout_level = self.breakout_level
        rt.setup_age = self.setup_age
        rt.ready_age = self.ready_age
        rt.closes_beyond = self.closes_beyond
        rt.arming_type = self.arming_type
        return rt

    def sync_from_setup_runtime(self, s: SetupRuntime) -> None:
        self.pullback_high = s.pullback_high
        self.pullback_low = s.pullback_low
        self.breakout_level = s.breakout_level
        self.closes_beyond = s.closes_beyond


# ---------------------------------------------------------------------------
# HTF-G1 / band touch / pullback_begin
# ---------------------------------------------------------------------------


def read_htf_major(row: Mapping[str, Any], cfg: ContinuationD1Config) -> int:
    col = cfg.htf_major_col
    if col not in row or row.get(col) is None or (isinstance(row.get(col), float) and math.isnan(row.get(col))):  # type: ignore[arg-type]
        if cfg.htf_missing_as_neutral:
            return NEUTRAL
        raise KeyError(f"missing HTF major column {col}")
    try:
        return int(row.get(col) or 0)
    except (TypeError, ValueError):
        return NEUTRAL


def read_ltf_major(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("major_direction") or 0)
    except (TypeError, ValueError):
        return NEUTRAL


def htf_g1_blocks(side: int, htf_major: int) -> bool:
    """G1: block long if HTF bearish; block short if HTF bullish. Neutral allowed."""
    if side > 0:
        return int(htf_major) == BEARISH
    if side < 0:
        return int(htf_major) == BULLISH
    return False


def relevant_protected(row: Mapping[str, Any], *, side: int) -> tuple[float | None, str | None]:
    if side > 0:
        pl = row.get("protected_low")
        if pl is not None and pd.notna(pl):
            v = _finite(pl)
            return (v, "low") if math.isfinite(v) else (None, None)
        return None, None
    ph = row.get("protected_high")
    if ph is not None and pd.notna(ph):
        v = _finite(ph)
        return (v, "high") if math.isfinite(v) else (None, None)
    return None, None


def ema_band_touch(row: Mapping[str, Any], filters: PullbackEntryConfig, *, side: int) -> bool:
    if side > 0:
        return _zone_reached_long(row, filters)
    if side < 0:
        return _zone_reached_short(row, filters)
    return False


def pullback_begin_long(
    row: Mapping[str, Any],
    prev_row: Mapping[str, Any] | None,
    cfg: ContinuationD1Config,
) -> bool:
    """First EMA9/20-band touch while LTF major already bullish.

    Rising edge only: touch now, no touch on previous closed bar.
    Internal BOS alone never returns True.
    """
    if read_ltf_major(row) != BULLISH:
        return False
    if cfg.htf_g1_enabled and htf_g1_blocks(BULLISH, read_htf_major(row, cfg)):
        return False
    lvl, _ = relevant_protected(row, side=BULLISH)
    if lvl is None:
        return False
    touch = ema_band_touch(row, cfg.filters, side=BULLISH)
    if not touch:
        return False
    if prev_row is None:
        return True
    return not ema_band_touch(prev_row, cfg.filters, side=BULLISH)


def pullback_begin_short(
    row: Mapping[str, Any],
    prev_row: Mapping[str, Any] | None,
    cfg: ContinuationD1Config,
) -> bool:
    """First EMA9/20-band touch while LTF major already bearish (mirror)."""
    if read_ltf_major(row) != BEARISH:
        return False
    if cfg.htf_g1_enabled and htf_g1_blocks(BEARISH, read_htf_major(row, cfg)):
        return False
    lvl, _ = relevant_protected(row, side=BEARISH)
    if lvl is None:
        return False
    touch = ema_band_touch(row, cfg.filters, side=BEARISH)
    if not touch:
        return False
    if prev_row is None:
        return True
    return not ema_band_touch(prev_row, cfg.filters, side=BEARISH)


def setup_protected_broken(rt: ContinuationRuntime, row: Mapping[str, Any]) -> bool:
    """Pre-entry: close beyond frozen setup_protected_level."""
    lvl = rt.setup_protected_level
    if lvl is None or not math.isfinite(float(lvl)):
        return True
    close = _finite(row.get("close"))
    if rt.side > 0:
        return close < float(lvl)
    if rt.side < 0:
        return close > float(lvl)
    return False


# ---------------------------------------------------------------------------
# Runtime helpers
# ---------------------------------------------------------------------------


def _reset(rt: ContinuationRuntime) -> None:
    rt.state = "IDLE"
    rt.side = 0
    rt.setup_id = None
    rt.start_bar = None
    rt.start_timestamp = None
    rt.armed_price = None
    rt.pullback_start_bar = None
    rt.pullback_start_timestamp = None
    rt.pullback_high = None
    rt.pullback_low = None
    rt.prior_swing_high = None
    rt.prior_swing_low = None
    rt.rejection_bar = None
    rt.rejection_timestamp = None
    rt.breakout_level = None
    rt.setup_age = 0
    rt.ready_age = 0
    rt.invalidation_reason = None
    rt.entry_reason = None
    rt.entry_bar = None
    rt.entry_timestamp = None
    rt.entry_price = None
    rt.closes_beyond = 0
    rt.arming_type = ARMING_MODE
    rt.last_event = None
    rt.last_reject_reason = None
    rt.setup_protected_level = None
    rt.setup_protected_side = None
    rt.entry_protected_level = None
    rt.entry_protected_side = None
    rt.htf_major_at_arm = None
    rt.ltf_major_at_arm = None


def _clear_terminal(rt: ContinuationRuntime) -> None:
    rt.terminal_outcome = None
    rt.terminal_reason = None
    rt.terminal_state = None
    rt.terminal_bar = None
    rt.terminal_setup_id = None
    rt.terminal_setup_age = None
    rt.terminal_ready_age = None
    rt.terminal_direction = None


def _terminate(
    rt: ContinuationRuntime,
    *,
    bar_i: int,
    reason: str,
    entered: bool = False,
    events: list[str] | None = None,
) -> None:
    state_before = rt.state
    outcome = classify_terminal_outcome(state_before, reason, entered=entered)
    # Map C3.5 state names for classifier: CONTINUATION_ARMED ≈ ARMED
    if "CONTINUATION_ARMED" in state_before and outcome == "timed_out":
        outcome = "never_reached_pullback" if reason == "max_age" else outcome
    rt.terminal_outcome = outcome
    rt.terminal_reason = reason
    rt.terminal_state = state_before
    rt.terminal_bar = bar_i
    rt.terminal_setup_id = rt.setup_id
    rt.terminal_setup_age = rt.setup_age
    rt.terminal_ready_age = rt.ready_age
    rt.terminal_direction = "short" if rt.side < 0 else ("long" if rt.side > 0 else None)
    if events is not None:
        events.append(f"terminal:{outcome}:{reason}")
    if not entered:
        rt.invalidation_reason = reason
    _reset(rt)


def _mark_entered(rt: ContinuationRuntime, *, bar_i: int, reason: str, events: list[str] | None = None) -> None:
    rt.terminal_outcome = "entered"
    rt.terminal_reason = reason
    rt.terminal_state = rt.state
    rt.terminal_bar = bar_i
    rt.terminal_setup_id = rt.setup_id
    rt.terminal_setup_age = rt.setup_age
    rt.terminal_ready_age = rt.ready_age
    rt.terminal_direction = "short" if rt.side < 0 else ("long" if rt.side > 0 else None)
    if events is not None:
        events.append(f"terminal:entered:{reason}")


def _pre_entry_invalidate(rt: ContinuationRuntime, row: Mapping[str, Any], cfg: ContinuationD1Config) -> str | None:
    if rt.setup_age > cfg.filters.max_age_bars:
        return "max_age"
    if setup_protected_broken(rt, row):
        return "setup_protected_broken"
    maj = read_ltf_major(row)
    if rt.side > 0 and maj < 0:
        return "ltf_major_flipped_bearish"
    if rt.side < 0 and maj > 0:
        return "ltf_major_flipped_bullish"
    if cfg.htf_g1_enabled and htf_g1_blocks(rt.side, read_htf_major(row, cfg)):
        return "htf_g1_blocked"
    # Prior swing break (reuse C3.5 idea)
    if rt.side < 0 and rt.prior_swing_high is not None and _finite(row.get("close")) > rt.prior_swing_high:
        return "prior_swing_high_broken"
    if rt.side > 0 and rt.prior_swing_low is not None and _finite(row.get("close")) < rt.prior_swing_low:
        return "prior_swing_low_broken"
    return None


def _arm_continuation(
    rt: ContinuationRuntime,
    row: Mapping[str, Any],
    *,
    side: int,
    bar_i: int,
    cfg: ContinuationD1Config,
    setup_id: int,
    events: list[str],
) -> None:
    _clear_terminal(rt)
    lvl, side_name = relevant_protected(row, side=side)
    assert lvl is not None and side_name is not None
    rt.side = side
    rt.setup_id = setup_id
    rt.start_bar = bar_i
    rt.start_timestamp = row.get("timestamp")
    rt.armed_price = _finite(row.get("close"))
    rt.setup_age = 0
    rt.ready_age = 0
    rt.arming_type = ARMING_MODE
    rt.setup_protected_level = float(lvl)
    rt.setup_protected_side = side_name
    rt.entry_protected_level = None
    rt.entry_protected_side = None
    rt.htf_major_at_arm = read_htf_major(row, cfg)
    rt.ltf_major_at_arm = read_ltf_major(row)
    if side > 0:
        rt.state = "LONG_CONTINUATION_ARMED"
        rt.prior_swing_low = (
            _finite(row.get("micro_swing_low"))
            if row.get("micro_swing_low") is not None
            else _finite(row.get("low"))
        )
        rt.prior_swing_high = (
            _finite(row.get("micro_swing_high")) if row.get("micro_swing_high") is not None else None
        )
        events.append("long_continuation_armed")
        rt.last_event = "long_continuation_armed"
    else:
        rt.state = "SHORT_CONTINUATION_ARMED"
        rt.prior_swing_high = (
            _finite(row.get("micro_swing_high"))
            if row.get("micro_swing_high") is not None
            else _finite(row.get("high"))
        )
        rt.prior_swing_low = (
            _finite(row.get("micro_swing_low")) if row.get("micro_swing_low") is not None else None
        )
        events.append("short_continuation_armed")
        rt.last_event = "short_continuation_armed"

    if cfg.arm_enters_pullback_same_bar:
        _enter_pullback(rt, row, bar_i=bar_i, events=events)


def _enter_pullback(
    rt: ContinuationRuntime,
    row: Mapping[str, Any],
    *,
    bar_i: int,
    events: list[str],
) -> None:
    if rt.side > 0:
        rt.state = "LONG_PULLBACK"
        events.append("long_pullback")
        rt.last_event = "long_pullback"
    else:
        rt.state = "SHORT_PULLBACK"
        events.append("short_pullback")
        rt.last_event = "short_pullback"
    rt.pullback_start_bar = bar_i
    rt.pullback_start_timestamp = row.get("timestamp")
    rt.pullback_high = _finite(row.get("high"))
    rt.pullback_low = _finite(row.get("low"))


# ---------------------------------------------------------------------------
# Step / apply
# ---------------------------------------------------------------------------


def step_continuation_d1(
    rt: ContinuationRuntime,
    row: Mapping[str, Any],
    *,
    cfg: ContinuationD1Config,
    prev_row: Mapping[str, Any] | None = None,
    next_open: float | None = None,
    setup_id_factory: Any | None = None,
) -> tuple[ContinuationRuntime, dict[str, Any]]:
    """One closed-bar step for D1 continuation SM."""
    bar_i = int(row.get("bar_index", 0))
    ts = row.get("timestamp")
    events: list[str] = []
    entry_now = False
    filters = cfg.filters
    allow_short = cfg.side_mode in {"both", "short"}
    allow_long = cfg.side_mode in {"both", "long"}

    def _alloc_id() -> int:
        if setup_id_factory is None:
            return int(bar_i) + 1
        return int(setup_id_factory())

    def _fill_price() -> float:
        if filters.entry_price_mode == "next_open" and next_open is not None:
            return _finite(next_open)
        return _finite(row.get("close"))

    if rt.state != "IDLE":
        rt.setup_age += 1
    if rt.state in {"SHORT_READY", "LONG_READY"}:
        rt.ready_age += 1

    # --- IDLE: continuation arm on first EMA band touch ---
    if rt.state == "IDLE":
        if allow_long and pullback_begin_long(row, prev_row, cfg):
            _arm_continuation(
                rt, row, side=BULLISH, bar_i=bar_i, cfg=cfg, setup_id=_alloc_id(), events=events
            )
        elif allow_short and pullback_begin_short(row, prev_row, cfg):
            _arm_continuation(
                rt, row, side=BEARISH, bar_i=bar_i, cfg=cfg, setup_id=_alloc_id(), events=events
            )

    # --- CONTINUATION_ARMED (only if same-bar pullback disabled) ---
    elif rt.state == "LONG_CONTINUATION_ARMED":
        reason = _pre_entry_invalidate(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        elif ema_band_touch(row, filters, side=BULLISH):
            _enter_pullback(rt, row, bar_i=bar_i, events=events)

    elif rt.state == "SHORT_CONTINUATION_ARMED":
        reason = _pre_entry_invalidate(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        elif ema_band_touch(row, filters, side=BEARISH):
            _enter_pullback(rt, row, bar_i=bar_i, events=events)

    # --- PULLBACK ---
    elif rt.state == "LONG_PULLBACK":
        reason = _pre_entry_invalidate(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        else:
            rt.pullback_high = max(rt.pullback_high or -1e18, _finite(row.get("high")))
            rt.pullback_low = min(rt.pullback_low or 1e18, _finite(row.get("low")))
            srt = rt.as_setup_runtime()
            if _long_rejection_ok(srt, row, filters):
                rt.state = "LONG_READY"
                rt.rejection_bar = bar_i
                rt.rejection_timestamp = ts
                rt.breakout_level = rt.pullback_high
                rt.ready_age = 0
                rt.last_event = "long_ready"
                events.append("long_ready")

    elif rt.state == "SHORT_PULLBACK":
        reason = _pre_entry_invalidate(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        else:
            rt.pullback_high = max(rt.pullback_high or -1e18, _finite(row.get("high")))
            rt.pullback_low = min(rt.pullback_low or 1e18, _finite(row.get("low")))
            srt = rt.as_setup_runtime()
            if _short_rejection_ok(srt, row, filters):
                rt.state = "SHORT_READY"
                rt.rejection_bar = bar_i
                rt.rejection_timestamp = ts
                rt.breakout_level = rt.pullback_low
                rt.ready_age = 0
                rt.last_event = "short_ready"
                events.append("short_ready")

    # --- READY / breakout ---
    elif rt.state == "LONG_READY":
        reason = _pre_entry_invalidate(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        else:
            srt = rt.as_setup_runtime()
            ok, level, br = _breakout_long(srt, row, filters)
            rt.sync_from_setup_runtime(srt)
            if ok:
                if not _ema_filters_ok(row, filters, side=1):
                    rt.last_reject_reason = "ema_filter"
                    events.append("break_rejected:ema_filter")
                elif not _adx_filters_ok(row, filters, side=1):
                    rt.last_reject_reason = "adx_filter"
                    events.append("break_rejected:adx_filter")
                else:
                    atr_ok, atr_reason = _atr_anti_chase_ok(srt, row, filters, side=1)
                    if not atr_ok:
                        rt.last_reject_reason = atr_reason
                        events.append(f"break_rejected:{atr_reason}")
                    else:
                        rt.state = "LONG_ENTERED"
                        rt.breakout_level = level
                        rt.entry_bar = bar_i
                        rt.entry_timestamp = ts
                        rt.entry_reason = br or "long_breakout"
                        rt.entry_price = _fill_price()
                        # Freeze entry protected at fill/trigger (snapshot for D2)
                        elvl, eside = relevant_protected(row, side=BULLISH)
                        rt.entry_protected_level = elvl
                        rt.entry_protected_side = eside
                        entry_now = True
                        events.append("long_entered")
                        _mark_entered(rt, bar_i=bar_i, reason=rt.entry_reason, events=events)
            else:
                rt.pullback_low = min(rt.pullback_low or 1e18, _finite(row.get("low")))
                rt.pullback_high = max(rt.pullback_high or -1e18, _finite(row.get("high")))

    elif rt.state == "SHORT_READY":
        reason = _pre_entry_invalidate(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        else:
            srt = rt.as_setup_runtime()
            ok, level, br = _breakout_short(srt, row, filters)
            rt.sync_from_setup_runtime(srt)
            if ok:
                if not _ema_filters_ok(row, filters, side=-1):
                    rt.last_reject_reason = "ema_filter"
                    events.append("break_rejected:ema_filter")
                elif not _adx_filters_ok(row, filters, side=-1):
                    rt.last_reject_reason = "adx_filter"
                    events.append("break_rejected:adx_filter")
                else:
                    atr_ok, atr_reason = _atr_anti_chase_ok(srt, row, filters, side=-1)
                    if not atr_ok:
                        rt.last_reject_reason = atr_reason
                        events.append(f"break_rejected:{atr_reason}")
                    else:
                        rt.state = "SHORT_ENTERED"
                        rt.breakout_level = level
                        rt.entry_bar = bar_i
                        rt.entry_timestamp = ts
                        rt.entry_reason = br or "short_breakout"
                        rt.entry_price = _fill_price()
                        elvl, eside = relevant_protected(row, side=BEARISH)
                        rt.entry_protected_level = elvl
                        rt.entry_protected_side = eside
                        entry_now = True
                        events.append("short_entered")
                        _mark_entered(rt, bar_i=bar_i, reason=rt.entry_reason, events=events)
            else:
                rt.pullback_high = max(rt.pullback_high or -1e18, _finite(row.get("high")))
                rt.pullback_low = min(rt.pullback_low or 1e18, _finite(row.get("low")))

    elif rt.state in {"SHORT_ENTERED", "LONG_ENTERED"}:
        events.append("reset_after_entry")
        _reset(rt)

    lo, hi = _ema_band(row, filters.ema_zone_mode)
    diag = {
        "entry_state": rt.state,
        "entry_side": rt.side,
        "setup_id": rt.setup_id,
        "setup_age": rt.setup_age,
        "ready_age": rt.ready_age,
        "armed_price": rt.armed_price,
        "pullback_high": rt.pullback_high,
        "pullback_low": rt.pullback_low,
        "breakout_level": rt.breakout_level,
        "rejection_bar": rt.rejection_bar,
        "invalidation_reason": rt.invalidation_reason,
        "entry_reason": rt.entry_reason,
        "entry_signal": entry_now,
        "entry_price": rt.entry_price if entry_now else None,
        "entry_bar": rt.entry_bar if entry_now else None,
        "events": "|".join(events) if events else None,
        "arming_type": rt.arming_type,
        "variant": cfg.name,
        "phase": PHASE,
        "setup_protected_level": rt.setup_protected_level,
        "setup_protected_side": rt.setup_protected_side,
        "entry_protected_level": rt.entry_protected_level if entry_now else None,
        "entry_protected_side": rt.entry_protected_side if entry_now else None,
        "htf_major_at_arm": rt.htf_major_at_arm,
        "ltf_major_at_arm": rt.ltf_major_at_arm,
        "ltf_major_direction": read_ltf_major(row),
        "htf_major_direction": read_htf_major(row, cfg),
        "pullback_begin_long": pullback_begin_long(row, prev_row, cfg),
        "pullback_begin_short": pullback_begin_short(row, prev_row, cfg),
        "ema_band_lo": lo,
        "ema_band_hi": hi,
        "last_reject_reason": rt.last_reject_reason,
        "terminal_outcome": rt.terminal_outcome,
        "terminal_reason": rt.terminal_reason,
        "terminal_state": rt.terminal_state,
        "terminal_bar": rt.terminal_bar,
        "terminal_setup_id": rt.terminal_setup_id,
        "terminal_setup_age": rt.terminal_setup_age,
        "terminal_ready_age": rt.terminal_ready_age,
        "terminal_direction": rt.terminal_direction,
        "filters_config_hash": c35_config_hash(filters),
        "d1_config_hash": config_hash(cfg),
    }
    return rt, diag


def apply_continuation_d1(
    frame: pd.DataFrame,
    cfg: ContinuationD1Config | None = None,
    *,
    return_lifecycles: bool = False,
) -> tuple[pd.DataFrame, list[dict[str, Any]]] | tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay D1 continuation SM. Frame must include major_direction, EMAs, protected_*."""
    cfg = cfg or default_d1_config()
    df = frame.reset_index(drop=True).copy()
    if "bar_index" not in df.columns:
        df["bar_index"] = np.arange(len(df))
    opens = df["open"].astype(float).tolist()
    rt = ContinuationRuntime()
    rows: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    lifecycles: dict[int, dict[str, Any]] = {}
    next_id = 1

    def _alloc() -> int:
        nonlocal next_id
        sid = next_id
        next_id += 1
        return sid

    prev_row: dict[str, Any] | None = None
    for i in range(len(df)):
        row = df.iloc[i].to_dict()
        next_open = opens[i + 1] if i + 1 < len(opens) else None
        prev_id = rt.setup_id
        rt, diag = step_continuation_d1(
            rt,
            row,
            cfg=cfg,
            prev_row=prev_row,
            next_open=next_open,
            setup_id_factory=_alloc,
        )
        out = {"bar_index": int(row.get("bar_index", i)), "timestamp": row.get("timestamp"), **diag}
        rows.append(out)
        bi = int(out["bar_index"])
        ev = str(diag.get("events") or "")

        if "continuation_armed" in ev and diag.get("setup_id") is not None:
            sid = int(diag["setup_id"])
            lifecycles[sid] = {
                "setup_id": sid,
                "direction": "long" if int(diag.get("entry_side") or 0) > 0 else "short",
                "variant": cfg.name,
                "arming_type": ARMING_MODE,
                "armed_bar": bi,
                "armed_timestamp": row.get("timestamp"),
                "armed_price": diag.get("armed_price"),
                "setup_protected_level": diag.get("setup_protected_level"),
                "setup_protected_side": diag.get("setup_protected_side"),
                "htf_major_at_arm": diag.get("htf_major_at_arm"),
                "ltf_major_at_arm": diag.get("ltf_major_at_arm"),
                "pullback_bar": bi if "pullback" in ev else None,
                "ready_bar": None,
                "trigger_bar": None,
                "fill_bar": None,
                "entry_protected_level": None,
                "terminal_outcome": None,
                "terminal_reason": None,
                "entry_created": False,
            }

        if "long_ready" in ev or "short_ready" in ev:
            sid = diag.get("setup_id")
            if sid is not None and int(sid) in lifecycles:
                lifecycles[int(sid)]["ready_bar"] = bi

        if diag.get("entry_signal"):
            sid = int(diag.get("setup_id") or 0)
            if sid in lifecycles:
                life = lifecycles[sid]
                life["trigger_bar"] = bi
                life["fill_bar"] = bi + 1 if next_open is not None else None
                life["entry_created"] = True
                life["entry_protected_level"] = diag.get("entry_protected_level")
                life["entry_protected_side"] = diag.get("entry_protected_side")
                life["terminal_outcome"] = "entered"
            if not (cfg.filters.entry_price_mode == "next_open" and next_open is None):
                # Rich freeze snapshot for D2 (additive metadata; D1 transitions unchanged).
                entries.append(
                    {
                        **out,
                        "side": diag["entry_side"],
                        "direction": "long" if int(diag["entry_side"]) > 0 else "short",
                        "close": float(row["close"]),
                        "trigger_bar": bi,
                        "trigger_timestamp": row.get("timestamp"),
                        "fill_bar": bi + 1 if next_open is not None else bi,
                        "entry_price": diag.get("entry_price"),
                        "arming_type": ARMING_MODE,
                        "setup_protected_level": rt.setup_protected_level,
                        "setup_protected_side": rt.setup_protected_side,
                        "entry_protected_level": rt.entry_protected_level,
                        "entry_protected_side": rt.entry_protected_side,
                        "frozen_breakout_level": rt.breakout_level,
                        "frozen_pullback_high": rt.pullback_high,
                        "frozen_pullback_low": rt.pullback_low,
                        "frozen_prior_swing_high": rt.prior_swing_high,
                        "frozen_prior_swing_low": rt.prior_swing_low,
                        "frozen_micro_swing_high": row.get("micro_swing_high"),
                        "frozen_micro_swing_low": row.get("micro_swing_low"),
                        "frozen_atr_14_at_trigger": _finite(row.get("atr_14")),
                        "ltf_major_at_trigger": read_ltf_major(row),
                        "htf_major_at_trigger": read_htf_major(row, cfg),
                    }
                )

        if diag.get("terminal_outcome") and "terminal:" in ev:
            sid = diag.get("terminal_setup_id") or prev_id
            if sid is not None and int(sid) in lifecycles:
                life = lifecycles[int(sid)]
                life["terminal_outcome"] = diag.get("terminal_outcome")
                life["terminal_reason"] = diag.get("terminal_reason")
                life["terminal_bar"] = diag.get("terminal_bar")

        prev_row = row

    if rt.state != "IDLE" and rt.setup_id is not None:
        last_i = int(df.iloc[-1].get("bar_index", len(df) - 1))
        sid = int(rt.setup_id)
        if sid in lifecycles and lifecycles[sid].get("terminal_outcome") is None:
            lifecycles[sid].update(
                {
                    "terminal_bar": last_i,
                    "terminal_outcome": classify_terminal_outcome(rt.state, "end_of_data"),
                    "terminal_reason": "end_of_data",
                }
            )
        _terminate(rt, bar_i=last_i, reason="end_of_data")

    timeline = pd.DataFrame(rows)
    life_list = [lifecycles[k] for k in sorted(lifecycles.keys())]
    if return_lifecycles:
        return timeline, entries, life_list
    return timeline, entries


def semantics_doc() -> dict[str, Any]:
    return {
        "phase": PHASE,
        "arming_mode": ARMING_MODE,
        "pullback_begin_long": (
            "LTF major_direction == +1 AND HTF-G1 allows AND protected_low present "
            "AND first EMA9/20 band touch (rising edge vs previous bar)"
        ),
        "pullback_begin_short": (
            "LTF major_direction == -1 AND HTF-G1 allows AND protected_high present "
            "AND first EMA9/20 band touch (rising edge vs previous bar)"
        ),
        "htf_g1": dict(HTF_G1_SEMANTICS_DOC),
        "setup_protected_level": "frozen at arming; pre-entry invalidate if close beyond",
        "entry_protected_level": "frozen at trigger/fill snapshot; D2 telemetry in separate module",
        "same_bar_arm": (
            "EMA touch bar may IDLE→CONTINUATION_ARMED→PULLBACK same bar; "
            "never READY or ENTERED on that same bar"
        ),
        "internal_bos_not_arm_trigger": True,
        "fill": "entry_price_mode next_open → fill at next bar open; trigger on READY breakout close",
        "not_in_d1": [
            "post_entry_telemetry",
            "WARNING",
            "EARLY_FAILURE",
            "STRUCTURE_INVALIDATED",
            "pine",
            "live_bot",
        ],
        "future_pullback_begin_variants": list(FUTURE_PULLBACK_BEGIN_VARIANTS_DOC),
    }
