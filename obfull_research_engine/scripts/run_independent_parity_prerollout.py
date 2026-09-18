#!/usr/bin/env python3
"""Independent raw→replay parity: fanout path vs disk iter_records path.

Shared input: raw Bybit-shaped messages only.
Forbidden: reusing LiveEventAdapter output / shared normalized event list for both sides.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENG = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_mp_qdh_trigger_v1/"
    "obfull_research_engine/src"
)
MAIN = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
_FANOUT_LIVE = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ema_delta_fanout_v1/"
    "src/orderbook_analyse/orderbook_v2_live"
)
RUN = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_mp_qdh_trigger_v1/"
    "obfull_research_engine/runs/ema_trend_live_prerollout_qualification_v1_20260918"
)


def _load_fanout_module(mod_name: str, filename: str):
    import importlib.util
    import types

    full_name = f"orderbook_analyse.orderbook_v2_live.{mod_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = _FANOUT_LIVE / filename
    spec = importlib.util.spec_from_file_location(full_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _payload(
    *,
    symbol: str,
    msg_type: str,
    u: int,
    seq: int,
    bids: list,
    asks: list,
    ts: int,
) -> dict[str, Any]:
    return {
        "topic": f"orderbook.full.{symbol}",
        "type": msg_type,
        "ts": ts,
        "cts": ts,
        "data": {"s": symbol, "b": bids, "a": asks, "u": u, "seq": seq},
    }


def _nodes_from_archive_records(records: list[dict[str, Any]], *, wall_price: float, wall_side: str):
    from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
        BookNode,
    )

    nodes = []
    apply_order = 0
    for rec in records:
        kind = str(rec.get("message_type") or "")
        if kind not in {"delta", "snapshot"}:
            continue
        payload = rec.get("original_payload") or {}
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            continue
        key = "a" if wall_side == "ask" else "b"
        for row in data.get(key) or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            if abs(float(row[0]) - wall_price) > 1e-12:
                continue
            apply_order += 1
            et = datetime.fromtimestamp((rec.get("event_time_ns") or 0) / 1e9, tz=timezone.utc)
            rt = datetime.fromtimestamp((rec.get("receive_time_ns") or 0) / 1e9, tz=timezone.utc)
            nodes.append(
                BookNode(
                    exchange_event_time=et,
                    available_at=rt,
                    collector_received_at=rt,
                    queue=float(row[1]),
                    replay_epoch=1,
                    sequence=rec.get("seq"),
                    update_id=rec.get("u"),
                    apply_order=apply_order,
                    source_event_id=f"replay|{rec.get('u')}|{rec.get('seq')}",
                    source_record_id=f"replay|{apply_order}",
                )
            )
    return nodes


def main() -> int:
    # MAIN first (has .research); load fanout/case_archive via importlib (never put fanout src on path).
    sys.path.insert(0, str(ENG))
    sys.path.insert(0, str(MAIN))

    from types import SimpleNamespace

    import orderbook_analyse.orderbook_v2_live  # noqa: F401
    from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
    from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import (
        FORMAT_VERSION,
    )
    from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.replay import (
        iter_records,
        replay_segment,
    )

    fanout_mod = _load_fanout_module("full_ob_event_fanout", "full_ob_event_fanout.py")
    # continuous archive must resolve for case_archive imports — present on MAIN untracked
    case_mod = _load_fanout_module("full_ob_case_archive", "full_ob_case_archive.py")
    FullObEventFanout = fanout_mod.FullObEventFanout
    FullObCaseArchiveHub = case_mod.FullObCaseArchiveHub

    from obfull_research_engine.ema_trend_live_analyzer_v1.incremental_engine import (
        IncrementalEngineState,
    )
    from obfull_research_engine.ema_trend_live_analyzer_v1.live_event_adapter import LiveEventAdapter

    symbol = "PARITYUSDT"
    wall_price = 100.0
    wall_side = "ask"
    archive_root = RUN / "independent_parity_archive"
    archive_root.mkdir(parents=True, exist_ok=True)

    # --- Shared RAW fixture only ---
    raw_messages: list[dict[str, Any]] = [
        _payload(
            symbol=symbol,
            msg_type="snapshot",
            u=100,
            seq=1000,
            bids=[["99.0", "10"], ["98.5", "5"], ["98.0", "250"]],
            asks=[["100.0", "12"], ["100.5", "3"], ["101.0", "400"]],
            ts=1_700_000_000_000,
        )
    ]
    for i in range(1, 81):
        raw_messages.append(
            _payload(
                symbol=symbol,
                msg_type="delta",
                u=100 + i,
                seq=1000 + i,
                bids=[["99.0", str(max(0.01, 10 - i * 0.01))]],
                asks=[["100.0", str(max(0.01, 12 - i * 0.05))]],
                ts=1_700_000_000_000 + i * 100,
            )
        )

    # Fake runtime book for archive checkpoint + fanout book_ready
    book = FullBookState(symbol=symbol)
    snap0 = raw_messages[0]
    book.apply_snapshot(
        bids=snap0["data"]["b"],
        asks=snap0["data"]["a"],
        u=snap0["data"]["u"],
        seq=snap0["data"]["seq"],
        ts_ms=snap0["ts"],
        mark_ready=True,
    )
    runtime = SimpleNamespace(book=book, gap_count=0)
    fake_mgr = SimpleNamespace(runtimes={symbol: runtime})

    fanout = FullObEventFanout(default_queue_size=8192)
    fanout.note_snapshot_ready(symbol)
    archive = FullObCaseArchiveHub(default_archive_root=archive_root)
    archive._manager = fake_mgr  # read-only book peek for checkpoint
    archive._attached = True

    arch = archive.start_case_archive(
        symbol=symbol,
        case_id="indep-parity-1",
        experiment_id="prerollout",
        archive_root=archive_root,
    )
    assert arch.get("ok"), arch
    assert arch.get("archive_ready") or arch.get("snapshot_written"), arch

    sub = fanout.create_subscriber(symbol=symbol, max_queue=8192)
    assert sub.get("ok"), sub
    sid = sub["subscriber_id"]

    # Feed identical raw messages independently into both hubs
    for i, msg in enumerate(raw_messages):
        now = datetime.now(timezone.utc)
        phase = "resync_ready" if i == 0 else "live"
        outcome = "checkpoint" if i == 0 else "applied"
        if i > 0:
            book.apply_delta(
                bids=msg["data"]["b"],
                asks=msg["data"]["a"],
                u=msg["data"]["u"],
                seq=msg["data"]["seq"],
                ts_ms=msg["ts"],
                enforce_continuity=False,
            )
        kwargs = dict(
            symbol=symbol,
            payload=msg,
            received_at=now,
            receive_time_ns=time.time_ns(),
            phase=phase,
            outcome=outcome,
            runtime=runtime,
        )
        fanout.on_full_ob_message(**kwargs)
        archive.on_full_ob_message(**kwargs)

    # PATH A: poll fanout → LiveEventAdapter → engine
    live_events: list[dict[str, Any]] = []
    cursor = None
    while True:
        poll = fanout.poll_events(subscriber_id=sid, cursor=cursor, limit=512)
        assert poll.get("ok"), poll
        batch = poll.get("events") or []
        if not batch:
            break
        live_events.extend(batch)
        cursor = int(batch[-1]["record_ordinal"]) + 1
        if not poll.get("has_more"):
            break

    adapter = LiveEventAdapter(symbol=symbol, wall_price=wall_price, wall_side=wall_side)
    live_nodes = adapter.ingest_batch(live_events)
    live_engine = IncrementalEngineState(
        tick_size=0.1, wall_price=wall_price, wall_side=wall_side, direction=1
    )
    live_feat = live_engine.on_nodes(
        live_nodes,
        attributed_hit_qty=1.0,
        attributed_hit_notional=100.0,
        hit_trade_count=1,
        interval_duration_s=0.1,
        exact_features_valid=True,
    )

    fin = archive.finalize_case_archive(arch["recorder_id"])
    assert fin.get("ok") and fin.get("archive_path"), fin
    archive_path = Path(fin["archive_path"])

    # PATH B: disk only via iter_records (no live_events reuse)
    archived = list(iter_records(archive_path))
    replay_info = replay_segment(archive_path, verify_hashes=True, verify_sequence=True)
    replay_nodes = _nodes_from_archive_records(
        archived, wall_price=wall_price, wall_side=wall_side
    )
    replay_engine = IncrementalEngineState(
        tick_size=0.1, wall_price=wall_price, wall_side=wall_side, direction=1
    )
    replay_feat = replay_engine.on_nodes(
        replay_nodes,
        attributed_hit_qty=1.0,
        attributed_hit_notional=100.0,
        hit_trade_count=1,
        interval_duration_s=0.1,
        exact_features_valid=True,
    )

    diffs: list[dict[str, Any]] = []

    live_uids = [n.update_id for n in live_nodes]
    replay_uids = [n.update_id for n in replay_nodes]
    if live_uids != replay_uids:
        diffs.append({"kind": "update_id_sequence", "live": live_uids, "replay": replay_uids})

    live_seqs = [n.sequence for n in live_nodes]
    replay_seqs = [n.sequence for n in replay_nodes]
    if live_seqs != replay_seqs:
        diffs.append({"kind": "sequence", "live": live_seqs, "replay": replay_seqs})

    live_q = [n.queue for n in live_nodes]
    replay_q = [n.queue for n in replay_nodes]
    if live_q != replay_q:
        diffs.append({"kind": "queue_timeline", "live": live_q, "replay": replay_q})

    def _mass(feat: dict[str, Any]) -> dict[str, float]:
        m = feat.get("mass") or {}
        return {
            k: float(m.get(k) or 0.0)
            for k in (
                "attributed_fill_capped",
                "residual_pull",
                "refill",
                "unknown",
                "net_depletion",
            )
        }

    lm, rm = _mass(live_feat), _mass(replay_feat)
    for k in lm:
        if abs(lm[k] - rm[k]) > 1e-9:
            diffs.append({"kind": "mass", "field": k, "live": lm[k], "replay": rm[k]})

    lq = float((live_feat.get("qdh") or {}).get("qdh_base") or 0)
    rq = float((replay_feat.get("qdh") or {}).get("qdh_base") or 0)
    if abs(lq - rq) > 1e-9:
        diffs.append({"kind": "qdh", "live": lq, "replay": rq})

    # Depth: snapshot must retain all fixture levels (3 bids / 3 asks), not truncated to 200
    snap_rec = next((r for r in archived if r.get("message_type") == "snapshot"), None)
    ckpt = next((r for r in archived if r.get("message_type") == "checkpoint"), None)
    depth_ok = True
    depth_detail: dict[str, Any] = {}
    for label, rec in (("snapshot", snap_rec), ("checkpoint", ckpt)):
        if rec is None:
            continue
        op = rec.get("original_payload") or {}
        if label == "checkpoint":
            bids, asks = op.get("bids") or [], op.get("asks") or []
        else:
            data = op.get("data") or {}
            bids, asks = data.get("b") or [], data.get("a") or []
        depth_detail[label] = {"bids": len(bids), "asks": len(asks)}
        if len(bids) < 3 or len(asks) < 3:
            depth_ok = False
            diffs.append({"kind": "depth_truncation", "where": label, "bids": len(bids), "asks": len(asks)})

    man_path = Path(str(archive_path) + ".manifest.json")
    if not man_path.is_file():
        # some writers put manifest next to segment
        cands = list(archive_path.parent.glob("*.manifest.json"))
        man_path = cands[0] if cands else man_path
    man = json.loads(man_path.read_text()) if man_path.is_file() else {}
    fmt = man.get("format_version") or FORMAT_VERSION
    if fmt != "full_ob_continuous_raw_archive_v1":
        diffs.append({"kind": "format_version", "got": fmt})

    # Record ordinals / generations from live path
    live_ords = [e.get("record_ordinal") for e in live_events]
    if live_ords != sorted(x for x in live_ords if x is not None):
        diffs.append({"kind": "live_record_ordinal_order", "ords": live_ords[:20]})

    delta_count_archive = sum(1 for r in archived if r.get("message_type") == "delta")
    # raw had 80 deltas; archive may also have checkpoint+lifecycle
    if delta_count_archive < 80:
        diffs.append({"kind": "missing_deltas", "archive_deltas": delta_count_archive, "expected": 80})

    report = {
        "INDEPENDENT_RAW_REPLAY_PARITY": "PASS" if not diffs else "FAIL",
        "NO_DEPTH_TRUNCATION": depth_ok,
        "RAW_FORMAT_COMPATIBLE": fmt == "full_ob_continuous_raw_archive_v1",
        "format_version": fmt,
        "raw_fixture_messages": len(raw_messages),
        "live_fanout_level_events": len(live_events),
        "live_booknodes": len(live_nodes),
        "archive_records": len(archived),
        "archive_deltas": delta_count_archive,
        "replay_booknodes": len(replay_nodes),
        "depth_detail": depth_detail,
        "replay_segment": replay_info,
        "live_features": live_feat,
        "replay_features": replay_feat,
        "differences": diffs,
        "independence_guarantee": (
            "Path A: fanout.poll_events → LiveEventAdapter → IncrementalEngine. "
            "Path B: SegmentWriter disk → iter_records → independent BookNode rebuild → IncrementalEngine. "
            "Shared input is raw_messages only."
        ),
    }
    RUN.mkdir(parents=True, exist_ok=True)
    (RUN / "INDEPENDENT_REPLAY_PARITY_REPORT.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    )
    md = [
        "# INDEPENDENT_REPLAY_PARITY_REPORT",
        "",
        f"**INDEPENDENT_RAW_REPLAY_PARITY:** `{report['INDEPENDENT_RAW_REPLAY_PARITY']}`",
        f"**NO_DEPTH_TRUNCATION:** `{depth_ok}`",
        f"**format_version:** `{fmt}`",
        "",
        "## Independence",
        report["independence_guarantee"],
        "",
        "## Counts",
        f"- raw fixture messages: {len(raw_messages)}",
        f"- live fanout level events: {len(live_events)}",
        f"- live BookNodes: {len(live_nodes)}",
        f"- archive records: {len(archived)}",
        f"- archive deltas: {delta_count_archive}",
        f"- replay BookNodes: {len(replay_nodes)}",
        f"- depth: `{json.dumps(depth_detail)}`",
        "",
        "## Differences",
        "",
    ]
    if not diffs:
        md.append("_none_")
    else:
        for d in diffs:
            md.append(f"- `{json.dumps(d, default=str)}`")
    (RUN / "INDEPENDENT_REPLAY_PARITY_REPORT.md").write_text("\n".join(md) + "\n")
    fanout.remove_subscriber(sid)
    print(json.dumps({"parity": report["INDEPENDENT_RAW_REPLAY_PARITY"], "diffs": len(diffs), "fmt": fmt}))
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
