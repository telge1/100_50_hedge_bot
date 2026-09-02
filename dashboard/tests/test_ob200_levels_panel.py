"""Tests for real OB200 levels side-panel helpers, cache, and API contract."""

from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import research_charts.ob200_walls as ob200_walls
from research_charts.ob200_levels import (
    FRESH_MS,
    STALE_MS,
    aggregate_levels,
    auto_bucket_size,
    bar_length,
    clear_ob200_levels_cache_for_tests,
    freshness_state,
    load_ob200_levels,
    sanitize_book_levels,
)
from research_charts.workspace_session import normalize_orderbook_levels


@pytest.fixture(autouse=True)
def _reset_levels_cache():
    clear_ob200_levels_cache_for_tests()
    yield
    clear_ob200_levels_cache_for_tests()


def test_sanitize_sorts_and_drops_invalid():
    bids, asks = sanitize_book_levels(
        [(100.0, 1.0), (float("nan"), 2.0), (101.0, -1.0), (99.0, 3.0), (100.0, 9.0)],
        [(102.0, 1.0), (float("inf"), 1.0), (103.0, 2.0), (102.0, 5.0)],
    )
    assert [b["price"] for b in bids] == [100.0, 99.0]
    assert [a["price"] for a in asks] == [102.0, 103.0]
    assert all(b["side"] == "bid" for b in bids)
    assert all(a["side"] == "ask" for a in asks)


def test_sanitize_empty():
    bids, asks = sanitize_book_levels([], [])
    assert bids == []
    assert asks == []


def test_aggregate_sums_and_separates_sides():
    bids = [
        {"price": 100.0, "size": 1.0, "side": "bid"},
        {"price": 100.4, "size": 2.0, "side": "bid"},
        {"price": 99.0, "size": 5.0, "side": "bid"},
    ]
    asks = [
        {"price": 101.0, "size": 1.0, "side": "ask"},
        {"price": 101.2, "size": 3.0, "side": "ask"},
    ]
    mixed = bids + asks
    out_b = aggregate_levels(mixed, bucket_size=1.0, side="bid")
    out_a = aggregate_levels(mixed, bucket_size=1.0, side="ask")
    assert all(x["side"] == "bid" for x in out_b)
    assert all(x["side"] == "ask" for x in out_a)
    top = next(x for x in out_b if abs(x["bucket_low"] - 100.0) < 1e-9)
    assert top["size"] == pytest.approx(3.0)
    assert top["raw_level_count"] == 2
    ask_top = next(x for x in out_a if abs(x["bucket_low"] - 101.0) < 1e-9)
    assert ask_top["size"] == pytest.approx(4.0)


def test_raw_mode_unchanged_via_zero_bucket():
    levels = [{"price": 1.0, "size": 2.0, "side": "bid"}]
    out = aggregate_levels(levels, bucket_size=0, side="bid")
    assert out[0]["price"] == 1.0 and out[0]["size"] == 2.0


def test_auto_bucket_respects_tick():
    b = auto_bucket_size(0.1, 100.0, 110.0, target_bars=20)
    assert b >= 0.1
    assert abs(b / 0.1 - round(b / 0.1)) < 1e-9


def test_bar_length_scales():
    assert bar_length(0, max_notional=10, scale="sqrt", panel_width=100) == 0
    assert bar_length(10, max_notional=10, scale="linear", panel_width=100) == pytest.approx(95.0)
    s = bar_length(2.5, max_notional=10, scale="sqrt", panel_width=100)
    l = bar_length(2.5, max_notional=10, scale="linear", panel_width=100)
    g = bar_length(2.5, max_notional=10, scale="log", panel_width=100)
    assert 0 < s <= 95 and 0 < l <= 95 and 0 < g <= 95
    assert math.isfinite(s) and math.isfinite(l) and math.isfinite(g)
    assert bar_length(1, max_notional=0, scale="sqrt", panel_width=100) == 0


