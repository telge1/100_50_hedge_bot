"""Isolated startup cutover harness helpers (not used by production runner)."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from bot.e1r_live_scanner.config import ScannerConfig
from bot.e1r_live_scanner.live_refresh import utc
from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.resume_checkpoint import e1r_resume_snapshot, payload_state_hash


@dataclass(frozen=True)
class FixedPaneSnapshot:
    """Single CH pane load fixed for all harness phases."""

    symbol: str
    fixed_now: datetime
    through_close: datetime
    pane_end: datetime
    market: dict
    candles15: list
    bars15: list
    packed: dict
    strict: bool

    def fetch_tuple(self, _symbol: str, _now: datetime | None = None) -> tuple:
        return (
            copy.deepcopy(self.market),
            copy.deepcopy(self.candles15),
            copy.deepcopy(self.bars15),
            dict(self.packed),
            self.strict,
        )


def capture_fixed_pane_snapshot(
    symbol: str,
    *,
    now: datetime | None = None,
    config: ScannerConfig | None = None,
) -> FixedPaneSnapshot:
    """Load pane once; ``through_close`` is the last closed 15m bar in that pane."""
    config = config or ScannerConfig(live=True)
    proc = SymbolProcessor(config)
    now = utc(now or datetime.now(timezone.utc))
    st = proc.load_state(symbol, now)
    if not st.bars15:
        raise RuntimeError(st.data_missing_reason or "no bars15 in snapshot load")
    through = utc(st.bars15[-1]["close_time"])
    # Align with resume startup merge (pane_end) and live lag at zero minutes behind ``now``.
    fixed_now = through
    return FixedPaneSnapshot(
        symbol=str(symbol).strip().upper(),
        fixed_now=fixed_now,
        through_close=through,
        pane_end=through,
        market=st.market,
        candles15=st.candles15,
        bars15=st.bars15,
        packed={"strict_complete_buckets": bool(st.strict_complete_buckets)},
        strict=bool(st.strict_complete_buckets),
    )


@contextmanager
def pin_processor_pane_fetch(snapshot: FixedPaneSnapshot) -> Iterator[None]:
    """Prevent live bar drift by reusing the same pane for every ``load_state``."""

    def _pinned_fetch(self: SymbolProcessor, symbol: str, now: datetime | None = None):
        if str(symbol).strip().upper() != snapshot.symbol:
            raise ValueError(f"unexpected symbol {symbol}")
        return snapshot.fetch_tuple(symbol, now)

    from unittest.mock import patch

    with patch.object(SymbolProcessor, "_fetch_market_and_pane", _pinned_fetch):
        yield


def bar_at_close(bars15: list[dict], close: datetime) -> dict:
    target = utc(close)
    for bar in bars15:
        if utc(bar["close_time"]) == target:
            return bar
    raise ValueError(f"no bar with close_time {target.isoformat()}")


def explain_e1r_snapshot_mismatch(left_st: Any, right_st: Any) -> dict[str, Any]:
    """Field-level diff when ``e1r_snapshot_hash`` differs."""
    left = e1r_resume_snapshot(left_st.e1r_engine)
    right = e1r_resume_snapshot(right_st.e1r_engine)
    out: dict[str, Any] = {
        "left_hash": payload_state_hash(left),
        "right_hash": payload_state_hash(right),
        "field_diffs": {},
    }
    keys = sorted(set(left.keys()) | set(right.keys()))
    for key in keys:
        lv, rv = left.get(key), right.get(key)
        if lv != rv:
            if key == "e1r_machine":
                out["field_diffs"][key] = {
                    "first_key": _first_machine_key_diff(lv or {}, rv or {}),
                    "left_keys": len(lv or {}),
                    "right_keys": len(rv or {}),
                }
            elif key == "reclaim_events":
                out["field_diffs"][key] = {
                    "left_len": len(lv or []),
                    "right_len": len(rv or []),
                }
            else:
                out["field_diffs"][key] = {"left": lv, "right": rv}
    return out


def _first_machine_key_diff(left: dict, right: dict) -> str | None:
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            return key
    return None
