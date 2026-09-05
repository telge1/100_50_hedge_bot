#!/usr/bin/env python3
"""LIQUIDITY_POOL_ARRIVAL_INTERNAL_WALL_MONITOR_V1

Pools via liquidity_pool_signal (chart engine).
Walls via existing MutableBook Raw-OB200 replay.
No trading / public trades / outcomes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.util import (
    median,
    notional,
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

DEFAULT_RAW_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
)
FOUNDATION_PARITY_SHA = {
    "2026-08-25T21:00:00Z": "c8a2ee21da88f9d537cec2cffd7d02a659df518ab30000d69aece3ec4d4f3a16",
    "2026-08-26T04:48:00Z": "4cd0764108306ca7144eed7f72cedb1434ad1307dd0aaa77a5df264d56aa2406",
}
WINDOW_START = "2026-08-25T00:00:00Z"
WINDOW_END = "2026-08-27T00:00:00Z"
SAMPLE_MS = 1000
MAX_GAP_MS = 2000
MONITOR_MAX_S = 300
PRE_WINDOW_S = 60
KNOWN_0230 = "2026-08-26T02:30:00Z"
FORBIDDEN_CAUSE = (
    "CONSUMED",
    "CANCELLED",
    "ABSORBED",
    "DEFENDED",
    "SPOOFED",
)


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


def _ms(dt: datetime) -> int:
    return int(_utc(dt).timestamp() * 1000)


def _dt_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def bps_distance(a: float, b: float) -> float:
    if b <= 0:
        return float("inf")
    return abs(a - b) / b * 10000.0


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
    assert eng is chart_pool_engine()
    out: dict[str, Any] = {
        "engine_module": eng.__module__,
        "engine_name": eng.__name__,
        "identical_to_chart": True,
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
            "chart": pr["chart_payload_sha256"],
            "cli": pr["cli_payload_sha256"],
            "match": ok,
        }
        if not ok:
            out["parity_pass"] = False
    return out


def significance_class(rank: int, percentile: float) -> str:
    if rank <= 5 or percentile >= 0.95:
        return "MAJOR"
    if rank <= 20 or percentile >= 0.80:
        return "MODERATE"
    return "MINOR"


def side_levels_ranked(levels: list[tuple[float, float]]) -> list[dict[str, Any]]:
    rows = []
    for price, qty in levels:
        if qty <= 0:
            continue
        rows.append({"price": price, "qty": qty, "notional": notional(price, qty)})
    rows.sort(key=lambda r: r["notional"], reverse=True)
    n = len(rows)
    notionals = [r["notional"] for r in rows]
    med = median(notionals) or 0.0
    for i, r in enumerate(rows):
        rank = i + 1
        pct = sum(1 for x in notionals if x <= r["notional"]) / n if n else 0.0
        r["rank"] = rank
        r["percentile"] = pct
        r["ratio_to_median"] = (r["notional"] / med) if med > 0 else None
        r["significance_class"] = significance_class(rank, pct)
    return rows


def load_causal_pools(window_start: datetime, window_end: datetime) -> list[dict[str, Any]]:
    pack_start = min(chart_lookback_start(window_end, "5m"), window_start - timedelta(days=3))
    bundle = run_chart_backend_lld(
        symbol="BTCUSDT",
        timeframe="5m",
        start=pack_start,
        end=window_end,
    )
    cfg = bundle["config"]
    return [
        pool_row_from_engine(p, cfg=cfg, as_of=window_end, market_price=None)
        for p in bundle["engine_result"].pools_all
    ]


def pool_active_at(pool: dict[str, Any], ts_ms: int) -> bool:
    if ts_ms < _ms(_utc(pool["available_at"])):
        return False
    inv = pool.get("invalidated_ts") or pool.get("invalidated_at")
    if inv and ts_ms >= _ms(_utc(inv)):
        return False
    return True


def inside_pool(mid: float, pool: dict[str, Any]) -> bool:
    return float(pool["lower_edge"]) <= mid <= float(pool["upper_edge"])


def episode_id(symbol: str, pool_id: str, arrival_ms: int) -> str:
    return hashlib.sha256(f"{symbol}|{pool_id}|{arrival_ms}".encode()).hexdigest()[:16]


class BookState:
    __slots__ = ("ts_ms", "mid", "bb", "ba", "genuine", "bids", "asks")

    def __init__(self, ts_ms, mid, bb, ba, genuine, bids, asks):
        self.ts_ms = ts_ms
        self.mid = mid
        self.bb = bb
        self.ba = ba
        self.genuine = genuine
        self.bids = bids
        self.asks = asks


def iter_ob_samples(
    raw_root: Path,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    sample_ms: int = SAMPLE_MS,
    with_levels: bool = False,
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
    start_ms = _ms(start)
    end_ms = _ms(end)
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
            if ts < start_ms - PRE_WINDOW_S * 1000:
                continue
            if ts > end_ms + 1000:
                return
            bucket = (ts // sample_ms) * sample_ms
            if last_emit is not None and bucket <= last_emit:
                continue
            last_emit = bucket
            bids_s = book.sorted_bids()
            asks_s = book.sorted_asks()
            bb, _ = bids_s[0]
            ba, _ = asks_s[0]
            if bb >= ba:
                continue
            mid = float((bb + ba) / 2)
            genuine = not gap_latched and book.is_valid
            if with_levels:
                bids = [(float(p), float(q)) for p, q in bids_s[:200]]
                asks = [(float(p), float(q)) for p, q in asks_s[:200]]
            else:
                bids, asks = [], []
            yield BookState(bucket, mid, float(bb), float(ba), genuine, bids, asks)


def detect_arrivals(
    pools: list[dict[str, Any]], mids: list[BookState]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ask_pools = [p for p in pools if p["side"] == "ASK"]
    bid_pools = [p for p in pools if p["side"] == "BID"]
    in_ep: dict[str, bool] = {}
    episodes: list[dict[str, Any]] = []
    born: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    seen_born: set[str] = set()
    prev: BookState | None = None

    for cur in mids:
        if not cur.genuine:
            prev = cur
            continue

        for p in ask_pools + bid_pools:
            pid = p["pool_id"]
            if pid in seen_born:
                continue
            avail = _ms(_utc(p["available_at"]))
            if cur.ts_ms < avail:
                continue
            if prev is None or prev.ts_ms < avail <= cur.ts_ms:
                seen_born.add(pid)
                if inside_pool(cur.mid, p) and pool_active_at(p, cur.ts_ms):
                    born.append(
                        {
                            "pool_id": pid,
                            "side": p["side"],
                            "available_at": p["available_at"],
                            "born_ts": _iso(_dt_ms(cur.ts_ms)),
                            "mid": cur.mid,
                            "lower_edge": p["lower_edge"],
                            "upper_edge": p["upper_edge"],
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
            lo = float(p["lower_edge"])
            hi = float(p["upper_edge"])
            if in_ep.get(pid) and prev.mid < lo:
                in_ep[pid] = False
            if in_ep.get(pid):
                continue
            if prev.mid < lo <= cur.mid:
                if is_gap:
                    gaps.append(
                        {
                            "pool_id": pid,
                            "side": "ASK",
                            "ts": _iso(_dt_ms(cur.ts_ms)),
                            "prev_mid": prev.mid,
                            "mid": cur.mid,
                            "gap_ms": gap,
                            "class": "GAP_CROSS",
                        }
                    )
                    continue
                episodes.append(
                    {
                        "arrival_episode_id": episode_id("BTCUSDT", pid, cur.ts_ms),
                        "symbol": "BTCUSDT",
                        "pool_id": pid,
                        "side": "ASK",
                        "arrival_kind": "ASK_ARRIVAL_FROM_BELOW",
                        "arrival_ts": _iso(_dt_ms(cur.ts_ms)),
                        "arrival_ts_ms": cur.ts_ms,
                        "arrival_edge": lo,
                        "approach_direction": "FROM_BELOW",
                        "mid_at_arrival": cur.mid,
                        "lower_edge": lo,
                        "upper_edge": hi,
                        "strength": p.get("strength"),
                        "available_at": p["available_at"],
                        "origin_ts": p.get("origin_ts") or p.get("source_timestamp"),
                        "invalidated_at": p.get("invalidated_ts"),
                        "source_timeframe": p.get("source_timeframe"),
                    }
                )
                in_ep[pid] = True

        for p in bid_pools:
            pid = p["pool_id"]
            if not pool_active_at(p, cur.ts_ms):
                in_ep[pid] = False
                continue
            lo = float(p["lower_edge"])
            hi = float(p["upper_edge"])
            if in_ep.get(pid) and prev.mid > hi:
                in_ep[pid] = False
            if in_ep.get(pid):
                continue
            if prev.mid > hi >= cur.mid:
                if is_gap:
                    gaps.append(
                        {
                            "pool_id": pid,
                            "side": "BID",
                            "ts": _iso(_dt_ms(cur.ts_ms)),
                            "prev_mid": prev.mid,
                            "mid": cur.mid,
                            "gap_ms": gap,
                            "class": "GAP_CROSS",
                        }
                    )
                    continue
                episodes.append(
                    {
                        "arrival_episode_id": episode_id("BTCUSDT", pid, cur.ts_ms),
                        "symbol": "BTCUSDT",
                        "pool_id": pid,
                        "side": "BID",
                        "arrival_kind": "BID_ARRIVAL_FROM_ABOVE",
                        "arrival_ts": _iso(_dt_ms(cur.ts_ms)),
                        "arrival_ts_ms": cur.ts_ms,
                        "arrival_edge": hi,
                        "approach_direction": "FROM_ABOVE",
                        "mid_at_arrival": cur.mid,
                        "lower_edge": lo,
                        "upper_edge": hi,
                        "strength": p.get("strength"),
                        "available_at": p["available_at"],
                        "origin_ts": p.get("origin_ts") or p.get("source_timestamp"),
                        "invalidated_at": p.get("invalidated_ts"),
                        "source_timeframe": p.get("source_timeframe"),
                    }
                )
                in_ep[pid] = True

        prev = cur
    return episodes, born, gaps


def _tick_key(side: str, price: float, tick: float) -> str:
    return f"{side}:{round(price / tick) * tick:.10g}"


def _new_mon_state(ep: dict[str, Any]) -> dict[str, Any]:
    arrival_ms = int(ep["arrival_ts_ms"])
    return {
        "ep": ep,
        "arrival_ms": arrival_ms,
        "lo": float(ep["lower_edge"]),
        "hi": float(ep["upper_edge"]),
        "side": ep["side"],
        "edge": float(ep["arrival_edge"]),
        "inv_ms": _ms(_utc(ep["invalidated_at"])) if ep.get("invalidated_at") else None,
        "ended": False,
        "monitoring_end_ms": arrival_ms + MONITOR_MAX_S * 1000,
        "end_reason": "MAX_300S",
        "tracks": {},
        "per_sec": [],
        "wall_snapshots": [],
        "at_arrival": None,
        "post_n": 0,
        "pre_seen_keys": set(),
        "post_new_majmod": False,
        "pre_majmod": False,
    }


def _finalize_mon(st: dict[str, Any], tick: float, write_snapshots: bool) -> dict[str, Any]:
    ep = st["ep"]
    track_rows = []
    post_n = max(1, st["post_n"])
    for tr in st["tracks"].values():
        notions = tr.pop("notionals")
        vis_frac = tr["visible_seconds"] / (PRE_WINDOW_S + post_n)
        size_change = None
        if tr["arrival_notional"] is not None:
            size_change = tr["final_notional"] - tr["arrival_notional"]
        elif notions:
            size_change = notions[-1] - notions[0]
        track_rows.append(
            {
                **tr,
                "visibility_fraction": vis_frac,
                "median_notional": median(notions),
                "size_change": size_change,
                "disappeared_ts": tr["last_seen_ts"] if tr["visible_seconds"] < post_n else None,
            }
        )

    at = st["at_arrival"]
    states: list[str] = []
    if not at or at.get("strongest_price") is None:
        states.append("NO_SAME_SIDE_WALL")
    else:
        if at.get("strongest_class") == "MAJOR":
            states.append("MAJOR_PRESENT_AT_ARRIVAL")
        elif at.get("strongest_class") == "MODERATE":
            states.append("MODERATE_PRESENT_AT_ARRIVAL")
        if (at.get("n_major") or 0) >= 2:
            states.append("MULTIPLE_MAJOR_WALLS")

    before_maj = st["pre_majmod"]
    after_maj = st["post_new_majmod"]
    if after_maj and not before_maj:
        states.append("WALL_APPEARED_AFTER_ARRIVAL")

    strongest_track = None
    if at and at.get("strongest_price") is not None:
        strongest_track = next(
            (t for t in track_rows if abs(t["price"] - at["strongest_price"]) < tick * 0.6),
            None,
        )
    if strongest_track:
        vf = strongest_track["visibility_fraction"]
        if vf >= 0.8:
            states.append("WALL_PERSISTENT")
        elif vf >= 0.2:
            states.append("WALL_INTERMITTENT")
        if strongest_track.get("size_change") is not None:
            if strongest_track["size_change"] > 0:
                states.append("WALL_SIZE_INCREASED")
            elif strongest_track["size_change"] < 0:
                states.append("WALL_SIZE_DECREASED")
                states.append("WALL_SIZE_DECREASED_CAUSE_UNKNOWN")
        if strongest_track.get("disappeared_ts"):
            states.append("WALL_DISAPPEARED")
            states.append("WALL_DISAPPEARED_CAUSE_UNKNOWN")

    prices = [x["strongest_price"] for x in st["per_sec"] if x.get("strongest_price") is not None]
    wall_switched = len({round(p / tick) for p in prices}) > 1 if prices else False
    if wall_switched:
        states.append("STRONGEST_WALL_PRICE_CHANGED")

    has_major = (at or {}).get("strongest_class") == "MAJOR" or any(
        t.get("max_class") == "MAJOR" for t in track_rows
    )
    has_moderate = (at or {}).get("strongest_class") == "MODERATE" or any(
        t.get("max_class") == "MODERATE" for t in track_rows
    )

    return {
        "monitoring_start_ts": _iso(_dt_ms(st["arrival_ms"] - PRE_WINDOW_S * 1000)),
        "arrival_ts": ep["arrival_ts"],
        "monitoring_end_ts": _iso(_dt_ms(st["monitoring_end_ms"])),
        "end_reason": st["end_reason"],
        "states": states,
        "at_arrival": at,
        "wall_snapshots": st["wall_snapshots"] if write_snapshots else [],
        "wall_tracks": track_rows,
        "per_sec": st["per_sec"],
        "wall_switched": wall_switched,
        "before_major_or_mod": before_maj,
        "after_major_or_mod": after_maj,
        "has_major": has_major,
        "has_moderate": has_moderate,
    }


def batch_monitor_episodes(
    episodes: list[dict[str, Any]],
    raw_root: Path,
    *,
    snapshot_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Single OB200 pass covering all episode monitoring windows."""
    if not episodes:
        return {}
    snapshot_ids = snapshot_ids or set()
    tick = tick_size("BTCUSDT")
    states = {e["arrival_episode_id"]: _new_mon_state(e) for e in episodes}
    overall_start = min(_dt_ms(s["arrival_ms"] - PRE_WINDOW_S * 1000) for s in states.values())
    overall_end = max(_dt_ms(s["arrival_ms"] + MONITOR_MAX_S * 1000) for s in states.values())

    for sample in iter_ob_samples(
        raw_root,
        symbol="BTCUSDT",
        start=overall_start,
        end=overall_end,
        with_levels=True,
    ):
        if not sample.genuine:
            continue
        for eid, st in states.items():
            if st["ended"]:
                continue
            arrival_ms = st["arrival_ms"]
            if sample.ts_ms < arrival_ms - PRE_WINDOW_S * 1000:
                continue
            if sample.ts_ms > arrival_ms + MONITOR_MAX_S * 1000:
                st["ended"] = True
                continue

            lo, hi, side, edge = st["lo"], st["hi"], st["side"], st["edge"]

            if sample.ts_ms >= arrival_ms:
                if st["inv_ms"] is not None and sample.ts_ms >= st["inv_ms"]:
                    st["monitoring_end_ms"] = sample.ts_ms
                    st["end_reason"] = "POOL_INVALIDATED"
                    st["ended"] = True
                    continue
                if side == "ASK" and sample.mid < lo:
                    st["monitoring_end_ms"] = sample.ts_ms
                    st["end_reason"] = "EXITED_ENTRY_SIDE"
                    st["ended"] = True
                    continue
                if side == "BID" and sample.mid > hi:
                    st["monitoring_end_ms"] = sample.ts_ms
                    st["end_reason"] = "EXITED_ENTRY_SIDE"
                    st["ended"] = True
                    continue
                if side == "ASK" and sample.mid > hi:
                    st["monitoring_end_ms"] = sample.ts_ms
                    st["end_reason"] = "EXITED_OPPOSITE_SIDE"
                    st["ended"] = True
                    continue
                if side == "BID" and sample.mid < lo:
                    st["monitoring_end_ms"] = sample.ts_ms
                    st["end_reason"] = "EXITED_OPPOSITE_SIDE"
                    st["ended"] = True
                    continue

            levels = sample.asks if side == "ASK" else sample.bids
            ranked_full = side_levels_ranked(levels)
            inside = [r for r in ranked_full if lo <= r["price"] <= hi]
            n_major = sum(1 for r in inside if r["significance_class"] == "MAJOR")
            n_mod = sum(1 for r in inside if r["significance_class"] == "MODERATE")
            total_n = sum(r["notional"] for r in inside)
            strongest = max(inside, key=lambda r: r["notional"]) if inside else None

            if sample.ts_ms >= arrival_ms:
                row = {
                    "ts": _iso(_dt_ms(sample.ts_ms)),
                    "ts_ms": sample.ts_ms,
                    "strongest_price": strongest["price"] if strongest else None,
                    "strongest_notional": strongest["notional"] if strongest else None,
                    "strongest_rank": strongest["rank"] if strongest else None,
                    "strongest_class": strongest["significance_class"] if strongest else None,
                    "n_major": n_major,
                    "n_moderate": n_mod,
                    "total_inside_notional": total_n,
                    "mid": sample.mid,
                }
                st["per_sec"].append(row)
                st["post_n"] += 1
                if abs(sample.ts_ms - arrival_ms) < SAMPLE_MS:
                    st["at_arrival"] = row

            for r in inside:
                key = _tick_key(side, r["price"], tick)
                rel = (sample.ts_ms - arrival_ms) / 1000.0
                cls = r["significance_class"]
                if sample.ts_ms < arrival_ms:
                    st["pre_seen_keys"].add(key)
                    if cls in ("MAJOR", "MODERATE"):
                        st["pre_majmod"] = True
                else:
                    if key not in st["pre_seen_keys"] and cls in ("MAJOR", "MODERATE"):
                        st["post_new_majmod"] = True

                if eid in snapshot_ids and sample.ts_ms >= arrival_ms:
                    st["wall_snapshots"].append(
                        {
                            "arrival_episode_id": eid,
                            "timestamp": _iso(_dt_ms(sample.ts_ms)),
                            "side": side,
                            "price": r["price"],
                            "quantity": r["qty"],
                            "notional": r["notional"],
                            "distance_to_arrival_edge_bps": bps_distance(r["price"], edge),
                            "distance_to_current_mid_bps": bps_distance(r["price"], sample.mid),
                            "side_rank": r["rank"],
                            "side_percentile": r["percentile"],
                            "ratio_to_side_median": r["ratio_to_median"],
                            "significance_class": cls,
                            "inside_pool": True,
                        }
                    )

                tr = st["tracks"].get(key)
                if tr is None:
                    st["tracks"][key] = {
                        "arrival_episode_id": eid,
                        "pool_id": ep["pool_id"] if (ep := st["ep"]) else None,
                        "side": side,
                        "price": r["price"],
                        "first_seen_ts": _iso(_dt_ms(sample.ts_ms)),
                        "first_seen_relative_to_arrival_s": rel,
                        "last_seen_ts": _iso(_dt_ms(sample.ts_ms)),
                        "visible_seconds": 1,
                        "initial_notional": r["notional"],
                        "arrival_notional": r["notional"] if abs(rel) < 0.5 else None,
                        "max_notional": r["notional"],
                        "notionals": [r["notional"]],
                        "final_notional": r["notional"],
                        "appeared_before_or_after_arrival": (
                            "BEFORE" if sample.ts_ms < arrival_ms else "AFTER"
                        ),
                        "price_level_stable": True,
                        "max_class": cls,
                    }
                else:
                    tr["last_seen_ts"] = _iso(_dt_ms(sample.ts_ms))
                    tr["visible_seconds"] += 1
                    tr["max_notional"] = max(tr["max_notional"], r["notional"])
                    tr["notionals"].append(r["notional"])
                    tr["final_notional"] = r["notional"]
                    if abs(rel) < 0.5 and tr["arrival_notional"] is None:
                        tr["arrival_notional"] = r["notional"]
                    if cls == "MAJOR":
                        tr["max_class"] = "MAJOR"
                    elif cls == "MODERATE" and tr["max_class"] != "MAJOR":
                        tr["max_class"] = "MODERATE"

    return {
        eid: _finalize_mon(st, tick, write_snapshots=(eid in snapshot_ids))
        for eid, st in states.items()
    }