@pytest.mark.parametrize(
    "ms,expected",
    [
        (0, "fresh"),
        (FRESH_MS - 1, "fresh"),
        (FRESH_MS, "fresh"),
        (FRESH_MS + 1, "delayed"),
        (60_000 - 1, "delayed"),
        (60_000, "delayed"),
        (60_000 + 1, "delayed"),
        (STALE_MS - 1, "delayed"),
        (STALE_MS, "delayed"),
        (STALE_MS + 1, "stale"),
        (10_000_000, "stale"),
        (-1, "unknown"),
        (None, "unknown"),
    ],
)
def test_freshness_state_boundaries(ms, expected):
    assert freshness_state(ms) == expected


def test_normalize_orderbook_levels():
    n = normalize_orderbook_levels({"enabled": True, "mode": "raw", "scale": "log", "width_px": 999})
    assert n["enabled"] is True
    assert n["mode"] == "raw"
    assert n["scale"] == "log"
    assert n["width_px"] == 220


def test_api_route_registered():
    from research_charts.api import build_router

    def require_auth():
        return {"username": "test"}

    def render_template(name, **ctx):
        return name

    router = build_router(require_auth=require_auth, render_template=render_template)
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/api/research/ob200-levels" in paths
    route = next(r for r in router.routes if getattr(r, "path", None) == "/api/research/ob200-levels")
    assert "GET" in (getattr(route, "methods", None) or set())


def test_iter_decompressed_objects_reads_closed_zstd():
    """Regression: ZstdDecompressionReader.readline raises UnsupportedOperation."""
    from pathlib import Path

    from research_charts.ob200_walls import DEFAULT_SHADOW_ROOT, iter_decompressed_objects

    root = Path(DEFAULT_SHADOW_ROOT) / "BTCUSDT"
    if not root.is_dir():
        pytest.skip("OB200 shadow archive not present")
    segs = sorted(root.rglob("*_ob200_v3.zst"))
    closed = [p for p in segs if "_open_" not in p.name and not p.name.endswith(".tmp")]
    if not closed:
        pytest.skip("no closed OB200 segments")
    n = 0
    types = set()
    for obj in iter_decompressed_objects(closed[-1]):
        n += 1
        types.add(obj.get("type"))
        if n >= 20:
            break
    assert n >= 1
    assert types & {"snapshot", "delta", "rotation_checkpoint"}


def test_load_ob200_levels_smoke_btc_doge():
    from research_charts.ob200_walls import has_ob200_archive

    for sym in ("BTCUSDT", "DOGEUSDT"):
        if not has_ob200_archive(sym):
            pytest.skip(f"no archive for {sym}")
        payload = load_ob200_levels(sym)
        assert payload["symbol"] == sym
        assert payload["source"] == "ob200_raw_shadow_v3"
        assert payload["depth"] == 200
        assert payload["sequence"] is None or isinstance(payload["sequence"], (int, str))
        assert isinstance(payload["timestamp_utc"], str)
        assert isinstance(payload["freshness_ms"], int)
        assert payload["freshness_state"] in {"fresh", "delayed", "stale", "unknown"}
        assert len(payload["bids"]) > 0 and len(payload["asks"]) > 0
        assert payload["bids"][0]["price"] >= payload["bids"][-1]["price"]
        assert payload["asks"][0]["price"] <= payload["asks"][-1]["price"]
        assert payload["bids"][0]["price"] < payload["asks"][0]["price"]
        assert all(math.isfinite(b["price"]) and math.isfinite(b["size"]) for b in payload["bids"])
        assert all(math.isfinite(a["price"]) and math.isfinite(a["size"]) for a in payload["asks"])


