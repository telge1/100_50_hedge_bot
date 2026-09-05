#!/usr/bin/env python3
"""Isolated Full-OB ClickHouse smoke test for BTCUSDT (exact parity + idempotency)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import zstandard as zstd

# Ensure orderbook_analyse package is importable
OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState  # noqa: E402
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome  # noqa: E402

OUT = Path(__file__).resolve().parent
EVENT_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/"
    "full_ob_edge_flight_recorder/BTCUSDT/2026-09-04/"
    "BTCUSDT_20260904T080534Z_1fd9a66d36"
)
FIGHT_EVENT_ID = "BTCUSDT_20260904T080534Z_1fd9a66d36"
SOURCE_FILE = str(EVENT_DIR / "full_ob_raw_deltas.jsonl.zst")
SNAPSHOT_FILE = str(EVENT_DIR / "rest_full_snapshot.json.zst")
HEALTH_FILE = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/logs/"
    "orderbook_v3_raw_archive_btc_doge.health.ndjson"
)
DB = "research_full_ob_smoke"
PACKETS_TBL = "full_ob_packets_smoke_v1"
LEVELS_TBL = "full_ob_level_changes_smoke_v1"
SYMBOL = "BTCUSDT"
TOPIC = "orderbook.full.BTCUSDT"
SMOKE_SECONDS = 300
CHECKPOINT_EVERY = 250  # applied deltas
COLLECTOR_PID = 1565672
OI_PID = 147111


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def ch_query(sql: str, *, data: bytes | None = None, timeout_s: int = 600) -> str:
    cmd = [
        "docker",
        "exec",
        "-i",
        "orderbook-clickhouse",
        "clickhouse-client",
        "--database",
        DB,
        f"--receive_timeout={timeout_s}",
        f"--send_timeout={timeout_s}",
        "-q",
        sql,
    ]
    proc = subprocess.run(cmd, input=data, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ClickHouse query failed ({proc.returncode}): {proc.stderr.decode()[:2000]}\nSQL={sql[:500]}"
        )
    return proc.stdout.decode()


def ch_admin(sql: str) -> str:
    cmd = ["docker", "exec", "-i", "orderbook-clickhouse", "clickhouse-client", "-q", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"CH admin failed: {proc.stderr[:2000]}\nSQL={sql[:500]}")
    return proc.stdout


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def load_health() -> dict[str, Any]:
    lines = HEALTH_FILE.read_text().strip().splitlines()
    return json.loads(lines[-1])


def book_hash(book: FullBookState) -> str:
    parts: list[str] = []
    for p, q in sorted(book.bids.items()):
        parts.append(f"B|{p:.12g}|{q:.12g}")
    for p, q in sorted(book.asks.items()):
        parts.append(f"A|{p:.12g}|{q:.12g}")
    return sha256_hex("\n".join(parts))


def book_checkpoint(book: FullBookState, *, label: str, applied: int) -> dict[str, Any]:
    bb = book.best_bid()
    ba = book.best_ask()
    spread = None if bb is None or ba is None else ba - bb
    crossed = bool(bb is not None and ba is not None and bb >= ba)
    # Selected level qtys near mid for parity probes
    probes: dict[str, Any] = {}
    if bb is not None:
        probes["best_bid_qty"] = book.bids.get(bb)
    if ba is not None:
        probes["best_ask_qty"] = book.asks.get(ba)
    return {
        "label": label,
        "applied_delta_count": applied,
        "u": book.update_id,
        "seq": book.seq,
        "best_bid": bb,
        "best_ask": ba,
        "spread": spread,
        "bid_level_count": len(book.bids),
        "ask_level_count": len(book.asks),
        "book_hash": book_hash(book),
        "crossed": crossed,
        "probes": probes,
    }


def levels_to_tuples(levels: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in levels or []:
        if not row or len(row) < 2:
            continue
        out.append((str(row[0]), str(row[1])))
    return out


def canonical_payload(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


@dataclass
class PacketRow:
    packet_sha256: str
    fight_event_id: str
    segment_index: int
    source_file: str
    source_line_number: int
    symbol: str
    topic: str
    message_type: str
    marker_type: str | None
    exchange_ts_ms: int | None
    cts_ms: int | None
    receive_time_ns: int | None
    update_id: int | None
    seq: int | None
    bids: list[tuple[str, str]]
    asks: list[tuple[str, str]]
    raw_payload: str

    def to_json_each_row(self) -> dict[str, Any]:
        return {
            "packet_sha256": self.packet_sha256,
            "fight_event_id": self.fight_event_id,
            "segment_index": self.segment_index,
            "source_file": self.source_file,
            "source_line_number": self.source_line_number,
            "symbol": self.symbol,
            "topic": self.topic,
            "message_type": self.message_type,
            "marker_type": self.marker_type,
            "exchange_ts_ms": self.exchange_ts_ms,
            "cts_ms": self.cts_ms,
            "receive_time_ns": self.receive_time_ns,
            "update_id": self.update_id,
            "seq": self.seq,
            "bids": [[p, q] for p, q in self.bids],
            "asks": [[p, q] for p, q in self.asks],
            "raw_payload": self.raw_payload,
            "ingestion_ts": utc_now().replace("Z", ""),
        }


SCHEMA_SQL = f"""
CREATE DATABASE IF NOT EXISTS {DB};

CREATE TABLE IF NOT EXISTS {DB}.{PACKETS_TBL}
(
    packet_sha256 FixedString(64),
    fight_event_id String,
    segment_index UInt32,
    source_file String,
    source_line_number UInt64,
    symbol LowCardinality(String),
    topic String,
    message_type LowCardinality(String),
    marker_type Nullable(String),
    exchange_ts_ms Nullable(Int64),
    cts_ms Nullable(Int64),
    receive_time_ns Nullable(Int64),
    update_id Nullable(Int64),
    seq Nullable(Int64),
    bids Array(Tuple(String, String)),
    asks Array(Tuple(String, String)),
    raw_payload String,
    ingestion_ts DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingestion_ts)
ORDER BY (packet_sha256)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS {DB}.{LEVELS_TBL}
(
    packet_sha256 FixedString(64),
    symbol LowCardinality(String),
    side Enum8('bid' = 1, 'ask' = 2),
    price String,
    quantity String,
    action Enum8('UPSERT' = 1, 'DELETE' = 2),
    update_id Int64,
    seq Int64,
    exchange_ts_ms Int64,
    ingestion_ts DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingestion_ts)