def select_primary(episodes: list[dict], monitors: dict[str, dict]) -> list[dict]:
    asks = sorted([e for e in episodes if e["side"] == "ASK"], key=lambda e: e["arrival_ts_ms"])
    bids = sorted([e for e in episodes if e["side"] == "BID"], key=lambda e: e["arrival_ts_ms"])

    def pick(side_eps: list[dict], n: int) -> list[dict]:
        chosen: list[dict] = []
        used_pools: set[str] = set()
        with_wall = [
            e
            for e in side_eps
            if monitors[e["arrival_episode_id"]].get("has_major")
            or monitors[e["arrival_episode_id"]].get("has_moderate")
        ]
        without = [
            e
            for e in side_eps
            if not (
                monitors[e["arrival_episode_id"]].get("has_major")
                or monitors[e["arrival_episode_id"]].get("has_moderate")
            )
        ]
        for bucket in (with_wall, without, side_eps):
            for e in bucket:
                if len(chosen) >= n:
                    break
                if e["pool_id"] in used_pools:
                    continue
                if any(c["arrival_episode_id"] == e["arrival_episode_id"] for c in chosen):
                    continue
                chosen.append(e)
                used_pools.add(e["pool_id"])
            if len(chosen) >= n:
                break
        return chosen[:n]

    return pick(asks, 3) + pick(bids, 3)


