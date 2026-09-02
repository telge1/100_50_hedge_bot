"""Controlled, idempotent Phase-2 one-day pilot."""

from __future__ import annotations

import json
import resource
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter, process_time
from typing import Any

from .clickhouse import connect, insert, rows, validate_write_sql
from .contracts import TARGET_DATABASE, sanitize_json, stable_hash
from .liquidation_transform import LIQUIDATION_COLUMNS, transform_liquidations
from .ob200_parser import OB200SegmentReader
from .phase2_contracts import (
    BUILD_ID,
    ORDERING_AMBIGUOUS_BUCKETS,
    PHASE2_CONTRACT_VERSION,
    PILOT_END,
    PILOT_START,
    PRODUCER_LINEAGE_CONTRACT,
    TICK_SIZE,
    pilot_contract,
    producer_lineage_rows,
)
from .phase2_ddl import statements as phase2_ddl
from .phase2_transform import compact_ob_state
from .pilot_runner import SOURCE_FILE_COLUMNS
from .source_file_registry import load_source_file
from .source_readers import read_liquidations, read_public_trades
from .config import OB200_ROOT

BATCH_ID = f"phase2:{PILOT_START:%Y%m%d}:{BUILD_ID[:16]}"

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


def _dml(client: Any, sql: str) -> None:
    validate_write_sql(sql)
    client.command(sql)


def _literal_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _create_schema(client: Any) -> list[str]:
    executed = []
    for sql in phase2_ddl():
        validate_write_sql(sql)
        client.command(sql)
        executed.append(sql)
    return executed


def _ready_exists(client: Any) -> bool:
    found = rows(
        client,
        f"""SELECT count() FROM {TARGET_DATABASE}.research_batch_runs
        WHERE batch_id=%(batch)s AND build_id=%(build)s AND status='READY'""",
        {"batch": BATCH_ID, "build": BUILD_ID},
    )
    return bool(found and int(found[0][0]))


def _batch_attempts(client: Any) -> int:
    return int(
        rows(
            client,
            f"SELECT count() FROM {TARGET_DATABASE}.research_batch_runs "
            "WHERE batch_id=%(batch)s AND build_id=%(build)s",
            {"batch": BATCH_ID, "build": BUILD_ID},
        )[0][0]
    )


def _metadata_exists(client: Any) -> bool:
    return bool(
        rows(
            client,
            f"SELECT count() FROM {TARGET_DATABASE}.research_schema_versions "
            "WHERE contract_version=%(contract)s AND build_id=%(build)s",
            {"contract": PHASE2_CONTRACT_VERSION, "build": BUILD_ID},
        )[0][0]
    )


def _count(client: Any, table: str, condition: str) -> int:
    return int(
        rows(
            client,
            f"SELECT count() FROM {TARGET_DATABASE}.{table} WHERE {condition}",
        )[0][0]
    )


def _batch_row(
    status: str,
    phase: str,
    started: datetime,
    *,
    completed: datetime | None = None,
    output_fingerprint: str = "0" * 64,
    rows_written: int = 0,
    error: str = "",
) -> tuple[Any, ...]:
    return (
        BATCH_ID, BUILD_ID, PHASE2_CONTRACT_VERSION, PILOT_START, PILOT_END,
        status, phase, stable_hash(pilot_contract()), output_fingerprint,
        rows_written, started, completed, error,
    )


def _insert_metadata(client: Any, now: datetime) -> int:
    insert(
        client,
        "research_schema_versions",
        [(PHASE2_CONTRACT_VERSION, stable_hash(phase2_ddl()), BUILD_ID, now, "ACTIVE")],
        ("contract_version", "schema_fingerprint", "build_id", "applied_at", "status"),
    )
    lineage = []
    for item in producer_lineage_rows():
        lineage.append(
            (
                item["symbol"], item["producer_id"], item["producer_type"],
                item["source_path_or_table"], item["source_semantics"],
                int(item["event_time_available"]), int(item["receive_time_available"]),
                item["reconstruction_clock"],
                datetime.fromisoformat(item["coverage_start"].replace("Z", "+00:00")),
                datetime.fromisoformat(item["coverage_end"].replace("Z", "+00:00")),
                item["terminal_reason"], int(item["coverage_complete"]),
                item["transition_contract"], item["source_fingerprint"],
                item["contract_version"], item["build_id"], now,
            )
        )
    insert(
        client,
        "research_producer_lineage",
        lineage,
        (
            "symbol", "producer_id", "producer_type", "source_path_or_table",
            "source_semantics", "event_time_available", "receive_time_available",
            "reconstruction_clock", "coverage_start", "coverage_end",
            "terminal_reason", "coverage_complete", "transition_contract",
            "source_fingerprint", "contract_version", "build_id", "recorded_at",
        ),
    )
    dq = []
    for symbol, raw_time in ORDERING_AMBIGUOUS_BUCKETS:
        event_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        dq.append(
            (
                stable_hash({"symbol": symbol, "time": raw_time, "type": "ORDERING_AMBIGUOUS"}),
                symbol, event_time, "ORDERING_AMBIGUOUS", "INFO",
                "event-loop callback versus wall-timer order was not persisted",
                "PHASE1B_SEAM_AUDIT", PHASE2_CONTRACT_VERSION, BUILD_ID, now,
            )
        )
    insert(
        client,
        "research_data_quality_events",
        dq,
        (
            "event_key", "symbol", "event_time", "quality_type", "severity",
            "details", "source_id", "contract_version", "build_id", "recorded_at",
        ),
    )
    return 1 + len(lineage) + len(dq)