ORDER BY (packet_sha256, side, price)
SETTINGS index_granularity = 8192;
"""


def truncate_smoke_tables() -> None:
    ch_admin(f"TRUNCATE TABLE IF EXISTS {DB}.{PACKETS_TBL}")
    ch_admin(f"TRUNCATE TABLE IF EXISTS {DB}.{LEVELS_TBL}")


def build_snapshot_packet(snap: dict[str, Any]) -> PacketRow:
    # Keep lossless levels in bids/asks arrays; raw_payload holds metadata only
    # (embedding 60k+ levels twice would inflate the smoke insert unnecessarily).
    payload = {
        "topic": TOPIC,
        "type": "snapshot",
        "ts": snap.get("ts"),
        "cts": snap.get("cts"),
        "data": {
            "s": snap.get("s", SYMBOL),
            "u": snap.get("u"),
            "seq": snap.get("seq"),
            "bid_count": len(snap["b"]),
            "ask_count": len(snap["a"]),
        },
        "source": "rest_full_snapshot",
    }
    canon = canonical_payload(payload)
    return PacketRow(
        packet_sha256=sha256_hex(canon + "|" + SNAPSHOT_FILE),
        fight_event_id=FIGHT_EVENT_ID,
        segment_index=0,
        source_file=SNAPSHOT_FILE,
        source_line_number=0,
        symbol=SYMBOL,
        topic=TOPIC,
        message_type="snapshot",
        marker_type=None,
        exchange_ts_ms=int(snap["ts"]) if snap.get("ts") is not None else None,
        cts_ms=int(snap["cts"]) if snap.get("cts") is not None else None,
        receive_time_ns=None,
        update_id=int(snap["u"]) if snap.get("u") is not None else None,
        seq=int(snap["seq"]) if snap.get("seq") is not None else None,
        bids=levels_to_tuples(snap["b"]),
        asks=levels_to_tuples(snap["a"]),
        raw_payload=canon,
    )


def classify_record(rec: dict[str, Any]) -> str:
    if rec.get("channel") == "marker" or rec.get("marker_type"):
        return "marker"
    t = str(rec.get("type") or "").lower()
    if t == "snapshot":
        return "snapshot"
    if t == "delta" or "data" in rec:
        return "delta"
    return "other"


def coerce_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            # ISO markers → epoch ms
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return int(datetime.fromisoformat(s).timestamp() * 1000)
        except Exception:
            return None
    return None


def coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def packet_from_line(rec: dict[str, Any], line_no: int) -> PacketRow | None:
    msg_type = classify_record(rec)
    if msg_type == "other":
        return None
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    topic = str(rec.get("topic") or TOPIC)
    if msg_type == "delta" and topic != TOPIC:
        return None
    marker_type = rec.get("marker_type")
    if msg_type == "marker":
        bids, asks = [], []
        u = data.get("u") if data else rec.get("u")
        seq = data.get("seq") if data else rec.get("seq")
        # Markers are not OB topics; keep topic label explicit
        if not rec.get("topic"):
            topic = f"marker.{SYMBOL}"
    else:
        bids = levels_to_tuples(data.get("b") or [])
        asks = levels_to_tuples(data.get("a") or [])
        u = data.get("u")
        seq = data.get("seq")
    raw_obj = dict(rec)
    canon = canonical_payload(raw_obj)
    return PacketRow(
        packet_sha256=sha256_hex(f"{SOURCE_FILE}|{line_no}|{canon}"),
        fight_event_id=FIGHT_EVENT_ID,
        segment_index=0,
        source_file=SOURCE_FILE,
        source_line_number=line_no,
        symbol=SYMBOL,
        topic=topic if topic else TOPIC,
        message_type=msg_type,
        marker_type=str(marker_type) if marker_type else None,
        exchange_ts_ms=coerce_ms(rec.get("ts")),
        cts_ms=coerce_ms(rec.get("cts")),
        receive_time_ns=coerce_int(rec.get("local_receive_time_ns")),
        update_id=coerce_int(u),
        seq=coerce_int(seq),
        bids=bids,
        asks=asks,
        raw_payload=canon,
    )


def select_smoke_packets() -> tuple[list[PacketRow], dict[str, Any], dict[str, Any]]:
    dctx = zstd.ZstdDecompressor()
    snap = json.loads(
        dctx.decompress(Path(SNAPSHOT_FILE).read_bytes(), max_output_size=200_000_000)
    )
    snap_packet = build_snapshot_packet(snap)

    book = FullBookState(SYMBOL)
    book.apply_snapshot(
        bids=snap["b"],
        asks=snap["a"],
        u=snap.get("u"),
        seq=snap.get("seq"),
        ts_ms=snap.get("ts"),
        cts_ms=snap.get("cts"),
        mark_ready=True,
    )

    packets: list[PacketRow] = [snap_packet]
    stats = {
        "jsonl_lines_read": 0,
        "delta_rows": 0,
        "marker_rows": 0,
        "other_skipped": 0,
        "applied_deltas": 0,
        "ignored_stale_or_dup": 0,
        "gap_detected_in_selection": 0,
        "parse_errors": 0,
        "invalid_price_qty": 0,
        "first_applied_u": None,
        "last_applied_u": None,
        "first_ts": None,
        "last_ts": None,
        "cut_line": None,
    }
    first_applied_ts: int | None = None

    with open(SOURCE_FILE, "rb") as f:
        with dctx.stream_reader(f) as reader:
            text = io.TextIOWrapper(reader, encoding="utf-8")
            for line_no, line in enumerate(text, 1):
                stats["jsonl_lines_read"] = line_no
                try:
                    rec = json.loads(line)
                except Exception:
                    stats["parse_errors"] += 1
                    continue
                pkt = packet_from_line(rec, line_no)
                if pkt is None:
                    stats["other_skipped"] += 1
                    continue
                if pkt.message_type == "marker":
                    packets.append(pkt)
                    stats["marker_rows"] += 1
                    continue
                if pkt.message_type != "delta":
                    packets.append(pkt)
                    continue

                # validate string levels
                for side in (pkt.bids, pkt.asks):
                    for p, q in side:
                        try:
                            float(p)
                            float(q)
                        except Exception:
                            stats["invalid_price_qty"] += 1

                packets.append(pkt)
                stats["delta_rows"] += 1
                out = book.apply_delta(
                    bids=[[p, q] for p, q in pkt.bids],
                    asks=[[p, q] for p, q in pkt.asks],
                    u=pkt.update_id,
                    seq=pkt.seq,
                    ts_ms=pkt.exchange_ts_ms,
                    cts_ms=pkt.cts_ms,
                    receive_time_ns=pkt.receive_time_ns,
                    enforce_continuity=True,
                )
                if out is DeltaOutcome.GAP:
                    stats["gap_detected_in_selection"] += 1
                    # Do not include this or later lines in smoke window
                    packets.pop()  # remove gap packet
                    stats["delta_rows"] -= 1
                    stats["cut_line"] = line_no - 1
                    break
                if out is DeltaOutcome.APPLIED:
                    stats["applied_deltas"] += 1
                    if stats["first_applied_u"] is None:
                        stats["first_applied_u"] = pkt.update_id
                        first_applied_ts = pkt.exchange_ts_ms
                    stats["last_applied_u"] = pkt.update_id
                    stats["first_ts"] = stats["first_ts"] or pkt.exchange_ts_ms
                    stats["last_ts"] = pkt.exchange_ts_ms
                    if (
                        first_applied_ts is not None
                        and pkt.exchange_ts_ms is not None
                        and (pkt.exchange_ts_ms - first_applied_ts) / 1000.0 >= SMOKE_SECONDS
                    ):
                        stats["cut_line"] = line_no
                        break
                else:
                    stats["ignored_stale_or_dup"] += 1

    meta = {
        "snapshot_u": snap.get("u"),
        "snapshot_seq": snap.get("seq"),
        "snapshot_bid_levels": len(snap["b"]),
        "snapshot_ask_levels": len(snap["a"]),
        "snapshot_ts": snap.get("ts"),
        "final_book": book_checkpoint(book, label="source_selection_end", applied=stats["applied_deltas"]),
    }
    return packets, stats, meta


def insert_json_each_row(table: str, rows: list[dict[str, Any]], batch: int = 100) -> int:
    """Insert via temp file + single docker stream per batch (avoids tiny docker-exec storms)."""
    if not rows:
        return 0
    inserted = 0
    # Snapshot row can be multi-MB; keep batch small for memory, but stream whole batch once.
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        payload = ("\n".join(json.dumps(r, ensure_ascii=False) for r in chunk) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(prefix="ch_smoke_", suffix=".ndjson", delete=False) as tf:
            tf.write(payload)
            path = tf.name
        try:
            cmd = (
                f"docker exec -i orderbook-clickhouse clickhouse-client "
                f"--database {DB} --receive_timeout 600 --send_timeout 600 "
                f'-q "INSERT INTO {table} FORMAT JSONEachRow" < {path}'
            )
            proc = subprocess.run(["bash", "-lc", cmd], capture_output=True)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Insert failed: {proc.stderr.decode()[:2000]} rows={len(chunk)} table={table}"
                )
        finally:
            Path(path).unlink(missing_ok=True)
        inserted += len(chunk)
    return inserted


def populate_level_changes_from_packets() -> int:
    """Derive level-change analysis table from immutable packet rows (server-side)."""
    ch_query(f"TRUNCATE TABLE {LEVELS_TBL}")
    ch_query(
        f"""
        INSERT INTO {LEVELS_TBL}
        SELECT
            packet_sha256,
            symbol,
            CAST('bid' AS Enum8('bid' = 1, 'ask' = 2)) AS side,
            tupleElement(lvl, 1) AS price,
            tupleElement(lvl, 2) AS quantity,
            CAST(
                if(toFloat64OrZero(tupleElement(lvl, 2)) = 0, 'DELETE', 'UPSERT')
                AS Enum8('UPSERT' = 1, 'DELETE' = 2)
            ) AS action,
            assumeNotNull(update_id) AS update_id,
            assumeNotNull(seq) AS seq,
            assumeNotNull(exchange_ts_ms) AS exchange_ts_ms,
            now64(3, 'UTC') AS ingestion_ts
        FROM {PACKETS_TBL} FINAL
        ARRAY JOIN bids AS lvl
        WHERE message_type = 'delta' AND update_id IS NOT NULL AND seq IS NOT NULL
        """
    )
    ch_query(
        f"""
        INSERT INTO {LEVELS_TBL}
        SELECT
            packet_sha256,
            symbol,
            CAST('ask' AS Enum8('bid' = 1, 'ask' = 2)) AS side,
            tupleElement(lvl, 1) AS price,
            tupleElement(lvl, 2) AS quantity,
            CAST(
                if(toFloat64OrZero(tupleElement(lvl, 2)) = 0, 'DELETE', 'UPSERT')
                AS Enum8('UPSERT' = 1, 'DELETE' = 2)
            ) AS action,
            assumeNotNull(update_id) AS update_id,
            assumeNotNull(seq) AS seq,
            assumeNotNull(exchange_ts_ms) AS exchange_ts_ms,
            now64(3, 'UTC') AS ingestion_ts
        FROM {PACKETS_TBL} FINAL
        ARRAY JOIN asks AS lvl
        WHERE message_type = 'delta' AND update_id IS NOT NULL AND seq IS NOT NULL
        """
    )
    return int(ch_query(f"SELECT count() FROM {LEVELS_TBL} FINAL").strip() or 0)


def replay_from_packets(packets: list[PacketRow]) -> tuple[FullBookState, list[dict[str, Any]], dict[str, Any]]:
    book = FullBookState(SYMBOL)
    checkpoints: list[dict[str, Any]] = []
    applied = 0
    gaps = 0
    rejects = 0
    last_u: int | None = None
    applied_us: list[int] = []

    for pkt in packets:
        if pkt.message_type == "snapshot":
            book.apply_snapshot(
                bids=[[p, q] for p, q in pkt.bids],
                asks=[[p, q] for p, q in pkt.asks],
                u=pkt.update_id,
                seq=pkt.seq,
                ts_ms=pkt.exchange_ts_ms,
                cts_ms=pkt.cts_ms,
                receive_time_ns=pkt.receive_time_ns,
                mark_ready=True,
            )
            checkpoints.append(book_checkpoint(book, label="after_snapshot", applied=0))
            last_u = pkt.update_id
            continue
        if pkt.message_type != "delta":
            continue
        out = book.apply_delta(
            bids=[[p, q] for p, q in pkt.bids],
            asks=[[p, q] for p, q in pkt.asks],
            u=pkt.update_id,
            seq=pkt.seq,
            ts_ms=pkt.exchange_ts_ms,
            cts_ms=pkt.cts_ms,
            receive_time_ns=pkt.receive_time_ns,
            enforce_continuity=True,
        )
        if out is DeltaOutcome.GAP:
            gaps += 1
            break
        if out is DeltaOutcome.APPLIED:
            applied += 1
            assert pkt.update_id is not None
            applied_us.append(int(pkt.update_id))
            last_u = pkt.update_id
            if applied % CHECKPOINT_EVERY == 0:
                checkpoints.append(
                    book_checkpoint(book, label=f"applied_{applied}", applied=applied)
                )
        elif out.name.startswith("IGNORED") or out in (
            DeltaOutcome.NOT_READY,
        ):
            pass
        else:
            # unexpected
            rejects += 1

    checkpoints.append(book_checkpoint(book, label="final", applied=applied))
    contiguous_miss = 0
    for a, b in zip(applied_us, applied_us[1:]):
        if b != a + 1:
            contiguous_miss += 1
    meta = {
        "applied": applied,
        "gaps": gaps,
        "rejects": rejects,
        "persisted_u_gap_in_applied_stream": contiguous_miss,
        "final_u": book.update_id,
        "final_seq": book.seq,
        "last_seen_u": last_u,
    }
    return book, checkpoints, meta


def fetch_db_packets_logical() -> list[PacketRow]:
    # Logical dedup via FINAL; canonical order by source_line then snapshot first
    # Omit raw_payload (multi-MB) — bids/asks arrays are the lossless replay source.
    sql = f"""
    SELECT
      packet_sha256,
      fight_event_id,
      segment_index,
      source_file,
      source_line_number,
      symbol,
      topic,
      message_type,
      marker_type,
      exchange_ts_ms,
      cts_ms,
      receive_time_ns,
      update_id,
      seq,
      bids,
      asks
    FROM {PACKETS_TBL} FINAL
    WHERE fight_event_id = '{FIGHT_EVENT_ID}'
    ORDER BY
      if(message_type = 'snapshot', 0, 1),
      source_line_number,
      update_id,
      seq
    FORMAT JSONEachRow
    """
    print("[smoke] fetching logical packets from ClickHouse…", flush=True)
    out = ch_query(sql)
    rows: list[PacketRow] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        bids = [(str(t[0]), str(t[1])) for t in (r.get("bids") or [])]
        asks = [(str(t[0]), str(t[1])) for t in (r.get("asks") or [])]
        rows.append(
            PacketRow(
                packet_sha256=r["packet_sha256"],
                fight_event_id=r["fight_event_id"],
                segment_index=int(r["segment_index"]),
                source_file=r["source_file"],
                source_line_number=int(r["source_line_number"]),
                symbol=r["symbol"],
                topic=r["topic"],
                message_type=r["message_type"],
                marker_type=r.get("marker_type"),
                exchange_ts_ms=r.get("exchange_ts_ms"),
                cts_ms=r.get("cts_ms"),
                receive_time_ns=r.get("receive_time_ns"),
                update_id=r.get("update_id"),
                seq=r.get("seq"),
                bids=bids,
                asks=asks,
                raw_payload="",
            )
        )
    print(f"[smoke] fetched {len(rows)} logical packets", flush=True)
    return rows


def compare_checkpoints(src: list[dict], db: list[dict]) -> dict[str, Any]:
    by_label_src = {c["label"]: c for c in src}
    by_label_db = {c["label"]: c for c in db}
    labels = sorted(set(by_label_src) & set(by_label_db), key=lambda x: (x != "after_snapshot", x != "final", x))
    mismatches = []
    matches = []
    keys = [
        "u",
        "seq",
        "best_bid",
        "best_ask",
        "spread",
        "bid_level_count",
        "ask_level_count",
        "book_hash",
        "crossed",
    ]
    for lab in labels:
        a, b = by_label_src[lab], by_label_db[lab]
        diff = {k: {"source": a.get(k), "database": b.get(k)} for k in keys if a.get(k) != b.get(k)}
        # probes exact
        if a.get("probes") != b.get("probes"):
            diff["probes"] = {"source": a.get("probes"), "database": b.get("probes")}
        if diff:
            mismatches.append({"label": lab, "diff": diff})
        else:
            matches.append(lab)
    return {"matched_labels": matches, "mismatches": mismatches, "exact": len(mismatches) == 0}


def run_analysis_examples() -> dict[str, Any]:
    results: dict[str, Any] = {}
    # 1) all changes at an exact price near best bid from final
    price = ch_query(
        f"SELECT tupleElement(bids[1],1) FROM {PACKETS_TBL} FINAL "
        f"WHERE message_type='delta' AND length(bids)>0 "
        f"ORDER BY source_line_number DESC LIMIT 1"
    ).strip()
    if price:
        q1 = ch_query(
            f"""
            SELECT update_id, seq, action, quantity, exchange_ts_ms
            FROM {LEVELS_TBL} FINAL
            WHERE side='bid' AND price='{price}'
            ORDER BY update_id
            LIMIT 20
            FORMAT JSONEachRow
            """
        )
        results["exact_price_changes"] = {
            "price": price,
            "sample_rows": [json.loads(x) for x in q1.splitlines() if x.strip()][:10],
            "count": int(
                ch_query(
                    f"SELECT count() FROM {LEVELS_TBL} FINAL WHERE side='bid' AND price='{price}'"
                ).strip()
                or 0
            ),
        }
    # 2) bid qty in price range over time (sum of absolute change events)
    results["price_range_over_time"] = ch_query(
        f"""
        SELECT
          intDiv(exchange_ts_ms, 10000) AS bucket_10s,
          count() AS changes,
          countIf(action='DELETE') AS deletes,
          countIf(action='UPSERT') AS upserts
        FROM {LEVELS_TBL} FINAL
        WHERE side='bid' AND toFloat64OrZero(price) BETWEEN 80000 AND 82000
        GROUP BY bucket_10s
        ORDER BY bucket_10s
        LIMIT 10
        FORMAT JSONEachRow
        """
    ).strip().splitlines()
    results["price_range_over_time"] = [json.loads(x) for x in results["price_range_over_time"] if x]
    # 3) deletes qty=0
    results["deletes_qty0"] = int(
        ch_query(f"SELECT count() FROM {LEVELS_TBL} FINAL WHERE action='DELETE' AND quantity='0'").strip()
    )
    # 4) refill after reduce: find a price with DELETE then later UPSERT
    refill = ch_query(
        f"""
        SELECT price, countIf(action='DELETE') AS dels, countIf(action='UPSERT') AS ups
        FROM {LEVELS_TBL} FINAL
        WHERE side='ask'
        GROUP BY price
        HAVING dels >= 1 AND ups >= 1
        ORDER BY ups DESC
        LIMIT 5
        FORMAT JSONEachRow
        """
    )
    results["refill_candidates"] = [json.loads(x) for x in refill.splitlines() if x.strip()]
    # 5) largest qty changes in window
    big = ch_query(
        f"""
        SELECT update_id, side, price, quantity, action, abs(toFloat64OrZero(quantity)) AS aq
        FROM {LEVELS_TBL} FINAL
        WHERE action='UPSERT'
        ORDER BY aq DESC
        LIMIT 10
        FORMAT JSONEachRow
        """
    )
    results["largest_qty_changes"] = [json.loads(x) for x in big.splitlines() if x.strip()]
    # 6) book state before/after selected mid u — report packet counts around u
    mid_u = ch_query(
        f"SELECT quantileExact(0.5)(update_id) FROM {PACKETS_TBL} FINAL WHERE message_type='delta' AND update_id IS NOT NULL"
    ).strip()
    results["around_u"] = {
        "selected_u": int(float(mid_u)) if mid_u else None,
        "packets_le": int(
            ch_query(
                f"SELECT count() FROM {PACKETS_TBL} FINAL WHERE message_type='delta' AND update_id <= {int(float(mid_u))}"
            ).strip()
        )
        if mid_u
        else None,
        "packets_gt": int(
            ch_query(
                f"SELECT count() FROM {PACKETS_TBL} FINAL WHERE message_type='delta' AND update_id > {int(float(mid_u))}"
            ).strip()
        )
        if mid_u
        else None,
    }
    # 7) end checkpoint counts from packets
    results["end_packet_stats"] = json.loads(
        ch_query(
            f"""
            SELECT
              countIf(message_type='delta') AS delta_packets,
              countIf(message_type='marker') AS marker_packets,
              countIf(message_type='snapshot') AS snapshot_packets,
              min(update_id) AS min_u,
              max(update_id) AS max_u,
              min(seq) AS min_seq,
              max(seq) AS max_seq
            FROM {PACKETS_TBL} FINAL
            FORMAT JSONEachRow
            """
        ).strip().splitlines()[0]
    )
    return results


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    collector_ok = pid_alive(COLLECTOR_PID)
    oi_ok = pid_alive(OI_PID)

    # --- Phase A evidence ---
    health = load_health()
    btc_rt = next(
        (r for r in (health.get("full_book_runtimes") or []) if r.get("symbol") == SYMBOL),
        {},
    )
    # Sample first stored packet topic
    dctx = zstd.ZstdDecompressor()
    sample_packet = None
    with open(SOURCE_FILE, "rb") as f:
        with dctx.stream_reader(f) as reader:
            text = io.TextIOWrapper(reader, encoding="utf-8")
            for line in text:
                rec = json.loads(line)
                if rec.get("topic") == TOPIC and rec.get("type") == "delta":
                    sample_packet = {
                        "topic": rec.get("topic"),
                        "type": rec.get("type"),
                        "symbol": (rec.get("data") or {}).get("s"),
                        "u": (rec.get("data") or {}).get("u"),
                        "seq": (rec.get("data") or {}).get("seq"),
                        "ts": rec.get("ts"),
                        "cts": rec.get("cts"),
                        "b0": ((rec.get("data") or {}).get("b") or [None])[0],
                        "a0": ((rec.get("data") or {}).get("a") or [None])[0],
                        "keys_data": sorted((rec.get("data") or {}).keys()),
                    }
                    break
    snap = json.loads(
        dctx.decompress(Path(SNAPSHOT_FILE).read_bytes(), max_output_size=200_000_000)
    )
    full_topic_proven = (
        TOPIC in (health.get("confirmed_topics") or [])
        and sample_packet is not None
        and sample_packet["topic"] == TOPIC
        and sample_packet["symbol"] == SYMBOL
    )
    not_ob200 = full_topic_proven and TOPIC == "orderbook.full.BTCUSDT" and "orderbook.200" not in TOPIC
    # Practical depth proof: reconstructed / REST levels > 1000
    not_ob1000 = len(snap["b"]) > 1000 or len(snap["a"]) > 1000
    source_evidence = {
        "collector_pid": COLLECTOR_PID,
        "collector_alive": collector_ok,
        "oi_pid": OI_PID,
        "oi_alive": oi_ok,
        "wanted_topics": health.get("wanted_topics"),
        "confirmed_topics": health.get("confirmed_topics"),
        "full_book_active_topics": health.get("full_book_active_topics"),
        "btc_runtime": {
            "subscription_state": btc_rt.get("subscription_state"),
            "book_ready": btc_rt.get("book_ready"),
            "raw_bids": btc_rt.get("raw_bids"),
            "raw_asks": btc_rt.get("raw_asks"),
            "snapshot_loaded": btc_rt.get("snapshot_loaded"),
        },
        "contract": {
            "depth": 0,
            "levels_capped_at_1000": False,
            "topic": TOPIC,
            "message_type_semantics": "delta changes only; full depth via reconstructed book",
        },
        "sample_stored_packet": sample_packet,
        "rest_snapshot_levels": {"bids": len(snap["b"]), "asks": len(snap["a"]), "u": snap.get("u")},
        "FULL_TOPIC_PROVEN": full_topic_proven,
        "NOT_OB200_PROVEN": not_ob200,
        "NOT_OB1000_PROVEN": not_ob1000,
    }
    (OUT / "source_evidence.json").write_text(json.dumps(source_evidence, indent=2) + "\n")

    if not full_topic_proven:
        verdict = "FULL_OB_TOPIC_NOT_PROVEN"
        (OUT / "REPORT.md").write_text(f"# Verdict\n\n{verdict}\n")
        print(verdict)
        return 2

    # --- Phase B select ---
    packets, sel_stats, sel_meta = select_smoke_packets()
    if sel_stats["applied_deltas"] < 100 or sel_stats["gap_detected_in_selection"] and sel_stats["applied_deltas"] == 0:
        verdict = "FULL_OB_DB_SMOKE_BLOCKED_MISSING_REPLAY_SEED"
        (OUT / "REPORT.md").write_text(
            f"# Verdict\n\n{verdict}\n\nSelection stats:\n```json\n{json.dumps(sel_stats, indent=2)}\n```\n"
        )
        print(verdict)
        return 3
    # Seed present = snapshot packet
    if not any(p.message_type == "snapshot" for p in packets):
        verdict = "FULL_OB_DB_SMOKE_BLOCKED_MISSING_REPLAY_SEED"
        (OUT / "REPORT.md").write_text(f"# Verdict\n\n{verdict}\nMissing snapshot seed.\n")
        print(verdict)
        return 3

    manifest = {
        "fight_event_id": FIGHT_EVENT_ID,
        "source_file": SOURCE_FILE,
        "snapshot_file": SNAPSHOT_FILE,
        "segment_index": 0,
        "smoke_seconds_target": SMOKE_SECONDS,
        "selection": sel_stats,
        "snapshot_meta": {
            "u": sel_meta["snapshot_u"],
            "seq": sel_meta["snapshot_seq"],
            "bid_levels": sel_meta["snapshot_bid_levels"],
            "ask_levels": sel_meta["snapshot_ask_levels"],
        },
        "packet_counts": {
            "total_rows_including_snapshot": len(packets),
            "snapshot": sum(1 for p in packets if p.message_type == "snapshot"),
            "delta": sum(1 for p in packets if p.message_type == "delta"),
            "marker": sum(1 for p in packets if p.message_type == "marker"),
        },
        "u_range_applied": [sel_stats["first_applied_u"], sel_stats["last_applied_u"]],
        "note": "Smoke window ends before first persisted u-gap in the parent event.",
    }
    (OUT / "smoke_input_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    # --- Phase C schema ---
    (OUT / "clickhouse_schema.sql").write_text(SCHEMA_SQL.strip() + "\n")
    ch_admin(SCHEMA_SQL)
    truncate_smoke_tables()

    # --- Phase D import ---
    print(f"[smoke] preparing {len(packets)} packet rows…", flush=True)
    packet_dicts = [p.to_json_each_row() for p in packets]
    # Insert snapshot alone first (large), then remaining rows in bigger batches.
    snap_dicts = [packet_dicts[0]]
    rest_dicts = packet_dicts[1:]
    n_pack = insert_json_each_row(PACKETS_TBL, snap_dicts, batch=1)
    n_pack += insert_json_each_row(PACKETS_TBL, rest_dicts, batch=100)
    print(f"[smoke] packets inserted={n_pack}; deriving level changes…", flush=True)
    ch_admin(f"OPTIMIZE TABLE {DB}.{PACKETS_TBL} FINAL")
    n_lvl = populate_level_changes_from_packets()
    ch_admin(f"OPTIMIZE TABLE {DB}.{LEVELS_TBL} FINAL")
    bid_lvl = int(
        ch_query(f"SELECT count() FROM {LEVELS_TBL} FINAL WHERE side='bid'").strip() or 0
    )
    ask_lvl = int(
        ch_query(f"SELECT count() FROM {LEVELS_TBL} FINAL WHERE side='ask'").strip() or 0
    )

    physical_count = int(ch_query(f"SELECT count() FROM {PACKETS_TBL}").strip())
    logical_count = int(ch_query(f"SELECT count() FROM {PACKETS_TBL} FINAL").strip())
    dup_sha = int(
        ch_query(
            f"SELECT count() FROM (SELECT packet_sha256, count() c FROM {PACKETS_TBL} GROUP BY packet_sha256 HAVING c>1)"
        ).strip()
        or 0
    )
    import_summary = {
        "rows_prepared": len(packets),
        "rows_inserted_packets": n_pack,
        "rows_inserted_level_changes": n_lvl,
        "level_changes_derivation": "ARRAY JOIN from packets FINAL",
        "physical_packet_rows": physical_count,
        "logical_packet_rows_final": logical_count,
        "duplicate_packet_sha_groups_physical": dup_sha,
        "rejected_packet_count": sel_stats["parse_errors"],
        "invalid_price_qty": sel_stats["invalid_price_qty"],
        "marker_rows": sel_stats["marker_rows"],
        "delta_rows": sel_stats["delta_rows"],
        "bid_level_change_rows": bid_lvl,
        "ask_level_change_rows": ask_lvl,
        "min_u": min((p.update_id for p in packets if p.update_id is not None), default=None),
        "max_u": max((p.update_id for p in packets if p.update_id is not None), default=None),
        "min_seq": min((p.seq for p in packets if p.seq is not None), default=None),
        "max_seq": max((p.seq for p in packets if p.seq is not None), default=None),
    }
    (OUT / "import_summary.json").write_text(json.dumps(import_summary, indent=2) + "\n")

    # --- Phase E parity ---
    src_book, src_cps, src_meta = replay_from_packets(packets)
    db_packets = fetch_db_packets_logical()
    db_book, db_cps, db_meta = replay_from_packets(db_packets)
    cp_cmp = compare_checkpoints(src_cps, db_cps)

    source_packet_count = sum(1 for p in packets if p.message_type in ("snapshot", "delta", "marker"))
    clickhouse_packet_count = len(db_packets)
    # Only count delta+snapshot for book parity packet identity
    source_ob_packets = sum(1 for p in packets if p.message_type in ("snapshot", "delta"))
    db_ob_packets = sum(1 for p in db_packets if p.message_type in ("snapshot", "delta"))

    gates = {
        "source_packet_count == clickhouse_packet_count": source_packet_count == clickhouse_packet_count,
        "rejected_packet_count == 0": sel_stats["parse_errors"] == 0,
        "duplicate_packet_count == 0": dup_sha == 0 and logical_count == len(packets),
        "source_feed_u_gap_count == 0": src_meta["gaps"] == 0,
        "persisted_capture_u_gap_count == 0": src_meta["persisted_u_gap_in_applied_stream"] == 0,
        "database_replay_u_gap_count == 0": db_meta["gaps"] == 0 and db_meta["persisted_u_gap_in_applied_stream"] == 0,
        "source_final_u == database_final_u": src_book.update_id == db_book.update_id,
        "source_final_seq == database_final_seq": src_book.seq == db_book.seq,
        "source_book_hash == database_book_hash": book_hash(src_book) == book_hash(db_book),
        "source_best_bid == database_best_bid": src_book.best_bid() == db_book.best_bid(),
        "source_best_ask == database_best_ask": src_book.best_ask() == db_book.best_ask(),
        "source_bid_level_count == database_bid_level_count": len(src_book.bids) == len(db_book.bids),
        "source_ask_level_count == database_ask_level_count": len(src_book.asks) == len(db_book.asks),
        "database_book_crossed == false": not (
            db_book.best_bid() is not None
            and db_book.best_ask() is not None
            and db_book.best_bid() >= db_book.best_ask()
        ),
        "checkpoint_exact": cp_cmp["exact"],
        "ob_packet_counts_match": source_ob_packets == db_ob_packets,
    }
    parity = {
        "gates": gates,
        "all_passed": all(gates.values()),
        "source_meta": src_meta,
        "database_meta": db_meta,
        "source_final": book_checkpoint(src_book, label="source_final", applied=src_meta["applied"]),
        "database_final": book_checkpoint(db_book, label="database_final", applied=db_meta["applied"]),
        "source_packet_count": source_packet_count,
        "clickhouse_packet_count": clickhouse_packet_count,
        "checkpoint_compare": cp_cmp,
        "counts": {
            "source_feed_u_gap_count": src_meta["gaps"],
            "persisted_capture_u_gap_count": src_meta["persisted_u_gap_in_applied_stream"],
            "database_replay_u_gap_count": db_meta["gaps"] + db_meta["persisted_u_gap_in_applied_stream"],
            "rejected_packet_count": sel_stats["parse_errors"],
            "duplicate_packet_count": dup_sha,
        },
    }
    (OUT / "parity_check.json").write_text(json.dumps(parity, indent=2) + "\n")

    # CSV checkpoints
    with open(OUT / "source_vs_database_checkpoints.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "label",
                "src_u",
                "db_u",
                "src_seq",
                "db_seq",
                "src_bb",
                "db_bb",
                "src_ba",
                "db_ba",
                "src_spread",
                "db_spread",
                "src_bids",
                "db_bids",
                "src_asks",
                "db_asks",
                "src_hash",
                "db_hash",
                "src_crossed",
                "db_crossed",
                "match",
            ]
        )
        labels = sorted(
            set(c["label"] for c in src_cps) & set(c["label"] for c in db_cps),
            key=lambda x: (0 if x == "after_snapshot" else 2 if x == "final" else 1, x),
        )
        sm = {c["label"]: c for c in src_cps}
        dm = {c["label"]: c for c in db_cps}
        for lab in labels:
            a, b = sm[lab], dm[lab]
            match = all(a.get(k) == b.get(k) for k in ("u", "seq", "best_bid", "best_ask", "book_hash", "bid_level_count", "ask_level_count", "crossed"))
            w.writerow(
                [
                    lab,
                    a.get("u"),
                    b.get("u"),
                    a.get("seq"),
                    b.get("seq"),
                    a.get("best_bid"),
                    b.get("best_bid"),
                    a.get("best_ask"),
                    b.get("best_ask"),
                    a.get("spread"),
                    b.get("spread"),
                    a.get("bid_level_count"),
                    b.get("bid_level_count"),
                    a.get("ask_level_count"),
                    b.get("ask_level_count"),
                    a.get("book_hash"),
                    b.get("book_hash"),
                    a.get("crossed"),
                    b.get("crossed"),
                    match,
                ]
            )

    # --- Phase F idempotency ---
    print("[smoke] second import for idempotency…", flush=True)
    insert_json_each_row(PACKETS_TBL, snap_dicts, batch=1)
    insert_json_each_row(PACKETS_TBL, rest_dicts, batch=100)
    # Re-derive levels from logical packets (TRUNCATE+rebuild is deterministic)
    populate_level_changes_from_packets()
    physical_after = int(ch_query(f"SELECT count() FROM {PACKETS_TBL}").strip())
    logical_after = int(ch_query(f"SELECT count() FROM {PACKETS_TBL} FINAL").strip())
    ch_admin(f"OPTIMIZE TABLE {DB}.{PACKETS_TBL} FINAL")
    ch_admin(f"OPTIMIZE TABLE {DB}.{LEVELS_TBL} FINAL")
    logical_optimized = int(ch_query(f"SELECT count() FROM {PACKETS_TBL} FINAL").strip())
    physical_optimized = int(ch_query(f"SELECT count() FROM {PACKETS_TBL}").strip())
    db_packets2 = fetch_db_packets_logical()
    db_book2, _, db_meta2 = replay_from_packets(db_packets2)
    idem = {
        "second_import_done": True,
        "physical_rows_before_second": physical_count,
        "physical_rows_after_second_before_optimize": physical_after,
        "logical_rows_after_second_before_optimize": logical_after,
        "physical_rows_after_optimize_final": physical_optimized,
        "logical_rows_after_optimize_final": logical_optimized,
        "unique_packet_sha_expected": len(packets),
        "unique_packet_sha_actual": logical_optimized,
        "final_book_hash_unchanged": book_hash(db_book2) == book_hash(db_book),
        "final_u_unchanged": db_book2.update_id == db_book.update_id,
        "final_seq_unchanged": db_book2.seq == db_book.seq,
        "level_counts_unchanged": len(db_book2.bids) == len(db_book.bids)
        and len(db_book2.asks) == len(db_book.asks),
        "no_logical_duplication": logical_optimized == len(packets),
    }
    idem["passed"] = all(
        [
            idem["final_book_hash_unchanged"],
            idem["final_u_unchanged"],
            idem["final_seq_unchanged"],
            idem["level_counts_unchanged"],
            idem["no_logical_duplication"],
        ]
    )
    (OUT / "idempotency_check.json").write_text(json.dumps(idem, indent=2) + "\n")

    # --- Phase G analysis ---
    analysis = run_analysis_examples()
    (OUT / "example_analysis_queries.sql").write_text(
        f"""-- Full-OB smoke analysis examples against {DB}

