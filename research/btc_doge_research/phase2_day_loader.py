"""Shared per-symbol-day loader used by Phase 2 pilot and full-history backfill."""

from __future__ import annotations

import gc
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .clickhouse import insert, rows, validate_write_sql
from .config import OB200_ROOT
from .contracts import TARGET_DATABASE, stable_hash
from .liquidation_transform import LIQUIDATION_COLUMNS, transform_liquidations
from .ob200_parser import OB200SegmentReader
from .phase2_contracts import PHASE2_CONTRACT_VERSION, TICK_SIZE
from .phase2_transform import compact_ob_state
from .source_file_registry import load_source_file
from .source_readers import read_liquidations, read_public_trades

TRADE_BUCKET_COLUMNS = (
    "symbol", "bucket_start", "buy_base_volume", "sell_base_volume",
    "buy_quote_notional", "sell_quote_notional", "taker_delta_quote_notional",
    "buy_trade_count", "sell_trade_count", "first_trade_ts", "last_trade_ts",
    "source_trade_count", "deduplicated_trade_count", "contract_version",
    "source_semantics_version", "build_id", "coverage_status", "computed_at",
)
OB_COLUMNS = (
    "symbol", "snapshot_ts", "producer_id", "bid_price_ticks",
    "bid_quantities", "ask_price_ticks", "ask_quantities", "best_bid",
    "best_ask", "mid", "spread", "bid_level_count", "ask_level_count",
    "genuine_depth", "reconstruction_clock", "source_event_count",
    "source_event_time", "source_update_id", "ordering_quality",
    "source_fingerprint", "contract_version", "source_semantics_version",
    "build_id", "coverage_status", "computed_at",
)


@dataclass(frozen=True)
class DayContext:
    symbol: str
    day_start: datetime
    day_end: datetime
    batch_id: str
    build_id: str
    contract_version: str
    producer_id: str
    source_semantics_version: str
    source_fingerprint: str


def _dml(client: Any, sql: str) -> None:
    validate_write_sql(sql)
    client.command(sql)


def _literal_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def ob_source(symbol: str, hour: datetime):
    end = hour + timedelta(hours=1)
    name = f"{symbol}_{hour:%Y%m%dT%H%M%SZ}_{end:%Y%m%dT%H%M%SZ}_ob200_v3.zst"
    return load_source_file(
        OB200_ROOT / symbol / f"{hour.year:04d}" / f"{hour.month:02d}"
        / f"{hour.day:02d}" / name,
        OB200_ROOT,
    )


def insert_trade_buckets(client: Any, ctx: DayContext, now: datetime) -> None:
    start, end = _literal_time(ctx.day_start), _literal_time(ctx.day_end)
    for milliseconds, suffix in ((100, "100ms"), (500, "500ms"), (1000, "1s")):
        if milliseconds == 100:
            source_sql = f"""
            (
              SELECT trade_id,
                     argMax(src.symbol,ingest_timestamp) logical_symbol,
                     argMax(trade_ts,ingest_timestamp) event_time,
                     argMax(size,ingest_timestamp) size,
                     argMax(notional,ingest_timestamp) notional,
                     argMax(side,ingest_timestamp) side
              FROM orderbook_analysis.public_trades_canonical AS src
              WHERE src.symbol = '{ctx.symbol}'
                AND trade_ts>=toDateTime64('{start}',3,'UTC')
                AND trade_ts<toDateTime64('{end}',3,'UTC')
              GROUP BY trade_id
            )"""
            bucket_expr = "toStartOfInterval(event_time, INTERVAL 100 MILLISECOND)"
            group_symbol = "logical_symbol"
            buy_base = "sumIf(size,side='Buy')"
            sell_base = "sumIf(size,side='Sell')"
            buy_quote = "sumIf(notional,side='Buy')"
            sell_quote = "sumIf(notional,side='Sell')"
            buy_count = "countIf(side='Buy')"
            sell_count = "countIf(side='Sell')"
            first_ts, last_ts, count_expr = "event_time", "event_time", "count()"
            where = ""
        else:
            source_sql = f"{TARGET_DATABASE}.research_public_trade_buckets_100ms"
            bucket_expr = f"toStartOfInterval(bucket_start, INTERVAL {milliseconds} MILLISECOND)"
            group_symbol = "symbol"
            buy_base, sell_base = "sum(buy_base_volume)", "sum(sell_base_volume)"
            buy_quote, sell_quote = "sum(buy_quote_notional)", "sum(sell_quote_notional)"
            buy_count, sell_count = "sum(buy_trade_count)", "sum(sell_trade_count)"
            first_ts, last_ts, count_expr = "first_trade_ts", "last_trade_ts", "sum(source_trade_count)"
            where = f"WHERE build_id='{ctx.build_id}' AND symbol='{ctx.symbol}'"
        sql = f"""
        INSERT INTO {TARGET_DATABASE}.research_public_trade_buckets_{suffix}
        ({','.join(TRADE_BUCKET_COLUMNS)})
        SELECT {group_symbol} AS symbol,{bucket_expr},{buy_base},{sell_base},{buy_quote},{sell_quote},
               {buy_quote}-{sell_quote},{buy_count},{sell_count},
               min({first_ts}),max({last_ts}),{count_expr},{count_expr},
               '{ctx.contract_version}','public_trade_taker_aggressor_v1',
               '{ctx.build_id}','COMPLETE',
               toDateTime64('{now.isoformat()}',6,'UTC')
        FROM {source_sql}
        {where}
        GROUP BY {group_symbol},{bucket_expr}
        """
        _dml(client, sql)


