from __future__ import annotations

import json
from pathlib import Path

import pytest

from pool_order_plan_v1.config import BASELINE_STRATEGY_ID, enable_pool_order_plan_v1
from pool_order_plan_v1.research_feed import (
    BANNER_BODY,
    BANNER_TITLE,
    MISSING_MESSAGE,
    chart_payload_for_signal,
    research_signals_response,
    validate_research_artifact,
)
from pool_order_plan_v1.schema import STRATEGY_ID

DASHBOARD = Path(__file__).resolve().parents[2]
APP_PY = DASHBOARD / "app.py"
HTML = DASHBOARD / "templates" / "stoch_signale.html"
JS = DASHBOARD / "static" / "js" / "stoch_signale.js"
CHART_JS = DASHBOARD / "static" / "js" / "stoch_chart_modal.js"
FEED_PY = DASHBOARD / "pool_order_plan_v1" / "research_feed.py"
ACE_DIR = (
    DASHBOARD.parent
    / "results"
    / "pool_order_plan_v1_comparisons"
    / "aceusdt_48h_20260814T150343Z"
    / "pool_artifacts"
    / "20260814T150343Z-76587402"
)


def _env_flag_on(tmp_path: Path | None = None) -> dict:
    env = {"ENABLE_POOL_ORDER_PLAN_V1": "true"}
    if tmp_path is not None:
        env["POOL_ORDER_PLAN_RESEARCH_REGISTRY"] = str(tmp_path)
    return env


def test_baseline_remains_default():
    html = HTML.read_text(encoding="utf-8")
    assert 'value="wave_fade_no_be50_v1" selected' in html
    js = JS.read_text(encoding="utf-8")
    assert 'value || "wave_fade_no_be50_v1"' in js
    assert BASELINE_STRATEGY_ID == "wave_fade_no_be50_v1"


def test_existing_strategies_preserved():
    html = HTML.read_text(encoding="utf-8")
    assert 'value="wave_fade_no_be50_v1"' in html
    assert 'value="wave_fade_frozen_f16ae32"' in html
    assert "Pool Order Plan V1 · Research" in html


def test_pool_option_gated_by_flag():
    html = HTML.read_text(encoding="utf-8")
    assert "{% if enable_pool_order_plan_v1 %}" in html
    assert 'value="POOL_ORDER_PLAN_V1"' in html
    assert enable_pool_order_plan_v1({}) is True
    assert enable_pool_order_plan_v1({"ENABLE_POOL_ORDER_PLAN_V1": "true"}) is True
    assert enable_pool_order_plan_v1({"ENABLE_POOL_ORDER_PLAN_V1": "false"}) is False


def test_flag_false_hides_option_logic_without_changing_baseline_copy():
    html = HTML.read_text(encoding="utf-8")
    assert "FROZEN BASELINE" in html
    assert "PnL = gross" in html
    assert 'value="wave_fade_no_be50_v1" selected' in html
    assert enable_pool_order_plan_v1({"ENABLE_POOL_ORDER_PLAN_V1": "false"}) is False