def write_known_0230(
    out: Path,
    episodes: list[dict],
    born: list[dict],
    monitors: dict[str, dict],
    raw_root: Path,
) -> None:
    known_ms = _ms(_utc(KNOWN_0230))
    known_eps = [
        e
        for e in episodes
        if abs(e["arrival_ts_ms"] - known_ms) <= 30 * 60 * 1000 and e["side"] == "ASK"
    ]
    prefer = [e for e in known_eps if "1787684400" in e["pool_id"]]
    known_ep = (prefer or known_eps or [None])[0]
    lines = ["# Known case ~2026-08-26T02:30:00Z", ""]
    if known_ep is None:
        born_hit = [b for b in born if "1787684400" in b["pool_id"]]
        lines.append("## Classification")
        if born_hit:
            lines.append(
                "Pool matching prior audit id appears as **BORN_INSIDE_POOL** "
                "(not forced into primary six)."
            )
            lines.append("```json")
            lines.append(json.dumps(born_hit[0], indent=2))
            lines.append("```")
        else:
            lines.append("No ASK arrival within ±30m of 02:30 for known pool.")
            near = sorted(
                [e for e in episodes if e["side"] == "ASK"],
                key=lambda e: abs(e["arrival_ts_ms"] - known_ms),
            )[:5]
            lines.append("Nearest ASK arrivals:")
            for e in near:
                lines.append(
                    f"- {e['arrival_ts']} `{e['pool_id']}` edge={e['arrival_edge']}"
                )
        # still dump born for that hour if any
        hour_born = [
            b
            for b in born
            if b["side"] == "ASK"
            and abs(_ms(_utc(b["born_ts"])) - known_ms) <= 2 * 3600 * 1000
        ]
        if hour_born:
            lines.append("")
            lines.append("### Nearby BORN_INSIDE ASK")
            for b in hour_born[:10]:
                lines.append(
                    f"- `{b['born_ts']}` `{b['pool_id']}` mid={b['mid']} "
                    f"[{b['lower_edge']}, {b['upper_edge']}]"
                )
    else:
        mon = monitors.get(known_ep["arrival_episode_id"])
        if mon is None or not mon.get("per_sec"):
            mon = batch_monitor_episodes(
                [known_ep], raw_root, snapshot_ids={known_ep["arrival_episode_id"]}
            )[known_ep["arrival_episode_id"]]
        lines += [
            f"- pool_id: `{known_ep['pool_id']}`",
            f"- bounds: [{known_ep['lower_edge']}, {known_ep['upper_edge']}]",
            f"- first arrival_ts: `{known_ep['arrival_ts']}`",
            f"- available_at: `{known_ep['available_at']}`",
            f"- available before arrival: `{known_ep['available_at'] <= known_ep['arrival_ts']}`",
            f"- arrival_edge: `{known_ep['arrival_edge']}` (expect ~79148)",
            f"- mid_at_arrival: `{known_ep['mid_at_arrival']}`",
            f"- at_arrival strongest: `{mon.get('at_arrival')}`",
            f"- states: `{mon.get('states')}`",
            f"- end: `{mon.get('monitoring_end_ts')}` ({mon.get('end_reason')})",
            f"- wall_switched: `{mon.get('wall_switched')}`",
            "",
            "### Strongest wall timeline (every 5s post-arrival)",
        ]
        for row in mon.get("per_sec", [])[::5][:70]:
            lines.append(
                f"- {row['ts']}: price={row['strongest_price']} "
                f"notional={row['strongest_notional']} class={row['strongest_class']}"
            )
        lines += [
            "",
            "Prior INTERMITTENT reflects visibility across samples, not a cause claim.",
            "No consumption/cancellation claim (`WALL_*_CAUSE_UNKNOWN` only).",
        ]
    (out / "known_0230_case.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        default=str(OA_ROOT / "results" / "liquidity_pool_arrival_internal_wall_monitor_v1"),
    )
    ap.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
    args = ap.parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw_root = Path(args.raw_root)

    print("Foundation parity...", flush=True)
    foundation = verify_foundation_parity()
    (out / "pool_foundation_verification.json").write_text(
        json.dumps(foundation, indent=2), encoding="utf-8"
    )
    if not foundation["parity_pass"]:
        print("POOL_FOUNDATION_PARITY_FAILED", flush=True)
        (out / "run_manifest.json").write_text(
            json.dumps({"verdict": "POOL_FOUNDATION_PARITY_FAILED"}, indent=2),
            encoding="utf-8",
        )
        return 3

    ws, we = _utc(WINDOW_START), _utc(WINDOW_END)
    print("Loading causal pools...", flush=True)
    pools = load_causal_pools(ws, we)
    n_ask = sum(1 for p in pools if p["side"] == "ASK")
    n_bid = sum(1 for p in pools if p["side"] == "BID")
    print(f"pools_all ASK={n_ask} BID={n_bid}", flush=True)

    print("Streaming OB200 mids...", flush=True)
    mids = [
        s
        for s in iter_ob_samples(raw_root, symbol="BTCUSDT", start=ws, end=we, with_levels=False)
        if s.genuine and _ms(ws) <= s.ts_ms <= _ms(we)
    ]
    print(f"genuine mid samples: {len(mids)}", flush=True)
    if len(mids) < 100:
        verdict = "LIQUIDITY_POOL_ARRIVAL_INTERNAL_WALL_MONITOR_V1_BLOCKED_COVERAGE"
        (out / "run_manifest.json").write_text(
            json.dumps({"verdict": verdict}, indent=2), encoding="utf-8"
        )
        return 4

    episodes, born, gaps = detect_arrivals(pools, mids)
    print(
        f"arrivals={len(episodes)} born_inside={len(born)} gap_cross={len(gaps)}",
        flush=True,
    )

    print("Batch wall monitor (all episodes)...", flush=True)
    monitors = batch_monitor_episodes(episodes, raw_root, snapshot_ids=set())
    primary = select_primary(episodes, monitors)
    snap_ids = {e["arrival_episode_id"] for e in primary}

    # known prefer id for snapshots
    known_ms = _ms(_utc(KNOWN_0230))
    for e in episodes:
        if e["side"] == "ASK" and abs(e["arrival_ts_ms"] - known_ms) <= 30 * 60 * 1000:
            if "1787684400" in e["pool_id"] or abs(e["arrival_ts_ms"] - known_ms) < 5 * 60 * 1000:
                snap_ids.add(e["arrival_episode_id"])

    print(f"Re-monitor primary+known with snapshots ({len(snap_ids)})...", flush=True)
    detail = batch_monitor_episodes(
        [e for e in episodes if e["arrival_episode_id"] in snap_ids],
        raw_root,
        snapshot_ids=snap_ids,
    )
    monitors.update(detail)

    write_known_0230(out, episodes, born, monitors, raw_root)

    arrival_summaries = []
    for ep in episodes:
        mon = monitors[ep["arrival_episode_id"]]
        at = mon.get("at_arrival") or {}
        arrival_summaries.append(
            {
                **{k: ep[k] for k in ep if k != "arrival_ts_ms"},
                "monitoring_start_ts": mon["monitoring_start_ts"],
                "monitoring_end_ts": mon["monitoring_end_ts"],
                "end_reason": mon["end_reason"],
                "strongest_wall_price_at_arrival": at.get("strongest_price"),
                "strongest_wall_notional_at_arrival": at.get("strongest_notional"),
                "strongest_wall_rank_at_arrival": at.get("strongest_rank"),
                "strongest_wall_class_at_arrival": at.get("strongest_class"),
                "n_major_at_arrival": at.get("n_major"),
                "n_moderate_at_arrival": at.get("n_moderate"),
                "total_same_side_notional_inside_pool_at_arrival": at.get("total_inside_notional"),
                "states": "|".join(mon["states"]),
                "wall_present_before": mon["before_major_or_mod"],
                "wall_appeared_after": mon["after_major_or_mod"],
                "wall_switched": mon["wall_switched"],
            }
        )

    ask_arr = [e for e in episodes if e["side"] == "ASK"]
    bid_arr = [e for e in episodes if e["side"] == "BID"]
    with_maj = sum(
        1 for s in arrival_summaries if s.get("strongest_wall_class_at_arrival") == "MAJOR"
    )
    with_mod = sum(
        1 for s in arrival_summaries if s.get("strongest_wall_class_at_arrival") == "MODERATE"
    )
    only_minor = sum(
        1 for s in arrival_summaries if s.get("strongest_wall_class_at_arrival") == "MINOR"
    )
    no_wall = sum(1 for s in arrival_summaries if not s.get("strongest_wall_price_at_arrival"))
    before_n = sum(1 for s in arrival_summaries if s.get("wall_present_before"))
    after_n = sum(1 for s in arrival_summaries if s.get("wall_appeared_after"))
    persistent_n = sum(1 for s in arrival_summaries if "WALL_PERSISTENT" in (s.get("states") or ""))
    intermittent_n = sum(
        1 for s in arrival_summaries if "WALL_INTERMITTENT" in (s.get("states") or "")
    )
    switched_n = sum(1 for s in arrival_summaries if s.get("wall_switched"))

    funnel = [
        {"metric": "causal_ask_pools", "value": n_ask},
        {"metric": "causal_bid_pools", "value": n_bid},
        {"metric": "ask_arrivals_from_below", "value": len(ask_arr)},
        {"metric": "bid_arrivals_from_above", "value": len(bid_arr)},
        {"metric": "born_inside", "value": len(born)},
        {"metric": "gap_cross", "value": len(gaps)},
        {"metric": "deduped_arrival_episodes", "value": len(episodes)},
        {"metric": "arrival_with_major_wall_at_arrival", "value": with_maj},
        {"metric": "arrival_with_moderate_wall_at_arrival", "value": with_mod},
        {"metric": "arrival_only_minor_wall", "value": only_minor},
        {"metric": "arrival_no_same_side_wall", "value": no_wall},
        {"metric": "wall_present_before_arrival_maj_mod", "value": before_n},
        {"metric": "wall_appeared_after_arrival_maj_mod", "value": after_n},
        {"metric": "wall_persistent_count", "value": persistent_n},
        {"metric": "wall_intermittent_count", "value": intermittent_n},
        {"metric": "strongest_wall_switched_count", "value": switched_n},
        {"metric": "genuine_mid_samples", "value": len(mids)},
        {"metric": "coverage_blocked", "value": 0},
    ]

    wall_snapshots: list[dict] = []
    wall_tracks: list[dict] = []
    primary_rows = []
    manual = [
        "# MANUAL_ARRIVAL_WALL_REVIEW",
        "",
        "Chart-Toggles: Liquidity Location + Orderbook Walls.",
        "",
        "Fragen: Pool sichtbar? Richtung? Erster Eintritt? Wall im Pool? "
        "Vor/nach Arrival? Persistenz? Verschwindet/verschiebt? Layer-Match?",
        "",
    ]
    for i, ep in enumerate(primary, 1):
        mon = monitors[ep["arrival_episode_id"]]
        at = mon.get("at_arrival") or {}
        wall_snapshots.extend(mon.get("wall_snapshots") or [])
        wall_tracks.extend(mon.get("wall_tracks") or [])
        arr_ts = ep["arrival_ts"]
        row = {
            "example_id": f"E{i:02d}",
            "side": ep["side"],
            "arrival_ts": arr_ts,
            "chart_window_start": _iso(_utc(arr_ts) - timedelta(minutes=10)),
            "chart_window_end": _iso(_utc(arr_ts) + timedelta(minutes=10)),
            "pool_id": ep["pool_id"],
            "lower_edge": ep["lower_edge"],
            "upper_edge": ep["upper_edge"],
            "strength": ep.get("strength"),
            "arrival_edge": ep["arrival_edge"],
            "approach_direction": ep["approach_direction"],
            "mid_at_arrival": ep["mid_at_arrival"],
            "strongest_wall_price": at.get("strongest_price"),
            "strongest_wall_notional": at.get("strongest_notional"),
            "strongest_wall_rank": at.get("strongest_rank"),
            "strongest_wall_class": at.get("strongest_class"),
            "distance_to_edge_bps": (
                bps_distance(at["strongest_price"], ep["arrival_edge"])
                if at.get("strongest_price") is not None
                else None
            ),
            "wall_before_or_after": (
                "BEFORE"
                if mon["before_major_or_mod"]
                else ("AFTER" if mon["after_major_or_mod"] else "NONE")
            ),
            "states": "|".join(mon["states"]),
            "wall_switched": mon["wall_switched"],
            "monitoring_end_ts": mon["monitoring_end_ts"],
            "end_reason": mon["end_reason"],
            "arrival_episode_id": ep["arrival_episode_id"],
        }
        primary_rows.append(row)
        manual += [
            f"## Beispiel {i} — {ep['side']}",
            f"- UTC arrival: `{arr_ts}`",
            f"- Chartfenster: `{row['chart_window_start']}` → `{row['chart_window_end']}`",
            f"- pool_id: `{ep['pool_id']}` bounds=[{ep['lower_edge']}, {ep['upper_edge']}]",
            f"- strength: `{ep.get('strength')}`",
            f"- arrival_edge / direction: `{ep['arrival_edge']}` / `{ep['approach_direction']}`",
            f"- mid: `{ep['mid_at_arrival']}`",
            f"- strongest wall: price=`{at.get('strongest_price')}` "
            f"notional=`{at.get('strongest_notional')}` "
            f"rank=`{at.get('strongest_rank')}` class=`{at.get('strongest_class')}`",
            f"- distance_to_edge_bps: `{row['distance_to_edge_bps']}`",
            f"- wall before/after: `{row['wall_before_or_after']}`",
            f"- states: `{row['states']}`",
            f"- wall_switched: `{mon['wall_switched']}`",
            f"- monitoring end: `{mon['monitoring_end_ts']}` ({mon['end_reason']})",
            "",
        ]

    write_csv(out / "arrival_funnel.csv", funnel)
    write_csv(out / "pool_arrival_episodes.csv", arrival_summaries)
    write_csv(out / "born_inside_cases.csv", born)
    write_csv(out / "gap_cross_cases.csv", gaps)
    write_csv(out / "wall_snapshots_inside_pool.csv", wall_snapshots)
    write_csv(out / "wall_tracks.csv", wall_tracks)
    write_csv(out / "arrival_wall_summary.csv", arrival_summaries)
    write_csv(out / "primary_examples.csv", primary_rows)
    (out / "MANUAL_ARRIVAL_WALL_REVIEW.md").write_text("\n".join(manual), encoding="utf-8")

    (out / "arrival_contract.json").write_text(
        json.dumps(
            {
                "ask_arrival": "prev_mid < lower_edge <= mid; pool available_at <= ts; active",
                "bid_arrival": "prev_mid > upper_edge >= mid; pool available_at <= ts; active",
                "price": "mid=(best_bid+best_ask)/2 from genuine Raw OB200",
                "born_inside": "first mid at/after available_at already inside pool",
                "gap_cross_ms": MAX_GAP_MS,
                "monitor_max_s": MONITOR_MAX_S,
                "pre_window_s": PRE_WINDOW_S,
                "wall_significance": "MAJOR=Top5 or P95; MODERATE=rank6-20 or P80-P95 (prior audit)",
                "forbidden_cause_labels": list(FORBIDDEN_CAUSE),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "coverage_inventory.json").write_text(
        json.dumps(
            {
                "window": {"start": WINDOW_START, "end": WINDOW_END},
                "symbol": "BTCUSDT",
                "pool_timeframe": "5m",
                "genuine_mid_samples": len(mids),
                "raw_root": str(raw_root),
                "pools_ask": n_ask,
                "pools_bid": n_bid,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if not episodes:
        verdict = "LIQUIDITY_POOL_ARRIVAL_INTERNAL_WALL_MONITOR_V1_NO_VALID_ARRIVALS"
    elif len(primary_rows) < 6:
        verdict = "LIQUIDITY_POOL_ARRIVAL_INTERNAL_WALL_MONITOR_V1_PARTIAL"
    else:
        verdict = "LIQUIDITY_POOL_ARRIVAL_INTERNAL_WALL_MONITOR_V1_COMPLETE"

    (out / "data_quality_report.json").write_text(
        json.dumps(
            {
                "foundation_parity_pass": foundation["parity_pass"],
                "n_episodes": len(episodes),
                "n_primary": len(primary_rows),
                "no_outcomes": True,
                "no_public_trades": True,
                "forbidden_cause_absent": True,
                "verdict": verdict,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    funnel_map = {r["metric"]: r["value"] for r in funnel}
    (out / "run_manifest.json").write_text(
        json.dumps(
            {
                "audit_id": "LIQUIDITY_POOL_ARRIVAL_INTERNAL_WALL_MONITOR_V1",
                "foundation_commit": "9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4",
                "verdict": verdict,
                "funnel": funnel_map,
                "primary_arrival_ts": [r["arrival_ts"] for r in primary_rows],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    prim_lines = "\n".join(
        f"- E{i:02d} {r['side']} `{r['arrival_ts']}` pool=`{r['pool_id']}` "
        f"wall_class=`{r['strongest_wall_class']}`"
        for i, r in enumerate(primary_rows, 1)
    )
    bericht = f"""# ABSCHLUSSBERICHT — LIQUIDITY_POOL_ARRIVAL_INTERNAL_WALL_MONITOR_V1

## 1. VERDICT

**{verdict}**

## 2. Live-Sicherheit

Read-only. Kein Commit, kein Push, keine Prozess-/CH-Mutation. Outputs nur unter diesem Ergebnisordner.

## 3. Branch / HEAD / Dirty

orderbook_analyse `feature/strategy-lab-phase1` @ `9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4`. Fremde Dirty-Dateien unverändert.

## 4. Pool-Foundation-Parität

parity_pass=`{foundation['parity_pass']}` — siehe `pool_foundation_verification.json`.

## 5. Coverage

genuine mid samples=`{len(mids)}` · raw=`{raw_root}` · Fenster {WINDOW_START} … {WINDOW_END}

## 6. Arrival-Definition

ASK: prev_mid < lower_edge ≤ mid · BID: prev_mid > upper_edge ≥ mid · Mid aus genuine OB200 · nur `available_at ≤ ts`.

## 7. ASK-Arrival-Count

{len(ask_arr)}

## 8. BID-Arrival-Count

{len(bid_arr)}

## 9. Born-inside / Gap-cross

born_inside=`{len(born)}` · gap_cross=`{len(gaps)}`

## 10. Deduplizierte Episoden

{len(episodes)}

## 11. Arrivals mit MAJOR-Wall

{with_maj}

## 12. Arrivals mit MODERATE-Wall

{with_mod}

## 13. Arrivals ohne bedeutende Wall

only_minor=`{only_minor}` · no_same_side_wall=`{no_wall}`

## 14. Walls vor Arrival

wall_present_before_maj_mod=`{before_n}`

## 15. Walls nach Arrival erschienen

wall_appeared_after_maj_mod=`{after_n}`

## 16. Persistenz

persistent=`{persistent_n}` · intermittent=`{intermittent_n}`

## 17. Wall-Wechsel

strongest_wall_switched=`{switched_n}`

## 18. Sechs Primary-Timestamps

{prim_lines}

## 19. Bekannter 02:30-Fall

Siehe `known_0230_case.md`.

## 20. Erscheinen bedeutende Walls vor oder während echter Pool-Ankünfte?

Bei Arrival: MAJOR=`{with_maj}`, MODERATE=`{with_mod}`, keine Wall=`{no_wall}`.
Vor Arrival (MAJ/MOD sichtbar): `{before_n}`. Neu nach Arrival (MAJ/MOD): `{after_n}`.
Siehe Primary-Beispiele und `arrival_wall_summary.csv`.

## 21. Einschränkung

Ursache von Größenänderung/Verschwinden noch unbekannt (keine Public Trades; nur `*_CAUSE_UNKNOWN`).

## 22. Stop

Audit beendet. Auf manuelle Chartprüfung warten. Keine Folgeimplementierung.
"""
    (out / "ABSCHLUSSBERICHT.md").write_text(bericht, encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": verdict,
                "primary": [r["arrival_ts"] for r in primary_rows],
                "funnel": funnel_map,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
