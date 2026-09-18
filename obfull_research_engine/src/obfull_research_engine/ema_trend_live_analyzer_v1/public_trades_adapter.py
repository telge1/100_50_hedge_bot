"""Public trades V1 — CH public_trades_canonical watermark poll (SELECT-only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import (
    CanonicalTrade,
    build_canonical_trades,
)

from . import PUBLIC_TRADES_SOURCE
from .schema import as_utc, iso_z

CANONICAL_FQN = PUBLIC_TRADES_SOURCE
ALLOW_CLICKHOUSE_WRITES = False

STREAM_AVAILABLE_BUT_NO_TRADES = "STREAM_AVAILABLE_BUT_NO_TRADES"
STREAM_STALE = "STREAM_STALE"
STREAM_MISSING = "STREAM_MISSING"
TRADES_PRESENT = "TRADES_PRESENT"
STALE_AFTER_SEC = 120.0


def assert_ch_select_only(sql: str) -> None:
    s = sql.strip().lower()
    if not s.startswith("select"):
        raise RuntimeError("clickhouse_writes_forbidden")
    for bad in ("insert ", "alter ", "drop ", "truncate ", "delete ", "optimize ", "create "):
        if bad in s:
            raise RuntimeError("clickhouse_writes_forbidden")


@dataclass
class TradeLatencySample:
    exchange_event_time: str | None
    receive_time: str | None
    query_time_ms: float
    ch_availability_time: str | None
    source_freshness_sec: float | None
    poll_latency_ms: float


@dataclass
class TradeCoverage:
    ok: bool
    status: str
    raw_count: int
    unique_count: int
    duplicate_count: int
    rejected_missing_trade_id: int
    blockers: list[str]
    min_trade_ts: str | None = None
    max_trade_ts: str | None = None
    source_max_ts: str | None = None
    poll_count: int = 0
    latency: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "raw_count": self.raw_count,
            "unique_count": self.unique_count,
            "duplicate_count": self.duplicate_count,
            "rejected_missing_trade_id": self.rejected_missing_trade_id,
            "blockers": list(self.blockers),
            "min_trade_ts": self.min_trade_ts,
            "max_trade_ts": self.max_trade_ts,
            "source_max_ts": self.source_max_ts,
            "poll_count": self.poll_count,
            "latency": self.latency,
        }


def classify_trade_stream(
    *,
    trades_in_window: int,
    source_max_ts: datetime | None,
    now: datetime,
    source_reachable: bool = True,
    load_error: bool = False,
    stale_after_sec: float = STALE_AFTER_SEC,
) -> str:
    if load_error or not source_reachable:
        return STREAM_MISSING
    n = as_utc(now)
    assert n is not None
    if source_max_ts is None:
        return STREAM_AVAILABLE_BUT_NO_TRADES if trades_in_window <= 0 else TRADES_PRESENT
    sm = as_utc(source_max_ts)
    assert sm is not None
    age = (n - sm).total_seconds()
    if age > stale_after_sec:
        return STREAM_STALE
    if trades_in_window > 0:
        return TRADES_PRESENT
    return STREAM_AVAILABLE_BUT_NO_TRADES


class FakePublicTradesAdapter:
    """In-memory SELECT-only stand-in for isolated tests / smoke."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.load_calls = 0
        self.force_unreachable = False
        self.query_times_ms: list[float] = []

    def source_max_trade_ts(self, *, symbol: str) -> datetime | None:
        sym = symbol.upper()
        times = []
        for r in self.rows:
            if str(r.get("symbol") or "").upper() != sym:
                continue
            t = as_utc(r.get("trade_ts") or r.get("exchange_event_time"))
            if t:
                times.append(t)
        return max(times) if times else None

    def load(
        self, *, symbol: str, start: datetime, end: datetime
    ) -> tuple[list[CanonicalTrade], TradeCoverage]:
        import time as _t

        self.load_calls += 1
        t0 = _t.perf_counter()
        if self.force_unreachable:
            self.query_times_ms.append((_t.perf_counter() - t0) * 1000)
            return [], TradeCoverage(
                ok=False,
                status=STREAM_MISSING,
                raw_count=0,
                unique_count=0,
                duplicate_count=0,
                rejected_missing_trade_id=0,
                blockers=["SOURCE_UNREACHABLE"],
            )
        start_u = as_utc(start)
        end_u = as_utc(end)
        assert start_u and end_u
        filtered = []
        for r in self.rows:
            if str(r.get("symbol") or "").upper() != symbol.upper():
                continue
            ts = as_utc(r.get("trade_ts") or r.get("exchange_event_time"))
            if ts is None or ts < start_u or ts > end_u:
                continue
            filtered.append(r)
        kept, report, _dropped = build_canonical_trades(
            filtered, symbol=symbol.upper(), source_file="fake_public_trades"
        )
        qms = (_t.perf_counter() - t0) * 1000
        self.query_times_ms.append(qms)
        status = TRADES_PRESENT if kept else STREAM_AVAILABLE_BUT_NO_TRADES
        cov = TradeCoverage(
            ok=True,
            status=status,
            raw_count=report.raw_count,
            unique_count=report.unique_count,
            duplicate_count=report.duplicate_count,
            rejected_missing_trade_id=report.rejected_missing_trade_id,
            blockers=[],
            min_trade_ts=iso_z(as_utc(kept[0].exchange_dt())) if kept else None,
            max_trade_ts=iso_z(as_utc(kept[-1].exchange_dt())) if kept else None,
            source_max_ts=iso_z(self.source_max_trade_ts(symbol=symbol)),
            latency={"query_time_ms": qms},
        )
        return kept, cov