def test_repeated_request_uses_cache_not_full_replay(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    import research_charts.ob200_levels as mod

    replays = {"n": 0}
    orig = ob200_walls._replay_path

    def counted(ref, *, cutoff_ms):
        replays["n"] += 1
        return orig(ref, cutoff_ms=cutoff_ms)

    monkeypatch.setattr(ob200_walls, "_replay_path", counted)
    load_ob200_levels("BTCUSDT")
    load_ob200_levels("BTCUSDT")
    load_ob200_levels("BTCUSDT")
    assert replays["n"] == 1


def test_parallel_identical_requests_single_replay(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    import research_charts.ob200_levels as mod

    replays = {"n": 0}
    orig = ob200_walls._replay_path
    lock = threading.Lock()

    def counted(ref, *, cutoff_ms):
        with lock:
            replays["n"] += 1
        return orig(ref, cutoff_ms=cutoff_ms)

    monkeypatch.setattr(ob200_walls, "_replay_path", counted)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(load_ob200_levels, "BTCUSDT") for _ in range(3)]
        results = [f.result() for f in futs]
    assert replays["n"] == 1
    assert all(r["symbol"] == "BTCUSDT" for r in results)


def test_btc_doge_cache_isolated(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT") or not ob200_walls.has_ob200_archive("DOGEUSDT"):
        pytest.skip("archive missing")
    import research_charts.ob200_levels as mod

    replays = {"n": 0}
    orig = ob200_walls._replay_path

    def counted(ref, *, cutoff_ms):
        replays["n"] += 1
        return orig(ref, cutoff_ms=cutoff_ms)

    monkeypatch.setattr(ob200_walls, "_replay_path", counted)
    b = load_ob200_levels("BTCUSDT")
    d = load_ob200_levels("DOGEUSDT")
    assert b["symbol"] == "BTCUSDT"
    assert d["symbol"] == "DOGEUSDT"
    assert replays["n"] == 2
    load_ob200_levels("BTCUSDT")
    load_ob200_levels("DOGEUSDT")
    assert replays["n"] == 2


def test_symbol_switch_does_not_reuse_other_symbol_snapshot():
    if not ob200_walls.has_ob200_archive("BTCUSDT") or not ob200_walls.has_ob200_archive("DOGEUSDT"):
        pytest.skip("archive missing")
    btc = load_ob200_levels("BTCUSDT")
    doge = load_ob200_levels("DOGEUSDT")
    assert btc["symbol"] == "BTCUSDT"
    assert doge["symbol"] == "DOGEUSDT"
    assert btc["best_bid"] != doge["best_bid"]


def test_cache_is_bounded(monkeypatch):
    import research_charts.ob200_levels as mod

    monkeypatch.setattr(mod, "_LEVELS_CACHE_MAX", 2)
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    now = datetime.now(timezone.utc)
    for i in range(3):
        at = now - timedelta(seconds=i * 90)
        load_ob200_levels("BTCUSDT", at=at)
    with mod._levels_cache_lock:
        assert len(mod._levels_cache) <= 2


def test_historical_at_cache_key_exact_per_second(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    import research_charts.ob200_levels as mod

    replays = {"n": 0}
    orig = ob200_walls._replay_path

    def counted(ref, *, cutoff_ms):
        replays["n"] += 1
        return orig(ref, cutoff_ms=cutoff_ms)

    monkeypatch.setattr(ob200_walls, "_replay_path", counted)
    at = datetime(2026, 9, 1, 10, 30, tzinfo=timezone.utc)
    load_ob200_levels("BTCUSDT", at=at)
    load_ob200_levels("BTCUSDT", at=at + timedelta(seconds=10))
    assert replays["n"] == 2
    k0 = mod._cache_key("BTCUSDT", at, explicit_at=True)
    k1 = mod._cache_key("BTCUSDT", at + timedelta(seconds=10), explicit_at=True)
    assert k0 != k1


def test_historical_at_cache_hit_same_second(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    import research_charts.ob200_levels as mod

    replays = {"n": 0}
    orig = ob200_walls._replay_path

    def counted(ref, *, cutoff_ms):
        replays["n"] += 1
        return orig(ref, cutoff_ms=cutoff_ms)

    monkeypatch.setattr(ob200_walls, "_replay_path", counted)
    at = datetime(2026, 9, 1, 10, 30, 15, tzinfo=timezone.utc)
    load_ob200_levels("BTCUSDT", at=at)
    load_ob200_levels("BTCUSDT", at=at)
    assert replays["n"] == 1


def test_historical_at_no_wrong_cache_sharing():
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    base = datetime(2026, 9, 2, 10, 54, 30, tzinfo=timezone.utc)
    at0 = base
    at10 = base + timedelta(seconds=10)
    snap0 = ob200_walls.replay_book_as_of("BTCUSDT", at0, roots=[ob200_walls.DEFAULT_SHADOW_ROOT])
    snap10 = ob200_walls.replay_book_as_of("BTCUSDT", at10, roots=[ob200_walls.DEFAULT_SHADOW_ROOT])
    assert float(snap0["best_bid"]) != float(snap10["best_bid"]) or float(snap0["best_ask"]) != float(snap10["best_ask"])
    clear_ob200_levels_cache_for_tests()
    p0 = load_ob200_levels("BTCUSDT", at=at0)
    p10 = load_ob200_levels("BTCUSDT", at=at10)
    assert p0["best_bid"] == pytest.approx(float(snap0["best_bid"]))
    assert p10["best_bid"] == pytest.approx(float(snap10["best_bid"]))
    assert p0["timestamp_utc"] != p10["timestamp_utc"]


def test_explicit_at_uses_hist_cache_key():
    import research_charts.ob200_levels as mod

    at = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)
    key = mod._cache_key("BTCUSDT", at, explicit_at=True)
    assert key == ("BTCUSDT", "hist", int(at.timestamp()))


def test_live_cache_key_is_per_symbol_only():
    import research_charts.ob200_levels as mod

    at = datetime(2026, 9, 2, 11, 0, 0, tzinfo=timezone.utc)
    assert mod._cache_key("BTCUSDT", at, explicit_at=False) == ("BTCUSDT", "live")


def test_live_ttl_limits_replays(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    import research_charts.ob200_levels as mod

    replays = {"n": 0}
    orig = ob200_walls._replay_path

    def counted(ref, *, cutoff_ms):
        replays["n"] += 1
        return orig(ref, cutoff_ms=cutoff_ms)

    monkeypatch.setattr(ob200_walls, "_replay_path", counted)
    load_ob200_levels("BTCUSDT")
    load_ob200_levels("BTCUSDT")
    assert replays["n"] == 1


def test_source_timestamp_not_request_time(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    import research_charts.ob200_levels as mod

    p1 = load_ob200_levels("BTCUSDT")
    ts1 = p1["timestamp_utc"]
    p2 = load_ob200_levels("BTCUSDT")
    assert p2["timestamp_utc"] == ts1
    assert p2.get("cached") is True


def test_freshness_recomputed_on_cache_hit(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    fixed_now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    import research_charts.ob200_levels as mod

    monkeypatch.setattr(mod, "datetime", _FixedDateTime)
    p1 = load_ob200_levels("BTCUSDT")
    later = fixed_now + timedelta(seconds=120)
    monkeypatch.setattr(
        mod,
        "datetime",
        type(
            "LaterDT",
            (_FixedDateTime,),
            {"now": classmethod(lambda cls, tz=None: later if tz else later.replace(tzinfo=None))},
        ),
    )
    p2 = load_ob200_levels("BTCUSDT")
    assert p2["timestamp_utc"] == p1["timestamp_utc"]
    assert p2["freshness_ms"] > p1["freshness_ms"]


def test_last_good_on_transient_replay_error(monkeypatch):
    if not ob200_walls.has_ob200_archive("BTCUSDT"):
        pytest.skip("no BTC archive")
    import research_charts.ob200_levels as mod

    good = load_ob200_levels("BTCUSDT")
    ts = good["timestamp_utc"]
    with mod._levels_cache_lock:
        mod._levels_cache.clear()

    def boom(*args, **kwargs):
        raise ob200_walls.Ob200WallsError("ob200_invalid_book", "simulated partial read")

    monkeypatch.setattr(mod, "replay_book_as_of", boom)
    fb = load_ob200_levels("BTCUSDT")
    assert fb["data_status"] == "last_good"
    assert fb["timestamp_utc"] == ts
    assert fb["symbol"] == "BTCUSDT"
    assert fb["freshness_state"] in {"fresh", "delayed", "stale", "unknown"}


def test_no_last_good_raises_controlled_error(monkeypatch):
    import research_charts.ob200_levels as mod

    def boom(*args, **kwargs):
        raise ob200_walls.Ob200WallsError("ob200_missing", "no book")

    monkeypatch.setattr(mod, "replay_book_as_of", boom)
    with pytest.raises(ValueError, match="ob200_missing"):
        load_ob200_levels("UNKNOWNXYZ")


def test_audit_sorted_uncrossed_ob200():
    from research_charts.ob200_levels import audit_book_levels

    bids = [{"price": 100.0 - i * 0.1, "size": 1.0, "side": "bid"} for i in range(50)]
    asks = [{"price": 100.1 + i * 0.1, "size": 1.0, "side": "ask"} for i in range(50)]
    audit = audit_book_levels(bids, asks)
    assert audit["ok"] is True
    assert audit["sorted_bids"] and audit["sorted_asks"] and audit["uncrossed"]
    assert audit["best_bid"] < audit["best_ask"]
    assert abs(audit["mid"] - (audit["best_bid"] + audit["best_ask"]) / 2) < 1e-9
    assert all(b["price"] <= audit["best_bid"] for b in bids)
    assert all(a["price"] >= audit["best_ask"] for a in asks)


def test_audit_sorted_uncrossed_ob1000_gt_200():
    from research_charts.ob200_levels import audit_book_levels

    bids = [{"price": 77000.0 - i * 0.5, "size": float(i + 1), "side": "bid"} for i in range(250)]
    asks = [{"price": 77001.0 + i * 0.5, "size": float(i + 1), "side": "ask"} for i in range(250)]
    audit = audit_book_levels(bids, asks)
    assert audit["bid_count"] > 200 and audit["ask_count"] > 200
    assert audit["ok"] is True
    assert audit["best_bid"] < audit["best_ask"]


def test_aggregate_keeps_bid_ask_strictly_separated():
    bids = [{"price": 100.0, "size": 1.0, "side": "bid"}, {"price": 99.5, "size": 2.0, "side": "bid"}]
    asks = [{"price": 101.0, "size": 1.0, "side": "ask"}, {"price": 101.5, "size": 3.0, "side": "ask"}]
    out_b = aggregate_levels(bids + asks, bucket_size=1.0, side="bid")
    out_a = aggregate_levels(bids + asks, bucket_size=1.0, side="ask")
    assert all(x["side"] == "bid" for x in out_b)
    assert all(x["side"] == "ask" for x in out_a)
    assert max(x["price"] for x in out_b) < min(x["price"] for x in out_a)


def test_desync_up_when_chart_above_best_ask():
    from research_charts.ob200_levels import book_chart_sync_status

    # Screenshot-like: chart ~77138.60, ~8s-old book whose asks sit below.
    best_bid = 77050.0
    best_ask = 77051.0
    mid = (best_bid + best_ask) / 2.0
    chart = 77138.60
    sync = book_chart_sync_status(
        chart_price=chart,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        tick=0.1,
        freshness_ms=8000,
    )
    assert sync["sync_state"] == "DESYNC_UP"
    assert sync["misleading_as_live"] is True
    assert sync["delta"] == pytest.approx(chart - mid)
    assert abs(sync["delta_pct"]) > 0


def test_desync_down_when_chart_below_best_bid():
    from research_charts.ob200_levels import book_chart_sync_status

    sync = book_chart_sync_status(
        chart_price=76900.0,
        best_bid=77050.0,
        best_ask=77051.0,
        mid=77050.5,
        tick=0.1,
        freshness_ms=8000,
    )
    assert sync["sync_state"] == "DESYNC_DOWN"
    assert sync["misleading_as_live"] is True


def test_sync_when_chart_inside_spread_band():
    from research_charts.ob200_levels import book_chart_sync_status

    sync = book_chart_sync_status(
        chart_price=77050.4,
        best_bid=77050.0,
        best_ask=77051.0,
        mid=77050.5,
        tick=0.1,
        freshness_ms=2000,
    )
    assert sync["sync_state"] == "SYNC"
    assert sync["misleading_as_live"] is False


def test_delayed_not_desync_when_price_still_inside_book():
    from research_charts.ob200_levels import book_chart_sync_status

    sync = book_chart_sync_status(
        chart_price=77050.5,
        best_bid=77050.0,
        best_ask=77051.0,
        mid=77050.5,
        tick=0.1,
        freshness_ms=20_000,
    )
    assert sync["sync_state"] == "DELAYED"
    assert sync["misleading_as_live"] is False


def test_stale_overrides_price_position():
    from research_charts.ob200_levels import book_chart_sync_status

    sync = book_chart_sync_status(
        chart_price=77050.5,
        best_bid=77050.0,
        best_ask=77051.0,
        mid=77050.5,
        tick=0.1,
        freshness_ms=200_000,
    )
    assert sync["sync_state"] == "STALE"
    assert sync["misleading_as_live"] is True


def test_sync_tolerance_not_large_percent_band():
    from research_charts.ob200_levels import sync_tolerance

    mid = 77000.0
    tol = sync_tolerance(tick=0.1, best_bid=76999.9, best_ask=77000.1, mid=mid)
    assert tol < mid * 0.001
    assert tol >= 0.2


def test_visible_filter_after_aggregation_preserves_gt_200_raw():
    bids = [{"price": 100.0 - i * 0.01, "size": 1.0, "side": "bid"} for i in range(250)]
    asks = [{"price": 101.0 + i * 0.01, "size": 1.0, "side": "ask"} for i in range(250)]
    agg_b = aggregate_levels(bids, bucket_size=0.05, side="bid")
    agg_a = aggregate_levels(asks, bucket_size=0.05, side="ask")
    assert len(bids) > 200 and len(asks) > 200
    vis_low, vis_high = 99.5, 101.5
    vis_b = [x for x in agg_b if vis_low <= x["price"] <= vis_high]
    vis_a = [x for x in agg_a if vis_low <= x["price"] <= vis_high]
    assert len(vis_b) < len(agg_b) or len(vis_a) < len(agg_a)
    assert all(x["side"] == "bid" for x in vis_b)
    assert all(x["side"] == "ask" for x in vis_a)


def test_frontend_ob1000_wiring_and_desync_helpers():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    host = (root / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    chart = (root / "static/research_trp/chart.js").read_text(encoding="utf-8")
    css = (root / "static/research_trp/style.css").read_text(encoding="utf-8")
    pane = (root / "static/research_trp/pane.html").read_text(encoding="utf-8")
    html = (root / "templates/research_charts.html").read_text(encoding="utf-8")
    assert 'value="200"' in html and 'value="1000"' in html
    assert "OBL1000_REFRESH_MS = 1 * 1000" in host
    assert "OBL_REFRESH_MS = 5 * 1000" in host
    assert "pane.oblInflight" in host
    assert "isOb1000Mode" in host
    assert "oblBookChartSyncStatus" in chart
    assert "DESYNC_UP" in chart and "DESYNC_DOWN" in chart
    assert "misleading_as_live" in chart
    assert "oblFilterVisible" in chart
    assert "position: absolute" in css
    assert "ob-levels-2" in pane
    assert "debugOrderbookLevels" in chart


def test_price_to_coordinate_ordering_contract():
    mid = 100.0
    ask = 101.0
    bid = 99.0
    high, low, height = 110.0, 90.0, 400.0

    def y_of(price: float) -> float:
        return (high - price) / (high - low) * height

    assert y_of(ask) < y_of(mid) < y_of(bid)
