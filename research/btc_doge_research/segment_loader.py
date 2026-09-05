"""Segment-scoped loaders for modality backfill."""

from __future__ import annotations

import gc
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .clickhouse import insert, rows, validate_write_sql
from .contracts import TARGET_DATABASE, sanitize_json
from .full_history_contracts import EXPECTED_OI, IMPORTABLE_MODALITIES, ModalityContractError
from .config import OB200_ROOT
from .phase2_day_loader import (
    DayContext,
    OB_COLUMNS,
    day_output_fingerprint,
    insert_liquidations,
    insert_oi,
    insert_profiles,
    insert_trade_buckets,
)
from .ob200_boundary import Ob200FileIndex, collect_ob200_snapshots
from .source_discovery import build_source_discovery
from .source_file_registry import load_source_file
from .phase2_transform import compact_ob_state


@dataclass(frozen=True)
class SegmentContext:
    symbol: str
    modality: str
    segment_start: datetime
    segment_end: datetime
    batch_id: str
    build_id: str
    contract_version: str
    producer_id: str
    source_semantics_version: str
    source_fingerprint: str
    source_path: str = ""
    expected_rows: int = 0
    boundary_auxiliary_path: str = ""
    boundary_auxiliary_fingerprint: str = ""

    def day_context(self) -> DayContext:
        day = self.segment_start.replace(hour=0, minute=0, second=0, microsecond=0)
        return DayContext(
            symbol=self.symbol,
            day_start=day,
            day_end=day + timedelta(days=1),
            batch_id=self.batch_id,
            build_id=self.build_id,
            contract_version=self.contract_version,
            producer_id=self.producer_id,
            source_semantics_version=self.source_semantics_version,
            source_fingerprint=self.source_fingerprint,
        )


def _dml(client: Any, sql: str) -> None:
    validate_write_sql(sql)
    client.command(sql)


def segment_counts(client: Any, ctx: SegmentContext) -> dict[str, int]:
    table_map = {
        "PUBLIC_TRADES": ("research_public_trade_buckets_1s", f"build_id='{ctx.build_id}'"),
        "LIQUIDATIONS": ("research_liquidation_events", f"ingestion_batch_id='{ctx.batch_id}'"),
        "OPEN_INTEREST": ("research_open_interest_observations", f"build_id='{ctx.build_id}'"),
        "OB200": (
            "research_ob200_snapshots_1s",
            f"build_id='{ctx.build_id}' AND snapshot_ts>='{ctx.segment_start:%Y-%m-%d %H:%M:%S}' "
            f"AND snapshot_ts<'{ctx.segment_end:%Y-%m-%d %H:%M:%S}'",
        ),
        "TPO_PROFILE": ("research_tpo_profile_bins_session", f"build_id='{ctx.build_id}'"),
        "VOLUME_PROFILE": ("research_volume_profile_bins_session", f"build_id='{ctx.build_id}'"),
    }
    table, condition = table_map[ctx.modality]
    count = int(rows(client, f"SELECT count() FROM {TARGET_DATABASE}.{table} WHERE {condition}")[0][0])
    return {table: count}