def _insert_trade_buckets(client: Any, milliseconds: int, now: datetime) -> None:
    suffix = {100: "100ms", 500: "500ms", 1000: "1s"}[milliseconds]
    start, end = _literal_time(PILOT_START), _literal_time(PILOT_END)
    source = (
        "logical"
        if milliseconds == 100
        else f"{TARGET_DATABASE}.research_public_trade_buckets_100ms"
    )
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
          WHERE src.symbol IN ('BTCUSDT','DOGEUSDT')
            AND trade_ts>=toDateTime64('{start}',3,'UTC')
            AND trade_ts<toDateTime64('{end}',3,'UTC')
          GROUP BY trade_id
        )"""
        bucket_expr = "toStartOfInterval(event_time, INTERVAL 100 MILLISECOND)"
        size, notional, side = "size", "notional", "side"
        first_ts, last_ts = "event_time", "event_time"
        count_expr = "count()"
    else:
        source_sql = source
        bucket_expr = f"toStartOfInterval(bucket_start, INTERVAL {milliseconds} MILLISECOND)"
        size, notional, side = (
            "buy_base_volume", "buy_quote_notional", "'Buy'"
        )
        first_ts, last_ts = "first_trade_ts", "last_trade_ts"
        count_expr = "sum(source_trade_count)"
    if milliseconds == 100:
        buy_base = f"sumIf({size},{side}='Buy')"
        sell_base = f"sumIf({size},{side}='Sell')"
        buy_quote = f"sumIf({notional},{side}='Buy')"
        sell_quote = f"sumIf({notional},{side}='Sell')"
        buy_count = f"countIf({side}='Buy')"
        sell_count = f"countIf({side}='Sell')"
    else:
        buy_base, sell_base = "sum(buy_base_volume)", "sum(sell_base_volume)"
        buy_quote, sell_quote = "sum(buy_quote_notional)", "sum(sell_quote_notional)"
        buy_count, sell_count = "sum(buy_trade_count)", "sum(sell_trade_count)"
    sql = f"""
    INSERT INTO {TARGET_DATABASE}.research_public_trade_buckets_{suffix}
    ({','.join(TRADE_BUCKET_COLUMNS)})
    SELECT {"logical_symbol AS symbol" if milliseconds == 100 else "symbol"},{bucket_expr},{buy_base},{sell_base},{buy_quote},{sell_quote},
           {buy_quote}-{sell_quote},{buy_count},{sell_count},
           min({first_ts}),max({last_ts}),{count_expr},{count_expr},
           '{PHASE2_CONTRACT_VERSION}','public_trade_taker_aggressor_v1',
           '{BUILD_ID}','COMPLETE',
           toDateTime64('{now.isoformat()}',6,'UTC')
    FROM {source_sql}
    {"WHERE build_id='"+BUILD_ID+"'" if milliseconds != 100 else ""}
    GROUP BY {"logical_symbol" if milliseconds == 100 else "symbol"},{bucket_expr}
    """
    _dml(client, sql)


def _insert_liquidations(client: Any, now: datetime) -> int:
    total = 0
    for symbol in ("BTCUSDT", "DOGEUSDT"):
        source, _ = read_liquidations(client, symbol, PILOT_START, PILOT_END)
        transformed = transform_liquidations(
            source, symbol=symbol, batch_id=BATCH_ID, ingested_at=now
        )
        insert(client, "research_liquidation_events", transformed, LIQUIDATION_COLUMNS)
        total += len(transformed)
    sql = f"""
    INSERT INTO {TARGET_DATABASE}.research_liquidation_buckets_500ms
    SELECT symbol,toStartOfInterval(event_time,INTERVAL 500 MILLISECOND),
           countIf(liquidated_position_side='LIQUIDATED_LONG'),
           countIf(liquidated_position_side='LIQUIDATED_SHORT'),
           sumIf(executed_base_size,forced_flow='FORCED_BUY'),
           sumIf(executed_base_size,forced_flow='FORCED_SELL'),
           sum(bankruptcy_reference_quote),count(),
           '{PHASE2_CONTRACT_VERSION}','liquidation_flow_facts_v1','{BUILD_ID}',
           'COMPLETE',toDateTime64('{now.isoformat()}',6,'UTC')
    FROM {TARGET_DATABASE}.research_liquidation_events
    WHERE ingestion_batch_id='{BATCH_ID}'
    GROUP BY symbol,toStartOfInterval(event_time,INTERVAL 500 MILLISECOND)
    """
    _dml(client, sql)
    return total


def _insert_oi(client: Any, now: datetime) -> None:
    sql = f"""
    INSERT INTO {TARGET_DATABASE}.research_open_interest_observations
    SELECT symbol,bucket_time,argMax(open_interest,inserted_at),
           argMax(open_interest_value,inserted_at),
           argMax(source_event_time,inserted_at),argMax(state_age_ms,inserted_at),
           argMax(state_valid,inserted_at),5000,'BYBIT_BASE_AND_QUOTE',
           '{PHASE2_CONTRACT_VERSION}','open_interest_5s_v1','{BUILD_ID}',
           'COMPLETE',toDateTime64('{now.isoformat()}',6,'UTC')
    FROM orderbook_analysis.open_interest_5s
    WHERE symbol IN ('BTCUSDT','DOGEUSDT')
      AND bucket_time>=toDateTime64('{_literal_time(PILOT_START)}',3,'UTC')
      AND bucket_time<toDateTime64('{_literal_time(PILOT_END)}',3,'UTC')
    GROUP BY symbol,bucket_time
    """
    _dml(client, sql)


def _profile_rows(client: Any, now: datetime) -> tuple[dict[str, Any], int]:
    orderbook_analyse_src = (
        "/home/telgenbuescher/projects/orderbook_analyse/src"
    )
    if orderbook_analyse_src not in sys.path:
        sys.path.insert(0, orderbook_analyse_src)
    from research.btc_ob_fight.tpo_profile import build_tpo_profile_from_trades
    from research.btc_ob_fight.volume_profile import (
        build_volume_profile_from_trades,
        profile_session_window,
    )

    session_start, anchor, session_id = profile_session_window(PILOT_END)
    summary: dict[str, Any] = {}
    written = 0
    for symbol in ("BTCUSDT", "DOGEUSDT"):
        source, stats = read_public_trades(client, symbol, session_start, anchor)
        trades = [
            {
                "trade_id": str(row[0]),
                "ts": (
                    row[1].replace(tzinfo=timezone.utc)
                    if row[1].tzinfo is None else row[1].astimezone(timezone.utc)
                ),
                "price": float(row[3]),
                "size": float(row[4]), "notional": float(row[5]), "side": str(row[6]),
            }
            for row in source
        ]
        ohlc_row = rows(
            client,
            """SELECT argMin(open,open_time),max(high),min(low),argMax(close,open_time)
            FROM signal_generator.candles_1m FINAL
            WHERE symbol=%(symbol)s AND interval='1m'
              AND open_time>=%(start)s AND open_time<%(end)s""",
            {"symbol": symbol, "start": session_start, "end": anchor},
        )[0]
        ohlc = tuple(float(value) for value in ohlc_row)
        volume = build_volume_profile_from_trades(
            trades, session_start=session_start, anchor=anchor, cl=client,
            symbol=symbol, compute_prefix=False, ohlc=ohlc,
        )
        tpo = build_tpo_profile_from_trades(
            trades, session_start=session_start, anchor=anchor, cl=client,
            symbol=symbol, ohlc=ohlc,
        )
        if volume["integrity"]["status"] != "PASS" or tpo["integrity"]["status"] != "PASS":
            raise RuntimeError(f"profile integrity failed: {symbol}")
        common = (
            PHASE2_CONTRACT_VERSION, "public_trade_taker_aggressor_v1",
            BUILD_ID, "COMPLETE", now,
        )
        bracket_rows = [
            (
                symbol, session_id, row["bracket_index"],
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
                symbol, session_id, row["price_bin_index"],
                int(Decimal(str(row["price"])) / TICK_SIZE[symbol]),
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
                symbol, session_id, row["price_bin_index"],
                int(Decimal(str(row["display_price"])) / TICK_SIZE[symbol]),
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
            [(symbol, session_id, kind, level, Decimal(str(price)), share, *common) for kind, level, price, share in levels],
            (
                "symbol","session_id","profile_kind","level_kind","price",
                "value_area_share","contract_version","source_semantics_version",
                "build_id","coverage_status","computed_at",
            ),
        )
        written += len(bracket_rows) + len(tpo_rows) + len(volume_rows) + len(levels)
        summary[symbol] = {
            "source_trade_stats": stats,
            "session_start": session_start,
            "session_end": anchor,
            "tpo_integrity": tpo["integrity"],
            "volume_integrity": volume["integrity"],
            "tpo": tpo["tpoc"] | tpo["value_area"],
            "volume": volume["vpoc"] | volume["value_area"],
        }
    return summary, written


def _ob_source(symbol: str, hour: datetime):
    end = hour + timedelta(hours=1)
    name = f"{symbol}_{hour:%Y%m%dT%H%M%SZ}_{end:%Y%m%dT%H%M%SZ}_ob200_v3.zst"
    return load_source_file(
        OB200_ROOT / symbol / f"{hour.year:04d}" / f"{hour.month:02d}"
        / f"{hour.day:02d}" / name,
        OB200_ROOT,
    )


def _insert_ob(client: Any, now: datetime) -> tuple[int, dict[str, Any]]:
    total = 0
    summary = {}
    for symbol in ("BTCUSDT", "DOGEUSDT"):
        symbol_count = 0
        for offset in range(24):
            hour = PILOT_START + timedelta(hours=offset)
            end = hour + timedelta(hours=1)
            source = _ob_source(symbol, hour)
            reader = OB200SegmentReader(source, symbol)
            by_second: dict[datetime, tuple[Any, int]] = {}
            counts: dict[datetime, int] = {}
            for event in reader.iter_full_books(hour, end):
                second = event.event_time.replace(microsecond=0)
                by_second[second] = (event, counts.get(second, 0) + 1)
                counts[second] = counts.get(second, 0) + 1
            if reader.audit.u_gaps or not reader.audit.full_file_consumed:
                raise RuntimeError(f"OB source invalid: {source.relative_path}")
            if len(by_second) != 3600:
                raise RuntimeError(f"OB second gap: {symbol} {hour} {len(by_second)}")
            batch = []
            for second in sorted(by_second):
                event = by_second[second][0]
                state = compact_ob_state(symbol, event.bids, event.asks)
                batch.append(
                    (
                        symbol, second, "BYBIT_OB200_LIVE_COLLECTOR_V3",
                        state["bid_price_ticks"], state["bid_quantities"],
                        state["ask_price_ticks"], state["ask_quantities"],
                        state["best_bid"], state["best_ask"], state["mid"],
                        state["spread"], state["bid_level_count"],
                        state["ask_level_count"], state["genuine_depth"],
                        "EVENT_TIME_END_OF_SECOND", counts[second],
                        event.event_time, event.update_id, "EXACT_EVENT_ORDER",
                        source.fingerprint, PHASE2_CONTRACT_VERSION,
                        "raw_ob200_event_time_eos_v1", BUILD_ID, "COMPLETE", now,
                    )
                )
            insert(client, "research_ob200_snapshots_1s", batch, OB_COLUMNS)
            symbol_count += len(batch)
        summary[symbol] = {"seconds": symbol_count, "expected": 86400}
        total += symbol_count
    return total, summary


def _counts(client: Any) -> dict[str, int]:
    tables = [
        "research_public_trade_buckets_100ms",
        "research_public_trade_buckets_500ms",
        "research_public_trade_buckets_1s",
        "research_liquidation_events",
        "research_liquidation_buckets_500ms",
        "research_open_interest_observations",
        "research_tpo_bracket_ranges_30m",
        "research_tpo_profile_bins_session",
        "research_volume_profile_bins_session",
        "research_profile_levels_session",
        "research_ob200_snapshots_1s",
    ]
    out = {}
    for table in tables:
        condition = (
            f"ingestion_batch_id='{BATCH_ID}'"
            if table == "research_liquidation_events"
            else f"build_id='{BUILD_ID}'"
        )
        out[table] = int(
            rows(client, f"SELECT count() FROM {TARGET_DATABASE}.{table} WHERE {condition}")[0][0]
        )
    return out


def run() -> dict[str, Any]:
    wall_start, cpu_start = perf_counter(), process_time()
    started = datetime.now(timezone.utc)
    client = connect()
    timings: dict[str, float] = {}
    try:
        t = perf_counter()
        ddl = _create_schema(client)
        timings["ddl_seconds"] = perf_counter() - t
        if _ready_exists(client):
            return {"status": "IDEMPOTENT_SKIP", "batch_id": BATCH_ID, "counts": _counts(client)}
        attempts = _batch_attempts(client)
        recovering = attempts > 0
        insert(
            client, "research_batch_runs",
            [_batch_row(
                f"RECOVERING_{attempts}" if recovering else "RUNNING",
                "RESUME" if recovering else "START",
                started,
            )],
            (
                "batch_id","build_id","contract_version","pilot_start","pilot_end",
                "status","phase","input_fingerprint","output_fingerprint",
                "rows_written","started_at","completed_at","error",
            ),
        )
        t = perf_counter()
        metadata = 0 if _metadata_exists(client) else _insert_metadata(client, started)
        timings["metadata_seconds"] = perf_counter() - t
        t = perf_counter()
        for ms in (100, 500, 1000):
            suffix = {100: "100ms", 500: "500ms", 1000: "1s"}[ms]
            if not _count(
                client,
                f"research_public_trade_buckets_{suffix}",
                f"build_id='{BUILD_ID}'",
            ):
                _insert_trade_buckets(client, ms, started)
        timings["trade_bucket_seconds"] = perf_counter() - t
        t = perf_counter()
        if not _count(
            client, "research_liquidation_events",
            f"ingestion_batch_id='{BATCH_ID}'",
        ):
            liquidation_count = _insert_liquidations(client, started)
        else:
            liquidation_count = _count(
                client, "research_liquidation_events",
                f"ingestion_batch_id='{BATCH_ID}'",
            )
        timings["liquidation_seconds"] = perf_counter() - t
        t = perf_counter()
        if not _count(
            client, "research_open_interest_observations",
            f"build_id='{BUILD_ID}'",
        ):
            _insert_oi(client, started)
        timings["oi_seconds"] = perf_counter() - t
        t = perf_counter()
        existing_profile_rows = _count(
            client, "research_profile_levels_session", f"build_id='{BUILD_ID}'"
        )
        if existing_profile_rows:
            profiles, profile_rows = {"recovered": "already present"}, existing_profile_rows
        else:
            profiles, profile_rows = _profile_rows(client, started)
        timings["profile_seconds"] = perf_counter() - t
        t = perf_counter()
        existing_ob = _count(
            client, "research_ob200_snapshots_1s", f"build_id='{BUILD_ID}'"
        )
        if existing_ob:
            if existing_ob != 172800:
                raise RuntimeError(f"partial OB stage cannot resume safely: {existing_ob}")
            ob_count, ob_summary = existing_ob, {"recovered": "already present"}
        else:
            ob_count, ob_summary = _insert_ob(client, started)
        timings["ob_seconds"] = perf_counter() - t
        counts = _counts(client)
        if (
            ob_count != 172800
            or counts["research_open_interest_observations"] != 34560
        ):
            raise RuntimeError(f"integrity count gate failed: {counts}")
        output_fingerprint = stable_hash(counts)
        completed = datetime.now(timezone.utc)
        insert(
            client, "research_batch_runs",
            [_batch_row(
                "READY", "COMPLETE", started, completed=completed,
                output_fingerprint=output_fingerprint,
                rows_written=sum(counts.values()),
            )],
            (
                "batch_id","build_id","contract_version","pilot_start","pilot_end",
                "status","phase","input_fingerprint","output_fingerprint",
                "rows_written","started_at","completed_at","error",
            ),
        )
        return sanitize_json(
            {
                "status": "READY", "batch_id": BATCH_ID, "build_id": BUILD_ID,
                "database": TARGET_DATABASE, "ddl_count": len(ddl),
                "counts": counts, "metadata_rows": metadata,
                "liquidation_events": liquidation_count,
                "profile_rows": profile_rows, "profiles": profiles,
                "ob_summary": ob_summary, "output_fingerprint": output_fingerprint,
                "timings": timings,
                "wall_seconds": perf_counter() - wall_start,
                "cpu_seconds": process_time() - cpu_start,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }
        )
    except Exception as exc:
        failed = datetime.now(timezone.utc)
        try:
            insert(
                client, "research_batch_runs",
                [_batch_row("FAILED", "ERROR", started, completed=failed, error=str(exc)[:1000])],
                (
                    "batch_id","build_id","contract_version","pilot_start","pilot_end",
                    "status","phase","input_fingerprint","output_fingerprint",
                    "rows_written","started_at","completed_at","error",
                ),
            )
        finally:
            raise
    finally:
        client.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