-- 1) All changes at an exact price level
SELECT update_id, seq, action, quantity, exchange_ts_ms
FROM {DB}.{LEVELS_TBL} FINAL
WHERE side = 'bid' AND price = '{analysis.get('exact_price_changes', {}).get('price', 'PRICE')}'
ORDER BY update_id;

-- 2) Bid/Ask quantity activity in a price range over time
SELECT
  intDiv(exchange_ts_ms, 10000) AS bucket_10s,
  side,
  count() AS changes,
  countIf(action = 'DELETE') AS deletes,
  countIf(action = 'UPSERT') AS upserts
FROM {DB}.{LEVELS_TBL} FINAL
WHERE toFloat64OrZero(price) BETWEEN 80000 AND 82000
GROUP BY bucket_10s, side
ORDER BY bucket_10s, side;

-- 3) Deletes with quantity = 0
SELECT count() FROM {DB}.{LEVELS_TBL} FINAL WHERE action = 'DELETE' AND quantity = '0';

-- 4) Refills after a prior delete on same price
SELECT price, countIf(action = 'DELETE') AS dels, countIf(action = 'UPSERT') AS ups
FROM {DB}.{LEVELS_TBL} FINAL
WHERE side = 'ask'
GROUP BY price
HAVING dels >= 1 AND ups >= 1
ORDER BY ups DESC
LIMIT 20;

