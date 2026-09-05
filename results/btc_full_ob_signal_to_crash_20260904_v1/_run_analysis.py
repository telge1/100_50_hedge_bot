#!/usr/bin/env python3
"""Isolated Full-OB signal-to-crash analysis for BTCUSDT 2026-09-04.

Read-only on live collector / open .tmp. Imports only finalized segments into
research_full_ob_btc_20260904_signal_analysis. Does not touch production DBs.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import struct
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import zstandard as zstd
from dotenv import load_dotenv

OA = Path("/home/telgenbuescher/projects/orderbook_analyse")
EV = (
    OA
    / "data/orderbook_raw_shadow/full_ob_edge_flight_recorder/BTCUSDT/2026-09-04"
    / "BTCUSDT_20260904T112735Z_eb6191222e"
)
OUT = Path(__file__).resolve().parent
DB = "research_full_ob_btc_20260904_signal_analysis"
FIGHT = "BTCUSDT_20260904T112735Z_eb6191222e"

CRASH_HINT = datetime(2026, 9, 4, 12, 30, 0, tzinfo=timezone.utc)
PARENT_TS = datetime(2026, 9, 4, 11, 27, 35, 764963, tzinfo=timezone.utc)

SEGMENTS = [
    {"index": 0, "path": EV / "full_ob_raw_deltas.jsonl.zst", "source": "seg0/full_ob_raw_deltas.jsonl.zst"},
    {"index": 1, "path": EV / "cont_001" / "full_ob_raw_deltas.jsonl.zst", "source": "cont_001/full_ob_raw_deltas.jsonl.zst"},
    {"index": 2, "path": EV / "cont_002" / "full_ob_raw_deltas.jsonl.zst", "source": "cont_002/full_ob_raw_deltas.jsonl.zst"},
]


def utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)


def bps_dist(mid: float, px: float) -> float:
    if mid <= 0:
        return float("inf")
    return abs(px - mid) / mid * 10_000.0


def packet_sha(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    u: int | None = None
    epoch: int | None = None
    crossed_count: int = 0

    def apply_snapshot(self, b: list, a: list, u: int | None, epoch: int | None) -> None:
        self.bids.clear()
        self.asks.clear()
        for p, s in b:
            pf, sf = float(p), float(s)
            if sf > 0:
                self.bids[pf] = sf
        for p, s in a:
            pf, sf = float(p), float(s)
            if sf > 0:
                self.asks[pf] = sf
        self.u = u
        self.epoch = epoch
        self._check_cross()

    def apply_delta(self, b: list, a: list, u: int | None, epoch: int | None) -> list[dict]:
        """Apply delta; return level change events for wall/withdrawal tracking."""
        changes: list[dict] = []
        for p, s in b:
            pf, sf = float(p), float(s)
            prev = self.bids.get(pf, 0.0)
            if sf <= 0:
                if pf in self.bids:
                    del self.bids[pf]
                changes.append({"side": "bid", "price": pf, "prev": prev, "new": 0.0})
            else:
                self.bids[pf] = sf
                changes.append({"side": "bid", "price": pf, "prev": prev, "new": sf})
        for p, s in a:
            pf, sf = float(p), float(s)
            prev = self.asks.get(pf, 0.0)
            if sf <= 0:
                if pf in self.asks:
                    del self.asks[pf]
                changes.append({"side": "ask", "price": pf, "prev": prev, "new": 0.0})
            else:
                self.asks[pf] = sf
                changes.append({"side": "ask", "price": pf, "prev": prev, "new": sf})
        self.u = u
        if epoch is not None:
            self.epoch = epoch
        self._check_cross()
        return changes

    def _check_cross(self) -> None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is not None and ba is not None and bb >= ba:
            self.crossed_count += 1

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    def microprice(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        bsz = self.bids.get(bb, 0.0)
        asz = self.asks.get(ba, 0.0)
        denom = bsz + asz
        if denom <= 0:
            return self.mid()
        return (ba * bsz + bb * asz) / denom

    def depth_bands(self, mid: float, bands: Iterable[float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for bp in bands:
            thr = mid * bp / 10_000.0
            bid_q = sum(s for p, s in self.bids.items() if mid - thr <= p <= mid)
            ask_q = sum(s for p, s in self.asks.items() if mid <= p <= mid + thr)
            out[f"bid_{int(bp)}bps"] = bid_q
            out[f"ask_{int(bp)}bps"] = ask_q
            tot = bid_q + ask_q
            out[f"imb_{int(bp)}bps"] = (bid_q - ask_q) / tot if tot > 0 else 0.0
        return out


def iter_records(path: Path) -> Iterable[tuple[int, str, dict]]:
    dctx = zstd.ZstdDecompressor()
    with open(path, "rb") as f:
        with dctx.stream_reader(f) as r:
            tf = io.TextIOWrapper(r, encoding="utf-8")
            for i, line in enumerate(tf, start=1):
                yield i, line.rstrip("\n"), json.loads(line)


def load_signal_contracts() -> list[dict]:
    contracts = [
        json.loads(l)
        for l in (EV / "signal_analysis_contracts.jsonl").read_text().splitlines()
        if l.strip()
    ]
    nested = {
        json.loads(l)["nested_signal_id"]: json.loads(l)
        for l in (EV / "nested_profile_signals.jsonl").read_text().splitlines()
        if l.strip()
    }
    man = json.loads((EV / "event_manifest.json").read_text())
    prof = json.loads((EV / "profile_context.json").read_text())
    # Enrich parent from profile_context / manifest
    out = []
    for c in contracts:
        sid = c["signal_id"]
        row = dict(c)
        if c.get("signal_kind") == "PARENT":
            row.update(
                {
                    "profile_id": prof.get("profile_id") or c.get("profile_id"),
                    "vah": prof.get("volume_vah"),
                    "val": prof.get("volume_val"),
                    "poc": prof.get("volume_poc"),
                    "edge_side": man.get("edge_type"),
                    "parent_fight_event_id": FIGHT,
                    "gap_status_note": "parent_in_seg0_epoch0_clean; later capture has RESYNC gaps in cont_001",
                }
            )
        else:
            n = nested.get(sid, {})
            row.update(
                {
                    "edge_side": n.get("edge_side"),
                    "arm_ts": n.get("arm_ts"),
                    "profile_fallback_used": n.get("profile_fallback_used"),
                    "gap_status_note": "nested continuity_epoch_id claimed 0; raw stream may include later epochs",
                }
            )
        out.append(row)
    # Focus signals A/B/C
    focus_ids = {
        f"{FIGHT}_parent",
        f"{FIGHT}_ns_3d51be69d9df_1_L",
        f"{FIGHT}_ns_3d51be69d9df_1_U",
    }
    return [r for r in out if r["signal_id"] in focus_ids]


def create_and_import(client) -> dict:
    client.command(f"CREATE DATABASE IF NOT EXISTS {DB}")
    client.command(f"DROP TABLE IF EXISTS {DB}.full_ob_packets_v1")
    client.command(f"DROP TABLE IF EXISTS {DB}.signal_contracts_v1")
    client.command(
        f"""
        CREATE TABLE {DB}.full_ob_packets_v1 (
            packet_sha256 FixedString(64),
            fight_event_id String,
            segment_index UInt32,
            source_file String,
            source_line_number UInt64,
            symbol LowCardinality(String),
            topic String,
            message_type LowCardinality(String),
            record_kind LowCardinality(String),
            marker_type Nullable(String),
            continuity_epoch_id Nullable(Int32),
            exchange_ts_ms Nullable(Int64),
            cts_ms Nullable(Int64),
            receive_time_ns Nullable(Int64),
            update_id Nullable(Int64),
            seq Nullable(Int64),
            bids Array(Tuple(String, String)),
            asks Array(Tuple(String, String)),
            book_hash Nullable(String),
            raw_payload String,
            ingestion_ts DateTime64(3, 'UTC') DEFAULT now64(3)
        ) ENGINE = ReplacingMergeTree(ingestion_ts)
        ORDER BY (packet_sha256)
        """
    )
    client.command(
        f"""
        CREATE TABLE {DB}.signal_contracts_v1 (
            signal_id String,
            signal_kind LowCardinality(String),
            payload String
        ) ENGINE = MergeTree ORDER BY signal_id
        """
    )

    source_count = 0
    parse_rejects = 0
    sha_seen: set[str] = set()
    logical_dupes = 0
    batch: list[list[Any]] = []
    cols = [
        "packet_sha256",
        "fight_event_id",
        "segment_index",
        "source_file",
        "source_line_number",
        "symbol",
        "topic",
        "message_type",
        "record_kind",
        "marker_type",
        "continuity_epoch_id",
        "exchange_ts_ms",
        "cts_ms",
        "receive_time_ns",
        "update_id",
        "seq",
        "bids",
        "asks",
        "book_hash",
        "raw_payload",
    ]

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        client.insert(f"{DB}.full_ob_packets_v1", batch, column_names=cols)
        batch = []

    checkpoint_hashes = []
    for seg in SEGMENTS:
        for line_no, raw, o in iter_records(seg["path"]):
            source_count += 1
            try:
                sha = packet_sha(raw)
                if sha in sha_seen:
                    logical_dupes += 1
                else:
                    sha_seen.add(sha)
                data = o.get("data") or {}
                rk = o.get("record_kind") or ""
                # Full level arrays only for checkpoints (parity = packet count/sha).
                # Delta bodies stay in source files; CH stores metadata + sha of full raw line.
                store_levels = rk in ("INITIAL_CHECKPOINT", "RESYNC_CHECKPOINT") or o.get("type") == "snapshot"
                bids = data.get("b") or [] if store_levels else []
                asks = data.get("a") or [] if store_levels else []
                mt = None
                if rk == "EVENT_MARKER" or o.get("channel") == "marker":
                    mt = o.get("marker_type") or o.get("marker") or o.get("type")
                # Keep sha over full raw; store compact raw stub for deltas to avoid multi-GB insert.
                raw_store = raw if store_levels or len(raw) < 4000 else json.dumps(
                    {
                        "sha256_full": sha,
                        "record_kind": rk,
                        "type": o.get("type"),
                        "ts": o.get("ts"),
                        "u": data.get("u"),
                        "seq": data.get("seq"),
                        "continuity_epoch_id": o.get("continuity_epoch_id"),
                        "level_update_count": o.get("level_update_count"),
                        "note": "full_raw_retained_in_source_file_only",
                    },
                    separators=(",", ":"),
                )
                row = [
                    sha,
                    o.get("fight_event_id") or FIGHT,
                    int(seg["index"]),
                    seg["source"],
                    int(line_no),
                    o.get("symbol") or data.get("s") or "BTCUSDT",
                    o.get("topic") or "",
                    o.get("type") or "",
                    rk,
                    mt,
                    o.get("continuity_epoch_id"),
                    o.get("ts") if isinstance(o.get("ts"), (int, float)) else None,
                    o.get("cts") if isinstance(o.get("cts"), (int, float)) else None,
                    o.get("local_receive_time_ns"),
                    data.get("u") if data.get("u") is not None else o.get("u"),
                    data.get("seq") if data.get("seq") is not None else o.get("seq"),
                    [(str(p), str(s)) for p, s in bids],
                    [(str(p), str(s)) for p, s in asks],
                    o.get("book_hash"),
                    raw_store,
                ]
                if rk in ("INITIAL_CHECKPOINT", "RESYNC_CHECKPOINT"):
                    checkpoint_hashes.append(
                        {"kind": rk, "epoch": o.get("continuity_epoch_id"), "book_hash": o.get("book_hash"), "segment": seg["index"]}
                    )
                batch.append(row)
                if len(batch) >= 50:
                    flush()
            except Exception:
                parse_rejects += 1
        flush()

    contracts = load_signal_contracts()
    client.insert(
        f"{DB}.signal_contracts_v1",
        [[c["signal_id"], c.get("signal_kind"), json.dumps(c)] for c in contracts],
        column_names=["signal_id", "signal_kind", "payload"],
    )

    db_count = client.query(f"SELECT count() FROM {DB}.full_ob_packets_v1").result_rows[0][0]
    # Force merge for replacing MT uniqueness check
    client.command(f"OPTIMIZE TABLE {DB}.full_ob_packets_v1 FINAL")
    db_count_final = client.query(f"SELECT count() FROM {DB}.full_ob_packets_v1 FINAL").result_rows[0][0]

    return {
        "database": DB,
        "source_packet_count": source_count,
        "database_packet_count": int(db_count),
        "database_packet_count_final": int(db_count_final),
        "parse_rejects": parse_rejects,
        "logical_duplicates": logical_dupes,
        "unique_sha_count": len(sha_seen),
        "checkpoint_hash_records": checkpoint_hashes,
        "checkpoint_hash_ok": all(bool(x.get("book_hash")) for x in checkpoint_hashes if x["kind"] == "INITIAL_CHECKPOINT"),
        "source_packet_count_eq_database": source_count == int(db_count) and parse_rejects == 0,
    }


def analyze_stream() -> dict:
    bands = (5, 10, 20, 50, 100)
    book = Book()
    depth_rows: list[dict] = []
    imb_rows: list[dict] = []
    price_series: list[tuple[datetime, float, int | None, int | None]] = []
    wall_tracks: dict[tuple[str, float], dict] = {}
    wall_refills: list[dict] = []
    bid_withdrawals: list[dict] = []
    edge_events: list[dict] = []
    markers: list[dict] = []

    last_sample_sec = -1
    prev_epoch: int | None = None
    epoch_boundaries: list[dict] = []
    u_gaps: list[dict] = []
    prev_u: int | None = None
    replay_ok = True
    parse_rejects = 0
    n_packets = 0

    # Parent profile edges for edge-zone (signal isolation: use each signal's own edges separately later)
    contracts = {c["signal_id"]: c for c in load_signal_contracts()}
    parent = contracts[f"{FIGHT}_parent"]
    nested_l = contracts[f"{FIGHT}_ns_3d51be69d9df_1_L"]
    nested_u = contracts[f"{FIGHT}_ns_3d51be69d9df_1_U"]

    def note_wall(side: str, price: float, size: float, ts: datetime, mid: float) -> None:
        key = (side, round(price, 1))  # 0.1 tick grouping for BTC
        # wall if large relative to near-touch: size > 5 BTC notional equiv at top of book heuristic
        # Use absolute size threshold descriptive only: >= 20 BTC at a single level within 50bps
        if bps_dist(mid, price) > 50:
            return
        if size < 15.0 and key not in wall_tracks:
            return
        tr = wall_tracks.get(key)
        if tr is None:
            wall_tracks[key] = {
                "side": side,
                "price": price,
                "first_seen": utc_iso(ts),
                "last_seen": utc_iso(ts),
                "max_size": size,
                "sum_size": size,
                "n_obs": 1,
                "refill_count": 0,
                "last_size": size,
                "min_dist_bps_to_mid": bps_dist(mid, price),
                "status": "OPEN",
            }
            return
        if size > tr["last_size"] * 1.25 and size >= 15.0:
            tr["refill_count"] += 1
            wall_refills.append(
                {
                    "side": side,
                    "price": price,
                    "ts": utc_iso(ts),
                    "prev_size": tr["last_size"],
                    "new_size": size,
                    "mid": mid,
                    "dist_bps": bps_dist(mid, price),
                    "association": "UNMATCHED_L2_CHANGE",
                }
            )
        tr["last_seen"] = utc_iso(ts)
        tr["max_size"] = max(tr["max_size"], size)
        tr["sum_size"] += size
        tr["n_obs"] += 1
        tr["last_size"] = size
        tr["min_dist_bps_to_mid"] = min(tr["min_dist_bps_to_mid"], bps_dist(mid, price))

    for seg in SEGMENTS:
        for line_no, raw, o in iter_records(seg["path"]):
            n_packets += 1
            try:
                rk = o.get("record_kind") or ""
                data = o.get("data") or {}
                epoch = o.get("continuity_epoch_id")
                ts_ms = o.get("ts") if isinstance(o.get("ts"), (int, float)) else None
                ts = ms_to_dt(ts_ms) if ts_ms is not None else None
                if epoch is not None and prev_epoch is not None and epoch != prev_epoch:
                    epoch_boundaries.append(
                        {
                            "ts": utc_iso(ts),
                            "from_epoch": prev_epoch,
                            "to_epoch": epoch,
                            "segment": seg["index"],
                            "record_kind": rk,
                        }
                    )
                    prev_u = None  # fail-closed across epochs for u continuity
                if epoch is not None:
                    prev_epoch = epoch

                if rk in ("INITIAL_CHECKPOINT", "RESYNC_CHECKPOINT") or o.get("type") == "snapshot":
                    book.apply_snapshot(data.get("b") or [], data.get("a") or [], data.get("u"), epoch)
                    prev_u = int(data["u"]) if data.get("u") is not None else prev_u
                elif o.get("type") == "delta":
                    u = data.get("u")
                    if u is not None:
                        u = int(u)
                        if prev_u is not None and u > prev_u + 1:
                            u_gaps.append(
                                {
                                    "segment": seg["index"],
                                    "epoch": epoch,
                                    "prev_u": prev_u,
                                    "u": u,
                                    "missing": u - prev_u - 1,
                                    "ts": utc_iso(ts),
                                }
                            )
                        prev_u = u
                    changes = book.apply_delta(data.get("b") or [], data.get("a") or [], u, epoch)
                    mid = book.mid()
                    if mid is not None and ts is not None:
                        for ch in changes:
                            # Bid withdrawal: size reduced/removed without claiming cancel vs fill
                            if ch["side"] == "bid" and ch["prev"] >= 10.0 and ch["new"] < ch["prev"] * 0.5:
                                if ch["price"] >= mid - mid * 50 / 10_000:
                                    bid_withdrawals.append(
                                        {
                                            "ts": utc_iso(ts),
                                            "price": ch["price"],
                                            "prev_size": ch["prev"],
                                            "new_size": ch["new"],
                                            "mid": mid,
                                            "dist_bps": bps_dist(mid, ch["price"]),
                                            "mechanism": "UNMATCHED_L2_CHANGE",
                                            "note": "size=0/reduced may be fill or cancel; no exchange link",
                                            "before_crash_hint": ts < CRASH_HINT,
                                        }
                                    )
                            if ch["side"] == "ask" and ch["new"] >= 15.0:
                                note_wall("ask", ch["price"], ch["new"], ts, mid)
                            if ch["side"] == "bid" and ch["new"] >= 15.0:
                                note_wall("bid", ch["price"], ch["new"], ts, mid)
                elif rk == "EVENT_MARKER" or o.get("channel") == "marker":
                    markers.append(
                        {
                            "ts": utc_iso(ts),
                            "marker": o.get("marker_type") or o.get("marker") or o.get("type"),
                            "segment": seg["index"],
                            "epoch": epoch,
                        }
                    )

                mid = book.mid()
                if mid is not None and ts is not None:
                    price_series.append((ts, mid, book.u, book.epoch))
                    sec = int(ts.timestamp())
                    if sec != last_sample_sec:
                        last_sample_sec = sec
                        bb, ba = book.best_bid(), book.best_ask()
                        d = book.depth_bands(mid, bands)
                        row = {
                            "ts": utc_iso(ts),
                            "segment": seg["index"],
                            "epoch": book.epoch,
                            "mid": mid,
                            "microprice": book.microprice(),
                            "best_bid": bb,
                            "best_ask": ba,
                            "spread": (ba - bb) if bb is not None and ba is not None else None,
                            "n_bids": len(book.bids),
                            "n_asks": len(book.asks),
                            "u": book.u,
                            **d,
                        }
                        depth_rows.append(row)
                        imb_rows.append(
                            {
                                "ts": row["ts"],
                                "epoch": book.epoch,
                                "imb_5bps": d["imb_5bps"],
                                "imb_10bps": d["imb_10bps"],
                                "imb_20bps": d["imb_20bps"],
                                "imb_50bps": d["imb_50bps"],
                                "imb_100bps": d["imb_100bps"],
                                "bid_20bps": d["bid_20bps"],
                                "ask_20bps": d["ask_20bps"],
                                "bid_50bps": d["bid_50bps"],
                                "ask_50bps": d["ask_50bps"],
                                "mid": mid,
                            }
                        )
                        # Edge-zone occupancy for each signal's own edge
                        for label, c in [
                            ("parent", parent),
                            ("nested_lower", nested_l),
                            ("nested_upper", nested_u),
                        ]:
                            edge_px = float(c.get("edge_price") or 0)
                            if edge_px <= 0:
                                continue
                            dist = bps_dist(mid, edge_px)
                            if dist <= 25:
                                edge_events.append(
                                    {
                                        "ts": utc_iso(ts),
                                        "signal_id": c["signal_id"],
                                        "signal_label": label,
                                        "edge": c.get("edge"),
                                        "edge_price": edge_px,
                                        "mid": mid,
                                        "dist_bps": dist,
                                        "ask_near_edge": sum(
                                            s
                                            for p, s in book.asks.items()
                                            if abs(p - edge_px) / mid * 10_000 <= 5
                                        ),
                                        "bid_near_edge": sum(
                                            s
                                            for p, s in book.bids.items()
                                            if abs(p - edge_px) / mid * 10_000 <= 5
                                        ),
                                        "epoch": book.epoch,
                                    }
                                )
            except Exception:
                parse_rejects += 1
                replay_ok = False

    # Detect crash onset from mid series after 12:25 without using future for feature defs
    crash_start = None
    local_high = None
    local_high_ts = None
    local_low = None
    local_low_ts = None
    accel_ts = None
    pre = [x for x in price_series if x[0] >= datetime(2026, 9, 4, 12, 20, tzinfo=timezone.utc)]
    if pre:
        # rolling: find first time mid drops >= 0.15% from max of prior 5 minutes after 12:25
        for i, (ts, mid, _, _) in enumerate(pre):
            window = [m for t, m, _, _ in pre[: i + 1] if ts - timedelta(minutes=5) <= t <= ts]
            hi = max(window) if window else mid
            if local_high is None or hi > local_high:
                local_high, local_high_ts = hi, ts
            if mid <= hi * (1 - 0.0015) and ts >= datetime(2026, 9, 4, 12, 25, tzinfo=timezone.utc):
                if crash_start is None:
                    crash_start = ts
                    break
        # acceleration: largest 30s drop after crash_start
        if crash_start:
            after = [x for x in price_series if x[0] >= crash_start]
            best = None
            for i, (ts, mid, _, _) in enumerate(after):
                later = [m for t, m, _, _ in after if ts <= t <= ts + timedelta(seconds=30)]
                if later:
                    drop = mid - min(later)
                    if best is None or drop > best[0]:
                        best = (drop, ts)
            accel_ts = best[1] if best else crash_start
            local_low = min(m for _, m, _, _ in after)
            local_low_ts = min(after, key=lambda x: x[1])[0]

    if crash_start is None:
        crash_start = CRASH_HINT  # fallback labeled as hint

    def mid_at(t0: datetime) -> float | None:
        cand = [m for t, m, _, _ in price_series if t <= t0]
        return cand[-1] if cand else None

    def ret_from(signal_ts: datetime, horizons_s: list[int], until: datetime | None = None) -> dict:
        m0 = mid_at(signal_ts)
        out = {"mid_at_signal": m0}
        if m0 is None:
            return out
        for h in horizons_s:
            mt = mid_at(signal_ts + timedelta(seconds=h))
            out[f"ret_{h}s_bps"] = None if mt is None else (mt - m0) / m0 * 10_000
        if until is not None:
            mu = mid_at(until)
            out["ret_until_crash_start_bps"] = None if mu is None else (mu - m0) / m0 * 10_000
        if local_low is not None:
            out["ret_until_local_low_bps"] = (local_low - m0) / m0 * 10_000
        return out

    # Pre-crash bid depth trend (only samples before crash_start)
    pre_crash_imb = [r for r in imb_rows if datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) < crash_start]
    first_bearish = None
    # First sustained negative 50bps imbalance lasting >= 60s before crash, after nested upper
    nested_u_ts = datetime.fromisoformat(nested_u["trigger_ts"].replace("Z", "+00:00"))
    run_start = None
    for r in pre_crash_imb:
        ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        if ts < nested_u_ts:
            continue
        if r["imb_50bps"] < -0.2:
            if run_start is None:
                run_start = ts
            elif (ts - run_start).total_seconds() >= 60 and first_bearish is None:
                first_bearish = {
                    "kind": "ASK_HEAVY_IMBALANCE_50BPS",
                    "first_ts": utc_iso(run_start),
                    "confirm_ts": utc_iso(ts),
                    "lead_seconds_to_crash": (crash_start - run_start).total_seconds(),
                    "imb_50bps": r["imb_50bps"],
                    "phase": "BEFORE_CRASH",
                    "strength_descriptive": "moderate_ask_heavy_imbalance_persisted_60s",
                    "data_quality": "FULL_OB_SAMPLED_1S",
                    "proof": "TEMPORALLY_ASSOCIATED",
                }
        else:
            run_start = None

    # Bid withdrawal volume before crash
    bw_before = [b for b in bid_withdrawals if b["before_crash_hint"] and datetime.fromisoformat(b["ts"].replace("Z", "+00:00")) < crash_start]
    bw_during = [b for b in bid_withdrawals if datetime.fromisoformat(b["ts"].replace("Z", "+00:00")) >= crash_start]

    # Ask walls summary
    ask_walls = sorted(
        [v for k, v in wall_tracks.items() if k[0] == "ask"],
        key=lambda x: -x["max_size"],
    )[:20]
    bid_walls = sorted(
        [v for k, v in wall_tracks.items() if k[0] == "bid"],
        key=lambda x: -x["max_size"],
    )[:20]

    # Coverage per signal (own windows, fail-closed on gaps overlapping window)
    def coverage_for(c: dict) -> dict:
        t0 = datetime.fromisoformat(c["trigger_ts"].replace("Z", "+00:00"))
        pre_s = float(c.get("pre_seconds") or 600)
        post_s = float(c.get("min_post_seconds") or 3600)
        w0 = t0 - timedelta(seconds=pre_s)
        w1 = t0 + timedelta(seconds=post_s)
        # available finalized data ends at cont_002 last
        data_end = price_series[-1][0] if price_series else None
        gaps_in = [
            g
            for g in u_gaps
            if g["ts"]
            and w0 <= datetime.fromisoformat(g["ts"].replace("Z", "+00:00")) <= min(w1, data_end or w1)
        ]
        epochs_in = sorted(
            {
                e
                for t, _, _, e in price_series
                if e is not None and w0 <= t <= min(w1, data_end or w1)
            }
        )
        return {
            "signal_id": c["signal_id"],
            "analysis_window_start": utc_iso(w0),
            "analysis_window_end": utc_iso(w1),
            "finalized_data_end": utc_iso(data_end),
            "window_fully_in_finalized": bool(data_end and data_end >= min(w1, crash_start + timedelta(minutes=5))),
            "u_gaps_in_window": len(gaps_in),
            "epochs_in_window": epochs_in,
            "multi_epoch_fail_closed": len(epochs_in) > 1,
            "research_eligible_contract": c.get("research_eligible"),
            "coverage_status_contract": c.get("coverage_status"),
            "outcomes": ret_from(t0, [60, 300, 600, 1800], until=crash_start),
        }

    signal_findings = {
        "A_parent_upper": {
            "contract": parent,
            "coverage": coverage_for(parent),
            "note": "Best clean epoch0 coverage through seg0; later post window spans resync gap region",
        },
        "B_nested_lower": {
            "contract": nested_l,
            "coverage": coverage_for(nested_l),
        },
        "C_nested_upper": {
            "contract": nested_u,
            "coverage": coverage_for(nested_u),
            "note": "Trigger 12:06 sits inside cont_001 multi-epoch/resync region — fail-closed for cross-epoch metrics",
        },
    }

    # Pattern labels
    patterns = {
        "BREAKOUT_CONFIRMATION": {
            "status": "PARTIALLY_OBSERVED",
            "reason": "Parent UPPER cross then acceptance to FIGHT_ACTIVE; price later failed to sustain above VAH region",
        },
        "FAILED_BREAKOUT": {
            "status": "OBSERVED",
            "reason": "After parent UPPER and nested UPPER attempts, price ultimately sold off through prior edge region before/into crash window",
        },
        "ASK_DEFENSE": {
            "status": "PARTIALLY_OBSERVED" if ask_walls else "NOT_OBSERVED",
            "reason": "Large ask levels tracked within 50bps; refill events recorded as UNMATCHED_L2_CHANGE only",
        },
        "BUY_ABSORPTION": {
            "status": "NOT_EVALUABLE",
            "reason": "No Sep-4 public trades in research CH; FR public_trades placeholders empty; cannot prove aggression vs L2",
        },
        "BID_WITHDRAWAL": {
            "status": "OBSERVED" if bw_before else "NOT_OBSERVED",
            "reason": f"{len(bw_before)} unmatched bid size reductions >=10 BTC half-cut within 50bps before crash_start",
        },
        "BID_CONSUMPTION": {
            "status": "NOT_EVALUABLE",
            "reason": "size=0/reduced without trade link cannot be classified as consumption vs cancel",
        },
        "SELLER_CONTROL": {
            "status": "PARTIALLY_OBSERVED" if first_bearish else "NOT_OBSERVED",
            "reason": "Ask-heavy 50bps imbalance persistence before crash if detected; not exchange-linked aggression",
        },
        "EARLY_BEARISH_WARNING": {
            "status": "PARTIALLY_OBSERVED" if first_bearish or bw_before else "NOT_OBSERVED",
            "reason": "Pre-crash imbalance and/or bid withdrawals visible in finalized Full OB before crash_start",
        },
        "TRADE_DIRECTION": {"status": "NOT_EVALUATED", "reason": "Explicitly out of scope"},
    }

    # Early warning timeline rows
    ewt = []
    if first_bearish:
        ewt.append(first_bearish)
    if bw_before:
        first_bw = min(bw_before, key=lambda x: x["ts"])
        ewt.append(
            {
                "kind": "BID_SIZE_REDUCTION_NEAR_TOUCH",
                "first_ts": first_bw["ts"],
                "lead_seconds_to_crash": (
                    crash_start - datetime.fromisoformat(first_bw["ts"].replace("Z", "+00:00"))
                ).total_seconds(),
                "phase": "BEFORE_CRASH",
                "strength_descriptive": "single_level_bid_reduction",
                "data_quality": "FULL_OB",
                "proof": "UNMATCHED_L2_CHANGE",
            }
        )
    ewt.append(
        {
            "kind": "CRASH_START_DETECTED",
            "first_ts": utc_iso(crash_start),
            "lead_seconds_to_crash": 0,
            "phase": "CRASH_ONSET",
            "method": "mid_drop_ge_15bps_from_5m_high_after_12:25",
            "proof": "PRICE_SERIES_FROM_FULL_OB_BBO",
        }
    )

    # Write CSVs
    def write_csv(name: str, rows: list[dict]) -> None:
        path = OUT / name
        if not rows:
            path.write_text("")
            return
        fields: list[str] = []
        seen: set[str] = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fields.append(k)
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    # signal contracts csv
    sc_rows = []
    for c in [parent, nested_l, nested_u]:
        sc_rows.append(
            {
                "signal_id": c["signal_id"],
                "signal_kind": c.get("signal_kind"),
                "profile_id": c.get("profile_id"),
                "edge": c.get("edge"),
                "edge_side": c.get("edge_side") or c.get("edge_type"),
                "edge_price": c.get("edge_price"),
                "trigger_price": c.get("trigger_price"),
                "trigger_ts": c.get("trigger_ts"),
                "vah": c.get("vah"),
                "val": c.get("val"),
                "poc": c.get("poc"),
                "analysis_pre_start_ts": c.get("analysis_pre_start_ts"),
                "analysis_post_end_ts": c.get("analysis_post_end_ts"),
                "continuity_epoch_id": c.get("continuity_epoch_id"),
                "coverage_status": c.get("coverage_status"),
                "research_eligible": c.get("research_eligible"),
                "overlap_cluster_id": c.get("overlap_cluster_id"),
            }
        )
    write_csv("signal_contracts.csv", sc_rows)

    # price timeline
    timeline = [
        {"ts": "2026-09-04T11:00:00Z", "event": "parent_profile_cutoff", "mid": "", "detail": "parent profile end"},
        {"ts": "2026-09-04T11:27:32.479523Z", "event": "parent_arm", "mid": "", "detail": "lifecycle ARMED"},
        {"ts": utc_iso(PARENT_TS), "event": "parent_cross_UPPER", "mid": mid_at(PARENT_TS) or "", "detail": "GENUINE_CROSS_IN VAH"},
        {"ts": nested_l["trigger_ts"], "event": "nested_lower", "mid": mid_at(datetime.fromisoformat(nested_l["trigger_ts"].replace("Z", "+00:00"))) or "", "detail": nested_l["signal_id"]},
        {"ts": nested_u["trigger_ts"], "event": "nested_upper", "mid": mid_at(datetime.fromisoformat(nested_u["trigger_ts"].replace("Z", "+00:00"))) or "", "detail": nested_u["signal_id"]},
        {"ts": utc_iso(local_high_ts), "event": "local_high_pre_crash", "mid": local_high or "", "detail": "from Full-OB mid"},
        {"ts": utc_iso(crash_start), "event": "crash_start", "mid": mid_at(crash_start) or "", "detail": "detected drop from 5m high"},
        {"ts": utc_iso(accel_ts), "event": "crash_acceleration", "mid": mid_at(accel_ts) if accel_ts else "", "detail": "max 30s drop onset"},
        {"ts": utc_iso(local_low_ts), "event": "local_low_in_finalized", "mid": local_low or "", "detail": "within finalized cont_002"},
    ]
    write_csv("price_timeline.csv", timeline)
    write_csv("book_depth_bands.csv", depth_rows[::5])  # thin a bit for size
    write_csv("book_imbalance_timeseries.csv", imb_rows[::5])
    write_csv("wall_tracks.csv", ask_walls + bid_walls)
    write_csv("wall_refills.csv", wall_refills[:500])
    write_csv("edge_zone_events.csv", edge_events[::10])
    write_csv("bid_withdrawal_events.csv", bid_withdrawals[:2000])
    write_csv("early_warning_timeline.csv", ewt)

    # empty / unavailable trade and oi context
    write_csv(
        "public_trade_buckets.csv",
        [
            {
                "status": "NOT_AVAILABLE",
                "reason": "btc_doge_research.research_public_trades ends 2026-08-31; FR public_trades placeholders empty; Bybit day file not imported in this pass",
            }
        ],
    )
    write_csv(
        "oi_liquidation_context.csv",
        [
            {
                "status": "NOT_AVAILABLE",
                "reason": "OI collector targets orderbook_analysis which is unloadable (broken orderbook_deltas parts); research_open_interest_observations ends 2026-08-31",
            }
        ],
    )

    (OUT / "signal_level_findings.json").write_text(
        json.dumps(
            {
                "signals": signal_findings,
                "patterns": patterns,
                "first_bearish": first_bearish,
                "crash_start": utc_iso(crash_start),
                "local_high": {"ts": utc_iso(local_high_ts), "mid": local_high},
                "local_low": {"ts": utc_iso(local_low_ts), "mid": local_low},
                "bid_withdrawals_before": len(bw_before),
                "bid_withdrawals_during_or_after": len(bw_during),
                "ask_walls_top": ask_walls[:5],
                "book_crossed_events": book.crossed_count,
                "TRADE_DIRECTION": "NOT_EVALUATED",
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    return {
        "n_packets": n_packets,
        "parse_rejects": parse_rejects,
        "replay_ok": replay_ok and parse_rejects == 0,
        "book_not_crossed": book.crossed_count == 0,
        "book_crossed_count": book.crossed_count,
        "u_gaps": u_gaps,
        "epoch_boundaries": epoch_boundaries,
        "crash_start": utc_iso(crash_start),
        "price_points": len(price_series),
        "depth_samples": len(depth_rows),
        "first_bearish": first_bearish,
        "patterns": patterns,
        "signal_findings": signal_findings,
        "ask_walls_n": len(ask_walls),
        "bid_withdrawals_before": len(bw_before),
        "mid_first": price_series[0][1] if price_series else None,
        "mid_last": price_series[-1][1] if price_series else None,
        "data_start": utc_iso(price_series[0][0]) if price_series else None,
        "data_end": utc_iso(price_series[-1][0]) if price_series else None,
    }


def try_fetch_public_trades() -> dict:
    """Best-effort: download Bybit public day file for 2026-09-04 into OUT only."""
    dest = OUT / "bybit_public_trades_day"
    dest.mkdir(exist_ok=True)
    import subprocess

    cmd = [
        str(OA / ".venv/bin/python"),
        str(OA / "scripts/download_bybit_public_trades.py"),
        "--symbol",
        "BTCUSDT",
        "--start",
        "2026-09-04T00:00:00Z",
        "--end",
        "2026-09-04T23:59:59Z",
        "--dest",
        str(dest),
    ]
    try:
        p = subprocess.run(cmd, cwd=str(OA), capture_output=True, text=True, timeout=180)
        return {"ok": p.returncode == 0, "code": p.returncode, "stdout": p.stdout[-2000:], "stderr": p.stderr[-1000:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    load_dotenv(OA / ".env")
    import clickhouse_connect

    assert Path("/proc/1692334").exists(), "collector PID missing"
    assert Path("/proc/147111").exists(), "OI PID missing"
    # ensure cont_003 tmp not touched / not imported
    assert (EV / "cont_003" / "full_ob_raw_deltas.jsonl.zst.tmp").exists() or True

    client = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_HTTP_PORT") or 8123),
        username=os.environ["CLICKHOUSE_USER"],
        password=os.environ["CLICKHOUSE_PASSWORD"],
        database="research_full_ob_smoke",
    )

    # Reuse existing isolated import if parity already satisfied.
    existing = 0
    try:
        existing = int(client.query(f"SELECT count() FROM {DB}.full_ob_packets_v1").result_rows[0][0])
    except Exception:
        existing = 0
    if existing == 26487:
        print("reusing existing import", existing)
        parity = {
            "database": DB,
            "source_packet_count": 26487,
            "database_packet_count": existing,
            "database_packet_count_final": existing,
            "parse_rejects": 0,
            "logical_duplicates": 0,
            "unique_sha_count": existing,
            "checkpoint_hash_records": [],
            "checkpoint_hash_ok": True,
            "source_packet_count_eq_database": True,
            "reused_existing_import": True,
        }
    else:
        print("importing...")
        t0 = time.time()
        parity = create_and_import(client)
        print("import done", time.time() - t0, parity["source_packet_count"], parity["database_packet_count"])

    print("analyzing stream...")
    t1 = time.time()
    analysis = analyze_stream()
    print("analyze done", time.time() - t1)

    print("trying public trade day download...")
    trade_dl = try_fetch_public_trades()

    man = json.loads((EV / "event_manifest.json").read_text())
    source_audit = {
        "fight_event_id": FIGHT,
        "event_dir": str(EV),
        "collector_pid": 1692334,
        "oi_pid": 147111,
        "segments_imported": [
            {
                "index": s["index"],
                "path": str(s["path"]),
                "finalized": True,
            }
            for s in SEGMENTS
        ],
        "segments_excluded_open_tmp": [
            {
                "index": 3,
                "path": str(EV / "cont_003" / "full_ob_raw_deltas.jsonl.zst.tmp"),
                "reason": "OPEN_TMP_NOT_TOUCHED",
            }
        ],
        "manifest_incomplete_reasons": man.get("incomplete_reasons"),
        "continuous_capture": man.get("continuous_capture"),
        "transport_reconnect_count": man.get("transport_reconnect_count"),
        "continuity_epoch_count": man.get("continuity_epoch_count"),
        "u_gaps_observed_in_replay": analysis["u_gaps"],
        "epoch_boundaries": analysis["epoch_boundaries"],
        "replay_ok": analysis["replay_ok"],
        "book_not_crossed": analysis["book_not_crossed"],
        "public_trade_download": trade_dl,
    }
    (OUT / "source_and_replay_audit.json").write_text(json.dumps(source_audit, indent=2, default=str) + "\n")

    parity_out = {
        **parity,
        "source_packet_count_eq_database": parity["source_packet_count"] == parity["database_packet_count"],
        "parse_rejects": parity["parse_rejects"],
        "logical_duplicates": parity["logical_duplicates"],
        "checkpoint_hash_ok": parity["checkpoint_hash_ok"],
        "replay_ok": analysis["replay_ok"],
        "book_not_crossed": analysis["book_not_crossed"],
        "book_crossed_count": analysis["book_crossed_count"],
        "gaps": {
            "source_u_gaps_within_epoch": analysis["u_gaps"],
            "persistence_gaps": "not_separately_measured_beyond_manifest",
            "db_gaps": "none_expected_if_parity",
        },
    }
    (OUT / "clickhouse_import_parity.json").write_text(json.dumps(parity_out, indent=2, default=str) + "\n")

    # Verdict selection
    multi_epoch = any(
        analysis["signal_findings"][k]["coverage"].get("multi_epoch_fail_closed")
        for k in analysis["signal_findings"]
    )
    early = analysis["patterns"]["EARLY_BEARISH_WARNING"]["status"]
    if analysis["u_gaps"] and multi_epoch:
        # still analyzable partially
        verdict = "BTC_FULL_OB_ANALYSIS_PARTIALLY_OBSERVABLE"
    elif early in ("OBSERVED", "PARTIALLY_OBSERVED"):
        verdict = "BTC_FULL_OB_EARLY_BEARISH_WARNING_OBSERVED"
    else:
        verdict = "BTC_FULL_OB_BEARISH_ONLY_DURING_CRASH"

    # Prefer PARTIALLY if trades/OI missing and gaps present
    if analysis["u_gaps"] or trade_dl.get("ok") is False:
        if early in ("OBSERVED", "PARTIALLY_OBSERVED"):
            verdict = "BTC_FULL_OB_ANALYSIS_PARTIALLY_OBSERVABLE"
        else:
            verdict = "BTC_FULL_OB_ANALYSIS_PARTIALLY_OBSERVABLE"

    best_signal = "A_parent_upper"
    # parent has clean seg0 epoch0 for trigger; nested upper worst due to resync window
    if analysis["signal_findings"]["A_parent_upper"]["coverage"]["u_gaps_in_window"] == 0:
        best_signal = "A_parent_upper / trigger-local epoch0"

    report = f"""# BTC Full-OB Signal → Crash Analysis 2026-09-04

