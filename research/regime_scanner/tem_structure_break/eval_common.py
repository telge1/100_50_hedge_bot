"""Shared evaluation helpers for frozen v2 TEM structure-break generalization."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import FULL_HISTORY_CANDLE_LIMIT
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.regime_scanner.tem_structure_break.frozen_v2 import (
    FROZEN_RULE_ID,
    frozen_semantics_public,
)
from research.regime_scanner.tem_structure_break.monitor import (
    SIGNAL_VERSION,
    MonitorRuntime,
    build_5m_trace,
    build_htf_structure_frame,
    candles_to_frame,
    find_bar_by_timestamp,
    run_in_trade_monitor,
)

ROOT = Path(__file__).resolve().parents[3]
BLOCKER_SRC = ROOT / "research/backtests/results/tem_continuous_27_blocker_root_cause_20260722"
CONTINUOUS_SRC = ROOT / "research/backtests/results/staging_profiles_continuous_1000_500_20260722"
AAVE_DEV_TRADE_ID = "AAVEUSDT|two_early_medium|continuous|0006"


@dataclass
class CoinFrames:
    coin: str
    frame_5m: pd.DataFrame
    trace: pd.DataFrame
    h1: pd.DataFrame
    h4: pd.DataFrame


@dataclass
class TradeSpec:
    coin: str
    trade_id: str
    entry_ts: str
    entry_price: float
    start_bar: int
    end_bar: int | None
    side: str = "long"
    cohort: str = "blocker"  # blocker | control
    holdout_bucket: str = "holdout"  # development | holdout | control
    final_pnl: float | None = None
    highest_cycle: int | None = None
    duration_bars: int | None = None
    flat_ts: str | None = None
    selection_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class CoinFrameCache:
    def __init__(self) -> None:
        self._cache: dict[str, CoinFrames] = {}

    def get(self, coin: str, candle_limit: int = FULL_HISTORY_CANDLE_LIMIT) -> CoinFrames:
        if coin in self._cache:
            return self._cache[coin]
        candles = normalize_candles(coin, load_candles_for_symbol(coin, limit=candle_limit))
        frame = candles_to_frame(candles)
        frames = CoinFrames(
            coin=coin,
            frame_5m=frame,
            trace=build_5m_trace(frame),
            h1=build_htf_structure_frame(frame, "1h"),
            h4=build_htf_structure_frame(frame, "4h"),
        )
        self._cache[coin] = frames
        return frames


def lead_hours(signal_ts: str | None, ref_ts: str | None) -> float | None:
    if not signal_ts or not ref_ts:
        return None
    a = datetime.fromisoformat(str(signal_ts).replace("Z", "+00:00"))
    b = datetime.fromisoformat(str(ref_ts).replace("Z", "+00:00"))
    return (b - a).total_seconds() / 3600.0


def bar_to_ts(frame: pd.DataFrame, bar: int | None) -> str | None:
    if bar is None or bar < 0 or bar >= len(frame):
        return None
    return str(frame.iloc[int(bar)]["timestamp"])


def load_blocker_specs() -> list[TradeSpec]:
    blockers = list(csv_dicts(BLOCKER_SRC / "tem_end_blockers_27.csv"))
    out: list[TradeSpec] = []
    for r in blockers:
        tid = r["trade_id"]
        bucket = "development" if tid == AAVE_DEV_TRADE_ID else "holdout"
        out.append(
            TradeSpec(
                coin=r["coin"],
                trade_id=tid,
                entry_ts=str(r["start_time"]),
                entry_price=float(r["entry_price"]),
                start_bar=int(float(r["start_bar"])),
                end_bar=int(float(r["end_bar"])),
                cohort="blocker",
                holdout_bucket=bucket,
                final_pnl=float(r["total_pnl"]),
                highest_cycle=int(float(r["highest_cycle"])),
                duration_bars=int(float(r["duration_bars"])),
                meta={"pair_key": r.get("pair_key"), "open_mtm": r.get("open_mtm")},
            )
        )
    return sorted(out, key=lambda t: (t.coin, t.start_bar, t.trade_id))


def csv_dicts(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_cycle_map() -> dict[str, dict[int, dict[str, str]]]:
    rows = csv_dicts(BLOCKER_SRC / "blocker_cycle_timelines.csv")
    out: dict[str, dict[int, dict[str, str]]] = {}
    for r in rows:
        tid = r["trade_id"]
        out.setdefault(tid, {})[int(float(r["cycle_index"]))] = r
    return out


def load_explosion_map() -> dict[str, dict[str, str]]:
    return {r["trade_id"]: r for r in csv_dicts(BLOCKER_SRC / "blocker_cycle_explosion.csv")}


def cycle_ts(frame: pd.DataFrame, cycles: dict[int, dict[str, str]], cycle_i: int) -> str | None:
    c = cycles.get(cycle_i)
    if not c:
        return None
    bar = int(float(c.get("first_leg_fill_bar") or c.get("start_bar") or -1))
    return bar_to_ts(frame, bar)


def explosion_ts(
    frame: pd.DataFrame,
    cycles: dict[int, dict[str, str]],
    explosion: dict[str, str] | None,
) -> str | None:
    if not explosion:
        return None
    raw = explosion.get("explosion_cycle") or explosion.get("first_cycle_mtm_lt_50")
    if raw in (None, "", "None"):
        return None
    try:
        cyc = int(float(raw))
    except ValueError:
        return None
    return cycle_ts(frame, cycles, cyc)


def _event_ts(events: list[dict], name: str, which: str = "first") -> str | None:
    matched = [e for e in events if e.get("event") == name]
    if not matched:
        return None
    e = matched[0] if which == "first" else matched[-1]
    return e.get("signal_available_ts") or e.get("timestamp")


def _pending_episodes(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("event") == "BREAK_PENDING_4H"]


def classify_failure(rt: MonitorRuntime, *, invalidated_before_cycle5: bool | None) -> str:
    flags: list[str] = []
    assert rt.frozen is not None
    if rt.invalidated_ts:
        if invalidated_before_cycle5 is False:
            return "BREAK_AFTER_CYCLE5"
        return ""
    if rt.frozen.protected_low_1h is None and rt.frozen.protected_low_4h is None:
        flags.append("NO_VALID_FROZEN_LEVEL")
    if rt.break_cycle_id == 0:
        # no 4h episode armed
        if any(e.get("event") == "STRUCTURE_WARNING" for e in rt.events):
            flags.append("NO_REBREAK_BELOW_RECLAIM_LEVEL")
        else:
            flags.append("NO_DYNAMIC_LEVEL")
        # heuristic annotations from timeline samples
        h4_nan = sum(1 for s in rt.timeline if s.get("h4_protected_low") is None)
        if rt.timeline and h4_nan / max(len(rt.timeline), 1) > 0.5:
            flags.append("MAJOR_BEARISH_TRACKS_HIGHS")
        bos = sum(1 for s in rt.timeline if s.get("brk_4h_kind") == "external_bearish_bos")
        if bos == 0 and rt.break_cycle_id == 0:
            flags.append("BOS_EDGE_ALREADY_CONSUMED")
    else:
        reclaims = sum(1 for e in rt.events if e.get("event") == "RECLAIMED")
        if reclaims >= 2 and not rt.invalidated_ts:
            flags.append("RECLAIMED_REPEATEDLY")
        elif not rt.invalidated_ts:
            flags.append("NO_REBREAK_BELOW_RECLAIM_LEVEL")
    return "|".join(dict.fromkeys(flags)) if flags else "OTHER"


def cycle_at_ts(cycles: dict[int, dict[str, str]], frame: pd.DataFrame, ts: str | None) -> int | None:
    if not ts or not cycles:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    best = 0
    for idx, c in cycles.items():
        bar = int(float(c.get("first_leg_fill_bar") or -1))
        cts = bar_to_ts(frame, bar)
        if not cts:
            continue
        ct = pd.Timestamp(cts)
        if ct.tzinfo is None:
            ct = ct.tz_localize("UTC")
        if ct <= t:
            best = max(best, int(idx))
    return best or None


def summarize_trade(
    spec: TradeSpec,
    rt: MonitorRuntime,
    *,
    frame: pd.DataFrame,
    cycles: dict[int, dict[str, str]] | None = None,
    explosion: dict[str, str] | None = None,
) -> dict[str, Any]:
    cycles = cycles or {}
    events = rt.events
    pendings = _pending_episodes(events)
    reclaims = [e for e in events if e.get("event") == "RECLAIMED"]
    at_risk = [e for e in events if e.get("event") == "STRUCTURE_AT_RISK"]

    first_break_ts = _event_ts(events, "BREAK_PENDING_4H", "first")
    second_break_ts = (
        (pendings[1].get("signal_available_ts") or pendings[1].get("timestamp"))
        if len(pendings) >= 2
        else None
    )
    first_reclaim_ts = _event_ts(events, "RECLAIMED", "first")
    second_reclaim_ts = (
        (reclaims[1].get("signal_available_ts") or reclaims[1].get("timestamp"))
        if len(reclaims) >= 2
        else None
    )
    warning_ts = rt.warning_ts
    inv_ts = rt.invalidated_ts
    c4 = cycle_ts(frame, cycles, 4)
    c5 = cycle_ts(frame, cycles, 5)
    exp_ts = explosion_ts(frame, cycles, explosion)

    lead_c4 = lead_hours(inv_ts, c4) if inv_ts else lead_hours(first_break_ts, c4)
    lead_c5 = lead_hours(inv_ts, c5) if inv_ts else lead_hours(first_break_ts, c5)
    lead_exp = lead_hours(inv_ts, exp_ts) if inv_ts else lead_hours(first_break_ts, exp_ts)
    lead_inv_c4 = lead_hours(inv_ts, c4)
    lead_inv_c5 = lead_hours(inv_ts, c5)
    lead_inv_exp = lead_hours(inv_ts, exp_ts)
    lead_warn_c4 = lead_hours(warning_ts, c4)
    lead_warn_c5 = lead_hours(warning_ts, c5)

    inv_before_c4 = None if lead_inv_c4 is None else lead_inv_c4 > 0
    inv_before_c5 = None if lead_inv_c5 is None else lead_inv_c5 > 0
    inv_before_exp = None if lead_inv_exp is None else lead_inv_exp > 0

    dq: list[str] = []
    if rt.frozen and rt.frozen.protected_low_4h is None:
        dq.append("entry_frozen_4h_pl_null")
    if rt.frozen and rt.frozen.protected_low_1h is None:
        dq.append("entry_frozen_1h_pl_null")
    dq.append("entry_d1_major_unavailable")

    inv_event = next((e for e in events if e.get("event") == "LONG_THESIS_INVALIDATED"), None)
    decisive = pendings[-1] if pendings and inv_ts else (pendings[0] if pendings else None)

    failure = classify_failure(rt, invalidated_before_cycle5=inv_before_c5)

    # diagnostic recovery fields for controls
    flat_ts = spec.flat_ts
    recovered_after_warning = bool(
        flat_ts and warning_ts and lead_hours(warning_ts, flat_ts) is not None and lead_hours(warning_ts, flat_ts) > 0 and (spec.final_pnl or 0) > 0
    )
    recovered_after_break = bool(
        flat_ts and first_break_ts and lead_hours(first_break_ts, flat_ts) and lead_hours(first_break_ts, flat_ts) > 0 and (spec.final_pnl or 0) > 0
    )
    recovered_after_inv = bool(
        flat_ts and inv_ts and lead_hours(inv_ts, flat_ts) and lead_hours(inv_ts, flat_ts) > 0 and (spec.final_pnl or 0) > 0
    )
    t_inv_to_flat = lead_hours(inv_ts, flat_ts)
    would_freeze_block = bool(inv_ts and flat_ts and t_inv_to_flat is not None and t_inv_to_flat > 0)
    would_exit_close_winner = bool(would_freeze_block and (spec.final_pnl or 0) > 0)

    # rough max DD after first signal using timeline closes vs entry
    max_dd = None
    if rt.timeline and (warning_ts or first_break_ts):
        start_sig = warning_ts or first_break_ts
        assert start_sig is not None
        entry = float(spec.entry_price)
        after = False
        mdd = 0.0
        for snap in rt.timeline:
            ts = snap.get("timestamp")
            if not after:
                if str(ts) >= str(pd.Timestamp(start_sig)):
                    after = True
                else:
                    continue
            close = float(snap["close"])
            dd = (close - entry) / entry * 100.0
            mdd = min(mdd, dd)
        max_dd = mdd

    return {
        "coin": spec.coin,
        "trade_id": spec.trade_id,
        "cohort": spec.cohort,
        "holdout_bucket": spec.holdout_bucket,
        "entry_ts": spec.entry_ts,
        "entry_price": spec.entry_price,
        "start_bar": spec.start_bar,
        "end_bar": spec.end_bar,
        "entry_decision": None if rt.decision is None else rt.decision.decision,
        "entry_h4_major": None if rt.decision is None else rt.decision.h4_major,
        "entry_d1_major": None,
        "g1_long": None if rt.decision is None else rt.decision.g1_long,
        "frozen_pl_5m": None if rt.frozen is None else rt.frozen.protected_low_5m,
        "frozen_pl_1h": None if rt.frozen is None else rt.frozen.protected_low_1h,
        "frozen_pl_4h": None if rt.frozen is None else rt.frozen.protected_low_4h,
        "first_warning_ts": warning_ts,
        "first_break_ts": first_break_ts,
        "first_reclaim_ts": first_reclaim_ts,
        "second_break_ts": second_break_ts,
        "second_reclaim_ts": second_reclaim_ts,
        "break_episode_count": rt.break_cycle_id,
        "reclaim_count": len(reclaims),
        "first_structure_at_risk_ts": _event_ts(events, "STRUCTURE_AT_RISK", "first"),
        "final_invalidation_ts": inv_ts,
        "final_state": rt.state.value,
        "invalidation_level_type": None if not inv_event else (decisive or {}).get("kind") or rt.break_kind,
        "invalidation_level_value": None if not inv_event else inv_event.get("level") or rt.broken_level,
        "cycle_at_warning": cycle_at_ts(cycles, frame, warning_ts),
        "cycle_at_first_break": cycle_at_ts(cycles, frame, first_break_ts),
        "cycle_at_invalidation": cycle_at_ts(cycles, frame, inv_ts),
        "cycle4_ts": c4,
        "cycle5_ts": c5,
        "mtm_explosion_ts": exp_ts,
        "lead_hours_warning_vs_cycle4": lead_warn_c4,
        "lead_hours_warning_vs_cycle5": lead_warn_c5,
        "lead_hours_vs_cycle4": lead_inv_c4 if inv_ts else lead_c4,
        "lead_hours_vs_cycle5": lead_inv_c5 if inv_ts else lead_c5,
        "lead_hours_vs_explosion": lead_inv_exp if inv_ts else lead_exp,
        "lead_hours_invalidation_vs_cycle4": lead_inv_c4,
        "lead_hours_invalidation_vs_cycle5": lead_inv_c5,
        "lead_hours_invalidation_vs_explosion": lead_inv_exp,
        "invalidated_before_cycle4": inv_before_c4,
        "invalidated_before_cycle5": inv_before_c5,
        "invalidated_before_explosion": inv_before_exp,
        "warned_before_cycle4": None if lead_warn_c4 is None else lead_warn_c4 > 0,
        "warned_before_cycle5": None if lead_warn_c5 is None else lead_warn_c5 > 0,
        "root_cause_if_no_signal": failure,
        "data_quality_flags": "|".join(dq),
        "signal_version": SIGNAL_VERSION,
        "frozen_rule_id": FROZEN_RULE_ID,
        "final_pnl": spec.final_pnl,
        "highest_cycle": spec.highest_cycle,
        "duration_bars": spec.duration_bars,
        "flat_ts": flat_ts,
        "selection_reason": spec.selection_reason,
        "profitable_flat_ts": flat_ts if spec.cohort == "control" else None,
        "max_drawdown_after_signal_pct": max_dd,
        "recovered_after_warning": recovered_after_warning if spec.cohort == "control" else None,
        "recovered_after_break": recovered_after_break if spec.cohort == "control" else None,
        "recovered_after_invalidation": recovered_after_inv if spec.cohort == "control" else None,
        "time_from_invalidation_to_profitable_flat": t_inv_to_flat if spec.cohort == "control" else None,
        "would_freeze_have_blocked_recovery": would_freeze_block if spec.cohort == "control" else None,
        "would_exit_have_closed_a_winner": would_exit_close_winner if spec.cohort == "control" else None,
        "n_events": len(events),
        "ever_broken": rt.ever_broken,
        "last_reclaim_level": rt.last_reclaim_level,
    }


def extract_episodes(spec: TradeSpec, rt: MonitorRuntime) -> list[dict[str, Any]]:
    rows = []
    pending_by_cycle: dict[int, dict] = {}
    for e in rt.events:
        cid = int(e.get("break_cycle_id") or 0)
        if e.get("event") == "BREAK_PENDING_4H":
            pending_by_cycle[cid] = e
            rows.append(
                {
                    "trade_id": spec.trade_id,
                    "coin": spec.coin,
                    "cohort": spec.cohort,
                    "break_cycle_id": cid,
                    "event": "BREAK_PENDING",
                    "timestamp": e.get("signal_available_ts") or e.get("timestamp"),
                    "level": e.get("level"),
                    "kind": e.get("kind"),
                }
            )
        elif e.get("event") == "RECLAIMED":
            rows.append(
                {
                    "trade_id": spec.trade_id,
                    "coin": spec.coin,
                    "cohort": spec.cohort,
                    "break_cycle_id": cid,
                    "event": "RECLAIMED",
                    "timestamp": e.get("signal_available_ts") or e.get("timestamp"),
                    "level": e.get("level"),
                    "kind": None,
                }
            )
        elif e.get("event") == "LONG_THESIS_INVALIDATED":
            rows.append(
                {
                    "trade_id": spec.trade_id,
                    "coin": spec.coin,
                    "cohort": spec.cohort,
                    "break_cycle_id": cid,
                    "event": "INVALIDATED",
                    "timestamp": e.get("signal_available_ts") or e.get("timestamp"),
                    "level": e.get("level"),
                    "kind": e.get("kind"),
                    "pending_kind": (pending_by_cycle.get(cid) or {}).get("kind"),
                }
            )
    return rows


def run_spec(spec: TradeSpec, cache: CoinFrameCache) -> tuple[MonitorRuntime, CoinFrames]:
    frames = cache.get(spec.coin)
    # Prefer artifact start_bar when it matches series; else timestamp lookup.
    entry_bar = int(spec.start_bar)
    if entry_bar < 0 or entry_bar >= len(frames.frame_5m):
        entry_bar = find_bar_by_timestamp(frames.frame_5m, spec.entry_ts)
    # Align entry price from bar close if needed is already provided.
    end_bar = spec.end_bar
    if end_bar is not None:
        end_bar = min(int(end_bar), len(frames.trace) - 1)
    rt = run_in_trade_monitor(
        frame_5m=frames.frame_5m,
        entry_bar=entry_bar,
        entry_price=float(spec.entry_price),
        side=spec.side,
        end_bar=end_bar,
        trace=frames.trace,
        h1_frame=frames.h1,
        h4_frame=frames.h4,
    )
    return rt, frames


def write_semantics_snapshot(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_dir / "frozen_v2_semantics.json", frozen_semantics_public())


def median(vals: list[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None and np.isfinite(float(v))]
    if not xs:
        return None
    return float(np.median(xs))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