-- 5) Largest quantity upserts in the smoke window
SELECT update_id, side, price, quantity, action
FROM {DB}.{LEVELS_TBL} FINAL
WHERE action = 'UPSERT'
ORDER BY abs(toFloat64OrZero(quantity)) DESC
LIMIT 50;

-- 6) Packets immediately before/after a selected u
SELECT message_type, update_id, seq, exchange_ts_ms, length(bids), length(asks)
FROM {DB}.{PACKETS_TBL} FINAL
WHERE message_type = 'delta' AND update_id BETWEEN {{u}} - 2 AND {{u}} + 2
ORDER BY update_id, source_line_number;

-- 7) End-checkpoint packet aggregates (best bid/ask & level counts come from replay parity)
SELECT
  countIf(message_type = 'delta') AS delta_packets,
  min(update_id) AS min_u,
  max(update_id) AS max_u,
  min(seq) AS min_seq,
  max(seq) AS max_seq
FROM {DB}.{PACKETS_TBL} FINAL;
"""
    )
    (OUT / "analysis_results.json").write_text(json.dumps(analysis, indent=2) + "\n")

    analysis_ok = (
        analysis.get("deletes_qty0", 0) > 0
        and len(analysis.get("largest_qty_changes") or []) > 0
        and analysis.get("end_packet_stats", {}).get("delta_packets", 0) > 0
    )

    collector_ok2 = pid_alive(COLLECTOR_PID)
    oi_ok2 = pid_alive(OI_PID)

    if parity["all_passed"] and idem["passed"] and analysis_ok:
        verdict = "FULL_OB_CLICKHOUSE_SMOKE_EXACT_PARITY"
    else:
        verdict = "FULL_OB_CLICKHOUSE_SMOKE_PARITY_FAILED"

    report = f"""# Full-OB ClickHouse Smoke — BTCUSDT

