#!/usr/bin/env python3
"""LIQUIDITY_POOL_EDGE_RAW_OB200_WALL_OVERLAP_AUDIT_V1

Pools: orderbook_analyse.liquidity_pool_signal (chart engine).
OB200: existing MutableBook replay from raw_shadow ob200_v3 archives.
No trading logic. Read-only.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
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
    export_snapshot,
    get_engine_function,
    nearest_front,
    parity_pair,
)
from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import load_chart_candles
from orderbook_analyse.ob200_v3_raw_discovery.audit import (
    is_replayable_line,
    iter_decompressed_lines,
    line_to_replay_payload,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import ZERO, MutableBook

DEFAULT_RAW_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
)
FOUNDATION_PARITY_SHA = {
    "2026-08-25T21:00:00Z": "c8a2ee21da88f9d537cec2cffd7d02a659df518ab30000d69aece3ec4d4f3a16",
    "2026-08-26T04:48:00Z": "4cd0764108306ca7144eed7f72cedb1434ad1307dd0aaa77a5df264d56aa2406",
}
SNAPSHOTS = [
    "2026-08-25T21:00:00Z",
    "2026-08-25T23:00:00Z",
    "2026-08-26T02:30:00Z",
    "2026-08-26T04:48:00Z",
    "2026-08-26T08:30:00Z",
    "2026-08-26T11:00:00Z",
]
MAX_OB_AGE_MS = 1000
EDGE_BPS_PRIMARY = 1.0
BPS_BANDS = (0.0, 0.5, 1.0, 2.0, 5.0)


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


def bps_distance(price: float, edge: float) -> float:
    if edge <= 0:
        return float("inf")
    return abs(price - edge) / edge * 10000.0


def verify_foundation_parity() -> dict[str, Any]:
    eng = get_engine_function()
    assert eng is chart_pool_engine()
    assert eng.__module__ == "indicators.liquidity_location.engine"
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
            "expected_sha256": expected,
            "chart_payload_sha256": pr["chart_payload_sha256"],
            "cli_payload_sha256": pr["cli_payload_sha256"],
            "match": ok,
        }
        if not ok:
            out["parity_pass"] = False
    return out


class BookSnap:
    __slots__ = (
        "ts_ms",
        "genuine",
        "reconstructed",
        "carried_forward",
        "seq_gap",
        "source_file",
        "bids",
        "asks",
    )

    def __init__(
        self,
        ts_ms: int,
        genuine: bool,
        reconstructed: bool,
        carried_forward: bool,
        seq_gap: bool,
        source_file: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> None:
        self.ts_ms = ts_ms
        self.genuine = genuine
        self.reconstructed = reconstructed
        self.carried_forward = carried_forward
        self.seq_gap = seq_gap
        self.source_file = source_file
        self.bids = bids
        self.asks = asks


def replay_books_at_times(
    *,
    raw_root: Path,
    symbol: str,
    as_of_ms: int,
    probe_ms_list: list[int],
) -> dict[int, BookSnap | None]:
    """Replay existing MutableBook path; capture book at each probe (last event <= probe)."""
    probes = sorted(set(probe_ms_list))
    if not probes:
        return {}
    start = _dt_ms(min(probes) - 3_600_000)  # prior hour warmup
    end = _dt_ms(max(probes) + 1_000)
    segments = list_closed_segments(
        raw_root, symbols=(symbol,), start=start, end=end, include_boundary_stubs=False
    )
    book = MutableBook()
    gap_latched = False
    last_event_ts: int | None = None
    last_source = ""
    # For each probe: best BookSnap with event_ts <= probe
    best: dict[int, BookSnap] = {}

    def capture(ts: int) -> None:
        nonlocal last_event_ts
        if not book.is_valid or not book.bids or not book.asks:
            return
        bids = [(float(p), float(q)) for p, q in book.sorted_bids()[:200]]
        asks = [(float(p), float(q)) for p, q in book.sorted_asks()[:200]]
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return
        snap = BookSnap(
            ts_ms=ts,
            genuine=not gap_latched and book.is_valid,
            reconstructed=False,
            carried_forward=False,
            seq_gap=gap_latched,
            source_file=last_source,
            bids=bids,
            asks=asks,
        )
        for p in probes:
            if ts <= p:
                prev = best.get(p)
                if prev is None or ts >= prev.ts_ms:
                    best[p] = snap

    for ref in segments:
        last_source = str(ref.path)
        for _line, obj in iter_decompressed_lines(ref.path):
            if not is_replayable_line(obj):
                continue
            payload = line_to_replay_payload(obj)
            data = payload.get("data") or {}
            mtype = payload.get("type")
            ts = obj.get("ts")
            if not isinstance(ts, int):
                continue
            if ts > max(probes) + MAX_OB_AGE_MS:
                # still apply? stop early once past max probe
                if ts > max(probes):
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
            last_event_ts = ts
            if ts <= max(probes):
                capture(ts)
        else:
            continue
        # if inner break due to past max probe, continue other segments? usually chronological
        if last_event_ts is not None and last_event_ts > max(probes):
            break

    out: dict[int, BookSnap | None] = {p: best.get(p) for p in probes}
    return out


def significance_class(rank: int, percentile: float) -> str:
    """Fixed descriptive classes (audit §9.2) — not a fitted trading gate."""
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
    mean = (sum(notionals) / n) if n else 0.0
    for i, r in enumerate(rows):
        rank = i + 1
        # empirical percentile: fraction of levels with notional <= this
        pct = sum(1 for x in notionals if x <= r["notional"]) / n if n else 0.0
        next_n = rows[i + 1]["notional"] if i + 1 < n else None
        r["top200_side_rank_by_notional"] = rank
        r["top200_side_percentile"] = pct
        r["ratio_to_side_median"] = (r["notional"] / med) if med > 0 else None
        r["ratio_to_side_mean"] = (r["notional"] / mean) if mean > 0 else None
        r["ratio_to_next_largest_level"] = (
            (r["notional"] / next_n) if next_n and next_n > 0 else None
        )
        r["significance_class"] = significance_class(rank, pct)
    return rows


def resolve_entry_side(
    *,
    candles: list,
    pool: dict[str, Any],
    as_of: datetime,
) -> dict[str, Any]:
    """Causal entry into pool using past candle closes only."""
    available = _utc(pool["available_at"])
    lo = float(pool["lower_edge"])
    hi = float(pool["upper_edge"])
    as_of_u = _utc(as_of)
    # candle.timestamp = open; use close as of bar open for sequence
    series: list[tuple[datetime, float]] = []
    for c in candles:
        ts = c.timestamp
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        if ts > as_of_u:
            break
        series.append((ts, float(c.close)))

    # find first time after available_at when price is inside [lo,hi]
    prev_out: str | None = None  # BELOW / ABOVE
    entry_ts = None
    entry_side = "ENTRY_SIDE_UNRESOLVED"
    entry_edge = None
    for ts, px in series:
        if ts < available:
            if px < lo:
                prev_out = "BELOW"
            elif px > hi:
                prev_out = "ABOVE"
            elif lo <= px <= hi:
                prev_out = "INSIDE"
            continue
        inside = lo <= px <= hi
        if inside:
            if prev_out == "BELOW":
                entry_ts = ts
                entry_side = "FROM_BELOW"
                entry_edge = lo
                break
            if prev_out == "ABOVE":
                entry_ts = ts
                entry_side = "FROM_ABOVE"
                entry_edge = hi
                break
            if prev_out == "INSIDE" or prev_out is None:
                # already inside at availability
                entry_side = "ENTRY_SIDE_UNRESOLVED"
                entry_ts = available
                entry_edge = None
                break
        else:
            if px < lo:
                prev_out = "BELOW"
            elif px > hi:
                prev_out = "ABOVE"

    market = series[-1][1] if series else None
    return {
        "pool_entry_ts": _iso(entry_ts) if entry_ts else None,
        "entry_side": entry_side,
        "entry_edge": entry_edge,
        "current_distance_to_lower_edge_bps": (
            bps_distance(market, lo) if market is not None else None
        ),
        "current_distance_to_upper_edge_bps": (
            bps_distance(market, hi) if market is not None else None
        ),
    }


def classify_overlap(
    *,
    front_edge: float | None,
    same_side_rows: list[dict[str, Any]],
    inside_rows: list[dict[str, Any]],
    ob_unavailable: bool,
    entry_unresolved: bool,
) -> str:
    if ob_unavailable:
        return "OB_SNAPSHOT_UNAVAILABLE"
    if entry_unresolved and front_edge is None:
        return "ENTRY_SIDE_UNRESOLVED"

    def at_edge(rows: list[dict[str, Any]], cls: str) -> bool:
        if front_edge is None:
            return False
        for r in rows:
            if r["significance_class"] != cls:
                continue
            if r.get("distance_to_front_edge_bps") is not None and r["distance_to_front_edge_bps"] <= EDGE_BPS_PRIMARY:
                return True
        return False

    if at_edge(same_side_rows, "MAJOR"):
        return "MAJOR_WALL_AT_FRONT_EDGE"
    if at_edge(same_side_rows, "MODERATE"):
        return "MODERATE_WALL_AT_FRONT_EDGE"
    if at_edge(same_side_rows, "MINOR"):
        return "MINOR_WALL_AT_FRONT_EDGE"
    if any(r["significance_class"] == "MAJOR" and r.get("inside_pool") for r in inside_rows):
        return "MAJOR_WALL_INSIDE_POOL"
    if any(r.get("inside_pool") for r in inside_rows):
        return "WALL_INSIDE_POOL_NOT_MAJOR"
    return "NO_SAME_SIDE_WALL_IN_POOL"


def persistence_for_wall(
    books: dict[int, BookSnap | None],
    *,
    as_of_ms: int,
    side: str,
    wall_price: float,
    tick: float,
) -> dict[str, Any]:
    windows = (5, 15, 30, 60)
    out: dict[str, Any] = {"wall_price": wall_price, "side": side, "windows": {}}
    for sec in windows:
        probe = as_of_ms - sec * 1000
        # examine all books with probe <= ts <= as_of for visibility? Spec: check at T-5 etc.
        snap = books.get(probe)
        # also need fraction over window — sample at 1s steps using available captured books
        samples = []
        for t, b in books.items():
            if b is None:
                continue
            if as_of_ms - sec * 1000 <= t <= as_of_ms:
                samples.append(b)
        visible = 0
        notionals = []
        first_seen = None
        for b in sorted(samples, key=lambda x: x.ts_ms):
            levels = b.bids if side == "BID" else b.asks
            hit = None
            for p, q in levels:
                if abs(p - wall_price) <= tick * 0.5 + 1e-12:
                    hit = notional(p, q)
                    break
            if hit is not None and hit > 0:
                visible += 1
                notionals.append(hit)
                if first_seen is None:
                    first_seen = b.ts_ms
        n = len(samples)
        frac = (visible / n) if n else 0.0
        genuine_n = sum(1 for b in samples if b.genuine)
        genuine_frac = (genuine_n / n) if n else 0.0
        if n == 0 or genuine_frac < 0.5:
            label = "INSUFFICIENT_COVERAGE"
        elif frac >= 0.8 and first_seen is not None and (as_of_ms - first_seen) >= sec * 1000 * 0.5:
            label = "PERSISTENT"
        elif frac >= 0.2:
            label = "INTERMITTENT"
        elif visible > 0 and first_seen is not None and (as_of_ms - first_seen) < 5000:
            label = "NEWLY_APPEARED"
        elif visible > 0:
            label = "INTERMITTENT"
        else:
            label = "NEWLY_APPEARED" if snap is None else "INSUFFICIENT_COVERAGE"
            if visible == 0:
                label = "INSUFFICIENT_COVERAGE"
        out["windows"][str(sec)] = {
            "seconds": sec,
            "n_samples": n,
            "seconds_visible_est": visible,  # count of sample points with wall
            "visibility_fraction": frac,
            "first_seen_ts": _iso(_dt_ms(first_seen)) if first_seen else None,
            "initial_notional": notionals[0] if notionals else None,
            "last_notional": notionals[-1] if notionals else None,
            "min_notional": min(notionals) if notionals else None,
            "max_notional": max(notionals) if notionals else None,
            "median_notional": median(notionals),
            "price_level_stable": True,  # same tick match
            "genuine_coverage_fraction": genuine_frac,
            "persistence_label": label,
        }
    # overall label = worst of 60s window primary
    w60 = out["windows"].get("60") or {}
    out["persistence_label"] = w60.get("persistence_label", "INSUFFICIENT_COVERAGE")
    return out


def pools_to_audit(snap: dict[str, Any]) -> list[dict[str, Any]]:
    loc = snap.get("market_pool_location") or snap["nearest"].get("market_pool_location")
    market = snap["market_price"]
    active = {p["pool_id"]: p for p in snap["active_pools"]}
    out: list[dict[str, Any]] = []

    if loc in ("INSIDE_ASK_POOL", "INSIDE_BID_POOL", "INSIDE_OVERLAPPING_POOLS"):
        ids = snap["nearest"].get("inside_pool_ids") or []
        for pid in ids:
            p = active.get(pid)
            if p:
                out.append({**p, "role": "INSIDE_POOL", "market_pool_location": loc})
        # descriptive external nearest
        outside = [p for p in snap["active_pools"] if p["pool_id"] not in ids]
        nf = nearest_front(outside, market)
        for key, role in (
            ("nearest_ask_pool_above_market", "EXTERNAL_ASK_DESCRIPTIVE"),
            ("nearest_bid_pool_below_market", "EXTERNAL_BID_DESCRIPTIVE"),
        ):
            v = nf.get(key)
            if isinstance(v, dict) and v.get("pool_id") in active:
                out.append({**active[v["pool_id"]], "role": role, "market_pool_location": loc})
        return out

    # BETWEEN
    for key, role in (
        ("nearest_ask_pool_above_market", "NEAREST_ASK_ABOVE"),
        ("nearest_bid_pool_below_market", "NEAREST_BID_BELOW"),
    ):
        v = snap["nearest"].get(key)
        if isinstance(v, dict) and v.get("pool_id") in active:
            out.append({**active[v["pool_id"]], "role": role, "market_pool_location": loc})
    return out


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        default=str(OA_ROOT / "results" / "liquidity_pool_edge_raw_ob200_wall_overlap_audit_v1"),
    )
    ap.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
    args = ap.parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw_root = Path(args.raw_root)

    print("Verifying foundation parity...", flush=True)
    foundation = verify_foundation_parity()
    (out / "pool_foundation_verification.json").write_text(
        json.dumps(foundation, indent=2), encoding="utf-8"
    )
    if not foundation["parity_pass"]:
        print("POOL_FOUNDATION_PARITY_FAILED", flush=True)
        return 3

    source_contract = {
        "raw_root": str(raw_root),
        "loader": "MutableBook via ob200_v3_raw_discovery.audit.iter_decompressed_lines + apply_snapshot/delta",
        "reuse_of": [
            "ob200_v3_raw_discovery.mutable_book.MutableBook",
            "ob200_v3_raw_discovery.files.list_closed_segments",
            "l2_wall_to_wall_discovery.major_defended_reclaim.rich_samples replay pattern",
        ],
        "full_200_levels": True,
        "not_used": "orderbook_features_1s_v2 aggregate proxy",
        "timestamp_field": "event ts (ms) on archive lines",
        "bid_ask_fields": "data.b / data.a [price, qty]",
        "notional": "price * qty (major_defended_reclaim.util.notional)",
        "tick_size": "l2_wall_attack_discovery.models.tick_size (BTCUSDT=0.1)",
        "genuine": "book.is_valid and no seq_gap latch",
        "carried_forward": False,
        "reconstructed": False,
        "max_age_ms": MAX_OB_AGE_MS,
        "wall_significance": {
            "mode": "descriptive_audit_classes",
            "note": "Existing walls.extract_wall_events uses qty_vs_median>=3 lifecycle gate; not reused as primary class to avoid conflating with lifecycle. Audit uses Top-5/P95 MAJOR etc.",
            "MAJOR": "rank<=5 OR percentile>=0.95",
            "MODERATE": "rank 6-20 OR percentile 0.80-0.95",
            "MINOR": "below",
        },
    }
    (out / "raw_ob200_source_contract.json").write_text(
        json.dumps(source_contract, indent=2), encoding="utf-8"
    )

    audited: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    persistence_rows: list[dict[str, Any]] = []
    manual_blocks: list[str] = []

    tick = tick_size("BTCUSDT")
    window_start = _utc("2026-08-25T20:00:00Z")

    for s in SNAPSHOTS:
        as_of = _utc(s)
        as_of_ms = _ms(as_of)
        print(f"Snapshot {s} ...", flush=True)
        snap = export_snapshot(
            symbol="BTCUSDT", timeframe="5m", window_start=window_start, as_of=as_of
        )
        # candles for entry side
        pack_start = chart_lookback_start(as_of, "5m")
        _packed, candles = load_chart_candles("BTCUSDT", "5m", start=pack_start, end=as_of)

        probes = [as_of_ms] + [as_of_ms - sec * 1000 for sec in (5, 15, 30, 60)]
        # denser samples for persistence fraction: every 1s in last 60s
        probes += list(range(as_of_ms - 60_000, as_of_ms + 1, 1000))
        books = replay_books_at_times(
            raw_root=raw_root, symbol="BTCUSDT", as_of_ms=as_of_ms, probe_ms_list=probes
        )
        book = books.get(as_of_ms)
        age_ms = None
        cov_status = "OB_SNAPSHOT_UNAVAILABLE"
        if book is not None:
            age_ms = as_of_ms - book.ts_ms
            if age_ms < 0:
                cov_status = "OB_SNAPSHOT_AFTER_ASOF_REJECTED"
                book = None
            elif age_ms > MAX_OB_AGE_MS:
                cov_status = "OB_SNAPSHOT_STALE"
                book = None
            elif not book.genuine:
                cov_status = "OB_SNAPSHOT_NOT_GENUINE"
                # still allow? Spec wants genuine primarily — treat as unavailable for overlap
                book = None
            else:
                cov_status = "OK"

        coverage.append(
            {
                "pool_as_of_ts": s,
                "ob_snapshot_ts": _iso(_dt_ms(book.ts_ms)) if book else None,
                "ob_age_ms": (
                    age_ms
                    if book
                    else (
                        (as_of_ms - books[as_of_ms].ts_ms)
                        if books.get(as_of_ms) is not None
                        else None
                    )
                ),
                "genuine": bool(book.genuine) if book else False,
                "reconstructed": False,
                "carried_forward": False,
                "coverage_status": cov_status,
                "source_file": (
                    book.source_file
                    if book
                    else (books[as_of_ms].source_file if books.get(as_of_ms) is not None else None)
                ),
                "n_bid_levels": len(book.bids) if book else 0,
                "n_ask_levels": len(book.asks) if book else 0,
            }
        )

        pool_list = pools_to_audit(snap)
        for pool in pool_list:
            side = pool["side"]
            role = pool["role"]
            lo, hi = float(pool["lower_edge"]), float(pool["upper_edge"])
            market = float(snap["market_price"])
            entry_info = {
                "pool_entry_ts": None,
                "entry_side": None,
                "entry_edge": None,
                "current_distance_to_lower_edge_bps": None,
                "current_distance_to_upper_edge_bps": None,
            }
            front_edge = None
            approach_side = None
            entry_unresolved = False

            if role == "INSIDE_POOL":
                entry_info = resolve_entry_side(candles=candles, pool=pool, as_of=as_of)
                if entry_info["entry_side"] == "ENTRY_SIDE_UNRESOLVED":
                    entry_unresolved = True
                    front_edge = None
                    approach_side = "ENTRY_SIDE_UNRESOLVED"
                else:
                    front_edge = entry_info["entry_edge"]
                    approach_side = entry_info["entry_side"]
            elif side == "ASK" and market < lo:
                front_edge = lo
                approach_side = "FROM_BELOW"
            elif side == "BID" and market > hi:
                front_edge = hi
                approach_side = "FROM_ABOVE"
            else:
                # descriptive external while inside market state — still geometric
                if side == "ASK":
                    front_edge = lo
                    approach_side = "FROM_BELOW"
                else:
                    front_edge = hi
                    approach_side = "FROM_ABOVE"

            audited.append(
                {
                    "as_of": s,
                    "market_price": market,
                    "market_pool_location": pool.get("market_pool_location"),
                    "role": role,
                    "pool_id": pool["pool_id"],
                    "pool_side": side,
                    "lower_edge": lo,
                    "upper_edge": hi,
                    "front_edge": front_edge,
                    "approach_side": approach_side,
                    **entry_info,
                    "available_at": pool["available_at"],
                    "strength": pool.get("strength"),
                }
            )

            if book is None:
                audit_class = (
                    "ENTRY_SIDE_UNRESOLVED"
                    if entry_unresolved and role == "INSIDE_POOL"
                    else "OB_SNAPSHOT_UNAVAILABLE"
                )
                summaries.append(
                    {
                        "as_of": s,
                        "pool_id": pool["pool_id"],
                        "pool_side": side,
                        "role": role,
                        "front_edge": front_edge,
                        "audit_class": audit_class,
                        "strongest_same_side_wall_price": None,
                        "strongest_same_side_wall_notional": None,
                        "strongest_rank": None,
                        "distance_to_edge_bps": None,
                        "persistence": None,
                    }
                )
                continue

            ranked = side_levels_ranked(book.asks if side == "ASK" else book.bids)
            opp = side_levels_ranked(book.bids if side == "ASK" else book.asks)
            same_enriched = []
            inside_enriched = []
            for r in ranked:
                dist_bps = bps_distance(r["price"], front_edge) if front_edge is not None else None
                dist_px = abs(r["price"] - front_edge) if front_edge is not None else None
                inside = lo <= r["price"] <= hi
                exact = front_edge is not None and abs(r["price"] - front_edge) <= tick * 0.5 + 1e-12
                band_hits = {
                    f"band_le_{b}_bps": (dist_bps is not None and dist_bps <= b)
                    for b in BPS_BANDS
                    if b > 0
                }
                band_hits["band_exact_tick"] = exact
                row = {
                    "as_of": s,
                    "pool_id": pool["pool_id"],
                    "pool_side": side,
                    "role": role,
                    "wall_side": side,
                    "price": r["price"],
                    "qty": r["qty"],
                    "notional": r["notional"],
                    "distance_to_front_edge_price": dist_px,
                    "distance_to_front_edge_bps": dist_bps,
                    "inside_pool": inside,
                    "exact_tick_match": exact,
                    "top200_side_rank_by_notional": r["top200_side_rank_by_notional"],
                    "top200_side_percentile": r["top200_side_percentile"],
                    "ratio_to_side_median": r["ratio_to_side_median"],
                    "ratio_to_side_mean": r["ratio_to_side_mean"],
                    "ratio_to_next_largest_level": r["ratio_to_next_largest_level"],
                    "significance_class": r["significance_class"],
                    "match_kind": "SAME_SIDE",
                    **band_hits,
                }
                # keep candidates near edge or inside pool
                if inside or (dist_bps is not None and dist_bps <= 5.0):
                    candidates.append(row)
                    same_enriched.append(row)
                    if inside:
                        inside_enriched.append(row)

            for r in opp[:5]:  # diagnostic top opposite only
                candidates.append(
                    {
                        "as_of": s,
                        "pool_id": pool["pool_id"],
                        "pool_side": side,
                        "role": role,
                        "wall_side": "BID" if side == "ASK" else "ASK",
                        "price": r["price"],
                        "qty": r["qty"],
                        "notional": r["notional"],
                        "distance_to_front_edge_price": None,
                        "distance_to_front_edge_bps": None,
                        "inside_pool": lo <= r["price"] <= hi,
                        "exact_tick_match": False,
                        "top200_side_rank_by_notional": r["top200_side_rank_by_notional"],
                        "top200_side_percentile": r["top200_side_percentile"],
                        "ratio_to_side_median": r["ratio_to_side_median"],
                        "ratio_to_side_mean": r["ratio_to_side_mean"],
                        "ratio_to_next_largest_level": r["ratio_to_next_largest_level"],
                        "significance_class": r["significance_class"],
                        "match_kind": "OPPOSITE_SIDE_DIAGNOSTIC",
                    }
                )

            audit_class = classify_overlap(
                front_edge=front_edge,
                same_side_rows=same_enriched,
                inside_rows=inside_enriched,
                ob_unavailable=False,
                entry_unresolved=entry_unresolved and front_edge is None,
            )

            # strongest same-side near edge or inside
            focus = [
                r
                for r in same_enriched
                if r.get("inside_pool")
                or (r.get("distance_to_front_edge_bps") is not None and r["distance_to_front_edge_bps"] <= 5)
            ]
            strongest = max(focus, key=lambda r: r["notional"]) if focus else None
            pers_label = None
            if strongest and strongest["significance_class"] in ("MAJOR", "MODERATE"):
                pers = persistence_for_wall(
                    books,
                    as_of_ms=as_of_ms,
                    side=side,
                    wall_price=strongest["price"],
                    tick=tick,
                )
                pers_label = pers["persistence_label"]
                for sec, w in pers["windows"].items():
                    persistence_rows.append(
                        {
                            "as_of": s,
                            "pool_id": pool["pool_id"],
                            "wall_side": side,
                            "wall_price": strongest["price"],
                            "window_seconds": sec,
                            **w,
                        }
                    )

            summaries.append(
                {
                    "as_of": s,
                    "pool_id": pool["pool_id"],
                    "pool_side": side,
                    "role": role,
                    "front_edge": front_edge,
                    "approach_side": approach_side,
                    "audit_class": audit_class,
                    "strongest_same_side_wall_price": strongest["price"] if strongest else None,
                    "strongest_same_side_wall_notional": strongest["notional"] if strongest else None,
                    "strongest_rank": strongest["top200_side_rank_by_notional"] if strongest else None,
                    "strongest_class": strongest["significance_class"] if strongest else None,
                    "distance_to_edge_bps": strongest["distance_to_front_edge_bps"] if strongest else None,
                    "persistence": pers_label,
                }
            )

            if role in ("NEAREST_ASK_ABOVE", "NEAREST_BID_BELOW", "INSIDE_POOL"):
                manual_blocks.append(
                    f"""## SNAPSHOT `{s}` — {role}
