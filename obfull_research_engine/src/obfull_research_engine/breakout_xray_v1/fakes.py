"""Fake / fixture repository implementations for offline X-Ray tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .adapters.bronze_baseline import (
    BronzeRecordFull,
    FixtureBaselineBookRepository,
)
from .models import BaselineBookState
from .ports import (
    AnalysisDependencies,
    AvrRepository,
    BookLevel,
    LevelChangeEvent,
    MarketProfileRepository,
    MidState,
    OpenInterestRepository,
    PublicTradesRepository,
    ReadinessRepository,
    ReadinessResult,
    SilverLevelChangesRepository,
    SilverMetricsRepository,
)
from .trades import DedupStats, XRayTrade, dedup_trades_by_id
from .time_windows import dt_to_ns


@dataclass
class FakeReadinessRepository:
    result: ReadinessResult

    def assess_window(
        self, *, symbol: str, start_ns: int, end_ns: int
    ) -> ReadinessResult:
        return self.result


@dataclass
class FakeSilverMetricsRepository:
    mid_series: list[MidState] = field(default_factory=list)
    minute_rows: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def load_mid_series(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> list[MidState]:
        self.calls.append(
            {
                "symbol": symbol,
                "start_ns": start_ns,
                "end_ns": end_ns,
                "chunk_keys": chunk_keys,
            }
        )
        # Half-open filter
        return [
            m
            for m in self.mid_series
            if start_ns <= m.bucket_start_ns < end_ns
        ]

    def load_minute_rows(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        return [
            r
            for r in self.minute_rows
            if start_ns <= int(r["minute_ns"]) < end_ns
        ]


@dataclass
class FakeSilverLevelChangesRepository:
    events: list[LevelChangeEvent] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def load_level_changes(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> list[LevelChangeEvent]:
        self.calls.append(
            {
                "symbol": symbol,
                "start_ns": start_ns,
                "end_ns": end_ns,
                "chunk_keys": chunk_keys,
            }
        )
        return [
            e for e in self.events if start_ns <= e.event_time_ns < end_ns
        ]


@dataclass
class FakePublicTradesRepository:
    trades: list[XRayTrade] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def load_trades(
        self, *, symbol: str, start_ns: int, end_ns: int
    ) -> tuple[list[XRayTrade], DedupStats]:
        self.calls.append(
            {"symbol": symbol, "start_ns": start_ns, "end_ns": end_ns}
        )
        windowed = [
            t
            for t in self.trades
            if start_ns <= dt_to_ns(t.trade_ts) < end_ns
        ]
        return dedup_trades_by_id(windowed)


@dataclass
class FakeMarketProfileRepository:
    profile: dict[str, Any]

    def load_previous_closed_30m_tpo(
        self, *, symbol: str, decision_time: datetime
    ) -> dict[str, Any]:
        return self.profile


@dataclass
class StubAvrRepository:
    def load_avr(self, *, symbol: str, start_ns: int, end_ns: int) -> dict[str, Any]:
        return {"status": "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"}


@dataclass
class StubOpenInterestRepository:
    def load_oi(self, *, symbol: str, start_ns: int, end_ns: int) -> dict[str, Any]:
        return {"status": "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"}


def make_fake_deps(
    *,
    readiness: ReadinessResult | None = None,
    mid_series: Sequence[MidState] | None = None,
    minute_rows: Sequence[dict[str, Any]] | None = None,
    level_changes: Sequence[LevelChangeEvent] | None = None,
    trades: Sequence[XRayTrade] | None = None,
    bronze_records: Sequence[BronzeRecordFull] | None = None,
    expected_book_hash: str | None = None,
    mp_profile: dict[str, Any] | None = None,
    enforce_continuity: bool = False,
) -> AnalysisDependencies:
    ready = readiness or ReadinessResult(
        status="READY",
        reason="FAKE_READY",
        epoch_id="epoch_fake",
        chunk_keys=("chunk_a",),
        level_change_count=len(level_changes or []),
        state_count=len(mid_series or []),
    )
    baseline_repo: FixtureBaselineBookRepository | _StaticBaseline
    if bronze_records is not None:
        baseline_repo = FixtureBaselineBookRepository(
            bronze_records, enforce_continuity=enforce_continuity
        )
    else:
        baseline_repo = _StaticBaseline(
            BaselineBookState(
                source="fake_empty",
                timestamp=None,
                age_ms=None,
                complete=False,
                hash=None,
                unresolved_reason="UNRESOLVED_BASELINE",
            )
        )
    # Wrap baseline to inject expected hash from caller context via attribute
    if isinstance(baseline_repo, FixtureBaselineBookRepository):
        baseline_repo._expected_override = expected_book_hash  # type: ignore[attr-defined]

        _orig = baseline_repo.load_baseline

        def _load(**kwargs):
            if kwargs.get("expected_book_hash") is None and expected_book_hash is not None:
                kwargs = {**kwargs, "expected_book_hash": expected_book_hash}
            return _orig(**kwargs)

        baseline_repo.load_baseline = _load  # type: ignore[method-assign]

    return AnalysisDependencies(
        readiness=FakeReadinessRepository(ready),
        metrics=FakeSilverMetricsRepository(
            mid_series=list(mid_series or []),
            minute_rows=list(minute_rows or []),
        ),
        level_changes=FakeSilverLevelChangesRepository(
            events=list(level_changes or [])
        ),
        trades=FakePublicTradesRepository(trades=list(trades or [])),
        baseline=baseline_repo,  # type: ignore[arg-type]
        market_profile=FakeMarketProfileRepository(mp_profile or {}),
        avr=StubAvrRepository(),
        open_interest=StubOpenInterestRepository(),
    )


@dataclass
class _StaticBaseline:
    state: BaselineBookState

    def load_baseline(
        self,
        *,
        symbol: str,
        start_ns: int,
        epoch_id: str,
        expected_book_hash: str | None,
    ) -> BaselineBookState:
        return self.state


def book_levels_from_dicts(rows: Sequence[dict[str, Any]]) -> list[BookLevel]:
    return [
        BookLevel(side=str(r["side"]), price=float(r["price"]), size=float(r["size"]))
        for r in rows
    ]