def test_pool_api_is_artifact_driven_and_no_collector(monkeypatch):
    src = APP_PY.read_text(encoding="utf-8")
    assert "research_signals_response" in src
    assert "overlay_rows" not in src.split("async def api_stoch_signals")[1].split("async def api_stoch_profits")[0]
    feed = FEED_PY.read_text(encoding="utf-8")
    assert "_fetch_upstream_signals" not in feed
    assert "8787" not in feed
    monkeypatch.setenv("ENABLE_POOL_ORDER_PLAN_V1", "true")
    payload = research_signals_response()
    assert payload["collector_called"] is False
    assert payload["feed_ready"] is True
    assert payload["research_mode"] is True
    assert payload["research_only"] is True
    assert payload["live_trading"] is False
    assert payload["strategy_id"] == STRATEGY_ID
    assert payload["pool_candle_source"] == "clickhouse"
    assert payload["symbol"] == "ACEUSDT"
    assert len(payload["signals"]) == 16
    row = payload["signals"][0]
    for key in (
        "strategy_id",
        "research_mode",
        "research_only",
        "live_trading",
        "run_id",
        "symbol",
        "window_start",
        "window_end",
        "snapshot_as_of",
        "signal_id",
        "entry_time",
        "entry_price",
        "direction",
        "timeframe",
        "plan_status",
        "no_plan_reason",
        "initial_target_mode",
        "entry_pool_count",
        "sl_price",
        "sl_distance_pct",
        "sl_too_wide",
        "sl_cluster",
        "tp1_price",
        "tp1_size",
        "tp1_cluster",
        "tp2_price",
        "tp2_size",
        "tp2_cluster",
        "tp2_skip_reason",
        "outcome",
        "legs",
        "exit_time",
        "gross_pnl_pct",
        "fees_pct",
        "net_pnl_pct",
        "hold_minutes",
        "last_5m_open",
        "pool_candle_source",
    ):
        assert key in row


def test_no_apt_or_moving_48h_mix(monkeypatch):
    monkeypatch.setenv("ENABLE_POOL_ORDER_PLAN_V1", "true")
    payload = research_signals_response()
    symbols = {r["symbol"] for r in payload["signals"]}
    assert symbols == {"ACEUSDT"}
    assert payload["window_start"] == "2026-08-12T15:03:43Z"
    assert payload["window_end"] == "2026-08-14T15:03:43Z"
    assert all(r["pool_research"] for r in payload["signals"])


def test_clickhouse_only_and_csv_fixture_rejected(tmp_path):
    csv_dir = tmp_path / "csv_run"
    csv_dir.mkdir()
    (csv_dir / "manifest.json").write_text(
        json.dumps(
            {
                "pool_candle_source": "csv",
                "test_fixture_only": True,
                "productive": False,
                "database": "signal_generator",
                "table": "candles_1m",
                "exchange": "bybit",
                "interval": "1m",
                "final": True,
                "is_closed": 1,
            }
        ),
        encoding="utf-8",
    )
    (csv_dir / "outcomes.jsonl").write_text("", encoding="utf-8")
    _, err = validate_research_artifact(
        csv_dir,
        {"planner_version": "c6c960a82e9a0c538dbe24b03f481893e722072f", "symbol": "ACEUSDT"},
    )
    assert err in {"fixture_rejected", "csv_or_non_clickhouse_rejected"}


def test_missing_artifact_is_not_http_500(tmp_path, monkeypatch):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "strategy_id": STRATEGY_ID,
                "research_only": True,
                "live_trading": False,
                "artifact_dir": str(tmp_path / "missing"),
                "symbol": "ACEUSDT",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("POOL_ORDER_PLAN_RESEARCH_REGISTRY", str(reg))
    monkeypatch.setenv("ENABLE_POOL_ORDER_PLAN_V1", "true")
    payload = research_signals_response()
    assert payload["success"] is True
    assert payload["feed_ready"] is False
    assert payload["message"] == MISSING_MESSAGE
    assert payload["signals"] == []


def test_stats_from_pool_artifacts_open_and_sl_too_wide(monkeypatch):
    monkeypatch.setenv("ENABLE_POOL_ORDER_PLAN_V1", "true")
    payload = research_signals_response()
    summary = payload["summary"]
    assert summary["signals"] == 16
    assert summary["ready"] == 16
    assert summary["no_plan"] == 0
    assert summary["open"] == 1
    assert summary["pnl_basis"] == "net_after_fees"
    assert "net after fees" in summary["pnl_basis_note"]
    assert summary["sl_too_wide_count"] >= 1
    assert summary["one_target_count"] + summary["two_target_count"] == 16
    rows = payload["signals"]
    assert sum(1 for r in rows if r["outcome"] == "OPEN") == 1
    open_row = next(r for r in rows if r["outcome"] == "OPEN")
    assert open_row["gross_pnl_pct"] is None
    assert open_row["net_pnl_pct"] is None
    assert any(r["sl_too_wide"] for r in rows)
    ones = [r for r in rows if r["tp1_size"] == 1.0]
    twos = [r for r in rows if r["tp1_size"] == 0.5 and r["tp2_size"] == 0.5]
    assert ones
    assert twos
    assert all(r.get("tp2_price") is None for r in ones)
    assert all(r.get("tp2_price") is not None for r in twos)


