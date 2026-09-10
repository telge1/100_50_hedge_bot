"""Short-horizon breach outcomes within continuous coverage; rest CENSORED."""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from . import (
    ASK_WALL_BREACH,
    BREACH_OUTCOME_OFFSETS_S,
    EPOCH4_COVERAGE_END,
    ORIGINAL_WALL_PRICE,
    TICK_SIZE,
)
from .segments import EpochSegment, book_state_at


CENSORED = "CENSORED_BY_EPOCH_BOUNDARY"


def _side(price: float | None, wall: float) -> str | None:
    if price is None:
        return None
    if float(price) >= float(wall):
        return "ATTACK_OR_BREACH"
    return "DEFENDER"


def load_wf_index(path: Path) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k = row.get("feature_available_at") or row.get("timestamp")
            if not k:
                continue
            idx[str(k).replace("+00:00", "Z")] = row
    return idx


def _asof_wf(idx: dict[str, dict[str, Any]], decision: Any) -> dict[str, Any] | None:
    dt = _as_dt(decision)
    last = None
    for k in sorted(idx.keys(), key=_as_dt):
        if _as_dt(k) <= dt:
            last = idx[k]
        else:
            break
    return last


def build_breach_outcomes(
    *,
    payload: dict[str, Any],
    segment: EpochSegment,
    wall_flow_csv: Path,
    liquidity_rows: list[dict[str, Any]],
    wall_price: float = ORIGINAL_WALL_PRICE,
    offsets_s: tuple[int, ...] = BREACH_OUTCOME_OFFSETS_S,
) -> list[dict[str, Any]]:
    breach = _as_dt(ASK_WALL_BREACH)
    cov_end = segment.coverage_end
    wf = load_wf_index(wall_flow_csv)
    liq_by_t = {_as_dt(r["timestamp"]): r for r in liquidity_rows}
    out: list[dict[str, Any]] = []

    for off in offsets_s:
        decision = breach + timedelta(seconds=int(off))
        if decision > cov_end:
            out.append(
                {
                    "anchor": "BREACH",
                    "offset_s": int(off),
                    "decision_time": format_utc_z(decision),
                    "status": CENSORED,
                    "reason": "beyond_epoch4_continuous_coverage",
                }
            )
            continue
        book = book_state_at(payload, until=decision + timedelta(microseconds=1), require_epoch=None)
        if book.get("replay_epoch") != segment.replay_epoch:
            out.append(
                {
                    "anchor": "BREACH",
                    "offset_s": int(off),
                    "decision_time": format_utc_z(decision),
                    "status": CENSORED,
                    "reason": "replay_epoch_change",
                }
            )
            continue
        bb, ba = book.get("best_bid"), book.get("best_ask")
        mid = 0.5 * (bb + ba) if bb is not None and ba is not None else None
        # micro approx without sizes if missing — use mid only for side; sizes from book
        bsz = float(book["bids"][bb]) if bb in book["bids"] else None
        asz = float(book["asks"][ba]) if ba in book["asks"] else None
        micro = None
        if bb is not None and ba is not None and bsz and asz and (bsz + asz) > 0:
            micro = (ba * bsz + bb * asz) / (bsz + asz)
        # nearest liq row
        liq = None
        for t, r in sorted(liq_by_t.items()):
            if t <= decision:
                liq = r
            else:
                break
        wf_row = _asof_wf(wf, decision)
        reclaim = bool(mid is not None and mid < wall_price)
        out.append(
            {
                "anchor": "BREACH",
                "offset_s": int(off),
                "decision_time": format_utc_z(decision),
                "status": "OK",
                "replay_epoch": book.get("replay_epoch"),
                "price_side": _side(mid, wall_price),
                "microprice_side": _side(micro, wall_price),
                "midprice": mid,
                "microprice": micro,
                "distance_to_original_wall_ticks": None
                if mid is None
                else (float(mid) - float(wall_price)) / TICK_SIZE,
                "nearest_active_ask_wall": None if not liq else liq.get("nearest_relevant_ask_price"),
                "nearest_active_ask_qty": None if not liq else liq.get("nearest_relevant_ask_qty"),
                "qdh_base": None if not wf_row else _f(wf_row.get("qdh_base")),
                "aggressor_persistence": None if not wf_row else _f(wf_row.get("persistence_ratio")),
                "new_ask_liquidity_total_band": None if not liq else liq.get("total_ask_qty"),
                "quantity_above_original_wall": None if not liq else liq.get("quantity_above_original_wall"),
                "reclaim": reclaim,
                "look_ahead": False,
                "coverage_ok": True,
            }
        )

    # Explicit longer horizons censored
    for off in (30, 60, 120):
        out.append(
            {
                "anchor": "BREACH",
                "offset_s": off,
                "decision_time": format_utc_z(breach + timedelta(seconds=off)),
                "status": CENSORED,
                "reason": "CENSORED_BY_EPOCH_BOUNDARY",
            }
        )
    return out


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
