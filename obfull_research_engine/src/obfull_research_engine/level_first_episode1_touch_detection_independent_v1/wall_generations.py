"""Wall-generation identity from reconstructed Full-OB level stream."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..drilldown.aggregation_100ms import _as_dt, reconstruct_book_asof_exclusive
from ..timeparse import format_utc_z
from . import WALL_PRICE, WALL_SIDE


class CoverageError(RuntimeError):
    """Required checkpoint / coverage missing — must not continue silently."""


@dataclass(frozen=True)
class WallGeneration:
    wall_side: str
    wall_price: float
    replay_epoch: int
    generation_index: int
    generation_start_exchange_time: datetime
    generation_end_exchange_time: datetime | None
    start_source_event_id: str | None
    start_update_id: int | None
    start_seq: int | None

    def generation_id(self) -> str:
        raw = (
            f"{self.wall_side}|{self.wall_price:.10f}|epoch={self.replay_epoch}|"
            f"gen={self.generation_index}|start={format_utc_z(self.generation_start_exchange_time)}"
        )
        return "wg_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def wall_id(self, episode_id: str) -> str:
        raw = f"{episode_id}|{self.generation_id()}"
        return "w_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def alive_at(self, ts: datetime) -> bool:
        if ts < self.generation_start_exchange_time:
            return False
        if self.generation_end_exchange_time is not None and ts >= self.generation_end_exchange_time:
            return False
        return True


def _qty_at(book_side: dict[float, float], price: float) -> float:
    for px, qty in (book_side or {}).items():
        if abs(float(px) - float(price)) <= 1e-9:
            return float(qty)
    return 0.0


def build_wall_generations(
    *,
    level_changes: list[dict[str, Any]],
    book_resets: list[dict[str, Any]] | None,
    initial_asks: dict[float, float],
    initial_bids: dict[float, float],
    initial_replay_epoch: int | None,
    window_start: datetime,
    wall_price: float = WALL_PRICE,
    wall_side: str = WALL_SIDE,
) -> list[WallGeneration]:
    """Generations = contiguous positive-size streaks at (side, price) within an epoch.

    A new generation starts when size transitions from <=0 to >0 (including after reset
    clearing), or when replay_epoch changes while size is positive.
    """
    events: list[tuple[datetime, int, str, dict[str, Any]]] = []
    for r in book_resets or []:
        events.append((_as_dt(r["event_time"]), int(r.get("apply_order") or -1), "reset", r))
    for r in level_changes:
        if str(r.get("side")) != wall_side:
            continue
        if abs(float(r["price"]) - float(wall_price)) > 1e-9:
            continue
        events.append((_as_dt(r["event_time"]), int(r.get("apply_order") or 0), "lc", r))
    events.sort(key=lambda x: (x[0].timestamp(), x[1], x[2]))

    book_asks = {float(k): float(v) for k, v in (initial_asks or {}).items() if v and float(v) > 0}
    book_bids = {float(k): float(v) for k, v in (initial_bids or {}).items() if v and float(v) > 0}
    epoch = int(initial_replay_epoch or 0)
    qty = _qty_at(book_asks if wall_side == "ask" else book_bids, wall_price)

    gens: list[WallGeneration] = []
    gen_index = 0
    open_gen: dict[str, Any] | None = None

    def close_gen(end_ts: datetime) -> None:
        nonlocal open_gen, gen_index
        if open_gen is None:
            return
        gens.append(
            WallGeneration(
                wall_side=wall_side,
                wall_price=wall_price,
                replay_epoch=int(open_gen["epoch"]),
                generation_index=int(open_gen["generation_index"]),
                generation_start_exchange_time=open_gen["start"],
                generation_end_exchange_time=end_ts,
                start_source_event_id=open_gen.get("source_event_id"),
                start_update_id=open_gen.get("u"),
                start_seq=open_gen.get("seq"),
            )
        )
        open_gen = None

    def open_new(*, start: datetime, ep: int, source: dict[str, Any] | None) -> None:
        nonlocal open_gen, gen_index
        gen_index += 1
        open_gen = {
            "start": start,
            "epoch": ep,
            "generation_index": gen_index,
            "source_event_id": None if not source else source.get("source_event_id"),
            "u": None if not source else source.get("u"),
            "seq": None if not source else source.get("seq"),
        }

    if qty > 0:
        open_new(start=window_start, ep=epoch, source={"source_event_id": "initial_book"})

    for ts, _, kind, rec in events:
        if kind == "reset":
            # Reset replaces the book; treat as generation boundary.
            new_epoch = int(rec.get("replay_epoch") or rec.get("epoch") or epoch)
            asks = {float(k): float(v) for k, v in (rec.get("asks") or {}).items() if v and float(v) > 0}
            bids = {float(k): float(v) for k, v in (rec.get("bids") or {}).items() if v and float(v) > 0}
            if isinstance(rec.get("asks"), list):
                asks = {float(p): float(q) for p, q in rec["asks"] if q and float(q) > 0}
            if isinstance(rec.get("bids"), list):
                bids = {float(p): float(q) for p, q in rec["bids"] if q and float(q) > 0}
            book_asks, book_bids = asks, bids
            new_qty = _qty_at(book_asks if wall_side == "ask" else book_bids, wall_price)
            if open_gen is not None:
                close_gen(ts)
            epoch = new_epoch
            qty = new_qty
            if qty > 0:
                open_new(start=ts, ep=epoch, source=rec)
            continue

        # level change
        new_qty = float(rec["new_size"])
        new_epoch = int(rec.get("replay_epoch") or epoch)
        if open_gen is not None and new_epoch != int(open_gen["epoch"]):
            close_gen(ts)
            epoch = new_epoch
            if new_qty > 0:
                open_new(start=ts, ep=epoch, source=rec)
            qty = new_qty
            continue
        epoch = new_epoch
        if qty <= 1e-12 and new_qty > 1e-12:
            open_new(start=ts, ep=epoch, source=rec)
        elif qty > 1e-12 and new_qty <= 1e-12:
            close_gen(ts)
        qty = new_qty

    if open_gen is not None:
        gens.append(
            WallGeneration(
                wall_side=wall_side,
                wall_price=wall_price,
                replay_epoch=int(open_gen["epoch"]),
                generation_index=int(open_gen["generation_index"]),
                generation_start_exchange_time=open_gen["start"],
                generation_end_exchange_time=None,
                start_source_event_id=open_gen.get("source_event_id"),
                start_update_id=open_gen.get("u"),
                start_seq=open_gen.get("seq"),
            )
        )
    return gens


def generation_alive_at(gens: list[WallGeneration], ts: datetime) -> WallGeneration | None:
    alive = [g for g in gens if g.alive_at(ts)]
    if not alive:
        return None
    # Prefer latest started still alive
    return sorted(alive, key=lambda g: g.generation_start_exchange_time)[-1]


def reconstruct_or_coverage_error(replay: dict[str, Any], until: datetime) -> dict[str, Any]:
    if not replay.get("ok", True) and replay.get("ok") is False:
        raise CoverageError("replay not ok")
    # Checkpoint requirement: initial book / resets must exist for window
    if replay.get("initial_asks") is None and not replay.get("book_resets"):
        raise CoverageError("missing initial book and resets — cannot reconstruct")
    try:
        return reconstruct_book_asof_exclusive(
            initial_bids=replay["initial_bids"],
            initial_asks=replay["initial_asks"],
            level_changes=replay["level_changes"],
            until=until,
            book_resets=replay.get("book_resets"),
            evidence_start=replay.get("window_start"),
            initial_update_id=replay.get("initial_update_id"),
            initial_sequence_id=replay.get("initial_sequence_id"),
            initial_replay_epoch=replay.get("initial_replay_epoch"),
            initial_checkpoint_id=replay.get("initial_checkpoint_id"),
        )
    except Exception as exc:  # noqa: BLE001
        raise CoverageError(f"book reconstruction failed: {exc}") from exc