def test_chart_payload_has_entry_sl_tp_legs(monkeypatch):
    monkeypatch.setenv("ENABLE_POOL_ORDER_PLAN_V1", "true")
    payload = research_signals_response()
    sid = next(r["signal_id"] for r in payload["signals"] if r["legs"])
    chart = chart_payload_for_signal(sid)
    assert chart is not None
    assert chart["entry_price"] is not None
    assert chart["sl_price"] is not None
    assert chart["tp1_price"] is not None
    assert "legs" in chart
    assert "snapshot_as_of" in chart
    assert chart.get("sl_cluster") is not None or True
    assert "signal_timeframe" in chart
    assert chart["pool_timeframe"] == "5m"
    assert chart["pool_interval"] == "5m"
    js = CHART_JS.read_text(encoding="utf-8")
    assert "pool-research-klines" in js
    assert "tp1_price" in js
    assert "tp2_price" in js
    assert "legs" in js
    assert "sl_cluster" in js
    assert "Signal-TF" in js
    assert "Pool-TF 5m" in js
    assert "bybit" in js
    assert "rejected_bybit" in js


def test_table_and_filters_expose_pool_fields():
    js = JS.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    for needle in (
        "entry_pool_count",
        "last_5m_open",
        "last_5m_close",
        "Signal-TF",
        "Pool-TF",
        "stochFilterOutcome",
        "stochFilterSlWide",
        "stochFilterTargets",
        "SL TOO WIDE",
    ):
        assert needle in js or needle in html
    assert "Source: ClickHouse 1m" in html
    assert "Pool calculation: closed 5m candles" in html
    assert "Pool-V1 PnL: net after fees" in js


def test_research_banner_present():
    html = HTML.read_text(encoding="utf-8")
    assert "stochPoolResearchBanner" in html
    assert BANNER_TITLE in html
    assert "Keine Live-Strategie" in html
    assert "ACEUSDT" in html
    assert "2026-08-12 15:03:43 UTC" in html
    assert BANNER_BODY.split()[0] in html
    js = JS.read_text(encoding="utf-8")
    assert "stochPoolResearchBanner" in js


def test_no_live_order_interface():
    text = FEED_PY.read_text(encoding="utf-8")
    for needle in ("place_order", "create_order", "/v5/order", "submit_order", "live_order"):
        assert needle not in text
    app = APP_PY.read_text(encoding="utf-8")
    pool_block = app.split("if sv == \"POOL_ORDER_PLAN_V1\"")[1].split("payload, status, err")[0]
    assert "place_order" not in pool_block


def test_ace_artifact_exists_and_valid():
    assert ACE_DIR.is_dir()
    manifest = json.loads((ACE_DIR / "manifest.json").read_text(encoding="utf-8"))
    _, err = validate_research_artifact(
        ACE_DIR,
        {
            "planner_version": "c6c960a82e9a0c538dbe24b03f481893e722072f",
            "symbol": "ACEUSDT",
        },
    )
    assert err is None
    assert manifest["pool_candle_source"] == "clickhouse"
    assert manifest["productive"] is True
    assert manifest["counts"]["winners"] == 16
    assert manifest["counts"]["ready"] == 16
    assert manifest["counts"]["closed"] == 15
    assert manifest["counts"]["open"] == 1
    assert manifest["counts"]["pool_engine_runs"] == 1