@dataclass
class TradeWatermarkPoller:
    adapter: Any
    symbol: str
    snapshot_ready_at: datetime
    feature_cutoff_at: datetime | None = None
    seen_ids: set[str] = field(default_factory=set)
    last_seen_trade_time: datetime | None = None
    last_seen_trade_id: str | None = None
    trades: list[CanonicalTrade] = field(default_factory=list)
    poll_count: int = 0
    last_status: str = STREAM_AVAILABLE_BUT_NO_TRADES
    last_source_max: datetime | None = None
    latency_samples: list[dict[str, Any]] = field(default_factory=list)

    def poll(self, *, now: datetime | None = None) -> dict[str, Any]:
        import time as _t

        self.poll_count += 1
        end = as_utc(now) or datetime.now(timezone.utc)
        if self.feature_cutoff_at is not None:
            end = min(end, as_utc(self.feature_cutoff_at) or end)
        start = as_utc(self.snapshot_ready_at)
        assert start is not None
        load_start = start
        if self.last_seen_trade_time is not None:
            load_start = self.last_seen_trade_time
        t0 = _t.perf_counter()
        batch, cov = self.adapter.load(symbol=self.symbol, start=load_start, end=end)
        poll_ms = (_t.perf_counter() - t0) * 1000.0
        new_ids: list[str] = []
        for t in batch:
            ts = as_utc(t.exchange_dt())
            assert ts is not None
            if ts < start:
                continue
            if self.feature_cutoff_at and ts > as_utc(self.feature_cutoff_at):
                continue
            tid = str(t.trade_id)
            if tid in self.seen_ids:
                continue
            if self.last_seen_trade_time is not None and ts < self.last_seen_trade_time:
                continue
            self.seen_ids.add(tid)
            self.trades.append(t)
            new_ids.append(tid)
            self.last_seen_trade_time = (
                ts if self.last_seen_trade_time is None else max(self.last_seen_trade_time, ts)
            )
            self.last_seen_trade_id = tid

        source_max = None
        if hasattr(self.adapter, "source_max_trade_ts"):
            try:
                source_max = self.adapter.source_max_trade_ts(symbol=self.symbol)
            except Exception:  # noqa: BLE001
                source_max = None
        self.last_source_max = as_utc(source_max) if source_max else self.last_source_max
        freshness = None
        if self.last_source_max is not None:
            freshness = (end - self.last_source_max).total_seconds()
        status = classify_trade_stream(
            trades_in_window=len(self.trades),
            source_max_ts=self.last_source_max,
            now=end,
            source_reachable="SOURCE_UNREACHABLE" not in (cov.blockers or []),
            load_error="SOURCE_UNREACHABLE" in (cov.blockers or []),
        )
        self.last_status = status
        sample = {
            "poll_n": self.poll_count,
            "query_time_ms": poll_ms,
            "source_freshness_sec": freshness,
            "ch_availability_proxy": iso_z(end),
            "last_canonical_ts": iso_z(self.last_seen_trade_time),
            "new_trades": len(new_ids),
            "status": status,
        }
        self.latency_samples.append(sample)
        return {
            "status": status,
            "new_trade_ids": new_ids,
            "n_trades": len(self.trades),
            "poll_count": self.poll_count,
            "source_max_ts": iso_z(self.last_source_max),
            "latency": sample,
            "coverage": cov.to_dict() if hasattr(cov, "to_dict") else cov,
        }

    def final_coverage(self) -> TradeCoverage:
        ok = self.last_status in {STREAM_AVAILABLE_BUT_NO_TRADES, TRADES_PRESENT}
        blockers = []
        if self.last_status in {STREAM_MISSING, STREAM_STALE}:
            blockers.append(self.last_status)
        return TradeCoverage(
            ok=ok,
            status=self.last_status,
            raw_count=len(self.trades),
            unique_count=len(self.seen_ids),
            duplicate_count=0,
            rejected_missing_trade_id=0,
            blockers=blockers,
            min_trade_ts=iso_z(as_utc(self.trades[0].exchange_dt())) if self.trades else None,
            max_trade_ts=iso_z(as_utc(self.trades[-1].exchange_dt())) if self.trades else None,
            source_max_ts=iso_z(self.last_source_max),
            poll_count=self.poll_count,
            latency={"samples": self.latency_samples},
        )


