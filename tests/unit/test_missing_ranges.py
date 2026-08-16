"""Tests for missing-range detection and FILL_MISSING_RANGES repair path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from signal_generator.bybit.checkpoint import (
    CheckpointStore,
    SymbolCheckpoint,
    merge_checkpoint_window,
    save_checkpoint,
    should_skip_symbol,
)
from signal_generator.bybit.missing_ranges import (
    apply_safety_limits,
    missing_ranges_from_open_times,
)
from signal_generator.bybit.universe import Universe, UniverseSymbol
from signal_generator.bybit.universe_backfill import (
    listing_effective_start,
    repair_symbol_missing_ranges,
    run_universe_backfill,
)
from signal_generator.db.candles import Candle1m


UTC = timezone.utc


def _ts(h: int, m: int = 0) -> datetime:
    return datetime(2026, 4, 15, h, m, tzinfo=UTC)


def _minutes(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(count)]


def _candle(symbol: str, open_time: datetime) -> Candle1m:
    return Candle1m(
        exchange="bybit",
        symbol=symbol,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        source="bybit_history",
    )


class RecordingHistory:
    def __init__(self, *, omit: set[datetime] | None = None) -> None:
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.omit = omit or set()

    def fetch_closed_1m(
        self, symbol: str, start: datetime, end: datetime, **kwargs: Any
    ) -> list[Candle1m]:
        self.calls.append((symbol, start, end))
        out: list[Candle1m] = []
        t = start
        while t < end:
            if t not in self.omit:
                out.append(_candle(symbol, t))
            t += timedelta(minutes=1)
        return out


class MemRepo:
    def __init__(self) -> None:
        self.by_symbol: dict[str, dict[datetime, Candle1m]] = {}
        self.insert_calls = 0

    def insert_candles(self, batch: list[Candle1m]) -> int:
        self.insert_calls += 1
        for c in batch:
            self.by_symbol.setdefault(c.symbol, {})[c.open_time] = c
        return len(batch)

    def open_times(self, symbol: str) -> list[datetime]:
        return sorted(self.by_symbol.get(symbol, {}))


def _universe(*symbols: str, launch: dict[str, str] | None = None) -> Universe:
    launch = launch or {}
    details = [
        UniverseSymbol(
            symbol=s,
            rank=i,
            turnover24h="1",
            volume24h="1",
            launch_time=launch.get(s),
            contract_type="LinearPerpetual",
            status="Trading",
            settle_coin="USDT",
        )
        for i, s in enumerate(symbols, start=1)
    ]
    return Universe(
        generated_at="2026-08-10T00:00:00+00:00",
        source="test",
        selection_method="test",
        target_size=len(symbols),
        symbols=list(symbols),
        details=details,
    )


# --- detection cases ---


def test_no_data_one_full_missing_range():
    start, end = _ts(10), _ts(10, 10)
    ranges = missing_ranges_from_open_times([], effective_start=start, requested_end=end)
    assert len(ranges) == 1
    assert ranges[0].kind == "FULL"
    assert ranges[0].start == start and ranges[0].end == end
    assert ranges[0].missing_minutes == 10


def test_full_coverage_zero_ranges():
    start, end = _ts(10), _ts(10, 5)
    present = _minutes(start, 5)
    assert missing_ranges_from_open_times(present, effective_start=start, requested_end=end) == []


def test_one_internal_missing_candle():
    start, end = _ts(10), _ts(10, 5)
    present = [_ts(10, 0), _ts(10, 1), _ts(10, 3), _ts(10, 4)]
    ranges = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    assert len(ranges) == 1
    assert ranges[0].kind == "INTERNAL"
    assert ranges[0].start == _ts(10, 2) and ranges[0].end == _ts(10, 3)
    assert ranges[0].missing_minutes == 1


def test_one_internal_multi_minute_gap():
    # Example from spec: 10:00-10:02, 10:07-10:08 present; end 10:10
    start, end = _ts(10), _ts(10, 10)
    present = [_ts(10, i) for i in (0, 1, 2, 7, 8)]
    ranges = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    assert [(r.start, r.end, r.kind) for r in ranges] == [
        (_ts(10, 3), _ts(10, 7), "INTERNAL"),
        (_ts(10, 9), _ts(10, 10), "TRAILING"),
    ]


def test_multiple_internal_gaps():
    start, end = _ts(10), _ts(10, 10)
    present = [_ts(10, 0), _ts(10, 2), _ts(10, 5), _ts(10, 9)]
    ranges = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    assert [r.kind for r in ranges] == ["INTERNAL", "INTERNAL", "INTERNAL"]
    assert [r.missing_minutes for r in ranges] == [1, 2, 3]
    # last present is 10:09; end=10:10 → no trailing beyond last+1m until end
    assert ranges[-1].start == _ts(10, 6) and ranges[-1].end == _ts(10, 9)


def test_leading_gap():
    start, end = _ts(10), _ts(10, 5)
    present = [_ts(10, 2), _ts(10, 3), _ts(10, 4)]
    ranges = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    assert ranges[0].kind == "LEADING"
    assert ranges[0].start == start and ranges[0].end == _ts(10, 2)


def test_trailing_gap():
    start, end = _ts(10), _ts(10, 5)
    present = [_ts(10, 0), _ts(10, 1), _ts(10, 2)]
    ranges = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    assert len(ranges) == 1
    assert ranges[0].kind == "TRAILING"
    assert ranges[0].start == _ts(10, 3) and ranges[0].end == end


def test_leading_internal_trailing():
    start, end = _ts(10), _ts(10, 10)
    present = [_ts(10, 2), _ts(10, 3), _ts(10, 7)]
    ranges = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    assert [r.kind for r in ranges] == ["LEADING", "INTERNAL", "TRAILING"]
    assert ranges[0].end == _ts(10, 2)
    assert ranges[1].start == _ts(10, 4) and ranges[1].end == _ts(10, 7)
    assert ranges[2].start == _ts(10, 8) and ranges[2].end == end


def test_pre_listing_excluded_via_effective_start():
    requested_start = datetime(2026, 1, 1, tzinfo=UTC)
    launch = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
    detail = UniverseSymbol(
        "NEWUSDT", 1, "1", "1", launch.isoformat(), "LinearPerpetual", "Trading", "USDT"
    )
    eff = listing_effective_start(requested_start, detail)
    assert eff == launch
    present = _minutes(launch, 5)
    end = launch + timedelta(minutes=5)
    ranges = missing_ranges_from_open_times(
        present, effective_start=eff, requested_end=end
    )
    assert ranges == []
    bad = missing_ranges_from_open_times(
        present, effective_start=requested_start, requested_end=end
    )
    assert bad[0].kind == "LEADING"
    assert bad[0].start == requested_start


def test_exact_half_open_semantics():
    start, end = _ts(10), _ts(10, 3)
    present = [_ts(10, 0), _ts(10, 1), _ts(10, 2)]
    assert missing_ranges_from_open_times(present, effective_start=start, requested_end=end) == []
    present2 = present + [_ts(10, 3)]
    assert missing_ranges_from_open_times(present2, effective_start=start, requested_end=end) == []


def test_contiguous_ranges_coalesced():
    start, end = _ts(10), _ts(10, 10)
    present = [_ts(10, 0), _ts(10, 5), _ts(10, 9)]
    ranges = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    # 10:01-10:05 is one INTERNAL range (4 minutes), not 4 singles
    assert ranges[0].kind == "INTERNAL"
    assert ranges[0].start == _ts(10, 1) and ranges[0].end == _ts(10, 5)
    assert ranges[0].missing_minutes == 4
    assert sum(r.missing_minutes for r in ranges) == 7  # [1,5)=4 + [6,9)=3


def test_safety_limits_truncate_not_silent():
    start = _ts(10)
    ranges = missing_ranges_from_open_times(
        [], effective_start=start, requested_end=start + timedelta(minutes=100)
    )
    limited, truncated, reason = apply_safety_limits(
        ranges, max_ranges=10, max_span_minutes=30
    )
    assert truncated is True
    assert limited
    assert "exceeds" in reason


def test_complete_without_repair_still_blind_skip():
    cp = SymbolCheckpoint(
        symbol="SOLUSDT",
        requested_start=_ts(10),
        requested_end=_ts(12),
        status="COMPLETE",
    )
    assert should_skip_symbol(cp, resume=True, retry_failed=False, repair_missing=False) is True
    assert should_skip_symbol(cp, resume=True, retry_failed=False, repair_missing=True) is False


def test_window_extension_reopens_complete_from_old_end():
    old_end = datetime(2026, 8, 9, tzinfo=UTC)
    new_end = datetime(2026, 8, 10, tzinfo=UTC)
    store = CheckpointStore(
        version=1,
        requested_start=datetime(2026, 1, 1, tzinfo=UTC),
        requested_end=old_end,
        chunk_days=7,
        symbols={
            "SOLUSDT": SymbolCheckpoint(
                symbol="SOLUSDT",
                requested_start=datetime(2026, 1, 1, tzinfo=UTC),
                requested_end=old_end,
                last_completed_timestamp=old_end,
                status="COMPLETE",
                final_status="COMPLETE_CLEAN",
            )
        },
    )
    merge_checkpoint_window(
        store,
        requested_start=datetime(2026, 1, 1, tzinfo=UTC),
        requested_end=new_end,
    )
    cp = store.symbols["SOLUSDT"]
    assert cp.status == "PARTIAL"
    assert cp.last_completed_timestamp == old_end
    assert cp.requested_end == new_end


def test_complete_with_no_gaps_skipped_via_repair_noop(tmp_path: Path):
    start, end = _ts(10), _ts(10, 5)
    present = _minutes(start, 5)
    hist = RecordingHistory()
    repo = MemRepo()
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=end,
        chunk_days=7,
        symbols={
            "SOLUSDT": SymbolCheckpoint(
                symbol="SOLUSDT",
                requested_start=start,
                requested_end=end,
                last_completed_timestamp=end,
                status="COMPLETE",
            )
        },
    )
    save_checkpoint(store, tmp_path / "cp.json")
    result = run_universe_backfill(
        universe=_universe("SOLUSDT"),
        ch=None,
        requested_start=start,
        requested_end=end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["SOLUSDT"],
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={"SOLUSDT": present},
    )
    assert hist.calls == []
    assert result.repair_reports is not None
    assert result.repair_reports[0].missing_range_count == 0
    assert result.checkpoint.symbols["SOLUSDT"].status == "COMPLETE"


def test_complete_with_internal_gap_repaired(tmp_path: Path):
    start, end = _ts(10), _ts(10, 5)
    present = [_ts(10, 0), _ts(10, 1), _ts(10, 3), _ts(10, 4)]
    hist = RecordingHistory()
    repo = MemRepo()
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=end,
        chunk_days=7,
        symbols={
            "SOLUSDT": SymbolCheckpoint(
                symbol="SOLUSDT",
                requested_start=start,
                requested_end=end,
                last_completed_timestamp=end,
                status="COMPLETE",
                final_status="COMPLETE_WITH_INTERNAL_GAPS",
            )
        },
    )
    save_checkpoint(store, tmp_path / "cp.json")
    result = run_universe_backfill(
        universe=_universe("SOLUSDT"),
        ch=None,
        requested_start=start,
        requested_end=end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["SOLUSDT"],
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={"SOLUSDT": present},
    )
    assert len(hist.calls) == 1
    assert hist.calls[0][1:] == (_ts(10, 2), _ts(10, 3))
    assert _ts(10, 2) in repo.open_times("SOLUSDT")
    assert result.checkpoint.symbols["SOLUSDT"].candles_repaired == 1


def test_complete_extended_end_repairs_tail_only(tmp_path: Path):
    start = _ts(0)
    old_end = _ts(2)
    new_end = _ts(3)
    present = _minutes(start, 120)
    hist = RecordingHistory()
    repo = MemRepo()
    for t in present:
        repo.by_symbol.setdefault("SOLUSDT", {})[t] = _candle("SOLUSDT", t)
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=old_end,
        chunk_days=1,
        symbols={
            "SOLUSDT": SymbolCheckpoint(
                symbol="SOLUSDT",
                requested_start=start,
                requested_end=old_end,
                last_completed_timestamp=old_end,
                status="COMPLETE",
            )
        },
    )
    save_checkpoint(store, tmp_path / "cp.json")
    result = run_universe_backfill(
        universe=_universe("SOLUSDT"),
        ch=None,
        requested_start=start,
        requested_end=new_end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["SOLUSDT"],
        chunk_days=1,
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={"SOLUSDT": present},
    )
    assert hist.calls
    for _sym, s, e in hist.calls:
        assert s >= old_end
        assert e <= new_end
    assert result.checkpoint.symbols["SOLUSDT"].requested_end == new_end


def test_partial_cursor_and_internal_gap_both_repaired(tmp_path: Path):
    start, mid, end = _ts(10), _ts(10, 3), _ts(10, 6)
    present = [_ts(10, 0), _ts(10, 1)]
    hist = RecordingHistory()
    repo = MemRepo()
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=end,
        chunk_days=1,
        symbols={
            "SOLUSDT": SymbolCheckpoint(
                symbol="SOLUSDT",
                requested_start=start,
                requested_end=end,
                last_completed_timestamp=mid,
                status="PARTIAL",
            )
        },
    )
    save_checkpoint(store, tmp_path / "cp.json")
    run_universe_backfill(
        universe=_universe("SOLUSDT"),
        ch=None,
        requested_start=start,
        requested_end=end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["SOLUSDT"],
        chunk_days=1,
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={"SOLUSDT": present},
    )
    assert any(s >= mid for _sym, s, _e in hist.calls)
    # With only 10:00-10:01 present, 10:02→end coalesces as one TRAILING range
    assert any(s == _ts(10, 2) and e == end for _sym, s, e in hist.calls)


def test_repair_rerun_idempotent(tmp_path: Path):
    start, end = _ts(10), _ts(10, 4)
    present = [_ts(10, 0), _ts(10, 1), _ts(10, 3)]
    hist = RecordingHistory()
    repo = MemRepo()
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=end,
        chunk_days=7,
        symbols={
            "SOLUSDT": SymbolCheckpoint(
                symbol="SOLUSDT",
                requested_start=start,
                requested_end=end,
                last_completed_timestamp=end,
                status="COMPLETE",
            )
        },
    )
    save_checkpoint(store, tmp_path / "cp.json")
    kwargs: dict[str, Any] = dict(
        universe=_universe("SOLUSDT"),
        ch=None,
        requested_start=start,
        requested_end=end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["SOLUSDT"],
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,
        repo=repo,
        open_times_by_symbol={"SOLUSDT": present},
    )
    run_universe_backfill(**kwargs)
    first_calls = list(hist.calls)
    full = _minutes(start, 4)
    hist.calls.clear()
    run_universe_backfill(**{**kwargs, "open_times_by_symbol": {"SOLUSDT": full}})
    assert hist.calls == []
    assert first_calls


def test_unresolved_gap_classified(tmp_path: Path):
    start, end = _ts(10), _ts(10, 3)
    present = [_ts(10, 0), _ts(10, 2)]
    hist = RecordingHistory(omit={_ts(10, 1)})
    repo = MemRepo()
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=end,
        chunk_days=7,
        symbols={
            "SOLUSDT": SymbolCheckpoint(
                symbol="SOLUSDT",
                requested_start=start,
                requested_end=end,
                status="COMPLETE",
            )
        },
    )
    save_checkpoint(store, tmp_path / "cp.json")
    result = run_universe_backfill(
        universe=_universe("SOLUSDT"),
        ch=None,
        requested_start=start,
        requested_end=end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["SOLUSDT"],
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={"SOLUSDT": present},
    )
    assert result.checkpoint.symbols["SOLUSDT"].unresolved_ranges == 1


def test_multiple_symbols_independent(tmp_path: Path):
    start, end = _ts(10), _ts(10, 3)
    hist = RecordingHistory()
    repo = MemRepo()
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=end,
        chunk_days=7,
        symbols={
            "AAAUSDT": SymbolCheckpoint(
                symbol="AAAUSDT",
                requested_start=start,
                requested_end=end,
                last_completed_timestamp=end,
                status="COMPLETE",
            ),
            "BBBUSDT": SymbolCheckpoint(
                symbol="BBBUSDT",
                requested_start=start,
                requested_end=end,
                last_completed_timestamp=end,
                status="COMPLETE",
            ),
        },
    )
    save_checkpoint(store, tmp_path / "cp.json")
    run_universe_backfill(
        universe=_universe("AAAUSDT", "BBBUSDT"),
        ch=None,
        requested_start=start,
        requested_end=end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["AAAUSDT", "BBBUSDT"],
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={
            "AAAUSDT": [],
            "BBBUSDT": _minutes(start, 3),
        },
    )
    aaa_calls = [c for c in hist.calls if c[0] == "AAAUSDT"]
    bbb_calls = [c for c in hist.calls if c[0] == "BBBUSDT"]
    assert aaa_calls
    assert bbb_calls == []


def test_no_unnecessary_full_window_redownload(tmp_path: Path):
    start, end = _ts(10), _ts(12)
    present = _minutes(start, 118)
    hist = RecordingHistory()
    repo = MemRepo()
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=end,
        chunk_days=7,
        symbols={
            "SOLUSDT": SymbolCheckpoint(
                symbol="SOLUSDT",
                requested_start=start,
                requested_end=end,
                last_completed_timestamp=end,
                status="COMPLETE",
            )
        },
    )
    save_checkpoint(store, tmp_path / "cp.json")
    run_universe_backfill(
        universe=_universe("SOLUSDT"),
        ch=None,
        requested_start=start,
        requested_end=end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["SOLUSDT"],
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={"SOLUSDT": present},
    )
    assert len(hist.calls) == 1
    assert hist.calls[0][1] == _ts(11, 58)
    assert hist.calls[0][2] == end
    assert all(s > start for _sym, s, _e in hist.calls)


def test_dry_run_performs_no_writes(tmp_path: Path):
    start, end = _ts(10), _ts(10, 5)
    present = [_ts(10, 0), _ts(10, 4)]
    hist = RecordingHistory()
    repo = MemRepo()
    run_universe_backfill(
        universe=_universe("SOLUSDT"),
        ch=None,
        requested_start=start,
        requested_end=end,
        checkpoint_path=tmp_path / "cp.json",
        symbols=["SOLUSDT"],
        dry_run=True,
        resume=False,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={"SOLUSDT": present},
    )
    assert hist.calls == []
    assert repo.insert_calls == 0
    assert repo.by_symbol == {}


def test_integration_remove_candle_and_repair(tmp_path: Path):
    """Isolated in-memory integration: import → gap → repair → gap_count 0."""
    start, end = _ts(10), _ts(10, 10)
    full = _minutes(start, 10)
    gap = _ts(10, 4)
    present = [t for t in full if t != gap]
    hist = RecordingHistory()
    repo = MemRepo()
    for t in present:
        repo.by_symbol.setdefault("TESTUSDT", {})[t] = _candle("TESTUSDT", t)

    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=end,
        chunk_days=7,
        symbols={
            "TESTUSDT": SymbolCheckpoint(
                symbol="TESTUSDT",
                requested_start=start,
                requested_end=end,
                last_completed_timestamp=end,
                status="COMPLETE",
                final_status="COMPLETE_WITH_INTERNAL_GAPS",
            )
        },
    )
    path = tmp_path / "cp.json"
    save_checkpoint(store, path)

    before = missing_ranges_from_open_times(present, effective_start=start, requested_end=end)
    assert len(before) == 1
    assert before[0].start == gap and before[0].end == gap + timedelta(minutes=1)

    repair_symbol_missing_ranges(
        symbol="TESTUSDT",
        index=1,
        total=1,
        client=hist,  # type: ignore[arg-type]
        ch=None,
        repo=repo,  # type: ignore[arg-type]
        store=store,
        checkpoint_path=path,
        requested_start=start,
        requested_end=end,
        batch_size=1000,
        dry_run=False,
        detail=None,
        open_times=present,
    )
    assert hist.calls == [("TESTUSDT", gap, gap + timedelta(minutes=1))]
    assert gap in repo.open_times("TESTUSDT")
    after = missing_ranges_from_open_times(
        repo.open_times("TESTUSDT"), effective_start=start, requested_end=end
    )
    assert after == []
    assert store.symbols["TESTUSDT"].status == "COMPLETE"


def test_integration_window_extension_only_tail(tmp_path: Path):
    start = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    old_end = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    new_end = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)
    present = _minutes(start, 120)
    hist = RecordingHistory()
    repo = MemRepo()
    for t in present:
        repo.by_symbol.setdefault("TESTUSDT", {})[t] = _candle("TESTUSDT", t)
    store = CheckpointStore(
        version=1,
        requested_start=start,
        requested_end=old_end,
        chunk_days=1,
        symbols={
            "TESTUSDT": SymbolCheckpoint(
                symbol="TESTUSDT",
                requested_start=start,
                requested_end=old_end,
                last_completed_timestamp=old_end,
                status="COMPLETE",
            )
        },
    )
    path = tmp_path / "cp.json"
    save_checkpoint(store, path)
    run_universe_backfill(
        universe=_universe("TESTUSDT"),
        ch=None,
        requested_start=start,
        requested_end=new_end,
        checkpoint_path=path,
        symbols=["TESTUSDT"],
        chunk_days=1,
        dry_run=False,
        resume=True,
        repair_missing=True,
        bybit=hist,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        open_times_by_symbol={"TESTUSDT": present},
    )
    assert all(s >= old_end for _sym, s, e in hist.calls)
    assert not any(s == start and e == new_end for _sym, s, e in hist.calls)
