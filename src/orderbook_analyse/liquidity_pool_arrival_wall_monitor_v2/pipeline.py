"""V2 pipeline: arrivals, exact walls, first-seen, clusters, migration."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.clustering import (
    ActiveCluster,
    PoolInterval,
    assign_market_clusters,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    FirstSeenClass,
    classify_first_seen,
    cluster_wall_identity,
    normalize_tick_price,
    wall_identity,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    side_levels_ranked_full,
    strongest_inside,
)
from orderbook_analyse.liquidity_pool_signal import (
    chart_lookback_start,
    chart_pool_engine,
    get_engine_function,
    parity_pair,
)
from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    pool_row_from_engine,
    run_chart_backend_lld,
)
from orderbook_analyse.ob200_v3_raw_discovery.audit import (
    is_replayable_line,
    iter_decompressed_lines,
    line_to_replay_payload,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import MutableBook

WINDOW_START = "2026-08-25T00:00:00Z"
WINDOW_END = "2026-08-27T00:00:00Z"
SAMPLE_MS = 1000
MAX_GAP_MS = 2000
MAX_OB_AGE_MS = 1000
PRE_WINDOW_S = 60
MONITOR_MAX_S = 300
FOUNDATION_PARITY_SHA = {
    "2026-08-25T21:00:00Z": "c8a2ee21da88f9d537cec2cffd7d02a659df518ab30000d69aece3ec4d4f3a16",
    "2026-08-26T04:48:00Z": "4cd0764108306ca7144eed7f72cedb1434ad1307dd0aaa77a5df264d56aa2406",
}
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRIOR_V1 = _PROJECT_ROOT / "results" / "liquidity_pool_arrival_internal_wall_monitor_v1"


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime | str) -> int:
    return int(_utc(dt).timestamp() * 1000)


def _dt_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def verify_foundation_parity() -> dict[str, Any]:
    eng = get_engine_function()
    out: dict[str, Any] = {
        "engine_module": eng.__module__,
        "identical_to_chart": eng is chart_pool_engine(),
        "by_snapshot": {},
        "parity_pass": True,
    }
    for s, expected in FOUNDATION_PARITY_SHA.items():
        as_of = _utc(s)
        pr = parity_pair(
            symbol="BTCUSDT",
            timeframe="5m",
            start=chart_lookback_start(as_of, "5m"),
            end=as_of,
        )
        ok = (
            pr["parity_pass"]
            and pr["chart_payload_sha256"] == expected
            and pr["cli_payload_sha256"] == expected
        )
        out["by_snapshot"][s] = {
            "expected": expected,
            "match": ok,
            "got": pr["chart_payload_sha256"],
        }
        if not ok:
            out["parity_pass"] = False
    return out


def load_pools(window_start: datetime, window_end: datetime) -> list[dict[str, Any]]:
    pack_start = min(chart_lookback_start(window_end, "5m"), window_start - timedelta(days=3))
    bundle = run_chart_backend_lld(
        symbol="BTCUSDT", timeframe="5m", start=pack_start, end=window_end
    )
    cfg = bundle["config"]
    return [
        pool_row_from_engine(p, cfg=cfg, as_of=window_end, market_price=None)
        for p in bundle["engine_result"].pools_all
    ]


def pool_active_at(pool: dict[str, Any], ts_ms: int) -> bool:
    if ts_ms < _ms(pool["available_at"]):
        return False
    inv = pool.get("invalidated_ts") or pool.get("invalidated_at")
    if inv and ts_ms >= _ms(inv):
        return False
    return True


def inside_pool(mid: float, pool: dict[str, Any]) -> bool:
    return float(pool["lower_edge"]) <= mid <= float(pool["upper_edge"])


def pool_arrival_id(symbol: str, pool_id: str, arrival_ms: int) -> str:
    return hashlib.sha256(f"{symbol}|{pool_id}|{arrival_ms}".encode()).hexdigest()[:16]


class MidSample:
    __slots__ = ("ts_ms", "mid", "genuine")

    def __init__(self, ts_ms: int, mid: float, genuine: bool):
        self.ts_ms = ts_ms
        self.mid = mid
        self.genuine = genuine


def iter_mids(
    raw_root: Path, *, symbol: str, start: datetime, end: datetime
):
    segments = list_closed_segments(
        raw_root,
        symbols=(symbol,),
        start=start - timedelta(hours=1),
        end=end,
        include_boundary_stubs=False,
    )
    book = MutableBook()
    gap_latched = False
    last_emit = None
    start_ms, end_ms = _ms(start), _ms(end)
    for ref in segments:
        for _line, obj in iter_decompressed_lines(ref.path):
            if not is_replayable_line(obj):
                continue
            payload = line_to_replay_payload(obj)
            data = payload.get("data") or {}
            mtype = payload.get("type")
            ts = obj.get("ts")
            if not isinstance(ts, int):
                continue
            if mtype == "snapshot":
                book.apply_snapshot(data)
                gap_latched = False
            elif mtype == "delta":
                warns = book.apply_delta(data)
                if any(str(w).startswith("seq_gap") for w in warns):
                    gap_latched = True
            else:
                continue
            if not book.is_valid or not book.bids or not book.asks:
                continue
            if ts < start_ms or ts > end_ms:
                if ts > end_ms:
                    return
                continue
            bucket = (ts // SAMPLE_MS) * SAMPLE_MS
            if last_emit is not None and bucket <= last_emit:
                continue
            last_emit = bucket
            bb = book.sorted_bids()[0][0]
            ba = book.sorted_asks()[0][0]
            if bb >= ba:
                continue
            yield MidSample(bucket, float((bb + ba) / 2), not gap_latched and book.is_valid)


def detect_pool_arrivals(
    pools: list[dict[str, Any]], mids: list[MidSample]
) -> tuple[list[dict[str, Any]], list[dict], list[dict]]:
    ask_pools = [p for p in pools if p["side"] == "ASK"]
    bid_pools = [p for p in pools if p["side"] == "BID"]
    in_ep: dict[str, bool] = {}
    episodes: list[dict[str, Any]] = []
    born: list[dict] = []
    gaps: list[dict] = []
    seen_born: set[str] = set()
    prev: MidSample | None = None

    for cur in mids:
        if not cur.genuine:
            prev = cur
            continue
        for p in ask_pools + bid_pools:
            pid = p["pool_id"]
            if pid in seen_born:
                continue
            avail = _ms(p["available_at"])
            if cur.ts_ms < avail:
                continue
            if prev is None or prev.ts_ms < avail <= cur.ts_ms:
                seen_born.add(pid)
                if inside_pool(cur.mid, p) and pool_active_at(p, cur.ts_ms):
                    born.append(
                        {
                            "pool_id": pid,
                            "side": p["side"],
                            "born_ts": _iso(_dt_ms(cur.ts_ms)),
                            "class": "BORN_INSIDE_POOL",
                        }
                    )
                    in_ep[pid] = True

        if prev is None or not prev.genuine:
            prev = cur
            continue
        gap = cur.ts_ms - prev.ts_ms
        is_gap = gap > MAX_GAP_MS

        for p in ask_pools:
            pid = p["pool_id"]
            if not pool_active_at(p, cur.ts_ms):
                in_ep[pid] = False
                continue
            lo, hi = float(p["lower_edge"]), float(p["upper_edge"])
            if in_ep.get(pid) and prev.mid < lo:
                in_ep[pid] = False
            if in_ep.get(pid):
                continue
            if prev.mid < lo <= cur.mid:
                if is_gap:
                    gaps.append({"pool_id": pid, "side": "ASK", "ts": _iso(_dt_ms(cur.ts_ms))})
                    continue
                paid = pool_arrival_id("BTCUSDT", pid, cur.ts_ms)
                episodes.append(
                    {
                        "pool_arrival_id": paid,
                        "symbol": "BTCUSDT",
                        "pool_id": pid,
                        "side": "ASK",
                        "arrival_kind": "ASK_ARRIVAL_FROM_BELOW",
                        "approach_direction": "FROM_BELOW",
                        "arrival_ts": _iso(_dt_ms(cur.ts_ms)),
                        "arrival_ts_ms": cur.ts_ms,
                        "arrival_edge": lo,
                        "mid_at_arrival": cur.mid,
                        "lower_edge": lo,
                        "upper_edge": hi,
                        "available_at": p["available_at"],
                        "invalidated_at": p.get("invalidated_ts"),
                        "origin_ts": p.get("origin_ts"),
                        "source_timeframe": p.get("source_timeframe"),
                        "strength": p.get("strength"),
                    }
                )
                in_ep[pid] = True

        for p in bid_pools:
            pid = p["pool_id"]
            if not pool_active_at(p, cur.ts_ms):
                in_ep[pid] = False
                continue
            lo, hi = float(p["lower_edge"]), float(p["upper_edge"])
            if in_ep.get(pid) and prev.mid > hi:
                in_ep[pid] = False
            if in_ep.get(pid):
                continue
            if prev.mid > hi >= cur.mid:
                if is_gap:
                    gaps.append({"pool_id": pid, "side": "BID", "ts": _iso(_dt_ms(cur.ts_ms))})
                    continue
                paid = pool_arrival_id("BTCUSDT", pid, cur.ts_ms)
                episodes.append(
                    {
                        "pool_arrival_id": paid,
                        "symbol": "BTCUSDT",
                        "pool_id": pid,
                        "side": "BID",
                        "arrival_kind": "BID_ARRIVAL_FROM_ABOVE",
                        "approach_direction": "FROM_ABOVE",
                        "arrival_ts": _iso(_dt_ms(cur.ts_ms)),
                        "arrival_ts_ms": cur.ts_ms,
                        "arrival_edge": hi,
                        "mid_at_arrival": cur.mid,
                        "lower_edge": lo,
                        "upper_edge": hi,
                        "available_at": p["available_at"],
                        "invalidated_at": p.get("invalidated_ts"),
                        "origin_ts": p.get("origin_ts"),
                        "source_timeframe": p.get("source_timeframe"),
                        "strength": p.get("strength"),
                    }
                )
                in_ep[pid] = True
        prev = cur
    return episodes, born, gaps


class BookSnap:
    __slots__ = ("ts_ms", "genuine", "bids", "asks")

    def __init__(self, ts_ms, genuine, bids, asks):
        self.ts_ms = ts_ms
        self.genuine = genuine
        self.bids = bids
        self.asks = asks


def replay_at_probes(
    raw_root: Path, symbol: str, probes: list[int]
) -> dict[int, BookSnap | None]:
    probes = sorted(set(int(p) for p in probes))
    if not probes:
        return {}
    out: dict[int, BookSnap | None] = {}
    chunk_ms = 6 * 3600 * 1000
    i = 0
    while i < len(probes):
        end_b = probes[i] + chunk_ms
        chunk = []
        while i < len(probes) and probes[i] <= end_b:
            chunk.append(probes[i])
            i += 1
        out.update(_replay_chunk(raw_root, symbol, chunk))
    return out


def _replay_chunk(raw_root: Path, symbol: str, probes: list[int]) -> dict[int, BookSnap | None]:
    start = _dt_ms(min(probes) - 3_600_000)
    end = _dt_ms(max(probes) + 1_000)
    segments = list_closed_segments(
        raw_root, symbols=(symbol,), start=start, end=end, include_boundary_stubs=False
    )
    book = MutableBook()
    gap_latched = False
    best: dict[int, BookSnap] = {}

    def capture(ts: int) -> None:
        if not book.is_valid or not book.bids or not book.asks:
            return
        bids = [(float(p), float(q)) for p, q in book.sorted_bids()[:200]]
        asks = [(float(p), float(q)) for p, q in book.sorted_asks()[:200]]
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return
        snap = BookSnap(ts, not gap_latched and book.is_valid, bids, asks)
        for p in probes:
            if ts <= p:
                prev = best.get(p)
                if prev is None or ts >= prev.ts_ms:
                    best[p] = snap

    for ref in segments:
        for _line, obj in iter_decompressed_lines(ref.path):
            if not is_replayable_line(obj):
                continue
            payload = line_to_replay_payload(obj)
            data = payload.get("data") or {}
            mtype = payload.get("type")
            ts = obj.get("ts")
            if not isinstance(ts, int):
                continue
            if ts > max(probes) + MAX_OB_AGE_MS and ts > max(probes):
                break
            if mtype == "snapshot":
                book.apply_snapshot(data)
                gap_latched = False
            elif mtype == "delta":
                warns = book.apply_delta(data)
                if any(str(w).startswith("seq_gap") for w in warns):
                    gap_latched = True
            else:
                continue
            if ts <= max(probes):
                capture(ts)
        else:
            continue
        break
    return {p: best.get(p) for p in probes}


def classify_exact_arrival(
    snap: BookSnap | None, *, side: str, lo: float, hi: float, arrival_ms: int
) -> dict[str, Any]:
    if snap is None or not snap.genuine:
        return {
            "snapshot_unavailable": True,
            "wall_class_at_arrival": "SNAPSHOT_UNAVAILABLE",
            "strongest": None,
            "inside": [],
            "ob_age_ms": None,
            "exact_ok": False,
        }
    age = arrival_ms - snap.ts_ms
    exact_ok = 0 <= age <= MAX_OB_AGE_MS
    if snap.ts_ms > arrival_ms:
        return {
            "snapshot_unavailable": True,
            "wall_class_at_arrival": "FUTURE_SNAPSHOT_REJECTED",
            "strongest": None,
            "inside": [],
            "ob_age_ms": age,
            "exact_ok": False,
        }
    levels = snap.asks if side == "ASK" else snap.bids
    ranked = side_levels_ranked_full(levels)
    st = strongest_inside(ranked, lo, hi)
    inside = [r for r in ranked if lo <= r["price"] <= hi]
    if not exact_ok:
        cls = "SNAPSHOT_STALE"
    elif st is None:
        cls = "NO_WALL"
    else:
        cls = st["significance_class"]
    return {
        "snapshot_unavailable": False,
        "snap_ts_ms": snap.ts_ms,
        "ob_age_ms": age,
        "exact_ok": exact_ok,
        "full_side_level_count": len(ranked),
        "inside_count": len(inside),
        "strongest": st,
        "inside": inside,
        "wall_class_at_arrival": cls if exact_ok else "SNAPSHOT_STALE",
        "wall_present_at_arrival": st is not None and exact_ok,
    }


def first_seen_for_arrival(
    *,
    ep: dict[str, Any],
    pre_snap: BookSnap | None,
    arr_class: dict[str, Any],
    post_snaps: list[BookSnap],
    tick: float,
) -> list[dict[str, Any]]:
    """Build wall first-seen rows for one pool arrival."""
    side = ep["side"]
    lo, hi = float(ep["lower_edge"]), float(ep["upper_edge"])
    arrival_ms = int(ep["arrival_ts_ms"])
    symbol = ep["symbol"]
    pool_id = ep["pool_id"]

    def inside_keys(snap: BookSnap | None) -> dict[float, dict]:
        if snap is None or not snap.genuine:
            return {}
        levels = snap.asks if side == "ASK" else snap.bids
        ranked = side_levels_ranked_full(levels)
        out = {}
        for r in ranked:
            if lo <= r["price"] <= hi:
                tp = normalize_tick_price(r["price"], tick)
                out[tp] = r
        return out

    pre = inside_keys(pre_snap)
    arr = {normalize_tick_price(r["price"], tick): r for r in arr_class.get("inside") or []}
    post_union: dict[float, dict] = {}
    first_post_ms: dict[float, int] = {}
    for snap in post_snaps:
        if snap.ts_ms <= arrival_ms:
            continue
        keys = inside_keys(snap)
        for tp, r in keys.items():
            if tp not in post_union:
                post_union[tp] = r
                first_post_ms[tp] = snap.ts_ms

    all_ticks = set(pre) | set(arr) | set(post_union)
    rows = []
    for tp in sorted(all_ticks):
        present_pre = tp in pre
        present_arr = tp in arr
        present_post = tp in post_union and tp not in pre and tp not in arr
        if present_pre:
            first_ms = arrival_ms - SAMPLE_MS  # approximate; pre probe
        elif present_arr:
            first_ms = arr_class.get("snap_ts_ms") or arrival_ms
        elif tp in first_post_ms:
            first_ms = first_post_ms[tp]
        else:
            first_ms = None
        fs = classify_first_seen(
            first_seen_ts_ms=first_ms,
            arrival_ts_ms=arrival_ms,
            present_in_pre=present_pre,
            present_at_exact_arrival=present_arr,
            present_strictly_after=present_post,
        )
        # Hard: never AFTER if present at arrival
        if present_arr and fs == FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL:
            fs = FirstSeenClass.FIRST_SEEN_AT_ARRIVAL
        src = arr.get(tp) or pre.get(tp) or post_union.get(tp)
        rows.append(
            {
                "pool_arrival_id": ep["pool_arrival_id"],
                "pool_id": pool_id,
                "side": side,
                "price": src["price"] if src else tp,
                "tick_price": tp,
                "pool_wall_identity": wall_identity(
                    symbol=symbol, pool_id=pool_id, side=side, tick_price=tp
                ),
                "cluster_wall_identity": cluster_wall_identity(
                    symbol=symbol, side=side, tick_price=tp
                ),
                "first_seen_class": fs.value,
                "first_seen_ts": _iso(_dt_ms(first_ms)) if first_ms is not None else None,
                "present_pre": present_pre,
                "present_exact_arrival": present_arr,
                "appeared_strictly_after": fs == FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL,
                "class_at_exact_arrival": (arr[tp]["significance_class"] if present_arr else None),
                "full_side_rank_at_exact": (arr[tp]["full_side_rank"] if present_arr else None),
                "notional_at_exact": (arr[tp]["notional"] if present_arr else None),
                "can_set_major_at_arrival": bool(
                    present_arr and arr[tp]["significance_class"] == "MAJOR"
                ),
            }
        )
    return rows


def build_migration(v2_rows: list[dict], first_seen_by_arrival: dict[str, list]) -> list[dict]:
    v1_path = PRIOR_V1 / "pool_arrival_episodes.csv"
    if not v1_path.exists():
        return []
    v1 = list(csv.DictReader(v1_path.open(encoding="utf-8")))
    v1_by_key = {(r["pool_id"], r["arrival_ts"]): r for r in v1}
    out = []
    for r in v2_rows:
        key = (r["pool_id"], r["arrival_ts"])
        old = v1_by_key.get(key)
        v2_cls = r.get("wall_class_at_arrival")
        v1_cls = old.get("strongest_wall_class_at_arrival") if old else None
        old_after = old.get("wall_appeared_after") if old else None
        fs_rows = first_seen_by_arrival.get(r["pool_arrival_id"], [])
        # dominant new first-seen for arrival strongest
        new_fs = None
        if r.get("strongest_wall_tick") is not None:
            for f in fs_rows:
                if abs(float(f["tick_price"]) - float(r["strongest_wall_tick"])) < 1e-9:
                    new_fs = f["first_seen_class"]
                    break
        reasons = []
        if old is None:
            reasons.append("OTHER")
        else:
            if v1_cls == v2_cls:
                reasons.append("UNCHANGED")
            elif v1_cls == "MAJOR" and v2_cls == "NO_WALL":
                reasons.append("MAJOR_TO_NO_WALL")
            elif v1_cls != v2_cls:
                reasons.append("OTHER")
            if str(old_after).lower() in ("true", "1") and new_fs == "FIRST_SEEN_AT_ARRIVAL":
                reasons.append("FIRST_SEEN_AT_ARRIVAL_NOT_AFTER")
            if new_fs == "APPEARED_STRICTLY_AFTER_ARRIVAL":
                reasons.append("STRICTLY_POST_ARRIVAL")
            if int(r.get("cluster_member_pool_count") or 1) > 1:
                reasons.append("MERGED_INTO_MARKET_CLUSTER")
        out.append(
            {
                "v1_pool_arrival_id": old.get("arrival_episode_id") if old else None,
                "v2_pool_arrival_id": r["pool_arrival_id"],
                "market_arrival_cluster_id": r.get("market_arrival_cluster_id"),
                "v1_reported_wall_class": v1_cls,
                "v2_exact_arrival_wall_class": v2_cls,
                "class_changed": v1_cls != v2_cls,
                "old_appeared_after": old_after,
                "new_first_seen_class": new_fs,
                "member_pool_count": r.get("cluster_member_pool_count"),
                "merge_count": max(0, int(r.get("cluster_pool_arrival_count") or 1) - 1),
                "migration_reason": "|".join(reasons) if reasons else "OTHER",
            }
        )
    return out