def insert_liquidations(client: Any, ctx: DayContext, now: datetime) -> int:
    source, _ = read_liquidations(client, ctx.symbol, ctx.day_start, ctx.day_end)
    transformed = transform_liquidations(
        source, symbol=ctx.symbol, batch_id=ctx.batch_id, ingested_at=now
    )
    if transformed:
        insert(client, "research_liquidation_events", transformed, LIQUIDATION_COLUMNS)
    sql = f"""
    INSERT INTO {TARGET_DATABASE}.research_liquidation_buckets_500ms
    SELECT symbol,toStartOfInterval(event_time,INTERVAL 500 MILLISECOND),
           countIf(liquidated_position_side='LIQUIDATED_LONG'),
           countIf(liquidated_position_side='LIQUIDATED_SHORT'),
           sumIf(executed_base_size,forced_flow='FORCED_BUY'),
           sumIf(executed_base_size,forced_flow='FORCED_SELL'),
           sum(bankruptcy_reference_quote),count(),
           '{ctx.contract_version}','liquidation_flow_facts_v1','{ctx.build_id}',
           'COMPLETE',toDateTime64('{now.isoformat()}',6,'UTC')
    FROM {TARGET_DATABASE}.research_liquidation_events
    WHERE ingestion_batch_id='{ctx.batch_id}'
    GROUP BY symbol,toStartOfInterval(event_time,INTERVAL 500 MILLISECOND)
    """
    _dml(client, sql)
    return len(transformed)


def insert_oi(client: Any, ctx: DayContext, now: datetime) -> None:
    sql = f"""
    INSERT INTO {TARGET_DATABASE}.research_open_interest_observations
    SELECT symbol,bucket_time,argMax(open_interest,inserted_at),
           argMax(open_interest_value,inserted_at),
           argMax(source_event_time,inserted_at),argMax(state_age_ms,inserted_at),
           argMax(state_valid,inserted_at),5000,'BYBIT_BASE_AND_QUOTE',
           '{ctx.contract_version}','open_interest_5s_v1','{ctx.build_id}',
           'COMPLETE',toDateTime64('{now.isoformat()}',6,'UTC')
    FROM orderbook_analysis.open_interest_5s
    WHERE symbol='{ctx.symbol}'
      AND bucket_time>=toDateTime64('{_literal_time(ctx.day_start)}',3,'UTC')
      AND bucket_time<toDateTime64('{_literal_time(ctx.day_end)}',3,'UTC')
    GROUP BY symbol,bucket_time
    """
    _dml(client, sql)


