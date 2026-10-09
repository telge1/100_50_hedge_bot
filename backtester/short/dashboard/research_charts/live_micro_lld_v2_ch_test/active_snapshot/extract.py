"""Extract true ACTIVE state from an original engine prefix batch (ground truth)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from indicators.live_micro_lld_v2.models import LiquidityPoolV2

from ..batch import _load_candles_for_window
from .canonical_candle_session import CanonicalCandleSession
from ..identity import (
    ENGINE_VERSION,
    build_live_micro_lld_v2_config,
    candle_sequence_fingerprint,
    config_hash,
    engine_input_document,
    engine_input_hash,
)
from ...trp_import import ensure_trp_on_path
from .fingerprint import active_state_fingerprint


@dataclass(frozen=True)
class ActiveStateSnapshot:
    symbol: str
    timeframe: str
    pool_detail: int
    config_hash: str
    engine_version: str
    engine_input_hash: str
    candle_load_start: datetime
    history_from_close: datetime
    decision_close: datetime
    prefix_candle_count: int
    prefix_candle_fingerprint: str
    known_pool_count: int
    active_pool_ids: tuple[str, ...]
    active_sides: tuple[str, ...]
    active_upper_count: int
    active_lower_count: int
    active_state_fingerprint: str
    history_policy: str = ""

    @property
    def active_pool_count(self) -> int:
        return len(self.active_pool_ids)


def run_engine_prefix(
    candles: Sequence,
    cfg,
):
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.engine import run_live_micro_lld_v2

    if not candles:
        raise ValueError("no candles for prefix engine run")
    return run_live_micro_lld_v2(candles, cfg)


def extract_active_from_result(
    result,
    *,
    decision_close: datetime,
) -> tuple[list[str], list[str], int]:
    """Active membership via pool.active at prefix end (engine does not export active lists)."""
    dc = decision_close.astimezone(timezone.utc)
    active_ids: list[str] = []
    active_sides: list[str] = []
    upper = 0
    lower = 0
    for pool in result.pools:
        if not pool.active:
            continue
        if pool.known_at.astimezone(timezone.utc) > dc:
            raise ValueError(f"future-known active pool {pool.pool_id}")
        if pool.break_at is not None:
            raise ValueError(f"active pool with break_at set: {pool.pool_id}")
        active_ids.append(pool.pool_id)
        side = str(pool.side).lower()
        active_sides.append(side)
        if side == "upper":
            upper += 1
        elif side == "lower":
            lower += 1
    return active_ids, active_sides, len(result.pools)


def build_active_state_at_decision_close(
    symbol: str,
    timeframe: str,
    history_from_close: datetime,
    decision_close: datetime,
    *,
    pool_detail: int = 10,
    cfg=None,
    history_policy: str = "",
    candle_session: CanonicalCandleSession | None = None,
) -> tuple[ActiveStateSnapshot, list, Any]:
    """
    Load candles with fixed history_from_close (Phase-1 window start) and run engine on prefix ≤ decision_close.
    """
    pd = int(pool_detail)
    if cfg is None:
        cfg = build_live_micro_lld_v2_config(pd)
    if candle_session is not None:
        candles, meta = candle_session.load_for_window(
            history_from_close, decision_close, pd
        )
    else:
        candles, meta = _load_candles_for_window(
            symbol, timeframe, history_from_close, decision_close, pd
        )
    return assemble_active_state_from_candles(
        candles,
        meta,
        history_from_close=history_from_close,
        decision_close=decision_close,
        pool_detail=pd,
        cfg=cfg,
        history_policy=history_policy,
        timeframe=timeframe,
    )


def assemble_active_state_from_candles(
    candles: list,
    meta: dict[str, Any],
    *,
    history_from_close: datetime,
    decision_close: datetime,
    pool_detail: int,
    cfg=None,
    history_policy: str = "",
    timeframe: str,
) -> tuple[ActiveStateSnapshot, list, Any]:
    pd = int(pool_detail)
    if cfg is None:
        cfg = build_live_micro_lld_v2_config(pd)
    if not candles:
        raise ValueError("no candles after load/filter")
    last_close = candles[-1]
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.availability import bar_close
    from indicators.live_micro_lld_v2.as_of import parse_close_ts

    dc = parse_close_ts(decision_close)
    actual_last = bar_close(last_close.timestamp, timeframe)
    if actual_last > dc:
        raise ValueError(
            f"last candle close {actual_last.isoformat()} > decision_close {dc.isoformat()}"
        )

    result = run_engine_prefix(candles, cfg)
    active_ids, active_sides, known_count = extract_active_from_result(
        result, decision_close=dc
    )
    prefix_fp = candle_sequence_fingerprint(candles)
    cfg_h = config_hash(cfg)
    input_doc = engine_input_document(
        cfg=cfg,
        symbol=meta["symbol"],
        timeframe=meta["timeframe"],
        from_close=meta["from_close"],
        to_close=dc,
        candle_load_start=meta["candle_load_start"],
        candles=candles,
        warmup_bars=meta["warmup_bars"],
        history_policy=history_policy or None,
    )
    sorted_pairs = sorted(zip(active_ids, active_sides), key=lambda x: x[0])
    sorted_ids = tuple(p[0] for p in sorted_pairs)
    sorted_sides = tuple(p[1] for p in sorted_pairs)
    snap = ActiveStateSnapshot(
        symbol=meta["symbol"],
        timeframe=meta["timeframe"],
        pool_detail=pd,
        config_hash=cfg_h,
        engine_version=ENGINE_VERSION,
        engine_input_hash=engine_input_hash(input_doc),
        candle_load_start=meta["candle_load_start"],
        history_from_close=history_from_close.astimezone(timezone.utc),
        decision_close=dc,
        prefix_candle_count=len(candles),
        prefix_candle_fingerprint=prefix_fp,
        known_pool_count=known_count,
        active_pool_ids=sorted_ids,
        active_sides=sorted_sides,
        active_upper_count=sum(1 for s in active_sides if s == "upper"),
        active_lower_count=sum(1 for s in active_sides if s == "lower"),
        active_state_fingerprint=active_state_fingerprint(active_ids, active_sides),
        history_policy=str(history_policy or ""),
    )
    return snap, candles, result


def build_stress_config_pd10_box10():
    cfg = build_live_micro_lld_v2_config(10)
    cfg.box_amount = 10
    return cfg


def compare_active_sets(
    expected: ActiveStateSnapshot,
    pool_ids: Sequence[str],
    sides: Sequence[str],
    *,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    exp_ids = set(expected.active_pool_ids)
    got_ids = set(pool_ids)
    missing = sorted(exp_ids - got_ids)
    extra = sorted(got_ids - exp_ids)
    dupes = sorted({x for x in pool_ids if pool_ids.count(x) > 1})
    exp_side = dict(zip(expected.active_pool_ids, expected.active_sides))
    got_side = dict(zip(pool_ids, sides))
    side_mismatch = []
    for pid in sorted(exp_ids & got_ids):
        if exp_side.get(pid) != got_side.get(pid):
            side_mismatch.append(
                {"pool_id": pid, "expected": exp_side.get(pid), "got": got_side.get(pid)}
            )
    fp = expected_fingerprint or active_state_fingerprint(pool_ids, sides)
    return {
        "missing": missing,
        "extra": extra,
        "duplicate_pool_ids": dupes,
        "side_mismatch": side_mismatch,
        "fingerprint_match": fp == expected.active_state_fingerprint,
        "pass": not missing and not extra and not dupes and not side_mismatch and fp == expected.active_state_fingerprint,
    }
