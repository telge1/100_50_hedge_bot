"""Measure 1h PD1 window_service cold/warm cache and zoom-style chunk counts (READ-ONLY)."""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pd1_landscape_viewer_v1.window_response_cache_v1 import clear_cache_for_tests
from pd1_landscape_viewer_v1.window_service_v1 import get_landscape_window_response, service_cache_stats

STEP_MS = 5 * 60 * 1000
PREFETCH_MS = 36 * 3600 * 1000
TAIL_PAD_MS = 6 * 3600 * 1000
MAX_CHUNK_MS = (864 - 1) * STEP_MS  # EOD_MAX_API_CHUNK_MS


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor5m(ms: int) -> int:
    return (ms // STEP_MS) * STEP_MS


def ceil5m(ms: int) -> int:
    return ((ms + STEP_MS - 1) // STEP_MS) * STEP_MS


def plan_sliding(load_from_ms: int, load_to_ms: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    cur = load_from_ms
    while cur < load_to_ms:
        end = min(cur + MAX_CHUNK_MS, load_to_ms)
        if end <= cur:
            break
        out.append((ms_to_iso(cur), ms_to_iso(end)))
        if end >= load_to_ms:
            break
        cur = end
    return out


def load_span(hit_from_ms: int, hit_to_ms: int, lo_ms: int, hi_ms: int) -> tuple[int, int]:
    from_ms = max(floor5m(lo_ms), floor5m(hit_from_ms - PREFETCH_MS))
    to_ms = min(ceil5m(hi_ms), ceil5m(hit_to_ms + TAIL_PAD_MS + PREFETCH_MS))
    return from_ms, to_ms


def bench_window(from_utc: str, to_utc: str) -> dict:
    t0 = time.perf_counter()
    p = get_landscape_window_response(
        from_utc=from_utc,
        to_utc=to_utc,
        timeframe="1h",
        pool_detail=1,
        response_mode="exact_origin_v1",
    )
    wall = (time.perf_counter() - t0) * 1000
    rc = p.get("response_cache") or {}
    timing = p.get("timing_ms") or {}
    return {
        "from": from_utc,
        "to": to_utc,
        "wall_ms": round(wall, 1),
        "cache_hit": bool(rc.get("hit")),
        "timing_ms": timing,
        "pool_count": len(p.get("pools") or []),
    }


def scenario(label: str, hit_from: str, hit_to: str, data_lo: str, data_hi: str) -> dict:
    h0 = int(datetime.fromisoformat(hit_from.replace("Z", "+00:00")).timestamp() * 1000)
    h1 = int(datetime.fromisoformat(hit_to.replace("Z", "+00:00")).timestamp() * 1000)
    lo = int(datetime.fromisoformat(data_lo.replace("Z", "+00:00")).timestamp() * 1000)
    hi = int(datetime.fromisoformat(data_hi.replace("Z", "+00:00")).timestamp() * 1000)
    lf, lt = load_span(h0, h1, lo, hi)
    chunks = plan_sliding(lf, lt)
    clear_cache_for_tests()
    cold = [bench_window(f, t) for f, t in chunks]
    warm = [bench_window(f, t) for f, t in chunks]
    repeat = bench_window(chunks[0][0], chunks[0][1])
    return {
        "label": label,
        "hit_from": hit_from,
        "hit_to": hit_to,
        "load_span": [ms_to_iso(lf), ms_to_iso(lt)],
        "chunk_count": len(chunks),
        "chunks": chunks,
        "cold_wall_ms_sum": round(sum(x["wall_ms"] for x in cold), 1),
        "cold_wall_ms_max": round(max(x["wall_ms"] for x in cold), 1),
        "warm_wall_ms_sum": round(sum(x["wall_ms"] for x in warm), 1),
        "cold_details": cold,
        "warm_details": warm,
        "repeat_first_chunk": repeat,
    }


def main() -> None:
    # Aug 2026 viewport (data available band similar to capabilities)
    data_lo, data_hi = "2026-07-01T00:00:00Z", "2026-11-01T00:00:00Z"
    cases = [
        scenario("load_24h", "2026-08-26T00:00:00Z", "2026-08-27T00:00:00Z", data_lo, data_hi),
        scenario("zoom_7d", "2026-08-20T00:00:00Z", "2026-08-27T00:00:00Z", data_lo, data_hi),
        scenario("zoom_30d", "2026-08-01T00:00:00Z", "2026-08-31T00:00:00Z", data_lo, data_hi),
    ]
    pan = scenario("pan_24h_shift_1h", "2026-08-26T01:00:00Z", "2026-08-27T01:00:00Z", data_lo, data_hi)
    cases.append(pan)
    out = {"scenarios": cases, "cache_stats": service_cache_stats()}
    path = Path(__file__).resolve().parent.parent / "logs" / "benchmark_1h_pd1_window_perf_v1.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({c["label"]: {
        "chunks": c["chunk_count"],
        "cold_sum_ms": c["cold_wall_ms_sum"],
        "warm_sum_ms": c["warm_wall_ms_sum"],
        "repeat_hit": c["repeat_first_chunk"]["cache_hit"],
    } for c in cases}, indent=2))
    print("wrote", path)


if __name__ == "__main__":
    main()