- UTC: `{s}`
- Marktpreis: `{market}`
- Pool-State: `{pool.get('market_pool_location')}`
- Pool-ID: `{pool['pool_id']}`
- Pool-Side: `{side}`
- Poolgrenzen: `[{lo} .. {hi}]`
- relevante Edge: `{front_edge}` ({approach_side})
- Eintrittsseite: `{entry_info.get('entry_side')}`
- OB-Snapshot: `{coverage[-1]['ob_snapshot_ts']}` age_ms=`{coverage[-1]['ob_age_ms']}` status=`{cov_status}`
- stärkste gleichseitige Wall: price=`{strongest['price'] if strongest else None}` notional=`{strongest['notional'] if strongest else None}` rank=`{strongest['top200_side_rank_by_notional'] if strongest else None}` class=`{strongest['significance_class'] if strongest else None}`
- Abstand zur Edge (bps): `{strongest['distance_to_front_edge_bps'] if strongest else None}`
- Auditklasse: `{audit_class}`
- Persistenz: `{pers_label}`
- manuell im Chart aktivieren: Liquidity Location + Orderbook Walls
- zu prüfen: Stimmen Poolkante und Wall-Preis visuell überein?
"""
                )

    write_csv(out / "audited_pool_edges.csv", audited)
    write_csv(out / "ob_snapshot_coverage.csv", coverage)
    write_csv(out / "wall_candidates.csv", candidates)
    write_csv(out / "wall_overlap_summary.csv", summaries)
    write_csv(out / "wall_persistence_pre_snapshot.csv", persistence_rows)
    (out / "MANUAL_WALL_REVIEW.md").write_text(
        "# MANUAL_WALL_REVIEW — Pool Edge ↔ Raw OB200\n\n"
        + "Keine Performance-/Outcome-Aussage.\n\n"
        + "\n".join(manual_blocks),
        encoding="utf-8",
    )

    class_counts: dict[str, int] = {}
    for r in summaries:
        if r["role"] in ("NEAREST_ASK_ABOVE", "NEAREST_BID_BELOW", "INSIDE_POOL"):
            class_counts[r["audit_class"]] = class_counts.get(r["audit_class"], 0) + 1

    n_cov_ok = sum(1 for c in coverage if c["coverage_status"] == "OK")
    if n_cov_ok == 0:
        verdict = "LIQUIDITY_POOL_EDGE_RAW_OB200_WALL_OVERLAP_AUDIT_V1_BLOCKED_COVERAGE"
    elif n_cov_ok < len(SNAPSHOTS):
        verdict = "LIQUIDITY_POOL_EDGE_RAW_OB200_WALL_OVERLAP_AUDIT_V1_PARTIAL"
    elif class_counts.get("MAJOR_WALL_AT_FRONT_EDGE", 0) + class_counts.get(
        "MODERATE_WALL_AT_FRONT_EDGE", 0
    ) + class_counts.get("MAJOR_WALL_INSIDE_POOL", 0) == 0:
        verdict = "LIQUIDITY_POOL_EDGE_RAW_OB200_WALL_OVERLAP_AUDIT_V1_NO_OVERLAP"
    else:
        verdict = "LIQUIDITY_POOL_EDGE_RAW_OB200_WALL_OVERLAP_AUDIT_V1_COMPLETE"

    dq = {
        "n_snapshots": len(SNAPSHOTS),
        "ob_coverage_ok": n_cov_ok,
        "foundation_parity_pass": foundation["parity_pass"],
        "class_counts_primary_roles": class_counts,
        "no_outcome_fields": True,
        "max_ob_age_ms_required": MAX_OB_AGE_MS,
    }
    (out / "data_quality_report.json").write_text(json.dumps(dq, indent=2), encoding="utf-8")
    (out / "run_manifest.json").write_text(
        json.dumps(
            {
                "audit_id": "LIQUIDITY_POOL_EDGE_RAW_OB200_WALL_OVERLAP_AUDIT_V1",
                "foundation_commit": "9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4",
                "verdict": verdict,
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "snapshots": SNAPSHOTS,
                "raw_root": str(raw_root),
                "class_counts": class_counts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # ABSCHLUSSBERICHT
    lines = [
        "# ABSCHLUSSBERICHT — LIQUIDITY_POOL_EDGE_RAW_OB200_WALL_OVERLAP_AUDIT_V1",
        "",
        "## 1. VERDICT",
        "",
        f"**{verdict}**",
        "",
        "## 2. Live-Sicherheit",
        "",
        "Read-only. Keine ClickHouse-Writes, kein Collector-/Dashboard-Restart, kein Git-Commit.",
        "",
        "## 3. Branch / HEAD / Dirty",
        "",
        "Siehe `run_manifest.json` / Git-Status bei Laufbeginn. Foundation-Commit `9b8fe7c…` erwartet.",
        "",
        "## 4. Pool-Foundation-Verifikation",
        "",
        f"parity_pass=`{foundation['parity_pass']}` — Details `pool_foundation_verification.json`.",
        "",
        "## 5. Raw-OB200-Quelle",
        "",
        f"`{raw_root}` via MutableBook replay (volle Levels). Vertrag: `raw_ob200_source_contract.json`.",
        "",
        "## 6. genuine/reconstructed Coverage",
        "",
        f"OK snapshots: {n_cov_ok}/{len(SNAPSHOTS)}. Siehe `ob_snapshot_coverage.csv`.",
        "",
        "## 7–16. Ergebnisse",
        "",
        f"Klassen (primäre Rollen): `{json.dumps(class_counts)}`",
        "",
        "Details: `wall_overlap_summary.csv`, `wall_candidates.csv`, `wall_persistence_pre_snapshot.csv`.",
        "",
        "## 17. Manuelle Review-Blöcke",
        "",
        "Siehe `MANUAL_WALL_REVIEW.md`.",
        "",
        "## 18. Gibt es gleichzeitig bedeutende Raw-OB200-Walls an Poolkanten?",
        "",
    ]
    maj = class_counts.get("MAJOR_WALL_AT_FRONT_EDGE", 0)
    mod = class_counts.get("MODERATE_WALL_AT_FRONT_EDGE", 0)
    maj_in = class_counts.get("MAJOR_WALL_INSIDE_POOL", 0)
    if maj + mod + maj_in > 0:
        lines.append(
            f"**Ja, in Teilen der Stichprobe:** MAJOR@Edge={maj}, MODERATE@Edge={mod}, MAJOR_INSIDE={maj_in}."
        )
    else:
        lines.append("**Nein / nicht in dieser Stichprobe** als MAJOR/MODERATE an Frontkante bzw. MAJOR inside.")
    lines += [
        "",
        "## 19. Einschränkung",
        "",
        "Keine Defense-/Consumption-/Trade-Aussage. Nur gleichzeitige Präsenz/Größe/Persistenz bis As-of.",
        "",
        "## 20. Stop",
        "",
        "Audit beendet. Auf manuelle Chartprüfung warten.",
        "",
    ]
    (out / "ABSCHLUSSBERICHT.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "class_counts": class_counts, "out": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
