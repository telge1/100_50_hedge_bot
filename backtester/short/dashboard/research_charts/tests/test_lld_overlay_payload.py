"""Offline golden fixtures + parity for LLD slim overlay projection.

No ClickHouse, no dashboard.app, no network, no live services.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_charts.lld_overlay_payload import (  # noqa: E402
    CONTRACT_VERSION,
    assert_no_candles,
    extract_mp_consumed_lld_state,
    measure_payload_bytes,
    project_lld_overlay_response,
)


def _zone(
    oid: str,
    *,
    direction: str = "support",
    y1: float = 100.0,
    y2: float = 101.0,
    t1: int = 1_700_000_000,
    t2: int = 1_700_086_400,
    status: str = "active",
    color: str = "#26a69a",
) -> dict:
    return {
        "id": oid,
        "namespace": "LLD",
        "type": "rectangle",
        "points": [{"time": t1, "price": y1}, {"time": t2, "price": y2}],
        "style": {"color": color, "fillColor": color},
        "metadata": {
            "source": "liquidity_location",
            "direction": direction,
            "status": status,
            "pool_id": oid,
        },
    }


def _drawing(oid: str = "draw:1") -> dict:
    return {
        "id": oid,
        "namespace": "USER_DRAWING",
        "type": "trend_line",
        "points": [{"time": 1, "price": 1}, {"time": 2, "price": 2}],
        "metadata": {"source": "drawing", "drawing_id": oid},
    }


def _candles(n: int, *, start: int = 1_700_000_000, step: int = 900) -> list[dict]:
    out = []
    px = 100.0
    for i in range(n):
        t = start + i * step
        o = px
        c = px + (0.1 if i % 2 == 0 else -0.05)
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        out.append(
            {
                "time": t,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(l, 4),
                "close": round(c, 4),
                "volume": 10 + (i % 7),
            }
        )
        px = c
    return out


def _ema_payload(n: int = 5) -> dict:
    return {
        "fast": [{"time": 1_700_000_000 + i * 900, "value": 100 + i * 0.01} for i in range(n)],
        "slow": [{"time": 1_700_000_000 + i * 900, "value": 99 + i * 0.01} for i in range(n)],
        "fast_visible": True,
        "slow_visible": True,
        "fast_color": "#f0b90b",
        "slow_color": "#3b82f6",
    }


def build_pane_fixture(
    name: str,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "15m",
    candle_count: int = 100,
    zones: list[dict] | None = None,
    include_drawing: bool = True,
    clusters: dict | None = None,
    as_of: str | None = None,
    mode: str = "live",
    coverage_incomplete: bool = False,
    error: dict | None = None,
    workspace_liquidity: dict | None = None,
) -> dict:
    candles = _candles(candle_count)
    if zones is None:
        zones = [
            _zone("lld:1"),
            _zone("lld:2", direction="resistance", y1=110, y2=111, color="#ef5350"),
        ]
    overlays: list[dict] = []
    if include_drawing:
        overlays.append(_drawing())
    overlays.extend(zones)
    start = candles[0]["time"] if candles else 1_700_000_000
    end = candles[-1]["time"] if candles else start
    lld_ema = _ema_payload(min(8, max(1, candle_count // 20 or 1)))
    if clusters is None:
        clusters = {"3": 1, "4-5": 0, "6+": 0}
    meta = {
        "mode": mode,
        "liquidity_location_as_of": as_of,
        "canonical_snapshot_sha256": None if coverage_incomplete else "abc123",
        "coverage_complete": not coverage_incomplete,
    }
    if error:
        meta["error"] = error
    liq_cfg = workspace_liquidity or {"enabled": True, "lookback": 20}
    return {
        "success": True,
        "message": "feed",
        "feed_ready": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "from": start,
        "to": end,
        "candles": candles,
        "ema": {"series": [{"length": 9, "values": [1, 2, 3]}]},
        "stochastic": {"enabled": False, "k": [], "d": []},
        "open_interest": {"enabled": False, "series": []},
        "overlays": overlays,
        "lld_ema": lld_ema,
        "clusters": clusters,
        "liquidity": {
            "overlays": zones,
            "ema": lld_ema,
            "clusters": clusters,
            "liquidity_location": meta,
        },
        "liquidity_location_mode": meta["mode"],
        "liquidity_location_as_of": meta["liquidity_location_as_of"],
        "canonical_snapshot_sha256": meta["canonical_snapshot_sha256"],
        "fixture_name": name,
        "liquidity_config": liq_cfg,
    }


def slim_from_pane(pane: dict) -> dict:
    return project_lld_overlay_response(
        symbol=pane["symbol"],
        timeframe=pane["timeframe"],
        start=pane.get("from"),
        end=pane.get("to"),
        overlays=pane.get("overlays"),
        lld_ema=pane.get("lld_ema"),
        clusters=pane.get("clusters"),
        liquidity_meta=(pane.get("liquidity") or {}).get("liquidity_location") or {},
        lld_serialized=(pane.get("liquidity") or {}).get("overlays"),
        liquidity_config=pane.get("liquidity_config"),
    )


def parity_core(state: dict) -> dict:
    return {
        "ordered_ids": state["ordered_ids"],
        "lld_payloads": state["lld_payloads"],
        "lld_ema": state["lld_ema"],
        "clusters": state["clusters"],
        "liquidity_location_mode": state["liquidity_location_mode"],
        "liquidity_location_as_of": state["liquidity_location_as_of"],
        "canonical_snapshot_sha256": state["canonical_snapshot_sha256"],
        "symbol": state["symbol"],
        "timeframe": state["timeframe"],
        "from": state["from"],
        "to": state["to"],
    }


CASES = [
    ("normal_zones", {}),
    ("no_zones", {"zones": []}),
    ("one_zone", {"zones": [_zone("lld:only")]}),
    (
        "overlapping_zones",
        {
            "zones": [
                _zone("lld:a", y1=100, y2=105),
                _zone("lld:b", y1=103, y2=108, direction="resistance", color="#ef5350"),
            ]
        },
    ),
    (
        "support_and_resistance",
        {
            "zones": [
                _zone("lld:sup", direction="support"),
                _zone("lld:res", direction="resistance", y1=120, y2=121, color="#ef5350"),
            ]
        },
    ),
    ("zone_at_range_start", {"zones": [_zone("lld:start", t1=1_700_000_000, t2=1_700_000_900)]}),
    (
        "zone_at_range_end",
        {
            "candle_count": 50,
            "zones": [
                _zone(
                    "lld:end",
                    t1=1_700_000_000 + 49 * 900,
                    t2=1_700_000_000 + 49 * 900,
                )
            ],
        },
    ),
    ("developing", {"zones": [_zone("lld:dev", status="developing")]}),
    ("as_of_inside_last", {"as_of": "2023-11-14T22:00:00+00:00", "mode": "causal_as_of"}),
    ("incomplete_coverage", {"coverage_incomplete": True}),
    ("fachlicher_fehler", {"error": {"code": "lld_failed", "message": "engine error"}, "zones": []}),
    ("empty_candles", {"candle_count": 0, "zones": []}),
    ("tf_5m", {"timeframe": "5m", "candle_count": 40}),
    ("symbol_eth", {"symbol": "ETHUSDT"}),
    (
        "workspace_param_changed",
        {"workspace_liquidity": {"enabled": True, "lookback": 55, "cluster_gap_pct": 0.15}},
    ),
]


@pytest.fixture(scope="module")
def golden_panes():
    return {name: build_pane_fixture(name, **kwargs) for name, kwargs in CASES}


@pytest.fixture(scope="module")
def large_pane():
    zones = [
        _zone(
            f"lld:{i}",
            direction="support" if i % 2 == 0 else "resistance",
            y1=100 + i * 0.5,
            y2=100.4 + i * 0.5,
            color="#26a69a" if i % 2 == 0 else "#ef5350",
            t1=1_700_000_000 + i * 3600,
            t2=1_700_000_000 + i * 3600 + 86_400,
        )
        for i in range(48)
    ]
    return build_pane_fixture("large_2879", candle_count=2879, zones=zones)


@pytest.mark.parametrize("name,_kwargs", CASES)
def test_pane_to_slim_parity(name, _kwargs, golden_panes):
    pane = golden_panes[name]
    slim = slim_from_pane(pane)
    assert_no_candles(slim)
    assert slim["contract_version"] == CONTRACT_VERSION
    assert "candles" not in slim
    # chart EMA / stoch / OI must not be echoed
    assert "ema" not in slim
    assert "stochastic" not in slim
    assert "open_interest" not in slim
    pane_state = extract_mp_consumed_lld_state(pane)
    slim_state = extract_mp_consumed_lld_state(slim)
    assert parity_core(pane_state) == parity_core(slim_state)


def test_large_fixture_payload_reduction(large_pane):
    slim = slim_from_pane(large_pane)
    assert_no_candles(slim)
    before = measure_payload_bytes(large_pane)
    after = measure_payload_bytes(slim)
    assert before["candle_count"] == 2879
    assert after["candle_count"] == 0
    assert after["has_candles_key"] is False
    assert before["candle_bytes"] > 0
    savings = before["total_bytes"] - after["total_bytes"]
    pct = 100.0 * savings / before["total_bytes"]
    assert pct >= 70.0, (
        f"DASHBOARD_LLD_SLIM_PARTIAL_TARGET_NOT_MET: {pct:.1f}% "
        f"(pane={before['total_bytes']} slim={after['total_bytes']} candles={before['candle_bytes']})"
    )
    assert parity_core(extract_mp_consumed_lld_state(large_pane)) == parity_core(
        extract_mp_consumed_lld_state(slim)
    )
    FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "large_pane_meta.json").write_text(
        json.dumps(
            {
                "pane_bytes": before["total_bytes"],
                "candle_bytes": before["candle_bytes"],
                "slim_bytes": after["total_bytes"],
                "savings_bytes": savings,
                "savings_pct": round(pct, 2),
                "candle_count_pane": before["candle_count"],
                "candle_count_slim": after["candle_count"],
                "field_count_pane": before["field_count"],
                "field_count_slim": after["field_count"],
            },
            indent=2,
        )
        + "\n"
    )


def test_old_pane_fields_preserved_in_fixture_contract(golden_panes):
    pane = golden_panes["normal_zones"]
    for key in (
        "candles",
        "ema",
        "stochastic",
        "open_interest",
        "overlays",
        "lld_ema",
        "clusters",
        "liquidity",
    ):
        assert key in pane


def test_service_shared_section_parity(monkeypatch):
    """pane_bundle and liquidity_location_overlay_bundle share LLD section (mocked I/O)."""
    from research_charts import service as svc

    candles_payload = _candles(30)
    packed = {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "from": candles_payload[0]["time"],
        "to": candles_payload[-1]["time"],
        "candles": candles_payload,
        "source": "test",
        "feed_ready": True,
    }
    zones = [_zone("lld:x"), _zone("lld:y", direction="resistance", y1=110, y2=111)]
    lld_ema = _ema_payload(3)
    clusters = {"3": 2, "4-5": 0, "6+": 0}
    meta = {"mode": "live", "liquidity_location_as_of": None, "canonical_snapshot_sha256": "x"}

    class FakeWs:
        ema_config = type("C", (), {"to_dict": lambda self: {"enabled": False, "lines": []}})()
        stoch_config = type("C", (), {"to_dict": lambda self: {"enabled": False}})()
        open_interest = {"enabled": False}
        lld_config = type(
            "C",
            (),
            {
                "to_dict": lambda self: {"enabled": True},
                "enabled": True,
            },
        )()

        def composed_overlays(self, symbol, timeframe, lld_overlays=None):
            items = [_drawing()]
            if lld_overlays:
                # serialize path normally converts objs; here pass zones directly
                items.extend(zones)
            else:
                items.extend(zones)
            return items

        def lld_objects(self, candles, config=None):
            return zones, lld_ema, clusters

    monkeypatch.setattr(svc, "resolve_candle_pack", lambda *a, **k: packed)
    monkeypatch.setattr(svc, "_candles_from_packed", lambda packed, allow_stale=True: ["c"] * len(packed["candles"]))
    monkeypatch.setattr(
        svc,
        "_indicators_from_candles",
        lambda *a, **k: {
            "ema": {"series": []},
            "stochastic": {"enabled": False},
            "open_interest": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        svc,
        "load_trp",
        lambda: {
            "LiquidityLocationConfig": type(
                "LC",
                (),
                {"from_dict": staticmethod(lambda d: type("Cfg", (), {"enabled": True})())},
            ),
            "serialize_overlays": lambda objs: list(objs) if objs else [],
        },
    )

    import research_charts.workspace_session as ws_mod

    monkeypatch.setattr(ws_mod, "get_workspace", lambda: FakeWs())

    pane = svc.pane_bundle("BTCUSDT", "15m", start=1, end=2, liquidity={"enabled": True})
    slim = svc.liquidity_location_overlay_bundle(
        "BTCUSDT", "15m", start=1, end=2, liquidity={"enabled": True}
    )
    assert "candles" in pane
    assert "candles" not in slim
    assert parity_core(extract_mp_consumed_lld_state(pane)) == parity_core(
        extract_mp_consumed_lld_state(slim)
    )
    assert slim["contract_version"] == CONTRACT_VERSION


def test_router_registers_liquidity_location_with_auth():
    from research_charts.api import build_router

    def require_auth():
        return {"id": "u1", "role": "admin"}

    def render_template(name, ctx):
        return "<html></html>"

    router = build_router(require_auth=require_auth, render_template=render_template)
    paths = {getattr(route, "path", None) for route in router.routes}
    methods = {
        getattr(route, "path", None): set(getattr(route, "methods", []) or [])
        for route in router.routes
    }
    assert "/api/research/pane" in paths
    assert "/api/research/liquidity-location" in paths
    assert "POST" in methods["/api/research/liquidity-location"]
    assert "POST" in methods["/api/research/pane"]

    # Both endpoints use the injected require_auth dependency (same auth contract).
    slim_route = next(r for r in router.routes if getattr(r, "path", None) == "/api/research/liquidity-location")
    pane_route = next(r for r in router.routes if getattr(r, "path", None) == "/api/research/pane")
    slim_deps = [d.call for d in (getattr(slim_route, "dependant", None).dependencies or [])]
    pane_deps = [d.call for d in (getattr(pane_route, "dependant", None).dependencies or [])]
    assert require_auth in slim_deps
    assert require_auth in pane_deps
    assert slim_deps == pane_deps or require_auth in slim_deps

    src = Path(__file__).resolve().parents[1] / "api.py"
    slim_src = src.read_text()
    assert "api_research_liquidity_location" in slim_src
    assert "liquidity_location_overlay_bundle" in slim_src
    assert "@router.post(\"/api/research/pane\")" in slim_src or '@router.post("/api/research/pane")' in slim_src


def test_write_small_fixture_files(golden_panes, large_pane):
    FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    sample = golden_panes["normal_zones"]
    (FIXTURE_DIR / "pane_normal_zones.json").write_text(
        json.dumps(sample, separators=(",", ":")) + "\n"
    )
    (FIXTURE_DIR / "slim_normal_zones.json").write_text(
        json.dumps(slim_from_pane(sample), separators=(",", ":")) + "\n"
    )
    # Large pane is huge; store meta + slim only
    slim = slim_from_pane(large_pane)
    (FIXTURE_DIR / "slim_large_2879.json").write_text(json.dumps(slim, separators=(",", ":")) + "\n")
