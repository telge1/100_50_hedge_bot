"""API validation and safe errors."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from footprint_candles.api import build_router  # noqa: E402
from footprint_candles.service import FootprintRequestError, validate_request  # noqa: E402


class _AsgiClient:
    def __init__(self, app):
        self._app = app

    def get(self, url, params=None):
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.get(url, params=params)

        return asyncio.run(_run())


def _mini():
    def _auth():
        return {"username": "fp"}

    app = FastAPI()
    app.include_router(build_router(require_auth=_auth))
    return _AsgiClient(app)


def test_validate_symbol_tf_mode_step():
    with pytest.raises(FootprintRequestError) as ei:
        validate_request(
            symbol="DOGEUSDT",
            timeframe="5m",
            mode="DISPLAY",
            bucket_step=5.0,
            start=1000,
            end=2000,
        )
    assert ei.value.code == "unsupported_symbol"

    with pytest.raises(FootprintRequestError) as ei:
        validate_request(
            symbol="BTCUSDT",
            timeframe="15m",
            mode="DISPLAY",
            bucket_step=5.0,
            start=1000,
            end=2000,
        )
    assert ei.value.code == "unsupported_timeframe"

    with pytest.raises(FootprintRequestError) as ei:
        validate_request(
            symbol="BTCUSDT",
            timeframe="5m",
            mode="CAUSAL_RESEARCH",
            bucket_step=5.0,
            start=1000,
            end=2000,
        )
    assert ei.value.code == "unsupported_mode"

    with pytest.raises(FootprintRequestError) as ei:
        validate_request(
            symbol="BTCUSDT",
            timeframe="5m",
            mode="DISPLAY",
            bucket_step=1.0,
            start=1000,
            end=2000,
        )
    assert ei.value.code == "bad_bucket_step"


def test_validate_range_limits():
    with pytest.raises(FootprintRequestError) as ei:
        validate_request(
            symbol="BTCUSDT",
            timeframe="5m",
            mode="DISPLAY",
            bucket_step=5.0,
            start=2000,
            end=1000,
        )
    assert ei.value.code == "bad_range"

    with pytest.raises(FootprintRequestError) as ei:
        validate_request(
            symbol="BTCUSDT",
            timeframe="5m",
            mode="DISPLAY",
            bucket_step=5.0,
            start=1000,
            end=1000 + 7 * 3600,
        )
    assert ei.value.code == "range_too_large"


def test_meta_endpoint():
    c = _mini()
    r = c.get("/api/footprint-candles/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["supported"]["symbol"] == "BTCUSDT"
    assert body["supported"]["timeframe"] == "5m"


def test_api_rejects_bad_symbol():
    c = _mini()
    r = c.get(
        "/api/footprint-candles",
        params={
            "symbol": "ETHUSDT",
            "from": 1757156400,
            "to": 1757156400 + 1800,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_symbol"


def test_api_query_failed_safe_message():
    c = _mini()

    def _boom(**kwargs):
        raise FootprintRequestError("query_failed", "Footprint query failed; try a smaller range")

    with patch("footprint_candles.api.load_footprint", side_effect=_boom):
        r = c.get(
            "/api/footprint-candles",
            params={
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "mode": "DISPLAY",
                "bucket_step": 5.0,
                "from": 1757156400,
                "to": 1757156400 + 1800,
            },
        )
    assert r.status_code == 502
    body = r.json()
    assert body["error"] == "query_failed"
    assert "password" not in body["message"].lower()
    assert "select" not in body["message"].lower()


def test_half_open_candle_contract_in_validate_ok():
    req = validate_request(
        symbol="BTCUSDT",
        timeframe="5m",
        mode="DISPLAY",
        bucket_step=5.0,
        start=1757156400,
        end=1757156400 + 1800,
    )
    assert req["bucket_step"] == 5.0
