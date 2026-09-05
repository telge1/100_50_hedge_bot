"""Unit tests for OI 5m backfill helpers + health contract (offline)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from collector_health.contract import sanitize_json
from collector_health.csrf import issue_csrf_token, mutate_post_guard, validate_csrf_token
from collector_health.oi_backfill import (
    expected_closed_buckets,
    fetch_all_pages,
    floor_5m,
    last_closed_5m,
    normalize_symbol,
    parse_rest_item,
    rows_for_insert,
    RestOiPoint,
)
from collector_health.jobs import validate_backfill_request


def test_sanitize_nan_inf():
    assert sanitize_json(float("nan")) is None
    assert sanitize_json(float("inf")) is None
    assert sanitize_json({"a": float("nan"), "b": [1.0, float("-inf")]}) == {"a": None, "b": [1.0, None]}


def test_expected_buckets_3_days():
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 3, 23, 55, tzinfo=timezone.utc)
    # Monkey: last_closed far in future relative to end by using end before now
    buckets = expected_closed_buckets(start, end)
    # 3 days inclusive of closed 5m from 00:00 Aug1 through min(end_floor, last_closed)
    # Aug1 00:00 .. Aug3 23:55 → if last_closed >= Aug3 23:55: 
    # from 0 to 23:55 on day3 = 3*24*12 = 864 buckets if end included as 23:55
    assert len(buckets) == 864


def test_last_closed_not_forming():
    # Construct: at 12:03, last closed start is 11:55
    fixed = datetime(2026, 9, 4, 12, 3, 0, tzinfo=timezone.utc)
    # patch via floor math locally
    floored = floor_5m(fixed)
    assert floored == datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    closed = floored - timedelta(minutes=5)
    assert closed.minute == 55


def test_parse_rest_open_interest_only():
    pt = parse_rest_item(
        {"openInterest": "12.5", "singleOpenInterest": "6.25", "timestamp": "1720000000000"}
    )
    assert pt is not None
    assert pt.open_interest == Decimal("12.5")
    assert pt.single_open_interest == Decimal("6.25")
    assert parse_rest_item({"timestamp": "1"}) is None  # no OI


def test_rows_only_missing_closed(monkeypatch):
    closed = datetime(2026, 8, 18, 15, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "collector_health.oi_backfill.last_closed_5m", lambda now=None: closed
    )
    pts = [
        RestOiPoint(
            timestamp_ms=int(closed.timestamp() * 1000),
            open_interest=Decimal("1"),
            single_open_interest=None,
            bucket_time=closed,
        ),
        RestOiPoint(
            timestamp_ms=int((closed + timedelta(minutes=5)).timestamp() * 1000),
            open_interest=Decimal("2"),
            single_open_interest=None,
            bucket_time=closed + timedelta(minutes=5),
        ),
    ]
    rows = rows_for_insert(
        "BTCUSDT",
        pts,
        instance_id="t",
        allow_buckets={closed},
    )
    assert len(rows) == 1
    assert rows[0]["source"] == "BYBIT_REST_5M_HISTORY"
    assert rows[0]["open_interest_value"] is None


def test_pagination_over_200(monkeypatch):
    pages = {
        "": ([{"openInterest": str(i), "timestamp": str(1_700_000_000_000 + i * 300_000)} for i in range(200)], "c1"),
        "c1": ([{"openInterest": "200", "timestamp": str(1_700_000_000_000 + 200 * 300_000)}], ""),
    }

    def fake_fetch(rest_url, *, symbol, start_ms, end_ms, cursor=""):
        return pages[cursor]

    monkeypatch.setattr("collector_health.oi_backfill.fetch_open_interest_page", fake_fetch)
    monkeypatch.setattr("collector_health.oi_backfill.time.sleep", lambda *_: None)
    pts = fetch_all_pages("https://api.bybit.com", symbol="BTCUSDT", start_ms=1, end_ms=9)
    assert len(pts) == 201


def test_symbol_reject_shell():
    with pytest.raises(ValueError):
        normalize_symbol("BTCUSDT; rm -rf /")


def test_csrf_roundtrip():
    now = 1_725_000_000.0
    tok = issue_csrf_token(now=now)
    assert validate_csrf_token(tok, now=now + 10)
    assert not validate_csrf_token(tok, now=now + 7200)
    # mutate_post_guard uses wall clock — issue live token
    live = issue_csrf_token()
    assert (
        mutate_post_guard(
            origin="https://dash.immotel.de",
            referer=None,
            content_type="application/json",
            csrf_header=live,
            csrf_cookie=live,
        )
        is None
    )
    assert (
        mutate_post_guard(
            origin="https://evil.example",
            referer=None,
            content_type="application/json",
            csrf_header=live,
            csrf_cookie=live,
        )
        == "ORIGIN_FORBIDDEN"
    )


def test_pt_backfill_blocked():
    parsed, err = validate_backfill_request(
        {
            "collector_id": "public_trades_live",
            "job_kind": "detect",
            "symbols": ["BTCUSDT"],
            "start": "2026-09-01T00:00:00Z",
            "end": "2026-09-02T00:00:00Z",
        }
    )
    assert parsed is None
    assert err and "PUBLIC_TRADES_BLOCKED" in err


def test_execute_fail_closed(monkeypatch):
    monkeypatch.setattr("collector_health.jobs.ALLOW_OI_EXECUTE", False)
    parsed, err = validate_backfill_request(
        {
            "job_kind": "oi_5m_backfill_execute",
            "symbols": ["BTCUSDT"],
            "start": "2026-09-01T00:00:00Z",
            "end": "2026-09-02T00:00:00Z",
        }
    )
    assert parsed is None
    assert err == "OI_EXECUTE_FAIL_CLOSED"


def test_health_pid_alive_db_stale(monkeypatch):
    from collector_health import service as svc

    monkeypatch.setattr(
        svc,
        "probe_oi_process",
        lambda: {
            "process_running": True,
            "pid": 147111,
            "process_started_at": "2026-08-18T00:00:00Z",
            "cmdline": "oi_liquidation_collector",
        },
    )
    old = datetime(2026, 9, 1, 16, 46, 50, tzinfo=timezone.utc)

    def fake_q(sql, parameters=None):
        return [(old, old, old, old, old)]

    monkeypatch.setattr(svc, "_ch_query", fake_q)
    row = svc._build_oi_live()
    assert row["process_running"] is True
    assert row["status"] == "STALE"
    assert row["status"] != "HEALTHY"


def test_full_ob_stopped(monkeypatch):
    from collector_health import service as svc

    monkeypatch.setattr(
        svc,
        "probe_full_ob_raw",
        lambda: {
            "process_running": False,
            "pid": None,
            "process_started_at": None,
            "health_state": "STOPPED",
            "connected": False,
            "last_error": "stale_market_data",
            "lock_present": False,
        },
    )
    row = svc._build_full_ob()
    assert row["status"] == "STOPPED"


def test_pt_degraded_with_drops(monkeypatch):
    from collector_health import service as svc

    monkeypatch.setattr(
        svc,
        "probe_stoch_process",
        lambda: {"process_running": True, "pid": 1, "process_started_at": None},
    )
    monkeypatch.setattr(
        svc,
        "probe_stoch_status",
        lambda timeout_s=3.0: {
            "ok": True,
            "data": {
                "websocket_connected": True,
                "public_trades_enabled": True,
                "public_trade_symbols": ["BTCUSDT"] * 51,
                "public_trade_metrics": {
                    "lag_seconds": 0.1,
                    "dropped_events": 493019,
                    "queue_depth": 2,
                    "insert_failures": 0,
                    "last_error": "queue_full_dropped_event",
                    "last_trade_event_ts": "2026-09-04T17:00:00Z",
                    "last_trade_ingest_ts": "2026-09-04T17:00:00Z",
                },
            },
        },
    )
    row = svc._build_public_trades()
    assert row["status"] == "DEGRADED"
    assert "DATA LOSS POSSIBLE" in row["evidence"]