class ClickHousePublicTradesAdapter:
    """SELECT-only live adapter. Instantiated only when CH client is provided."""

    def __init__(self, client: Any, *, table: str = CANONICAL_FQN) -> None:
        self.client = client
        self.table = table

    def source_max_trade_ts(self, *, symbol: str) -> datetime | None:
        sql = (
            f"SELECT max(trade_ts) AS mx FROM {self.table} "
            f"WHERE symbol = %(symbol)s"
        )
        assert_ch_select_only(sql)
        rows = self.client.query(sql, parameters={"symbol": symbol.upper()}).result_rows
        if not rows or rows[0][0] is None:
            return None
        return as_utc(rows[0][0])

    def load(
        self, *, symbol: str, start: datetime, end: datetime
    ) -> tuple[list[CanonicalTrade], TradeCoverage]:
        import time as _t

        sql = (
            f"SELECT symbol, trade_id, trade_ts, price, size, side, "
            f"receive_ts "
            f"FROM {self.table} "
            f"WHERE symbol = %(symbol)s AND trade_ts >= %(start)s AND trade_ts <= %(end)s "
            f"ORDER BY trade_ts, trade_id"
        )
        assert_ch_select_only(sql)
        t0 = _t.perf_counter()
        try:
            result = self.client.query(
                sql,
                parameters={
                    "symbol": symbol.upper(),
                    "start": as_utc(start),
                    "end": as_utc(end),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return [], TradeCoverage(
                ok=False,
                status=STREAM_MISSING,
                raw_count=0,
                unique_count=0,
                duplicate_count=0,
                rejected_missing_trade_id=0,
                blockers=["SOURCE_UNREACHABLE", str(exc)],
            )
        qms = (_t.perf_counter() - t0) * 1000
        rows = []
        for r in result.named_results():
            rows.append(
                {
                    "symbol": r.get("symbol"),
                    "trade_id": r.get("trade_id"),
                    "trade_ts": r.get("trade_ts"),
                    "price": r.get("price"),
                    "size": r.get("size") or r.get("size_base"),
                    "side": r.get("side") or r.get("taker_side"),
                    "collector_received_at": r.get("receive_ts"),
                }
            )
        kept, report, _ = build_canonical_trades(
            rows, symbol=symbol.upper(), source_file=self.table
        )
        return kept, TradeCoverage(
            ok=True,
            status=TRADES_PRESENT if kept else STREAM_AVAILABLE_BUT_NO_TRADES,
            raw_count=report.raw_count,
            unique_count=report.unique_count,
            duplicate_count=report.duplicate_count,
            rejected_missing_trade_id=report.rejected_missing_trade_id,
            blockers=[],
            latency={"query_time_ms": qms},
        )