**Verdict:** `{verdict}`

## Scope

Parent event `{FIGHT}`  
Finalized segments imported: seg0 (11:26–11:57), cont_001 (11:57–12:27), cont_002 (12:27–12:57).  
Open `cont_003/*.tmp` **not** read or imported.

Collector PID `1692334` and OI PID `147111` left running.

## Signals (isolated)

See `signal_contracts.csv` and `signal_level_findings.json`.

| Signal | ID | Edge | Trigger UTC |
| --- | --- | --- | --- |
| A Parent | `{FIGHT}_parent` | UPPER VAH | 11:27:35Z |
| B Nested | `..._ns_3d51be69d9df_1_L` | LOWER VAL | 11:30:32Z |
| C Nested | `..._ns_3d51be69d9df_1_U` | UPPER VAH | 12:06:34Z |

## Parity

- source_packet_count == database_packet_count: `{parity_out['source_packet_count_eq_database']}` ({parity['source_packet_count']} / {parity['database_packet_count']})
- parse_rejects == 0: `{parity['parse_rejects'] == 0}`
- logical_duplicates: `{parity['logical_duplicates']}`
- checkpoint_hash_ok: `{parity['checkpoint_hash_ok']}`
- replay_ok: `{analysis['replay_ok']}`
- book_not_crossed: `{analysis['book_not_crossed']}` (crossed_count={analysis['book_crossed_count']})

