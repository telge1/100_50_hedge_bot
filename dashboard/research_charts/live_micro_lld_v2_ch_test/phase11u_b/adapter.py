"""Isolated historical remaining-pool adapter. Read-only publications, immutable candles only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, Sequence

from data.models import ensure_utc
from data.timeframes import timeframe_seconds
from indicators.live_micro_lld_v2.availability import bar_close

from ..active_snapshot.fingerprint import active_state_fingerprint
from ..immutable_candle_snapshot_v1 import (
    SnapshotError,
    load_snapshot,
    ohlcv_content_hash,
    revision_metadata_hash,
)
from ..immutable_snapshot_candle_session import (
    ImmutableInputCoverageError,
    ImmutableSnapshotCandleSession,
)
from ..phase11o_candle_ranges import CandleRangeIndex
from ..remaining_pool_projection_v1 import (
    build_fixed_segments,
    remaining_from_penetration,
)
from ..remaining_pool_projection_v1_config import (
    DEFAULT_SEGMENT_COUNT,
    STRENGTH_CLASS_COLORS,
)

READY = "READY"
MISSING_PUBLICATION = "MISSING_PUBLICATION"
MISSING_MEMBERSHIP = "MISSING_MEMBERSHIP"
MISSING_CATALOG = "MISSING_CATALOG"
MISSING_IMMUTABLE_INPUT = "MISSING_IMMUTABLE_INPUT"
INSUFFICIENT_CANDLE_COVERAGE = "INSUFFICIENT_CANDLE_COVERAGE"
AMBIGUOUS_PUBLICATION = "AMBIGUOUS_PUBLICATION"
INPUT_IDENTITY_MISMATCH = "INPUT_IDENTITY_MISMATCH"
INVALID_REQUEST = "INVALID_REQUEST"


class AdapterStop(Exception):
    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class PublicationHit:
    snapshot_id: str
    pipeline_line_id: str
    active_state_fingerprint: str
    config_hash: str
    publish_complete: int


@dataclass(frozen=True)
class MemberRow:
    pool_id: str
    side: str


@dataclass(frozen=True)
class CatalogRow:
    pool_id: str
    side: str
    pool_type: str
    top: float
    bottom: float
    strength: float
    strength_class: str
    known_at: datetime


class PublicationSource(Protocol):
    def find(
        self,
        *,
        symbol: str,
        timeframe: str,
        pool_detail: int,
        decision_close: datetime,
    ) -> list[PublicationHit]:
        ...

    def members(self, snapshot_id: str) -> list[MemberRow]:
        ...

    def catalog(self, snapshot_id: str) -> list[CatalogRow]:
        ...


class ClickHousePublicationSource:
    """Read-only lookup. No DDL and no writes."""

    def __init__(self, client, database: str) -> None:
        self._client = client
        self._database = database

    def find(self, *, symbol: str, timeframe: str, pool_detail: int, decision_close: datetime) -> list[PublicationHit]:
        rows = self._client.query(
            f"""
            SELECT snapshot_id, pipeline_line_id, active_state_fingerprint, config_hash, publish_complete
            FROM {self._database}.research_lmv2_active_snapshot_publish_v1 FINAL
            WHERE symbol = {{sym:String}}
              AND timeframe = {{tf:String}}
              AND pool_detail = {{pd:UInt8}}
              AND decision_close = {{dc:DateTime64(3, 'UTC')}}
              AND publish_complete = 1
            """,
            parameters={
                "sym": symbol,
                "tf": timeframe,
                "pd": int(pool_detail),
                "dc": decision_close,
            },
        ).result_rows
        return [
            PublicationHit(
                snapshot_id=str(r[0]),
                pipeline_line_id=str(r[1]),
                active_state_fingerprint=str(r[2]),
                config_hash=str(r[3]),
                publish_complete=int(r[4]),
            )
            for r in rows
        ]

    def members(self, snapshot_id: str) -> list[MemberRow]:
        rows = self._client.query(
            f"""
            SELECT pool_id, side
            FROM {self._database}.research_lmv2_active_snapshot_member_v1
            WHERE snapshot_id = {{sid:String}}
            """,
            parameters={"sid": snapshot_id},
        ).result_rows
        return [MemberRow(pool_id=str(r[0]), side=str(r[1]).lower()) for r in rows]

    def catalog(self, snapshot_id: str) -> list[CatalogRow]:
        rows = self._client.query(
            f"""
            SELECT pool_id, side, pool_type, top_price, bottom_price, strength, strength_class, known_at
            FROM {self._database}.research_lmv2_snapshot_active_pool_v1
            WHERE snapshot_id = {{sid:String}}
            """,
            parameters={"sid": snapshot_id},
        ).result_rows
        return [
            CatalogRow(
                pool_id=str(r[0]),
                side=str(r[1]).lower(),
                pool_type=str(r[2]),
                top=float(r[3]),
                bottom=float(r[4]),
                strength=float(r[5]),
                strength_class=str(r[6]),
                known_at=ensure_utc(r[7]),
            )
            for r in rows
        ]


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise AdapterStop(INVALID_REQUEST, "naive datetime")
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def locate_manifests(root: Path, snapshot_id: str) -> list[Path]:
    return sorted(Path(root).glob(f"**/{snapshot_id}.manifest.json"))


def resolve_snapshot_dir(root: Path, snapshot_id: str, price_hash: str, revision_hash: str) -> Path:
    matches = locate_manifests(root, snapshot_id)
    if not matches:
        raise AdapterStop(MISSING_IMMUTABLE_INPUT, f"no manifest for {snapshot_id}")
    chosen: Optional[Path] = None
    for path in matches:
        import json

        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("snapshot_id") != snapshot_id:
            continue
        if manifest.get("ohlcv_content_hash") != price_hash or manifest.get("revision_metadata_hash") != revision_hash:
            continue
        if manifest.get("snapshot_completeness_status") != "COMPLETE":
            continue
        chosen = path.parent
        break
    if chosen is None:
        raise AdapterStop(INPUT_IDENTITY_MISMATCH, "manifest hashes do not match the requested identity")
    return chosen


def candle_window_status(
    *,
    range_start: datetime,
    range_end_exclusive: datetime,
    decision_close: datetime,
    known_ats: Sequence[datetime],
    timeframe: str,
) -> str:
    step = timeframe_seconds(timeframe)
    if step < 60 or step % 60 != 0:
        return INVALID_REQUEST
    dc = _utc(decision_close)
    start = _utc(range_start)
    end = _utc(range_end_exclusive)
    if int(dc.timestamp()) % step != 0:
        return INVALID_REQUEST
    first_1m = int(dc.timestamp()) - step
    last_1m = int(dc.timestamp()) - 60
    if first_1m < int(start.timestamp()) or last_1m >= int(end.timestamp()):
        return INSUFFICIENT_CANDLE_COVERAGE
    for known in known_ats:
        known_u = _utc(known)
        if int(known_u.timestamp()) < int(start.timestamp()):
            return INSUFFICIENT_CANDLE_COVERAGE
        if known_u > dc:
            return INVALID_REQUEST
    return READY


def resolve_publication(hits: Sequence[PublicationHit]) -> tuple[str, str, tuple[str, ...]]:
    if not hits:
        raise AdapterStop(MISSING_PUBLICATION, "no completed publication")
    ids = {h.snapshot_id for h in hits}
    fingerprints = {h.active_state_fingerprint for h in hits}
    if len(ids) != 1 or len(fingerprints) != 1:
        raise AdapterStop(AMBIGUOUS_PUBLICATION, "publish rows disagree on snapshot id or membership fingerprint")
    return hits[0].snapshot_id, hits[0].active_state_fingerprint, tuple(sorted({h.pipeline_line_id for h in hits}))


def check_membership(snapshot_id: str, fingerprint: str, members: Sequence[MemberRow], catalog: Sequence[CatalogRow]) -> None:
    got = active_state_fingerprint([m.pool_id for m in members], [m.side for m in members])
    if got != fingerprint:
        raise AdapterStop(MISSING_MEMBERSHIP, "membership fingerprint does not match the publication")
    member_ids = {m.pool_id for m in members}
    catalog_ids = {c.pool_id for c in catalog}
    if not member_ids and not catalog_ids:
        return
    if member_ids and not catalog_ids:
        raise AdapterStop(MISSING_CATALOG, f"no catalog rows for {snapshot_id}")
    if member_ids != catalog_ids:
        raise AdapterStop(MISSING_CATALOG, "catalog pool ids do not match active membership")


def project_active_pools(
    catalog: Sequence[CatalogRow],
    candles: Sequence,
    *,
    decision_close: datetime,
    timeframe: str,
    segment_count: int = DEFAULT_SEGMENT_COUNT,
) -> dict:
    dc = _utc(decision_close)
    dc_unix = int(dc.timestamp())
    usable = []
    for candle in candles:
        close_unix = int(bar_close(ensure_utc(candle.timestamp), timeframe).timestamp())
        if close_unix > dc_unix:
            continue
        usable.append((close_unix, candle))
    if any(close_unix > dc_unix for close_unix, _candle in usable):
        raise AdapterStop(INVALID_REQUEST, "future candle passed the filter")
    index = CandleRangeIndex(
        [item[0] for item in usable],
        [float(item[1].high) for item in usable],
        [float(item[1].low) for item in usable],
    )
    active = []
    remaining = []
    traversed = []
    for row in catalog:
        if row.strength_class not in STRENGTH_CLASS_COLORS:
            raise AdapterStop(INVALID_REQUEST, f"unknown strength class {row.strength_class}")
        color = STRENGTH_CLASS_COLORS[row.strength_class]
        span = index.eligible_index_span(int(_utc(row.known_at).timestamp()), dc_unix)
        if span is None:
            pen_h = pen_l = None
        else:
            pen_h, pen_l = index.query(*span)
        rem_t, rem_b, frac, full, _ph, _pl = remaining_from_penetration(
            row.side,
            row.bottom,
            row.top,
            pen_h if row.side == "upper" else None,
            pen_l if row.side == "lower" else None,
        )
        segments = build_fixed_segments(
            original_bottom=row.bottom,
            original_top=row.top,
            remaining_bottom=rem_b,
            remaining_top=rem_t,
            strength_class=row.strength_class,
            segment_count=segment_count,
        )
        if not segments or segments[0].segment_bottom != row.bottom or segments[-1].segment_top != row.top:
            raise AdapterStop(INVALID_REQUEST, f"segment grid left the original bounds for {row.pool_id}")
        if segments[0].display_color != color:
            raise AdapterStop(INVALID_REQUEST, f"strength color mismatch for {row.pool_id}")
        active.append(
            {
                "pool_id": row.pool_id,
                "side": row.side,
                "top": row.top,
                "bottom": row.bottom,
                "strength": row.strength,
                "strength_class": row.strength_class,
                "known_at": _iso(row.known_at),
                "color": color,
            }
        )
        if full or frac <= 0:
            traversed.append(row.pool_id)
            continue
        for seg in segments:
            if not seg.segment_visible or seg.visible_top <= seg.visible_bottom:
                continue
            remaining.append(
                {
                    "pool_id": row.pool_id,
                    "top": seg.visible_top,
                    "bottom": seg.visible_bottom,
                    "color": seg.display_color,
                }
            )
    return {"active": active, "remaining": remaining, "fully_traversed_active_ids": traversed}


@dataclass
class CandleCache:
    max_entries: int = 2
    hits: int = 0
    loads: int = 0
    _store: dict = field(default_factory=dict)
    _order: list = field(default_factory=list)

    def get_candles(
        self,
        *,
        root: Path,
        snapshot_id: str,
        price_hash: str,
        revision_hash: str,
        symbol: str,
        timeframe: str,
    ):
        key = (snapshot_id, price_hash, revision_hash, symbol, timeframe)
        cached = self._store.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        directory = resolve_snapshot_dir(root, snapshot_id, price_hash, revision_hash)
        rows = load_snapshot(directory, snapshot_id)
        if (
            not rows
            or rows[0].symbol != symbol
            or ohlcv_content_hash(rows) != price_hash
            or revision_metadata_hash(rows) != revision_hash
        ):
            raise AdapterStop(INPUT_IDENTITY_MISMATCH, "snapshot bytes do not match the requested identity")
        try:
            session = ImmutableSnapshotCandleSession(
                directory,
                snapshot_id,
                ohlcv_hash=price_hash,
                symbol=symbol,
                timeframe=timeframe,
            )
            session._fetch_1m_range(session._cover_lo, session._cover_hi)
        except ImmutableInputCoverageError as exc:
            raise AdapterStop(INSUFFICIENT_CANDLE_COVERAGE, str(exc)) from exc
        except SnapshotError as exc:
            raise AdapterStop(INPUT_IDENTITY_MISMATCH, str(exc)) from exc
        if session.stats.ch_fetch_calls != 0:
            raise AdapterStop(INVALID_REQUEST, "clickhouse candle fallback")
        candles = [session._5m_by_open_unix[k] for k in sorted(session._5m_by_open_unix)]
        import json

        manifest = json.loads((directory / f"{snapshot_id}.manifest.json").read_text(encoding="utf-8"))
        payload = {
            "candles": candles,
            "range_start": _parse_manifest_time(manifest["range_start"]),
            "range_end_exclusive": _parse_manifest_time(manifest["range_end_exclusive"]),
        }
        if len(self._order) >= self.max_entries:
            self._store.pop(self._order.pop(0), None)
        self._store[key] = payload
        self._order.append(key)
        self.loads += 1
        return payload


def _parse_manifest_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def load_historical_remaining(
    source: PublicationSource,
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    decision_close: datetime,
    immutable_candle_snapshot_id: str,
    price_content_hash: str,
    revision_metadata_hash: str,
    snapshot_root: Path,
    cache: Optional[CandleCache] = None,
) -> dict:
    symbol = str(symbol).upper().strip()
    timeframe = str(timeframe).strip()
    if pool_detail < 1 or pool_detail > 10:
        raise AdapterStop(INVALID_REQUEST, "pool_detail must be 1..10")
    try:
        timeframe_seconds(timeframe)
    except ValueError as exc:
        raise AdapterStop(INVALID_REQUEST, str(exc)) from exc
    dc = _utc(decision_close)
    hits = source.find(symbol=symbol, timeframe=timeframe, pool_detail=int(pool_detail), decision_close=dc)
    snapshot_id, fingerprint, lines = resolve_publication(hits)
    members = source.members(snapshot_id)
    catalog = source.catalog(snapshot_id)
    check_membership(snapshot_id, fingerprint, members, catalog)
    directory = resolve_snapshot_dir(
        snapshot_root,
        immutable_candle_snapshot_id,
        price_content_hash,
        revision_metadata_hash,
    )
    import json

    manifest = json.loads((directory / f"{immutable_candle_snapshot_id}.manifest.json").read_text(encoding="utf-8"))
    status = candle_window_status(
        range_start=_parse_manifest_time(manifest["range_start"]),
        range_end_exclusive=_parse_manifest_time(manifest["range_end_exclusive"]),
        decision_close=dc,
        known_ats=[row.known_at for row in catalog],
        timeframe=timeframe,
    )
    if status != READY:
        raise AdapterStop(status, "immutable candle window does not cover this close")
    holder = cache or CandleCache()
    loaded = holder.get_candles(
        root=snapshot_root,
        snapshot_id=immutable_candle_snapshot_id,
        price_hash=price_content_hash,
        revision_hash=revision_metadata_hash,
        symbol=symbol,
        timeframe=timeframe,
    )
    projected = project_active_pools(catalog, loaded["candles"], decision_close=dc, timeframe=timeframe)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "pool_detail": int(pool_detail),
        "decision_close": _iso(dc),
        "published_snapshot_id": snapshot_id,
        "pipeline_line_ids": list(lines),
        "immutable_candle_snapshot_id": immutable_candle_snapshot_id,
        "price_content_hash": price_content_hash,
        "revision_metadata_hash": revision_metadata_hash,
        "coverage": READY,
        "active_count": len(projected["active"]),
        "remaining_segment_count": len(projected["remaining"]),
        "fully_traversed_active_ids": projected["fully_traversed_active_ids"],
        "active": projected["active"],
        "remaining": projected["remaining"],
    }
