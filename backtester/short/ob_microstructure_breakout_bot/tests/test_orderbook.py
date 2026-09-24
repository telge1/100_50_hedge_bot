"""Unit tests for Full-OB replay boundary (no live archive required)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState

from ob_microstructure_breakout_bot.data import orderbook as ob_mod
from ob_microstructure_breakout_bot.data.orderbook import sample_ob_bands


WHEN = datetime(2026, 9, 18, 10, 5, 0, tzinfo=timezone.utc)
WHEN_NS = int(WHEN.timestamp() * 1_000_000_000)

# Distinct books so applied vs skipped records are observable via band notionals.
BOOK_A_BIDS = [[100.0, 10.0]]
BOOK_A_ASKS = [[100.05, 10.0]]
BOOK_B_BIDS = [[90.0, 1.0]]
BOOK_B_ASKS = [[90.05, 1.0]]
# Future delta: huge ask inside 5 bps of BOOK_A mid (~100.025).
FUTURE_ASK = [[100.03, 500.0]]


def _checkpoint(event_ns: int, bids, asks, *, u: int = 1) -> dict:
    return {
        "message_type": "checkpoint",
        "event_time_ns": event_ns,
        "receive_time_ns": event_ns,
        "original_payload": {
            "bids": bids,
            "asks": asks,
            "u": u,
            "seq": u,
            "event_time": event_ns,
            "receive_time": event_ns,
        },
    }


def _delta(event_ns: int, bids, asks, *, u: int) -> dict:
    ts_ms = event_ns // 1_000_000
    return {
        "message_type": "delta",
        "event_time_ns": event_ns,
        "receive_time_ns": event_ns,
        "original_payload": {
            "ts": ts_ms,
            "data": {"b": bids, "a": asks, "u": u, "seq": u, "ts": ts_ms},
        },
    }


def _patch_replay(monkeypatch, records: list[dict]) -> list[tuple[str, int | None]]:
    """Stub segment resolve + iter_records; track apply order via FullBookState."""
    applied: list[tuple[str, int | None]] = []
    monkeypatch.setattr(
        ob_mod,
        "_resolve_segment",
        lambda symbol, when, raw_root: Path("/tmp/fake_ob_segment.ndjson.zst"),
    )
    monkeypatch.setattr(
        "orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.replay.iter_records",
        lambda path: iter(records),
    )
    orig_snap = FullBookState.apply_snapshot
    orig_delta = FullBookState.apply_delta

    def wrap_snap(self, **kwargs):
        applied.append(("checkpoint", kwargs.get("u")))
        return orig_snap(self, **kwargs)

    def wrap_delta(self, **kwargs):
        applied.append(("delta", kwargs.get("u")))
        return orig_delta(self, **kwargs)

    monkeypatch.setattr(FullBookState, "apply_snapshot", wrap_snap)
    monkeypatch.setattr(FullBookState, "apply_delta", wrap_delta)
    return applied


def test_a_future_delta_not_applied(monkeypatch):
    """Record before when applied; record after when excluded."""
    before = WHEN_NS - 100_000_000  # 100 ms
    after = WHEN_NS + 100_000_000
    records = [
        _checkpoint(before, BOOK_A_BIDS, BOOK_A_ASKS, u=1),
        _delta(after, [], FUTURE_ASK, u=2),
    ]
    applied = _patch_replay(monkeypatch, records)

    snap = sample_ob_bands("DOGEUSDT", WHEN, raw_root=Path("/tmp"))

    assert applied == [("checkpoint", 1)]
    # BOOK_A mid ≈ 100.025 → bid notional 1000; future ask must not inflate ask band.
    assert snap.bid_5bps == pytest.approx(1000.0)
    assert snap.ask_5bps == pytest.approx(1000.5)
    assert snap.ask_5bps < 10_000  # would be huge if FUTURE_ASK applied


def test_b_exact_equality_not_applied(monkeypatch):
    """event_time == when must not be applied."""
    before = WHEN_NS - 50_000_000
    records = [
        _checkpoint(before, BOOK_A_BIDS, BOOK_A_ASKS, u=1),
        _delta(WHEN_NS, [], FUTURE_ASK, u=2),
    ]
    applied = _patch_replay(monkeypatch, records)

    snap = sample_ob_bands("DOGEUSDT", WHEN, raw_root=Path("/tmp"))

    assert applied == [("checkpoint", 1)]
    assert snap.ask_5bps == pytest.approx(1000.5)


def test_c_future_checkpoint_not_applied(monkeypatch):
    """Checkpoint after when must not replace the prior book."""
    before = WHEN_NS - 200_000_000
    after = WHEN_NS + 50_000_000
    records = [
        _checkpoint(before, BOOK_A_BIDS, BOOK_A_ASKS, u=1),
        _checkpoint(after, BOOK_B_BIDS, BOOK_B_ASKS, u=99),
    ]
    applied = _patch_replay(monkeypatch, records)

    snap = sample_ob_bands("DOGEUSDT", WHEN, raw_root=Path("/tmp"))

    assert applied == [("checkpoint", 1)]
    assert snap.bid_5bps == pytest.approx(1000.0)
    # BOOK_B would yield bid_5bps ≈ 90.0
    assert snap.bid_5bps != pytest.approx(90.0)


def test_d_no_prior_state_raises(monkeypatch):
    """No record before when → RuntimeError; future record must not seed state."""
    after = WHEN_NS + 1_000_000
    records = [
        _checkpoint(after, BOOK_A_BIDS, BOOK_A_ASKS, u=1),
    ]
    applied = _patch_replay(monkeypatch, records)

    with pytest.raises(RuntimeError, match="No mid"):
        sample_ob_bands("DOGEUSDT", WHEN, raw_root=Path("/tmp"))

    assert applied == []


def test_e_nanosecond_boundary(monkeypatch):
    """Only event_time < when is applied (when-1ns yes; when and when+1ns no)."""
    records = [
        _checkpoint(WHEN_NS - 1, BOOK_A_BIDS, BOOK_A_ASKS, u=1),
        _delta(WHEN_NS, [], FUTURE_ASK, u=2),
        _delta(WHEN_NS + 1, [], FUTURE_ASK, u=3),
    ]
    applied = _patch_replay(monkeypatch, records)

    snap = sample_ob_bands("DOGEUSDT", WHEN, raw_root=Path("/tmp"))

    assert applied == [("checkpoint", 1)]
    assert snap.bid_5bps == pytest.approx(1000.0)
    assert snap.ask_5bps == pytest.approx(1000.5)
