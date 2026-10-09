"""Settings guard: COMMON scanner engine read-only vs Research workspace LLD."""

from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

_repo = Path(__file__).resolve().parents[4]
_dash = _repo / "backtester" / "short"
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from pool_pattern.market import ensure_paths

ensure_paths()

from dashboard.research_charts.frozen_lld_config_v1 import (
    FROZEN_LLD_CONFIG_VERSION,
    HISTORY_WEEKS,
    PANE_FROM_UTC,
    frozen_lld_config_snapshot_v1,
)
from dashboard.research_charts.scanner_settings_guard_v1 import (
    READ_ONLY_SCANNER_ENGINE_MSG,
    reject_scanner_engine_settings_keys,
    scanner_pool_engine_payload,
    workspace_frozen_engine_parity,
)
from dashboard.research_charts.workspace_session import ResearchWorkspace

_TRP = Path("/home/telgenbuescher/projects/trading_research_platform").is_dir()
PANE_FROM_UNIX = int(PANE_FROM_UTC.timestamp())
NOW_UNIX = int(datetime.now(timezone.utc).timestamp())
TIMEFRAME = "15m"


class _AsgiClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def get(self, path: str):
        async def _go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(path)

        return asyncio.run(_go())

    def put(self, path: str, json: dict | None = None):
        async def _go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.put(path, json=json)

        return asyncio.run(_go())


def _router_client() -> _AsgiClient:
    from dashboard.research_charts.api import build_router

    app = FastAPI()
    app.include_router(
        build_router(
            require_auth=lambda: {"username": "guard-test"},
            render_template=lambda n, c: "",
        )
    )
    return _AsgiClient(app)


def test_scanner_pool_engine_payload_matches_frozen():
    payload = scanner_pool_engine_payload()
    frozen = frozen_lld_config_snapshot_v1(TIMEFRAME)
    assert payload["locked"] is True
    assert payload["source"] == "COMMON_FROZEN"
    assert payload["config_version"] == FROZEN_LLD_CONFIG_VERSION
    assert payload["timeframe"] == TIMEFRAME
    assert payload["enabled"] == frozen["enabled"]
    assert payload["amount_base"] == frozen["amount_base"]
    assert payload["amount_effective"] == frozen["amount_effective"]
    assert payload["highest_len"] == frozen["highest_len"]
    assert payload["lowest_len"] == frozen["lowest_len"]


def test_get_scanner_pool_engine_endpoint():
    r = _router_client().get("/api/research/scanner-pool-engine")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["locked"] is True
    assert body["source"] == "COMMON_FROZEN"


@pytest.mark.parametrize(
    "body",
    [
        {"scanner_engine": {"amount": 1}},
        {"frozen_config": {}},
        {"common_pool_config": {}},
        {"common_scanner_config": {}},
        {"liquidity": {"scanner_engine": True}},
    ],
)
def test_forbidden_settings_keys_raise(body: dict):
    with pytest.raises(ValueError, match="read-only"):
        reject_scanner_engine_settings_keys(body)


@pytest.mark.parametrize("body", [{"scanner_engine": {}}, {"frozen_config": {}}])
def test_put_settings_rejects_frozen_keys(body: dict):
    r = _router_client().put("/api/research/settings", json=body)
    assert r.status_code == 400
    data = r.json()
    assert READ_ONLY_SCANNER_ENGINE_MSG in str(data.get("message") or data.get("detail") or "")


def test_workspace_liquidity_apply_sets_research_affects():
    ws = ResearchWorkspace()
    base = deepcopy(ws.lld_config.to_dict())
    snap = ws.apply_settings(liquidity={**base, "amount": int(base.get("amount") or 2000)})
    assert snap.get("settings_affects") == ["research_lld_only"]


def test_workspace_frozen_parity_true_when_matching():
    from dashboard.research_charts import workspace_session as wsmod

    ws = ResearchWorkspace()
    trp = ws._trp
    frozen_snap = frozen_lld_config_snapshot_v1(TIMEFRAME)
    ws.lld_config = trp["LiquidityLocationConfig"].from_dict(
        {
            **ws.lld_config.to_dict(),
            "enabled": frozen_snap["enabled"],
            "amount": frozen_snap["amount_base"],
            "highest_len": frozen_snap["highest_len"],
            "lowest_len": frozen_snap["lowest_len"],
        }
    )
    wsmod._SESSION = ws
    try:
        parity = workspace_frozen_engine_parity()
        assert parity["workspace_frozen_parity"] is True
    finally:
        wsmod._SESSION = None


