"""Single source of truth for historical LMV2 pool state (read-only CH + phase11u_b).

Domain state only — no chart caps, colors, or viewport culling. Chart/backtester adapters
must consume this module rather than reimplementing membership or RemainingPoolProjectionV1.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
    UnpublishedSnapshotError,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_visibility import (
    LIVE_TREATS_BACKFILL_AS_SIGNAL,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    AdapterStop,
    ClickHousePublicationSource,
    load_historical_remaining,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import (
    STRENGTH_CLASS_COLORS,
)

from .chart_live_micro_lld_v2 import _parse_close
from .clickhouse_config import load_clickhouse_config
from .short_pool_oracle_v1 import parse_decision_iso

VISIBILITY_MODE = "RESEARCH_HISTORICAL"
PIPELINE_LINE_ID = "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1"
PILOT_SYMBOL = "DOGEUSDT"
PILOT_TIMEFRAME = "15m"
PILOT_POOL_DETAIL = 10
PILOT_FROM = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
PILOT_TO = datetime(2026, 8, 1, 23, 45, tzinfo=timezone.utc)
PILOT_STEP = timedelta(minutes=15)
PILOT_MAX_CLOSES = 96  # one UTC day @ 15m
# Research chart GO TO / lazy window (Aug–Sep 2026 production line; Oct fail-closed until verified).
RESEARCH_CHART_RELEASED_FROM = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
RESEARCH_CHART_RELEASED_TO_NOMINAL = datetime(2026, 9, 30, 23, 45, tzinfo=timezone.utc)
RESEARCH_CHART_OCTOBER_FROM = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
RESEARCH_CHART_OCTOBER_TO_NOMINAL = datetime(2026, 10, 31, 23, 45, tzinfo=timezone.utc)
# Phase 5N — Apr–Sep imported catalog (static timeline loads; no October reconstruction).
STATIC_TIMELINE_IMPORT_FROM = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
STATIC_TIMELINE_IMPORT_TO_NOMINAL = datetime(2026, 9, 30, 23, 45, tzinfo=timezone.utc)
STATIC_TIMELINE_MAX_DISPLAY_CLOSES = PILOT_MAX_CLOSES * 3  # 3 UTC days @ 15m
EPISODE_MEMBERSHIP_WARMUP_CLOSES = PILOT_MAX_CLOSES - 1

_PUBLISHED_CLOSES_CACHE: dict[tuple[str, str], list[datetime]] = {}
PILOT_MAX_REMAINING_SEGMENTS = 6500  # display ceiling; full projection unchanged

IMMUTABLE_SNAPSHOT_ID = "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93"
IMMUTABLE_PRICE_HASH = "d489aa686f6f648850f248d4809338abbee0d7ab6f0bda3ec5ffe3e698757f48"
IMMUTABLE_REVISION_HASH = "9a3ba3066376d27aabc81ffe01b9b08741e819ba5095bf895f8dd0125812258e"
_LMV2_ROOT = Path(__file__).resolve().parent / "live_micro_lld_v2_ch_test"
_IMMUTABLE_INPUTS_DIR = _LMV2_ROOT / "doge_apr_sep_2026_v1_history" / "inputs"
SNAPSHOT_ROOT = _IMMUTABLE_INPUTS_DIR.resolve() if _IMMUTABLE_INPUTS_DIR.is_dir() else _LMV2_ROOT

_POOL_TYPE_RE = re.compile(r"^lldv2:[^:]+:[^:]+:([^:]+):")


class HistoricalPoolStateError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class HistoricalPoolStateAtClose:
    """Causal pool state at one decision close (domain truth)."""

    pipeline_line_id: str
    symbol: str
    timeframe: str
    pool_detail: int
    decision_close: str
    snapshot_id: str
    active_state_fingerprint: str
    active_members: list[dict[str, Any]]
    remaining_projection: dict[str, Any]
    remaining_segments: list[dict[str, Any]]
    remaining_fingerprint: str
    visibility_mode: str = VISIBILITY_MODE
    immutable_candle_snapshot_id: str = IMMUTABLE_SNAPSHOT_ID
    config_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_line_id": self.pipeline_line_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pool_detail": self.pool_detail,
            "decision_close": self.decision_close,
            "snapshot_id": self.snapshot_id,
            "active_state_fingerprint": self.active_state_fingerprint,
            "active_members": self.active_members,
            "remaining_segment_count": len(self.remaining_segments),
            "remaining_fingerprint": self.remaining_fingerprint,
            "visibility_mode": self.visibility_mode,
            "immutable_candle_snapshot_id": self.immutable_candle_snapshot_id,
            "config_hash": self.config_hash,
        }


def pilot_decision_closes() -> list[datetime]:
    """Phase 5C/5I single UTC day (Aug 1) — audit scripts only; chart uses published closes."""
    out: list[datetime] = []
    cur = PILOT_FROM
    while cur <= PILOT_TO:
        out.append(cur)
        cur += PILOT_STEP
    return out


def clear_published_closes_cache() -> None:
    _PUBLISHED_CLOSES_CACHE.clear()


def get_published_decision_closes(
    reader: PublishedActiveReader,
    cfg_hash: str,
    *,
    pipeline_line_id: str = PIPELINE_LINE_ID,
) -> list[datetime]:
    key = (pipeline_line_id, cfg_hash)
    if key not in _PUBLISHED_CLOSES_CACHE:
        _PUBLISHED_CLOSES_CACHE[key] = reader.list_published_closes(
            pipeline_line_id, config_hash=cfg_hash
        )
    return _PUBLISHED_CLOSES_CACHE[key]


def _closes_in_nominal_research_window(published: Sequence[datetime]) -> list[datetime]:
    return [c for c in published if RESEARCH_CHART_RELEASED_FROM <= c <= RESEARCH_CHART_RELEASED_TO_NOMINAL]


def _closes_in_october_nominal(published: Sequence[datetime]) -> list[datetime]:
    return [
        c
        for c in published
        if RESEARCH_CHART_OCTOBER_FROM <= c <= RESEARCH_CHART_OCTOBER_TO_NOMINAL
    ]


def _expected_15m_closes_in_range(from_dt: datetime, to_dt: datetime) -> int:
    if from_dt > to_dt:
        return 0
    step_sec = int(PILOT_STEP.total_seconds())
    return int((to_dt - from_dt).total_seconds() // step_sec) + 1


def research_chart_data_bounds(
    published: Sequence[datetime],
    *,
    pipeline_line_id: str = PIPELINE_LINE_ID,
) -> dict[str, Any]:
    """Released chart window derived from CH publications (fail-closed on gaps in Aug–Sep)."""
    aug_sep = _closes_in_nominal_research_window(published)
    october = _closes_in_october_nominal(published)
    expected_aug_sep = _expected_15m_closes_in_range(
        RESEARCH_CHART_RELEASED_FROM, RESEARCH_CHART_RELEASED_TO_NOMINAL
    )
    aug_sep_gapless = len(aug_sep) == expected_aug_sep and (
        not aug_sep or aug_sep[0] == RESEARCH_CHART_RELEASED_FROM and aug_sep[-1] == RESEARCH_CHART_RELEASED_TO_NOMINAL
    )
    released_to = aug_sep[-1] if aug_sep_gapless else (
        aug_sep[-1] if aug_sep else RESEARCH_CHART_RELEASED_FROM - PILOT_STEP
    )
    oct_expected = _expected_15m_closes_in_range(
        RESEARCH_CHART_OCTOBER_FROM, RESEARCH_CHART_OCTOBER_TO_NOMINAL
    )
    oct_gapless = len(october) == oct_expected and (
        not october
        or october[0] == RESEARCH_CHART_OCTOBER_FROM and october[-1] == RESEARCH_CHART_OCTOBER_TO_NOMINAL
    )
    if not aug_sep:
        aug_sep_status = "NOT_IMPORTED"
    elif aug_sep_gapless:
        aug_sep_status = "VERIFIED_AVAILABLE"
    elif len(aug_sep) > 0:
        aug_sep_status = "PARTIALLY_AVAILABLE"
    else:
        aug_sep_status = "NOT_VERIFIED"
    if not october:
        oct_status = "NOT_IMPORTED"
    elif oct_gapless:
        oct_status = "VERIFIED_AVAILABLE"
    elif len(october) > 0:
        oct_status = "PARTIALLY_AVAILABLE"
    else:
        oct_status = "NOT_VERIFIED"
    return {
        "pipeline_line_id": pipeline_line_id,
        "released_from": _iso_z(RESEARCH_CHART_RELEASED_FROM),
        "released_to_nominal": _iso_z(RESEARCH_CHART_RELEASED_TO_NOMINAL),
        "released_to": _iso_z(released_to) if aug_sep_gapless else _iso_z(released_to),
        "released_to_effective": _iso_z(released_to),
        "chart_released_gapless": aug_sep_gapless,
        "max_closes_per_request": PILOT_MAX_CLOSES,
        "august_september": {
            "status": aug_sep_status,
            "published_close_count": len(aug_sep),
            "expected_close_count": expected_aug_sep,
            "first_published": _iso_z(aug_sep[0]) if aug_sep else None,
            "last_published": _iso_z(aug_sep[-1]) if aug_sep else None,
        },
        "october": {
            "status": oct_status,
            "chart_released": False,
            "published_close_count": len(october),
            "expected_close_count_full_month": oct_expected,
            "first_published": _iso_z(october[0]) if october else None,
            "last_published": _iso_z(october[-1]) if october else None,
        },
    }


def effective_research_chart_released_to(published: Sequence[datetime]) -> datetime:
    bounds = research_chart_data_bounds(published)
    if not bounds["chart_released_gapless"]:
        raise HistoricalPoolStateError(
            "OUT_OF_PILOT_SCOPE",
            "research chart Aug–Sep publications not gapless; fail-closed",
            bounds,
        )
    return _parse_close(bounds["released_to_effective"])


def published_closes_apr_sep_import(published: Sequence[datetime]) -> list[datetime]:
    return [
        c
        for c in published
        if STATIC_TIMELINE_IMPORT_FROM <= c <= STATIC_TIMELINE_IMPORT_TO_NOMINAL
    ]


def static_timeline_import_bounds(published: Sequence[datetime]) -> tuple[datetime, datetime]:
    closes = published_closes_apr_sep_import(published)
    if not closes:
        raise HistoricalPoolStateError(
            "OUT_OF_PILOT_SCOPE",
            "no published closes in Apr–Sep static timeline import range",
            {
                "import_from": _iso_z(STATIC_TIMELINE_IMPORT_FROM),
                "import_to_nominal": _iso_z(STATIC_TIMELINE_IMPORT_TO_NOMINAL),
            },
        )
    return closes[0], closes[-1]


def assert_static_timeline_window(
    from_dt: datetime,
    to_dt: datetime,
    published: Sequence[datetime],
) -> None:
    lo, hi = static_timeline_import_bounds(published)
    if from_dt < lo or to_dt > hi or from_dt > to_dt:
        raise HistoricalPoolStateError(
            "OUT_OF_PILOT_SCOPE",
            "static timeline request outside Apr–Sep imported publications",
            {
                "import_from": _iso_z(lo),
                "import_to_effective": _iso_z(hi),
                "requested_from": _iso_z(from_dt),
                "requested_to": _iso_z(to_dt),
            },
        )
    close_count = len(decision_closes_in_range(published, from_dt, to_dt))
    if close_count > STATIC_TIMELINE_MAX_DISPLAY_CLOSES:
        raise HistoricalPoolStateError(
            "INVALID_REQUEST",
            f"static timeline window exceeds {STATIC_TIMELINE_MAX_DISPLAY_CLOSES} display closes",
            {
                "requested_close_count": close_count,
                "max_display_closes": STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
            },
        )


def assert_research_chart_window(
    from_dt: datetime,
    to_dt: datetime,
    published: Sequence[datetime],
) -> None:
    released_to = effective_research_chart_released_to(published)
    if from_dt < RESEARCH_CHART_RELEASED_FROM or to_dt > released_to or from_dt > to_dt:
        raise HistoricalPoolStateError(
            "OUT_OF_PILOT_SCOPE",
            "request outside released research chart window",
            {
                "released_from": _iso_z(RESEARCH_CHART_RELEASED_FROM),
                "released_to": _iso_z(released_to),
                "requested_from": _iso_z(from_dt),
                "requested_to": _iso_z(to_dt),
            },
        )


def decision_closes_in_range(
    published: Sequence[datetime],
    from_dt: datetime,
    to_dt: datetime,
) -> list[datetime]:
    i0 = bisect.bisect_left(published, from_dt)
    i1 = bisect.bisect_right(published, to_dt)
    return list(published[i0:i1])


def resolve_as_of_close(published: Sequence[datetime], as_of_dt: datetime) -> datetime:
    i = bisect.bisect_right(published, as_of_dt) - 1
    if i < 0:
        raise HistoricalPoolStateError("OUT_OF_PILOT_SCOPE", "as_of_close before first publication")
    return published[i]


def episode_closes_for_chart_request(
    published: Sequence[datetime],
    from_dt: datetime,
    as_of_dt: datetime,
) -> list[datetime]:
    """Membership walk: up to 96 closes before window start, through as_of (causal)."""
    as_of_dt = resolve_as_of_close(published, as_of_dt)
    i_as_of = bisect.bisect_right(published, as_of_dt) - 1
    i_from = bisect.bisect_left(published, from_dt)
    i_start = max(0, min(i_from, i_as_of) - EPISODE_MEMBERSHIP_WARMUP_CLOSES)
    return list(published[i_start : i_as_of + 1])


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pool_type_from_id(pool_id: str, fallback: str | None) -> str:
    if fallback:
        return str(fallback)
    m = _POOL_TYPE_RE.match(str(pool_id))
    return m.group(1) if m else "normal"


def remaining_fingerprint(rem: dict[str, Any]) -> str:
    rem_rows = sorted(
        (r["pool_id"], r["top"], r["bottom"], r.get("color"))
        for r in rem.get("remaining") or []
    )
    act_rows = sorted(
        (a["pool_id"], a["top"], a["bottom"], a["strength"], a["known_at"])
        for a in rem.get("active") or []
    )
    payload = {
        "snapshot_id": rem.get("published_snapshot_id"),
        "active": act_rows,
        "remaining": rem_rows,
        "traversed": sorted(rem.get("fully_traversed_active_ids") or []),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


class LineFilteredPublicationSource(ClickHousePublicationSource):
    def __init__(self, client, database: str, *, pipeline_line_id: str = PIPELINE_LINE_ID):
        super().__init__(client, database)
        self._pipeline_line_id = pipeline_line_id

    def find(self, *, symbol: str, timeframe: str, pool_detail: int, decision_close: datetime):
        hits = super().find(
            symbol=symbol,
            timeframe=timeframe,
            pool_detail=pool_detail,
            decision_close=decision_close,
        )
        return [h for h in hits if h.pipeline_line_id == self._pipeline_line_id]


def _load_publication_line(
    reader: PublishedActiveReader,
    cfg_hash: str,
    decision_close: datetime,
    *,
    pipeline_line_id: str = PIPELINE_LINE_ID,
) -> dict[str, Any]:
    return reader.load_published(
        pipeline_line_id, decision_close, config_hash=cfg_hash
    )


def _load_catalog(client, database: str, snapshot_id: str, decision_close: datetime) -> list[dict]:
    rows = client.query(
        f"""
        SELECT pool_id, side, pool_type, top_price, bottom_price, strength, strength_class,
               known_at, break_at, confirmed_at, invalidated_at
        FROM {database}.research_lmv2_snapshot_active_pool_v1
        WHERE snapshot_id = {{sid:String}}
        """,
        parameters={"sid": snapshot_id},
    ).result_rows
    dc_iso = _iso_z(decision_close)
    out = []
    for r in rows:
        known = r[7]
        if known is not None and _utc(known) > decision_close:
            raise HistoricalPoolStateError(
                "CAUSALITY_VIOLATION",
                f"known_at after decision_close for {r[0]}",
                {"pool_id": str(r[0]), "decision_close": dc_iso},
            )
        out.append(
            {
                "pool_id": str(r[0]),
                "side": str(r[1]).lower(),
                "pool_type": str(r[2]),
                "top": float(r[3]),
                "bottom": float(r[4]),
                "strength": float(r[5]),
                "strength_class": str(r[6]),
                "known_at": _iso_z(r[7]) if r[7] is not None else None,
                "break_at": _iso_z(r[8]) if r[8] is not None else None,
                "confirmed_at": _iso_z(r[9]) if r[9] is not None else None,
                "invalidated_at": _iso_z(r[10]) if r[10] is not None else None,
                "decision_close": dc_iso,
                "active_membership": True,
            }
        )
    return out


def _active_members_from_catalog(
    catalog: list[dict],
    snapshot_id: str,
    *,
    pipeline_line_id: str = PIPELINE_LINE_ID,
) -> list[dict[str, Any]]:
    members = []
    for row in catalog:
        members.append(
            {
                "pipeline_line_id": pipeline_line_id,
                "snapshot_id": snapshot_id,
                "pool_id": row["pool_id"],
                "side": row["side"],
                "pool_type": _pool_type_from_id(row["pool_id"], row.get("pool_type")),
                "lower_bound": row["bottom"],
                "upper_bound": row["top"],
                "strength": row["strength"],
                "strength_class": row["strength_class"],
                "known_at": row["known_at"],
                "break_at": row.get("break_at"),
                "confirmed_at": row.get("confirmed_at"),
                "invalidated_at": row.get("invalidated_at"),
                "decision_close": row["decision_close"],
                "active_membership": True,
                "removal_cause": "UNCLASSIFIED",
            }
        )
    return members


def load_historical_pool_state_at_close(
    decision_close: datetime | str,
    *,
    symbol: str = PILOT_SYMBOL,
    timeframe: str = PILOT_TIMEFRAME,
    pool_detail: int = PILOT_POOL_DETAIL,
    client=None,
    database: str | None = None,
    reader: PublishedActiveReader | None = None,
    cfg_hash: str | None = None,
    include_remaining: bool = True,
) -> HistoricalPoolStateAtClose:
    from .historical_lmv2_pd_contract_v1 import resolve_historical_pd_config

    dc = _parse_close(decision_close)
    reader = reader or PublishedActiveReader()
    hcfg = resolve_historical_pd_config(pool_detail, timeframe=timeframe)
    cfg_hash = cfg_hash or hcfg.config_hash
    line_id = hcfg.pipeline_line_id
    published = get_published_decision_closes(
        reader, cfg_hash, pipeline_line_id=line_id
    )
    i = bisect.bisect_left(published, dc)
    if i >= len(published) or published[i] != dc:
        raise HistoricalPoolStateError("MISSING_PUBLICATION", "no published snapshot at decision_close")
    sym = symbol.upper()
    if sym != hcfg.symbol or timeframe != hcfg.timeframe:
        raise HistoricalPoolStateError(
            "INVALID_SCOPE",
            "historical scope symbol/timeframe mismatch",
            {"symbol": sym, "timeframe": timeframe, "pool_detail": hcfg.pool_detail},
        )
    own_client = client is None
    if client is None:
        ch_cfg = load_clickhouse_config()
        import clickhouse_connect

        client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
        database = ch_cfg.database
    try:
        pub = _load_publication_line(
            reader, cfg_hash, dc, pipeline_line_id=line_id
        )
        catalog = _load_catalog(client, database, pub["snapshot_id"], dc)
        member_ids = set(pub["pool_ids"])
        if member_ids != {r["pool_id"] for r in catalog}:
            raise HistoricalPoolStateError("MEMBER_CATALOG_MISMATCH", "active membership vs catalog")
        active_members = _active_members_from_catalog(
            catalog, pub["snapshot_id"], pipeline_line_id=line_id
        )
        if include_remaining:
            source = LineFilteredPublicationSource(
                client, database, pipeline_line_id=line_id
            )
            try:
                rem = load_historical_remaining(
                    source,
                    symbol=sym,
                    timeframe=timeframe,
                    pool_detail=pool_detail,
                    decision_close=dc,
                    immutable_candle_snapshot_id=IMMUTABLE_SNAPSHOT_ID,
                    price_content_hash=IMMUTABLE_PRICE_HASH,
                    revision_metadata_hash=IMMUTABLE_REVISION_HASH,
                    snapshot_root=SNAPSHOT_ROOT,
                )
            except AdapterStop as exc:
                raise HistoricalPoolStateError(exc.status, str(exc)) from exc
            rem["decision_close"] = _iso_z(dc)
            segments = list(rem.get("remaining") or [])
        else:
            rem = {
                "decision_close": _iso_z(dc),
                "remaining": [],
                "active": [],
                "published_snapshot_id": pub["snapshot_id"],
                "active_only_load": True,
            }
            segments = []
        return HistoricalPoolStateAtClose(
            pipeline_line_id=line_id,
            symbol=sym,
            timeframe=timeframe,
            pool_detail=hcfg.pool_detail,
            decision_close=_iso_z(dc),
            snapshot_id=pub["snapshot_id"],
            active_state_fingerprint=pub["active_state_fingerprint"],
            active_members=active_members,
            remaining_projection=rem,
            remaining_segments=segments,
            remaining_fingerprint=remaining_fingerprint(rem),
            config_hash=cfg_hash,
        )
    finally:
        if own_client:
            client.close()


def build_active_episode_records(
    reader: PublishedActiveReader,
    cfg_hash: str,
    client,
    database: str,
    closes: Sequence[datetime],
    episode_batch: Any | None = None,
    *,
    pipeline_line_id: str = PIPELINE_LINE_ID,
) -> list[dict[str, Any]]:
    """Membership-bounded ACTIVE episodes across decision closes."""
    if not closes:
        return []
    episodes: dict[str, dict[str, Any]] = {}
    open_members: dict[str, dict[str, Any]] = {}
    window_end_unix = int(closes[-1].timestamp())

    for i, dc in enumerate(closes):
        if episode_batch is not None:
            pub = episode_batch.publication(cfg_hash, dc)
            sid = pub["snapshot_id"]
            catalog = episode_batch.catalog(database, sid, dc)
        else:
            pub = _load_publication_line(
                reader, cfg_hash, dc, pipeline_line_id=pipeline_line_id
            )
            sid = pub["snapshot_id"]
            catalog = _load_catalog(client, database, sid, dc)
        for row in catalog:
            row["snapshot_id"] = sid
        by_id = {row["pool_id"]: row for row in catalog}
        member_ids = set(pub["pool_ids"])
        if member_ids != set(by_id):
            raise HistoricalPoolStateError(
                "MEMBER_CATALOG_MISMATCH",
                f"member/catalog mismatch at {_iso_z(dc)}",
            )
        prev_dc = closes[i - 1] if i > 0 else None
        for pid in list(open_members):
            if pid not in member_ids:
                ep = open_members.pop(pid)
                end_dc = prev_dc if prev_dc is not None else dc
                ep["membership_end_close_unix"] = int(end_dc.timestamp())
                ep["membership_exit_observed_at"] = _iso_z(dc)
                ep["right_censored"] = False
                ep["removal_cause"] = "UNCLASSIFIED"
                episodes[pid + ":" + str(ep["generation"])] = ep
        for pid in member_ids:
            row = by_id[pid]
            if pid in open_members:
                open_members[pid]["last_row"] = row
                open_members[pid]["last_close_unix"] = int(dc.timestamp())
                continue
            open_members[pid] = {
                "generation": len([k for k in episodes if k.startswith(pid + ":")]),
                "first_row": row,
                "last_row": row,
                "membership_start_close_unix": int(dc.timestamp()),
                "last_close_unix": int(dc.timestamp()),
            }
    for pid, ep in open_members.items():
        ep["membership_end_close_unix"] = window_end_unix
        ep["right_censored"] = True
        ep["removal_cause"] = "RIGHT_CENSORED"
        episodes[pid + ":" + str(ep["generation"])] = ep

    pools: list[dict[str, Any]] = []
    for ep in episodes.values():
        row = ep["last_row"]
        known_unix = int(_parse_close(row["known_at"]).timestamp())
        break_at = row.get("break_at")
        end_unix = int(ep["membership_end_close_unix"])
        if break_at:
            end_unix = min(end_unix, int(_parse_close(break_at).timestamp()))
        pools.append(
            {
                "pool_id": row["pool_id"],
                "pool_type": _pool_type_from_id(row["pool_id"], row.get("pool_type")),
                "side": row["side"],
                "known_at": row["known_at"],
                "break_at": break_at,
                "top_price": float(row["top"]),
                "bottom_price": float(row["bottom"]),
                "strength": float(row["strength"]),
                "strength_class": row["strength_class"],
                "membership_start_close_unix": ep.get("membership_start_close_unix"),
                "membership_end_close_unix": int(ep["membership_end_close_unix"]),
                "membership_exit_observed_at": ep.get("membership_exit_observed_at"),
                "right_censored": bool(ep.get("right_censored")),
                "removal_cause": ep.get("removal_cause") or "UNCLASSIFIED",
                "_zone_start_unix": max(known_unix, int(ep.get("membership_start_close_unix", known_unix))),
                "_zone_end_unix": end_unix,
                "snapshot_id": row.get("snapshot_id"),
            }
        )
    return pools


def active_episode_domain_zones(
    pools: list[dict[str, Any]],
    *,
    from_unix: int,
    to_unix: int,
    layer: str,
    namespace: str,
) -> list[dict[str, Any]]:
    """Chart/backtester overlay geometry from membership-bounded episodes (no request-horizon extension)."""
    from indicators.live_micro_lld_v2.styles import BORDER_LOWER, BORDER_UPPER, FILL_BY_CLASS

    zones: list[dict[str, Any]] = []
    for p in pools:
        start = max(from_unix, int(p["_zone_start_unix"]))
        end = min(to_unix, int(p["_zone_end_unix"]))
        if end < start:
            continue
        side = str(p.get("side") or "")
        sc = str(p.get("strength_class") or "")
        border = BORDER_UPPER if side == "upper" else BORDER_LOWER
        fill = FILL_BY_CLASS.get(sc, "#88888833")
        zones.append(
            {
                "id": f"historical-lmv2:{p['pool_id']}",
                "pool_id": p["pool_id"],
                "namespace": namespace,
                "type": "zone",
                "side": side,
                "pool_type": p.get("pool_type"),
                "strength_class": sc,
                "lower_price": float(p["bottom_price"]),
                "upper_price": float(p["top_price"]),
                "bottom_price": float(p["bottom_price"]),
                "top_price": float(p["top_price"]),
                "start_timestamp": int(start),
                "end_timestamp": int(end),
                "style": {"color": border, "fillColor": fill, "lineWidth": 2},
                "metadata": {
                    "source": layer,
                    "visibility_mode": VISIBILITY_MODE,
                    "layer": "active",
                    "snapshot_id": p.get("snapshot_id"),
                    "known_at": p.get("known_at"),
                    "strength": p.get("strength"),
                    "live_availability": "NOT_VERIFIED",
                    "removal_cause": p.get("removal_cause") or "UNCLASSIFIED",
                    "right_censored": bool(p.get("right_censored")),
                    "membership_exit_observed_at": p.get("membership_exit_observed_at"),
                },
            }
        )
    return zones


def remaining_domain_zones(
    rem: dict[str, Any],
    *,
    from_unix: int,
    to_unix: int,
    as_of_unix: int,
    layer: str,
    namespace: str,
) -> list[dict[str, Any]]:
    zones: list[dict[str, Any]] = []
    active_by_id = {a["pool_id"]: a for a in rem.get("active") or []}
    span_start = max(from_unix, as_of_unix - int(PILOT_STEP.total_seconds()))
    span_end = min(to_unix, as_of_unix)
    for seg in rem.get("remaining") or []:
        pid = str(seg["pool_id"])
        act = active_by_id.get(pid)
        if not act:
            continue
        sc = str(act.get("strength_class") or "")
        if sc not in STRENGTH_CLASS_COLORS:
            continue
        bot = float(seg["bottom"])
        top = float(seg["top"])
        if top <= bot:
            continue
        zones.append(
            {
                "id": f"historical-lmv2-rem:{pid}:{bot:.8f}:{top:.8f}",
                "pool_id": pid,
                "namespace": namespace,
                "type": "zone",
                "side": act.get("side"),
                "strength_class": sc,
                "lower_price": bot,
                "upper_price": top,
                "bottom_price": bot,
                "top_price": top,
                "start_timestamp": int(span_start),
                "end_timestamp": int(span_end),
                "style": {"color": seg.get("color") or "#888888", "lineWidth": 1},
                "metadata": {
                    "source": layer,
                    "visibility_mode": VISIBILITY_MODE,
                    "layer": "remaining",
                    "snapshot_id": rem.get("published_snapshot_id"),
                    "decision_close": rem.get("decision_close"),
                    "known_at": act.get("known_at"),
                    "strength": act.get("strength"),
                    "live_availability": "NOT_VERIFIED",
                },
            }
        )
    return zones


def apply_display_filters(
    remaining_zones: list[dict[str, Any]],
    *,
    price_min: float | None,
    price_max: float | None,
    display_limit: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Viewport/display-only filters — never mutates domain segment count in returned meta."""
    full_count = len(remaining_zones)
    filtered = remaining_zones
    if price_min is not None and price_max is not None:
        filtered = [
            z
            for z in filtered
            if float(z["top_price"]) >= price_min and float(z["bottom_price"]) <= price_max
        ]
    after_price = len(filtered)
    limit = int(display_limit) if display_limit is not None else PILOT_MAX_REMAINING_SEGMENTS
    if limit <= 0:
        limit = PILOT_MAX_REMAINING_SEGMENTS
    truncated = len(filtered) > limit
    display = filtered[:limit] if truncated else filtered
    meta = {
        "remaining_segment_count_full": full_count,
        "remaining_segment_count_after_price_filter": after_price,
        "remaining_segment_count_display": len(display),
        "display_remaining_truncated": truncated,
        "display_limit_applied": limit,
    }
    if truncated:
        for z in display:
            z.setdefault("metadata", {})["display_viewport_truncated"] = True
    return display, meta


