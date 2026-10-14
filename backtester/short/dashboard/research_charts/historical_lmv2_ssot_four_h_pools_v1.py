"""Read-only 4h ACTIVE pools from Original-LMV2 CH publications (guard support)."""

from __future__ import annotations

import bisect
from datetime import datetime, timezone
from typing import Any

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.historical_lmv2_ssot_legacy_bridge_v1 import bridge_member_to_legacy
from research_charts.historical_pool_state_v1 import (
    HistoricalPoolStateError,
    _active_members_from_catalog,
    _load_catalog,
    _load_publication_line,
    get_published_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)

FOUR_H_PIPELINE_LINE_ID = "historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1"
FOUR_H_POOL_DETAIL = 10
FOUR_H_TIMEFRAME = "4h"
FOUR_H_SYMBOL = "DOGEUSDT"


def four_h_config_hash() -> str:
    return config_hash(build_live_micro_lld_v2_config(FOUR_H_POOL_DETAIL))


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def list_published_4h_closes(reader: PublishedActiveReader | None = None) -> list[datetime]:
    reader = reader or PublishedActiveReader()
    return get_published_decision_closes(
        reader,
        four_h_config_hash(),
        pipeline_line_id=FOUR_H_PIPELINE_LINE_ID,
    )


def inventory_four_h_source() -> dict[str, Any]:
    """Publication inventory for floor-guard 4h SSOT (read-only)."""
    reader = PublishedActiveReader()
    closes = list_published_4h_closes(reader)
    june = [c for c in closes if c.year == 2026 and c.month == 6]
    july = [c for c in closes if c.year == 2026 and c.month == 7]
    sep = [c for c in closes if c.year == 2026 and c.month == 9]
    return {
        "pipeline_line_id": FOUR_H_PIPELINE_LINE_ID,
        "config_hash": four_h_config_hash(),
        "timeframe": FOUR_H_TIMEFRAME,
        "pool_detail": FOUR_H_POOL_DETAIL,
        "total_published_closes": len(closes),
        "june_2026_closes": len(june),
        "july_2026_closes": len(july),
        "september_2026_closes": len(sep),
        "first_close": closes[0].isoformat() if closes else None,
        "last_close": closes[-1].isoformat() if closes else None,
    }


def resolve_published_4h_close_at_or_before(
    moment: datetime | str,
    *,
    reader: PublishedActiveReader | None = None,
) -> datetime:
    """Last published 4h decision_close with close <= moment (causal for guard)."""
    m = _utc(
        moment
        if isinstance(moment, datetime)
        else datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    )
    published = list_published_4h_closes(reader)
    if not published:
        raise HistoricalPoolStateError("MISSING_4H_PUBLICATION", "no 4h publications")
    i = bisect.bisect_right(published, m) - 1
    if i < 0:
        raise HistoricalPoolStateError(
            "MISSING_4H_PUBLICATION",
            "no published 4h close at or before moment",
            {"moment": m.isoformat()},
        )
    return published[i]


def load_ssot_legacy_pools_4h_at_close(
    decision_close: datetime | str,
    *,
    client=None,
    database: str | None = None,
    reader: PublishedActiveReader | None = None,
    allow_nearest_published: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """ACTIVE 4h lowers/uppers as legacy pool dicts at a published 4h decision_close."""
    reader = reader or PublishedActiveReader()
    if allow_nearest_published:
        dc = resolve_published_4h_close_at_or_before(decision_close, reader=reader)
    else:
        dc = _utc(
            decision_close
            if isinstance(decision_close, datetime)
            else datetime.fromisoformat(str(decision_close).replace("Z", "+00:00"))
        )
    cfg_h = four_h_config_hash()
    published = list_published_4h_closes(reader)
    if not allow_nearest_published:
        i = bisect.bisect_left(published, dc)
        if i >= len(published) or published[i] != dc:
            raise HistoricalPoolStateError(
                "MISSING_4H_PUBLICATION",
                "no published 4h snapshot at decision_close",
                {"decision_close": dc.isoformat()},
            )
    else:
        if dc not in published:
            raise HistoricalPoolStateError(
                "MISSING_4H_PUBLICATION",
                "resolved 4h close not in published list",
                {"decision_close": dc.isoformat()},
            )
    own_client = client is None
    if client is None:
        import clickhouse_connect

        ch_cfg = load_clickhouse_config()
        client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
        database = ch_cfg.database
    if database is None and client is not None:
        database = getattr(client, "database", None)
    if not database:
        raise HistoricalPoolStateError("MISSING_DATABASE", "clickhouse database required")

    try:
        pub = _load_publication_line(reader, cfg_h, dc, pipeline_line_id=FOUR_H_PIPELINE_LINE_ID)
        catalog = _load_catalog(client, database, pub["snapshot_id"], dc)
        member_ids = set(pub["pool_ids"])
        if member_ids != {r["pool_id"] for r in catalog}:
            raise HistoricalPoolStateError("MEMBER_CATALOG_MISMATCH", "4h active membership vs catalog")
        members = _active_members_from_catalog(
            catalog, pub["snapshot_id"], pipeline_line_id=FOUR_H_PIPELINE_LINE_ID
        )
        legacy = [bridge_member_to_legacy(m, decision_close=dc) for m in members]
        meta = {
            "decision_close": dc.isoformat(),
            "snapshot_id": pub["snapshot_id"],
            "pipeline_line_id": FOUR_H_PIPELINE_LINE_ID,
            "config_hash": cfg_h,
            "active_member_count": len(legacy),
        }
        return legacy, meta
    finally:
        if own_client:
            client.close()