def load_ob_file(client: Any, ctx: SegmentContext, now: datetime) -> dict[str, Any]:
    if not ctx.source_path:
        raise FileNotFoundError("OB200 segment missing source_path")
    existing = segment_counts(client, ctx).get("research_ob200_snapshots_1s", 0)
    if int(existing) > 0:
        cov = rows(
            client,
            f"""SELECT any(coverage_status) FROM {TARGET_DATABASE}.research_ob200_snapshots_1s
                WHERE build_id='{ctx.build_id}'
                  AND snapshot_ts>='{ctx.segment_start:%Y-%m-%d %H:%M:%S}'
                  AND snapshot_ts<'{ctx.segment_end:%Y-%m-%d %H:%M:%S}'""",
        )[0][0]
        return {
            "rows": int(existing),
            "status": "IDEMPOTENT_SKIP",
            "coverage_status": str(cov or "PARTIAL"),
            "levels_200x200": int(existing),
        }
    source = load_source_file(OB200_ROOT / ctx.source_path, OB200_ROOT)
    start = ctx.segment_start
    end = ctx.segment_end
    expected = ctx.expected_rows or int((end - start).total_seconds())
    discovery = build_source_discovery()
    index = Ob200FileIndex.from_discovery(discovery["ob200_files"])
    collected = collect_ob200_snapshots(
        source=source,
        symbol=ctx.symbol,
        start=start,
        end=end,
        index=index,
    )
    if collected.missing_seconds and collected.classification in {
        "MISSING_INITIAL_STATE",
        "PARTIAL_TRUE_GAP",
    }:
        coverage_status = "PARTIAL"
    elif collected.classification == "COMPLETE_WITH_PROVEN_BOUNDARY_SEED":
        coverage_status = "COMPLETE"
    elif len(collected.by_second) == expected:
        coverage_status = "COMPLETE"
    else:
        coverage_status = "PARTIAL"
    if collected.classification == "MISSING_INITIAL_STATE" and not collected.by_second:
        raise RuntimeError(
            f"OB missing initial state: {ctx.symbol} {source.relative_path} gaps={collected.source_gaps}"
        )
    batch = []
    for second in sorted(collected.by_second):
        event, event_count = collected.by_second[second]
        state = compact_ob_state(ctx.symbol, event.bids, event.asks)
        if state["bid_level_count"] != 200 or state["ask_level_count"] != 200:
            raise RuntimeError(f"OB levels not 200/200 at {second}")
        seed_fp = (
            collected.boundary_seed.auxiliary_fingerprint
            if collected.boundary_seed and second == collected.boundary_seed.second
            else source.fingerprint
        )
        batch.append(
            (
                ctx.symbol, second, ctx.producer_id,
                state["bid_price_ticks"], state["bid_quantities"],
                state["ask_price_ticks"], state["ask_quantities"],
                state["best_bid"], state["best_ask"], state["mid"],
                state["spread"], state["bid_level_count"],
                state["ask_level_count"], state["genuine_depth"],
                "EVENT_TIME_END_OF_SECOND", event_count,
                event.event_time, event.update_id, "EXACT_EVENT_ORDER",
                seed_fp, ctx.contract_version,
                ctx.source_semantics_version, ctx.build_id, coverage_status, now,
            )
        )
    rows_written = len(batch)
    insert(client, "research_ob200_snapshots_1s", batch, OB_COLUMNS)
    result = sanitize_json(
        {
            "rows": rows_written,
            "levels_200x200": rows_written,
            "coverage_status": coverage_status,
            "classification": collected.classification,
            "expected_rows": expected,
            "missing_seconds": collected.source_gaps,
            "boundary_seed_source": (
                collected.boundary_seed.boundary_seed_source if collected.boundary_seed else ""
            ),
            "state_proven": bool(collected.boundary_seed and collected.boundary_seed.state_proven),
            "carried_forward": bool(collected.boundary_seed and collected.boundary_seed.carried_forward),
        }
    )
    del collected, batch
    gc.collect()
    return result


def load_ob_hour(client: Any, ctx: SegmentContext, now: datetime) -> dict[str, Any]:
    return load_ob_file(client, ctx, now)


def load_public_trades_day(client: Any, ctx: SegmentContext, now: datetime) -> dict[str, Any]:
    insert_trade_buckets(client, ctx.day_context(), now)
    counts = segment_counts(client, ctx)
    return {"rows": counts.get("research_public_trade_buckets_1s", 0)}


def load_liquidations_day(client: Any, ctx: SegmentContext, now: datetime) -> dict[str, Any]:
    count = insert_liquidations(client, ctx.day_context(), now)
    return {"rows": count}


def load_open_interest_day(client: Any, ctx: SegmentContext, now: datetime) -> dict[str, Any]:
    day_ctx = ctx.day_context()
    start, end = day_ctx.day_start, day_ctx.day_end
    expected = rows(
        client,
        """SELECT count() FROM orderbook_analysis.open_interest_5s
           WHERE symbol=%(symbol)s AND bucket_time>=%(start)s AND bucket_time<%(end)s""",
        {"symbol": ctx.symbol, "start": start, "end": end},
    )[0][0]
    coverage = "COMPLETE" if int(expected) == EXPECTED_OI else "PARTIAL"
    insert_oi(client, day_ctx, now, coverage_status=coverage)
    counts = segment_counts(client, ctx)
    row_count = counts.get("research_open_interest_observations", 0)
    return {"rows": row_count, "coverage_status": coverage}


def load_profiles_day(client: Any, ctx: SegmentContext, now: datetime) -> dict[str, Any]:
    existing = rows(
        client,
        f"""SELECT count() FROM {TARGET_DATABASE}.research_tpo_profile_bins_session
            WHERE build_id='{ctx.build_id}'""",
    )[0][0]
    if int(existing):
        return {"rows": int(existing), "status": "IDEMPOTENT_SKIP"}
    rows_written = insert_profiles(client, ctx.day_context(), now)
    return {"rows": rows_written}


def load_segment(client: Any, ctx: SegmentContext, now: datetime) -> dict[str, Any]:
    if ctx.modality not in IMPORTABLE_MODALITIES:
        raise ModalityContractError(
            f"modality {ctx.modality} is COVERAGE_ONLY or not importable; "
            "must not reach segment_loader.load_segment()"
        )
    loaders = {
        "OB200": load_ob_hour,
        "PUBLIC_TRADES": load_public_trades_day,
        "LIQUIDATIONS": load_liquidations_day,
        "OPEN_INTEREST": load_open_interest_day,
        "TPO_PROFILE": load_profiles_day,
        "VOLUME_PROFILE": load_profiles_day,
    }
    loader = loaders.get(ctx.modality)
    if not loader:
        raise ValueError(f"unsupported modality: {ctx.modality}")
    return loader(client, ctx, now)


def segment_output_fingerprint(client: Any, ctx: SegmentContext) -> str:
    return day_output_fingerprint(segment_counts(client, ctx))
