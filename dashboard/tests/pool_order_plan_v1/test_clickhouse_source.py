from __future__ import annotations

import pytest

from pool_order_plan_v1 import overlay, store
from pool_order_plan_v1.batch import BatchAbort, run_batch
from pool_order_plan_v1.schema import clickhouse_candle_stamp
from pool_order_plan_v1.store import SourceRejected


def _ch_manifest(**extra):
    m = clickhouse_candle_stamp()
    m.update({"ok": True, **extra})
    return m


def _fixture_manifest():
    return {
        "ok": True,
        "pool_candle_source": "TEST_FIXTURE_ONLY",
        "test_fixture_only": True,
        "TEST_FIXTURE_ONLY": True,
    }


def test_clickhouse_plan_may_write_smoke_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    run = store.write_run(
        "ch-smoke",
        manifest=_ch_manifest(),
        preflight={},
        coverage={},
        plans=[{"signal_id": "s1", **clickhouse_candle_stamp()}],
        outcomes=[{"signal_id": "s1", "plan_status": "READY", "tp1_price": 1.0}],
        ignored=[],
    )
    store.publish_latest(run)
    assert store.artifact_available()
    idx = store.load_latest_index()
    assert idx["s1"]["tp1_price"] == 1.0


def test_csv_fixture_plan_must_not_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    run = store.write_run(
        "csv-fixture",
        manifest=_fixture_manifest(),
        preflight={},
        coverage={},
        plans=[],
        outcomes=[{"signal_id": "s1", "tp1_price": 99.0}],
        ignored=[],
    )
    with pytest.raises(SourceRejected):
        store.publish_latest(run)
    assert not store.artifact_available()
    assert not (tmp_path / "latest").exists()


def test_csv_fixture_plan_must_not_update_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    ch = store.write_run(
        "ch-ok",
        manifest=_ch_manifest(),
        preflight={},
        coverage={},
        plans=[],
        outcomes=[{"signal_id": "keep", "tp1_price": 2.0}],
        ignored=[],
    )
    store.publish_latest(ch)
    csv_run = store.write_run(
        "csv-later",
        manifest=_fixture_manifest(),
        preflight={},
        coverage={},
        plans=[],
        outcomes=[{"signal_id": "keep", "tp1_price": 99.0}],
        ignored=[],
    )
    with pytest.raises(SourceRejected):
        store.publish_latest(csv_run)
    assert (tmp_path / "latest").resolve().name == "ch-ok"
    assert store.load_latest_index()["keep"]["tp1_price"] == 2.0


def test_csv_cannot_write_under_production_results(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(store, "production_artifacts_dir", lambda: tmp_path)
    with pytest.raises(SourceRejected, match="non-ClickHouse"):
        store.write_run(
            "blocked",
            manifest=_fixture_manifest(),
            preflight={},
            coverage={},
            plans=[],
            outcomes=[],
            ignored=[],
        )


def test_overlay_rejects_missing_clickhouse_source(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    run = store.write_run(
        "no-stamp",
        manifest={"ok": True, "pool_candle_source": "csv"},
        preflight={},
        coverage={},
        plans=[],
        outcomes=[{"signal_id": "s1", "plan_status": "READY", "tp1_price": 50.0, "sl_price": 1.0}],
        ignored=[],
    )
    latest = tmp_path / "latest"
    latest.symlink_to(run.name)
    assert not store.artifact_available()
    rows = overlay.overlay_rows(
        [{"signal_id": "s1", "tp_price": 10.0, "sl_price": 9.0}]
    )
    assert rows[0]["no_plan_reason"] == "POOL_CANDLE_SOURCE_NOT_CLICKHOUSE"
    assert rows[0]["tp1_price"] is None
    assert rows[0]["pool_sl_price"] is None


def test_dashboard_never_shows_csv_pool_plans(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("ENABLE_POOL_ORDER_PLAN_V1", "true")
    run = store.write_run(
        "csv-dash",
        manifest=_fixture_manifest(),
        preflight={},
        coverage={},
        plans=[],
        outcomes=[
            {
                "signal_id": "s1",
                "plan_status": "READY",
                "tp1_price": 77.0,
                "sl_price": 11.0,
                "net_pnl_pct": 5.0,
            }
        ],
        ignored=[],
    )
    (tmp_path / "latest").symlink_to(run.name)
    assert store.artifact_available() is False
    assert overlay.overlay_enabled() is False
    rows = overlay.overlay_rows([{"signal_id": "s1", "result": "OPEN"}])
    assert rows[0]["tp1_price"] is None
    assert rows[0]["no_plan_reason"] == "POOL_CANDLE_SOURCE_NOT_CLICKHOUSE"


def test_batch_without_clickhouse_aborts_no_csv_fallback(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("pool_order_plan_v1.batch.load_closed_1m", _boom)
    with pytest.raises(BatchAbort, match="CSV fallback is forbidden"):
        run_batch(
            signals=[
                {
                    "signal_id": "s1",
                    "symbol": "HYPEUSDT",
                    "direction": "LONG",
                    "timeframe": "15m",
                    "entry_time": "2026-08-11T01:17:00Z",
                    "entry_price": 54.91,
                    "available_at": "2026-08-11T01:15:00Z",
                    "created_at": "2026-08-11T01:15:00Z",
                }
            ],
            skip_pin=True,
            publish=False,
        )


def test_fixture_batch_cannot_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    with pytest.raises(BatchAbort, match="TEST_FIXTURE_ONLY"):
        run_batch(
            signals=[
                {
                    "signal_id": "s1",
                    "symbol": "HYPEUSDT",
                    "direction": "LONG",
                    "timeframe": "15m",
                    "entry_time": "2026-08-11T01:17:00Z",
                    "entry_price": 54.91,
                    "available_at": "2026-08-11T01:15:00Z",
                    "created_at": "2026-08-11T01:15:00Z",
                }
            ],
            one_minute_by_symbol={"HYPEUSDT": []},
            skip_pin=True,
            publish=True,
        )