def insert_profiles(client: Any, ctx: DayContext, now: datetime) -> int:
    orderbook_analyse_src = "/home/telgenbuescher/projects/orderbook_analyse/src"
    if orderbook_analyse_src not in sys.path:
        sys.path.insert(0, orderbook_analyse_src)
    from research.btc_ob_fight.tpo_profile import build_tpo_profile_from_trades
    from research.btc_ob_fight.volume_profile import (
        build_volume_profile_from_trades,
        profile_session_window,
    )

    session_start, anchor, session_id = profile_session_window(ctx.day_end)
    source, stats = read_public_trades(client, ctx.symbol, session_start, anchor)
    trades = [
        {
            "trade_id": str(row[0]),
            "ts": (
                row[1].replace(tzinfo=timezone.utc)
                if row[1].tzinfo is None else row[1].astimezone(timezone.utc)
            ),
            "price": float(row[3]),
            "size": float(row[4]),
            "notional": float(row[5]),
            "side": str(row[6]),
        }
        for row in source
    ]
    ohlc_row = rows(
        client,
        """SELECT argMin(open,open_time),max(high),min(low),argMax(close,open_time)
        FROM signal_generator.candles_1m FINAL
        WHERE symbol=%(symbol)s AND interval='1m'
          AND open_time>=%(start)s AND open_time<%(end)s""",
        {"symbol": ctx.symbol, "start": session_start, "end": anchor},
    )[0]
    ohlc = tuple(float(value) for value in ohlc_row)
    volume = build_volume_profile_from_trades(
        trades, session_start=session_start, anchor=anchor, cl=client,
        symbol=ctx.symbol, compute_prefix=False, ohlc=ohlc,
    )
    tpo = build_tpo_profile_from_trades(
        trades, session_start=session_start, anchor=anchor, cl=client,
        symbol=ctx.symbol, ohlc=ohlc,
    )
    if volume["integrity"]["status"] != "PASS" or tpo["integrity"]["status"] != "PASS":
        raise RuntimeError(f"profile integrity failed: {ctx.symbol} {ctx.day_start:%Y-%m-%d}")
    common = (
        ctx.contract_version, "public_trade_taker_aggressor_v1",
        ctx.build_id, "COMPLETE", now,
    )
    bracket_rows = [
        (
            ctx.symbol, session_id, row["bracket_index"],
            datetime.fromisoformat(row["bracket_start"].replace("Z", "+00:00")),
            datetime.fromisoformat(row["bracket_end_contract"].replace("Z", "+00:00")),
            row["low"], row["high"], row["low_bin"], row["high_bin"],
            row["touched_bin_count"], row["trade_count"], 0, *common,
        )
        for row in tpo["bracket_rows"]
    ]
    insert(
        client, "research_tpo_bracket_ranges_30m", bracket_rows,
        (
            "symbol","session_id","bracket_index","bracket_start","bracket_end",
            "low","high","low_bin","high_bin","touched_bin_count","trade_count",
            "trade_size_used_as_weight","contract_version",
            "source_semantics_version","build_id","coverage_status","computed_at",
        ),
    )
    tpo_rows = [
        (
            ctx.symbol, session_id, row["price_bin_index"],
            int(Decimal(str(row["price"])) / TICK_SIZE[ctx.symbol]),
            Decimal(str(row["price"])), row["tpo_count"], row["tpo_share"],
            row["bracket_count"], int(row["is_poc"]), int(row["is_value_area"]),
            *common,
        )
        for row in tpo["rows"]
    ]
    insert(
        client, "research_tpo_profile_bins_session", tpo_rows,
        (
            "symbol","session_id","price_bin_index","price_bin_tick",
            "display_price","tpo_count","tpo_share","bracket_count","is_poc",
            "is_value_area","contract_version","source_semantics_version",
            "build_id","coverage_status","computed_at",
        ),
    )
    volume_rows = [
        (
            ctx.symbol, session_id, row["price_bin_index"],
            int(Decimal(str(row["display_price"])) / TICK_SIZE[ctx.symbol]),
            Decimal(str(row["price_bin_low"])), Decimal(str(row["price_bin_high"])),
            Decimal(str(row["display_price"])), Decimal(str(row["base_volume"])),
            Decimal(str(row["quote_notional"])), row["trade_count"],
            Decimal(str(row["taker_buy_base_volume"])),
            Decimal(str(row["taker_sell_base_volume"])), *common,
        )
        for row in volume["rows"]
    ]
    insert(
        client, "research_volume_profile_bins_session", volume_rows,
        (
            "symbol","session_id","price_bin_index","price_bin_tick",
            "price_bin_low","price_bin_high","display_price","base_volume",
            "quote_notional","trade_count","taker_buy_base_volume",
            "taker_sell_base_volume","contract_version",
            "source_semantics_version","build_id","coverage_status","computed_at",
        ),
    )
    levels = [
        ("TPO", "POC", tpo["tpoc"]["tpoc_price"], tpo["value_area"]["actual_value_area_share"]),
        ("TPO", "VAH", tpo["value_area"]["tpoc_vah"], tpo["value_area"]["actual_value_area_share"]),
        ("TPO", "VAL", tpo["value_area"]["tpoc_val"], tpo["value_area"]["actual_value_area_share"]),
        ("VOLUME", "POC", volume["vpoc"]["vpoc_price"], volume["value_area"]["actual_value_area_share"]),
        ("VOLUME", "VAH", volume["value_area"]["vvah"], volume["value_area"]["actual_value_area_share"]),
        ("VOLUME", "VAL", volume["value_area"]["vval"], volume["value_area"]["actual_value_area_share"]),
    ]
    insert(
        client, "research_profile_levels_session",
        [(ctx.symbol, session_id, kind, level, Decimal(str(price)), share, *common) for kind, level, price, share in levels],
        (
            "symbol","session_id","profile_kind","level_kind","price",
            "value_area_share","contract_version","source_semantics_version",
            "build_id","coverage_status","computed_at",
        ),
    )
    return len(bracket_rows) + len(tpo_rows) + len(volume_rows) + len(levels)


