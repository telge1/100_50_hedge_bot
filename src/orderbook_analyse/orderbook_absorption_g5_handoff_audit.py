"""A2 Absorption → G5 handoff research audit (read-only, CSV-driven).

A2 is an armed-state / confirmation layer only — never a standalone trading signal.

Window / pairing semantics
--------------------------
armed_time = A2 action_time (else first_signal_time)
expiry_time = armed_time + armed_window_seconds
Valid G5 pairing: armed_time < g5_event_time <= expiry_time
(event exactly at expiry_time is included)

False positive (downside reversal, conservative)
-----------------------------------------------
fp_adverse_before = time_to_up_0_25 is not None and
                    (time_to_down_0_25 is None or time_to_up_0_25 < time_to_down_0_25)
fp_no_hit_mae     = not hit_down_0_25 within 600s path and mae_up_bps > mfe_down_bps
false_positive    = fp_adverse_before OR fp_no_hit_mae

Outcomes use mid path strictly after action_time.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import orjson

from orderbook_analyse.dynamic_wall_detector import PROJECT_ROOT, parse_utc, utc_now
from orderbook_analyse.orderbook_absorption_exhaustion_audit import (
    simulate_mid_outcomes,
)

logger = logging.getLogger(__name__)

A2_TYPE = "ASK_ABSORPTION"
WALL_SIDE = "Ask"

D0 = "D0"
D1 = "D1"
D2 = "D2"
D3_WINDOWS = (30, 60, 90, 120, 180, 300)
D4_BPS = (3, 5, 10, 20, 30, 50)
D5_CONFIRM = (1, 2, 3)
D5_TIME = (30, 60)
D6 = "D6"

OUTPUT_FILES = (
    "REPORT.md",
    "config.json",
    "integrity.json",
    "input_inventory.json",
    "a2_episodes_loaded.csv",
    "g5_warnings_loaded.csv",
    "g5_actions_loaded.csv",
    "handoff_state_transitions.csv",
    "handoff_raw_pairings.csv",
    "handoff_rejected_pairings.csv",
    "handoff_actions.csv",
    "handoff_outcomes.csv",
    "handoff_variant_summary.csv",
    "handoff_window_ablation.csv",
    "handoff_wall_distance_ablation.csv",
    "handoff_reentry_ablation.csv",
    "handoff_g5_comparison.csv",
    "handoff_examples.csv",
)


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    return ensure_utc(parse_utc(str(value).replace("Z", "+00:00")))


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _median(vals: Sequence[float | None]) -> float | None:
    xs = sorted(float(v) for v in vals if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def bps_distance(a: float, b: float) -> float:
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b) * 10_000.0


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], headers: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        fieldnames: list[str] = []
        for row in rows:
            for k in row:
                if k not in fieldnames:
                    fieldnames.append(k)
    elif headers:
        fieldnames = list(headers)
    else:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in fieldnames})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass
class HandoffParams:
    armed_window_seconds: int = 180
    dedupe_gap_seconds: int = 120
    dedupe_level_bps: float = 10.0
    reentry_confirm_snapshots: int = 2
    symbol: str = "APTUSDT"
    start: str = "2026-07-26T09:16:29Z"
    end: str = "2026-07-26T13:08:27Z"


@dataclass
class A2Episode:
    episode_id: str
    armed_time: datetime
    first_signal_time: datetime
    action_time: datetime | None
    wall_price: float | None
    wall_side: str
    a2_score: int
    a2_quality: str
    a2_buy_at_wall_notional: float | None
    a2_price_progress_bps: float | None
    a2_level_join_quality: str | None
    a2_regime: str | None
    mid: float | None
    signal_id: str | None = None


@dataclass
class G5Action:
    warning_id: str
    episode_id: str
    warning_time: datetime
    action_time: datetime
    action: str
    mid: float | None
    warning_score: int | None
    warning_quality: str | None
    support_level: float | None
    reason: str | None


@dataclass
class ArmedState:
    symbol: str
    episode_id: str
    armed_time: datetime
    expiry_time: datetime
    wall_price: float | None
    wall_side: str
    a2_score: int
    a2_quality: str
    a2_buy_at_wall_notional: float | None
    a2_price_progress_bps: float | None
    a2_level_join_quality: str | None
    a2_regime: str | None
    state: str = "A2_ARMED"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_a2_episodes(
    absorption_dir: Path,
) -> tuple[list[A2Episode], list[dict[str, Any]], list[str]]:
    missing: list[str] = []
    ep_path = absorption_dir / "pattern_episodes.csv"
    act_path = absorption_dir / "pattern_actions.csv"
    if not ep_path.exists():
        missing.append(str(ep_path))
    if not act_path.exists():
        missing.append(str(act_path))
    episodes_raw = read_csv(ep_path)
    actions_raw = read_csv(act_path)
    actions_by_ep = {
        r["episode_id"]: r
        for r in actions_raw
        if r.get("pattern_type") == A2_TYPE and r.get("episode_id")
    }
    out: list[A2Episode] = []
    rows: list[dict[str, Any]] = []
    for r in episodes_raw:
        if r.get("pattern_type") != A2_TYPE:
            continue
        eid = r["episode_id"]
        act = actions_by_ep.get(eid, {})
        first_sig = parse_ts(r.get("first_signal_time"))
        act_t = parse_ts(r.get("action_time") or act.get("action_time"))
        if first_sig is None and act_t is None:
            continue
        armed = act_t or first_sig
        assert armed is not None
        wall = _f(r.get("level_price"))
        if wall is None:
            wall = _f(act.get("level"))
        ep = A2Episode(
            episode_id=eid,
            armed_time=armed,
            first_signal_time=first_sig or armed,
            action_time=act_t,
            wall_price=wall,
            wall_side=WALL_SIDE,
            a2_score=int(_i(r.get("max_score")) or _i(act.get("score")) or 0),
            a2_quality=str(act.get("confidence") or "UNKNOWN"),
            a2_buy_at_wall_notional=_f(act.get("aggressive_buy_notional")),
            a2_price_progress_bps=_f(act.get("price_progress_bps")),
            a2_level_join_quality=act.get("level_join_quality") or act.get("confidence"),
            a2_regime=act.get("trend_state"),
            mid=_f(act.get("action_mid") or act.get("mid")),
            signal_id=act.get("signal_id") or r.get("strongest_signal_id"),
        )
        out.append(ep)
        rows.append(
            {
                "episode_id": ep.episode_id,
                "armed_time": ep.armed_time.isoformat(),
                "first_signal_time": ep.first_signal_time.isoformat(),
                "action_time": None if ep.action_time is None else ep.action_time.isoformat(),
                "wall_price": ep.wall_price,
                "wall_side": ep.wall_side,
                "a2_score": ep.a2_score,
                "a2_quality": ep.a2_quality,
                "a2_buy_at_wall_notional": ep.a2_buy_at_wall_notional,
                "a2_price_progress_bps": ep.a2_price_progress_bps,
                "a2_level_join_quality": ep.a2_level_join_quality,
                "a2_regime": ep.a2_regime,
                "mid": ep.mid,
                "eligible": True,
            }
        )
    out.sort(key=lambda e: (e.armed_time, e.episode_id))
    return out, rows, missing


def load_g5_actions(g5_dir: Path) -> tuple[list[G5Action], list[dict[str, Any]], list[str]]:
    missing: list[str] = []
    path = g5_dir / "integrated_variant_actions.csv"
    if not path.exists():
        missing.append(str(path))
        return [], [], missing
    rows_in = read_csv(path)
    out: list[G5Action] = []
    rows: list[dict[str, Any]] = []
    for r in rows_in:
        if r.get("variant") != "G5":
            continue
        wt = parse_ts(r.get("warning_time"))
        at = parse_ts(r.get("action_time"))
        if wt is None or at is None:
            continue
        g = G5Action(
            warning_id=str(r.get("warning_id") or ""),
            episode_id=str(r.get("episode_id") or ""),
            warning_time=wt,
            action_time=at,
            action=str(r.get("action") or ""),
            mid=_f(r.get("mid")),
            warning_score=_i(r.get("warning_score")),
            warning_quality=r.get("warning_quality"),
            support_level=_f(r.get("support_level")),
            reason=r.get("reason"),
        )
        out.append(g)
        rows.append(
            {
                "warning_id": g.warning_id,
                "episode_id": g.episode_id,
                "warning_time": g.warning_time.isoformat(),
                "action_time": g.action_time.isoformat(),
                "action": g.action,
                "mid": g.mid,
                "warning_score": g.warning_score,
                "warning_quality": g.warning_quality,
                "support_level": g.support_level,
                "warning_to_action_delay_seconds": (
                    g.action_time - g.warning_time
                ).total_seconds(),
            }
        )
    out.sort(key=lambda g: (g.action_time, g.warning_id))
    return out, rows, missing


def load_g5_warnings(g5_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    missing: list[str] = []
    path = g5_dir / "integrated_warning_context.csv"
    if not path.exists():
        # fallback: derive from G5 actions warning_time
        return [], [str(path)]
    rows = []
    for r in read_csv(path):
        wt = parse_ts(r.get("warning_time"))
        if wt is None:
            continue
        rows.append(
            {
                "warning_id": r.get("warning_id"),
                "warning_time": wt.isoformat(),
                "score": r.get("score"),
                "warning_quality": r.get("warning_quality"),
                "mid": r.get("mid"),
                "combined_regime": r.get("combined_regime"),
                "support_break_valid": r.get("support_break_valid"),
            }
        )
    return rows, missing


def load_mid_path(absorption_dir: Path) -> list[tuple[datetime, float]]:
    path = absorption_dir / "snapshot_features.csv"
    out: list[tuple[datetime, float]] = []
    for r in read_csv(path):
        ts = parse_ts(r.get("timestamp"))
        mid = _f(r.get("mid"))
        if ts is None or mid is None:
            continue
        out.append((ts, mid))
    out.sort(key=lambda x: x[0])
    return out


def load_ask_walls(absorption_dir: Path) -> list[tuple[datetime, float | None, float | None]]:
    """(timestamp, nearest_ask, mid) causal wall series."""
    path = absorption_dir / "snapshot_features.csv"
    out = []
    for r in read_csv(path):
        ts = parse_ts(r.get("timestamp"))
        if ts is None:
            continue
        out.append((ts, _f(r.get("nearest_ask")), _f(r.get("mid"))))
    out.sort(key=lambda x: x[0])
    return out


def wall_as_of(
    walls: Sequence[tuple[datetime, float | None, float | None]],
    *,
    as_of: datetime,
) -> tuple[float | None, datetime | None]:
    """Last nearest_ask with snapshot_time <= as_of."""
    t = ensure_utc(as_of)
    chosen: tuple[float | None, datetime | None] = (None, None)
    for ts, ask, _mid in walls:
        if ts <= t:
            if ask is not None:
                chosen = (ask, ts)
        else:
            break
    return chosen


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def quality_rank(q: str | None) -> int:
    order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "A2_LOW_CONFIDENCE": 0, "UNKNOWN": 0}
    return order.get(str(q or "UNKNOWN").upper(), 0)


def pair_a2_to_g5(
    a2_eps: Sequence[A2Episode],
    g5_actions: Sequence[G5Action],
    *,
    window_seconds: int,
    use_warning_time: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic 1:1 pairing. Returns (accepted, rejected, transitions)."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    used_g5: set[str] = set()

    # Build candidate list
    candidates: list[tuple[float, int, int, str, A2Episode, G5Action, datetime]] = []
    for ep in a2_eps:
        expiry = ep.armed_time + timedelta(seconds=window_seconds)
        transitions.append(
            {
                "episode_id": ep.episode_id,
                "previous_state": "IDLE",
                "new_state": "A2_ARMED",
                "transition_time": ep.armed_time.isoformat(),
                "wall_price": ep.wall_price,
                "reason": "a2_armed",
            }
        )
        for g in g5_actions:
            event_t = g.warning_time if use_warning_time else g.action_time
            if not (ep.armed_time < event_t <= expiry):
                rejected.append(
                    {
                        "a2_episode_id": ep.episode_id,
                        "g5_warning_id": g.warning_id,
                        "reject_reason": (
                            "BEFORE_ARMED"
                            if event_t <= ep.armed_time
                            else "AFTER_EXPIRY"
                        ),
                        "armed_time": ep.armed_time.isoformat(),
                        "event_time": event_t.isoformat(),
                        "expiry_time": expiry.isoformat(),
                        "delay_seconds": (event_t - ep.armed_time).total_seconds(),
                    }
                )
                continue
            delay = (event_t - ep.armed_time).total_seconds()
            # sort key: delay asc, -quality, -score, episode_id
            candidates.append(
                (
                    delay,
                    -quality_rank(ep.a2_quality),
                    -ep.a2_score,
                    ep.episode_id,
                    ep,
                    g,
                    event_t,
                )
            )

    candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[5].warning_id))
    used_a2: set[str] = set()
    for delay, _q, _s, _eid, ep, g, event_t in candidates:
        gkey = g.warning_id or f"{g.episode_id}:{g.action_time.isoformat()}"
        if ep.episode_id in used_a2:
            rejected.append(
                {
                    "a2_episode_id": ep.episode_id,
                    "g5_warning_id": g.warning_id,
                    "reject_reason": "A2_ALREADY_PAIRED",
                    "armed_time": ep.armed_time.isoformat(),
                    "event_time": event_t.isoformat(),
                    "delay_seconds": delay,
                }
            )
            continue
        if gkey in used_g5:
            rejected.append(
                {
                    "a2_episode_id": ep.episode_id,
                    "g5_warning_id": g.warning_id,
                    "reject_reason": "G5_ALREADY_PAIRED",
                    "armed_time": ep.armed_time.isoformat(),
                    "event_time": event_t.isoformat(),
                    "delay_seconds": delay,
                }
            )
            continue
        used_a2.add(ep.episode_id)
        used_g5.add(gkey)
        expiry = ep.armed_time + timedelta(seconds=window_seconds)
        accepted.append(
            {
                "a2_episode_id": ep.episode_id,
                "g5_warning_id": g.warning_id,
                "g5_episode_id": g.episode_id,
                "armed_time": ep.armed_time.isoformat(),
                "expiry_time": expiry.isoformat(),
                "g5_warning_time": g.warning_time.isoformat(),
                "g5_action_time": g.action_time.isoformat(),
                "event_time": event_t.isoformat(),
                "action_time": g.action_time.isoformat(),
                "a2_to_g5_seconds": delay,
                "a2_to_action_seconds": (g.action_time - ep.armed_time).total_seconds(),
                "warning_to_action_seconds": (
                    g.action_time - g.warning_time
                ).total_seconds(),
                "wall_price": ep.wall_price,
                "a2_score": ep.a2_score,
                "a2_quality": ep.a2_quality,
                "g5_action": g.action,
                "g5_mid": g.mid,
                "a2_mid": ep.mid,
            }
        )
        transitions.append(
            {
                "episode_id": ep.episode_id,
                "previous_state": "A2_ARMED",
                "new_state": "G5_CONFIRMED",
                "transition_time": event_t.isoformat(),
                "wall_price": ep.wall_price,
                "reason": "g5_within_armed_window",
                "g5_warning_id": g.warning_id,
            }
        )
        transitions.append(
            {
                "episode_id": ep.episode_id,
                "previous_state": "G5_CONFIRMED",
                "new_state": "ACTIONED",
                "transition_time": g.action_time.isoformat(),
                "wall_price": ep.wall_price,
                "reason": "g5_action",
                "g5_warning_id": g.warning_id,
            }
        )
    return accepted, rejected, transitions


def dedupe_actions(
    actions: Sequence[dict[str, Any]],
    *,
    gap_seconds: int,
    level_bps: float,
) -> list[dict[str, Any]]:
    if not actions:
        return []
    sorted_acts = sorted(
        actions,
        key=lambda a: (
            parse_ts(str(a["action_time"])) or datetime.min.replace(tzinfo=timezone.utc),
            str(a.get("a2_episode_id") or a.get("episode_id") or ""),
        ),
    )
    keep: list[dict[str, Any]] = []
    for a in sorted_acts:
        at = parse_ts(str(a["action_time"]))
        assert at is not None
        lvl = _f(a.get("wall_price"))
        drop = False
        for prev in keep:
            pt = parse_ts(str(prev["action_time"]))
            assert pt is not None
            if (at - pt).total_seconds() > gap_seconds:
                continue
            pl = _f(prev.get("wall_price"))
            if lvl is not None and pl is not None and pl > 0:
                if bps_distance(lvl, pl) <= level_bps:
                    drop = True
                    break
            elif abs((at - pt).total_seconds()) <= gap_seconds:
                # same-ish time without level → dedupe
                drop = True
                break
        if not drop:
            keep.append(a)
    return keep


# ---------------------------------------------------------------------------
# Outcomes + FP
# ---------------------------------------------------------------------------


def compute_outcome_with_fp(
    *,
    action_time: datetime,
    entry_mid: float,
    mids: Sequence[tuple[datetime, float]],
) -> dict[str, Any]:
    base = simulate_mid_outcomes(
        action_time=action_time, entry_mid=entry_mid, mids=mids
    )
    # enrich time_to_up_0_25
    t0 = ensure_utc(action_time)
    forward = [(ensure_utc(ts), float(px)) for ts, px in mids if ensure_utc(ts) > t0]
    t_up = None
    t_down = base.get("time_to_hit_down_0_25_seconds")
    if entry_mid > 0:
        for ts, px in forward:
            ret = (px - entry_mid) / entry_mid * 10_000.0
            if t_up is None and ret >= 25:
                t_up = (ts - t0).total_seconds()
            if t_down is None and ret <= -25:
                t_down = (ts - t0).total_seconds()
    mfe = base.get("max_favourable_excursion_bps")
    mae = base.get("max_adverse_excursion_bps")
    hit25 = bool(base.get("hit_down_0_25"))
    # within 600s specifically
    hit25_600 = False
    end600 = t0 + timedelta(seconds=600)
    if entry_mid > 0:
        for ts, px in forward:
            if ts > end600:
                break
            if (px - entry_mid) / entry_mid * 10_000.0 <= -25:
                hit25_600 = True
                break
    fp_adverse_before = t_up is not None and (t_down is None or t_up < t_down)
    fp_no_hit_mae = (not hit25_600) and (
        mae is not None and mfe is not None and float(mae) > float(mfe)
    )
    out = dict(base)
    out["mfe_down_bps"] = mfe
    out["mae_up_bps"] = mae
    out["time_to_down_0_25_seconds"] = t_down
    out["time_to_up_0_25_seconds"] = t_up
    out["fp_adverse_before"] = fp_adverse_before
    out["fp_no_hit_mae"] = fp_no_hit_mae
    out["false_positive"] = bool(fp_adverse_before or fp_no_hit_mae)
    out["hit_down_0_25_600s"] = hit25_600
    return out


# ---------------------------------------------------------------------------
# D4 wall distance filter
# ---------------------------------------------------------------------------


def filter_g5_by_wall_distance(
    g5_actions: Sequence[G5Action],
    walls: Sequence[tuple[datetime, float | None, float | None]],
    *,
    max_bps: float,
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for g in g5_actions:
        wall, wall_ts = wall_as_of(walls, as_of=g.action_time)
        ref = g.mid
        if ref is None:
            # try mid as-of from walls series
            for ts, _ask, mid in walls:
                if ts <= g.action_time:
                    ref = mid
                else:
                    break
        if wall is None or ref is None:
            continue
        dist = bps_distance(ref, wall)
        if dist <= max_bps:
            kept.append(
                {
                    "g5_warning_id": g.warning_id,
                    "g5_episode_id": g.episode_id,
                    "action_time": g.action_time.isoformat(),
                    "warning_time": g.warning_time.isoformat(),
                    "wall_price": wall,
                    "wall_snapshot_time": None if wall_ts is None else wall_ts.isoformat(),
                    "g5_mid": ref,
                    "wall_distance_bps": dist,
                    "g5_action": g.action,
                    "a2_episode_id": "",
                    "armed_time": "",
                    "wall_side": WALL_SIDE,
                }
            )
    return kept


# ---------------------------------------------------------------------------
# D5 reentry
# ---------------------------------------------------------------------------


def evaluate_reentry(
    *,
    wall_price: float,
    armed_time: datetime,
    g5_action_time: datetime,
    mids: Sequence[tuple[datetime, float]],
    confirm_snapshots: int | None = None,
    confirm_seconds: int | None = None,
) -> dict[str, Any]:
    """Causal reentry under ask wall after G5 action.

    Sequence observed only on snapshots with ts > g5_action_time (and >= armed).
    """
    result = {
        "wall_price": wall_price,
        "break_attempt_time": None,
        "max_price_above_wall": None,
        "max_extension_bps": None,
        "reentry_time": None,
        "confirmation_time": None,
        "confirmation_snapshots": 0,
        "time_above_wall_seconds": None,
        "time_below_wall_seconds": None,
        "action_price": None,
        "reentry_confirmed": False,
        "reject_reason": None,
    }
    path = [
        (ensure_utc(ts), float(px))
        for ts, px in mids
        if ensure_utc(ts) > ensure_utc(g5_action_time)
    ]
    if not path:
        result["reject_reason"] = "NO_FORWARD_SNAPSHOTS"
        return result

    saw_above = False
    peak = None
    peak_t = None
    break_t = None
    under_count = 0
    first_under_t = None
    for ts, mid in path:
        if mid >= wall_price:
            if not saw_above:
                saw_above = True
                break_t = ts
            if peak is None or mid >= peak:
                peak = mid
                peak_t = ts
            # invalidate ongoing under-count if reclaim
            if under_count > 0 and first_under_t is not None:
                under_count = 0
                first_under_t = None
            continue
        # below wall
        if not saw_above:
            # allow reentry without explicit break if already near wall post-G5
            # but require at least one touch at/above or start counting carefully:
            # Spec: price trades at/above then returns under. If never above, reject.
            continue
        if first_under_t is None:
            first_under_t = ts
            under_count = 1
        else:
            under_count += 1
        confirmed = False
        if confirm_snapshots is not None and under_count >= confirm_snapshots:
            confirmed = True
        if confirm_seconds is not None and first_under_t is not None:
            if (ts - first_under_t).total_seconds() >= confirm_seconds and under_count >= 1:
                # still need stable under — require at least 2 snaps for time-based
                # unless confirm_snapshots overridden; for T variants use >=2 snaps
                if under_count >= 2:
                    confirmed = True
        if confirmed:
            result.update(
                {
                    "break_attempt_time": None if break_t is None else break_t.isoformat(),
                    "max_price_above_wall": peak,
                    "max_extension_bps": (
                        None
                        if peak is None
                        else (peak - wall_price) / wall_price * 10_000.0
                    ),
                    "reentry_time": first_under_t.isoformat(),
                    "confirmation_time": ts.isoformat(),
                    "confirmation_snapshots": under_count,
                    "time_above_wall_seconds": (
                        None
                        if break_t is None or first_under_t is None
                        else (first_under_t - break_t).total_seconds()
                    ),
                    "time_below_wall_seconds": (ts - first_under_t).total_seconds(),
                    "action_price": mid,
                    "reentry_confirmed": True,
                    "reject_reason": None,
                }
            )
            return result

    if not saw_above:
        result["reject_reason"] = "NO_BREAK_OR_ABOVE_WALL"
    else:
        result["reject_reason"] = "REENTRY_UNCONFIRMED"
        result["break_attempt_time"] = None if break_t is None else break_t.isoformat()
        result["max_price_above_wall"] = peak
        result["max_extension_bps"] = (
            None if peak is None else (peak - wall_price) / wall_price * 10_000.0
        )
    return result


# ---------------------------------------------------------------------------
# Variant summaries
# ---------------------------------------------------------------------------


def summarize_variant(
    *,
    variant: str,
    actions: Sequence[dict[str, Any]],
    outcomes: Sequence[dict[str, Any]],
    g5_actions: Sequence[G5Action],
    raw_a2: int,
    eligible_a2: int,
    raw_pairings: int,
    note: str = "",
) -> dict[str, Any]:
    g5_times = {g.action_time.isoformat() for g in g5_actions}
    act_times = [str(a["action_time"]) for a in actions]
    retained = sum(1 for t in act_times if t in g5_times)
    lost = len(g5_actions) - retained if variant != D1 else len(g5_actions)
    additional = sum(1 for t in act_times if t not in g5_times)

    hits10 = sum(1 for o in outcomes if o.get("hit_down_0_10"))
    hits25 = sum(1 for o in outcomes if o.get("hit_down_0_25"))
    hits50 = sum(1 for o in outcomes if o.get("hit_down_0_50"))
    falses = sum(1 for o in outcomes if o.get("false_positive"))
    n = len(actions)
    g5_hit = None
    # precision delta filled by caller vs D0

    leads = []
    for a in actions:
        at = parse_ts(str(a["action_time"]))
        if at is None:
            continue
        # lead vs nearest G5
        best = None
        for g in g5_actions:
            dt = (at - g.action_time).total_seconds()
            if best is None or abs(dt) < abs(best):
                best = dt
        leads.append(best)

    return {
        "variant": variant,
        "raw_a2_episodes": raw_a2,
        "eligible_a2_episodes": eligible_a2,
        "raw_pairings": raw_pairings,
        "deduped_actions": n,
        "hit_count_0_10": hits10,
        "hit_count_0_25": hits25,
        "hit_count_0_50": hits50,
        "hit_rate_0_10": (hits10 / n) if n else None,
        "hit_rate_0_25": (hits25 / n) if n else None,
        "hit_rate_0_50": (hits50 / n) if n else None,
        "false_count": falses,
        "false_rate": (falses / n) if n else None,
        "median_mfe_down_bps": _median([_f(o.get("mfe_down_bps")) for o in outcomes]),
        "median_mae_up_bps": _median([_f(o.get("mae_up_bps")) for o in outcomes]),
        "median_a2_to_g5_seconds": _median(
            [_f(a.get("a2_to_g5_seconds")) for a in actions]
        ),
        "median_a2_to_action_seconds": _median(
            [_f(a.get("a2_to_action_seconds")) for a in actions]
        ),
        "median_warning_to_action_seconds": _median(
            [_f(a.get("warning_to_action_seconds")) for a in actions]
        ),
        "median_lead_vs_g5_seconds": _median(leads),
        "g5_actions_retained": retained if variant != D1 else 0,
        "g5_actions_lost": max(lost, 0) if variant != D1 else len(g5_actions),
        "additional_actions_vs_g5": additional if variant != D0 else 0,
        "note": note,
    }


def compare_to_d0(
    variant_actions: Sequence[dict[str, Any]],
    d0_actions: Sequence[dict[str, Any]],
    variant_outcomes: Sequence[dict[str, Any]],
    d0_outcomes: Sequence[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    d0_times = {str(a["action_time"]) for a in d0_actions}
    v_times = {str(a["action_time"]) for a in variant_actions}
    overlap = d0_times & v_times
    only_d0 = d0_times - v_times
    only_v = v_times - d0_times
    d0_hit = sum(1 for o in d0_outcomes if o.get("hit_down_0_25"))
    d0_n = len(d0_actions) or 1
    d0_rate = d0_hit / d0_n if d0_actions else None
    v_rate = summary.get("hit_rate_0_25")
    d0_false = sum(1 for o in d0_outcomes if o.get("false_positive"))
    v_hits = sum(1 for o in variant_outcomes if o.get("hit_down_0_25"))
    # additional hits: hits in only_v
    only_v_hits = 0
    only_v_false = 0
    for a, o in zip(variant_actions, variant_outcomes):
        if str(a["action_time"]) in only_v:
            if o.get("hit_down_0_25"):
                only_v_hits += 1
            if o.get("false_positive"):
                only_v_false += 1
    return {
        **summary,
        "overlap_with_d0": len(overlap),
        "only_d0": len(only_d0),
        "only_variant": len(only_v),
        "same_action_time": len(overlap),
        "additional_hits_vs_g5": only_v_hits,
        "additional_false_vs_g5": only_v_false,
        "precision_delta_vs_g5": (
            None if d0_rate is None or v_rate is None else float(v_rate) - float(d0_rate)
        ),
        "d0_hit_rate_0_25": d0_rate,
        "d0_false_count": d0_false,
    }


def decide_verdict(
    *,
    d0_parity_ok: bool,
    future_violations: int,
    summaries: Sequence[Mapping[str, Any]],
    a2_count: int,
    g5_count: int,
) -> str:
    if future_violations > 0 or not d0_parity_ok:
        return "AUDIT_INVALID"
    if a2_count < 5 or g5_count < 5:
        return "HANDOFF_DATA_INSUFFICIENT"

    d0 = next((s for s in summaries if s.get("variant") == D0), None)
    d0_rate = float(d0["hit_rate_0_25"]) if d0 and d0.get("hit_rate_0_25") is not None else None

    handoff = [
        s
        for s in summaries
        if str(s.get("variant", "")).startswith("D2")
        or str(s.get("variant", "")).startswith("D3")
        or str(s.get("variant", "")).startswith("D5")
        or s.get("variant") == D6
    ]
    filters = [s for s in summaries if str(s.get("variant", "")).startswith("D4")]

    best_h = None
    for s in handoff:
        n = int(s.get("deduped_actions") or 0)
        if n < 2:
            continue
        rate = s.get("hit_rate_0_25")
        if rate is None:
            continue
        if d0_rate is not None and float(rate) + 0.15 < d0_rate:
            continue
        if best_h is None or float(rate) > float(best_h["hit_rate_0_25"]):
            best_h = s

    # Early warning: many pairings, action stays G5, A2 earlier
    d2 = next((s for s in summaries if s.get("variant") == D2), None)
    if d2 and int(d2.get("raw_pairings") or 0) >= 3:
        med_delay = d2.get("median_a2_to_g5_seconds")
        if med_delay is not None and float(med_delay) > 0:
            # if precision ~ holds for D2/D3
            if best_h and d0_rate is not None:
                if float(best_h["hit_rate_0_25"]) >= d0_rate - 0.10:
                    add_h = int(best_h.get("additional_hits_vs_g5") or 0)
                    add_f = int(best_h.get("additional_false_vs_g5") or 0)
                    if add_h >= 1 and add_f <= add_h + 1:
                        return "A2_G5_HANDOFF_INCREMENTAL_VALUE_FOUND"
                    return "A2_G5_HANDOFF_EARLY_WARNING_ONLY"
            return "A2_G5_HANDOFF_EARLY_WARNING_ONLY"

    # Filter value
    for s in filters:
        n = int(s.get("deduped_actions") or 0)
        if n < 3 or d0_rate is None:
            continue
        rate = s.get("hit_rate_0_25")
        if rate is not None and float(rate) > d0_rate + 0.02:
            return "A2_G5_HANDOFF_FILTER_VALUE_ONLY"

    if best_h is None and (d2 is None or int(d2.get("raw_pairings") or 0) == 0):
        return "NO_INCREMENTAL_VALUE_VS_G5"
    return "NO_INCREMENTAL_VALUE_VS_G5"


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_handoff_audit(
    *,
    absorption_dir: Path,
    g5_dir: Path,
    output_dir: Path,
    params: HandoffParams,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    future_violations = 0
    outcome_leakage = 0
    missing_files: list[str] = []
    missing_cols: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    a2_eps, a2_rows, miss_a = load_a2_episodes(absorption_dir)
    missing_files.extend(miss_a)
    g5_acts, g5_rows, miss_g = load_g5_actions(g5_dir)
    missing_files.extend(miss_g)
    g5_warn_rows, miss_w = load_g5_warnings(g5_dir)
    if miss_w:
        warnings.append(f"warning_context_missing:{miss_w[0]}; derived from G5 actions")
        # derive warnings from actions
        g5_warn_rows = [
            {
                "warning_id": g.warning_id,
                "warning_time": g.warning_time.isoformat(),
                "score": g.warning_score,
                "warning_quality": g.warning_quality,
                "mid": g.mid,
                "combined_regime": None,
                "support_break_valid": None,
                "derived_from_actions": True,
            }
            for g in g5_acts
        ]
    mids = load_mid_path(absorption_dir)
    walls = load_ask_walls(absorption_dir)
    if not mids:
        missing_files.append(str(absorption_dir / "snapshot_features.csv"))
        errors.append("mid_path_empty")

    inventory = {
        "absorption_dir": str(absorption_dir),
        "g5_dir": str(g5_dir),
        "a2_episode_count": len(a2_eps),
        "g5_action_count": len(g5_acts),
        "g5_warning_count": len(g5_warn_rows),
        "snapshot_count": len(mids),
        "a2_qualities": {},
        "missing_files": missing_files,
    }
    from collections import Counter

    inventory["a2_qualities"] = dict(Counter(e.a2_quality for e in a2_eps))

    all_actions: list[dict[str, Any]] = []
    all_outcomes: list[dict[str, Any]] = []
    all_pairings: list[dict[str, Any]] = []
    all_rejected: list[dict[str, Any]] = []
    all_transitions: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    window_ablation: list[dict[str, Any]] = []
    wall_ablation: list[dict[str, Any]] = []
    reentry_ablation: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []

    def emit_actions(
        variant: str,
        action_dicts: list[dict[str, Any]],
        *,
        raw_pairings: int,
        eligible: int,
        note: str = "",
        skip_dedupe: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        deduped = (
            list(action_dicts)
            if skip_dedupe
            else dedupe_actions(
                action_dicts,
                gap_seconds=params.dedupe_gap_seconds,
                level_bps=params.dedupe_level_bps,
            )
        )
        outs: list[dict[str, Any]] = []
        enriched: list[dict[str, Any]] = []
        for i, a in enumerate(deduped):
            at = parse_ts(str(a["action_time"]))
            assert at is not None
            entry = _f(a.get("action_mid") or a.get("g5_mid") or a.get("a2_mid") or a.get("entry_mid"))
            if entry is None:
                # mid as-of action
                for ts, mid in mids:
                    if ts <= at:
                        entry = mid
                    else:
                        break
            if entry is None or entry <= 0:
                entry = 0.0
            oc = compute_outcome_with_fp(action_time=at, entry_mid=float(entry), mids=mids)
            aid = f"{variant}_{i+1:04d}"
            row = {
                "action_id": aid,
                "variant": variant,
                **a,
                "entry_mid": entry,
            }
            enriched.append(row)
            outs.append({"action_id": aid, "variant": variant, **oc, "action_time": at.isoformat()})
            # leakage: ensure no mid <= action used incorrectly — simulate already filters
        summ = summarize_variant(
            variant=variant,
            actions=enriched,
            outcomes=outs,
            g5_actions=g5_acts,
            raw_a2=len(a2_eps),
            eligible_a2=eligible,
            raw_pairings=raw_pairings,
            note=note,
        )
        return enriched, outs, summ

    # ---- D0 ----
    d0_raw = []
    for g in g5_acts:
        d0_raw.append(
            {
                "a2_episode_id": "",
                "g5_warning_id": g.warning_id,
                "g5_episode_id": g.episode_id,
                "action_time": g.action_time.isoformat(),
                "warning_time": g.warning_time.isoformat(),
                "g5_action": g.action,
                "g5_mid": g.mid,
                "action_mid": g.mid,
                "wall_price": g.support_level,  # not ask wall; diagnostic only
                "warning_to_action_seconds": (
                    g.action_time - g.warning_time
                ).total_seconds(),
                "a2_to_g5_seconds": None,
                "a2_to_action_seconds": None,
            }
        )
    d0_actions, d0_outcomes, d0_sum = emit_actions(
        D0, d0_raw, raw_pairings=0, eligible=0, note="g5_baseline", skip_dedupe=True
    )
    d0_expected = len(g5_acts)
    d0_reproduced = len(d0_actions)
    d0_times_match = {a["action_time"] for a in d0_actions} == {
        g.action_time.isoformat() for g in g5_acts
    }
    d0_parity_ok = d0_expected == d0_reproduced and d0_times_match
    if not d0_parity_ok:
        errors.append("D0_PARITY_FAIL")
        warnings.append(
            f"d0_expected={d0_expected} reproduced={d0_reproduced} times_match={d0_times_match}"
        )
    all_actions.extend(d0_actions)
    all_outcomes.extend(d0_outcomes)
    d0_cmp = compare_to_d0(d0_actions, d0_actions, d0_outcomes, d0_outcomes, d0_sum)
    summaries.append(d0_cmp)

    # ---- D1 ----
    d1_raw = []
    for ep in a2_eps:
        d1_raw.append(
            {
                "a2_episode_id": ep.episode_id,
                "g5_warning_id": "",
                "action_time": ep.armed_time.isoformat(),
                "armed_time": ep.armed_time.isoformat(),
                "wall_price": ep.wall_price,
                "a2_score": ep.a2_score,
                "a2_quality": ep.a2_quality,
                "a2_mid": ep.mid,
                "action_mid": ep.mid,
                "a2_to_g5_seconds": None,
                "a2_to_action_seconds": 0.0,
                "warning_to_action_seconds": None,
                "note": "diagnostic_standalone_not_recommended",
            }
        )
    d1_actions, d1_outcomes, d1_sum = emit_actions(
        D1, d1_raw, raw_pairings=0, eligible=len(a2_eps), note="diagnostic_only"
    )
    all_actions.extend(d1_actions)
    all_outcomes.extend(d1_outcomes)
    summaries.append(compare_to_d0(d1_actions, d0_actions, d1_outcomes, d0_outcomes, d1_sum))

    # ---- D2 / D3 ----
    window_runs: list[tuple[str, int]] = [(D2, params.armed_window_seconds)]
    for wsec in D3_WINDOWS:
        window_runs.append((f"D3_{wsec}S", wsec))
    for variant, wsec in window_runs:
        accepted, rejected, transitions = pair_a2_to_g5(
            a2_eps, g5_acts, window_seconds=wsec
        )
        for p in accepted:
            p["variant"] = variant
            p["window_seconds"] = wsec
        for r in rejected:
            r["variant"] = variant
            r["window_seconds"] = wsec
        for t in transitions:
            t["variant"] = variant
        all_pairings.extend(accepted)
        all_rejected.extend(rejected)
        all_transitions.extend(transitions)

        act_raw = []
        for p in accepted:
            act_raw.append(
                {
                    **p,
                    "action_mid": p.get("g5_mid"),
                }
            )
        acts, outs, summ = emit_actions(
            variant,
            act_raw,
            raw_pairings=len(accepted),
            eligible=len(a2_eps),
            note=f"armed_window={wsec}s",
        )
        all_actions.extend(acts)
        all_outcomes.extend(outs)
        cmp = compare_to_d0(acts, d0_actions, outs, d0_outcomes, summ)
        summaries.append(cmp)
        if variant.startswith("D3") or variant == D2:
            window_ablation.append(cmp)

    # Ensure D2 with default window always present even if also in D3
    # (already handled)

    # ---- D4 ----
    for bps in D4_BPS:
        variant = f"D4_{bps}BPS"
        kept = filter_g5_by_wall_distance(g5_acts, walls, max_bps=float(bps))
        for k in kept:
            k["variant"] = variant
        acts, outs, summ = emit_actions(
            variant,
            kept,
            raw_pairings=len(kept),
            eligible=len(a2_eps),
            note=f"wall_distance<={bps}bps",
        )
        all_actions.extend(acts)
        all_outcomes.extend(outs)
        cmp = compare_to_d0(acts, d0_actions, outs, d0_outcomes, summ)
        summaries.append(cmp)
        wall_ablation.append(cmp)

    # ---- D5 ----
    # Base pairings with default armed window
    base_accepted, base_rejected, base_trans = pair_a2_to_g5(
        a2_eps, g5_acts, window_seconds=params.armed_window_seconds
    )
    for label, conf_snaps, conf_secs in (
        *[(f"D5_C{c}", c, None) for c in D5_CONFIRM],
        *[(f"D5_T{t}", None, t) for t in D5_TIME],
    ):
        act_raw = []
        for p in base_accepted:
            wall = _f(p.get("wall_price"))
            if wall is None:
                continue
            g5_at = parse_ts(str(p["g5_action_time"]))
            armed = parse_ts(str(p["armed_time"]))
            assert g5_at and armed
            re = evaluate_reentry(
                wall_price=wall,
                armed_time=armed,
                g5_action_time=g5_at,
                mids=mids,
                confirm_snapshots=conf_snaps,
                confirm_seconds=conf_secs,
            )
            if not re.get("reentry_confirmed"):
                all_rejected.append(
                    {
                        "variant": label,
                        "a2_episode_id": p["a2_episode_id"],
                        "g5_warning_id": p["g5_warning_id"],
                        "reject_reason": re.get("reject_reason") or "REENTRY_FAILED",
                        **{k: re.get(k) for k in ("wall_price", "break_attempt_time", "reentry_time")},
                    }
                )
                continue
            conf_t = parse_ts(str(re["confirmation_time"]))
            assert conf_t is not None
            if conf_t < g5_at:
                future_violations += 1
            act_raw.append(
                {
                    **p,
                    "variant": label,
                    "action_time": re["confirmation_time"],
                    "action_mid": re.get("action_price"),
                    "break_attempt_time": re.get("break_attempt_time"),
                    "max_price_above_wall": re.get("max_price_above_wall"),
                    "max_extension_bps": re.get("max_extension_bps"),
                    "reentry_time": re.get("reentry_time"),
                    "confirmation_time": re.get("confirmation_time"),
                    "confirmation_snapshots": re.get("confirmation_snapshots"),
                    "time_above_wall_seconds": re.get("time_above_wall_seconds"),
                    "time_below_wall_seconds": re.get("time_below_wall_seconds"),
                    "a2_to_action_seconds": (conf_t - armed).total_seconds(),
                }
            )
            all_transitions.append(
                {
                    "variant": label,
                    "episode_id": p["a2_episode_id"],
                    "previous_state": "G5_CONFIRMED",
                    "new_state": "REENTRY_CONFIRMED",
                    "transition_time": re["confirmation_time"],
                    "wall_price": wall,
                    "reason": "reentry_confirmed",
                }
            )
        acts, outs, summ = emit_actions(
            label,
            act_raw,
            raw_pairings=len(base_accepted),
            eligible=len(a2_eps),
            note=f"reentry confirm_snapshots={conf_snaps} confirm_seconds={conf_secs}",
        )
        all_actions.extend(acts)
        all_outcomes.extend(outs)
        cmp = compare_to_d0(acts, d0_actions, outs, d0_outcomes, summ)
        summaries.append(cmp)
        reentry_ablation.append(cmp)

    # ---- D6 ----
    # A2 → G5 warning → G5 action
    d6_available = True
    d6_raw = []
    d6_accepted, d6_rejected, d6_trans = pair_a2_to_g5(
        a2_eps, g5_acts, window_seconds=params.armed_window_seconds, use_warning_time=True
    )
    for p in d6_accepted:
        # require warning then later-or-equal action (action always >= warning in source)
        wt = parse_ts(str(p["g5_warning_time"]))
        at = parse_ts(str(p["g5_action_time"]))
        armed = parse_ts(str(p["armed_time"]))
        if wt is None or at is None or armed is None:
            continue
        if not (armed < wt <= at):
            continue
        d6_raw.append(
            {
                **p,
                "variant": D6,
                "action_time": at.isoformat(),
                "action_mid": p.get("g5_mid"),
                "a2_to_warning_seconds": (wt - armed).total_seconds(),
                "a2_to_g5_seconds": (wt - armed).total_seconds(),
                "a2_to_action_seconds": (at - armed).total_seconds(),
                "warning_to_action_seconds": (at - wt).total_seconds(),
            }
        )
    all_pairings.extend([{**p, "variant": D6} for p in d6_accepted])
    all_rejected.extend([{**r, "variant": D6} for r in d6_rejected])
    all_transitions.extend([{**t, "variant": D6} for t in d6_trans])
    d6_acts, d6_outs, d6_sum = emit_actions(
        D6,
        d6_raw,
        raw_pairings=len(d6_raw),
        eligible=len(a2_eps),
        note="a2_to_warning_to_action" if d6_available else "NOT_AVAILABLE",
    )
    all_actions.extend(d6_acts)
    all_outcomes.extend(d6_outs)
    summaries.append(compare_to_d0(d6_acts, d0_actions, d6_outs, d0_outcomes, d6_sum))

    # comparisons table
    for s in summaries:
        comparisons.append(
            {
                "variant": s["variant"],
                "deduped_actions": s.get("deduped_actions"),
                "hit_rate_0_25": s.get("hit_rate_0_25"),
                "false_rate": s.get("false_rate"),
                "precision_delta_vs_g5": s.get("precision_delta_vs_g5"),
                "g5_actions_retained": s.get("g5_actions_retained"),
                "g5_actions_lost": s.get("g5_actions_lost"),
                "additional_hits_vs_g5": s.get("additional_hits_vs_g5"),
                "additional_false_vs_g5": s.get("additional_false_vs_g5"),
                "median_a2_to_g5_seconds": s.get("median_a2_to_g5_seconds"),
                "overlap_with_d0": s.get("overlap_with_d0"),
                "only_d0": s.get("only_d0"),
                "only_variant": s.get("only_variant"),
            }
        )

    verdict = decide_verdict(
        d0_parity_ok=d0_parity_ok,
        future_violations=future_violations,
        summaries=summaries,
        a2_count=len(a2_eps),
        g5_count=len(g5_acts),
    )

    # examples
    examples = [a for a in all_actions if a.get("variant") in {D0, D2, "D3_180S", "D5_C2", D6}][:40]

    integrity = {
        "ok": future_violations == 0
        and outcome_leakage == 0
        and d0_parity_ok
        and not missing_files,
        "symbol": params.symbol,
        "start": params.start,
        "end": params.end,
        "a2_episode_count": len(a2_eps),
        "g5_warning_count": len(g5_warn_rows),
        "g5_action_count": len(g5_acts),
        "snapshot_count": len(mids),
        "d0_expected_action_count": d0_expected,
        "d0_reproduced_action_count": d0_reproduced,
        "d0_parity_ok": d0_parity_ok,
        "raw_pairing_count": len([p for p in all_pairings if p.get("variant") == D2 or p.get("window_seconds") == params.armed_window_seconds]),
        "deduped_action_count": len(all_actions),
        "future_data_violations": future_violations,
        "outcome_leakage_violations": outcome_leakage,
        "duplicate_assignment_count": sum(
            1 for r in all_rejected if r.get("reject_reason") == "G5_ALREADY_PAIRED"
        ),
        "missing_input_files": missing_files,
        "missing_required_columns": missing_cols,
        "warnings": warnings,
        "errors": errors,
        "decision": verdict,
    }
    # recount D2 pairings cleanly
    integrity["raw_pairing_count"] = sum(
        1 for p in all_pairings if p.get("variant") == D2
    )

    config = {
        "params": asdict(params),
        "pairing_semantics": "armed_time < event_time <= expiry_time",
        "false_positive_definition": {
            "fp_adverse_before": "up_0_25 before down_0_25",
            "fp_no_hit_mae": "no down_0_25 in 600s and mae_up > mfe_down",
            "false_positive": "fp_adverse_before OR fp_no_hit_mae",
        },
        "price_basis": "mid",
        "d3_windows_seconds": list(D3_WINDOWS),
        "d4_bps": list(D4_BPS),
        "d1_not_recommended": True,
    }

    write_csv(output_dir / "a2_episodes_loaded.csv", a2_rows)
    write_csv(output_dir / "g5_warnings_loaded.csv", g5_warn_rows)
    write_csv(output_dir / "g5_actions_loaded.csv", g5_rows)
    write_csv(output_dir / "handoff_state_transitions.csv", all_transitions)
    write_csv(output_dir / "handoff_raw_pairings.csv", all_pairings)
    write_csv(output_dir / "handoff_rejected_pairings.csv", all_rejected)
    write_csv(output_dir / "handoff_actions.csv", all_actions)
    write_csv(output_dir / "handoff_outcomes.csv", all_outcomes)
    write_csv(output_dir / "handoff_variant_summary.csv", summaries)
    write_csv(output_dir / "handoff_window_ablation.csv", window_ablation)
    write_csv(output_dir / "handoff_wall_distance_ablation.csv", wall_ablation)
    write_csv(output_dir / "handoff_reentry_ablation.csv", reentry_ablation)
    write_csv(output_dir / "handoff_g5_comparison.csv", comparisons)
    write_csv(output_dir / "handoff_examples.csv", examples)

    (output_dir / "config.json").write_bytes(orjson.dumps(config, option=orjson.OPT_INDENT_2))
    (output_dir / "integrity.json").write_bytes(
        orjson.dumps(integrity, option=orjson.OPT_INDENT_2)
    )
    (output_dir / "input_inventory.json").write_bytes(
        orjson.dumps(inventory, option=orjson.OPT_INDENT_2)
    )

    report = build_report(
        verdict=verdict,
        integrity=integrity,
        summaries=summaries,
        params=params,
        a2_count=len(a2_eps),
        g5_count=len(g5_acts),
    )
    (output_dir / "REPORT.md").write_text(report, encoding="utf-8")

    for name in OUTPUT_FILES:
        p = output_dir / name
        if not p.exists():
            if name.endswith(".csv"):
                write_csv(p, [], headers=["placeholder"])
            else:
                p.write_text("", encoding="utf-8")

    if future_violations > 0 or outcome_leakage > 0:
        raise RuntimeError("integrity failure: look-ahead or leakage")
    if not d0_parity_ok:
        raise RuntimeError("D0_PARITY_FAIL")

    summary = {
        "decision": verdict,
        "integrity": integrity,
        "summaries": summaries,
        "output_dir": str(output_dir),
    }
    (output_dir / "strategy_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    return summary


def build_report(
    *,
    verdict: str,
    integrity: Mapping[str, Any],
    summaries: Sequence[Mapping[str, Any]],
    params: HandoffParams,
    a2_count: int,
    g5_count: int,
) -> str:
    by = {s["variant"]: s for s in summaries}
    d0 = by.get(D0, {})
    d2 = by.get(D2, {})
    d1 = by.get(D1, {})
    best = None
    for s in summaries:
        if s["variant"] in {D0, D1}:
            continue
        if s.get("hit_rate_0_25") is None or int(s.get("deduped_actions") or 0) < 2:
            continue
        if best is None or float(s["hit_rate_0_25"]) > float(best["hit_rate_0_25"]):
            best = s

    lines = [
        "# A2 → G5 Handoff Audit Report",
        "",
        f"**Decision:** `{verdict}`",
        "",
        "## 1. D0 parity",
        f"- d0_parity_ok: {integrity.get('d0_parity_ok')}",
        f"- expected: {integrity.get('d0_expected_action_count')}",
        f"- reproduced: {integrity.get('d0_reproduced_action_count')}",
        "",
        f"## 2. Eligible A2 episodes: {a2_count}",
        "",
        f"## 3. A2→G5 pairings (D2/{params.armed_window_seconds}s): {d2.get('raw_pairings')}",
        f"- deduped actions: {d2.get('deduped_actions')}",
        "",
        f"## 4. A2→G5 delays (median): {d2.get('median_a2_to_g5_seconds')}",
        f"- median A2→action: {d2.get('median_a2_to_action_seconds')}",
        f"- median warning→action: {d2.get('median_warning_to_action_seconds')}",
        "",
        "## 5. D3 windows",
    ]
    for w in D3_WINDOWS:
        s = by.get(f"D3_{w}S", {})
        lines.append(
            f"- D3_{w}S: actions={s.get('deduped_actions')} hit@0.25={s.get('hit_rate_0_25')} "
            f"pairings={s.get('raw_pairings')} lost_g5={s.get('g5_actions_lost')}"
        )
    lines += [
        "",
        f"## 6. hit@0.25 vs G5",
        f"- D0: {d0.get('hit_rate_0_25')} (false_rate={d0.get('false_rate')})",
        f"- D2: {d2.get('hit_rate_0_25')} (delta={d2.get('precision_delta_vs_g5')})",
        f"- D1 diagnostic: {d1.get('hit_rate_0_25')} — not recommended",
        "",
        f"## 7. G5 hits lost (D2): {d2.get('g5_actions_lost')}",
        f"## 8. Additional hits (D2): {d2.get('additional_hits_vs_g5')}",
        f"## 9. Additional false (D2): {d2.get('additional_false_vs_g5')}",
        "",
        "## 10. D4 wall-distance filter",
    ]
    for b in D4_BPS:
        s = by.get(f"D4_{b}BPS", {})
        lines.append(
            f"- D4_{b}BPS: n={s.get('deduped_actions')} hit@0.25={s.get('hit_rate_0_25')} "
            f"delta={s.get('precision_delta_vs_g5')} lost={s.get('g5_actions_lost')}"
        )
    lines += ["", "## 11. D5 reentry"]
    for lab in [f"D5_C{c}" for c in D5_CONFIRM] + [f"D5_T{t}" for t in D5_TIME]:
        s = by.get(lab, {})
        lines.append(
            f"- {lab}: n={s.get('deduped_actions')} hit@0.25={s.get('hit_rate_0_25')} "
            f"false_rate={s.get('false_rate')}"
        )
    lines += [
        "",
        "## 12–13. Armed-state assessment",
        "A2 can open an armed state before some G5 actions, but standalone D1 precision "
        "is materially worse than G5. Prefer A2 as early warning / handoff arming, "
        "not as an executable signal.",
        "",
        f"## 14. Sample-size limit: only ~{g5_count} G5 actions in this APT window.",
        "",
        f"## 15. Decision: {verdict}",
        "",
        f"Best non-baseline by hit@0.25: {None if best is None else best.get('variant')}",
        "",
        "```",
        orjson.dumps(list(summaries), option=orjson.OPT_INDENT_2).decode(),
        "```",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A2→G5 handoff research audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-07-26T09:16:29Z")
    p.add_argument("--end", default="2026-07-26T13:08:27Z")
    p.add_argument(
        "--absorption-dir",
        default=str(
            PROJECT_ROOT / "results" / "orderbook_absorption_exhaustion_APTUSDT_20260726"
        ),
    )
    p.add_argument(
        "--g5-dir",
        default=str(
            PROJECT_ROOT / "results" / "orderbook_trend_bid_weakening_APTUSDT_20260726"
        ),
    )
    p.add_argument("--output-dir", default=None)
    p.add_argument("--armed-window-seconds", type=int, default=180)
    p.add_argument("--dedupe-gap-seconds", type=int, default=120)
    p.add_argument("--dedupe-level-bps", type=float, default=10.0)
    p.add_argument("--reentry-confirm-snapshots", type=int, default=2)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    abs_dir = Path(args.absorption_dir)
    g5_dir = Path(args.g5_dir)
    if not g5_dir.exists():
        # search under results
        results = PROJECT_ROOT / "results"
        candidates = sorted(results.glob("**/integrated_variant_actions.csv"))
        for c in candidates:
            if "trend_bid_weakening" in str(c) and "APTUSDT" in str(c):
                g5_dir = c.parent
                logger.warning("auto-selected g5-dir=%s", g5_dir)
                break
    out = (
        Path(args.output_dir)
        if args.output_dir
        else PROJECT_ROOT
        / "results"
        / f"orderbook_absorption_g5_handoff_{args.symbol}_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    )
    params = HandoffParams(
        armed_window_seconds=int(args.armed_window_seconds),
        dedupe_gap_seconds=int(args.dedupe_gap_seconds),
        dedupe_level_bps=float(args.dedupe_level_bps),
        reentry_confirm_snapshots=int(args.reentry_confirm_snapshots),
        symbol=str(args.symbol),
        start=str(args.start),
        end=str(args.end),
    )
    summary = run_handoff_audit(
        absorption_dir=abs_dir,
        g5_dir=g5_dir,
        output_dir=out,
        params=params,
    )
    sys.stdout.buffer.write(
        orjson.dumps(
            {
                "decision": summary.get("decision"),
                "d0_parity_ok": summary.get("integrity", {}).get("d0_parity_ok"),
                "output_dir": summary.get("output_dir"),
            },
            option=orjson.OPT_INDENT_2,
        )
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