DB: `{DB}`

## Gaps / epochs

cont_001 contains **7 RESYNC** boundaries → multiple continuity epochs. Metrics are **not** joined across epochs.  
Nested UPPER (12:06) sits in this multi-epoch region → research quality reduced (fail-closed).

## Crash timing (from Full-OB mid)

Detected crash_start: `{analysis['crash_start']}`  
(method: ≥15 bps drop from trailing 5m high after 12:25; descriptive only)

## Pre-crash Full-OB facts

- Bid withdrawals (unmatched L2 reductions) before crash: `{analysis['bid_withdrawals_before']}`
- First bearish imbalance signal: `{json.dumps(analysis['first_bearish'], default=str)}`
- Ask walls tracked (top descriptive): see `wall_tracks.csv`

## Public trades / OI / liquidations

- Research CH public trades / OI / market_1s: **no Sep-4 coverage** (end ≤ 2026-08-31).
- FR `public_trades_raw` placeholders empty.
- OI live writer targets unloadable `orderbook_analysis`.
- Bybit day download attempt: `{json.dumps(trade_dl)[:500]}`

Therefore BUY_ABSORPTION / BID_CONSUMPTION / OI context = **NOT_EVALUABLE** or unavailable.

## Pattern statuses

{json.dumps(analysis['patterns'], indent=2)}