def load_window_pool_state(
    from_close: datetime | str,
    to_close: datetime | str,
    as_of_close: datetime | str | None = None,
    *,
    symbol: str = PILOT_SYMBOL,
    timeframe: str = PILOT_TIMEFRAME,
    pool_detail: int = PILOT_POOL_DETAIL,
) -> dict[str, Any]:
    """Aggregate state for chart window: per-close publications + domain remaining at as_of."""
    from_dt = _parse_close(from_close)
    to_dt = _parse_close(to_close)
    cfg_hash = config_hash(build_live_micro_lld_v2_config(pool_detail))
    reader = PublishedActiveReader()
    published = get_published_decision_closes(reader, cfg_hash)
    assert_research_chart_window(from_dt, to_dt, published)
    closes = decision_closes_in_range(published, from_dt, to_dt)
    if len(closes) > PILOT_MAX_CLOSES:
        raise HistoricalPoolStateError(
            "INVALID_REQUEST",
            f"more than {PILOT_MAX_CLOSES} closes",
        )
    as_of_dt = _parse_close(as_of_close) if as_of_close else to_dt
    as_of_dt = resolve_as_of_close(published, as_of_dt)
    ch_cfg = load_clickhouse_config()
    import clickhouse_connect

    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    database = ch_cfg.database
    try:
        per_close = []
        for dc in closes:
            st = load_historical_pool_state_at_close(
                dc,
                symbol=symbol,
                timeframe=timeframe,
                pool_detail=pool_detail,
                client=client,
                database=database,
                reader=reader,
                cfg_hash=cfg_hash,
            )
            per_close.append(st.to_dict())
        at_as_of = load_historical_pool_state_at_close(
            as_of_dt,
            symbol=symbol,
            timeframe=timeframe,
            pool_detail=pool_detail,
            client=client,
            database=database,
            reader=reader,
            cfg_hash=cfg_hash,
        )
        ep_closes = episode_closes_for_chart_request(published, from_dt, as_of_dt)
        episodes = build_active_episode_records(reader, cfg_hash, client, database, ep_closes)
        return {
            "from_close": _iso_z(from_dt),
            "to_close": _iso_z(to_dt),
            "as_of_close": _iso_z(as_of_dt),
            "closes": [_iso_z(c) for c in closes],
            "per_close": per_close,
            "as_of_state": at_as_of.to_dict(),
            "active_episodes": episodes,
            "config_hash": cfg_hash,
        }
    finally:
        client.close()