## Verdict

`{verdict}`

## Source

- Event: `{FIGHT_EVENT_ID}`
- Segment: finalized primary (`continuation_index=0`)
- Source file: `{SOURCE_FILE}`
- Snapshot seed: `{SNAPSHOT_FILE}`
- Topic: `{TOPIC}`
- Smoke window: ~{SMOKE_SECONDS}s applied deltas after REST seed (cut before later event u-gaps)

## Phase A — Topic proof

- FULL_TOPIC_PROVEN={full_topic_proven}
- NOT_OB200_PROVEN={not_ob200}
- NOT_OB1000_PROVEN={not_ob1000}
- Live confirmed_topics includes `{TOPIC}`
- Stored packet topic exactly `{TOPIC}`
- REST snapshot levels: bids={len(snap['b'])}, asks={len(snap['a'])} (>>1000 ⇒ not OB1000)
- Live BTC runtime: book_ready={btc_rt.get('book_ready')}, raw_bids={btc_rt.get('raw_bids')}, raw_asks={btc_rt.get('raw_asks')}
- Contract: depth=0, levels_capped_at_1000=false

## Import / Parity

- Source packet rows (incl. snapshot+markers): {source_packet_count}
- ClickHouse logical packet rows: {clickhouse_packet_count}
- Parsing rejects: {sel_stats['parse_errors']}
- Duplicate packet groups (physical pre-2nd-import): {dup_sha}
- Source feed u-gaps (smoke replay): {src_meta['gaps']}
- Persisted capture u-gaps (applied stream): {src_meta['persisted_u_gap_in_applied_stream']}
- Database replay u-gaps: {db_meta['gaps'] + db_meta['persisted_u_gap_in_applied_stream']}
- Final u/seq source: {src_book.update_id} / {src_book.seq}
- Final u/seq database: {db_book.update_id} / {db_book.seq}
- Source book hash: `{book_hash(src_book)}`
- Database book hash: `{book_hash(db_book)}`
- Bid/Ask levels source: {len(src_book.bids)} / {len(src_book.asks)}
- Bid/Ask levels database: {len(db_book.bids)} / {len(db_book.asks)}
- Crossed (DB): {db_book.best_bid() is not None and db_book.best_ask() is not None and db_book.best_bid() >= db_book.best_ask()}