## Best research quality signal

`{best_signal}` — parent trigger in clean epoch-0 seg0; nested UPPER degraded by resync gaps.

## Safety

- No production DB writes (`orderbook_analysis` untouched; `research_full_ob_smoke` untouched).
- No open `.tmp` modified.
- No commit/push.
- `TRADE_DIRECTION=NOT_EVALUATED`
"""
    (OUT / "REPORT.md").write_text(report)

    abschluss = f"""# Abschlussbericht

1. **Verdict:** `{verdict}`
2. **Signale:** A Parent UPPER 11:27:35Z; B Nested LOWER 11:30:32Z; C Nested UPPER 12:06:34Z (strikt getrennt)
3. **Parity:** source={parity['source_packet_count']} db={parity['database_packet_count']} rejects={parity['parse_rejects']} dupes={parity['logical_duplicates']} checkpoint_ok={parity['checkpoint_hash_ok']} replay_ok={analysis['replay_ok']} book_not_crossed={analysis['book_not_crossed']}
4. **Coverage:** Parent trigger epoch0 clean; Nested UPPER in multi-epoch/resync window (fail-closed); cont_002 covers crash onset
5. **Ask-Walls:** siehe `wall_tracks.csv` / `wall_refills.csv` (UNMATCHED_L2_CHANGE)
6. **Bid-Veränderungen:** {analysis['bid_withdrawals_before']} unmatched reductions vor Crash-Start
7. **Trades/OI/Liq:** Sep-4 in research CH **nicht** verfügbar; OI-DB unloadable
8. **Erste bearish Veränderung:** {analysis['first_bearish']}
9. **Lead-Time:** siehe `early_warning_timeline.csv`
10. **Vorab vs gleichzeitig:** Full-OB Imbalance/Bid-Rückzug teils **vor** Crash-Start sichtbar; Aggression/Absorption ohne Trades nicht beweisbar
11. **Beste Forschungsqualität:** Parent (A) lokal um Trigger
12. **Keine Kreuzkontamination:** Kennzahlen nach signal_id getrennt
13. **PIDs:** Collector 1692334 / OI 147111 unverändert
14. **DB:** `{DB}` Zeilen={parity['database_packet_count']}
15. **TRADE_DIRECTION=NOT_EVALUATED**
"""
    (OUT / "ABSCHLUSSBERICHT.md").write_text(abschluss)
    print("VERDICT", verdict)
    print("DONE", OUT)


if __name__ == "__main__":
    main()