def insert_ob(client: Any, ctx: DayContext, now: datetime) -> int:
    symbol_count = 0
    for offset in range(24):
        hour = ctx.day_start + timedelta(hours=offset)
        end = hour + timedelta(hours=1)
        source = ob_source(ctx.symbol, hour)
        reader = OB200SegmentReader(source, ctx.symbol)
        by_second: dict[datetime, tuple[Any, int]] = {}
        counts: dict[datetime, int] = {}
        for event in reader.iter_full_books(hour, end):
            second = event.event_time.replace(microsecond=0)
            by_second[second] = (event, counts.get(second, 0) + 1)
            counts[second] = counts.get(second, 0) + 1
        if reader.audit.u_gaps or not reader.audit.full_file_consumed:
            raise RuntimeError(f"OB source invalid: {source.relative_path}")
        if len(by_second) != 3600:
            raise RuntimeError(
                f"OB second gap: {ctx.symbol} {hour} {len(by_second)}"
            )
        batch = []
        for second in sorted(by_second):
            event = by_second[second][0]
            state = compact_ob_state(ctx.symbol, event.bids, event.asks)
            batch.append(
                (
                    ctx.symbol, second, ctx.producer_id,
                    state["bid_price_ticks"], state["bid_quantities"],
                    state["ask_price_ticks"], state["ask_quantities"],
                    state["best_bid"], state["best_ask"], state["mid"],
                    state["spread"], state["bid_level_count"],
                    state["ask_level_count"], state["genuine_depth"],
                    "EVENT_TIME_END_OF_SECOND", counts[second],
                    event.event_time, event.update_id, "EXACT_EVENT_ORDER",
                    source.fingerprint, ctx.contract_version,
                    ctx.source_semantics_version, ctx.build_id, "COMPLETE", now,
                )
            )
        insert(client, "research_ob200_snapshots_1s", batch, OB_COLUMNS)
        symbol_count += len(batch)
        del by_second, counts, batch, reader
        gc.collect()
    return symbol_count


def day_counts(client: Any, ctx: DayContext) -> dict[str, int]:
    tables = {
        "research_public_trade_buckets_100ms": f"build_id='{ctx.build_id}'",
        "research_public_trade_buckets_500ms": f"build_id='{ctx.build_id}'",
        "research_public_trade_buckets_1s": f"build_id='{ctx.build_id}'",
        "research_liquidation_events": f"ingestion_batch_id='{ctx.batch_id}'",
        "research_liquidation_buckets_500ms": f"build_id='{ctx.build_id}'",
        "research_open_interest_observations": f"build_id='{ctx.build_id}'",
        "research_tpo_bracket_ranges_30m": f"build_id='{ctx.build_id}'",
        "research_tpo_profile_bins_session": f"build_id='{ctx.build_id}'",
        "research_volume_profile_bins_session": f"build_id='{ctx.build_id}'",
        "research_profile_levels_session": f"build_id='{ctx.build_id}'",
        "research_ob200_snapshots_1s": f"build_id='{ctx.build_id}'",
    }
    out: dict[str, int] = {}
    for table, condition in tables.items():
        out[table] = int(
            rows(client, f"SELECT count() FROM {TARGET_DATABASE}.{table} WHERE {condition}")[0][0]
        )
    return out


def day_output_fingerprint(counts: dict[str, int]) -> str:
    return stable_hash(counts)