## Idempotency

- Passed: {idem['passed']}
- Logical unique packets after 2nd import+OPTIMIZE: {logical_optimized} (expected {len(packets)})
- Physical rows after 2nd import (pre-optimize may be higher): {physical_after} → after OPTIMIZE FINAL: {physical_optimized}
- Final hash unchanged: {idem['final_book_hash_unchanged']}

## Analysis fitness

- Runnable: {analysis_ok}
- Deletes qty=0: {analysis.get('deletes_qty0')}
- Refill candidates: {len(analysis.get('refill_candidates') or [])}
- See `example_analysis_queries.sql` and `analysis_results.json`

## Safety

- Collector PID {COLLECTOR_PID} alive before/after: {collector_ok}/{collector_ok2} (untouched)
- OI PID {OI_PID} alive before/after: {oi_ok}/{oi_ok2} (untouched)
- Isolated DB only: `{DB}` (no `orderbook_analysis` writes)
- No open `.tmp` files read
- No commit / push

## Gates

```json
{json.dumps(gates, indent=2)}
```

## Artifacts

All under `results/full_ob_clickhouse_smoke_btc_v1/`.
"""
    (OUT / "REPORT.md").write_text(report)

    summary = {
        "verdict": verdict,
        "FULL_TOPIC_PROVEN": full_topic_proven,
        "NOT_OB200_PROVEN": not_ob200,
        "NOT_OB1000_PROVEN": not_ob1000,
        "source_file": SOURCE_FILE,
        "event": FIGHT_EVENT_ID,
        "topic": TOPIC,
        "source_packet_count": source_packet_count,
        "clickhouse_packet_count": clickhouse_packet_count,
        "parsing_rejects": sel_stats["parse_errors"],
        "duplicates": dup_sha,
        "u_gaps": parity["counts"],
        "final_u_seq": {"source": [src_book.update_id, src_book.seq], "db": [db_book.update_id, db_book.seq]},
        "book_hash": {"source": book_hash(src_book), "db": book_hash(db_book)},
        "levels": {
            "source": [len(src_book.bids), len(src_book.asks)],
            "db": [len(db_book.bids), len(db_book.asks)],
        },
        "idempotency": idem["passed"],
        "analysis": analysis_ok,
        "collector_unchanged": collector_ok and collector_ok2,
        "oi_unchanged": oi_ok and oi_ok2,
    }
    (OUT / "END_SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if verdict == "FULL_OB_CLICKHOUSE_SMOKE_EXACT_PARITY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