def test_workspace_frozen_parity_false_on_amount_drift():
    ws = ResearchWorkspace()
    trp = ws._trp
    frozen_snap = frozen_lld_config_snapshot_v1(TIMEFRAME)
    ws.lld_config = trp["LiquidityLocationConfig"].from_dict(
        {
            **ws.lld_config.to_dict(),
            "enabled": frozen_snap["enabled"],
            "amount": int(frozen_snap["amount_base"]) + 17,
            "highest_len": frozen_snap["highest_len"],
            "lowest_len": frozen_snap["lowest_len"],
        }
    )
    # workspace_frozen_engine_parity uses get_workspace singleton — set session
    from dashboard.research_charts import workspace_session as wsmod

    wsmod._SESSION = ws
    try:
        parity = workspace_frozen_engine_parity()
        assert parity["workspace_frozen_parity"] is False
        snap = ws.snapshot()
        assert snap["workspace_frozen_parity"] is False
    finally:
        wsmod._SESSION = None


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_common_rows_hash_unchanged_after_workspace_amount_change():
    from bot.common_causal_pools_v1 import common_causal_pool_universe
    from dashboard.research_charts.chart_scanner_pools_v1 import chart_scanner_pool_snapshot
    from dashboard.research_charts.lld_research_kernel import candle_stamps, load_pane_candles
    from pool_scan.clock import bar_close

    symbol = "DOGEUSDT"
    _packed, candles = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps = candle_stamps(candles)
    decision = bar_close(stamps[-1], TIMEFRAME)
    before_common = common_causal_pool_universe(symbol, decision)
    before_chart = chart_scanner_pool_snapshot(symbol, decision.isoformat())

    ws = ResearchWorkspace()
    ws.persist_settings = lambda: None  # type: ignore[method-assign]
    base = ws.lld_config.to_dict()
    ws.apply_settings(liquidity={**base, "amount": int(base.get("amount") or 2000) + 111})

    after_common = common_causal_pool_universe(symbol, decision)
    after_chart = chart_scanner_pool_snapshot(symbol, decision.isoformat())
    assert before_common.rows_hash == after_common.rows_hash
    assert before_chart["rows_hash"] == after_chart["rows_hash"]
    assert before_chart["rows_hash"] == before_common.rows_hash


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("field,value", [("highest_len", 9), ("lowest_len", 9)])
def test_common_unchanged_after_workspace_len_change(field: str, value: int):
    from bot.common_causal_pools_v1 import common_causal_pool_universe
    from dashboard.research_charts.lld_research_kernel import candle_stamps, load_pane_candles
    from pool_scan.clock import bar_close

    symbol = "XRPUSDT"
    _packed, candles = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps = candle_stamps(candles)
    decision = bar_close(stamps[-1], TIMEFRAME)
    before = common_causal_pool_universe(symbol, decision).rows_hash

    ws = ResearchWorkspace()
    ws.persist_settings = lambda: None  # type: ignore[method-assign]
    patch = {**ws.lld_config.to_dict(), field: value}
    ws.apply_settings(liquidity=patch)
    after = common_causal_pool_universe(symbol, decision).rows_hash
    assert before == after


def test_research_lld_layer_can_change_workspace_amount():
    ws = ResearchWorkspace()
    before = int(ws.lld_config.amount)
    snap = ws.apply_settings(liquidity={**ws.lld_config.to_dict(), "amount": before + 50})
    assert int(snap["liquidity"]["amount"]) == before + 50


def test_html_contains_scanner_engine_locked_block():
    html_path = Path(__file__).resolve().parents[3] / "dashboard" / "templates" / "research_charts.html"
    html = html_path.read_text(encoding="utf-8")
    assert "Scanner Pool Engine (LOCKED)" in html
    assert "Scanner pool parameters are frozen and cannot be changed from the chart." in html
    assert "Research LLD Settings" in html
    js = (Path(__file__).resolve().parents[3] / "dashboard" / "static" / "js" / "research" / "research_charts.js").read_text(
        encoding="utf-8"
    )
    assert "Scanner Pool Engine (LOCKED)" in js or "fillScannerEngineLocked" in js
